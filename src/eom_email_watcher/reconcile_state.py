"""Transactional coverage and frozen progress, owned by the reconcile pass."""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime

from .mailbox import MessageMetadata

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
            received_at TEXT, metadata TEXT CHECK(metadata IS NULL OR json_valid(metadata)),
            folder TEXT NOT NULL CHECK(folder IN ('inbox','sent','all')),
            done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)),
            PRIMARY KEY({KEY},generation,phase,unit,source_id))""",
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

    def observe(self, *, active: bool, retention_days: int, sent_folder: str, now=None):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                f"INSERT OR IGNORE INTO coverage_generation({KEY},generation) VALUES (?,?,?,0)",
                self.scope,
            )
            old = db.execute(
                f"SELECT * FROM coverage_generation WHERE {WHERE}", self.scope
            ).fetchone()
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

    def gap(self, *, now=None):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _bump_coverage_generation(db, self.scope, clear_watermarks=True)
            self.store._clear_location_stamps(db, *self.scope, now=now)

    def generation(self):
        with self.store.connection() as db:
            row = db.execute(
                f"SELECT generation FROM coverage_generation WHERE {WHERE}", self.scope
            ).fetchone()
            return row[0] if row else None

    def stale(self):
        with self.store.connection() as db:
            row = db.execute(
                f"""SELECT g.generation,c.generation AS reconciled
                FROM coverage_generation g LEFT JOIN reconcile_coverage c USING({KEY})
                WHERE g.provider=? AND g.account_id=? AND g.mailbox_identity_key=?""",
                self.scope,
            ).fetchone()
            return (
                row is None
                or row["generation"] != row["reconciled"]
                or bool(
                    db.execute(
                        f"""SELECT 1 FROM thread_follow f LEFT JOIN thread_watermarks w
                USING({KEY},thread_key) WHERE f.provider=? AND f.account_id=?
                AND f.mailbox_identity_key=? AND f.followed=1 AND w.thread_key IS NULL LIMIT 1""",
                        self.scope,
                    ).fetchone()
                )
            )

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
                row = None
            if row is None:
                # Query bounds have second precision; polling handles the tail.
                stamp = now.astimezone(UTC).replace(microsecond=0).isoformat()
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

    def metadata(self, progress, source_id, value=None):
        with self.store.connection() as db:
            if value is not None:
                data = asdict(value)
                for key in ("labels", "locations", "reply_ids", "to", "cc"):
                    data[key] = list(data[key])
                db.execute(
                    f"UPDATE reconcile_items SET metadata=? WHERE {WHERE} "
                    "AND generation=? AND source_id=?",
                    (json.dumps(data), *self.scope, progress["generation"], source_id),
                )
                return value
            row = db.execute(
                f"SELECT metadata FROM reconcile_items WHERE {WHERE} "
                "AND generation=? AND source_id=? AND metadata IS NOT NULL LIMIT 1",
                (*self.scope, progress["generation"], source_id),
            ).fetchone()
            if row is None:
                return None
            data = json.loads(row[0])
            for key in ("labels", "locations"):
                data[key] = frozenset(data[key])
            for key in ("reply_ids", "to", "cc"):
                data[key] = tuple(data[key])
            return MessageMetadata(**data)

    def sources(self, key):
        from .db import _effective_message_identity_sql

        with self.store.connection() as db:
            return [
                r[0]
                for r in db.execute(
                    f"""SELECT provider_message_id FROM logical_messages m
                    WHERE m.provider=? AND m.account_id=?
                    AND {_effective_message_identity_sql("m")}=? AND thread_key=?""",
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

    def complete(self, progress):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f" SELECT generation,version FROM reconcile_progress WHERE {WHERE}", self.scope
            ).fetchone()
            if row is None or tuple(row) != (progress["generation"], progress["version"]):
                raise RuntimeError("reconcile progress changed concurrently")
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
                f"DELETE FROM reconcile_progress WHERE {WHERE} AND generation=? AND version=?",
                (*self.scope, progress["generation"], progress["version"]),
            )
