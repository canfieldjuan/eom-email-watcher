"""Transactional coverage and frozen progress, owned by the reconcile pass."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

Scope = tuple[str, str, str]
KEY = "provider,account_id,mailbox_identity_key"
WHERE = "provider=? AND account_id=? AND mailbox_identity_key=?"


def ensure_schema(db: sqlite3.Connection) -> None:
    scope = "provider TEXT NOT NULL,account_id TEXT NOT NULL,mailbox_identity_key TEXT NOT NULL"
    statements = (
        f"""CREATE TABLE IF NOT EXISTS coverage_generation ({scope},
            generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation>=0),
            entitlement_active INTEGER CHECK(entitlement_active IN (0,1)),
            retention_days INTEGER CHECK(retention_days>0), sent_folder TEXT,
            PRIMARY KEY({KEY}))""",
        f"""CREATE TABLE IF NOT EXISTS reconcile_coverage ({scope},
            generation INTEGER NOT NULL CHECK(generation>=0), recorded_at TEXT NOT NULL,
            PRIMARY KEY({KEY}))""",
        f"""CREATE TABLE IF NOT EXISTS reconcile_progress ({scope},
            generation INTEGER NOT NULL, started_at TEXT NOT NULL,
            retention_days INTEGER NOT NULL CHECK(retention_days>0),
            version INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL CHECK(json_valid(payload)),
            PRIMARY KEY({KEY}))""",
        f"""CREATE TABLE IF NOT EXISTS reconcile_items ({scope},
            generation INTEGER NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('discovery','sync')),
            unit TEXT NOT NULL, source_id TEXT NOT NULL, native_thread_id TEXT,
            received_at TEXT,
            folder TEXT NOT NULL CHECK(folder IN ('inbox','sent','all')),
            done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)),
            PRIMARY KEY({KEY},generation,phase,unit,source_id))""",
        f"""CREATE TABLE IF NOT EXISTS reconcile_catchup ({scope},
            generation INTEGER NOT NULL,thread_key TEXT NOT NULL,target TEXT NOT NULL,
            PRIMARY KEY({KEY},generation,thread_key),
            FOREIGN KEY({KEY},thread_key) REFERENCES thread_follow({KEY},thread_key)
            ON DELETE CASCADE)""",
        """CREATE TRIGGER IF NOT EXISTS coverage_generation_monotonic
            BEFORE UPDATE ON coverage_generation WHEN NEW.generation<OLD.generation
            BEGIN SELECT RAISE(ABORT,'coverage generation cannot move backwards'); END""",
        """CREATE TRIGGER IF NOT EXISTS reconcile_progress_frozen
            BEFORE UPDATE ON reconcile_progress WHEN
              NEW.generation<>OLD.generation OR NEW.started_at<>OLD.started_at OR
              NEW.retention_days<>OLD.retention_days OR NEW.version<>OLD.version+1
            BEGIN SELECT RAISE(ABORT,'reconcile bounds are frozen'); END""",
    )
    for statement in statements:
        db.execute(statement)
    db.execute(f"""INSERT OR IGNORE INTO coverage_generation({KEY},generation)
        SELECT {KEY},0 FROM mail_accounts WHERE mailbox_identity_key IS NOT NULL""")


def record_follow_transition(db, scope):
    """Called only by D-derived, before the follow-input transaction commits."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='reconcile_catchup'").fetchone():
        return
    row = db.execute(
        f"SELECT generation FROM reconcile_progress WHERE {WHERE}", scope[:3]
    ).fetchone()
    if row is not None:
        db.execute(
            f"""INSERT INTO reconcile_catchup({KEY},generation,thread_key,target)
            VALUES (?,?,?,?,?,?) ON CONFLICT({KEY},generation,thread_key)
            DO UPDATE SET target=MAX(target,excluded.target)""",
            (*scope[:3], row[0], scope[3], datetime.now(UTC).isoformat()),
        )


def _bump_coverage_generation(db, scope: Scope, *, amount: int = 1, clear_watermarks=False):
    """The only counter writer; caller's input transaction owns its effects."""
    db.execute(
        f"INSERT OR IGNORE INTO coverage_generation({KEY},generation) VALUES (?,?,?,0)", scope
    )
    db.execute(
        f"UPDATE coverage_generation SET generation=generation+? WHERE {WHERE}", (amount, *scope)
    )
    if clear_watermarks:
        db.execute(f"DELETE FROM thread_watermarks WHERE {WHERE}", scope)


def coverage_input_sql(table: str, event: str) -> str:
    """Append row evidence to the existing D-derived input triggers."""
    if table not in {"vendors", "vendor_addresses", "vendor_domains", "mail_accounts"}:
        return ""
    condition = "1"
    if event == "UPDATE":
        if table == "mail_accounts":
            condition = (
                "OLD.address IS NOT NEW.address "
                "OR OLD.mailbox_identity_key IS NOT NEW.mailbox_identity_key"
            )
        elif table == "vendors":
            condition = "OLD.display_name IS NOT NEW.display_name"
        elif table == "vendor_addresses":
            condition = "OLD.address IS NOT NEW.address OR OLD.vendor_id IS NOT NEW.vendor_id"
        else:
            condition = "OLD.domain IS NOT NEW.domain OR OLD.vendor_id IS NOT NEW.vendor_id"
    return f"""INSERT INTO coverage_changed_accounts({KEY},amount)
        SELECT {KEY},1 FROM mail_accounts WHERE mailbox_identity_key IS NOT NULL AND ({condition})
        ON CONFLICT({KEY}) DO UPDATE SET amount=amount+1;"""


def track_coverage_inputs(db) -> bool:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='coverage_generation'").fetchone():
        return False
    db.execute(
        f"CREATE TEMP TABLE coverage_changed_accounts({KEY},amount INTEGER,PRIMARY KEY({KEY}))"
    )
    return True


def flush_coverage_inputs(db) -> None:
    if db.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE name='coverage_changed_accounts'"
    ).fetchone():
        for row in db.execute(f"SELECT {KEY},amount FROM coverage_changed_accounts").fetchall():
            _bump_coverage_generation(db, tuple(row)[:3], amount=row[3])
        db.execute("DELETE FROM coverage_changed_accounts")


class ReconcileState:
    def __init__(self, store, scope: Scope):
        self.store = store
        self.scope = scope

    def observe(self, *, active: bool, retention_days: int, sent_folder: str | None, now=None):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                f"INSERT OR IGNORE INTO coverage_generation({KEY},generation) VALUES (?,?,?,0)",
                self.scope,
            )
            old = db.execute(
                f"SELECT * FROM coverage_generation WHERE {WHERE}", self.scope
            ).fetchone()
            if sent_folder is None:
                sent_folder = old["sent_folder"]
            changed = (old["entitlement_active"], old["retention_days"], old["sent_folder"]) != (
                int(active),
                retention_days,
                sent_folder,
            )
            if not changed:
                return
            gap = old["entitlement_active"] == 0 and active
            clear = (
                gap
                or old["sent_folder"] != sent_folder
                or (old["retention_days"] is not None and retention_days > old["retention_days"])
            )
            _bump_coverage_generation(db, self.scope, clear_watermarks=clear)
            db.execute(
                f"""UPDATE coverage_generation SET entitlement_active=?,retention_days=?,
                sent_folder=? WHERE {WHERE}""",
                (int(active), retention_days, sent_folder, *self.scope),
            )
            if gap:
                # The same source-observation owner as polling; no stamp assignment here.
                self.store._clear_location_stamps(db, *self.scope, now=now)

    def gap(self, *, now=None, db=None):
        if db is not None:
            _bump_coverage_generation(db, self.scope, clear_watermarks=True)
            self.store._clear_location_stamps(db, *self.scope, now=now)
            return
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.gap(now=now, db=db)

    def generation(self):
        with self.store.connection() as db:
            row = db.execute(
                f"SELECT generation FROM coverage_generation WHERE {WHERE}", self.scope
            ).fetchone()
            return row[0] if row else None

    def stale(self):
        with self.store.connection() as db:
            db.execute("BEGIN")
            return not coverage_current(db, self.scope, now=datetime.now(UTC))

    def needs_discovery(self):
        with self.store.connection() as db:
            row = db.execute(
                f"""SELECT g.generation,c.generation FROM coverage_generation g
                LEFT JOIN reconcile_coverage c USING({KEY}) WHERE g.provider=?
                AND g.account_id=? AND g.mailbox_identity_key=?""",
                self.scope,
            ).fetchone()
            return row is None or row[0] != row[1]

    def load_or_start(self, now: datetime, retention_days: int, payload):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            generation = db.execute(
                f"SELECT generation FROM coverage_generation WHERE {WHERE}", self.scope
            ).fetchone()[0]
            row = db.execute(
                f"SELECT * FROM reconcile_progress WHERE {WHERE}", self.scope
            ).fetchone()
            if row is not None and row["generation"] != generation:
                db.execute(f"DELETE FROM reconcile_progress WHERE {WHERE}", self.scope)
                db.execute(f"DELETE FROM reconcile_items WHERE {WHERE}", self.scope)
                db.execute(f"DELETE FROM reconcile_catchup WHERE {WHERE}", self.scope)
                row = None
            if row is None:
                stamp = now.astimezone(UTC).isoformat()
                db.execute(
                    f"""INSERT INTO reconcile_progress(
                    {KEY},generation,started_at,retention_days,payload)
                    VALUES (?,?,?,?,?,?,?)""",
                    (*self.scope, generation, stamp, retention_days, json.dumps(payload)),
                )
                row = db.execute(
                    f"SELECT * FROM reconcile_progress WHERE {WHERE}", self.scope
                ).fetchone()
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def save(self, progress):
        with self.store.connection() as db:
            cursor = db.execute(
                f"""UPDATE reconcile_progress SET payload=?,version=version+1
                WHERE {WHERE} AND generation=? AND version=?""",
                (
                    json.dumps(progress["payload"]),
                    *self.scope,
                    progress["generation"],
                    progress["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("reconcile progress changed concurrently")
            progress["version"] += 1

    def put_items(self, progress, phase, unit, items, folder):
        with self.store.connection() as db:
            db.executemany(
                f"""INSERT OR IGNORE INTO reconcile_items(
                {KEY},generation,phase,unit,source_id,native_thread_id,received_at,folder)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        *self.scope,
                        progress["generation"],
                        phase,
                        unit,
                        i.message_id,
                        i.thread_id,
                        i.received_at,
                        folder,
                    )
                    for i in items
                ],
            )

    def pending(self, progress, phase, unit):
        with self.store.connection() as db:
            row = db.execute(
                f"""SELECT * FROM reconcile_items WHERE {WHERE}
                AND generation=? AND phase=? AND unit=? AND done=0 ORDER BY source_id LIMIT 1""",
                (*self.scope, progress["generation"], phase, unit),
            ).fetchone()
            return dict(row) if row else None

    def finish_item(self, progress, item):
        with self.store.connection() as db:
            db.execute(
                f"""UPDATE reconcile_items SET done=1 WHERE {WHERE}
                AND generation=? AND phase=? AND unit=? AND source_id=?""",
                (
                    *self.scope,
                    progress["generation"],
                    item["phase"],
                    item["unit"],
                    item["source_id"],
                ),
            )

    def catchup_targets(self, progress, *, db=None):
        if db is None:
            with self.store.connection() as db:
                return self.catchup_targets(progress, db=db)
        return dict(
            db.execute(
                f"""SELECT c.thread_key,c.target FROM reconcile_catchup c
            JOIN thread_follow f USING({KEY},thread_key)
            WHERE c.provider=? AND c.account_id=? AND c.mailbox_identity_key=?
            AND c.generation=? AND f.followed=1""",
                (*self.scope, progress["generation"]),
            )
        )

    def next_interval(self, progress, now, payload):
        """Replace an exhausted interval without certifying its catch-up gap."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute(
                f"DELETE FROM reconcile_progress WHERE {WHERE} AND generation=? AND version=?",
                (*self.scope, progress["generation"], progress["version"]),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("reconcile progress changed concurrently")
            db.execute(f"DELETE FROM reconcile_items WHERE {WHERE}", self.scope)
            db.execute(
                f"""INSERT INTO reconcile_progress(
                {KEY},generation,started_at,retention_days,payload)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    *self.scope,
                    progress["generation"],
                    now.astimezone(UTC).isoformat(),
                    progress["retention_days"],
                    json.dumps(payload),
                ),
            )
            row = db.execute(
                f"SELECT * FROM reconcile_progress WHERE {WHERE}", self.scope
            ).fetchone()
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def sources(self, key):
        from .db import _effective_message_identity_sql

        with self.store.connection() as db:
            return [
                r[0]
                for r in db.execute(
                    f"""SELECT provider_message_id FROM logical_messages m
                    WHERE m.provider=?1 AND m.account_id=?2
                    AND {_effective_message_identity_sql("m")}=?3 AND thread_key=?4
                    UNION SELECT l.provider_message_id FROM message_locations l
                    JOIN messages m USING(message_id)
                    WHERE l.provider=?1 AND l.account_id=?2 AND l.mailbox_identity_key=?3
                    AND m.thread_key=?4""",
                    (*self.scope, key),
                )
            ]

    def threads(self):
        from .db import _effective_message_identity_sql

        with self.store.connection() as db:
            db.execute("BEGIN")
            result = []
            for row in db.execute(
                f"SELECT * FROM thread_follow WHERE {WHERE} AND followed=1", self.scope
            ):
                key = row["thread_key"]
                native = tuple(
                    r[0]
                    for r in db.execute(
                        f"""SELECT DISTINCT thread_id FROM logical_messages
                    WHERE provider=? AND account_id=?
                    AND {_effective_message_identity_sql("logical_messages")}=?
                    AND thread_key=? AND thread_id IS NOT NULL""",
                        (*self.scope, key),
                    )
                )
                ids = tuple(
                    r[0]
                    for r in db.execute(
                        f"""SELECT rfc_id FROM imap_thread_ids
                    WHERE {WHERE} AND thread_key=? ORDER BY rfc_id""",
                        (*self.scope, key),
                    )
                )
                watermark = db.execute(
                    f"SELECT synced_through FROM thread_watermarks WHERE {WHERE} AND thread_key=?",
                    (*self.scope, key),
                ).fetchone()
                aliases = db.execute(
                    "SELECT count(*) FROM thread_key_aliases WHERE survivor_key=?", (key,)
                ).fetchone()[0]
                result.append(
                    dict(
                        key=key,
                        native=list(native),
                        ids=list(ids),
                        aliases=aliases,
                        watermark=watermark[0] if watermark else None,
                    )
                )
            return result

    def watermark(self, progress, thread):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            followed = db.execute(
                f"SELECT followed FROM thread_follow WHERE {WHERE} AND thread_key=?",
                (*self.scope, thread["key"]),
            ).fetchone()
            aliases = db.execute(
                "SELECT count(*) FROM thread_key_aliases WHERE survivor_key=?", (thread["key"],)
            ).fetchone()[0]
            if followed is None or not followed[0] or aliases != thread["aliases"]:
                return False
            db.execute(
                f"""INSERT INTO thread_watermarks({KEY},thread_key,synced_through)
                VALUES (?,?,?,?,?) ON CONFLICT({KEY},thread_key) DO UPDATE SET
                synced_through=MAX(synced_through,excluded.synced_through)""",
                (*self.scope, thread["key"], progress["started_at"]),
            )
            return True

    def catchup_pending(self, progress, *, db=None):
        if db is None:
            with self.store.connection() as db:
                return self.catchup_pending(progress, db=db)
        for key, target in self.catchup_targets(progress, db=db).items():
            row = db.execute(
                f"""SELECT w.synced_through FROM thread_follow f
                LEFT JOIN thread_watermarks w USING({KEY},thread_key)
                WHERE f.provider=? AND f.account_id=? AND f.mailbox_identity_key=?
                AND f.thread_key=? AND f.followed=1""",
                (*self.scope, key),
            ).fetchone()
            if row is not None and (row[0] is None or row[0] < target):
                return True
        return False

    def complete(self, progress):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.catchup_pending(progress, db=db):
                raise RuntimeError("reconcile catch-up remains pending")
            row = db.execute(
                f" SELECT generation,version FROM reconcile_progress WHERE {WHERE}", self.scope
            ).fetchone()
            if row is None or tuple(row) != (progress["generation"], progress["version"]):
                raise RuntimeError("reconcile progress changed concurrently")
            from . import body_state

            start = datetime.fromisoformat(progress["started_at"])
            if body_state.pending(
                db, self.scope, cutoff=start - timedelta(days=progress["retention_days"]),
                now=start, limit=1,
            ):
                raise RuntimeError("reconcile body work remains pending")
            db.execute(
                f"""INSERT INTO reconcile_coverage({KEY},generation,recorded_at) VALUES (?,?,?,?,?)
                ON CONFLICT({KEY}) DO UPDATE SET generation=excluded.generation,
                recorded_at=excluded.recorded_at
                WHERE reconcile_coverage.generation<=excluded.generation""",
                (*self.scope, progress["generation"], datetime.now(UTC).isoformat()),
            )
            db.execute(
                f"DELETE FROM reconcile_items WHERE {WHERE} AND generation=?",
                (*self.scope, progress["generation"]),
            )
            db.execute(
                f"DELETE FROM reconcile_catchup WHERE {WHERE} AND generation=?",
                (*self.scope, progress["generation"]),
            )
            db.execute(
                f"DELETE FROM reconcile_progress WHERE {WHERE} AND generation=? AND version=?",
                (*self.scope, progress["generation"], progress["version"]),
            )


def coverage_current(db, scope, *, now, retention_days=None):
    """The single current-coverage predicate, shared with destructive retention."""
    from . import body_state

    row = db.execute(
        f"""SELECT g.generation,c.generation AS reconciled,g.retention_days
        FROM coverage_generation g LEFT JOIN reconcile_coverage c USING({KEY})
        WHERE g.provider=? AND g.account_id=? AND g.mailbox_identity_key=?""",
        scope,
    ).fetchone()
    if row is None or row["generation"] != row["reconciled"]:
        return False
    if db.execute(f"SELECT 1 FROM reconcile_progress WHERE {WHERE}", scope).fetchone():
        return False
    if db.execute(
        f"""SELECT 1 FROM thread_follow f LEFT JOIN thread_watermarks w
        USING({KEY},thread_key) WHERE f.provider=? AND f.account_id=? AND f.mailbox_identity_key=?
        AND f.followed=1 AND w.thread_key IS NULL LIMIT 1""",
        scope,
    ).fetchone():
        return False
    days = retention_days if retention_days is not None else row["retention_days"]
    return not body_state.pending(db, scope, cutoff=now - timedelta(days=days), now=now, limit=1)
