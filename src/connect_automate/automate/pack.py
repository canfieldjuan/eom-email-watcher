"""Signed workflow packs: the pack format, Ed25519 pack-signature verification, and the
per-PC pack license grant.

A pack is the unit the Automate host sells and loads. Its bytes are a signed envelope with
the same shape as the Connect entitlement license (``{format_version, key_id,
payload_base64url, signature_base64url}``) whose payload is a canonical :class:`PackManifest`
carrying a stable pack identity, a monotonic pack version, and the signed :class:`Workflow`
(trigger, stages, conditions, effects) from :mod:`connect_automate.automate.definition`.

Two independent signatures gate a pack:

- The **publisher signature** authenticates the pack bytes. Verifying it proves the workflow
  semantics are exactly what the publisher signed; it says nothing about who may run them.
- The **pack grant** is a separately signed envelope, bound to the pack identity and the
  licensed subject (the PC or customer), that authorizes *this* machine to run *this* pack.
  A publisher signature without a matching grant is an unlicensed copy.

Verification reuses the entitlement substrate: the one Ed25519 verification lives in
:func:`connect_automate.entitlement.verify_signature`, and the base64url, strict-JSON, and
UTC-timestamp primitives are shared too, so no crypto or parsing is duplicated. The pack
envelope carries a larger payload than the license envelope (it embeds a whole workflow), so
its size bounds are its own.

Deferred hardening (post-proof): grant revocation lists, revalidation of the grant at every
trigger and admission boundary (not only at load), signed-pack immutability enforced through
the rule CRUD, and separating mutable operator parameters into a constrained schema. This
slice provides load-time signature and grant verification.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from pydantic import Field, StrictInt, StrictStr, ValidationError

from ..entitlement import (
    SIGNATURE_BYTES,
    KeyId,
    UtcTimestamp,
    UuidV4,
    _decode_base64url,
    _parse_utc,
    _strict_json_object,
    _StrictModel,
    verify_signature,
)
from .definition import Workflow, canonical_workflow

# A pack payload embeds a full workflow (bounded to 16 KiB canonical by the definition model)
# plus small identity metadata, so the pack envelope is larger than the license envelope.
MAX_PACK_BYTES = 32 * 1024
# A grant payload is a small claims object; keep it tightly bounded.
MAX_GRANT_BYTES = 8 * 1024
# base64url of MAX_PACK_BYTES raw bytes, the largest payload either envelope carries.
_MAX_ENVELOPE_PAYLOAD_B64_CHARS = MAX_PACK_BYTES * 4 // 3 + 8


class PackError(ValueError):
    """Raised when a pack or its grant is malformed, untrusted, or not authorized."""


class _SignedEnvelope(_StrictModel):
    """The signed-envelope shape shared with the entitlement license, sized for packs."""

    format_version: Literal[1]
    key_id: KeyId
    payload_base64url: Annotated[
        StrictStr, Field(min_length=2, max_length=_MAX_ENVELOPE_PAYLOAD_B64_CHARS)
    ]
    signature_base64url: Annotated[StrictStr, Field(min_length=86, max_length=86)]


class PackManifest(_StrictModel):
    """The signed inner payload of a pack: its identity, version, and workflow semantics."""

    format_version: Literal[1]
    pack_id: UuidV4
    # A positive integer identifying this pack revision. It is carried for the deferred
    # version-freeze / anti-downgrade work (persisting the referenced version and refusing an
    # older one): the ``ge=1`` bound is only well-formedness, not a monotonic guarantee, so
    # load-time verification here does not by itself prevent pairing an older validly-signed
    # pack with a current grant.
    pack_version: Annotated[StrictInt, Field(ge=1)]
    workflow: Workflow


class PackGrant(_StrictModel):
    """The signed inner payload of a per-PC grant binding a pack to a licensed subject."""

    format_version: Literal[1]
    grant_id: UuidV4
    pack_id: UuidV4
    subject: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    issued_at: UtcTimestamp
    not_before: UtcTimestamp
    expires_at: UtcTimestamp


@dataclass(frozen=True)
class LoadedPack:
    """A verified pack: its authenticated identity, version, and workflow."""

    pack_id: str
    pack_version: int
    workflow: Workflow


@dataclass(frozen=True)
class GrantView:
    """A verified, authorized pack grant."""

    grant_id: str
    pack_id: str
    subject: str


def _verify_envelope(content: bytes, keys: Mapping[str, bytes], *, max_bytes: int) -> bytes:
    """Verify a signed envelope and return its payload bytes, or raise :class:`PackError`.

    Mirrors the entitlement license envelope but with the pack size bounds and reuses the
    shared crypto (:func:`verify_signature`) and decode/parse primitives.
    """
    if not content or len(content) > max_bytes:
        raise PackError("signed document is empty or oversized")
    try:
        envelope = _SignedEnvelope.model_validate(_strict_json_object(content))
        payload = _decode_base64url(envelope.payload_base64url, max_bytes)
        signature = _decode_base64url(envelope.signature_base64url, SIGNATURE_BYTES)
        verify_signature(keys, envelope.key_id, payload, signature)
    except (InvalidSignature, ValidationError, ValueError, RecursionError) as exc:
        # RecursionError: deeply nested JSON within the size bound exhausts the decoder's
        # recursion before verification. This is a trust boundary, so every failure over
        # untrusted bytes fails closed as PackError, never escaping to crash a caller that
        # handles invalid packs by catching PackError.
        raise PackError(f"signed document is malformed or untrusted: {exc}") from exc
    return payload


def load_pack(pack_bytes: bytes, *, keys: Mapping[str, bytes]) -> LoadedPack:
    """Verify a pack's publisher signature and parse its manifest.

    Proves the pack bytes were signed by a trusted publisher key and that the embedded
    workflow is well-formed and within the canonical-definition size bound. It does not
    authorize the pack to run on this machine; that is :func:`verify_grant`.
    """
    payload = _verify_envelope(pack_bytes, keys, max_bytes=MAX_PACK_BYTES)
    try:
        manifest = PackManifest.model_validate(_strict_json_object(payload))
        # Enforce the workflow's canonical-definition size bound (DefinitionError is a
        # ValueError), so a pack cannot smuggle an oversized or unserializable workflow.
        canonical_workflow(manifest.workflow)
    except (ValidationError, ValueError, RecursionError) as exc:
        raise PackError(f"pack manifest is invalid: {exc}") from exc
    return LoadedPack(
        pack_id=manifest.pack_id,
        pack_version=manifest.pack_version,
        workflow=manifest.workflow,
    )


def verify_grant(
    grant_bytes: bytes,
    *,
    keys: Mapping[str, bytes],
    pack_id: str,
    subject: str,
    now: datetime,
) -> GrantView:
    """Verify a per-PC pack grant and authorize the pack for this subject.

    Proves the grant was signed by a trusted key, is within its validity window, and is bound
    to this ``pack_id`` and ``subject`` (the licensed PC or customer). Any failure raises
    :class:`PackError`; the caller treats an absent grant as unauthorized. Revocation-list
    checks and revalidation at every trigger/admission boundary are deferred hardening.
    """
    if now.tzinfo is None:
        raise PackError("current time must be timezone-aware")
    payload = _verify_envelope(grant_bytes, keys, max_bytes=MAX_GRANT_BYTES)
    try:
        grant = PackGrant.model_validate(_strict_json_object(payload))
        issued_at = _parse_utc(grant.issued_at)
        not_before = _parse_utc(grant.not_before)
        expires_at = _parse_utc(grant.expires_at)
    except (ValidationError, ValueError, RecursionError) as exc:
        raise PackError(f"pack grant is invalid: {exc}") from exc
    if issued_at > not_before or not_before >= expires_at:
        raise PackError("pack grant has an invalid validity window")
    if grant.pack_id != pack_id:
        raise PackError("pack grant is bound to a different pack")
    if grant.subject != subject:
        raise PackError("pack grant is bound to a different subject")
    current = now.astimezone(UTC)
    if current < not_before:
        raise PackError("pack grant is not yet valid")
    if current >= expires_at:
        raise PackError("pack grant has expired")
    return GrantView(grant_id=grant.grant_id, pack_id=grant.pack_id, subject=grant.subject)
