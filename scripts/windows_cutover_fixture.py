"""Public native cutover inputs; no product hooks or entitlement bypasses.

GreenMail is the same server used by the existing IMAP integration lane, run
as a checksum-pinned standalone Java artifact on Windows rather than Docker.
Model and Connect responses are deterministic protocol peers, not patched clients.
"""

from __future__ import annotations

import base64
import hashlib
import imaplib
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from connect_reference_provider import ReferenceProvider, _Handler, _now, _Server
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

GREENMAIL_URL = (
    "https://repo.maven.apache.org/maven2/com/icegreen/greenmail-standalone/2.1.3/"
    "greenmail-standalone-2.1.3.jar"
)
GREENMAIL_SHA256 = "9457fdaf45ded6c87bf84a321a5ced4b4b5e72b1d03686b778df381378afc4c8"
OWNER = "owner@example.test"
VENDOR = "vendor@example.test"
PASSWORD = "public-fixture-password"
QUALIFICATION_AUTHORITY_ID = "cutover-test-key"
PDF = b"%PDF-1.4\nPublic native cutover fixture.\n%%EOF\n"


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        from connect_automate.connect_windows import atomic_replace_bytes

        atomic_replace_bytes(path, data, max(1, len(data)), allow_empty=True)
    else:
        path.write_bytes(data)
        path.chmod(0o600)


def fixture_authority(output: Path) -> tuple[Path, Path]:
    """Only qualification builds consume this key; never a release authority."""
    key = Ed25519PrivateKey.generate()

    def encoded(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    keyring = output / "fixture-keyring.json"
    write_private(
        keyring,
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": QUALIFICATION_AUTHORITY_ID,
                        "algorithm": "Ed25519",
                        "public_key_base64url": encoded(key.public_key().public_bytes_raw()),
                    }
                ]
            }
        ).encode(),
    )
    now = datetime.now(UTC)

    def stamp(value):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = json.dumps(
        {
            "format_version": 1,
            "entitlement_id": "11111111-1111-4111-8111-111111111111",
            "subject": "public-cutover-qualification",
            "features": ["connect.capability_exchange", "connect.automations"],
            "issued_at": stamp(now - timedelta(days=1)),
            "not_before": stamp(now - timedelta(days=1)),
            "expires_at": stamp(now + timedelta(days=1)),
        },
        separators=(",", ":"),
    ).encode()
    licence = output / "fixture-entitlement.json"
    write_private(
        licence,
        json.dumps(
            {
                "format_version": 1,
                "key_id": QUALIFICATION_AUTHORITY_ID,
                "payload_base64url": encoded(payload),
                "signature_base64url": encoded(key.sign(payload)),
            }
        ).encode(),
    )
    return keyring, licence


class ModelHandler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def respond(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        assert self.path == "/v1/models", self.path
        self.respond({"data": [{"id": "cutover-fixture"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/chat/completions", self.path
        assert body["response_format"]["json_schema"]["name"] == "email_analysis", body
        type(self).calls += 1
        self.respond(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "category": "informational",
                                    "priority": "normal",
                                    "summary": "Public cutover fixture analysed locally.",
                                    "action_required": False,
                                    "suggested_action": None,
                                    "deadline_text": None,
                                    "deadline_iso": None,
                                    "confidence": 0.9,
                                }
                            )
                        }
                    }
                ]
            }
        )


class MailFixture:
    def __init__(self, output: Path):
        self.output = output
        self.process = None
        self.log = None
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        self.model.daemon_threads = True
        self.model_thread = threading.Thread(target=self.model.serve_forever, daemon=True)
        self.model_thread.start()

    def __enter__(self):
        try:
            jar = self.output / "greenmail.jar"
            if not jar.exists():
                write_private(jar, urllib.request.urlopen(GREENMAIL_URL, timeout=30).read())
            assert hashlib.sha256(jar.read_bytes()).hexdigest() == GREENMAIL_SHA256
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.now(UTC)
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
                .sign(key, hashes.SHA256())
            )
            self.ca = self.output / "greenmail-ca.pem"
            write_private(self.ca, cert.public_bytes(serialization.Encoding.PEM))
            keystore = self.output / "greenmail.p12"
            write_private(
                keystore,
                pkcs12.serialize_key_and_certificates(
                    b"greenmail",
                    key,
                    cert,
                    None,
                    serialization.BestAvailableEncryption(b"changeit"),
                ),
            )
            # Fixed test port, isolated runner. Refuse to reuse an existing peer.
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 3993))
            self.log = (self.output / "greenmail.log").open("w", encoding="utf-8")
            (self.output / "greenmail.log").chmod(0o600)
            version = subprocess.run(
                ["java", "-version"], capture_output=True, text=True, check=True
            )
            write_private(
                self.output / "java-version.txt", (version.stdout + version.stderr).encode()
            )
            self.process = subprocess.Popen(
                [
                    "java",
                    "-Dgreenmail.setup.test.imaps",
                    # Allow TLS startup within the readiness budget below.
                    "-Dgreenmail.startup.timeout=25000",
                    "-Dgreenmail.hostname=127.0.0.1",
                    f"-Dgreenmail.tls.keystore.file={keystore}",
                    "-Dgreenmail.tls.keystore.password=changeit",
                    "-Dgreenmail.users=owner:public-fixture-password@example.test",
                    "-Dgreenmail.users.login=email",
                    "-jar",
                    str(jar),
                ],
                stdout=self.log,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 30
            while True:
                for line in (
                    (self.output / "greenmail.log")
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                ):
                    if "java.lang.IllegalStateException: Could not start mail server " in line:
                        raise RuntimeError(f"GreenMail startup failed: {line.strip()}")
                try:
                    with self.connection() as client:
                        assert client.create("Sent")[0] == "OK"
                    break
                except (OSError, imaplib.IMAP4.error) as exc:
                    if self.process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("GreenMail TLS/login did not become ready") from exc
                    time.sleep(0.1)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def connection(self):
        client = imaplib.IMAP4_SSL(
            "localhost", 3993, ssl_context=ssl.create_default_context(cafile=str(self.ca))
        )
        assert client.login(OWNER, PASSWORD)[0] == "OK"
        return client

    def append(self, name: str, *, old=False, sent=False, reply_to=None, pdf=False):
        stamp = datetime.now(UTC) - timedelta(days=60 if old else 0)
        message = EmailMessage()
        message["From"] = OWNER if sent else VENDOR
        message["To"] = VENDOR if sent else OWNER
        message["Subject"] = name
        message["Date"] = format_datetime(stamp)
        message["Message-ID"] = f"<{name}@example.test>"
        if reply_to:
            message["References"] = f"<{reply_to}@example.test>"
            message["In-Reply-To"] = f"<{reply_to}@example.test>"
        message.set_content(f"{name}: public promised price is 100.\n> earlier public quote\n")
        if pdf:
            message.add_attachment(
                PDF, maintype="application", subtype="pdf", filename="public.pdf"
            )
        with self.connection() as client:
            assert (
                client.append(
                    "Sent" if sent else "INBOX",
                    None,
                    imaplib.Time2Internaldate(stamp),
                    message.as_bytes(),
                )[0]
                == "OK"
            )

    @property
    def connection_payload(self):
        return {
            "email_address": OWNER,
            "host": "localhost",
            "port": 3993,
            "security": "tls",
            "username": OWNER,
            "password": PASSWORD,
            "ca_file": str(self.ca),
            "sent_folder": "Sent",
        }

    def __exit__(self, *args):
        if self.process is not None:
            self.process.terminate()
            self.process.wait(timeout=10)
        if self.log is not None:
            self.log.close()
        self.model.shutdown()
        self.model.server_close()
        self.model_thread.join(5)


@contextmanager
def reference_provider(private: Path):
    """Reuse the proven provider protocol with native private Windows storage."""
    posted, release = threading.Event(), threading.Event()

    class PausedHandler(_Handler):
        def _json(self, status, value):
            # Store completed real job first; pause the authoritative POST reply.
            if self.command == "POST" and status == 200:
                posted.set()
                if not release.wait(150):
                    raise TimeoutError("Native handoff fixture was not released")
            super()._json(status, value)

    server = _Server()
    server.RequestHandlerClass = PausedHandler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    if os.name == "nt":
        from connect_automate.connect_windows import ensure_private_directory

        providers = ensure_private_directory(
            private / "LocalConnect/runtime/v2/providers", root=private
        )
    else:
        providers = private / "local-connect/v2/providers"
        providers.mkdir(parents=True, mode=0o700)
    path = providers / f"{server.instance_id}.json"
    write_private(
        path,
        json.dumps(
            {
                "protocol_version": 2,
                "instance_id": server.instance_id,
                "app_id": "connect-reference-provider",
                "pid": os.getpid(),
                "started_at": _now(),
                "transport": {"kind": "http-loopback-v2", "base_url": server.base_url},
                "auth": {"scheme": "bearer", "token": server.token},
            }
        ).encode(),
    )
    provider = ReferenceProvider(server, thread, path)
    thread.start()
    try:
        yield provider, posted, release
    finally:
        release.set()
        provider.stop()
