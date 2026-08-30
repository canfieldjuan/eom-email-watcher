import hashlib
import json
import os
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pytest

from eom_email_watcher import connect

TOKEN = "A" * 43
INSTANCE_A = "11111111-1111-4111-8111-111111111111"
INSTANCE_B = "22222222-2222-4222-8222-222222222222"
OUTPUT_ID = "33333333-3333-4333-8333-333333333333"


def registration(
    *,
    instance_id: str,
    app_id: str,
    base_url: str,
    protocol_version: int = 1,
    started_at: str | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": protocol_version,
        "instance_id": instance_id,
        "app_id": app_id,
        "pid": os.getpid(),
        "started_at": started_at or datetime.now(UTC).isoformat(),
        "transport": {"kind": "http-loopback-v1", "base_url": base_url},
        "auth": {"scheme": "bearer", "token": TOKEN},
    }


def manifest(instance_id: str, app_id: str, max_bytes: int = 1024) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "instance_id": instance_id,
        "app": {"id": app_id, "name": "Alternate Summarizer", "version": "1.2.3"},
        "capabilities": [
            {
                "id": "document.summarize",
                "version": "1.0",
                "accepts": [{"media_type": "application/pdf", "max_bytes": max_bytes}],
                "produces": ["application/vnd.local-connect.document-summary+json"],
            }
        ],
    }


def registration_v2(
    *, instance_id: str, app_id: str, base_url: str, token: str = TOKEN
) -> dict[str, object]:
    return {
        "protocol_version": 2,
        "instance_id": instance_id,
        "app_id": app_id,
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "transport": {"kind": "http-loopback-v2", "base_url": base_url},
        "auth": {"scheme": "bearer", "token": token},
    }


def capability_v2(
    capability_id: str,
    *,
    label: str,
    accepts: str,
    produces: str,
    max_bytes: int = 1024,
    parameters: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": capability_id,
        "version": "1.0",
        "action": {"label": label, "description": f"{label} this artifact locally."},
        "accepts": [{"media_type": accepts, "max_bytes": max_bytes}],
        "produces": [produces],
        "parameters": parameters or [],
        "effects": {"external": False, "confirmation_required": False},
    }


def manifest_v2(
    instance_id: str,
    app_id: str,
    *,
    name: str = "Capability Provider",
    capabilities: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": 2,
        "instance_id": instance_id,
        "app": {"id": app_id, "name": name, "version": "1.2.3"},
        "capabilities": capabilities
        or [
            capability_v2(
                "document.summarize",
                label="Summarize",
                accepts="application/pdf",
                produces="application/vnd.local-connect.document-summary+json",
            )
        ],
    }


def providers_dir(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    path = tmp_path / "local-connect/v1/providers"
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def providers_dir_v2(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    path = tmp_path / "local-connect/v2/providers"
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_registration(path: Path, value: dict[str, object], mode: int = 0o600) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(mode)


def test_discovery_uses_capability_not_provider_identity(tmp_path: Path) -> None:
    directory = providers_dir(tmp_path)
    write_registration(
        directory / "provider.json",
        registration(
            instance_id=INSTANCE_A,
            app_id="some-other-provider",
            base_url="http://127.0.0.1:32123/",
        ),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=manifest(INSTANCE_A, "some-other-provider"))

    with httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    ) as client:
        discovery = connect.discover_summary_capability(tmp_path, client=client)

    assert discovery.provider is not None
    assert discovery.provider.app_id == "some-other-provider"
    assert discovery.public_result() == {
        "items": [
            {
                "id": "document.summarize",
                "version": "1.0",
                "accepts": ["application/pdf"],
                "max_input_bytes": 1024,
            }
        ],
        "diagnostic": None,
    }
    assert requests[0].url == "http://127.0.0.1:32123/v1/manifest"
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert "origin" not in requests[0].headers


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        (
            registration(
                instance_id=INSTANCE_A,
                app_id="provider",
                base_url="http://192.0.2.10:32123/",
            ),
            0o600,
        ),
        (
            registration(
                instance_id=INSTANCE_A,
                app_id="provider",
                base_url="http://127.0.0.1:32123/",
                protocol_version=2,
            ),
            0o600,
        ),
        (
            registration(
                instance_id=INSTANCE_A,
                app_id="provider",
                base_url="http://127.0.0.1:32123/",
            ),
            0o644,
        ),
        (
            registration(
                instance_id=INSTANCE_A,
                app_id="provider",
                base_url="http://127.0.0.1:32123/",
                started_at="2026-08-29T12:00:00",
            ),
            0o600,
        ),
    ],
)
def test_discovery_ignores_unsafe_or_unsupported_registration(
    tmp_path: Path, value: dict[str, object], mode: int
) -> None:
    directory = providers_dir(tmp_path)
    write_registration(directory / "provider.json", value, mode)

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unsafe registration reached HTTP: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as client:
        discovery = connect.discover_summary_capability(tmp_path, client=client)

    assert discovery.provider is None
    assert discovery.diagnostic_code == "provider_unavailable"


def test_discovery_ignores_stale_provider_and_reports_multiple_live_providers(
    tmp_path: Path,
) -> None:
    directory = providers_dir(tmp_path)
    write_registration(
        directory / "a.json",
        registration(
            instance_id=INSTANCE_A,
            app_id="provider-a",
            base_url="http://127.0.0.1:32123/",
        ),
    )
    write_registration(
        directory / "b.json",
        registration(
            instance_id=INSTANCE_B,
            app_id="provider-b",
            base_url="http://127.0.0.1:32124/",
        ),
    )
    write_registration(
        directory / "stale.json",
        registration(
            instance_id="44444444-4444-4444-8444-444444444444",
            app_id="stale-provider",
            base_url="http://127.0.0.1:32125/",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 32125:
            raise httpx.ConnectError("stale", request=request)
        instance = INSTANCE_A if request.url.port == 32123 else INSTANCE_B
        app_id = "provider-a" if request.url.port == 32123 else "provider-b"
        return httpx.Response(200, json=manifest(instance, app_id))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        discovery = connect.discover_summary_capability(tmp_path, client=client)
        selected = connect.discover_summary_capability(
            tmp_path,
            client=client,
            provider_instance_id=INSTANCE_A,
        )
        missing = connect.discover_summary_capability(
            tmp_path,
            client=client,
            provider_instance_id="55555555-5555-4555-8555-555555555555",
        )

    assert discovery.provider is None
    assert discovery.public_result() == {
        "items": [],
        "diagnostic": {"code": "ambiguous_provider"},
    }
    assert selected.provider is not None
    assert selected.provider.instance_id == INSTANCE_A
    assert missing.provider is None
    assert missing.diagnostic_code == "provider_unavailable"


def test_generic_discovery_returns_every_capability_without_provider_secrets(
    tmp_path: Path,
) -> None:
    directory = providers_dir_v2(tmp_path)
    write_registration(
        directory / "provider.json",
        registration_v2(
            instance_id=INSTANCE_A,
            app_id="some-other-provider",
            base_url="http://127.0.0.1:32123/",
        ),
    )
    capabilities = [
        capability_v2(
            "document.summarize",
            label="Summarize",
            accepts="application/pdf",
            produces="application/vnd.local-connect.document-summary+json",
        ),
        capability_v2(
            "text.translate",
            label="Translate",
            accepts="text/plain",
            produces="text/plain",
            max_bytes=connect.MAX_INPUT_BYTES * 2,
            parameters=[
                {
                    "name": "target-language",
                    "value_type": "string",
                    "required": True,
                    "label": "Target language",
                    "description": "Language for the translated output.",
                }
            ],
        ),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=manifest_v2(
                INSTANCE_A,
                "some-other-provider",
                name="Other Local Tools",
                capabilities=capabilities,
            ),
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    ) as client:
        catalog = connect.discover_capabilities(tmp_path, client=client)

    assert [item.capability_id for item in catalog.items] == [
        "document.summarize",
        "text.translate",
    ]
    assert [item.capability_id for item in catalog.compatible("APPLICATION/PDF", 1024)] == [
        "document.summarize"
    ]
    assert [item.capability_id for item in catalog.compatible("text/plain", 2048)] == [
        "text.translate"
    ]
    assert [
        item.capability_id
        for item in catalog.compatible("text/plain", connect.MAX_INPUT_BYTES)
    ] == ["text.translate"]
    assert catalog.compatible("text/plain", connect.MAX_INPUT_BYTES + 1) == ()
    assert catalog.compatible("application/pdf", 1025) == ()
    assert catalog.compatible("application/pdf", 0) == ()
    public = catalog.public_result()
    assert public["diagnostic"] is None
    assert public["items"][1]["provider"] == {  # type: ignore[index]
        "app_id": "some-other-provider",
        "name": "Other Local Tools",
        "version": "1.2.3",
        "instance_id": INSTANCE_A,
        "available": True,
    }
    assert public["items"][1]["capability"]["accepts"] == [  # type: ignore[index]
        {"media_type": "text/plain", "max_bytes": connect.MAX_INPUT_BYTES}
    ]
    assert public["items"][1]["capability"]["parameters"][0] == {  # type: ignore[index]
        "name": "target-language",
        "value_type": "string",
        "required": True,
        "label": "Target language",
        "description": "Language for the translated output.",
    }
    public_json = json.dumps(public)
    assert TOKEN not in public_json
    assert "base_url" not in public_json
    assert requests[0].url == "http://127.0.0.1:32123/v2/manifest"
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert "origin" not in requests[0].headers


def test_generic_discovery_preserves_multiple_provider_choices(tmp_path: Path) -> None:
    directory = providers_dir_v2(tmp_path)
    write_registration(
        directory / "b.json",
        registration_v2(
            instance_id=INSTANCE_B,
            app_id="provider-b",
            base_url="http://127.0.0.1:32124/",
        ),
    )
    write_registration(
        directory / "a.json",
        registration_v2(
            instance_id=INSTANCE_A,
            app_id="provider-a",
            base_url="http://127.0.0.1:32123/",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        instance_id = INSTANCE_A if request.url.port == 32123 else INSTANCE_B
        app_id = "provider-a" if request.url.port == 32123 else "provider-b"
        return httpx.Response(200, json=manifest_v2(instance_id, app_id))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = connect.discover_capabilities(tmp_path, client=client)
        selected = connect.discover_capabilities(
            tmp_path,
            client=client,
            provider_instance_id=INSTANCE_B,
        )

    assert catalog.diagnostic_code is None
    assert [item.app_id for item in catalog.items] == ["provider-a", "provider-b"]
    assert [item.instance_id for item in selected.items] == [INSTANCE_B]


@pytest.mark.parametrize(
    "registration_value",
    [
        registration_v2(
            instance_id=INSTANCE_A,
            app_id="provider",
            base_url="http://192.0.2.10:32123/",
        ),
        {
            **registration_v2(
                instance_id=INSTANCE_A,
                app_id="provider",
                base_url="http://127.0.0.1:32123/",
            ),
            "protocol_version": 1,
        },
    ],
)
def test_generic_discovery_rejects_unsafe_registration_before_http(
    tmp_path: Path, registration_value: dict[str, object]
) -> None:
    directory = providers_dir_v2(tmp_path)
    write_registration(directory / "provider.json", registration_value)

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unsafe registration reached HTTP: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as client:
        catalog = connect.discover_capabilities(tmp_path, client=client)

    assert catalog.items == ()
    assert catalog.diagnostic_code == "provider_unavailable"


@pytest.mark.parametrize("duplicate", ["capability", "accepted_media", "parameter"])
def test_generic_discovery_rejects_ambiguous_manifest_members(
    tmp_path: Path, duplicate: str
) -> None:
    directory = providers_dir_v2(tmp_path)
    write_registration(
        directory / "provider.json",
        registration_v2(
            instance_id=INSTANCE_A,
            app_id="provider",
            base_url="http://127.0.0.1:32123/",
        ),
    )
    declared = capability_v2(
        "document.summarize",
        label="Summarize",
        accepts="application/pdf",
        produces="application/vnd.local-connect.document-summary+json",
    )
    capabilities = [declared, dict(declared)]
    if duplicate == "accepted_media":
        declared["accepts"] = [
            {"media_type": "application/pdf", "max_bytes": 1024},
            {"media_type": "application/pdf", "max_bytes": 2048},
        ]
        capabilities = [declared]
    elif duplicate == "parameter":
        parameter = {
            "name": "mode",
            "value_type": "string",
            "required": False,
            "label": "Mode",
            "description": "Summary mode.",
        }
        declared["parameters"] = [parameter, dict(parameter)]
        capabilities = [declared]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=manifest_v2(INSTANCE_A, "provider", capabilities=capabilities),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = connect.discover_capabilities(tmp_path, client=client)

    assert catalog.items == ()
    assert catalog.diagnostic_code == "provider_unavailable"


def test_generic_discovery_requires_registration_manifest_attribution(tmp_path: Path) -> None:
    directory = providers_dir_v2(tmp_path)
    write_registration(
        directory / "provider.json",
        registration_v2(
            instance_id=INSTANCE_A,
            app_id="registered-provider",
            base_url="http://127.0.0.1:32123/",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=manifest_v2(INSTANCE_B, "different-provider"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = connect.discover_capabilities(tmp_path, client=client)

    assert catalog.items == ()
    assert catalog.diagnostic_code == "provider_unavailable"


def test_discovery_does_not_follow_provider_redirects(tmp_path: Path) -> None:
    directory = providers_dir(tmp_path)
    write_registration(
        directory / "provider.json",
        registration(
            instance_id=INSTANCE_A,
            app_id="provider",
            base_url="http://127.0.0.1:32123/",
        ),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://192.0.2.10/manifest"})

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        discovery = connect.discover_summary_capability(tmp_path, client=client)

    assert discovery.provider is None
    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"


def test_response_limit_stops_stream_before_buffering_extra_bytes() -> None:
    class OversizedStream(httpx.SyncByteStream):
        yielded = 0

        def __iter__(self):
            self.yielded += 1
            yield b"{" + b" " * (connect.MAX_MANIFEST_BYTES - 1)
            self.yielded += 1
            yield b"x"
            self.yielded += 1
            raise AssertionError("reader continued after exceeding its response limit")

    stream = OversizedStream()
    response = httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        stream=stream,
    )
    try:
        with pytest.raises(connect.ConnectError) as raised:
            connect._response_json(response, connect.MAX_MANIFEST_BYTES)
    finally:
        response.close()

    assert raised.value.code == "RESPONSE_TOO_LARGE"
    assert stream.yielded == 2


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
    job: connect.PreparedSummaryJob,
    status: str,
    *,
    result: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "protocol_version": 1,
        "job_id": job.job_id,
        "capability": {"id": "document.summarize", "version": "1.0"},
        "provider": {"app_id": "alternate-provider", "instance_id": INSTANCE_A},
        "status": status,
        "created_at": "2026-08-29T12:00:00+00:00",
        "updated_at": "2026-08-29T12:00:01+00:00",
        "input_artifacts": [job.artifact.public_dict()],
    }
    if result is not None:
        value["result"] = result
    if error is not None:
        value["error"] = error
    return value
def completed_result(job: connect.PreparedSummaryJob) -> dict[str, object]:
    content = {
        "summary_version": "1.0",
        "text": "Résumé for José — exact $1,247.17.",
        "warnings": [{"code": "SEMANTIC_VERIFICATION_DEFERRED", "message": "Review it."}],
        "input_artifact": job.artifact.public_dict(),
    }
    encoded = json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode()
    return {
        "outputs": [
            {
                "artifact_id": OUTPUT_ID,
                "media_type": "application/vnd.local-connect.document-summary+json",
                "byte_size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "content": content,
            }
        ]
    }


def test_job_handoff_has_no_path_or_mailbox_metadata_and_polls_to_completion() -> None:
    content = b"%PDF-1.4\nlocal fixture\n%%EOF"
    job = connect.prepare_summary_job(content, "../../private.pdf")
    observed_parts: list[list[str]] = []
    observed_request: dict[str, object] = {}
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls, observed_request
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert "origin" not in request.headers
        if request.method == "POST":
            parts = multipart_parts(request)
            observed_parts.append(list(parts))
            observed_request = json.loads(parts["request"])
            assert parts["artifact"] == content
            return httpx.Response(202, json=job_status(job, "accepted"))
        polls += 1
        if polls == 1:
            return httpx.Response(200, json=job_status(job, "processing"))
        return httpx.Response(
            200,
            json=job_status(job, "completed", result=completed_result(job)),
        )

    provider = connect.ProviderCapability(
        base_url="http://127.0.0.1:32123/",
        token=TOKEN,
        app_id="alternate-provider",
        instance_id=INSTANCE_A,
        max_input_bytes=1024,
    )
    updates: list[str] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = connect.ConnectClient(
            provider,
            client=http_client,
            poll_interval_seconds=0,
            sleep=lambda _seconds: None,
        )
        initial = client.submit(job, content)
        final = client.wait_for_terminal(job, initial, lambda update: updates.append(update.status))

    assert observed_parts == [["request", "artifact"]]
    assert observed_request["inputs"] == [
        {
            **job.artifact.public_dict(),
            "display_name": "private.pdf",
            "source_app_id": "email-watcher",
        }
    ]
    encoded_request = json.dumps(observed_request)
    assert "path" not in encoded_request
    assert "message_id" not in encoded_request
    assert "sender" not in encoded_request
    assert "subject" not in encoded_request
    assert updates == ["processing", "completed"]
    assert final.result is not None
    assert final.result.text == "Résumé for José — exact $1,247.17."


def test_job_rejects_tampered_inline_output_integrity() -> None:
    content = b"%PDF-1.4\nfixture\n%%EOF"
    job = connect.prepare_summary_job(content, "document.pdf")
    result = completed_result(job)
    result["outputs"][0]["sha256"] = "0" * 64  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=job_status(job, "completed", result=result))

    provider = connect.ProviderCapability(
        base_url="http://127.0.0.1:32123/",
        token=TOKEN,
        app_id="alternate-provider",
        instance_id=INSTANCE_A,
        max_input_bytes=1024,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = connect.ConnectClient(provider, client=http_client)
        with pytest.raises(connect.ConnectError, match="integrity") as raised:
            client.submit(job, content)

    assert raised.value.code == "OUTPUT_INTEGRITY_INVALID"


def test_job_rejects_status_from_a_different_provider_instance() -> None:
    content = b"%PDF-1.4\nfixture\n%%EOF"
    job = connect.prepare_summary_job(content, "document.pdf")

    def handler(request: httpx.Request) -> httpx.Response:
        value = job_status(job, "accepted")
        value["provider"] = {
            "app_id": "alternate-provider",
            "instance_id": INSTANCE_B,
        }
        return httpx.Response(202, json=value)

    provider = connect.ProviderCapability(
        base_url="http://127.0.0.1:32123/",
        token=TOKEN,
        app_id="alternate-provider",
        instance_id=INSTANCE_A,
        max_input_bytes=1024,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = connect.ConnectClient(provider, client=http_client)
        with pytest.raises(connect.ConnectError) as raised:
            client.submit(job, content)

    assert raised.value.code == "RESPONSE_MISMATCH"
