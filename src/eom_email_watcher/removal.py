"""D-removal's durable request, bound target, and eligibility owner."""

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
            "request_id": self.request_id, "intent_ids": list(self.intent_ids),
            "outcome": "deferred" if self.pending else "completed",
            "requested": self.requested, "completed": self.completed, "pending": self.pending,
            "message": "Deletion pending until the local upgrade completes" if self.pending else None,
        }


def request_hash(kind, parameters):
    if (
        not isinstance(kind, str) or kind not in {"delete", "clear", "unfollow"}
        or not isinstance(parameters, dict)
    ):
        raise RemovalRequestInvalid("Invalid local removal request")
    try:
        payload = json.dumps(
            {"kind": kind, "parameters": parameters}, sort_keys=True,
            ensure_ascii=False, allow_nan=False, separators=(",", ":"),
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
    if type(retention_days) is not int or retention_days < 1:
        raise RemovalRequestInvalid("Invalid removal retention interval")
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        expires = int((stamp + timedelta(days=retention_days)).timestamp())
    except OverflowError as exc:
        raise RemovalRequestInvalid("Invalid removal retention interval") from exc
    unsigned = f"rm1.{expires}.{secrets.token_hex(16)}"
    fingerprint = request_hash(kind, parameters)
    with store.connection() as db:
        signature = _signature(db, unsigned, kind, fingerprint)
    return f"{unsigned}.{signature}"


def validate_key(db, key, kind, fingerprint, *, now):
    if not isinstance(key, str) or (match := KEY.fullmatch(key)) is None:
        raise RemovalRequestInvalid("Malformed local removal request key")
    unsigned = key.rsplit(".", 1)[0]
    if not hmac.compare_digest(match[3], _signature(db, unsigned, kind, fingerprint)):
        raise RemovalRequestInvalid("Local removal request key does not match its parameters")
    if int(match[1]) <= now.timestamp():
        raise RemovalRequestInvalid("Local removal request key has expired; start a new request")


def outcome(db, key, *, kind=None, fingerprint=None):
    request = db.execute(
        "SELECT kind,parameters_sha256 FROM message_removal_requests WHERE request_id=?", (key,),
    ).fetchone()
    if request is None:
        return None
    if (kind is not None and request[0] != kind) or (
        fingerprint is not None and not hmac.compare_digest(request[1], fingerprint)
    ):
        raise RemovalRequestInvalid("Removal request key was already used with different parameters")
    children = db.execute(
        "SELECT intent_id,requested_count,completed_count FROM message_removal_intents "
        "WHERE request_id=? ORDER BY intent_id", (key,),
    ).fetchall()
    return Outcome(key, tuple(r[0] for r in children), request[0],
                   sum(r[1] for r in children), sum(r[2] for r in children))


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
            mailbox_identity_key TEXT CHECK(mailbox_identity_key IS NULL OR mailbox_identity_key<>''),
            accepted_at TEXT NOT NULL CHECK(accepted_at<>''),
            state TEXT NOT NULL CHECK(state IN ('pending','completed')),
            requested_count INTEGER NOT NULL CHECK(requested_count>0),
            completed_count INTEGER NOT NULL CHECK(completed_count>=0),
            settled_at TEXT,
            CHECK(completed_count<=requested_count),
            CHECK((state='completed')=(completed_count=requested_count)),
            CHECK((state='completed')=(settled_at IS NOT NULL)),
            UNIQUE(intent_id,provider,account_id),
            FOREIGN KEY(request_id,kind) REFERENCES message_removal_requests(request_id,kind)
                ON DELETE CASCADE)""",
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
