"""Composition-layer Connect v2 invoker tests.

These exercise the real render-submit-poll-map path against a mock loopback provider: the
frozen ``connect.invoke`` request is rendered into a v2 job under the caller-minted stable
job id, submitted, polled to terminal, and mapped back. The render must be deterministic in
the frozen request so a re-drive under the same job id re-POSTs identical bytes (ADR-0002
idempotent re-POST), and the frozen capability target is matched, never widened.
"""

import base64
import hashlib
import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from connect_automate import automate_connect, connect

TOKEN = "A" * 43
INSTANCE_A = "11111111-1111-4111-8111-111111111111"
INSTANCE_B = "22222222-2222-4222-8222-222222222222"
OUTPUT_ID = "33333333-3333-4333-8333-333333333333"
ARTIFACT_ID = "5aaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACTION_ID = "6bbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

CONTENT = b'{"lead":"abc"}'
MEDIA_TYPE = "application/json"


def make_capability(
    *,
    instance_id: str = INSTANCE_A,
    capability_id: str = "lead.customer-handoff",
    produces: tuple[str, ...] = ("application/json",),
    parameters: tuple[connect.CapabilityParameter, ...] = (),
    confirmation_required: bool = False,
) -> connect.DiscoveredCapability:
    return connect.DiscoveredCapability(
        protocol_version=2,
        base_url="http://127.0.0.1:32123/",
        token=TOKEN,
        app_id="eom-funnel-provider",
        app_name="EOM Funnel Provider",
        app_version="1.0.0",
        instance_id=instance_id,
        capability_id=capability_id,
        capability_version="1.0",
        action_label="Hand off",
        action_description="Hand a qualified lead to the CRM.",
        accepts=(connect.AcceptedArtifactType(MEDIA_TYPE, 1024),),
        produces=produces,
        parameters=parameters,
        external_effects=confirmation_required,
        confirmation_required=confirmation_required,
    )


def fake_discover(
    *capabilities: connect.DiscoveredCapability,
) -> automate_connect.DiscoverCapabilities:
    def discover(runtime_dir=None, *, client=None, provider_instance_id=None):
        items = (
            tuple(c for c in capabilities if c.instance_id == provider_instance_id)
            if provider_instance_id is not None
            else capabilities
        )
        return connect.CapabilityCatalog(items=items)

    return discover


def invoke_request(
    *,
    capability_id: str = "lead.customer-handoff",
    version: str = "1.0",
    instance_id: str | None = INSTANCE_A,
    artifact_id: str = ARTIFACT_ID,
    media_type: str = MEDIA_TYPE,
    filename: str = "request.json",
    content: bytes = CONTENT,
    parameters: dict[str, object] | None = None,
    confirmed: bool | None = None,
) -> dict[str, object]:
    request: dict[str, object] = {
        "capability": {"id": capability_id, "version": version},
        "input": {
            "artifact_id": artifact_id,
            "media_type": media_type,
            "filename": filename,
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
    }
    if instance_id is not None:
        request["provider"] = {"instance_id": instance_id}
    if parameters is not None:
        request["parameters"] = parameters
    if confirmed is not None:
        request["confirmed"] = confirmed
    return request


def multipart_parts(request: httpx.Request) -> dict[str, bytes]:
    body = request.read()
    raw = (
        f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + body
    )
    message = BytesParser(policy=default).parsebytes(raw)
    return {
        str(part.get_param("name", header="content-disposition")): part.get_payload(decode=True)
        for part in message.iter_parts()
    }


def job_status(
    job: connect.PreparedCapabilityJob,
    status: str,
    *,
    result: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "protocol_version": 2,
        "job_id": job.job_id,
        "capability": {"id": job.capability_id, "version": job.capability_version},
        "provider": {"app_id": job.provider_app_id, "instance_id": job.provider_instance_id},
        "status": status,
        "created_at": "2026-09-17T12:00:00+00:00",
        "updated_at": "2026-09-17T12:00:01+00:00",
        "input_artifacts": [job.artifact.public_dict()],
    }
    if result is not None:
        value["result"] = result
    if error is not None:
        value["error"] = error
    return value


def completed_result(payload: bytes = b'{"receipt":"ok"}') -> dict[str, object]:
    return {
        "outputs": [
            {
                "artifact_id": OUTPUT_ID,
                "media_type": "application/json",
                "display_name": "receipt.json",
                "byte_size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "payload_base64": base64.b64encode(payload).decode("ascii"),
            }
        ]
    }


def accepted_then_completed(
    *,
    result: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
    terminal: str = "completed",
    captured: list[dict[str, object]] | None = None,
) -> httpx.MockTransport:
    """A mock provider: POST accepts the job, GET returns the terminal status.

    The GET status echoes the exact artifact and identity the POST submitted, so the client's
    request-to-status provenance check passes for any frozen input (including a zero-byte one).
    """
    submitted: dict[str, connect.PreparedCapabilityJob] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            parts = multipart_parts(request)
            observed = json.loads(parts["request"])
            if captured is not None:
                captured.append(observed)
            job = _job_from_request(observed)
            submitted[job.job_id] = job
            return httpx.Response(202, json=job_status(job, "accepted"))
        job_id = request.url.path.rsplit("/", 1)[-1]
        job = submitted[job_id]
        return httpx.Response(
            200, json=job_status(job, terminal, result=result, error=error)
        )

    return httpx.MockTransport(handler)


def _job_from_request(observed: dict[str, object]) -> connect.PreparedCapabilityJob:
    artifact = observed["inputs"][0]
    return connect.PreparedCapabilityJob(
        job_id=observed["job_id"],
        provider_app_id="eom-funnel-provider",
        provider_app_version="1.0.0",
        provider_instance_id=INSTANCE_A,
        capability_id=observed["capability"]["id"],
        capability_version=observed["capability"]["version"],
        artifact=connect.ArtifactIdentity(
            artifact_id=artifact["artifact_id"],
            media_type=artifact["media_type"],
            byte_size=artifact["byte_size"],
            sha256=artifact["sha256"],
        ),
        display_name=artifact["display_name"],
        parameters=(),
        request_json=b"{}",
    )


def make_invoker(
    transport: httpx.MockTransport,
    *capabilities: connect.DiscoveredCapability,
) -> tuple[automate_connect.ConnectV2CapabilityInvoker, httpx.Client]:
    http_client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False)
    invoker = automate_connect.ConnectV2CapabilityInvoker(
        client=http_client,
        poll_interval_seconds=0,
        sleep=lambda _seconds: None,
        discover=fake_discover(*(capabilities or (make_capability(),))),
    )
    return invoker, http_client


def test_invoke_renders_submits_and_maps_the_completed_outcome() -> None:
    captured: list[dict[str, object]] = []
    transport = accepted_then_completed(result=completed_result(), captured=captured)
    invoker, http_client = make_invoker(transport)
    with http_client:
        outcome = invoker.invoke(invoke_request(), job_id=ACTION_ID)

    assert outcome["status"] == "completed"
    assert outcome["job_id"] == ACTION_ID
    assert outcome["provider"] == {"app_id": "eom-funnel-provider", "instance_id": INSTANCE_A}
    assert outcome["outputs"][0]["artifact_id"] == OUTPUT_ID
    # The submitted v2 request carries the caller-minted stable job id and the frozen input id.
    assert captured[0]["job_id"] == ACTION_ID
    assert captured[0]["inputs"][0]["artifact_id"] == ARTIFACT_ID
    assert captured[0]["capability"] == {"id": "lead.customer-handoff", "version": "1.0"}


def test_render_is_deterministic_so_a_redrive_reposts_identical_bytes() -> None:
    captured: list[dict[str, object]] = []
    transport = accepted_then_completed(result=completed_result(), captured=captured)
    invoker, http_client = make_invoker(transport)
    request = invoke_request()
    with http_client:
        invoker.invoke(request, job_id=ACTION_ID)
        invoker.invoke(request, job_id=ACTION_ID)

    # A re-drive under the same job id and frozen request must submit the exact same request,
    # so the provider replays the recorded outcome for that job rather than starting a new one.
    assert captured[0] == captured[1]
    assert captured[0]["job_id"] == captured[1]["job_id"] == ACTION_ID


def test_parameters_flow_into_the_rendered_request() -> None:
    captured: list[dict[str, object]] = []
    transport = accepted_then_completed(result=completed_result(), captured=captured)
    capability = make_capability(
        parameters=(
            connect.CapabilityParameter(
                name="dry-run",
                value_type="boolean",
                required=False,
                label="Dry run",
                description="Preview without committing.",
            ),
        )
    )
    invoker, http_client = make_invoker(transport, capability)
    with http_client:
        invoker.invoke(invoke_request(parameters={"dry-run": True}), job_id=ACTION_ID)

    assert captured[0]["parameters"] == {"dry-run": True}


def test_a_failed_job_raises_the_providers_error_so_the_action_terminalizes_failed() -> None:
    transport = accepted_then_completed(
        terminal="failed",
        error={
            "code": "EOM_HANDOFF_CONFLICT",
            "message": "The handoff conflicts with a concurrent one.",
            "retryable": False,
        },
    )
    invoker, http_client = make_invoker(transport)
    with http_client, pytest.raises(connect.ConnectError) as excinfo:
        invoker.invoke(invoke_request(), job_id=ACTION_ID)
    assert excinfo.value.code == "EOM_HANDOFF_CONFLICT"
    assert excinfo.value.retryable is False


def test_confirmation_required_without_confirmation_refuses_before_contacting_provider() -> None:
    contacted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(202, json={})

    capability = make_capability(confirmation_required=True)
    invoker, http_client = make_invoker(httpx.MockTransport(handler), capability)
    with http_client, pytest.raises(connect.ConnectError) as excinfo:
        invoker.invoke(invoke_request(confirmed=False), job_id=ACTION_ID)
    assert excinfo.value.code == "CONFIRMATION_REQUIRED"
    assert contacted is False


def test_confirmation_required_with_confirmation_is_invoked() -> None:
    transport = accepted_then_completed(result=completed_result())
    capability = make_capability(confirmation_required=True)
    invoker, http_client = make_invoker(transport, capability)
    with http_client:
        outcome = invoker.invoke(invoke_request(confirmed=True), job_id=ACTION_ID)
    assert outcome["status"] == "completed"


def test_unknown_capability_is_capability_not_found() -> None:
    transport = accepted_then_completed(result=completed_result())
    invoker, http_client = make_invoker(transport)
    with http_client, pytest.raises(connect.ConnectError) as excinfo:
        invoker.invoke(invoke_request(capability_id="lead.unknown"), job_id=ACTION_ID)
    assert excinfo.value.code == "CAPABILITY_NOT_FOUND"


def test_two_providers_without_a_pinned_instance_is_ambiguous() -> None:
    transport = accepted_then_completed(result=completed_result())
    invoker, http_client = make_invoker(
        transport,
        make_capability(instance_id=INSTANCE_A),
        make_capability(instance_id=INSTANCE_B),
    )
    with http_client, pytest.raises(connect.ConnectError) as excinfo:
        invoker.invoke(invoke_request(instance_id=None), job_id=ACTION_ID)
    assert excinfo.value.code == "CAPABILITY_AMBIGUOUS"


def test_a_pinned_instance_selects_among_two_providers() -> None:
    captured: list[dict[str, object]] = []
    transport = accepted_then_completed(result=completed_result(), captured=captured)
    invoker, http_client = make_invoker(
        transport,
        make_capability(instance_id=INSTANCE_A),
        make_capability(instance_id=INSTANCE_B),
    )
    with http_client:
        outcome = invoker.invoke(invoke_request(instance_id=INSTANCE_A), job_id=ACTION_ID)
    assert outcome["status"] == "completed"


def test_a_non_uuid4_job_id_is_rejected_before_contacting_provider() -> None:
    contacted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(202, json={})

    invoker, http_client = make_invoker(httpx.MockTransport(handler))
    with http_client, pytest.raises(connect.ConnectError) as excinfo:
        invoker.invoke(invoke_request(), job_id="not-a-uuid")
    assert excinfo.value.code == "JOB_REQUEST_INVALID"
    assert contacted is False


def test_a_malformed_request_is_a_permanent_request_invalid_failure() -> None:
    transport = accepted_then_completed(result=completed_result())
    invoker, http_client = make_invoker(transport)
    with http_client, pytest.raises(connect.ConnectError) as excinfo:
        invoker.invoke({"capability": {"id": "lead.customer-handoff"}}, job_id=ACTION_ID)
    assert excinfo.value.code == "CONNECT_INVOKE_REQUEST_INVALID"


def test_input_content_not_accepted_by_the_capability_is_rejected() -> None:
    transport = accepted_then_completed(result=completed_result())
    invoker, http_client = make_invoker(transport)
    with http_client, pytest.raises(connect.ConnectError) as excinfo:
        invoker.invoke(
            invoke_request(media_type="application/octet-stream"), job_id=ACTION_ID
        )
    assert excinfo.value.code == "INPUT_ARTIFACT_INVALID"


def test_a_zero_byte_input_is_permitted() -> None:
    captured: list[dict[str, object]] = []
    transport = accepted_then_completed(result=completed_result(), captured=captured)
    invoker, http_client = make_invoker(transport)
    with http_client:
        outcome = invoker.invoke(invoke_request(content=b""), job_id=ACTION_ID)
    assert outcome["status"] == "completed"
    assert captured[0]["inputs"][0]["byte_size"] == 0


def test_a_definition_normalized_request_is_driven_end_to_end() -> None:
    # Cross-layer: the signed definition's normalized connect.invoke request is exactly the
    # frozen request the composition-layer invoker parses and drives. A shape drift between the
    # two layers would fail this rather than only at runtime.
    from connect_automate.automate import ConnectInvokeRequest

    request = ConnectInvokeRequest.model_validate(
        {
            "capability": {"id": "lead.customer-handoff", "version": "1.0"},
            "input": {
                "artifact_id": ARTIFACT_ID,
                "media_type": MEDIA_TYPE,
                "filename": "request.json",
                "content_base64": base64.b64encode(CONTENT).decode("ascii"),
            },
            "provider": {"instance_id": INSTANCE_A},
            "parameters": {"page-size": 25},
            "confirmed": True,
        }
    ).model_dump(mode="json", exclude_none=True)

    captured: list[dict[str, object]] = []
    transport = accepted_then_completed(result=completed_result(), captured=captured)
    capability = make_capability(
        confirmation_required=True,
        parameters=(
            connect.CapabilityParameter(
                name="page-size",
                value_type="integer",
                required=False,
                label="Page size",
                description="How many links to list.",
            ),
        ),
    )
    invoker, http_client = make_invoker(transport, capability)
    with http_client:
        outcome = invoker.invoke(request, job_id=ACTION_ID)

    assert outcome["status"] == "completed"
    assert captured[0]["job_id"] == ACTION_ID
    assert captured[0]["inputs"][0]["artifact_id"] == ARTIFACT_ID
    assert captured[0]["parameters"] == {"page-size": 25}
