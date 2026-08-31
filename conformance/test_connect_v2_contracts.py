"""Opt-in conformance checks against the canonical Local Connect v2 corpus."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx

from eom_email_watcher import connect

CONTRACTS_REVISION = "4d46af25ef5112f76daf841c7622987f05d25142"
CONTRACTS_ENV = "CONNECT_CONTRACTS_DIR"
FIXTURE_ROOT = "fixtures/v2"
CONSUMER_SCHEMAS = {
    "error.schema.json",
    "job-status.schema.json",
    "manifest.schema.json",
    "registration.schema.json",
}


def _contracts_dir() -> Path:
    value = os.environ.get(CONTRACTS_ENV)
    if value is None:
        raise RuntimeError(f"{CONTRACTS_ENV} must name a connect-contracts Git checkout")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeError(f"{CONTRACTS_ENV} is not a directory: {path}")
    return path


def _git_show(relative_path: str) -> bytes:
    repository = _contracts_dir()
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "show",
            f"{CONTRACTS_REVISION}:{FIXTURE_ROOT}/{relative_path}",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"canonical Connect fixture unavailable at {CONTRACTS_REVISION}: {detail}"
        )
    return result.stdout


def _load_json(relative_path: str) -> dict[str, object] | list[dict[str, object]]:
    return json.loads(_git_show(relative_path))


def _capability_for(
    manifest_path: str, request: dict[str, object]
) -> connect.DiscoveredCapability:
    manifest_value = _load_json(manifest_path)
    registration_value = _load_json("valid/registration.json")
    assert isinstance(manifest_value, dict)
    assert isinstance(registration_value, dict)
    manifest = connect._AppManifestV2.model_validate(manifest_value)
    registration_value["instance_id"] = manifest.instance_id
    registration_value["app_id"] = manifest.app.id
    registration = connect._RuntimeRegistrationV2.model_validate(registration_value)
    capability_ref = request["capability"]
    assert isinstance(capability_ref, dict)
    return next(
        capability
        for capability in connect._generic_capabilities(
            registration,
            manifest,
            "http://127.0.0.1:43127/",
        )
        if capability.capability_id == capability_ref["id"]
        and capability.capability_version == capability_ref["version"]
    )


def _prepared_job(
    request: dict[str, object], capability: connect.DiscoveredCapability
) -> connect.PreparedCapabilityJob:
    inputs = request["inputs"]
    parameters = request["parameters"]
    assert isinstance(inputs, list) and len(inputs) == 1
    assert isinstance(inputs[0], dict)
    assert isinstance(parameters, dict)
    artifact = inputs[0]
    return connect.PreparedCapabilityJob(
        job_id=str(request["job_id"]),
        provider_app_id=capability.app_id,
        provider_app_version=capability.app_version,
        provider_instance_id=capability.instance_id,
        capability_id=capability.capability_id,
        capability_version=capability.capability_version,
        artifact=connect.ArtifactIdentity(
            artifact_id=str(artifact["artifact_id"]),
            media_type=str(artifact["media_type"]),
            byte_size=int(artifact["byte_size"]),
            sha256=str(artifact["sha256"]),
        ),
        display_name=str(artifact["display_name"]),
        parameters=tuple(parameters.items()),
        request_json=json.dumps(request, separators=(",", ":")).encode("utf-8"),
    )


def _consumer_admits(case: dict[str, object]) -> bool:
    fixture = case["fixture"]
    schema = case["schema"]
    assert isinstance(fixture, str)
    assert isinstance(schema, str)
    value = _load_json(fixture)
    assert isinstance(value, dict)
    try:
        if schema == "manifest.schema.json":
            connect._AppManifestV2.model_validate(value)
        elif schema == "registration.schema.json":
            registration = connect._RuntimeRegistrationV2.model_validate(value)
            manifest_path = case["provider_manifest"]
            assert isinstance(manifest_path, str)
            manifest_value = _load_json(manifest_path)
            assert isinstance(manifest_value, dict)
            manifest = connect._AppManifestV2.model_validate(manifest_value)
            if (
                registration.app_id != manifest.app.id
                or registration.instance_id != manifest.instance_id
                or connect._validated_base_url(registration.transport.base_url) is None
            ):
                return False
        elif schema == "job-status.schema.json":
            request_path = case["request_fixture"]
            manifest_path = case["provider_manifest"]
            assert isinstance(request_path, str)
            assert isinstance(manifest_path, str)
            request = _load_json(request_path)
            assert isinstance(request, dict)
            capability = _capability_for(manifest_path, request)
            response = httpx.Response(200, json=value)
            connect.ConnectV2Client(capability)._job_update(
                response,
                _prepared_job(request, capability),
            )
        elif schema == "error.schema.json":
            connect._ErrorEnvelopeV2.model_validate(value)
        else:
            raise AssertionError(f"unexpected consumer schema: {schema}")
    except (connect.ConnectError, LookupError, TypeError, ValueError):
        return False
    return True


def test_email_watcher_consumer_matches_canonical_v2_fixtures() -> None:
    cases = _load_json("index.json")
    assert isinstance(cases, list)
    consumer_cases = [case for case in cases if case.get("schema") in CONSUMER_SCHEMAS]
    assert {case["schema"] for case in consumer_cases} == CONSUMER_SCHEMAS
    assert any(case["valid"] for case in consumer_cases)
    assert any(not case["valid"] for case in consumer_cases)

    mismatches = []
    for case in consumer_cases:
        admitted = _consumer_admits(case)
        if admitted != case["valid"]:
            mismatches.append(
                {
                    "fixture": case["fixture"],
                    "expected": case["valid"],
                    "admitted": admitted,
                }
            )
    assert mismatches == []
