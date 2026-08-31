from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

REFERENCE_APP_ID = "connect-reference-provider"
SUMMARY_CAPABILITY_ID = "document.summarize"
TRANSLATE_CAPABILITY_ID = "document.translate"
INSPECT_CAPABILITY_ID = "document.inspect"
INSPECT_OUTPUT_MEDIA_TYPE = "application/vnd.local-connect.inspection+json"
INSPECT_PAYLOAD = b"opaque-reference-output-must-not-reach-the-dom"
MAX_INPUT_BYTES = 50 * 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _capability(
    capability_id: str,
    label: str,
    *,
    parameters: list[dict[str, object]] | None = None,
    output_media_type: str = "text/plain",
) -> dict[str, object]:
    return {
        "id": capability_id,
        "version": "1.0",
        "action": {
            "label": label,
            "description": f"{label} this PDF with the deterministic reference provider.",
        },
        "accepts": [{"media_type": "application/pdf", "max_bytes": MAX_INPUT_BYTES}],
        "produces": [output_media_type],
        "parameters": parameters or [],
        "effects": {"external": False, "confirmation_required": False},
    }


CAPABILITIES = (
    _capability(SUMMARY_CAPABILITY_ID, "Reference summary"),
    _capability(
        TRANSLATE_CAPABILITY_ID,
        "Reference translation",
        parameters=[
            {
                "name": "target-language",
                "value_type": "string",
                "required": True,
                "label": "Target language",
                "description": "Language to produce.",
            }
        ],
    ),
    _capability(
        INSPECT_CAPABILITY_ID,
        "Reference inspection",
        output_media_type=INSPECT_OUTPUT_MEDIA_TYPE,
    ),
)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.instance_id = str(uuid4())
        self.token = secrets.token_urlsafe(32)
        self.jobs: dict[str, tuple[str, dict[str, object], dict[str, object]]] = {}
        self.submissions: dict[str, int] = {}
        self.lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"

    def manifest(self) -> dict[str, object]:
        return {
            "protocol_version": 2,
            "instance_id": self.instance_id,
            "app": {
                "id": REFERENCE_APP_ID,
                "name": "Connect Reference Provider",
                "version": "0.1.0",
            },
            "capabilities": list(CAPABILITIES),
        }


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: int, value: dict[str, object]) -> None:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(payload)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(
            status,
            {
                "protocol_version": 2,
                "error": {"code": code, "message": message, "retryable": False},
            },
        )

    def _authorized(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.server.token}"
        )

    def do_GET(self) -> None:
        if not self._authorized():
            self._error(401, "UNAUTHORIZED", "Reference provider authorization failed.")
        elif self.path == "/v2/manifest":
            self._json(200, self.server.manifest())
        elif self.path.startswith("/v2/jobs/"):
            with self.server.lock:
                stored = self.server.jobs.get(self.path.removeprefix("/v2/jobs/"))
            if stored is None:
                self._error(404, "JOB_NOT_FOUND", "Reference provider job was not found.")
            else:
                self._json(200, stored[1])
        else:
            self._error(404, "NOT_FOUND", "Reference provider route was not found.")

    def do_POST(self) -> None:
        if not self._authorized():
            self._error(401, "UNAUTHORIZED", "Reference provider authorization failed.")
            return
        if self.path != "/v2/jobs":
            self._error(404, "NOT_FOUND", "Reference provider route was not found.")
            return
        try:
            request, artifact = self._request_parts()
            job_id, signature, status = self._completed_job(request, artifact)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            self._error(400, "INVALID_REQUEST", str(error))
            return
        with self.server.lock:
            self.server.submissions[job_id] = self.server.submissions.get(job_id, 0) + 1
            stored = self.server.jobs.get(job_id)
            if stored is not None and stored[0] != signature:
                self._error(409, "JOB_CONFLICT", "Job identity was reused for other input.")
                return
            if stored is None:
                self.server.jobs[job_id] = (signature, status, request)
            else:
                status = stored[1]
        self._json(200, status)

    def _request_parts(self) -> tuple[dict[str, object], bytes]:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_INPUT_BYTES + 256 * 1024:
            raise ValueError("Request body size is invalid.")
        content_type = self.headers.get("Content-Type", "")
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii")
        message = BytesParser(policy=default).parsebytes(header + self.rfile.read(length))
        if not message.is_multipart():
            raise ValueError("Request must be multipart/form-data.")
        parts: dict[str, bytes] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if name not in {"request", "artifact"} or not isinstance(payload, bytes):
                raise ValueError("Multipart request contains an invalid part.")
            if name in parts:
                raise ValueError("Multipart request contains a duplicate part.")
            parts[name] = payload
        if set(parts) != {"request", "artifact"}:
            raise ValueError("Multipart request must contain request and artifact parts.")
        request = json.loads(parts["request"])
        if not isinstance(request, dict):
            raise ValueError("Job request must be an object.")
        return request, parts["artifact"]

    def _completed_job(
        self, request: dict[str, object], artifact: bytes
    ) -> tuple[str, str, dict[str, object]]:
        if set(request) != {"protocol_version", "job_id", "capability", "inputs", "parameters"}:
            raise ValueError("Job request shape is invalid.")
        job_id = request["job_id"]
        capability = request["capability"]
        inputs = request["inputs"]
        if (
            request["protocol_version"] != 2
            or not isinstance(job_id, str)
            or not _uuid4(job_id)
            or not isinstance(capability, dict)
            or capability.get("id")
            not in {
                SUMMARY_CAPABILITY_ID,
                TRANSLATE_CAPABILITY_ID,
                INSPECT_CAPABILITY_ID,
            }
            or capability.get("version") != "1.0"
            or not isinstance(inputs, list)
            or len(inputs) != 1
            or not isinstance(inputs[0], dict)
        ):
            raise ValueError("Job selection is invalid.")
        input_artifact = inputs[0]
        if (
            set(input_artifact)
            != {
                "artifact_id",
                "media_type",
                "byte_size",
                "sha256",
                "display_name",
                "source_app_id",
            }
            or input_artifact.get("media_type") != "application/pdf"
            or input_artifact.get("byte_size") != len(artifact)
            or input_artifact.get("sha256") != hashlib.sha256(artifact).hexdigest()
        ):
            raise ValueError("Input artifact integrity is invalid.")
        parameters = request["parameters"]
        capability_id = capability["id"]
        if capability_id == TRANSLATE_CAPABILITY_ID:
            if not (
                isinstance(parameters, dict)
                and set(parameters) == {"target-language"}
                and isinstance(parameters["target-language"], str)
                and bool(parameters["target-language"].strip())
            ):
                raise ValueError("Capability parameters are invalid.")
            output = f"Reference translation target: {parameters['target-language']}.\n".encode()
            output_media_type = "text/plain"
            display_name = "reference-translation.txt"
        elif capability_id == SUMMARY_CAPABILITY_ID:
            if parameters != {}:
                raise ValueError("Capability parameters are invalid.")
            output = b"Reference summary completed locally.\n"
            output_media_type = "text/plain"
            display_name = "reference-summary.txt"
        else:
            if parameters != {}:
                raise ValueError("Capability parameters are invalid.")
            output = INSPECT_PAYLOAD
            output_media_type = INSPECT_OUTPUT_MEDIA_TYPE
            display_name = "../../reference-inspection.json"
        output_artifact = {
            "artifact_id": str(uuid4()),
            "media_type": output_media_type,
            "display_name": display_name,
            "byte_size": len(output),
            "sha256": hashlib.sha256(output).hexdigest(),
            "payload_base64": base64.b64encode(output).decode("ascii"),
        }
        timestamp = _now()
        status = {
            "protocol_version": 2,
            "job_id": job_id,
            "capability": {"id": capability_id, "version": "1.0"},
            "provider": {"app_id": REFERENCE_APP_ID, "instance_id": self.server.instance_id},
            "status": "completed",
            "created_at": timestamp,
            "updated_at": timestamp,
            "input_artifacts": [
                {
                    key: input_artifact[key]
                    for key in ("artifact_id", "media_type", "byte_size", "sha256")
                }
            ],
            "result": {"outputs": [output_artifact]},
        }
        signature = hashlib.sha256(
            json.dumps(request, separators=(",", ":"), sort_keys=True).encode() + b"\0" + artifact
        ).hexdigest()
        return job_id, signature, status


def _uuid4(value: str) -> bool:
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _write_registration(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        with temporary.open("xb") as stream:
            temporary.chmod(0o600)
            stream.write(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass
class ReferenceProvider:
    server: _Server
    thread: threading.Thread
    registration_path: Path

    @classmethod
    def start(cls, runtime_dir: Path) -> ReferenceProvider:
        providers = runtime_dir / "local-connect" / "v2" / "providers"
        for directory in (runtime_dir, runtime_dir / "local-connect", providers.parent, providers):
            _private_directory(directory)
        server = _Server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        registration_path = providers / f"{server.instance_id}.json"
        try:
            _write_registration(
                registration_path,
                {
                    "protocol_version": 2,
                    "instance_id": server.instance_id,
                    "app_id": REFERENCE_APP_ID,
                    "pid": os.getpid(),
                    "started_at": _now(),
                    "transport": {"kind": "http-loopback-v2", "base_url": server.base_url},
                    "auth": {"scheme": "bearer", "token": server.token},
                },
            )
        except BaseException:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            raise
        return cls(server, thread, registration_path)

    @property
    def instance_id(self) -> str:
        return self.server.instance_id

    def requests(self) -> list[dict[str, object]]:
        with self.server.lock:
            return [stored[2] for stored in self.server.jobs.values()]

    def submission_count(self, job_id: str) -> int:
        with self.server.lock:
            return self.server.submissions.get(job_id, 0)

    def stop(self) -> None:
        self.registration_path.unlink(missing_ok=True)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
