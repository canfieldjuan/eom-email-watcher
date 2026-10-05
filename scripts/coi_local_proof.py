"""Retain a real certificate automation proof in a new, private evidence directory.

Run with an isolated XDG_RUNTIME_DIR containing a running invoice-connect provider.
Uses the installed entitlement through the published keyring; never reads it directly.
Only mailbox retrieval is staged from --pdf. Provider results are never mocked.
--model-kind is required: it records the caller's declaration, not runtime attestation.
Real-model evidence also requires a separately retained runtime/profile receipt.
Run with this checkout's installed package (for example, uv run python scripts/coi_local_proof.py).
The evidence directory contains document text and must remain private.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from connect_automate import connect, entitlement
from tomlkit import dumps as toml_dumps

from eom_email_watcher import engine_api
from eom_email_watcher.config import config_admission_snapshot
from eom_email_watcher.db import AdmissionProvenance
from eom_email_watcher.mailbox import DEFAULT_MAIL_ACCOUNT_ID, DEFAULT_MAIL_PROVIDER
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

# The only replaced boundary is mailbox attachment retrieval. All Connect discovery,
# dispatch, provider processing, validation and persistence run unchanged.
ROOT: Path
INPUT: Path
STATE: Path
CONFIG: Path
IDENTITY = hashlib.sha256(b"coi-m3-isolated-mailbox").hexdigest()
MESSAGE_ID = "coi-m3-proof"
PART_ID = "part-1"
ATTACHMENT_ID = "attachment-1"
CHECKOUT = Path(__file__).resolve().parents[1]


def watcher_identity() -> dict[str, str]:
    """Only attribute loaded, unchanged checkout code to its Git revision."""
    package = CHECKOUT / "src" / "eom_email_watcher"
    for name, module in tuple(sys.modules.items()):
        if name != "eom_email_watcher" and not name.startswith("eom_email_watcher."):
            continue
        loaded = getattr(module, "__file__", None)
        if loaded is None or not Path(loaded).resolve().is_relative_to(package):
            raise RuntimeError(f"Loaded {name} is not from the proof checkout")
    changes = subprocess.check_output(
        ["git", "-C", str(CHECKOUT), "status", "--porcelain", "--", "src/eom_email_watcher"],
        text=True,
    )
    if changes:
        raise RuntimeError("Proof checkout has uncommitted watcher source changes")
    return {
        "watcher_head": subprocess.check_output(
            ["git", "-C", str(CHECKOUT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "engine_sha256": hashlib.sha256(Path(engine_api.__file__).read_bytes()).hexdigest(),
        "proof_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def require_checks(checks: dict[str, bool]) -> None:
    required = {
        "one_fire",
        "one_attempt",
        "one_job",
        "expected_terminal",
        "expected_projection",
        "replay_unchanged",
        "restart_unchanged",
    }
    if set(checks) != required or any(value is not True for value in checks.values()):
        raise RuntimeError(f"Incomplete or failed proof checks: {checks}")


def write_private_json(name: str, value: object) -> None:
    path = ROOT / name
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    path.chmod(0o600)


def request(operation: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "protocol": 1,
        "operation": operation,
        "config_path": str(CONFIG),
        "payload": payload or {},
    }
    if operation in engine_api.ADMISSION_REQUIRED_OPERATIONS:
        value["admission_token"] = config_admission_snapshot(CONFIG).token
    return value


class StagedMailbox:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def mailbox_identity_key(self) -> str:
        return IDENTITY

    def set_operation_timeout(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("invalid mailbox timeout")

    def attachment_bytes(self, message_id: str, part_id: str, attachment_id: str | None) -> bytes:
        if (message_id, part_id, attachment_id) != (MESSAGE_ID, PART_ID, ATTACHMENT_ID):
            raise AssertionError("unexpected attachment identity")
        return self.content


def stage_authority(bundle: Path, public_keyring: Path) -> None:
    keyring = bundle / entitlement.BUNDLED_KEYRING
    keyring.parent.mkdir(parents=True, mode=0o700)
    shutil.copyfile(public_keyring, keyring)
    keyring.chmod(0o600)
    sys._MEIPASS = str(bundle)


def main() -> None:
    global ROOT, INPUT, STATE, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--keyring", type=Path, required=True, help="Public release keyring")
    parser.add_argument("--expect-policy-count", type=int, required=True)
    parser.add_argument("--expect-error", choices=["DOCUMENT_UNREADABLE"])
    parser.add_argument("--today", required=True)
    parser.add_argument("--model-kind", choices=["real", "fixture"], required=True)
    args = parser.parse_args()
    if args.expect_policy_count < 0 or (args.expect_error and args.expect_policy_count != 0):
        parser.error("Expected errors require zero policy rows; counts must be nonnegative")
    identity = watcher_identity()
    os.umask(0o077)
    INPUT = args.pdf.resolve(strict=True)
    ROOT = args.evidence_dir.resolve()
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=False)
    STATE = ROOT / "watcher-state"
    STATE.mkdir(mode=0o700)
    CONFIG = STATE / "config.toml"
    bundle = ROOT / "bundle"
    stage_authority(bundle, args.keyring)
    content = INPUT.read_bytes()
    CONFIG.write_text(
        toml_dumps({
            "timezone": "America/Chicago",
            "gmail_credentials_file": str(STATE / "credentials.json"),
            "gmail_token_file": str(STATE / "token.json"),
            "gmail_send_token_file": str(STATE / "send-token.json"),
            "database_file": str(STATE / "watcher.sqlite3"),
            "model_base_url": "http://127.0.0.1:18081/v1",
            "model_name": "qwen35-9b",
            "model_require_auth": False,
            "notifications_enabled": False,
        }),
        encoding="utf-8",
    )
    CONFIG.chmod(0o600)
    runtime = load_runtime(CONFIG)
    runtime.config.gmail_token_file.write_text("isolated-fixture-only", encoding="utf-8")
    runtime.config.gmail_token_file.chmod(0o600)
    runtime.store.reconcile_mailbox_identity(
        DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID, IDENTITY, legacy_status="replacement"
    )
    now = datetime.now(UTC).isoformat()
    runtime.store.add_message(
        message_id=MESSAGE_ID,
        thread_id=None,
        sender="fixture@example.invalid",
        sender_name="Isolated fixture",
        subject="Approved COI proof",
        received_at=now,
        provider=DEFAULT_MAIL_PROVIDER,
        account_id=DEFAULT_MAIL_ACCOUNT_ID,
        provider_message_id=MESSAGE_ID,
        mailbox_identity_key=IDENTITY,
        admission=AdmissionProvenance(
            kind="exact_sender",
            selector_id="sender:fixture@example.invalid",
            display_name="Isolated fixture",
            mailbox_identity_key=IDENTITY,
            admitted_at=now,
        ),
    )
    runtime.store.replace_attachments(
        MESSAGE_ID,
        (
            AttachmentDescriptor(
                PART_ID, ATTACHMENT_ID, "certificate.pdf", "application/pdf", len(content), 0
            ),
        ),
    )
    candidates = [
        item
        for item in connect.discover_capabilities().items
        if item.app_id == "invoice-processor" and item.capability_id == "certificate.extract"
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"expected one certificate provider, found {len(candidates)}")
    selected = candidates[0]
    rule = {
        "name": "COI M3 isolated proof",
        "scope": {},
        "trigger": {"source_kind": "mail.message"},
        "conditions": [
            {"field": "attachment.media_type", "op": "equals", "value": "application/pdf"}
        ],
        "action": {
            "kind": "connect.invoke",
            "capability": {"id": selected.capability_id, "version": selected.capability_version},
            "provider": {
                "app_id": selected.app_id,
                "version": selected.app_version,
                "instance_id": selected.instance_id,
            },
            "parameters": {},
        },
        "confirm_each": False,
    }
    put = engine_api._response(request("automation.rules.put", {"definition": rule}))
    if not put.get("ok"):
        raise RuntimeError(f"rule put failed: {put.get('error', {}).get('code')}")
    runtime.store.mark_analyzed(
        MESSAGE_ID,
        {
            "category": "informational",
            "priority": "normal",
            "summary": "Certificate arrived.",
            "action_required": True,
            "suggested_action": "Review certificate expiry.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=IDENTITY,
    )
    fires = runtime.store.automation_fires_for_message(MESSAGE_ID)
    if len(fires) != 1:
        raise RuntimeError(f"expected one fire, found {len(fires)}")
    fire_id = fires[0].fire_id
    progress: list[dict[str, object]] = []
    deadline = time.monotonic() + 300
    with patch.object(engine_api.GmailGateway, "from_token", return_value=StagedMailbox(content)):
        while time.monotonic() < deadline:
            pumped = engine_api._response(request("connect.queue.pump", {"limit": 25}))
            if not pumped.get("ok"):
                raise RuntimeError(f"queue pump failed: {pumped.get('error', {}).get('code')}")
            fire = runtime.store.automation_fire(fire_id)
            assert fire is not None
            job = runtime.store.connect_job(fire.job_id) if fire.job_id else None
            state = {
                "fire_state": fire.state,
                "fire_reason": fire.reason,
                "job_status": job.status if job else None,
                "job_error_code": job.error_code if job else None,
            }
            if not progress or state != progress[-1]:
                progress.append(state)
                print("progress", json.dumps(state, sort_keys=True), flush=True)
            if fire.state in {"completed", "failed", "manual_review", "source_unavailable"}:
                break
            time.sleep(1)
    ledger = engine_api._response(
        request("certificate.expiry_ledger.list", {"today": args.today, "limit": 100})
    )
    before = ledger
    with patch.object(engine_api.GmailGateway, "from_token", return_value=StagedMailbox(content)):
        for _ in range(3):
            replay = engine_api._response(request("connect.queue.pump", {"limit": 25}))
            if not replay.get("ok"):
                raise RuntimeError("Replay pump failed")
    ledger = engine_api._response(
        request("certificate.expiry_ledger.list", {"today": args.today, "limit": 100})
    )
    restarted = subprocess.run(
        [sys.executable, "-m", "eom_email_watcher.engine_api"],
        cwd=CHECKOUT,
        env={**os.environ, "PYTHONPATH": str(CHECKOUT / "src")},
        input=json.dumps(
            request("certificate.expiry_ledger.list", {"today": args.today, "limit": 100})
        ),
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    after_restart = json.loads(restarted.stdout)
    write_private_json("restart-ledger-result.json", after_restart)
    write_private_json("watcher-progress.json", progress)
    write_private_json("watcher-ledger-result.json", ledger)
    fire = runtime.store.automation_fire(fire_id)
    assert fire is not None
    job = runtime.store.connect_job(fire.job_id) if fire.job_id else None
    result = {
        "input_sha256": hashlib.sha256(content).hexdigest(),
        "mailbox_source": "isolated staged adapter, not live Gmail",
        "provider_capability": selected.capability_id,
        "fire_state": fire.state,
        "fire_reason": fire.reason,
        "job_status": job.status if job else None,
        "job_error_code": job.error_code if job else None,
        "ledger_api_ok": ledger.get("ok"),
        "ledger_items": len(ledger.get("data", {}).get("items", [])) if ledger.get("ok") else None,
    }
    with runtime.store.connection() as db:
        parents = db.execute("SELECT canonical_result_json FROM certificate_records").fetchall()
        children = db.execute("SELECT COUNT(*) FROM certificate_policy_rows").fetchone()[0]
        jobs = db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0]
    if parents:
        write_private_json("persisted-record.json", json.loads(parents[0][0]))
    checks = {
        "one_fire": len(runtime.store.automation_fires_for_message(MESSAGE_ID)) == 1,
        "one_attempt": len(runtime.store.automation_fire_attempts(fire_id)) == 1,
        "one_job": jobs == 1,
        "expected_terminal": (
            fire.state == "failed" and job is not None and job.error_code == args.expect_error
            if args.expect_error
            else fire.state == "completed" and job is not None and job.status == "completed"
        ),
        "expected_projection": (
            ledger.get("ok") is True
            and len(parents) == (0 if args.expect_error else 1)
            and children == args.expect_policy_count
            and len(ledger.get("data", {}).get("items", []))
            == (0 if args.expect_error else max(1, args.expect_policy_count))
        ),
        "replay_unchanged": before == ledger,
        "restart_unchanged": after_restart == ledger,
    }
    result["checks"] = checks
    result["model_kind"] = args.model_kind
    result["model_kind_source"] = (
        "explicit caller declaration; runtime receipt required for real proof"
    )
    result["expected_error"] = args.expect_error
    if watcher_identity() != identity:
        raise RuntimeError("Proof checkout changed during the run")
    result.update(identity)
    write_private_json("watcher-result-summary.json", result)
    print(
        json.dumps({"checks": checks, "job_error_code": result["job_error_code"]}, sort_keys=True)
    )
    require_checks(checks)


if __name__ == "__main__":
    main()
