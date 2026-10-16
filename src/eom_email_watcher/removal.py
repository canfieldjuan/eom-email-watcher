"""D-removal's durable request, bound target, and eligibility owner."""

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from connect_automate.locking import OperationLockBusy

KEY = re.compile(r"rm1\.([1-9][0-9]{0,11})\.([0-9a-f]{32})\.([0-9a-f]{64})\Z")
SIGNING_KEY = "removal_request_signing_v1"


class RemovalRequestInvalid(ValueError):
    pass


@dataclass(frozen=True)
class Outcome:
    request_id: str
    intent_ids: tuple[str, ...]
    kind: str
    requested: int
    completed: int

    @property
    def pending(self):
        return self.requested - self.completed

    def data(self):
        return {
            "request_id": self.request_id,
            "intent_ids": list(self.intent_ids),
            "outcome": "deferred" if self.pending else "completed",
            "requested": self.requested,
            "completed": self.completed,
            "pending": self.pending,
            "message": "Deletion pending until the local upgrade completes"
            if self.pending
            else None,
        }


def request_hash(kind, parameters):
    if (
        not isinstance(kind, str)
        or kind not in {"delete", "clear", "unfollow"}
        or not isinstance(parameters, dict)
    ):
        raise RemovalRequestInvalid("Invalid local removal request")
    try:
        payload = json.dumps(
            {"kind": kind, "parameters": parameters},
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as exc:
        raise RemovalRequestInvalid("Invalid local removal parameters") from exc
    return hashlib.sha256(payload).hexdigest()


def _signature(db, unsigned, kind, parameters_sha256):
    row = db.execute("SELECT value FROM maintenance_state WHERE key=?", (SIGNING_KEY,)).fetchone()
    if row is None or not re.fullmatch(r"[0-9a-f]{64}", row[0]):
        raise RemovalRequestInvalid("Local removal key authority is unavailable")
    message = f"{unsigned}:{kind}:{parameters_sha256}".encode()
    return hmac.new(bytes.fromhex(row[0]), message, hashlib.sha256).hexdigest()


def issue_key(store, kind, parameters, *, retention_days, now=None):
    with store.connection() as db:
        return _issue_key(db, kind, parameters, retention_days=retention_days, now=now)


def _issue_key(db, kind, parameters, *, retention_days, now=None):
    if type(retention_days) is not int or retention_days < 1:
        raise RemovalRequestInvalid("Invalid removal retention interval")
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        expires = int((stamp + timedelta(days=retention_days)).timestamp())
    except OverflowError as exc:
        raise RemovalRequestInvalid("Invalid removal retention interval") from exc
    unsigned = f"rm1.{expires}.{secrets.token_hex(16)}"
    fingerprint = request_hash(kind, parameters)
    signature = _signature(db, unsigned, kind, fingerprint)
    return f"{unsigned}.{signature}"


def _parse_key(db, key, kind, fingerprint):
    if not isinstance(key, str) or (match := KEY.fullmatch(key)) is None:
        raise RemovalRequestInvalid("Malformed local removal request key")
    unsigned = key.rsplit(".", 1)[0]
    if not hmac.compare_digest(match[3], _signature(db, unsigned, kind, fingerprint)):
        raise RemovalRequestInvalid("Local removal request key does not match its parameters")
    return int(match[1])


def validate_key(db, key, kind, fingerprint, *, now):
    if _parse_key(db, key, kind, fingerprint) <= now.timestamp():
        raise RemovalRequestInvalid("Local removal request key has expired; start a new request")


def outcome(db, key, *, kind=None, fingerprint=None):
    request = db.execute(
        "SELECT kind,parameters_sha256 FROM message_removal_requests WHERE request_id=?",
        (key,),
    ).fetchone()
    if request is None:
        return None
    if (kind is not None and request[0] != kind) or (
        fingerprint is not None and not hmac.compare_digest(request[1], fingerprint)
    ):
        raise RemovalRequestInvalid(
            "Removal request key was already used with different parameters"
        )
    children = db.execute(
        "SELECT intent_id,requested_count,completed_count FROM message_removal_intents "
        "WHERE request_id=? ORDER BY intent_id",
        (key,),
    ).fetchall()
    return Outcome(
        key,
        tuple(r[0] for r in children),
        request[0],
        sum(r[1] for r in children),
        sum(r[2] for r in children),
    )


def status(store, key, *, kind=None, parameters=None, now=None):
    """Read one frozen receipt snapshot; never select targets or resume removal."""
    fingerprint = request_hash(kind, parameters) if kind is not None else None
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    with store.connection() as db:
        db.execute("BEGIN")
        result = outcome(db, key, kind=kind, fingerprint=fingerprint)
        if result is not None:
            return result
        if kind is None:
            raise RemovalRequestInvalid("Local removal request has not been accepted")
        validate_key(db, key, kind, fingerprint, now=stamp)
        return None


def retire(store, retention_days, *, now):
    """Retire complete request groups through ordinary retention maintenance."""
    cutoff = now - timedelta(days=retention_days)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not has_schema(db):
            return
        requests = db.execute("SELECT * FROM message_removal_requests").fetchall()
        for request in requests:
            expiry = _parse_key(
                db, request["request_id"], request["kind"], request["parameters_sha256"]
            )
            if expiry > now.timestamp():
                continue
            children = db.execute(
                "SELECT state,settled_at FROM message_removal_intents WHERE request_id=?",
                (request["request_id"],),
            ).fetchall()
            if datetime.fromisoformat(request["accepted_at"]) > cutoff or any(
                child["state"] != "completed"
                or datetime.fromisoformat(child["settled_at"]) > cutoff
                for child in children
            ):
                continue
            db.execute(
                "DELETE FROM message_removal_intents WHERE request_id=?", (request["request_id"],)
            )
            db.execute(
                "DELETE FROM message_removal_requests WHERE request_id=?", (request["request_id"],)
            )


def ensure_schema(db):
    for statement in (
        """CREATE TABLE IF NOT EXISTS message_removal_requests (
            request_id TEXT PRIMARY KEY CHECK(length(request_id) BETWEEN 1 AND 256),
            kind TEXT NOT NULL CHECK(kind IN ('delete','clear','unfollow')),
            parameters_sha256 TEXT NOT NULL CHECK(length(parameters_sha256)=64),
            accepted_at TEXT NOT NULL CHECK(accepted_at<>''),
            UNIQUE(request_id,kind))""",
        """CREATE TABLE IF NOT EXISTS message_removal_intents (
            intent_id TEXT PRIMARY KEY CHECK(length(intent_id)=36),
            request_id TEXT NOT NULL, kind TEXT NOT NULL,
            provider TEXT NOT NULL CHECK(provider<>''),
            account_id TEXT NOT NULL CHECK(account_id<>''),
            mailbox_identity_key TEXT CHECK(
                mailbox_identity_key IS NULL OR mailbox_identity_key<>''),
            accepted_at TEXT NOT NULL CHECK(accepted_at<>''),
            state TEXT NOT NULL CHECK(state IN ('pending','completed')),
            requested_count INTEGER NOT NULL CHECK(requested_count>0),
            completed_count INTEGER NOT NULL CHECK(completed_count>=0),
            settled_at TEXT,
            CHECK(completed_count<=requested_count),
            CHECK((state='completed')=(completed_count=requested_count)),
            CHECK((state='completed')=(settled_at IS NOT NULL)),
            UNIQUE(intent_id,provider,account_id),
            FOREIGN KEY(request_id,kind) REFERENCES message_removal_requests(request_id,kind))""",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_removal_intent_binding
            ON message_removal_intents(request_id,provider,account_id,mailbox_identity_key)
            WHERE mailbox_identity_key IS NOT NULL""",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_removal_intent_legacy_binding
            ON message_removal_intents(request_id,provider,account_id)
            WHERE mailbox_identity_key IS NULL""",
        """CREATE TABLE IF NOT EXISTS message_removal_targets (
            intent_id TEXT NOT NULL, logical_target_id TEXT NOT NULL,
            message_id TEXT NOT NULL CHECK(message_id<>''),
            provider TEXT NOT NULL, account_id TEXT NOT NULL,
            stored_identity_key TEXT,
            provider_message_id TEXT NOT NULL CHECK(provider_message_id<>''),
            source_identities_json TEXT NOT NULL CHECK(
                json_valid(source_identities_json) AND json_type(source_identities_json)='array'),
            state TEXT NOT NULL CHECK(state IN ('pending','completed')),
            settled_at TEXT,
            CHECK((state='completed')=(settled_at IS NOT NULL)),
            PRIMARY KEY(intent_id,message_id),
            FOREIGN KEY(intent_id,provider,account_id)
                REFERENCES message_removal_intents(intent_id,provider,account_id)
                ON DELETE CASCADE)""",
        """CREATE INDEX IF NOT EXISTS idx_removal_pending_source
            ON message_removal_targets(state,message_id,provider,account_id,
                stored_identity_key,provider_message_id)""",
        """CREATE INDEX IF NOT EXISTS idx_removal_logical_target
            ON message_removal_targets(intent_id,logical_target_id,state)""",
        """CREATE INDEX IF NOT EXISTS idx_removal_pending_intents
            ON message_removal_intents(state,accepted_at,intent_id)""",
    ):
        db.execute(statement)
    db.execute(
        "INSERT OR IGNORE INTO maintenance_state(key,value,updated_at) VALUES(?,?,?)",
        (SIGNING_KEY, secrets.token_hex(32), datetime.now(UTC).isoformat()),
    )


def has_schema(db):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_removal_targets'"
        ).fetchone()
        is not None
    )


def available_sql(alias, *, include_unfollow=False):
    """The one frozen-target predicate, for stored rows and candidate sources.

    Aliases are static SQL names supplied by owners, never request input. Stable
    stored identity and frozen source membership survive canonical promotion;
    neither gives an old request authority over a reused id in another binding.
    A legacy source can lack a stored identity and location; its intent keeps
    the effective binding for later provider starts, even after the row is gone.
    """
    kinds = "1" if include_unfollow else "i.kind IN ('delete','clear')"
    return f"""NOT EXISTS (
        SELECT 1 FROM message_removal_targets t
        JOIN message_removal_intents i ON i.intent_id=t.intent_id
        WHERE t.state='pending' AND i.state='pending' AND {kinds}
        AND t.provider={alias}.provider AND t.account_id={alias}.account_id
        AND (
            (t.message_id={alias}.message_id
             AND t.stored_identity_key IS {alias}.mailbox_identity_key
             AND t.provider_message_id={alias}.provider_message_id)
            OR (t.provider_message_id={alias}.provider_message_id
                AND i.mailbox_identity_key IS {alias}.mailbox_identity_key)
            OR EXISTS (SELECT 1 FROM json_each(t.source_identities_json) s
                WHERE json_extract(s.value,'$.provider')={alias}.provider
                AND json_extract(s.value,'$.account_id')={alias}.account_id
                AND json_extract(s.value,'$.mailbox_identity_key')
                    IS {alias}.mailbox_identity_key
                AND json_extract(s.value,'$.provider_message_id')={alias}.provider_message_id)
        ))"""


def retained_available_sql(alias, *, message_id_field="message_id", include_unfollow=False):
    """Apply target eligibility to a live or frozen, retained source binding."""
    bound_id = f"{alias}.{message_id_field}"
    live = f"""live.message_id={bound_id} AND live.provider={alias}.provider
        AND live.account_id={alias}.account_id
        AND live.mailbox_identity_key IS {alias}.mailbox_identity_key"""
    return f"""NOT EXISTS (
        SELECT 1 FROM (
            SELECT live.message_id,live.provider,live.account_id,
                live.mailbox_identity_key,live.provider_message_id
            FROM messages live WHERE {live}
            UNION ALL
            SELECT frozen.message_id,frozen.provider,frozen.account_id,
                frozen.stored_identity_key AS mailbox_identity_key,frozen.provider_message_id
            FROM message_removal_targets frozen
            JOIN message_removal_intents intent ON intent.intent_id=frozen.intent_id
            WHERE frozen.message_id={bound_id} AND frozen.provider={alias}.provider
                AND frozen.account_id={alias}.account_id
                AND intent.mailbox_identity_key IS {alias}.mailbox_identity_key
                AND NOT EXISTS (SELECT 1 FROM messages live WHERE {live})
        ) candidate WHERE NOT ({available_sql("candidate", include_unfollow=include_unfollow)})
    )"""


def source_available(
    db,
    *,
    provider,
    account_id,
    mailbox_identity_key,
    provider_message_id,
    message_id=None,
    include_unfollow=False,
):
    if not has_schema(db):
        return True
    return bool(
        db.execute(
            f"""SELECT {available_sql("source", include_unfollow=include_unfollow)} FROM (
            SELECT ? AS message_id, ? AS provider, ? AS account_id,
                ? AS mailbox_identity_key, ? AS provider_message_id) source""",
            (message_id, provider, account_id, mailbox_identity_key, provider_message_id),
        ).fetchone()[0]
    )


def message_available(
    db,
    message_id,
    *,
    include_unfollow=False,
    require_source=True,
    retained_bindings=(),
):
    schema = has_schema(db)
    eligible = available_sql("m", include_unfollow=include_unfollow) if schema else "1"
    row = db.execute(
        f"SELECT ({eligible}) FROM messages m WHERE m.message_id=?",
        (message_id,),
    ).fetchone()
    if row is not None:
        return bool(row[0])
    if require_source:
        return False
    # Existing source-less terminal receipts remain valid. If the live row has
    # disappeared during an accepted request, its immutable retained binding
    # selects the frozen source candidates for the SAME eligibility predicate.
    if schema:
        for binding in retained_bindings:
            candidates = db.execute(
                f"""SELECT ({retained_available_sql("retained", include_unfollow=include_unfollow)})
                    FROM (SELECT ? AS message_id,? AS provider,? AS account_id,
                        ? AS mailbox_identity_key) retained""",
                (
                    message_id,
                    binding["provider"],
                    binding["account_id"],
                    binding["mailbox_identity_key"],
                ),
            ).fetchall()
            if any(not candidate[0] for candidate in candidates):
                return False
    return True


def freeze(db, key, kind, message_ids, *, accepted_at):
    """Capture existing logical units; caller owns acceptance's write transaction."""
    from dataclasses import asdict

    from .db import _effective_message_identity_sql, _logical_unit

    groups = {}
    for message_id in message_ids:
        row = db.execute(
            f"""SELECT m.*, {_effective_message_identity_sql("m")} AS effective_identity
                FROM messages m WHERE m.message_id=?""",
            (message_id,),
        ).fetchone()
        if row is None:
            continue
        canonical = row["logical_of"] or row["message_id"]
        binding = (row["provider"], row["account_id"], row["effective_identity"])
        groups.setdefault(binding, {})[canonical] = _logical_unit(db, canonical)
    for binding, units in groups.items():
        intent_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO message_removal_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (intent_id, key, kind, *binding, accepted_at, "pending", len(units), 0, None),
        )
        for canonical, unit in units.items():
            sources = json.dumps([asdict(source) for source in unit.sources])
            for message_id in unit.rows:
                row = db.execute(
                    "SELECT * FROM messages WHERE message_id=?", (message_id,)
                ).fetchone()
                db.execute(
                    "INSERT INTO message_removal_targets VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent_id,
                        canonical,
                        message_id,
                        row["provider"],
                        row["account_id"],
                        row["mailbox_identity_key"],
                        row["provider_message_id"],
                        sources,
                        "pending",
                        None,
                    ),
                )


def _accept_in_transaction(db, key, kind, parameters, message_ids, *, now, authority):
    """One receipt/freeze/invalidation owner; never opens a transaction or lock."""
    from . import thread_migration
    from .db import _mark_automation_sources_unavailable

    if type(authority) is _RuntimeAcceptance:
        authority.require(db)
    else:
        thread_migration.require_migration_owner(authority, db)
    fingerprint = request_hash(kind, parameters)
    previous = outcome(db, key, kind=kind, fingerprint=fingerprint)
    if previous is not None:
        return previous, False
    validate_key(db, key, kind, fingerprint, now=now)
    if kind == "delete":
        message_ids = [parameters["message_id"]]
        if not db.execute(
            "SELECT 1 FROM logical_messages WHERE message_id=?", (message_ids[0],)
        ).fetchone():
            raise KeyError(message_ids[0])
    elif kind == "clear":
        message_ids = [r[0] for r in db.execute("SELECT message_id FROM logical_messages")]
    elif kind != "unfollow" or message_ids is None:
        raise RemovalRequestInvalid("Unfollow acceptance belongs to the derived owner")
    stamp = now.astimezone(UTC).isoformat()
    db.execute(
        "INSERT INTO message_removal_requests VALUES(?,?,?,?)", (key, kind, fingerprint, stamp)
    )
    freeze(db, key, kind, message_ids, accepted_at=stamp)
    selected = [
        r[0] for r in db.execute(
            "SELECT t.message_id FROM message_removal_targets t JOIN message_removal_intents i "
            "ON i.intent_id=t.intent_id WHERE i.request_id=?", (key,),
        )
    ]
    _mark_automation_sources_unavailable(db, selected, updated_at=stamp)
    return outcome(db, key, kind=kind, fingerprint=fingerprint), True


class _RuntimeAcceptance:
    def __init__(self, db):
        self.db = db
        self.live = True

    def require(self, db):
        if not self.live or self.db is not db or not db.in_transaction:
            raise RuntimeError("Removal acceptance requires its guarded write transaction")


def accept(store, key, kind, parameters, *, now):
    """Accept a frozen selection under the caller's existing operation lock."""
    from . import source_locks

    fingerprint = request_hash(kind, parameters)
    with (
        source_locks.protocol_guard(
            source_locks.selection_path(store.path),
            "Source-lock protocol is being selected",
            timeout_seconds=source_locks.SOURCE_LOCK_WAIT_SECONDS,
        ),
        store.connection() as db,
    ):
        db.execute("BEGIN IMMEDIATE")
        authority = _RuntimeAcceptance(db)
        try:
            result, created = _accept_in_transaction(
                db, key, kind, parameters, None, now=now, authority=authority,
            )
        finally:
            authority.live = False
        if not created:
            return result
    resume(store, now=now)
    with store.connection() as db:
        return outcome(db, key, kind=kind, fingerprint=fingerprint)


def convert_saved_unfollow(db, migration_owner, *, now):
    """Represent retained pre-36 unfollowed bodies in the upgrade snapshot."""
    from . import thread_migration
    from .config import MAX_RETENTION_DAYS
    from .db import _effective_message_identity_sql, _logical_unit

    thread_migration.require_migration_owner(migration_owner, db)
    candidates = [r[0] for r in db.execute(
        f"""SELECT m.message_id FROM messages m
        WHERE NOT EXISTS (SELECT 1 FROM thread_follow f
            WHERE f.provider=m.provider AND f.account_id=m.account_id
            AND f.mailbox_identity_key={_effective_message_identity_sql("m")}
            AND f.thread_key=m.thread_key AND f.followed=1) AND (
            EXISTS(SELECT 1 FROM message_bodies b WHERE b.message_id=m.message_id)
            OR EXISTS(SELECT 1 FROM message_body_unavailable b WHERE b.message_id=m.message_id))
        ORDER BY m.message_id"""
    )]
    selected = {}
    for message_id in candidates:
        unit = _logical_unit(db, message_id)
        if unit is None:
            continue
        if any(not message_available(db, row_id, include_unfollow=True) for row_id in unit.rows):
            continue
        selected[unit.rows] = message_id
    if not selected:
        return
    parameters = {"message_ids": sorted(selected.values())}
    key = _issue_key(db, "unfollow", parameters, retention_days=MAX_RETENTION_DAYS, now=now)
    _accept_in_transaction(
        db, key, "unfollow", parameters, selected.values(), now=now, authority=migration_owner,
    )


def resume(store, *, now=None):
    """One physical completion owner; caller holds the existing operation lock."""
    from .db import (
        _message_suppression_key,
        _purge_expired_automation_tombstones,
        _suppression_expiry,
    )

    now = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = now.isoformat()
    with store.connection() as db:
        groups = [
            tuple(r)
            for r in db.execute(
                "SELECT DISTINCT intent_id,logical_target_id FROM message_removal_targets "
                "WHERE state='pending' ORDER BY intent_id,logical_target_id",
            )
        ]
    for intent_id, logical_id in groups:
        with store.connection() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT message_id FROM message_removal_targets WHERE intent_id=? "
                    "AND logical_target_id=? AND state='pending'",
                    (intent_id, logical_id),
                )
            ]
        try:
            with store._source_cleanup_locks(ids) as acquired, store.connection() as db:
                if not acquired:
                    continue
                db.execute("BEGIN IMMEDIATE")
                intent = db.execute(
                    "SELECT * FROM message_removal_intents WHERE intent_id=?", (intent_id,)
                ).fetchone()
                targets = db.execute(
                    "SELECT * FROM message_removal_targets "
                    "WHERE intent_id=? AND logical_target_id=? "
                    "AND state='pending'",
                    (intent_id, logical_id),
                ).fetchall()
                if not targets:
                    continue
                if intent["kind"] != "unfollow":
                    for frozen in dict.fromkeys(t["source_identities_json"] for t in targets):
                        for source in json.loads(frozen):
                            db.execute(
                                "INSERT INTO suppressed_messages VALUES(?,?,?,?) "
                                "ON CONFLICT(provider,account_id,message_key) "
                                "DO UPDATE SET expires_at=excluded.expires_at",
                                (
                                    source["provider"],
                                    source["account_id"],
                                    _message_suppression_key(
                                        source["provider"],
                                        source["account_id"],
                                        source["provider_message_id"],
                                        source["mailbox_identity_key"],
                                    ),
                                    _suppression_expiry(source["received_at"], now),
                                ),
                            )
                for target in targets:
                    row = db.execute(
                        "SELECT * FROM messages WHERE message_id=? AND provider=? AND account_id=? "
                        "AND mailbox_identity_key IS ? AND provider_message_id=?",
                        (
                            target["message_id"],
                            target["provider"],
                            target["account_id"],
                            target["stored_identity_key"],
                            target["provider_message_id"],
                        ),
                    ).fetchone()
                    if row is None:
                        continue
                    if intent["kind"] == "unfollow":
                        for table in ("message_bodies", "message_body_unavailable"):
                            db.execute(
                                f"DELETE FROM {table} WHERE message_id=?", (row["message_id"],)
                            )
                    else:
                        db.execute("DELETE FROM messages WHERE message_id=?", (row["message_id"],))
                if intent["kind"] != "unfollow":
                    _purge_expired_automation_tombstones(db, now=stamp)
                db.execute(
                    "UPDATE message_removal_targets SET state='completed',settled_at=? "
                    "WHERE intent_id=? AND logical_target_id=? AND state='pending'",
                    (stamp, intent_id, logical_id),
                )
                completed = db.execute(
                    "SELECT COUNT(DISTINCT logical_target_id) FROM message_removal_targets "
                    "WHERE intent_id=? AND state='completed'",
                    (intent_id,),
                ).fetchone()[0]
                settled = completed == intent["requested_count"]
                db.execute(
                    "UPDATE message_removal_intents SET completed_count=?,state=?,settled_at=? "
                    "WHERE intent_id=?",
                    (
                        completed,
                        "completed" if settled else "pending",
                        stamp if settled else None,
                        intent_id,
                    ),
                )
        except OperationLockBusy:
            # A still-active source owns its bytes; keep its receipt/barrier.
            continue
