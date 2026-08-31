from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from eom_email_watcher import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from connect_reference_provider import (  # noqa: E402
    INSPECT_CAPABILITY_ID,
    INSPECT_OUTPUT_MEDIA_TYPE,
    INSPECT_PAYLOAD,
    REFERENCE_APP_ID,
    SUMMARY_CAPABILITY_ID,
    TRANSLATE_CAPABILITY_ID,
    ReferenceProvider,
)


@pytest.fixture(autouse=True)
def active_connect_entitlement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: connect.entitlement.EntitlementDecision.ACTIVE,
    )


def test_reference_provider_is_discoverable_generic_and_idempotent(tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    provider = ReferenceProvider.start(runtime_dir)
    try:
        catalog = connect.discover_capabilities(runtime_dir)
        capabilities = {
            item.capability_id: item for item in catalog.items if item.app_id == REFERENCE_APP_ID
        }
        assert set(capabilities) == {
            INSPECT_CAPABILITY_ID,
            SUMMARY_CAPABILITY_ID,
            TRANSLATE_CAPABILITY_ID,
        }
        if os.name != "nt":
            assert provider.registration_path.stat().st_mode & 0o777 == 0o600

        content = b"%PDF-1.4\nreference provider fixture\n%%EOF"
        capability = capabilities[TRANSLATE_CAPABILITY_ID]
        job = connect.prepare_capability_job(
            capability,
            content,
            "application/pdf",
            "received.pdf",
            parameters={"target-language": "Spanish"},
        )
        client = connect.ConnectV2Client(capability)
        completed = client.submit(job, content)
        replayed = client.submit(job, content)

        assert completed.status == "completed"
        assert completed.result is not None
        assert completed.result.outputs[0].payload == b"Reference translation target: Spanish.\n"
        assert replayed.result == completed.result
        assert provider.submission_count(job.job_id) == 2

        changed = connect.prepare_capability_job(
            capability,
            content + b"changed",
            "application/pdf",
            "received.pdf",
            parameters={"target-language": "Spanish"},
            job_id=job.job_id,
        )
        with pytest.raises(connect.ConnectError) as conflict:
            client.submit(changed, content + b"changed")
        assert conflict.value.code == "JOB_CONFLICT"

        inspect_capability = capabilities[INSPECT_CAPABILITY_ID]
        inspect_job = connect.prepare_capability_job(
            inspect_capability,
            content,
            "application/pdf",
            "received.pdf",
        )
        inspection = connect.ConnectV2Client(inspect_capability).submit(inspect_job, content)
        assert inspection.result is not None
        assert inspection.result.outputs[0].media_type == INSPECT_OUTPUT_MEDIA_TYPE
        assert inspection.result.outputs[0].display_name == "../../reference-inspection.json"
        assert inspection.result.outputs[0].payload == INSPECT_PAYLOAD
        assert provider.submission_count(inspect_job.job_id) == 1
        assert provider.post_attempt_count() == 4
    finally:
        provider.stop()

    assert all(
        item.app_id != REFERENCE_APP_ID for item in connect.discover_capabilities(runtime_dir).items
    )
