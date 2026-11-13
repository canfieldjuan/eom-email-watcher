"""Local stand-ins for tests/test_scheduled_confinement_reachability.py (#363).

`FakeImap` is an implicit-TLS IMAP server that answers only the read-only commands an
IMAP `check` issues, for single-part text/plain messages.  `ModelHandler` answers the
email-analysis completion.  Both run in the test process and are reached over loopback.
"""

from __future__ import annotations

import imaplib
import json
import re
import socketserver
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

SUMMARY = "Reachability fixture analysed locally."
UID_VALIDITY = 7
_ITEM = re.compile(r"BODY(?:\.PEEK)?\[([^\]]*)\](?:<0\.(\d+)>)?|[A-Z0-9.]+", re.IGNORECASE)


def localhost_certificate(directory: Path) -> tuple[Path, Path]:
    """A self-signed `localhost` certificate, its own CA, and its private key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "imap-ca.pem", directory / "imap-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _members(selector: str, highest: int) -> set[int]:
    members: set[int] = set()
    for part in selector.split(","):
        low_text, _, high_text = part.partition(":")
        low = highest if low_text == "*" else int(low_text)
        high = low if not high_text else highest if high_text == "*" else int(high_text)
        members.update(range(min(low, high), max(low, high) + 1))
    return members


def _section(raw: bytes, section: str) -> bytes:
    header, _, body = raw.partition(b"\r\n\r\n")
    if section.upper().startswith("HEADER.FIELDS"):
        fields = set(section[section.index("(") + 1 : section.index(")")].upper().split())
        lines = [
            line for line in header.split(b"\r\n")
            if line.split(b":", 1)[0].decode("ascii").upper() in fields
        ]
        return b"".join(line + b"\r\n" for line in lines) + b"\r\n"
    if section == "1":  # a single-part message's only part is its body
        return body
    raise ValueError(f"unsupported section {section!r}")


class FakeImap(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, cert: Path, key: Path):
        super().__init__(("127.0.0.1", 0), _ImapSession)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.lock = threading.Lock()
        self.messages: list[tuple[int, datetime, bytes]] = []
        self.commands: list[str] = []
        self.unsupported: list[str] = []

    def get_request(self):
        connection, address = super().get_request()
        return self.context.wrap_socket(
            connection, server_side=True, do_handshake_on_connect=False
        ), address

    def append(self, raw: bytes) -> None:
        with self.lock:
            uid = self.messages[-1][0] + 1 if self.messages else 1
            self.messages.append((uid, datetime.now(UTC), raw))

    def answer(self, tag: str, line: str) -> bytes:
        command, _, arguments = line.partition(" ")
        command = command.upper()
        if command == "UID":
            subcommand, _, arguments = arguments.partition(" ")
            command = f"UID {subcommand.upper()}"
        with self.lock:
            self.commands.append(command)  # never the arguments: LOGIN carries a password
            messages = list(self.messages)
        uid_next = messages[-1][0] + 1 if messages else 1
        try:
            if command == "CAPABILITY":
                untagged = b"* CAPABILITY IMAP4rev1\r\n"
            elif command in {"LOGIN", "NOOP"}:
                untagged = b""
            elif command == "EXAMINE" and arguments.strip('"').upper() == "INBOX":
                untagged = (
                    f"* {len(messages)} EXISTS\r\n* 0 RECENT\r\n* FLAGS (\\Seen)\r\n"
                    f"* OK [UIDVALIDITY {UID_VALIDITY}] UIDs valid\r\n"
                    f"* OK [UIDNEXT {uid_next}] Predicted next UID\r\n"
                ).encode()
            elif command in {"FETCH", "UID FETCH"}:
                untagged = self._fetch(messages, arguments, by_uid=command == "UID FETCH")
            elif command == "LOGOUT":
                untagged = b"* BYE fake IMAP closing\r\n"
            else:
                raise ValueError("unsupported command")
        except (ValueError, IndexError):
            with self.lock:
                self.unsupported.append(command)
            return f"{tag} BAD {command} is not supported here\r\n".encode()
        return untagged + f"{tag} OK {command} completed\r\n".encode()

    @staticmethod
    def _fetch(messages, arguments: str, *, by_uid: bool) -> bytes:
        selector, _, items = arguments.partition(" ")
        highest = (messages[-1][0] if by_uid else len(messages)) if messages else 0
        wanted = _members(selector, highest)
        matches = list(_ITEM.finditer(items))
        names = [match.group(0).upper() for match in matches]
        output = b""
        for sequence, (uid, received, raw) in enumerate(messages, start=1):
            if (uid if by_uid else sequence) not in wanted:
                continue
            # A UID FETCH always reports the UID (RFC 3501 6.4.8).
            parts = [f"UID {uid}".encode()] if by_uid and "UID" not in names else []
            for match, item in zip(matches, names, strict=True):
                if match.group(1) is not None:
                    data = _section(raw, match.group(1))
                    limit = match.group(2)
                    data = data[: int(limit)] if limit else data
                    name = f"BODY[{match.group(1)}]" + ("<0>" if limit else "")
                    parts.append(f"{name} {{{len(data)}}}\r\n".encode() + data)
                elif item == "UID":
                    parts.append(f"UID {uid}".encode())
                elif item == "INTERNALDATE":
                    parts.append(f"INTERNALDATE {imaplib.Time2Internaldate(received)}".encode())
                elif item == "BODYSTRUCTURE":
                    body = raw.partition(b"\r\n\r\n")[2]
                    size, lines = len(body), body.count(b"\r\n")
                    parts.append(
                        f'BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" '
                        f"{size} {lines})".encode()
                    )
                else:
                    raise ValueError(f"unsupported fetch item {item}")
            output += f"* {sequence} FETCH (".encode() + b" ".join(parts) + b")\r\n"
        return output


class _ImapSession(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self) -> None:
        try:
            self.request.do_handshake()
            self.wfile.write(b"* OK fake IMAP ready\r\n")
            while line := self.rfile.readline(65536):
                tag, _, rest = line.decode("ascii").rstrip("\r\n").partition(" ")
                self.wfile.write(self.server.answer(tag, rest))
                if rest.upper() == "LOGOUT":
                    return
        except (OSError, ssl.SSLError):
            return


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _respond(self, status: int, value: object) -> None:
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._respond(200, {"data": [{"id": "local-model"}]})

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        schema = request["response_format"]["json_schema"]["name"]
        self.server.requests.append((self.path, schema))
        if self.path != "/v1/chat/completions" or schema != "email_analysis":
            self._respond(404, {"error": "unexpected request"})
            return
        analysis = {
            "category": "informational",
            "priority": "normal",
            "summary": SUMMARY,
            "action_required": False,
            "suggested_action": None,
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        }
        self._respond(200, {"choices": [{"message": {"content": json.dumps(analysis)}}]})


def model_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    server.daemon_threads = True
    server.requests = []
    return server


@contextmanager
def serving[Server: socketserver.BaseServer](server: Server) -> Iterator[Server]:
    """Serve on a daemon thread; always shut down and close, even on failure."""
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                              daemon=True)
    try:
        thread.start()
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
