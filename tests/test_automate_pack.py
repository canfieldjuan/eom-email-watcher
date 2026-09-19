from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from connect_automate.automate import (
    GrantView,
    LoadedPack,
    PackError,
    load_pack,
    verify_grant,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)
PACK_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
GRANT_ID = "33333333-3333-4333-8333-333333333333"
SUBJECT = "pc-0001"
KEY_ID = "pack-key"

WORKFLOW = {
    "name": "lead-funnel",
    "stages": ["captured", "reviewing"],
    "initial_stage": "captured",
    "definitions": [
        {
            "name": "advance",
            "trigger": {"source_kind": "operator.decision", "decision": "review"},
            "conditions": [],
            "effects": [{"kind": "record.transition", "to_stage": "reviewing"}],
        }
    ],
}


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _keys(key: Ed25519PrivateKey, key_id: str = KEY_ID) -> dict[str, bytes]:
    return {key_id: key.public_key().public_bytes_raw()}


def _envelope(key: Ed25519PrivateKey, payload: bytes, key_id: str = KEY_ID) -> bytes:
    return json.dumps(
        {
            "format_version": 1,
            "key_id": key_id,
            "payload_base64url": _encoded(payload),
            "signature_base64url": _encoded(key.sign(payload)),
        },
        separators=(",", ":"),
    ).encode()


def _pack_payload(workflow: dict, *, pack_id: str = PACK_ID, pack_version: int = 1) -> bytes:
    return json.dumps(
        {
            "format_version": 1,
            "pack_id": pack_id,
            "pack_version": pack_version,
            "workflow": workflow,
        },
        separators=(",", ":"),
    ).encode()


def _grant_payload(
    *,
    pack_id: str = PACK_ID,
    subject: str = SUBJECT,
    not_before: str = "2026-01-01T00:00:00Z",
    expires_at: str = "2027-01-01T00:00:00Z",
    issued_at: str = "2026-01-01T00:00:00Z",
) -> bytes:
    return json.dumps(
        {
            "format_version": 1,
            "grant_id": GRANT_ID,
            "pack_id": pack_id,
            "subject": subject,
            "issued_at": issued_at,
            "not_before": not_before,
            "expires_at": expires_at,
        },
        separators=(",", ":"),
    ).encode()


# --- pack loading -------------------------------------------------------------------------


def test_load_pack_returns_the_verified_workflow() -> None:
    key = Ed25519PrivateKey.generate()
    pack = _envelope(key, _pack_payload(WORKFLOW, pack_version=3))
    loaded = load_pack(pack, keys=_keys(key))
    assert isinstance(loaded, LoadedPack)
    assert loaded.pack_id == PACK_ID
    assert loaded.pack_version == 3
    assert loaded.workflow.name == "lead-funnel"
    assert loaded.workflow.definitions[0].effects[0].to_stage == "reviewing"


def test_load_pack_rejects_a_tampered_payload() -> None:
    key = Ed25519PrivateKey.generate()
    signed = key.sign(_pack_payload(WORKFLOW, pack_version=1))
    tampered = _pack_payload(WORKFLOW, pack_version=999)  # a different payload, old signature
    envelope = json.dumps(
        {
            "format_version": 1,
            "key_id": KEY_ID,
            "payload_base64url": _encoded(tampered),
            "signature_base64url": _encoded(signed),
        },
        separators=(",", ":"),
    ).encode()
    with pytest.raises(PackError):
        load_pack(envelope, keys=_keys(key))


def test_load_pack_rejects_an_untrusted_key() -> None:
    publisher = Ed25519PrivateKey.generate()
    pack = _envelope(publisher, _pack_payload(WORKFLOW))
    # A keyring that does not contain the pack's signing key rejects it.
    stranger = Ed25519PrivateKey.generate()
    with pytest.raises(PackError):
        load_pack(pack, keys=_keys(stranger))


def test_load_pack_rejects_an_unknown_key_id() -> None:
    key = Ed25519PrivateKey.generate()
    pack = _envelope(key, _pack_payload(WORKFLOW), key_id="not-in-ring")
    with pytest.raises(PackError):
        load_pack(pack, keys=_keys(key))


def test_load_pack_rejects_deeply_nested_json_as_pack_error() -> None:
    # Deeply nested JSON within the size bound exhausts the decoder's recursion; the trust
    # boundary must fail closed as PackError, not let RecursionError escape and crash a caller
    # that handles invalid packs by catching PackError.
    key = Ed25519PrivateKey.generate()
    deep = (b"[" * 12000) + (b"]" * 12000)  # ~24 KB, under MAX_PACK_BYTES
    with pytest.raises(PackError):
        load_pack(deep, keys=_keys(key))


def test_load_pack_rejects_a_malformed_workflow() -> None:
    key = Ed25519PrivateKey.generate()
    bad = dict(WORKFLOW)
    bad["definitions"] = [
        {
            "name": "advance",
            "trigger": {"source_kind": "operator.decision", "decision": "review"},
            "conditions": [],
            "effects": [],  # violates min_length=1
        }
    ]
    pack = _envelope(key, _pack_payload(bad))
    with pytest.raises(PackError):
        load_pack(pack, keys=_keys(key))


# --- grant verification -------------------------------------------------------------------


def test_verify_grant_authorizes_a_valid_grant() -> None:
    key = Ed25519PrivateKey.generate()
    grant = _envelope(key, _grant_payload())
    view = verify_grant(grant, keys=_keys(key), pack_id=PACK_ID, subject=SUBJECT, now=NOW)
    assert isinstance(view, GrantView)
    assert view.pack_id == PACK_ID
    assert view.subject == SUBJECT


def test_verify_grant_rejects_a_different_subject() -> None:
    key = Ed25519PrivateKey.generate()
    grant = _envelope(key, _grant_payload(subject="pc-9999"))
    with pytest.raises(PackError):
        verify_grant(grant, keys=_keys(key), pack_id=PACK_ID, subject=SUBJECT, now=NOW)


def test_verify_grant_rejects_a_different_pack() -> None:
    key = Ed25519PrivateKey.generate()
    grant = _envelope(key, _grant_payload(pack_id=OTHER_ID))
    with pytest.raises(PackError):
        verify_grant(grant, keys=_keys(key), pack_id=PACK_ID, subject=SUBJECT, now=NOW)


def test_verify_grant_rejects_an_expired_grant() -> None:
    key = Ed25519PrivateKey.generate()
    grant = _envelope(
        key, _grant_payload(not_before="2026-01-01T00:00:00Z", expires_at="2026-02-01T00:00:00Z")
    )
    with pytest.raises(PackError):
        verify_grant(grant, keys=_keys(key), pack_id=PACK_ID, subject=SUBJECT, now=NOW)


def test_verify_grant_rejects_a_not_yet_valid_grant() -> None:
    key = Ed25519PrivateKey.generate()
    grant = _envelope(
        key,
        _grant_payload(
            issued_at="2026-08-01T00:00:00Z",
            not_before="2026-09-01T00:00:00Z",
            expires_at="2027-01-01T00:00:00Z",
        ),
    )
    with pytest.raises(PackError):
        verify_grant(grant, keys=_keys(key), pack_id=PACK_ID, subject=SUBJECT, now=NOW)


def test_verify_grant_rejects_a_tampered_signature() -> None:
    key = Ed25519PrivateKey.generate()
    signed = key.sign(_grant_payload())
    tampered = _grant_payload(subject="pc-9999")
    envelope = json.dumps(
        {
            "format_version": 1,
            "key_id": KEY_ID,
            "payload_base64url": _encoded(tampered),
            "signature_base64url": _encoded(signed),
        },
        separators=(",", ":"),
    ).encode()
    with pytest.raises(PackError):
        verify_grant(envelope, keys=_keys(key), pack_id=PACK_ID, subject="pc-9999", now=NOW)


def test_verify_grant_rejects_an_untrusted_key() -> None:
    key = Ed25519PrivateKey.generate()
    grant = _envelope(key, _grant_payload())
    stranger = Ed25519PrivateKey.generate()
    with pytest.raises(PackError):
        verify_grant(grant, keys=_keys(stranger), pack_id=PACK_ID, subject=SUBJECT, now=NOW)


def test_verify_grant_rejects_a_naive_now() -> None:
    key = Ed25519PrivateKey.generate()
    grant = _envelope(key, _grant_payload())
    with pytest.raises(PackError):
        verify_grant(
            grant,
            keys=_keys(key),
            pack_id=PACK_ID,
            subject=SUBJECT,
            now=datetime(2026, 6, 1),  # naive, no tzinfo
        )
