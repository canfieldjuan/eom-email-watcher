"""Retain a real certificate automation proof in a new, private evidence directory.

Run with an isolated XDG_RUNTIME_DIR containing a running invoice-connect provider.
Uses the installed entitlement through the published keyring; never reads it directly.
Message ingestion, attachment discovery, analysis and attachment retrieval are staged.
Provider results are never mocked; --model-kind records any model substitution.
Supply the expected provider instance and versions from its retained launch receipt;
that receipt must record the tested source revision. Manifest versions alone are not
source revision attestation. The selected identity is retained with the result.
--model-kind is required: it records the caller's declaration, not runtime attestation.
Real-model evidence also requires a separately retained runtime/profile receipt.
Run with this checkout's installed package (for example, uv run python scripts/coi_local_proof.py).
The evidence directory contains document text and must remain private.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version as distribution_version
from pathlib import Path
from unittest.mock import patch

from build_desktop_sidecar import validate_entitlement_keyring
from coi_evidence import create_evidence_directory
from connect_automate import connect, entitlement
from tomlkit import dumps as toml_dumps

from eom_email_watcher import engine_api
from eom_email_watcher.config import config_admission_snapshot
from eom_email_watcher.db import AdmissionProvenance
from eom_email_watcher.mailbox import DEFAULT_MAIL_ACCOUNT_ID, DEFAULT_MAIL_PROVIDER
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

# Mailbox state and analysis are seeded; attachment bytes come from the staged adapter.
# Connect discovery, dispatch, provider processing, validation and persistence run unchanged.
ROOT: Path
STATE: Path
CONFIG: Path
IDENTITY = hashlib.sha256(b"coi-m3-isolated-mailbox").hexdigest()
MESSAGE_ID = "coi-m3-proof"
PART_ID = "part-1"
ATTACHMENT_ID = "attachment-1"
CHECKOUT = Path(__file__).resolve().parents[1]


def package_sources(name: str, package: Path) -> dict[str, str]:
    """Fingerprint executable sources and bind loaded modules to that source tree."""
    files = {path.resolve(): path.relative_to(package).as_posix()
             for path in package.rglob("*.py")}
    if not files or any(not path.is_relative_to(package) for path in files):
        raise RuntimeError(f"Cannot identify executable sources for {name}")
    for module_name, module in tuple(sys.modules.items()):
        if module_name != name and not module_name.startswith(name + "."):
            continue
        loaded = getattr(module, "__file__", None)
        if loaded is None or Path(loaded).resolve() not in files:
            raise RuntimeError(f"Loaded {module_name} is outside the recorded checkout/package")
    return {relative: hashlib.sha256(path.read_bytes()).hexdigest()
            for path, relative in sorted(files.items())}


def watcher_identity() -> dict[str, object]:
    """Record the clean checkout and the Connect implementation actually executed."""
    watcher_sources = package_sources(
        "eom_email_watcher", CHECKOUT / "src" / "eom_email_watcher"
    )
    dependency_sources = package_sources(
        "connect_automate", Path(connect.__file__).resolve().parent
    )
    changes = subprocess.check_output(
        ["git", "-C", str(CHECKOUT), "status", "--porcelain", "--untracked-files=all"],
        text=True,
    )
    if changes:
        raise RuntimeError("Proof checkout has uncommitted changes")
    return {
        "watcher_head": subprocess.check_output(
            ["git", "-C", str(CHECKOUT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "engine_sha256": watcher_sources["engine_api.py"],
        "proof_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "connect_dependency": {
            "version": distribution_version("connect-automate"),
            "sources": dependency_sources,
        },
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
    write_private_bytes(name, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def write_private_bytes(name: str, content: bytes) -> None:
    path = ROOT / name
    with path.open("xb") as stream:
        stream.write(content)
    path.chmod(0o600)


def retain_inputs(content: bytes, args: argparse.Namespace) -> dict[str, object]:
    write_private_bytes("input.pdf", content)
    inputs = {
        "input_artifact": "input.pdf",
        "input_sha256": hashlib.sha256(content).hexdigest(),
        "input_byte_size": len(content),
        "expected_policy_count": args.expect_policy_count,
        "expected_error": args.expect_error,
        "today": args.today,
    }
    write_private_json("run-inputs.json", inputs)
    return inputs


def certificate_snapshot(store) -> dict[str, list[dict[str, object]]]:
    def row_values(row):
        return {key: {"base64": base64.b64encode(value).decode("ascii")}
                if isinstance(value, bytes) else value for key, value in dict(row).items()}

    with store.connection() as db:
        return {
            "certificates": [row_values(row) for row in db.execute(
                "SELECT * FROM certificate_records ORDER BY certificate_id"
            )],
            "policies": [row_values(row) for row in db.execute(
                "SELECT * FROM certificate_policy_rows ORDER BY certificate_id, ordinal"
            )],
        }


def replay_terminal_update(runtime, selected, job) -> dict[str, object]:
    if job is None or job.status not in {"completed", "failed"}:
        raise RuntimeError("Terminal replay requires a completed or failed provider job")
    tracked = engine_api._tracked_generic_job(job, selected)
    update = connect.ConnectV2Client(selected).get(tracked)
    if update.status != job.status:
        raise RuntimeError("Provider did not return the recorded terminal status")
    recorded_error = {
        "code": job.error_code, "message": job.error_message,
        "retryable": bool(job.error_retryable),
    } if job.status == "failed" else None
    error = {
        "code": update.error.code, "message": str(update.error),
        "retryable": update.error.retryable,
    } if update.error is not None else None
    if error != recorded_error:
        raise RuntimeError("Provider did not return the recorded terminal error")
    engine_api._apply_connect_update(runtime.store, update)
    receipt = {
        "job_id": update.job_id,
        "status": update.status,
        "provider_app_id": update.provider_app_id,
        "provider_instance_id": update.provider_instance_id,
        "result": update.result.store_dict() if update.result else None,
        "error": error,
        "recorded_error": recorded_error,
    }
    write_private_json("replayed-terminal-update.json", receipt)
    return {"artifact": "replayed-terminal-update.json", "job_id": update.job_id,
            "status": update.status, "method": "ConnectV2Client.get -> _apply_connect_update"}


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
        self.substitutions = {
            "attachment_retrieval": "StagedMailbox returns --pdf bytes; no live mailbox retrieval",
        }

    def seed_message(self, runtime) -> None:
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
                    PART_ID, ATTACHMENT_ID, "certificate.pdf", "application/pdf",
                    len(self.content), 0
                ),
            ),
        )
        self.substitutions.update({
            "message_ingestion": "Store.add_message seeds a retained message; no mailbox ingestion",
            "attachment_discovery": "Store.replace_attachments seeds one attachment descriptor",
        })

    def seed_analysis(self, runtime) -> None:
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
        self.substitutions["message_analysis"] = (
            "Store.mark_analyzed seeds a completed analysis; no analyzer call"
        )

    def mailbox_identity_key(self) -> str:
        return IDENTITY

    def set_operation_timeout(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("invalid mailbox timeout")

    def attachment_bytes(self, message_id: str, part_id: str, attachment_id: str | None) -> bytes:
        if (message_id, part_id, attachment_id) != (MESSAGE_ID, PART_ID, ATTACHMENT_ID):
            raise AssertionError("unexpected attachment identity")
        return self.content


def select_provider(items, expected):
    """Bind discovery to the instance and versions from the retained launch receipt."""
    candidates = [
        item for item in items
        if all(getattr(item, field) == value for field, value in expected.items())
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"expected one pinned certificate provider, found {len(candidates)}")
    return candidates[0]


def projection_matches(ledger, parents, children, count, expect_error):
    """Compare independent DB identities in the order defined by the consumer contract."""
    if (
        ledger.get("ok") is not True
        or len(parents) != (0 if expect_error else 1)
        or len(children) != count
    ):
        return False
    expected = []
    for parent in parents:
        policies = [child for child in children
                    if child["certificate_id"] == parent["certificate_id"]]
        for policy in policies or [None]:
            expiration = policy["expiration_date_iso"] if policy is not None else None
            ordinal = policy["ordinal"] if policy is not None else None
            identity = {
                "certificate_id": parent["certificate_id"],
                "policy_id": policy["policy_id"] if policy is not None else None,
                "policy_ordinal": ordinal,
                "source_message_id": parent["source_message_id"],
                "source_part_id": parent["source_part_id"],
                "connect_job_id": parent["connect_job_id"],
            }
            expected.append(((expiration is None, expiration or "",
                              parent["certificate_id"], ordinal), identity))
    if sum(child["certificate_id"] == parent["certificate_id"]
           for child in children for parent in parents) != len(children):
        return False
    ordered = [identity for _, identity in sorted(expected, key=lambda item: item[0])]
    items = ledger.get("data", {}).get("items")
    if not isinstance(items, list) or len(items) != len(ordered):
        return False
    return all(
        isinstance(row, dict)
        and all(field in row and type(row[field]) is type(value) and row[field] == value
                for field, value in identity.items())
        for row, identity in zip(items, ordered, strict=True)
    )


def stage_authority(bundle: Path, public_keyring: Path) -> str:
    # Packaging and local proof use the same approved authority, owned by connect-automate.
    content = validate_entitlement_keyring(public_keyring)
    keyring = bundle / entitlement.BUNDLED_KEYRING
    keyring.parent.mkdir(parents=True, mode=0o700)
    keyring.write_bytes(content)
    keyring.chmod(0o600)
    sys._MEIPASS = str(bundle)
    return hashlib.sha256(content).hexdigest()


def main() -> None:
    global ROOT, STATE, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--keyring", type=Path, required=True, help="Public release keyring")
    parser.add_argument("--expect-policy-count", type=int, required=True)
    parser.add_argument("--expect-error", choices=["DOCUMENT_UNREADABLE"])
    parser.add_argument("--today", required=True)
    parser.add_argument("--model-kind", choices=["real", "fixture"], required=True)
    parser.add_argument("--expect-provider-instance-id", required=True)
    parser.add_argument("--expect-provider-app-version", required=True)
    parser.add_argument("--expect-capability-version", required=True)
    args = parser.parse_args()
    if args.expect_policy_count < 0 or (args.expect_error and args.expect_policy_count != 0):
        parser.error("Expected errors require zero policy rows; counts must be nonnegative")
    identity = watcher_identity()
    os.umask(0o077)
    content = args.pdf.resolve(strict=True).read_bytes()
    ROOT = create_evidence_directory(args.evidence_dir)
    STATE = ROOT / "watcher-state"
    STATE.mkdir(mode=0o700)
    CONFIG = STATE / "config.toml"
    bundle = ROOT / "bundle"
    keyring_sha256 = stage_authority(bundle, args.keyring)
    inputs = retain_inputs(content, args)
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
    staged = StagedMailbox(content)
    staged.seed_message(runtime)
    expected_provider = {
        "app_id": "invoice-processor",
        "capability_id": "certificate.extract",
        "instance_id": args.expect_provider_instance_id,
        "app_version": args.expect_provider_app_version,
        "capability_version": args.expect_capability_version,
    }
    selected = select_provider(connect.discover_capabilities().items, expected_provider)
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
    staged.seed_analysis(runtime)
    fires = runtime.store.automation_fires_for_message(MESSAGE_ID)
    if len(fires) != 1:
        raise RuntimeError(f"expected one fire, found {len(fires)}")
    fire_id = fires[0].fire_id
    progress: list[dict[str, object]] = []
    deadline = time.monotonic() + 300
    with patch.object(engine_api.GmailGateway, "from_token", return_value=staged):
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
    before_persisted = certificate_snapshot(runtime.store)
    terminal_replay = replay_terminal_update(runtime, selected, job)
    after_persisted = certificate_snapshot(runtime.store)
    write_private_json("replay-persistence.json", {
        "before": before_persisted, "after": after_persisted,
    })
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
        **inputs,
        "terminal_replay": terminal_replay,
        "substituted_boundaries": staged.substitutions,
        "provider_identity": {
            field: getattr(selected, field) for field in expected_provider
        },
        "provider_identity_source": "expected instance/versions from separate launch receipt",
        "release_keyring_sha256": keyring_sha256,
        "fire_state": fire.state,
        "fire_reason": fire.reason,
        "job_status": job.status if job else None,
        "job_error_code": job.error_code if job else None,
        "ledger_api_ok": ledger.get("ok"),
        "ledger_items": len(ledger.get("data", {}).get("items", [])) if ledger.get("ok") else None,
    }
    with runtime.store.connection() as db:
        parents = db.execute("SELECT * FROM certificate_records").fetchall()
        children = db.execute("SELECT * FROM certificate_policy_rows").fetchall()
        jobs = db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0]
    if parents:
        write_private_json("persisted-record.json", json.loads(parents[0]["canonical_result_json"]))
    checks = {
        "one_fire": len(runtime.store.automation_fires_for_message(MESSAGE_ID)) == 1,
        "one_attempt": len(runtime.store.automation_fire_attempts(fire_id)) == 1,
        "one_job": jobs == 1,
        "expected_terminal": (
            fire.state == "failed" and job is not None and job.error_code == args.expect_error
            if args.expect_error
            else fire.state == "completed" and job is not None and job.status == "completed"
        ),
        "expected_projection": projection_matches(
            ledger, parents, children, args.expect_policy_count, args.expect_error
        ),
        "replay_unchanged": before == ledger and before_persisted == after_persisted,
        "restart_unchanged": after_restart == ledger,
    }
    result["checks"] = checks
    result["model_kind"] = args.model_kind
    result["model_kind_source"] = (
        "explicit caller declaration; runtime receipt required for real proof"
    )
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
