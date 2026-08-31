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
from contextlib import ExitStack, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
from connect_reference_provider import (
    INSPECT_CAPABILITY_ID,
    INSPECT_OUTPUT_MEDIA_TYPE,
    INSPECT_PAYLOAD,
    REFERENCE_APP_ID,
    SUMMARY_CAPABILITY_ID,
    TRANSLATE_CAPABILITY_ID,
    ReferenceProvider,
)

from eom_email_watcher import connect, engine_api
from eom_email_watcher.db import SCHEMA_VERSION
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

FIXTURE_PART_ID = "fixture-mime-part"


class FixtureModelHandler(BaseHTTPRequestHandler):
    model_id = "connect-proof-model"
    pause_next_generation = False
    generation_started = threading.Event()
    generation_release = threading.Event()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, value: dict[str, object]) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(encoded)

    @classmethod
    def pause_one_generation(cls) -> None:
        cls.generation_started.clear()
        cls.generation_release.clear()
        cls.pause_next_generation = True

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
            handler = type(self)
            if handler.pause_next_generation:
                handler.pause_next_generation = False
                handler.generation_started.set()
                if not handler.generation_release.wait(timeout=15):
                    self.send_error(504)
                    return
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

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModelHandler)
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()
    fixture_model_base_url = f"http://127.0.0.1:{model_server.server_port}/v1/"
    if configured_model:
        model_base_url = args.model_base_url
        model_name = args.model_name
        model_mode = "configured"
        model_timeout_seconds = args.model_timeout_seconds or 120
    else:
        model_base_url = fixture_model_base_url
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
    recovered: subprocess.Popen[bytes] | None = None
    reference_provider: ReferenceProvider | None = None
    original_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    original_connect_client_factory = connect._client

    def stop_active_providers() -> None:
        nonlocal provider, recovered, reference_provider, restarted
        if provider is not None:
            stop_provider(provider)
            provider = None
        if restarted is not None:
            stop_provider(restarted)
            restarted = None
        if recovered is not None:
            stop_provider(recovered)
            recovered = None
        if reference_provider is not None:
            reference_provider.stop()
            reference_provider = None

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
            restart_environment = environment.copy()
            restart_environment.pop("DOC_SUM_MODEL_API_TOKEN_FILE", None)
            restart_environment.update(
                {
                    "DOC_SUM_MODEL_BASE_URL": fixture_model_base_url,
                    "DOC_SUM_MODEL_NAME": FixtureModelHandler.model_id,
                    "DOC_SUM_MODEL_TIMEOUT_SECONDS": "10",
                }
            )

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

            reference_provider = ReferenceProvider.start(runtime_dir)
            reference_capabilities = engine_api._response(
                request(
                    config_path,
                    "connect.attachment.capabilities",
                    {"message_id": "fixture-message", "part_id": FIXTURE_PART_ID},
                )
            )
            if not reference_capabilities["ok"]:
                raise RuntimeError(
                    f"Reference capability discovery failed: {reference_capabilities}"
                )
            reference_items = reference_capabilities["data"]["items"]
            summary_choices = [
                item
                for item in reference_items
                if item["capability"]["id"] == SUMMARY_CAPABILITY_ID
                and item["capability"]["version"] == "1.0"
            ]
            translation_choices = [
                item
                for item in reference_items
                if item["provider"]["app_id"] == REFERENCE_APP_ID
                and item["capability"]["id"] == TRANSLATE_CAPABILITY_ID
                and item["capability"]["version"] == "1.0"
            ]
            inspection_choices = [
                item
                for item in reference_items
                if item["provider"]["app_id"] == REFERENCE_APP_ID
                and item["capability"]["id"] == INSPECT_CAPABILITY_ID
                and item["capability"]["version"] == "1.0"
                and item["capability"]["produces"] == [INSPECT_OUTPUT_MEDIA_TYPE]
            ]
            if len(translation_choices) != 1 or len(inspection_choices) != 1:
                raise RuntimeError(
                    f"Reference provider did not expose its generic capabilities: "
                    f"{reference_capabilities}"
                )
            translation = translation_choices[0]
            inspection = inspection_choices[0]
            translation_request_id = str(uuid4())
            inspection_request_id = str(uuid4())
            translation_invocation = {
                "request_id": translation_request_id,
                "message_id": "fixture-message",
                "part_id": FIXTURE_PART_ID,
                "provider": {
                    "app_id": translation["provider"]["app_id"],
                    "version": translation["provider"]["version"],
                    "instance_id": translation["provider"]["instance_id"],
                },
                "capability": {
                    "id": translation["capability"]["id"],
                    "version": translation["capability"]["version"],
                },
                "parameters": {"target-language": "Spanish"},
                "confirmed": False,
            }
            stale_request_id = str(uuid4())
            stale_invocation = {
                **translation_invocation,
                "request_id": stale_request_id,
                "capability": {
                    **translation_invocation["capability"],
                    "version": "9.0",
                },
            }
            inspection_invocation = {
                "request_id": inspection_request_id,
                "message_id": "fixture-message",
                "part_id": FIXTURE_PART_ID,
                "provider": {
                    "app_id": inspection["provider"]["app_id"],
                    "version": inspection["provider"]["version"],
                    "instance_id": inspection["provider"]["instance_id"],
                },
                "capability": {
                    "id": inspection["capability"]["id"],
                    "version": inspection["capability"]["version"],
                },
                "parameters": {},
                "confirmed": False,
            }
            stale_post_attempts_before = reference_provider.post_attempt_count()
            engine_api.GmailGateway.from_token = staticmethod(
                lambda *_args: (_ for _ in ()).throw(
                    AssertionError("stale capability selection reached Gmail")
                )
            )
            try:
                stale_response = engine_api._response(
                    request(
                        config_path,
                        "connect.attachment.invoke",
                        stale_invocation,
                    )
                )
            finally:
                engine_api.GmailGateway.from_token = original_from_token
            stale_post_attempts_after = reference_provider.post_attempt_count()
            engine_api.GmailGateway.from_token = staticmethod(lambda *_args: FixtureGmail(pdf))
            try:
                translation_response = engine_api._response(
                    request(
                        config_path,
                        "connect.attachment.invoke",
                        translation_invocation,
                    )
                )
                inspection_response = engine_api._response(
                    request(
                        config_path,
                        "connect.attachment.invoke",
                        inspection_invocation,
                    )
                )
            finally:
                engine_api.GmailGateway.from_token = original_from_token
            if not translation_response["ok"] or not inspection_response["ok"]:
                raise RuntimeError(
                    "Reference capability invocation failed: "
                    f"translation={translation_response}, inspection={inspection_response}"
                )
            translation_output = translation_response["data"]["outputs"][0]
            inspection_output = inspection_response["data"]["outputs"][0]
            translation_presentation = engine_api._response(
                request(
                    config_path,
                    "connect.output.present",
                    {
                        "message_id": "fixture-message",
                        "part_id": FIXTURE_PART_ID,
                        "job_id": translation_request_id,
                        "artifact_id": translation_output["artifact_id"],
                    },
                )
            )
            if not translation_presentation["ok"]:
                raise RuntimeError(
                    f"Reference output presentation failed: {translation_presentation}"
                )
            inspection_presentation = engine_api._response(
                request(
                    config_path,
                    "connect.output.present",
                    {
                        "message_id": "fixture-message",
                        "part_id": FIXTURE_PART_ID,
                        "job_id": inspection_request_id,
                        "artifact_id": inspection_output["artifact_id"],
                    },
                )
            )
            export_dir = root / "reference-export"
            export_dir.mkdir(mode=0o700)

            def export_inspection_output() -> dict[str, object]:
                return engine_api._response(
                    request(
                        config_path,
                        "connect.output.export",
                        {
                            "message_id": "fixture-message",
                            "part_id": FIXTURE_PART_ID,
                            "job_id": inspection_request_id,
                            "artifact_id": inspection_output["artifact_id"],
                            "destination_dir": str(export_dir),
                        },
                    )
                )

            inspection_exports = tuple(export_inspection_output() for _ in range(2))
            if not inspection_presentation["ok"] or any(
                not export["ok"] for export in inspection_exports
            ):
                raise RuntimeError(
                    "Opaque output handling failed: "
                    f"present={inspection_presentation}, exports={inspection_exports}"
                )
            inspection_export_paths = tuple(
                Path(export["data"]["path"]) for export in inspection_exports
            )
            translation_job = runtime.store.connect_job(translation_request_id)
            inspection_job = runtime.store.connect_job(inspection_request_id)
            reference_requests = reference_provider.requests()
            reference_request_json = json.dumps(
                privacy_projection(reference_requests), separators=(",", ":"), sort_keys=True
            )
            reference_instance_id = reference_provider.instance_id
            stale_job = runtime.store.connect_job(stale_request_id)
            stale_submissions = reference_provider.submission_count(stale_request_id)
            translation_submissions = reference_provider.submission_count(translation_request_id)
            inspection_submissions = reference_provider.submission_count(inspection_request_id)
            reference_provider.stop()
            reference_provider = None
            after_reference_stop = engine_api._response(
                request(
                    config_path,
                    "connect.attachment.capabilities",
                    {"message_id": "fixture-message", "part_id": FIXTURE_PART_ID},
                )
            )
            reference_inbox_after_stop = engine_api._response(
                request(config_path, "inbox.recent", {"limit": 1})
            )
            if not after_reference_stop["ok"] or not reference_inbox_after_stop["ok"]:
                raise RuntimeError(
                    "Reference provider removal damaged Email Watcher state: "
                    f"capabilities={after_reference_stop}, inbox={reference_inbox_after_stop}"
                )

            stop_provider(provider)
            provider = None
            after_stop = wait_for_capability(False)
            replayed = engine_api._response(
                request(config_path, "connect.attachment.invoke", invocation)
            )
            inbox_without_connect = engine_api._response(
                request(config_path, "inbox.recent", {"limit": 1})
            )
            restarted = start_provider(provider_binary, restart_environment)
            after_restart = wait_for_capability(True)

            interrupted_invocation = {**invocation, "request_id": str(uuid4())}
            interrupted_result: dict[str, object] = {}
            interrupted_provider_submissions = 0

            def instrumented_connect_client() -> httpx.Client:
                client = original_connect_client_factory()

                def record_request(request: httpx.Request) -> None:
                    nonlocal interrupted_provider_submissions
                    if request.method == "POST" and request.url.path == "/v2/jobs":
                        interrupted_provider_submissions += 1

                client.event_hooks["request"].append(record_request)
                return client

            connect._client = instrumented_connect_client

            def invoke_interrupted_job() -> None:
                try:
                    interrupted_result["response"] = engine_api._response(
                        request(
                            config_path,
                            "connect.attachment.invoke",
                            interrupted_invocation,
                        )
                    )
                except BaseException as error:
                    interrupted_result["error_type"] = type(error).__name__

            FixtureModelHandler.pause_one_generation()
            engine_api.GmailGateway.from_token = staticmethod(lambda *_args: FixtureGmail(pdf))
            interrupted_thread = threading.Thread(
                target=invoke_interrupted_job,
                name="connect-interrupted-invocation",
            )
            interrupted_thread.start()
            try:
                if not FixtureModelHandler.generation_started.wait(timeout=10):
                    raise RuntimeError(
                        "Interrupted Connect job did not reach deterministic model work"
                    )
                stop_provider(restarted)
                restarted = None
                after_interruption = wait_for_capability(False)
            finally:
                FixtureModelHandler.generation_release.set()
                interrupted_thread.join(timeout=15)
                engine_api.GmailGateway.from_token = original_from_token
            if interrupted_thread.is_alive():
                raise RuntimeError("Interrupted Connect invocation did not stop")
            if "error_type" in interrupted_result:
                raise RuntimeError(
                    f"Interrupted Connect invocation raised {interrupted_result['error_type']}"
                )
            interrupted_response = interrupted_result.get("response")
            if not isinstance(interrupted_response, dict):
                raise RuntimeError("Interrupted Connect invocation returned no response")

            recovered = start_provider(provider_binary, restart_environment)
            after_job_restart = wait_for_capability(True)
            gmail_reconciliation_reads = 0

            def reject_gmail_reopen(*_args: object) -> FixtureGmail:
                nonlocal gmail_reconciliation_reads
                gmail_reconciliation_reads += 1
                raise AssertionError("Provider reconciliation must not reopen Gmail")

            engine_api.GmailGateway.from_token = staticmethod(reject_gmail_reopen)
            try:
                reconciled_interruption = engine_api._response(
                    request(
                        config_path,
                        "connect.attachment.invoke",
                        interrupted_invocation,
                    )
                )
            finally:
                engine_api.GmailGateway.from_token = original_from_token

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
                interrupted_rows = database.execute(
                    """SELECT job_id, provider_instance_id, status, error_code,
                    error_retryable
                    FROM connect_attachment_jobs WHERE job_id = ?""",
                    (interrupted_invocation["request_id"],),
                ).fetchall()
            if job_row is None:
                raise RuntimeError("Connect proof result was not durable")
            durable_request = json.loads(job_row[12])
            replayed_data = replayed.get("data") if replayed["ok"] else None
            interrupted_error = interrupted_response.get("error")
            interrupted_error_code = (
                interrupted_error.get("code") if isinstance(interrupted_error, dict) else None
            )
            reconciled_error = reconciled_interruption.get("error")
            reconciled_error_code = (
                reconciled_error.get("code") if isinstance(reconciled_error, dict) else None
            )
            interrupted_row = interrupted_rows[0] if len(interrupted_rows) == 1 else None
            restarted_matches = [
                item
                for item in after_restart["items"]
                if item["provider"]["app_id"] == selected["provider"]["app_id"]
                and item["provider"]["version"] == selected["provider"]["version"]
                and item["capability"]["id"] == selected["capability"]["id"]
                and item["capability"]["version"] == selected["capability"]["version"]
            ]
            recovered_matches = [
                item
                for item in after_job_restart["items"]
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
            reference_capability_ids = {
                item["capability"]["id"]
                for item in reference_items
                if item["provider"]["app_id"] == REFERENCE_APP_ID
            }
            reference_inbox_results = reference_inbox_after_stop["data"]["items"][0]["attachments"][
                0
            ]["capability_results"]
            reference_results_by_job = {
                item["job_id"]: item
                for item in reference_inbox_results
                if item.get("provider", {}).get("app_id") == REFERENCE_APP_ID
            }
            after_reference_items = after_reference_stop["data"]["items"]
            translation_durable_request = (
                json.loads(translation_job.request_json)
                if translation_job is not None and translation_job.request_json is not None
                else None
            )
            proof_checks = {
                "before_provider_absent": not before["items"],
                "provider_available": bool(during["items"]),
                "provider_removed": not after_stop["items"],
                "provider_restored": len(restarted_matches) == 1,
                "provider_instance_stable": (
                    len(restarted_matches) == 1
                    and restarted_matches[0]["provider"]["instance_id"]
                    == selected["provider"]["instance_id"]
                ),
                "interrupted_provider_removed": not after_interruption["items"],
                "interrupted_provider_restored": (
                    len(recovered_matches) == 1
                    and recovered_matches[0]["provider"]["instance_id"]
                    == selected["provider"]["instance_id"]
                ),
                "interrupted_submission_ambiguous": (
                    interrupted_response.get("ok") is False
                    and interrupted_error_code == "provider_unavailable"
                ),
                "interrupted_job_reconciled": (
                    reconciled_interruption.get("ok") is False
                    and reconciled_error_code == "provider_restarted"
                ),
                "interrupted_job_persisted_once": (
                    interrupted_row is not None
                    and interrupted_row[0] == interrupted_invocation["request_id"]
                    and interrupted_row[1] == selected["provider"]["instance_id"]
                    and interrupted_row[2] == "failed"
                    and interrupted_row[3] == "PROVIDER_RESTARTED"
                    and interrupted_row[4] == 1
                ),
                "interrupted_provider_submitted_once": interrupted_provider_submissions == 1,
                "interrupted_reconciliation_skipped_gmail": gmail_reconciliation_reads == 0,
                "multiple_provider_selection_available": (
                    len(summary_choices) == 2
                    and {item["provider"]["app_id"] for item in summary_choices}
                    == {selected["provider"]["app_id"], REFERENCE_APP_ID}
                ),
                "stale_capability_version_rejected_before_handoff": (
                    stale_response.get("ok") is False
                    and stale_response.get("error", {}).get("code") == "capability_unavailable"
                    and stale_job is None
                    and stale_submissions == 0
                    and stale_post_attempts_after == stale_post_attempts_before
                ),
                "reference_capabilities_discovered": reference_capability_ids
                == {
                    INSPECT_CAPABILITY_ID,
                    SUMMARY_CAPABILITY_ID,
                    TRANSLATE_CAPABILITY_ID,
                },
                "reference_capability_jobs_completed": (
                    translation_response["data"]["status"] == "completed"
                    and translation_job is not None
                    and translation_job.status == "completed"
                    and inspection_response["data"]["status"] == "completed"
                    and inspection_job is not None
                    and inspection_job.status == "completed"
                ),
                "reference_capability_provenance_persisted": (
                    translation_job is not None
                    and translation_job.provider_app_id == REFERENCE_APP_ID
                    and translation_job.provider_instance_id == reference_instance_id
                    and translation_job.capability_id == TRANSLATE_CAPABILITY_ID
                    and inspection_job is not None
                    and inspection_job.provider_app_id == REFERENCE_APP_ID
                    and inspection_job.provider_instance_id == reference_instance_id
                    and inspection_job.capability_id == INSPECT_CAPABILITY_ID
                ),
                "reference_capability_parameters_persisted": (
                    isinstance(translation_durable_request, dict)
                    and translation_durable_request["parameters"] == {"target-language": "Spanish"}
                    and translation_durable_request["inputs"][0]["source_app_id"]
                    == connect.SOURCE_APP_ID
                ),
                "reference_output_presented_as_text": (
                    translation_presentation["data"]["presentation"]
                    == {
                        "kind": "text",
                        "text": "Reference translation target: Spanish.\n",
                    }
                ),
                "unknown_output_uses_safe_path": (
                    inspection_output["media_type"] == INSPECT_OUTPUT_MEDIA_TYPE
                    and set(inspection_output)
                    == {
                        "artifact_id",
                        "media_type",
                        "display_name",
                        "byte_size",
                        "sha256",
                    }
                    and inspection_presentation
                    == {
                        "data": {
                            "job_id": inspection_request_id,
                            "output": inspection_output,
                            "presentation": {"kind": "opaque"},
                        },
                        "ok": True,
                        "operation": "connect.output.present",
                        "protocol": engine_api.PROTOCOL_VERSION,
                    }
                    and len(set(inspection_export_paths)) == 2
                    and all(path.parent == export_dir for path in inspection_export_paths)
                    and all(path.suffix == ".bin" for path in inspection_export_paths)
                    and all(
                        "reference-inspection" not in path.name for path in inspection_export_paths
                    )
                    and all(
                        path.read_bytes() == INSPECT_PAYLOAD for path in inspection_export_paths
                    )
                    and all(
                        os.name == "nt" or path.stat().st_mode & 0o777 == 0o600
                        for path in inspection_export_paths
                    )
                ),
                "reference_provider_requests_private": (
                    len(reference_requests) == 2
                    and all(
                        private_value not in reference_request_json
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
                "reference_provider_submitted_once_per_job": (
                    translation_submissions == 1 and inspection_submissions == 1
                ),
                "reference_results_reached_inbox": (
                    translation_job is not None
                    and inspection_job is not None
                    and reference_results_by_job
                    == {
                        translation_request_id: {
                            "job_id": translation_request_id,
                            "capability_id": TRANSLATE_CAPABILITY_ID,
                            "capability_version": translation["capability"]["version"],
                            "status": "completed",
                            "updated_at": translation_job.updated_at,
                            "protocol_version": connect.GENERIC_PROTOCOL_VERSION,
                            "provider": {
                                "app_id": REFERENCE_APP_ID,
                                "version": translation["provider"]["version"],
                                "instance_id": reference_instance_id,
                            },
                            "parameters": {"target-language": "Spanish"},
                            "outputs": [translation_output],
                        },
                        inspection_request_id: {
                            "job_id": inspection_request_id,
                            "capability_id": INSPECT_CAPABILITY_ID,
                            "capability_version": inspection["capability"]["version"],
                            "status": "completed",
                            "updated_at": inspection_job.updated_at,
                            "protocol_version": connect.GENERIC_PROTOCOL_VERSION,
                            "provider": {
                                "app_id": REFERENCE_APP_ID,
                                "version": inspection["provider"]["version"],
                                "instance_id": reference_instance_id,
                            },
                            "parameters": {},
                            "outputs": [inspection_output],
                        },
                    }
                ),
                "reference_removal_preserves_watcher": (
                    after_reference_stop["ok"]
                    and all(
                        item["provider"]["app_id"] != REFERENCE_APP_ID
                        for item in after_reference_items
                    )
                    and len(
                        [
                            item
                            for item in after_reference_items
                            if item["capability"]["id"] == SUMMARY_CAPABILITY_ID
                            and item["provider"]["app_id"] == selected["provider"]["app_id"]
                        ]
                    )
                    == 1
                    and reference_inbox_after_stop["ok"]
                ),
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
                "after_interruption_capabilities": len(after_interruption["items"]),
                "after_job_restart_capabilities": len(after_job_restart["items"]),
                "after_restart_capabilities": len(after_restart["items"]),
                "after_stop_capabilities": len(after_stop["items"]),
                "before_provider_capabilities": len(before["items"]),
                "capability_id": selected["capability"]["id"],
                "capability_version": selected["capability"]["version"],
                "during_provider_capabilities": len(during["items"]),
                "email_database_quick_check": quick_check,
                "email_database_schema_version": schema_version,
                "job_status": response["data"]["status"],
                "interrupted_initial_error_code": interrupted_error_code,
                "interrupted_provider_submissions": interrupted_provider_submissions,
                "interrupted_reconciled_error_code": reconciled_error_code,
                "model_id": model_name,
                "model_mode": model_mode,
                "persisted_interrupted_job_status": (
                    interrupted_row[2] if interrupted_row is not None else None
                ),
                "persisted_job_status": job_row[11],
                "persisted_protocol_version": job_row[1],
                "proof_input": "synthetic Gmail attachment bytes",
                "proof_passed": all(proof_checks.values()),
                "reference_capability_count": len(reference_capability_ids),
                "reference_inspection_submissions": inspection_submissions,
                "reference_translation_submissions": translation_submissions,
                "source_app_id": job_row[10],
                "stale_capability_error_code": stale_response.get("error", {}).get("code"),
                "summary_provider_choices": len(summary_choices),
                "summary_sha256": hashlib.sha256(summary_text.encode()).hexdigest(),
                **proof_checks,
            }
            print(json.dumps(result, separators=(",", ":"), sort_keys=True))
            require_proof_checks(proof_checks)
    finally:
        connect._client = original_connect_client_factory
        if provider is not None:
            stop_provider(provider)
        if restarted is not None:
            stop_provider(restarted)
        if recovered is not None:
            stop_provider(recovered)
        if reference_provider is not None:
            reference_provider.stop()
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(timeout=5)
        if original_runtime_dir is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = original_runtime_dir


if __name__ == "__main__":
    main()
