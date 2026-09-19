"""Composition-layer Connect v2 invoker for the ``connect.invoke`` action kind.

The Automate package (:mod:`connect_automate.automate.actions`) declares the abstract
``connect.invoke`` kind and the :class:`CapabilityInvoker` seam without naming a transport,
so that package stays provider- and transport-free. This module is the composition layer
that fulfils the seam with the real Connect v2 client: it renders a frozen ``connect.invoke``
request into a prepared v2 job under the caller-minted stable ``job_id``, submits it to the
same-PC loopback provider, polls to a terminal outcome, and maps the result back to the
action lane. It lives outside the Automate package precisely because it depends on the
concrete v2 client and its loopback transport (:mod:`connect_automate.connect`).

Idempotent re-POST is the whole point of the stable identity, so the render is *deterministic*
in the frozen request: the input artifact id is carried on the request, not minted per
dispatch, so a retry or crash-recovery re-drive under the same ``job_id`` rebuilds the exact
same v2 request bytes and the provider replays its recorded outcome for that job rather than
starting a second invocation (ADR-0002). The target is re-resolved by discovery each dispatch
(the loopback base URL and token are ephemeral), but the frozen capability id, version, and
provider instance are matched against the discovered catalog, never widened: a re-drive that
cannot find the same target fails rather than invoking a different one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .connect import (
    DEFAULT_JOB_TIMEOUT_SECONDS,
    CapabilityCatalog,
    CapabilityJobUpdate,
    ConnectError,
    ConnectV2Client,
    DiscoveredCapability,
    discover_capabilities,
    restore_capability_job,
    validate_job_id,
)

_REQUEST_INVALID = "CONNECT_INVOKE_REQUEST_INVALID"


@dataclass(frozen=True)
class _InvokeRequest:
    """The parsed, frozen ``connect.invoke`` request the invoker renders into a v2 job."""

    capability_id: str
    capability_version: str
    provider_instance_id: str | None
    artifact_id: str
    media_type: str
    filename: str
    parameters: dict[str, object]
    confirmed: bool
    content: bytes = field(repr=False)


def _require_mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConnectError(_REQUEST_INVALID, f"The connect.invoke {what} must be an object.")
    return value


def _require_str(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConnectError(
            _REQUEST_INVALID, f"The connect.invoke {what} must be a non-empty string."
        )
    return value


def _parse_request(request: Mapping[str, object]) -> _InvokeRequest:
    """Validate the frozen action request and return its typed, deterministic form.

    A malformed request is a permanent failure of the frozen intent, not a transient one:
    it raises :class:`ConnectError` with ``CONNECT_INVOKE_REQUEST_INVALID`` so the action lane
    terminalizes the row as failed rather than retrying an intent it can never render.
    """
    _require_mapping(request, "request")
    capability = _require_mapping(request.get("capability"), "capability")
    capability_id = _require_str(capability.get("id"), "capability id")
    capability_version = _require_str(capability.get("version"), "capability version")

    provider_instance_id: str | None = None
    provider = request.get("provider")
    if provider is not None:
        provider = _require_mapping(provider, "provider")
        instance = provider.get("instance_id")
        if instance is not None:
            provider_instance_id = _require_str(instance, "provider instance id")

    input_artifact = _require_mapping(request.get("input"), "input")
    artifact_id = _require_str(input_artifact.get("artifact_id"), "input artifact id")
    media_type = _require_str(input_artifact.get("media_type"), "input media type")
    filename = _require_str(input_artifact.get("filename"), "input filename")
    content_base64 = input_artifact.get("content_base64", "")
    if not isinstance(content_base64, str):
        raise ConnectError(
            _REQUEST_INVALID, "The connect.invoke input content must be base64 text."
        )
    try:
        # An empty string decodes to b"" -- a zero-byte input is permitted (ADR-0002).
        content = base64.b64decode(content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ConnectError(
            _REQUEST_INVALID, "The connect.invoke input content is not valid base64."
        ) from exc

    parameters = request.get("parameters")
    if parameters is None:
        parameters = {}
    parameters = _require_mapping(parameters, "parameters")

    confirmed = request.get("confirmed", False)
    if not isinstance(confirmed, bool):
        raise ConnectError(_REQUEST_INVALID, "The connect.invoke confirmed flag must be a boolean.")

    return _InvokeRequest(
        capability_id=capability_id,
        capability_version=capability_version,
        provider_instance_id=provider_instance_id,
        artifact_id=artifact_id,
        media_type=media_type,
        filename=filename,
        parameters=dict(parameters),
        confirmed=confirmed,
        content=content,
    )


def _select_capability(
    catalog: CapabilityCatalog, request: _InvokeRequest
) -> DiscoveredCapability:
    """Match the frozen capability against the discovered catalog, never widening the target.

    The frozen request pins capability id and version (and optionally the provider instance);
    a re-drive resolves the loopback endpoint fresh but must invoke the *same* capability, so a
    mismatch is a hard error rather than a fallback to a different provider.
    """
    matches = [
        capability
        for capability in catalog.items
        if capability.capability_id == request.capability_id
        and capability.capability_version == request.capability_version
        and (
            request.provider_instance_id is None
            or capability.instance_id == request.provider_instance_id
        )
    ]
    if not matches:
        raise ConnectError(
            "CAPABILITY_NOT_FOUND",
            "No local provider offers the requested capability at the requested version.",
            retryable=True,
        )
    if len(matches) > 1:
        # Two providers offer the same capability and the request did not pin an instance:
        # invoking either could target the wrong provider, so refuse rather than guess.
        raise ConnectError(
            "CAPABILITY_AMBIGUOUS",
            "Multiple local providers offer the requested capability; pin a provider instance.",
        )
    return matches[0]


def _terminal_result(update: CapabilityJobUpdate) -> Mapping[str, object]:
    """Map a terminal v2 job update to the action outcome, raising on a failed job.

    A ``completed`` job settles the action with its provider identity and outputs. A ``failed``
    job is a business failure of the action, so it raises the provider's :class:`ConnectError`
    (preserving its code and retryability) and the action lane terminalizes the row as failed.
    """
    if update.status == "failed":
        raise update.error or ConnectError(
            "JOB_FAILED", "The capability job failed without a reported error."
        )
    result = update.result
    outputs = list(result.outputs) if result is not None else []
    return {
        "status": update.status,
        "job_id": update.job_id,
        "provider": {
            "app_id": update.provider_app_id,
            "instance_id": update.provider_instance_id,
        },
        "outputs": [output.store_dict() for output in outputs],
    }


DiscoverCapabilities = Callable[..., CapabilityCatalog]


class ConnectV2CapabilityInvoker:
    """Fulfils the Automate ``CapabilityInvoker`` seam with the real Connect v2 client.

    One instance drives any local Connect v2 capability: :meth:`invoke` discovers the local
    provider, matches the frozen capability, rebuilds the durable v2 job under the caller's
    stable ``job_id``, submits it, polls to terminal, and maps the outcome. Repeated invocation
    with the same ``job_id`` and request is idempotent: the render is deterministic in the
    frozen request, so the provider replays the recorded outcome for the job it already
    accepted rather than performing the effect twice.

    ``discover`` is injectable for testing; it defaults to the entitlement-gated
    :func:`discover_capabilities`, so an unlicensed host cannot even enumerate providers.
    """

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        client: httpx.Client | None = None,
        poll_interval_seconds: float = 0.25,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        discover: DiscoverCapabilities = discover_capabilities,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._client = client
        self._poll_interval_seconds = poll_interval_seconds
        self._job_timeout_seconds = job_timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._discover = discover

    def invoke(self, request: Mapping[str, object], *, job_id: str) -> Mapping[str, object]:
        parsed = _parse_request(request)
        # The stable job id is the durable action id, which the store mints as a uuid4; assert
        # it here so a non-conforming identity fails before any provider is contacted.
        validate_job_id(job_id)

        catalog = self._discover(
            self._runtime_dir,
            client=self._client,
            provider_instance_id=parsed.provider_instance_id,
        )
        capability = _select_capability(catalog, parsed)

        # Enforce confirmation at the invocation boundary too: an automatic trigger must never
        # drive a confirmation-required capability without a confirmation frozen onto the intent.
        if capability.confirmation_required and not parsed.confirmed:
            raise ConnectError(
                "CONFIRMATION_REQUIRED",
                "This capability requires explicit confirmation before invocation.",
            )

        job = restore_capability_job(
            capability,
            job_id=job_id,
            artifact_id=parsed.artifact_id,
            media_type=parsed.media_type,
            byte_size=len(parsed.content),
            sha256=hashlib.sha256(parsed.content).hexdigest(),
            filename=parsed.filename,
            parameters=parsed.parameters,
        )

        client_kwargs: dict[str, object] = {
            "client": self._client,
            "poll_interval_seconds": self._poll_interval_seconds,
            "job_timeout_seconds": self._job_timeout_seconds,
        }
        if self._sleep is not None:
            client_kwargs["sleep"] = self._sleep
        if self._monotonic is not None:
            client_kwargs["monotonic"] = self._monotonic
        v2 = ConnectV2Client(capability, **client_kwargs)  # type: ignore[arg-type]

        initial = v2.submit(job, parsed.content)
        terminal = v2.wait_for_terminal(job, initial, lambda _update: None)
        return _terminal_result(terminal)
