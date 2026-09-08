from __future__ import annotations

import base64
import binascii
import errno
import itertools
import json
import os
import stat
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StrictStr, TypeAdapter, ValidationError

from .connect_windows import (
    WindowsFileLock,
    WindowsLockBusy,
    atomic_replace_bytes,
    ensure_private_directory,
    local_app_data_root,
    read_bounded_regular_file,
    unlink_regular_file,
)

CONNECT_FEATURE_ID = "connect.capability_exchange"
AUTOMATIONS_FEATURE_ID = "connect.automations"
# Backward-compatible name for existing Connect-only callers and fixtures.
FEATURE_ID = CONNECT_FEATURE_ID
ENTITLEMENT_FILE_NAME = "entitlement-v1.json"
ENTITLEMENT_LOCK_FILE_NAME = ".entitlement-v1.lock"
BUNDLED_KEYRING = Path("eom_email_watcher_data/connect-entitlement-keyring.json")
INSTALLED_RELEASE_KEYRING = Path(
    "eom-email-watcher/connect-entitlement-keyring.json"
)
APPROVED_RELEASE_AUTHORITIES = frozenset(
    {
        (
            "local-connect-prod-2026-01",
            bytes.fromhex(
                "80df29263f56d87f3d2c1b0826a939c9d9a5c4d0ab6d25f101b0436107f57dad"
            ),
        )
    }
)
MAX_ENTITLEMENT_BYTES = 16 * 1024
MAX_KEYRING_BYTES = 64 * 1024
MAX_PAYLOAD_BASE64URL_CHARS = 8192
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64

KEY_ID_PATTERN = r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$"
FEATURE_PATTERN = r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
UUID_V4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
UTC_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
_TEMP_SEQUENCE = itertools.count()

KeyId = Annotated[StrictStr, Field(pattern=KEY_ID_PATTERN, max_length=100)]
FeatureId = Annotated[StrictStr, Field(pattern=FEATURE_PATTERN, max_length=100)]
UuidV4 = Annotated[StrictStr, Field(pattern=UUID_V4_PATTERN)]
UtcTimestamp = Annotated[StrictStr, Field(pattern=UTC_TIMESTAMP_PATTERN)]
_FEATURE_ID_ADAPTER = TypeAdapter(FeatureId)


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


@dataclass(frozen=True)
class EntitlementStatus:
    state: EntitlementDecision
    active: bool

    @classmethod
    def from_decision(cls, state: EntitlementDecision) -> EntitlementStatus:
        return cls(state=state, active=state.is_active)

    def public_dict(self) -> dict[str, object]:
        return {"state": self.state.value, "active": self.active}


class EntitlementInstallError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


AUTHORITY_UNAVAILABLE = "CONNECT_ENTITLEMENT_AUTHORITY_UNAVAILABLE"
SOURCE_INVALID = "CONNECT_ENTITLEMENT_SOURCE_INVALID"
NOT_ACTIVE = "CONNECT_ENTITLEMENT_NOT_ACTIVE"
STORAGE_UNAVAILABLE = "CONNECT_ENTITLEMENT_STORAGE_UNAVAILABLE"
ACTIVATION_BUSY = "CONNECT_ENTITLEMENT_ACTIVATION_BUSY"
INSTALL_FAILED = "CONNECT_ENTITLEMENT_INSTALL_FAILED"

_INSTALL_ERROR_MESSAGES = {
    AUTHORITY_UNAVAILABLE: "this build has no trusted Connect entitlement authority",
    SOURCE_INVALID: "the selected Connect entitlement is not a safe, valid license file",
    NOT_ACTIVE: "the selected Connect entitlement is not currently active",
    STORAGE_UNAVAILABLE: "the private Connect entitlement directory is unavailable or unsafe",
    ACTIVATION_BUSY: "another Connect entitlement activation is already in progress",
    INSTALL_FAILED: "the Connect entitlement could not be installed safely",
}


def _install_error(code: str) -> EntitlementInstallError:
    return EntitlementInstallError(code, _INSTALL_ERROR_MESSAGES[code])


class _CandidateInstallError(EntitlementInstallError):
    def __init__(self, *, promoted: bool):
        super().__init__(INSTALL_FAILED, _INSTALL_ERROR_MESSAGES[INSTALL_FAILED])
        self.promoted = promoted


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
    now: datetime | None

    @classmethod
    def from_installation(cls) -> EntitlementGate:
        return cls(
            path=_entitlement_path(
                os.environ.get("XDG_CONFIG_HOME"),
                os.environ.get("HOME"),
                os.environ.get("LOCALAPPDATA"),
            ),
            keys=_load_bundled_keyring() or _load_installed_release_keyring(),
            now=None,
        )

    @classmethod
    def for_test(
        cls,
        path: Path,
        keyring_document: bytes,
        now: datetime,
    ) -> EntitlementGate:
        return cls(path=path, keys=_parse_keyring(keyring_document), now=now)

    def decision(self, feature_id: str = CONNECT_FEATURE_ID) -> EntitlementDecision:
        requested_feature = _validate_feature_id(feature_id)
        if not self.keys:
            return EntitlementDecision.AUTHORITY_UNAVAILABLE
        if self.path is None:
            return EntitlementDecision.MISSING
        content = _read_private_entitlement(self.path)
        if content is None:
            return EntitlementDecision.MISSING
        return _evaluate_entitlement(
            content,
            self.keys,
            self._current_time(),
            requested_feature,
        )

    def status(self, feature_id: str = CONNECT_FEATURE_ID) -> EntitlementStatus:
        return EntitlementStatus.from_decision(self.decision(feature_id))

    def features_active(self, feature_ids: Iterable[str]) -> bool:
        requested_features = tuple(_validate_feature_id(value) for value in feature_ids)
        if not requested_features:
            raise ValueError("at least one entitlement feature is required")
        if not self.keys or self.path is None:
            return False
        content = _read_private_entitlement(self.path)
        if content is None:
            return False
        observed_at = self._current_time()
        return all(
            _evaluate_entitlement(content, self.keys, observed_at, feature).is_active
            for feature in requested_features
        )

    def install(self, source: Path) -> EntitlementStatus:
        if not self.keys:
            raise _install_error(AUTHORITY_UNAVAILABLE)
        if self.path is None:
            raise _install_error(STORAGE_UNAVAILABLE)
        candidate = _read_candidate_entitlement(source)
        _require_active_candidate(candidate, self.keys, self._current_time())
        if os.name == "nt":
            return _install_windows_entitlement(self, candidate)
        if (
            os.name != "posix"
            or not hasattr(os, "geteuid")
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_CLOEXEC")
            or not hasattr(os, "O_NONBLOCK")
            or not hasattr(os, "O_DIRECTORY")
        ):
            raise _install_error(STORAGE_UNAVAILABLE)

        parent = self.path.parent
        _ensure_private_directory(parent)
        with _activation_lock(parent):
            previous = _read_existing_destination(self.path)
            _require_active_candidate(candidate, self.keys, self._current_time())
            try:
                _install_candidate(self.path, candidate)
            except _CandidateInstallError as exc:
                if exc.promoted:
                    _restore_previous_entitlement(self.path, previous)
                raise _install_error(INSTALL_FAILED) from exc
            status = self.status()
            if not status.active:
                _restore_previous_entitlement(self.path, previous)
                raise _install_error(INSTALL_FAILED)
            return status

    def _current_time(self) -> datetime:
        return self.now if self.now is not None else datetime.now(UTC)


def connect_entitlement_decision() -> EntitlementDecision:
    return feature_entitlement_decision(CONNECT_FEATURE_ID)


def connect_entitlement_status() -> EntitlementStatus:
    return feature_entitlement_status(CONNECT_FEATURE_ID)


def feature_entitlement_decision(feature_id: str) -> EntitlementDecision:
    return EntitlementGate.from_installation().decision(feature_id)


def feature_entitlements_active(*feature_ids: str) -> bool:
    return EntitlementGate.from_installation().features_active(feature_ids)


def feature_entitlement_status(feature_id: str) -> EntitlementStatus:
    return EntitlementGate.from_installation().status(feature_id)


def install_connect_entitlement(source: Path) -> EntitlementStatus:
    return EntitlementGate.from_installation().install(source)


def _entitlement_path(
    xdg_config_home: str | None,
    home: str | None,
    local_app_data: str | None = None,
) -> Path | None:
    if os.name == "nt":
        try:
            return local_app_data_root(local_app_data) / "LocalConnect" / ENTITLEMENT_FILE_NAME
        except OSError:
            return None
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


def _load_installed_release_keyring() -> MappingProxyType[str, bytes] | None:
    path = _installed_release_keyring_path(
        os.environ.get("XDG_DATA_HOME"),
        os.environ.get("HOME"),
    )
    if path is None:
        return None
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_KEYRING_BYTES:
            return None
        keys = _parse_keyring(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        return None
    if frozenset(keys.items()) != APPROVED_RELEASE_AUTHORITIES:
        return None
    return keys


def _installed_release_keyring_path(
    xdg_data_home: str | None,
    home: str | None,
) -> Path | None:
    if xdg_data_home:
        root = Path(xdg_data_home)
    elif home:
        root = Path(home) / ".local" / "share"
    else:
        return None
    if not root.is_absolute():
        return None
    return root / INSTALLED_RELEASE_KEYRING


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


def _validate_feature_id(feature_id: str) -> str:
    try:
        return _FEATURE_ID_ADAPTER.validate_python(feature_id)
    except ValidationError as exc:
        raise ValueError("entitlement feature ID is invalid") from exc


def _evaluate_entitlement(
    content: bytes,
    keys: MappingProxyType[str, bytes],
    now: datetime,
    feature_id: str = CONNECT_FEATURE_ID,
) -> EntitlementDecision:
    requested_feature = _validate_feature_id(feature_id)
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
    if requested_feature not in claims.features:
        return EntitlementDecision.FEATURE_MISSING
    return EntitlementDecision.ACTIVE


def _require_active_candidate(
    content: bytes,
    keys: MappingProxyType[str, bytes],
    now: datetime,
) -> None:
    decision = _evaluate_entitlement(content, keys, now)
    if decision is EntitlementDecision.ACTIVE:
        return
    if decision in {
        EntitlementDecision.NOT_YET_VALID,
        EntitlementDecision.EXPIRED,
        EntitlementDecision.FEATURE_MISSING,
    }:
        raise _install_error(NOT_ACTIVE)
    if decision is EntitlementDecision.AUTHORITY_UNAVAILABLE:
        raise _install_error(AUTHORITY_UNAVAILABLE)
    raise _install_error(SOURCE_INVALID)


def _read_private_entitlement(path: Path) -> bytes | None:
    if os.name == "nt":
        try:
            return read_bounded_regular_file(path, MAX_ENTITLEMENT_BYTES)
        except OSError:
            return None
    if (
        os.name != "posix"
        or not hasattr(os, "geteuid")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_CLOEXEC")
        or not hasattr(os, "O_NONBLOCK")
    ):
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
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
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
        with suppress(OSError):
            os.close(descriptor)


def _read_candidate_entitlement(path: Path) -> bytes:
    if os.name == "nt":
        try:
            return read_bounded_regular_file(
                path,
                MAX_ENTITLEMENT_BYTES,
                require_private_acl=False,
            )
        except OSError as exc:
            raise _install_error(SOURCE_INVALID) from exc
    try:
        candidate = path.lstat()
        if (
            not stat.S_ISREG(candidate.st_mode)
            or not 0 < candidate.st_size <= MAX_ENTITLEMENT_BYTES
        ):
            raise _install_error(SOURCE_INVALID)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except EntitlementInstallError:
        raise
    except OSError as exc:
        raise _install_error(SOURCE_INVALID) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not 0 < opened.st_size <= MAX_ENTITLEMENT_BYTES
            or (opened.st_dev, opened.st_ino) != (candidate.st_dev, candidate.st_ino)
        ):
            raise _install_error(SOURCE_INVALID)
        content = bytearray()
        while len(content) <= MAX_ENTITLEMENT_BYTES:
            chunk = os.read(descriptor, min(8192, MAX_ENTITLEMENT_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if not 0 < len(content) <= MAX_ENTITLEMENT_BYTES:
            raise _install_error(SOURCE_INVALID)
        return bytes(content)
    except EntitlementInstallError:
        raise
    except OSError as exc:
        raise _install_error(SOURCE_INVALID) from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _install_windows_entitlement(
    gate: EntitlementGate,
    candidate: bytes,
) -> EntitlementStatus:
    assert gate.path is not None
    destination = gate.path
    lock: WindowsFileLock | None = None
    promoted = False
    previous: bytes | None = None
    try:
        root = local_app_data_root()
        ensure_private_directory(destination.parent, root=root)
        try:
            lock = WindowsFileLock(destination.parent / ENTITLEMENT_LOCK_FILE_NAME)
        except WindowsLockBusy as exc:
            raise _install_error(ACTIVATION_BUSY) from exc

        previous = _read_existing_windows_entitlement(destination)
        assert gate.keys is not None
        _require_active_candidate(candidate, gate.keys, gate._current_time())
        atomic_replace_bytes(destination, candidate, MAX_ENTITLEMENT_BYTES)
        promoted = True
        installed = read_bounded_regular_file(destination, MAX_ENTITLEMENT_BYTES)
        decision = _evaluate_entitlement(installed, gate.keys, gate._current_time())
        if installed != candidate or not decision.is_active:
            raise _install_error(INSTALL_FAILED)
        return EntitlementStatus.from_decision(decision)
    except EntitlementInstallError:
        if promoted:
            _restore_windows_entitlement(destination, previous)
        raise
    except OSError as exc:
        if promoted:
            _restore_windows_entitlement(destination, previous)
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    finally:
        if lock is not None:
            with suppress(OSError):
                lock.close()


def _read_existing_windows_entitlement(path: Path) -> bytes | None:
    try:
        return read_bounded_regular_file(path, MAX_ENTITLEMENT_BYTES, allow_empty=True)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc


def _restore_windows_entitlement(destination: Path, previous: bytes | None) -> None:
    try:
        if previous is None:
            with suppress(FileNotFoundError):
                unlink_regular_file(destination)
            if destination.exists():
                raise OSError(errno.EIO, "candidate removal failed")
            return
        atomic_replace_bytes(
            destination,
            previous,
            MAX_ENTITLEMENT_BYTES,
            allow_empty=True,
        )
        restored = read_bounded_regular_file(
            destination,
            MAX_ENTITLEMENT_BYTES,
            allow_empty=True,
        )
        if restored != previous:
            raise OSError(errno.EIO, "prior entitlement restoration mismatch")
    except OSError as exc:
        raise _install_error(INSTALL_FAILED) from exc


def _ensure_private_directory(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        missing: list[Path] = []
        cursor = path
        while True:
            try:
                cursor.lstat()
                break
            except FileNotFoundError:
                missing.append(cursor)
                parent = cursor.parent
                if parent == cursor:
                    raise _install_error(STORAGE_UNAVAILABLE) from None
                cursor = parent
            except OSError as exc:
                raise _install_error(STORAGE_UNAVAILABLE) from exc

        for directory in reversed(missing):
            created = False
            try:
                directory.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise _install_error(STORAGE_UNAVAILABLE) from exc
            if created:
                try:
                    directory.chmod(0o700)
                except OSError as exc:
                    raise _install_error(STORAGE_UNAVAILABLE) from exc
            try:
                metadata = directory.lstat()
            except OSError as exc:
                raise _install_error(STORAGE_UNAVAILABLE) from exc
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise _install_error(STORAGE_UNAVAILABLE) from None
            if created:
                _sync_directory(directory)
                _sync_directory(directory.parent)
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise _install_error(STORAGE_UNAVAILABLE)


def _read_existing_destination(path: Path) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or metadata.st_size > MAX_ENTITLEMENT_BYTES
    ):
        raise _install_error(STORAGE_UNAVAILABLE)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_mode & 0o077
            or opened.st_size > MAX_ENTITLEMENT_BYTES
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise _install_error(STORAGE_UNAVAILABLE)
        content = bytearray()
        while len(content) <= MAX_ENTITLEMENT_BYTES:
            chunk = os.read(descriptor, min(8192, MAX_ENTITLEMENT_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > MAX_ENTITLEMENT_BYTES:
            raise _install_error(STORAGE_UNAVAILABLE)
        return bytes(content)
    except EntitlementInstallError:
        raise
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)


@contextmanager
def _activation_lock(parent: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc

    path = parent / ENTITLEMENT_LOCK_FILE_NAME
    expected: os.stat_result | None
    try:
        expected = path.lstat()
    except FileNotFoundError:
        expected = None
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    if expected is not None and (
        not stat.S_ISREG(expected.st_mode)
        or expected.st_uid != os.geteuid()
        or expected.st_mode & 0o077
    ):
        raise _install_error(STORAGE_UNAVAILABLE)

    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    try:
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (
                expected is not None
                and (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
            )
        ):
            raise _install_error(STORAGE_UNAVAILABLE)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise _install_error(ACTIVATION_BUSY) from exc
            raise _install_error(STORAGE_UNAVAILABLE) from exc
        yield
    except EntitlementInstallError:
        raise
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _create_temporary_entitlement(parent: Path) -> tuple[int, Path]:
    for _ in range(64):
        path = parent / (f".{ENTITLEMENT_FILE_NAME}.tmp.{os.getpid()}.{next(_TEMP_SEQUENCE)}")
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise _install_error(INSTALL_FAILED) from exc
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise _install_error(INSTALL_FAILED)
            return descriptor, path
        except (EntitlementInstallError, OSError) as exc:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(OSError):
                path.unlink(missing_ok=True)
            if isinstance(exc, EntitlementInstallError):
                raise
            raise _install_error(INSTALL_FAILED) from exc
    raise _install_error(INSTALL_FAILED)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("entitlement write made no progress")
        view = view[written:]


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise _install_error(STORAGE_UNAVAILABLE) from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _install_candidate(destination: Path, content: bytes) -> None:
    descriptor, temporary = _create_temporary_entitlement(destination.parent)
    replaced = False
    try:
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.replace(temporary, destination)
            replaced = True
        except OSError as exc:
            raise _CandidateInstallError(promoted=replaced) from exc
        try:
            _sync_directory(destination.parent)
        except EntitlementInstallError as exc:
            raise _CandidateInstallError(promoted=True) from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)
        if not replaced:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _restore_previous_entitlement(destination: Path, previous: bytes | None) -> None:
    if previous is None:
        try:
            destination.unlink()
        except OSError as exc:
            raise _install_error(INSTALL_FAILED) from exc
        _sync_directory(destination.parent)
        try:
            destination.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise _install_error(INSTALL_FAILED) from exc
        raise _install_error(INSTALL_FAILED)

    _install_candidate(destination, previous)
    if _read_existing_destination(destination) != previous:
        raise _install_error(INSTALL_FAILED)
