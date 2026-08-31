from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from eom_email_watcher import connect, engine_api
from eom_email_watcher.db import SCHEMA_VERSION
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

FIXTURE_PART_ID = "fixture-mime-part"


class FixtureModelHandler(BaseHTTPRequestHandler):
    model_id = "connect-proof-model"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, value: dict[str, object]) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self._json({"data": [{"id": self.model_id}]})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            schema_name = request["response_format"]["json_schema"]["name"]
            user_prompt = next(
                message["content"] for message in request["messages"] if message["role"] == "user"
            )
            prompt = json.loads(user_prompt)
            text = self._structured_output(schema_name, prompt)
        except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        self._json({"choices": [{"message": {"content": text}}]})

    @staticmethod
    def _structured_output(schema_name: str, prompt: dict[str, object]) -> str:
        if schema_name == "document_chunk_evidence_v1":
            evidence = []
            for block in prompt["source_blocks"]:
                exact_quote = next(
                    line.strip() for line in block["text"].splitlines() if line.strip()
                )[:200]
                evidence.append(
                    {
                        "block_id": block["block_id"],
                        "claim_text": exact_quote,
                        "exact_quote": exact_quote,
                    }
                )
            return json.dumps({"evidence": evidence}, separators=(",", ":"))
        if schema_name == "document_summary_claims_v1":
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": evidence["claim_text"],
                            "evidence_ids": [evidence["evidence_id"]],
                        }
                        for evidence in prompt["evidence"]
                    ]
                },
                separators=(",", ":"),
            )
        if schema_name == "document_candidate_claims_v1":
            candidate = prompt["candidates"][0]
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": candidate["text"],
                            "candidate_ids": [candidate["candidate_id"]],
                        }
                    ]
                },
                separators=(",", ":"),
            )
        if schema_name == "document_claim_verdicts_v1":
            return json.dumps(
                {
                    "verdicts": [
                        {"claim_id": claim["claim_id"], "verdict": "supported"}
                        for claim in prompt["claims"]
                    ]
                },
                separators=(",", ":"),
            )
        raise ValueError("unsupported fixture response schema")


class FixtureGmail:
    def __init__(self, content: bytes):
        self.content = content

    def attachment_bytes(self, message_id: str, part_id: str, attachment_id: str | None) -> bytes:
        if (message_id, part_id, attachment_id) != (
            "fixture-message",
            FIXTURE_PART_ID,
            "fixture-attachment",
        ):
            raise AssertionError("Unexpected fixture attachment identity")
        return self.content


def write_config(path: Path) -> None:
    path.write_text(
        f'''timezone = "America/Chicago"
gmail_credentials_file = "{path.parent / "credentials.json"}"
gmail_token_file = "{path.parent / "token.json"}"
gmail_send_token_file = "{path.parent / "send-token.json"}"
database_file = "{path.parent / "watcher.sqlite3"}"
model_base_url = "http://127.0.0.1:1234/v1"
model_name = "local-model"
model_require_auth = false
notifications_enabled = false
''',
        encoding="utf-8",
    )
    path.chmod(0o600)


def request(config_path: Path, operation: str, payload: dict[str, object] | None = None):
    return {
        "protocol": 1,
        "operation": operation,
        "config_path": str(config_path),
        "payload": payload or {},
    }


def wait_for_capability(available: bool, timeout_seconds: float = 15) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] = {"items": [], "diagnostic": None}
    while time.monotonic() < deadline:
        latest = connect.discover_capabilities().public_result()
        summary_items = [
            item
            for item in latest["items"]
            if item["capability"]["id"] == connect.CAPABILITY_ID
            and item["capability"]["version"] == connect.CAPABILITY_VERSION
        ]
        if bool(summary_items) is available:
            return latest
        time.sleep(0.1)
    raise RuntimeError(f"Capability availability did not become {available}: {latest}")


def start_provider(binary: Path, environment: dict[str, str]) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [str(binary)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_capability(True)
    except BaseException as error:
        return_code = process.poll()
        stop_provider(process)
        detail = (
            f"provider exited with status {return_code}"
            if return_code is not None
            else "provider stayed running without publishing a usable v2 registration"
        )
        raise RuntimeError(
            f"Document Summarizer did not advertise Local Connect v2 ({detail}); "
            "build the provider from its current main branch before running this proof"
        ) from error
    return process


def stop_provider(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return seconds


def require_proof_checks(checks: dict[str, bool]) -> None:
    failed_checks = sorted(name for name, passed in checks.items() if not passed)
    if failed_checks:
        raise RuntimeError(f"Connect proof failed checks: {', '.join(failed_checks)}")


def privacy_projection(value: object) -> object:
    if isinstance(value, list):
        return [privacy_projection(item) for item in value]
    if not isinstance(value, dict):
        return value
    projected = dict(value)
    inputs = value.get("inputs")
    if not isinstance(inputs, list) or not inputs or not isinstance(inputs[0], dict):
        return projected
    first_input = dict(inputs[0])
    first_input["display_name"] = "<allowed-artifact-display-name>"
    projected["inputs"] = [first_input, *inputs[1:]]
    return projected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exercise Connect with a real provider process and synthetic Gmail bytes"
    )
    parser.add_argument("--provider-binary", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument(
        "--model-base-url",
        help="Use an existing exact-loopback OpenAI-compatible endpoint instead of the fixture",
    )
    parser.add_argument("--model-name")
    parser.add_argument("--model-api-token-file", type=Path)
    parser.add_argument("--model-timeout-seconds", type=positive_seconds)
    args = parser.parse_args()
    configured_model = args.model_base_url is not None or args.model_name is not None
    if configured_model and not (args.model_base_url and args.model_name):
        parser.error("--model-base-url and --model-name must be supplied together")
    if args.model_api_token_file is not None and not configured_model:
        parser.error("--model-api-token-file requires a configured model endpoint")
    provider_binary = args.provider_binary.resolve(strict=True)
    pdf_path = args.pdf.resolve(strict=True)
    pdf = pdf_path.read_bytes()

    model_server: ThreadingHTTPServer | None = None
    model_thread: threading.Thread | None = None
    if configured_model:
        model_base_url = args.model_base_url
        model_name = args.model_name
        model_mode = "configured"
        model_timeout_seconds = args.model_timeout_seconds or 120
    else:
        model_server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModelHandler)
        model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
        model_thread.start()
        model_base_url = f"http://127.0.0.1:{model_server.server_port}/v1/"
        model_name = FixtureModelHandler.model_id
        model_mode = "fixture"
        model_timeout_seconds = args.model_timeout_seconds or 10
    token_path = (
        args.model_api_token_file.resolve(strict=True)
        if args.model_api_token_file is not None
        else None
    )
    provider: subprocess.Popen[bytes] | None = None
    restarted: subprocess.Popen[bytes] | None = None
    original_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")

    def stop_active_providers() -> None:
        nonlocal provider, restarted
        if provider is not None:
            stop_provider(provider)
            provider = None
        if restarted is not None:
            stop_provider(restarted)
            restarted = None

    try:
        with (
            tempfile.TemporaryDirectory(prefix="connect-proof-") as temporary,
            ExitStack() as provider_scope,
        ):
            provider_scope.callback(stop_active_providers)
            root = Path(temporary)
            runtime_dir = root / "runtime"
            data_dir = root / "data"
            email_dir = root / "email"
            for directory in (runtime_dir, data_dir, email_dir):
                directory.mkdir(mode=0o700)
            os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
            environment = os.environ.copy()
            environment.pop("DOC_SUM_MODEL_API_TOKEN_FILE", None)
            environment.update(
                {
                    "XDG_DATA_HOME": str(data_dir),
                    "DOC_SUM_MODEL_BASE_URL": model_base_url,
                    "DOC_SUM_MODEL_NAME": model_name,
                    "DOC_SUM_MODEL_TIMEOUT_SECONDS": str(model_timeout_seconds),
                }
            )
            if token_path is not None:
                environment["DOC_SUM_MODEL_API_TOKEN_FILE"] = str(token_path)

            before = wait_for_capability(False)
            provider = start_provider(provider_binary, environment)
            during = wait_for_capability(True)

            config_path = email_dir / "config.toml"
            write_config(config_path)
            runtime = load_runtime(config_path)
            runtime.store.add_message(
                message_id="fixture-message",
                thread_id=None,
                sender="fixture@example.invalid",
                sender_name="Fixture Sender",
                subject="Fixture document",
                received_at="2026-08-29T12:00:00+00:00",
            )
            runtime.store.replace_attachments(
                "fixture-message",
                (
                    AttachmentDescriptor(
                        FIXTURE_PART_ID,
                        "fixture-attachment",
                        pdf_path.name,
                        "application/pdf",
                        len(pdf),
                        0,
                    ),
                ),
            )
            capabilities = engine_api._response(
                request(
                    config_path,
                    "connect.attachment.capabilities",
                    {"message_id": "fixture-message", "part_id": FIXTURE_PART_ID},
                )
            )
            if not capabilities["ok"]:
                raise RuntimeError(f"Connect capability discovery failed: {capabilities}")
            matches = [
                item
                for item in capabilities["data"]["items"]
                if item["capability"]["id"] == connect.CAPABILITY_ID
                and item["capability"]["version"] == connect.CAPABILITY_VERSION
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    f"Expected one compatible document summary capability: {capabilities}"
                )
            selected = matches[0]
            request_id = str(uuid4())
            invocation = {
                "request_id": request_id,
                "message_id": "fixture-message",
                "part_id": FIXTURE_PART_ID,
                "provider": {
                    "app_id": selected["provider"]["app_id"],
                    "version": selected["provider"]["version"],
                    "instance_id": selected["provider"]["instance_id"],
                },
                "capability": {
                    "id": selected["capability"]["id"],
                    "version": selected["capability"]["version"],
                },
                "parameters": {},
                "confirmed": False,
            }
            original_from_token = engine_api.GmailGateway.__dict__["from_token"]
            engine_api.GmailGateway.from_token = staticmethod(lambda *_args: FixtureGmail(pdf))
            try:
                response = engine_api._response(
                    request(
                        config_path,
                        "connect.attachment.invoke",
                        invocation,
                    )
                )
            finally:
                engine_api.GmailGateway.from_token = original_from_token
            if not response["ok"]:
                raise RuntimeError(f"Connect proof job failed: {response}")
            outputs = response["data"]["outputs"]
            if len(outputs) != 1:
                raise RuntimeError(f"Expected one Connect output: {response}")
            presentation = engine_api._response(
                request(
                    config_path,
                    "connect.output.present",
                    {
                        "message_id": "fixture-message",
                        "part_id": FIXTURE_PART_ID,
                        "job_id": request_id,
                        "artifact_id": outputs[0]["artifact_id"],
                    },
                )
            )
            if not presentation["ok"]:
                raise RuntimeError(f"Connect output presentation failed: {presentation}")
            rendered = presentation["data"]["presentation"]
            if rendered["kind"] != "document_summary":
                raise RuntimeError("Unexpected Connect output presentation kind")
            summary_text = rendered["summary"]["text"]

            stop_provider(provider)
            provider = None
            after_stop = wait_for_capability(False)
            replayed = engine_api._response(
                request(config_path, "connect.attachment.invoke", invocation)
            )
            inbox_without_connect = engine_api._response(
                request(config_path, "inbox.recent", {"limit": 1})
            )
            restarted = start_provider(provider_binary, environment)
            after_restart = wait_for_capability(True)

            with sqlite3.connect(runtime.config.database_file) as database:
                quick_check = database.execute("PRAGMA quick_check").fetchone()[0]
                schema_version = database.execute("PRAGMA user_version").fetchone()[0]
                job_row = database.execute(
                    """SELECT job_id, protocol_version, capability_id, capability_version,
                    provider_app_id, provider_app_version, provider_instance_id,
                    input_media_type, input_byte_size, input_sha256, source_app_id,
                    status, request_json, result_json
                    FROM connect_attachment_jobs WHERE job_id = ?""",
                    (request_id,),
                ).fetchone()
            if job_row is None:
                raise RuntimeError("Connect proof result was not durable")
            durable_request = json.loads(job_row[12])
            replayed_data = replayed.get("data") if replayed["ok"] else None
            restarted_matches = [
                item
                for item in after_restart["items"]
                if item["provider"]["app_id"] == selected["provider"]["app_id"]
                and item["provider"]["version"] == selected["provider"]["version"]
                and item["capability"]["id"] == selected["capability"]["id"]
                and item["capability"]["version"] == selected["capability"]["version"]
            ]
            request_inputs = durable_request.get("inputs")
            serialized_request = json.dumps(
                privacy_projection(durable_request), separators=(",", ":"), sort_keys=True
            )
            request_has_safe_shape = (
                set(durable_request)
                == {"protocol_version", "job_id", "capability", "inputs", "parameters"}
                and isinstance(request_inputs, list)
                and len(request_inputs) == 1
                and isinstance(request_inputs[0], dict)
                and set(request_inputs[0])
                == {
                    "artifact_id",
                    "media_type",
                    "byte_size",
                    "sha256",
                    "display_name",
                    "source_app_id",
                }
            )
            proof_checks = {
                "before_provider_absent": not before["items"],
                "provider_available": bool(during["items"]),
                "provider_removed": not after_stop["items"],
                "provider_restored": len(restarted_matches) == 1,
                "email_database_healthy": quick_check == "ok",
                "email_database_current": schema_version == SCHEMA_VERSION,
                "inbox_healthy_without_connect": (
                    inbox_without_connect["ok"]
                    and inbox_without_connect["data"]["items"][0]["message_id"] == "fixture-message"
                ),
                "input_media_type_matches": job_row[7] == "application/pdf",
                "input_byte_size_matches": job_row[8] == len(pdf),
                "input_sha256_matches": job_row[9] == hashlib.sha256(pdf).hexdigest(),
                "job_completed": response["data"]["status"] == "completed",
                "persisted_capability_matches": (
                    job_row[2] == selected["capability"]["id"]
                    and job_row[3] == selected["capability"]["version"]
                ),
                "persisted_job_completed": job_row[11] == "completed",
                "persisted_protocol_matches": job_row[1] == connect.GENERIC_PROTOCOL_VERSION,
                "persisted_provider_matches": (
                    job_row[4] == selected["provider"]["app_id"]
                    and job_row[5] == selected["provider"]["version"]
                    and job_row[6] == selected["provider"]["instance_id"]
                ),
                "persisted_result_present": job_row[13] is not None,
                "provider_instance_rotated": (
                    len(restarted_matches) == 1
                    and restarted_matches[0]["provider"]["instance_id"]
                    != selected["provider"]["instance_id"]
                ),
                "replayed_completed_job_without_provider": (
                    replayed_data is not None and replayed_data == response["data"]
                ),
                "request_excludes_gmail_identity": (
                    request_has_safe_shape
                    and durable_request["job_id"] == request_id
                    and request_inputs[0]["display_name"]
                    == connect._safe_artifact_display_name(pdf_path.name)
                    and all(
                        private_value not in serialized_request
                        for private_value in (
                            "fixture-message",
                            FIXTURE_PART_ID,
                            "fixture-attachment",
                            "fixture@example.invalid",
                            "Fixture Sender",
                            "Fixture document",
                        )
                    )
                ),
                "source_app_matches": job_row[10] == connect.SOURCE_APP_ID,
            }
            result = {
                "after_restart_capabilities": len(after_restart["items"]),
                "after_stop_capabilities": len(after_stop["items"]),
                "before_provider_capabilities": len(before["items"]),
                "capability_id": selected["capability"]["id"],
                "capability_version": selected["capability"]["version"],
                "during_provider_capabilities": len(during["items"]),
                "email_database_quick_check": quick_check,
                "email_database_schema_version": schema_version,
                "job_status": response["data"]["status"],
                "model_id": model_name,
                "model_mode": model_mode,
                "persisted_job_status": job_row[11],
                "persisted_protocol_version": job_row[1],
                "proof_input": "synthetic Gmail attachment bytes",
                "proof_passed": all(proof_checks.values()),
                "source_app_id": job_row[10],
                "summary_sha256": hashlib.sha256(summary_text.encode()).hexdigest(),
                **proof_checks,
            }
            print(json.dumps(result, separators=(",", ":"), sort_keys=True))
            require_proof_checks(proof_checks)
    finally:
        if provider is not None:
            stop_provider(provider)
        if restarted is not None:
            stop_provider(restarted)
        if model_server is not None:
            model_server.shutdown()
            model_server.server_close()
        if model_thread is not None:
            model_thread.join(timeout=5)
        if original_runtime_dir is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = original_runtime_dir


if __name__ == "__main__":
    main()
