"""Local body state; D-derived owns invalidation and reconcile owns fetching."""

import json

from .mime import MAX_STORED_BODY_CHARS


def ensure_schema(db):
    for statement in (
        f"""CREATE TABLE IF NOT EXISTS message_bodies (
            message_id TEXT PRIMARY KEY, text TEXT NOT NULL,
            stored_chars INTEGER NOT NULL CHECK(
                stored_chars BETWEEN 0 AND {MAX_STORED_BODY_CHARS}),
            source_chars INTEGER NOT NULL CHECK(source_chars>=stored_chars),
            fetched_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS message_body_unavailable (
            message_id TEXT PRIMARY KEY, reason TEXT NOT NULL
            CHECK(reason IN ('source_gone','outside_folders')))""",
        """CREATE TABLE IF NOT EXISTS maintenance_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS message_bodies_checkpoint
            AFTER DELETE ON message_bodies BEGIN
            INSERT INTO maintenance_state VALUES ('checkpoint_pending',lower(hex(randomblob(16))),
                strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at;
            END""",
    ):
        db.execute(statement)


def pending(db, scope, *, cutoff, now, limit=None, exclude=()):
    from .db import _effective_message_identity_sql, _logical_messages_past_cutoff_sql

    expired, parameters = _logical_messages_past_cutoff_sql(
        cutoff.timestamp(), now.timestamp(), include_recovery=False
    )
    return [
        str(row[0])
        for row in db.execute(
            f"""SELECT m.message_id FROM logical_messages m JOIN thread_follow f
        ON f.provider=m.provider AND f.account_id=m.account_id
        AND f.mailbox_identity_key={_effective_message_identity_sql("m")}
        AND f.thread_key=m.thread_key
        WHERE f.provider=? AND f.account_id=? AND f.mailbox_identity_key=? AND f.followed=1
        AND m.message_id NOT IN (SELECT message_id FROM message_bodies)
        AND m.message_id NOT IN (SELECT message_id FROM message_body_unavailable)
        AND m.message_id NOT IN ({expired})
        AND m.message_id NOT IN (SELECT value FROM json_each(?))
        ORDER BY m.message_id LIMIT ?""",
            (*scope, *parameters, json.dumps(list(exclude)), -1 if limit is None else limit),
        )
    ]


def sources(db, message_id, scope, folders):
    placeholders = ",".join("?" for _ in folders)
    return [
        list(r)
        for r in db.execute(
            f"""SELECT l.provider_message_id, COALESCE(o.folder_observed_at, '')
        FROM message_locations l LEFT JOIN message_source_observations o
        USING(provider,account_id,mailbox_identity_key,provider_message_id)
        WHERE l.message_id=? AND l.provider=? AND l.account_id=? AND l.mailbox_identity_key=?
        AND l.location IN ({placeholders}) GROUP BY l.provider_message_id
        ORDER BY l.provider_message_id""",
            (message_id, *scope, *sorted(folders)),
        )
    ]


def record(store, message_id, body, *, fetched_at, reason=None):
    from . import removal
    from .db import _effective_message_identity_sql

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        # A refollow cannot revive content already accepted for body-only removal.
        if not removal.message_available(db, message_id, include_unfollow=True):
            return
        # The derived input owner may have deleted or unfollowed this source.
        present = db.execute(
            f"""SELECT 1 FROM logical_messages m JOIN thread_follow f
            ON f.provider=m.provider AND f.account_id=m.account_id
            AND f.mailbox_identity_key={_effective_message_identity_sql("m")}
        AND f.thread_key=m.thread_key
            WHERE m.message_id=? AND f.followed=1""",
            (message_id,),
        ).fetchone()
        if present is None:
            return
        if reason is None:
            text, source_chars = body
            db.execute("DELETE FROM message_body_unavailable WHERE message_id=?", (message_id,))
            db.execute(
                "INSERT OR REPLACE INTO message_bodies VALUES (?,?,?,?,?)",
                (message_id, text, len(text), source_chars, fetched_at.isoformat()),
            )
        else:
            db.execute("DELETE FROM message_bodies WHERE message_id=?", (message_id,))
            db.execute(
                "INSERT OR REPLACE INTO message_body_unavailable VALUES (?,?)", (message_id, reason)
            )


def recompute(db):
    from . import removal, source_locks
    from .db import SOURCE_CLEANUP_LOCK_BATCH_SIZE, _effective_message_identity_sql, _unit_rows

    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='message_bodies'").fetchone():
        return
    eligible = f"""SELECT m.message_id FROM logical_messages m JOIN thread_follow f
        ON f.provider=m.provider AND f.account_id=m.account_id
        AND f.mailbox_identity_key={_effective_message_identity_sql("m")}
        AND f.thread_key=m.thread_key
        WHERE f.followed=1"""
    # A durable target remains physical until the completion owner acquires its
    # source locks; hiding it from the projection is not deletion authority.
    held = []
    if removal.has_schema(db):
        eligible_content = removal.available_sql("m", include_unfollow=True)
        held = [r[0] for r in db.execute(
            f"SELECT m.message_id FROM messages m WHERE NOT ({eligible_content})"
        )]
    # This runs in the same write transaction as follow/attribution, so the
    # protocol marker cannot change between the admission read and cleanup.
    ready = source_locks.protocol_ready(db)
    if not ready:
        for (message_id,) in db.execute(
            f"SELECT message_id FROM message_bodies WHERE message_id NOT IN ({eligible}) "
            f"UNION SELECT message_id FROM message_body_unavailable "
            f"WHERE message_id NOT IN ({eligible})"
        ).fetchall():
            if source_locks.cleanup_held(
                ready=ready, row_count=len(_unit_rows(db, (message_id,))),
                legacy_limit=SOURCE_CLEANUP_LOCK_BATCH_SIZE,
            ):
                held.append(message_id)
    db._held_body_cleanup = tuple(held)
    for table in ("message_bodies", "message_body_unavailable"):
        db.execute(
            f"DELETE FROM {table} WHERE message_id NOT IN ({eligible}) "
            "AND message_id NOT IN (SELECT value FROM json_each(?))",
            (json.dumps(held),),
        )
    if db.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE name='derived_returned_sources'"
    ).fetchone():
        db.execute(
            "DELETE FROM message_body_unavailable WHERE message_id IN "
            "(SELECT message_id FROM derived_returned_sources)"
        )


def reparent(db, old, new):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='message_bodies'").fetchone():
        return
    for table in ("message_bodies", "message_body_unavailable"):
        db.execute(f"UPDATE OR IGNORE {table} SET message_id=? WHERE message_id=?", (new, old))
    if db.execute("SELECT 1 FROM message_bodies WHERE message_id=?", (new,)).fetchone():
        db.execute("DELETE FROM message_body_unavailable WHERE message_id=?", (new,))


def note_returned_source(db, message_id):
    db.execute(
        "CREATE TEMP TABLE IF NOT EXISTS derived_returned_sources(message_id TEXT PRIMARY KEY)"
    )
    db.execute("INSERT OR IGNORE INTO derived_returned_sources VALUES (?)", (message_id,))


def checkpoint(store):
    """Only clear the deletion generation actually covered by this checkpoint."""
    with store.connection() as db:
        row = db.execute(
            "SELECT value FROM maintenance_state WHERE key='checkpoint_pending'"
        ).fetchone()
        if row is None:
            return True
        token = row[0]
        busy, frames, checkpointed = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy or frames != checkpointed:
            return False
        db.execute(
            "DELETE FROM maintenance_state WHERE key='checkpoint_pending' AND value=?", (token,)
        )
    return True
