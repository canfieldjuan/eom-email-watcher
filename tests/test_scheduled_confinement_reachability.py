"""#363 verification item 3: confinement reaches the shipped scheduled entry.

The real `eom-mail-watch check` runs as its own process against stand-in IMAP and model
servers (tests/confinement_reachability_fixture.py).  The analysis notification it
delivers starts `notify-send`, here a stub first on PATH that tries one write outside
the write roots and one inside.  The same harness also runs the entry with the single
confinement call neutralised: there the outside write succeeds, so the refusal seen in
the confined run is the confinement's, not the harness's.

Contract: plans/PR-Scheduled-Confinement-363.md.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

import pytest
from confinement_reachability_fixture import (
    SUMMARY,
    FakeImap,
    localhost_certificate,
    model_server,
    serving,
)
from test_scheduled_confinement import CONSOLE_SCRIPT, _env, _landlock_present  # noqa: F401

from eom_email_watcher import engine_api

# The imported `_landlock_present` is autouse: fail, never skip, when Landlock is missing.

pytestmark = pytest.mark.platform_contract(
    platforms=["linux"],
    owner="src/eom_email_watcher/confinement.py:134",
    reason="Landlock write confinement is Linux-only; other platforms are unchanged",
)

OWNER = "owner@example.test"
SENDER = "vendor@example.test"
SUBJECT = "Reachability check"

# Test-only: the same `cli.main` with `confinement.scheduled_run` replaced by a no-op,
# as if the one call in `main` were removed.  No shipped entry point can take this path.
WITHOUT_CONFINEMENT = (
    "import contextlib, sys\n"
    "from eom_email_watcher import cli, confinement\n"
    "confinement.scheduled_run = lambda config_path: contextlib.nullcontext()\n"
    "cli.main(sys.argv[1:])\n"
)


def _home(tmp_path: Path, model_port: int) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    config_root = home / ".config" / "eom-email-watcher"
    config_root.mkdir(parents=True, mode=0o700)
    state = home / ".local" / "state" / "eom-email-watcher"
    state.mkdir(parents=True, mode=0o700)
    config = config_root / "config.toml"
    config.write_text(
        'timezone = "America/Chicago"\n'
        'model_backend = "loopback"\n'
        f'model_base_url = "http://127.0.0.1:{model_port}/v1"\n'
        'model_name = "local-model"\n'
        "model_require_auth = false\n"
        "notifications_enabled = true\n"
        f'\n[[senders]]\nemail = "{SENDER}"\nname = "Vendor"\n',
        encoding="utf-8",
    )
    config.chmod(0o600)
    return home, config, state


def _connect_imap(monkeypatch, home: Path, config: Path, port: int, ca_file: Path) -> None:
    # Unconfined, in this process, while the inbox is empty: the baseline starts at UID 0.
    # DEFAULT_CONFIG here follows the real HOME, so the config path is always explicit.
    monkeypatch.setenv("HOME", str(home))
    for key in ("STATE_DIRECTORY", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)
    response = engine_api._response({
        "protocol": 1,
        "operation": "mail.accounts.connect",
        "config_path": str(config),
        "payload": {
            "provider": "imap",
            "connection": {
                "email_address": OWNER,
                "host": "localhost",
                "port": port,
                "security": "tls",
                "username": OWNER,
                "password": "fixture-password",
                "ca_file": str(ca_file),
            },
        },
    })
    assert response["ok"] is True, response
    assert response["data"]["baseline_initialized"] is True


def _watched_message() -> bytes:
    message = EmailMessage()
    message["From"] = f"Vendor <{SENDER}>"
    message["To"] = OWNER
    message["Subject"] = SUBJECT
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain="example.test")
    message.set_content("A watched message for the scheduled run.\n")
    return message.as_bytes(policy=policy.SMTP)


def _notify_send_stub(directory: Path, outside: Path, inside: Path, record: Path) -> None:
    # The record lives in the state root, not in $TMPDIR: the run's private temp
    # directory is removed when the run ends.
    directory.mkdir()
    stub = directory / "notify-send"
    stub.write_text(
        "#!/bin/sh\n"
        f"if error=$( (printf x > {shlex.quote(str(outside))}) 2>&1 ); then outside=ok;"
        ' else outside="refused: $error"; fi\n'
        f"if error=$( (printf x > {shlex.quote(str(inside))}) 2>&1 ); then inside=ok;"
        ' else inside="refused: $error"; fi\n'
        "{ printf 'outside=%s\\n' \"$outside\"; printf 'inside=%s\\n' \"$inside\";"
        " for arg in \"$@\"; do printf 'arg=%s\\n' \"$arg\"; done; }"
        f" > {shlex.quote(str(record))}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _record(path: Path) -> tuple[dict[str, str], list[str]]:
    fields, arguments = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key == "arg":
            arguments.append(value)
        else:
            fields[key] = value
    return fields, arguments


@pytest.mark.parametrize("entry", ["console_script", "without_confinement"])
def test_child_of_the_scheduled_entry_is_refused_outside_and_allowed_inside(
    tmp_path, monkeypatch, entry
):
    cert, key = localhost_certificate(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()  # exists, so only the boundary can refuse the write
    with serving(FakeImap(cert, key)) as imap, serving(model_server()) as model:
        home, config, state = _home(tmp_path, model.server_address[1])
        _connect_imap(monkeypatch, home, config, imap.server_address[1], cert)
        imap.append(_watched_message())  # one fresh message, so the notification fires
        stubs, record = tmp_path / "bin", state / "notify-send-record"
        _notify_send_stub(stubs, outside / "written", state / "notify-send-inside", record)
        env = _env(home)
        env["PATH"] = f"{stubs}{os.pathsep}{env.get('PATH', '')}"
        if entry == "console_script":
            assert CONSOLE_SCRIPT.exists(), CONSOLE_SCRIPT
            argv = [str(CONSOLE_SCRIPT), "check"]
        else:
            argv = [sys.executable, "-c", WITHOUT_CONFINEMENT, "check"]
        run = subprocess.run(argv, env=env, capture_output=True, text=True, check=False,
                             timeout=120)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert (result["discovered"], result["summarized"], result["fallback_notified"]) == (1, 1, 0)
    assert imap.unsupported == []
    assert model.requests == [("/v1/chat/completions", "email_analysis")]
    fields, arguments = _record(record)  # the stub ran: the body started a child
    assert arguments == [
        "--app-name=EOM Email Watcher", "--urgency=normal", f"Vendor: {SUBJECT}", SUMMARY,
    ]
    assert fields["inside"] == "ok"
    assert (state / "notify-send-inside").read_text() == "x"
    if entry == "console_script":
        assert fields["outside"].startswith("refused: "), fields
        assert "Permission denied" in fields["outside"], fields
        assert not (outside / "written").exists()
    else:  # the same harness sees the write once the confinement call is gone
        assert fields["outside"] == "ok", fields
        assert (outside / "written").read_text() == "x"
