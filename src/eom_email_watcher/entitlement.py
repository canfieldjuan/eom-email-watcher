from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

FEATURE_ID = "connect.capability_exchange"
ENTITLEMENT_FILE_NAME = "entitlement-v1.json"
BUNDLED_KEYRING = Path("eom_email_watcher_data/connect-entitlement-keyring.json")
MAX_ENTITLEMENT_BYTES = 16 * 1024
MAX_KEYRING_BYTES = 64 * 1024
MAX_PAYLOAD_BASE64URL_CHARS = 8192
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64

KEY_ID_PATTERN = r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$"
FEATURE_PATTERN = r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
UUID_V4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
UTC_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"

KeyId = Annotated[StrictStr, Field(pattern=KEY_ID_PATTERN, max_length=100)]
FeatureId = Annotated[StrictStr, Field(pattern=FEATURE_PATTERN, max_length=100)]
UuidV4 = Annotated[StrictStr, Field(pattern=UUID_V4_PATTERN)]
UtcTimestamp = Annotated[StrictStr, Field(pattern=UTC_TIMESTAMP_PATTERN)]


class EntitlementDecision(StrEnum):
    ACTIVE = "active"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    MISSING = "missing"
    INVALID = "invalid"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    FEATURE_MISSING = "feature_missing"

    @property
    def is_active(self) -> bool:
        return self is EntitlementDecision.ACTIVE


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _TrustedKey(_StrictModel):
    key_id: KeyId
    algorithm: Literal["Ed25519"]
    public_key_base64url: Annotated[StrictStr, Field(min_length=43, max_length=43)]


class _Keyring(_StrictModel):
    keys: Annotated[list[_TrustedKey], Field(max_length=16)]


class _Envelope(_StrictModel):
    format_version: Literal[1]
    key_id: KeyId
    payload_base64url: Annotated[StrictStr, Field(min_length=2, max_length=8192)]
    signature_base64url: Annotated[StrictStr, Field(min_length=86, max_length=86)]


class _Claims(_StrictModel):
    format_version: Literal[1]
    entitlement_id: UuidV4
    subject: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    features: Annotated[list[FeatureId], Field(min_length=1, max_length=32)]
    issued_at: UtcTimestamp
    not_before: UtcTimestamp
    expires_at: UtcTimestamp


@dataclass(frozen=True)
class EntitlementGate:
    path: Path | None
    keys: MappingProxyType[str, bytes] | None
    now: datetime

    @classmethod
    def from_installation(cls) -> EntitlementGate:
        return cls(
            path=_entitlement_path(
                os.environ.get("XDG_CONFIG_HOME"),
                os.environ.get("HOME"),
            ),
            keys=_load_bundled_keyring(),
            now=datetime.now(UTC),
        )

    @classmethod
    def for_test(
        cls,
        path: Path,
        keyring_document: bytes,
        now: datetime,
    ) -> EntitlementGate:
        return cls(path=path, keys=_parse_keyring(keyring_document), now=now)

    def decision(self) -> EntitlementDecision:
        if not self.keys:
            return EntitlementDecision.AUTHORITY_UNAVAILABLE
        if self.path is None:
            return EntitlementDecision.MISSING
        content = _read_private_entitlement(self.path)
        if content is None:
            return EntitlementDecision.MISSING
        return _evaluate_entitlement(content, self.keys, self.now)


def connect_entitlement_decision() -> EntitlementDecision:
    return EntitlementGate.from_installation().decision()


def _entitlement_path(xdg_config_home: str | None, home: str | None) -> Path | None:
    if xdg_config_home:
        root = Path(xdg_config_home)
    elif home:
        root = Path(home) / ".config"
    else:
        return None
    if not root.is_absolute():
        return None
    return root / "local-connect" / ENTITLEMENT_FILE_NAME


def _load_bundled_keyring() -> MappingProxyType[str, bytes] | None:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not isinstance(bundle_root, str):
        return None
    path = Path(bundle_root) / BUNDLED_KEYRING
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_KEYRING_BYTES:
            return None
        return _parse_keyring(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        return None


def _parse_keyring(content: bytes) -> MappingProxyType[str, bytes]:
    if not content or len(content) > MAX_KEYRING_BYTES:
        raise ValueError("Connect entitlement key ring is empty or oversized")
    document = _Keyring.model_validate(_strict_json_object(content))
    keys: dict[str, bytes] = {}
    for item in document.keys:
        public_key = _decode_base64url(item.public_key_base64url, PUBLIC_KEY_BYTES)
        if len(public_key) != PUBLIC_KEY_BYTES or item.key_id in keys:
            raise ValueError("Connect entitlement key ring is invalid")
        keys[item.key_id] = public_key
    return MappingProxyType(keys)


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object member: {key}")
        value[key] = child
    return value


def _strict_json_object(content: bytes) -> dict[str, object]:
    document = json.loads(content, object_pairs_hook=_reject_duplicate_members)
    if not isinstance(document, dict):
        raise ValueError("Connect entitlement JSON must be an object")
    return document


def _decode_base64url(value: str, max_bytes: int) -> bytes:
    if not value or "=" in value:
        raise ValueError("base64url value must be canonical and unpadded")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError("base64url value is invalid") from exc
    if len(decoded) > max_bytes or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != value:
        raise ValueError("base64url value is not canonical")
    return decoded


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("timestamp must be UTC")
    return parsed


def _evaluate_entitlement(
    content: bytes,
    keys: MappingProxyType[str, bytes],
    now: datetime,
) -> EntitlementDecision:
    if not content or len(content) > MAX_ENTITLEMENT_BYTES or now.tzinfo is None:
        return EntitlementDecision.INVALID
    try:
        envelope = _Envelope.model_validate(_strict_json_object(content))
        payload = _decode_base64url(
            envelope.payload_base64url,
            MAX_PAYLOAD_BASE64URL_CHARS * 3 // 4,
        )
        signature = _decode_base64url(envelope.signature_base64url, SIGNATURE_BYTES)
        if len(signature) != SIGNATURE_BYTES:
            return EntitlementDecision.INVALID
        public_key = keys.get(envelope.key_id)
        if public_key is None:
            return EntitlementDecision.INVALID
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
        claims = _Claims.model_validate(_strict_json_object(payload))
        if len(set(claims.features)) != len(claims.features):
            return EntitlementDecision.INVALID
        issued_at = _parse_utc(claims.issued_at)
        not_before = _parse_utc(claims.not_before)
        expires_at = _parse_utc(claims.expires_at)
    except (InvalidSignature, OSError, ValidationError, ValueError):
        return EntitlementDecision.INVALID
    if issued_at > not_before or not_before >= expires_at:
        return EntitlementDecision.INVALID
    current = now.astimezone(UTC)
    if current < not_before:
        return EntitlementDecision.NOT_YET_VALID
    if current >= expires_at:
        return EntitlementDecision.EXPIRED
    if FEATURE_ID not in claims.features:
        return EntitlementDecision.FEATURE_MISSING
    return EntitlementDecision.ACTIVE


def _read_private_entitlement(path: Path) -> bytes | None:
    if os.name != "posix" or not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"):
        return None
    try:
        expected_uid = os.geteuid()
        directory = path.parent.lstat()
        candidate = path.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != expected_uid
            or directory.st_mode & 0o077
            or not stat.S_ISREG(candidate.st_mode)
            or candidate.st_uid != expected_uid
            or candidate.st_mode & 0o077
            or not 0 < candidate.st_size <= MAX_ENTITLEMENT_BYTES
        ):
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or opened.st_mode & 0o077
            or not 0 < opened.st_size <= MAX_ENTITLEMENT_BYTES
            or (opened.st_dev, opened.st_ino) != (candidate.st_dev, candidate.st_ino)
        ):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_ENTITLEMENT_BYTES + 1)
        return content if 0 < len(content) <= MAX_ENTITLEMENT_BYTES else None
    except OSError:
        return None
    finally:
        os.close(descriptor)
