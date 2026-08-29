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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eom_email_watcher import connect, engine_api
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime


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
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        max_tokens = request.get("max_tokens")
        text = (
            "The structured report records Q1 revenue of $1,247,392.17 and a change of -4.75%."
            if max_tokens == 512
            else "Q1 revenue was $1,247,392.17, with a -4.75% change."
        )
        self._json({"choices": [{"message": {"content": text}}]})


class FixtureGmail:
    def __init__(self, content: bytes):
        self.content = content

    def attachment_bytes(
        self, message_id: str, part_id: str, attachment_id: str | None
    ) -> bytes:
        if (message_id, part_id, attachment_id) != (
            "fixture-message",
            "2",
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
        latest = connect.discover_summary_capability().public_result()
        if bool(latest["items"]) is available:
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
    wait_for_capability(True)
    return process


def stop_provider(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exercise Connect with a real provider process and synthetic Gmail bytes"
    )
    parser.add_argument("--provider-binary", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    args = parser.parse_args()
    provider_binary = args.provider_binary.resolve(strict=True)
    pdf_path = args.pdf.resolve(strict=True)
    pdf = pdf_path.read_bytes()

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModelHandler)
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()
    provider: subprocess.Popen[bytes] | None = None
    restarted: subprocess.Popen[bytes] | None = None
    original_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="connect-proof-") as temporary:
            root = Path(temporary)
            runtime_dir = root / "runtime"
            data_dir = root / "data"
            email_dir = root / "email"
            for directory in (runtime_dir, data_dir, email_dir):
                directory.mkdir(mode=0o700)
            os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
            environment = os.environ.copy()
            environment.update(
                {
                    "XDG_DATA_HOME": str(data_dir),
                    "DOC_SUM_MODEL_BASE_URL": (
                        f"http://127.0.0.1:{model_server.server_port}/v1/"
                    ),
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
                        "2",
                        "fixture-attachment",
                        pdf_path.name,
                        "application/pdf",
                        len(pdf),
                        0,
                    ),
                ),
            )
            original_from_token = engine_api.GmailGateway.__dict__["from_token"]
            engine_api.GmailGateway.from_token = staticmethod(
                lambda *_args: FixtureGmail(pdf)
            )
            try:
                response = engine_api._response(
                    request(
                        config_path,
                        "connect.attachment.summarize",
                        {"message_id": "fixture-message", "part_id": "2"},
                    )
                )
            finally:
                engine_api.GmailGateway.from_token = original_from_token
            if not response["ok"]:
                raise RuntimeError(f"Connect proof job failed: {response}")

            stop_provider(provider)
            provider = None
            after_stop = wait_for_capability(False)
            restarted = start_provider(provider_binary, environment)
            after_restart = wait_for_capability(True)

            with sqlite3.connect(runtime.config.database_file) as database:
                quick_check = database.execute("PRAGMA quick_check").fetchone()[0]
                schema_version = database.execute("PRAGMA user_version").fetchone()[0]
                job_row = database.execute(
                    """SELECT status, summary_text, input_sha256
                    FROM connect_attachment_jobs ORDER BY created_at DESC LIMIT 1"""
                ).fetchone()
            if job_row is None:
                raise RuntimeError("Connect proof result was not durable")
            summary_text = str(response["data"]["summary"]["text"])
            print(
                json.dumps(
                    {
                        "after_restart_capabilities": len(after_restart["items"]),
                        "after_stop_capabilities": len(after_stop["items"]),
                        "before_provider_capabilities": len(before["items"]),
                        "during_provider_capabilities": len(during["items"]),
                        "email_database_quick_check": quick_check,
                        "email_database_schema_version": schema_version,
                        "input_sha256_matches": job_row[2] == hashlib.sha256(pdf).hexdigest(),
                        "job_status": response["data"]["status"],
                        "persisted_job_status": job_row[0],
                        "persisted_summary_matches": job_row[1] == summary_text,
                        "proof_input": "synthetic Gmail attachment bytes",
                        "summary_sha256": hashlib.sha256(summary_text.encode()).hexdigest(),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
    finally:
        if provider is not None:
            stop_provider(provider)
        if restarted is not None:
            stop_provider(restarted)
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(timeout=5)
        if original_runtime_dir is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = original_runtime_dir


if __name__ == "__main__":
    main()
