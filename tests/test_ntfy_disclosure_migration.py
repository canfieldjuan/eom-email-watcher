from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import threading
from pathlib import Path

import pytest

import eom_email_watcher.config as config_module
import eom_email_watcher.notifications as notifications_module
from eom_email_watcher import cli, engine_api
from eom_email_watcher.config import ConfigError, load_config, ntfy_disclosure_status

TOPIC = "YOUR_NTFY_TOPIC_0123456789"


def _config_bytes(
    *,
    topic: str | None = TOPIC,
    acknowledgement: str = "",
    extra: str = "",
    newline: str = "\n",
) -> bytes:
    lines = [
        'model_base_url = "http://127.0.0.1:1234/v1"',
        'model_name = "local-model"',
        "model_require_auth = false",
    ]
    if topic is not None:
        lines.append(f'ntfy_topic = "{topic}"')
    if acknowledgement:
        lines.append(acknowledgement)
    if extra:
        lines.append(extra)
    return (newline.join(lines) + newline).encode()


def _write_bytes(path: Path, content: bytes) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(content)
    path.chmod(0o600)
    return content


def _write_legacy_config(path: Path, *, acknowledgement: str = "") -> bytes:
    return _write_bytes(path, _config_bytes(acknowledgement=acknowledgement))


def _request(path: Path, operation: str, payload: dict[str, object] | None = None):
    return {
        "protocol": 1,
        "operation": operation,
        "config_path": str(path),
        "payload": payload or {},
    }


def _transaction_paths(path: Path) -> tuple[Path, Path]:
    return (
        path.parent / f".{path.name}.ntfy-disclosure-transaction",
        path.parent / f".{path.name}.ntfy-disclosure-candidate",
    )


def _unsupported_path(path: Path) -> Path:
    return path.parent / f".{path.name}.ntfy-disclosure-unsupported"


def _disposition_path(path: Path) -> Path:
    return path.parent / f".{path.name}.ntfy-disclosure-disposition"


def _crash_at_transaction_stage(path: Path, revision: str, stage: str) -> None:
    child = os.fork()
    if child == 0:
        def crash_probe(observed: str) -> None:
            if observed == stage:
                os._exit(86)

        config_module._transaction_probe = crash_probe
        engine_api._response(
            _request(
                path,
                "config.ntfy_disclosure.acknowledge",
                {"expected_revision": revision},
            )
        )
        os._exit(87)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86


def _crash_immediately_after_exchange(path: Path, revision: str) -> None:
    child = os.fork()
    if child == 0:
        real_exchange = config_module._rename_exchange_at

        def exchange_then_exit(parent_fd: int, first: str, second: str) -> None:
            real_exchange(parent_fd, first, second)
            os._exit(86)

        config_module._rename_exchange_at = exchange_then_exit
        engine_api._response(
            _request(
                path,
                "config.ntfy_disclosure.acknowledge",
                {"expected_revision": revision},
            )
        )
        os._exit(87)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86


def test_legacy_valid_topic_without_ack_requires_explicit_migration(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)

    with pytest.raises(ConfigError, match="ntfy_content_disclosure_acknowledged"):
        load_config(path)

    response = engine_api._response(_request(path, "config.ntfy_disclosure.status"))
    assert response == {
        "data": {
            "state": "acknowledgement_required",
            "expected_revision": f"sha256:{hashlib.sha256(original).hexdigest()}",
        },
        "ok": True,
        "operation": "config.ntfy_disclosure.status",
        "protocol": 1,
    }
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("content", "state"),
    [
        (_config_bytes(), "acknowledgement_required"),
        (
            _config_bytes(acknowledgement="ntfy_content_disclosure_acknowledged = false"),
            "acknowledgement_required",
        ),
        (
            _config_bytes(acknowledgement="ntfy_content_disclosure_acknowledged = true"),
            "normal_admission",
        ),
        (_config_bytes(topic=None), "normal_admission"),
        (_config_bytes(topic=""), "manual_repair_required"),
        (_config_bytes(topic="short"), "manual_repair_required"),
        (_config_bytes(topic="a" * 65), "manual_repair_required"),
        (
            _config_bytes(topic=None, extra="ntfy_topic = 12345678901234567890"),
            "manual_repair_required",
        ),
        (
            _config_bytes(acknowledgement='ntfy_content_disclosure_acknowledged = "false"'),
            "manual_repair_required",
        ),
        (
            _config_bytes(acknowledgement="ntfy_content_disclosure_acknowledged = 0"),
            "manual_repair_required",
        ),
        (
            _config_bytes(acknowledgement="ntfy_content_disclosure_acknowledged = [false]"),
            "manual_repair_required",
        ),
        (_config_bytes(extra="poll_interval_minutes = 0"), "manual_repair_required"),
        (
            _config_bytes(extra='broken = "unterminated'),
            "manual_repair_required",
        ),
        (
            _config_bytes(extra=f'ntfy_topic = "{TOPIC}"'),
            "manual_repair_required",
        ),
    ],
)
def test_status_boundary_matrix(tmp_path: Path, content: bytes, state: str) -> None:
    path = tmp_path / "config.toml"
    original = _write_bytes(path, content)

    response = engine_api._response(_request(path, "config.ntfy_disclosure.status"))

    assert response["ok"] is True
    assert response["data"]["state"] == state
    assert set(response["data"]) == (
        {"state", "expected_revision"} if state == "acknowledgement_required" else {"state"}
    )
    assert path.read_bytes() == original


def test_status_reports_missing_without_creating_parent(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "config.toml"

    assert ntfy_disclosure_status(path).state == "missing"
    assert not path.parent.exists()


@pytest.mark.parametrize("failure", [PermissionError("denied"), IsADirectoryError("dir")])
def test_non_posix_status_classifies_path_read_errors_generically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    path = tmp_path / "sensitive-config-name.toml"
    monkeypatch.setattr(config_module.os, "name", "nt")
    monkeypatch.setattr(Path, "read_bytes", lambda _path: (_ for _ in ()).throw(failure))

    response = engine_api._response(
        _request(path, "config.ntfy_disclosure.status")
    )

    assert response["data"] == {"state": "manual_repair_required"}
    assert str(path) not in json.dumps(response)


def test_non_posix_admission_rejects_atomic_swap_during_same_fd_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_bytes(path, _config_bytes(topic=None))

    class NonPosixOsProxy:
        name = "nt"

        def __getattr__(self, attribute: str):
            return getattr(os, attribute)

    real_read = config_module._read_fd_bytes
    swapped = False

    def swap_after_read(file_fd: int) -> bytes:
        nonlocal swapped
        content = real_read(file_fd)
        if not swapped:
            replacement = tmp_path / "replacement.toml"
            _write_bytes(replacement, original)
            os.replace(replacement, path)
            swapped = True
        return content

    monkeypatch.setattr(config_module, "os", NonPosixOsProxy())
    monkeypatch.setattr(config_module, "_read_fd_bytes", swap_after_read)

    response = engine_api._response(
        _request(path, "config.admission.snapshot")
    )

    assert response["error"] == {
        "code": "configuration_error",
        "message": "Configuration admission snapshot is unavailable",
    }
    assert str(path) not in json.dumps(response)


def test_non_posix_runtime_load_rejects_atomic_swap_generically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sensitive-config-name.toml"
    original = _write_bytes(path, _config_bytes(topic=None))

    class NonPosixOsProxy:
        name = "nt"

        def __getattr__(self, attribute: str):
            return getattr(os, attribute)

    real_read = config_module._read_fd_bytes
    swapped = False

    def swap_after_read(file_fd: int) -> bytes:
        nonlocal swapped
        content = real_read(file_fd)
        if not swapped:
            replacement = tmp_path / "replacement.toml"
            _write_bytes(replacement, original)
            os.replace(replacement, path)
            swapped = True
        return content

    monkeypatch.setattr(config_module, "os", NonPosixOsProxy())
    monkeypatch.setattr(config_module, "_read_fd_bytes", swap_after_read)

    with pytest.raises(ConfigError) as raised:
        load_config(path)

    assert str(raised.value) == "Configuration is unavailable or requires manual repair"
    assert str(path) not in str(raised.value)
    assert TOPIC not in str(raised.value)


@pytest.mark.parametrize("unsafe", ["file_mode", "parent_mode", "hardlink", "fifo"])
def test_status_rejects_unsafe_filesystem_boundaries(tmp_path: Path, unsafe: str) -> None:
    parent = tmp_path / unsafe
    path = parent / "config.toml"
    parent.mkdir(mode=0o700)
    if unsafe == "fifo":
        os.mkfifo(path, 0o600)
    else:
        _write_bytes(path, _config_bytes())
    if unsafe == "file_mode":
        path.chmod(0o640)
    elif unsafe == "parent_mode":
        parent.chmod(0o750)
    elif unsafe == "hardlink":
        os.link(path, parent / "second-link.toml")

    assert ntfy_disclosure_status(path).state == "manual_repair_required"


def test_status_rejects_file_and_parent_symlinks_without_readthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_parent = tmp_path / "target"
    target = target_parent / "config.toml"
    _write_bytes(target, _config_bytes())
    file_link = tmp_path / "file-link.toml"
    file_link.symlink_to(target)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(target_parent, target_is_directory=True)
    reads = 0
    real_read = config_module._read_fd_bytes

    def counted_read(file_fd: int) -> bytes:
        nonlocal reads
        reads += 1
        return real_read(file_fd)

    monkeypatch.setattr(config_module, "_read_fd_bytes", counted_read)

    assert ntfy_disclosure_status(file_link).state == "manual_repair_required"
    assert ntfy_disclosure_status(parent_link / "config.toml").state == "manual_repair_required"
    assert reads == 0


def test_status_rejects_wrong_effective_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_bytes(path, _config_bytes())
    effective_user = os.geteuid()
    monkeypatch.setattr(config_module.os, "geteuid", lambda: effective_user + 1)

    assert ntfy_disclosure_status(path).state == "manual_repair_required"


def test_status_rejects_inode_swap_between_inspect_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_bytes(path, _config_bytes())
    real_open = config_module.os.open
    swapped = False

    def swapping_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            not swapped
            and name == "config.toml"
            and dir_fd is not None
            and not flags & getattr(os, "O_DIRECTORY", 0)
        ):
            replacement = tmp_path / "replacement.toml"
            _write_bytes(replacement, _config_bytes(topic=None))
            os.replace(replacement, path)
            swapped = True
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(config_module.os, "open", swapping_open)

    assert ntfy_disclosure_status(path).state == "manual_repair_required"


@pytest.mark.parametrize("mutation", ["mode", "hardlink"])
def test_status_rechecks_complete_safety_predicate_after_no_follow_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    path = tmp_path / "config.toml"
    _write_bytes(path, _config_bytes())
    real_open = config_module.os.open
    mutated = False

    def mutate_after_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal mutated
        opened = real_open(name, flags, mode, dir_fd=dir_fd)
        if (
            not mutated
            and name == "config.toml"
            and dir_fd is not None
            and not flags & getattr(os, "O_DIRECTORY", 0)
        ):
            if mutation == "mode":
                path.chmod(0o640)
            else:
                os.link(path, tmp_path / "second-link.toml")
            mutated = True
        return opened

    monkeypatch.setattr(config_module.os, "open", mutate_after_open)

    assert ntfy_disclosure_status(path).state == "manual_repair_required"


def test_false_ack_replaces_only_the_boolean_token(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    marker = b"ntfy_content_disclosure_acknowledged = false"
    original = _write_bytes(
        path,
        (
            b'# false and "ntfy_content_disclosure_acknowledged = false" are decoys\n'
            + _config_bytes(
                acknowledgement=marker.decode(),
                extra=(
                    'operator_note = "Unicode snowman ☃ and false"\n'
                    "[nested]\n"
                    "ntfy_content_disclosure_acknowledged = false\n"
                    '[[senders]]\nemail = "person@example.com"'
                ),
            ).rstrip(b"\n")
        ),
    )
    assignment = b"\n" + marker
    expected = original.replace(assignment, b"\n" + marker[:-5] + b"true", 1)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is True
    assert response["data"] == {"acknowledged": True}
    assert path.read_bytes() == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_config(path).ntfy_content_disclosure_acknowledged is True


def test_candidate_is_same_directory_regular_owner_private_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    real_exchange = config_module._rename_exchange_at
    observed: dict[str, object] = {}

    def inspect_exchange(parent_fd: int, source: str, destination: str) -> None:
        candidate_stat = os.stat(source, dir_fd=parent_fd, follow_symlinks=False)
        observed.update(
            {
                "destination": destination,
                "same_directory": True,
                "regular": stat.S_ISREG(candidate_stat.st_mode),
                "mode": stat.S_IMODE(candidate_stat.st_mode),
                "owner": candidate_stat.st_uid,
            }
        )
        real_exchange(parent_fd, source, destination)

    monkeypatch.setattr(config_module, "_rename_exchange_at", inspect_exchange)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is True
    assert observed == {
        "destination": "config.toml",
        "same_directory": True,
        "regular": True,
        "mode": 0o600,
        "owner": os.geteuid(),
    }


def test_absent_ack_inserts_one_root_assignment_and_preserves_crlf(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = _write_bytes(
        path,
        _config_bytes(
            newline="\r\n",
            extra=(
                'operator_note = "ntfy_content_disclosure_acknowledged = false"\r\n'
                "nested.ntfy_content_disclosure_acknowledged = false\r\n"
                "[table]\r\nntfy_content_disclosure_acknowledged = false"
            ),
        ),
    )
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is True
    assert path.read_bytes() == (b"ntfy_content_disclosure_acknowledged = true\r\n" + original)


def test_quoted_root_key_and_no_final_newline_preserve_exact_suffix(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = _write_bytes(
        path,
        _config_bytes().rstrip(b"\n") + b'\n"ntfy_content_disclosure_acknowledged" = false',
    )
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is True
    assert path.read_bytes() == original[:-5] + b"true"


def test_stale_and_repeated_acknowledgements_do_not_mutate(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    edited = original + b"# operator edit\n"
    _write_bytes(path, edited)

    stale = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )
    assert stale["ok"] is False
    assert stale["error"]["code"] == "conflict"
    assert path.read_bytes() == edited

    current_revision = ntfy_disclosure_status(path).expected_revision
    assert current_revision is not None
    success = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": current_revision},
        )
    )
    assert success["ok"] is True
    acknowledged = path.read_bytes()
    repeated = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": current_revision},
        )
    )
    assert repeated["ok"] is False
    assert repeated["error"]["code"] == "conflict"
    assert path.read_bytes() == acknowledged
    assert ntfy_disclosure_status(path).state == "normal_admission"


def test_two_concurrent_acknowledgements_replace_at_most_once(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    barrier = threading.Barrier(2)
    responses: list[dict[str, object]] = []

    def acknowledge() -> None:
        barrier.wait()
        responses.append(
            engine_api._response(
                _request(
                    path,
                    "config.ntfy_disclosure.acknowledge",
                    {"expected_revision": revision},
                )
            )
        )

    threads = [threading.Thread(target=acknowledge) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(response["ok"] is True for response in responses) == 1
    assert (
        sum(
            response.get("error", {}).get("code") == "conflict"  # type: ignore[union-attr]
            for response in responses
        )
        == 1
    )
    assert ntfy_disclosure_status(path).state == "normal_admission"


def test_manual_edit_during_precommit_wins_without_lost_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    edited = original + b"# edit outside lock\n"
    real_replace = config_module._durable_exchange_at

    def edit_then_replace(
        parent_fd,
        name,
        content,
        expected_stat,
        expected_content,
        *,
        parent_path,
        before_replace,
    ):
        _write_bytes(path, edited)
        return real_replace(
            parent_fd,
            name,
            content,
            expected_stat,
            expected_content,
            parent_path=parent_path,
            before_replace=before_replace,
        )

    monkeypatch.setattr(config_module, "_durable_exchange_at", edit_then_replace)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "conflict"
    assert path.read_bytes() == edited
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_manual_edit_after_precommit_check_is_atomically_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    edited = original + b"# exact precommit race\n"
    real_replace = config_module._durable_exchange_at

    def edit_after_check(
        parent_fd,
        name,
        content,
        expected_stat,
        expected_content,
        *,
        parent_path,
        before_replace,
    ):
        def check_then_edit() -> None:
            if before_replace is not None:
                before_replace()
            _write_bytes(path, edited)

        return real_replace(
            parent_fd,
            name,
            content,
            expected_stat,
            expected_content,
            parent_path=parent_path,
            before_replace=check_then_edit,
        )

    monkeypatch.setattr(config_module, "_durable_exchange_at", edit_after_check)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "conflict"
    assert path.read_bytes() == edited
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_atomic_replacement_after_final_precheck_is_restored_without_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    manual = _config_bytes(topic=None, extra="# atomic manual replacement")
    real_exchange = config_module._durable_exchange_at

    def replace_after_check(
        parent_fd,
        name,
        content,
        expected_stat,
        expected_content,
        *,
        parent_path,
        before_replace,
    ):
        def check_then_replace() -> None:
            before_replace()
            replacement = path.parent / "manual.toml"
            _write_bytes(replacement, manual)
            os.replace(replacement, path)

        return real_exchange(
            parent_fd,
            name,
            content,
            expected_stat,
            expected_content,
            parent_path=parent_path,
            before_replace=check_then_replace,
        )

    monkeypatch.setattr(config_module, "_durable_exchange_at", replace_after_check)
    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "conflict"
    assert path.read_bytes() == manual
    assert ntfy_disclosure_status(path).state == "normal_admission"
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()


@pytest.mark.parametrize("crash_point", ["after_exchange", "after_rollback"])
def test_restart_restores_atomic_replacement_across_mismatch_rollback(
    tmp_path: Path, crash_point: str
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    manual = _config_bytes(topic=None, extra=f"# crash {crash_point}")
    child = os.fork()
    if child == 0:
        real_exchange = config_module._rename_exchange_at
        exchange_calls = 0
        replaced = False

        def replace_before_exchange(stage: str) -> None:
            nonlocal replaced
            if stage == "before_exchange" and not replaced:
                replacement = path.parent / "manual.toml"
                _write_bytes(replacement, manual)
                os.replace(replacement, path)
                replaced = True

        def exchange_then_crash(parent_fd: int, first: str, second: str) -> None:
            nonlocal exchange_calls
            exchange_calls += 1
            real_exchange(parent_fd, first, second)
            if crash_point == "after_exchange" and exchange_calls == 1:
                os._exit(86)
            if crash_point == "after_rollback" and exchange_calls == 2:
                os._exit(86)

        config_module._transaction_probe = replace_before_exchange
        config_module._rename_exchange_at = exchange_then_crash
        engine_api._response(
            _request(
                path,
                "config.ntfy_disclosure.acknowledge",
                {"expected_revision": revision},
            )
        )
        os._exit(87)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86

    assert ntfy_disclosure_status(path).state == "normal_admission"
    assert path.read_bytes() == manual
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()


def test_systemd_cli_check_recovers_mismatch_before_consuming_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    runtime_paths = (
        f'database_file = "{tmp_path / "watcher.sqlite3"}"\n'
        f'gmail_credentials_file = "{tmp_path / "credentials.json"}"\n'
        f'microsoft_credentials_file = "{tmp_path / "microsoft.json"}"\n'
        f'gmail_token_file = "{tmp_path / "token.json"}"\n'
        f'gmail_send_token_file = "{tmp_path / "send-token.json"}"'
    )
    _write_bytes(path, _config_bytes(extra=runtime_paths))
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    manual = _config_bytes(
        topic=None,
        extra=f"{runtime_paths}\n# manual topic removal",
    )
    child = os.fork()
    if child == 0:
        real_exchange = config_module._rename_exchange_at
        replaced = False

        def replace_before_exchange(stage: str) -> None:
            nonlocal replaced
            if stage == "before_exchange" and not replaced:
                replacement = path.parent / "manual.toml"
                _write_bytes(replacement, manual)
                os.replace(replacement, path)
                replaced = True

        def exchange_then_crash(parent_fd: int, first: str, second: str) -> None:
            real_exchange(parent_fd, first, second)
            os._exit(86)

        config_module._transaction_probe = replace_before_exchange
        config_module._rename_exchange_at = exchange_then_crash
        engine_api._response(
            _request(
                path,
                "config.ntfy_disclosure.acknowledge",
                {"expected_revision": revision},
            )
        )
        os._exit(87)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86
    observed_topics: list[str | None] = []
    captured_requests: list[tuple[object, ...]] = []
    real_check = cli.run_watcher_check

    def observe_check(config, store, model, *, dry_run: bool):
        observed_topics.append(config.ntfy_topic)
        return real_check(config, store, model, dry_run=dry_run)

    monkeypatch.setattr(cli, "run_watcher_check", observe_check)
    monkeypatch.setattr(
        notifications_module,
        "_send_ntfy",
        lambda *args: captured_requests.append(args),
    )

    assert cli._check(path, dry_run=False) == 0

    assert observed_topics == [None]
    assert captured_requests == []
    assert path.read_bytes() == manual
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert not _disposition_path(path).exists()


def test_post_exchange_displaced_replacement_is_restored_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    manual = _config_bytes(topic=None, extra="# post exchange replacement")
    real_exchange = config_module._rename_exchange_at
    exchange_calls = 0

    def exchange_then_replace(parent_fd: int, first: str, second: str) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        real_exchange(parent_fd, first, second)
        if exchange_calls == 1:
            replacement = path.parent / "manual.toml"
            _write_bytes(replacement, manual)
            os.replace(replacement, _transaction_paths(path)[1])

    monkeypatch.setattr(config_module, "_rename_exchange_at", exchange_then_replace)
    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "conflict"
    assert path.read_bytes() == manual
    assert ntfy_disclosure_status(path).state == "normal_admission"
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()


def test_restart_restores_manual_replacement_after_disposition_is_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    manual = _config_bytes(topic=None, extra="# disposition crash")
    child = os.fork()
    if child == 0:
        replaced = False

        def replace_then_crash(stage: str) -> None:
            nonlocal replaced
            if stage == "before_exchange" and not replaced:
                replacement = path.parent / "manual.toml"
                _write_bytes(replacement, manual)
                os.replace(replacement, path)
                replaced = True
            if stage == "after_disposition_durable":
                os._exit(86)

        config_module._transaction_probe = replace_then_crash
        engine_api._response(
            _request(
                path,
                "config.ntfy_disclosure.acknowledge",
                {"expected_revision": revision},
            )
        )
        os._exit(87)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86
    assert _disposition_path(path).exists()

    snapshot = engine_api._response(_request(path, "config.admission.snapshot"))

    assert snapshot["ok"] is True
    assert snapshot["data"]["settings"]["polling_supported"] is True
    assert path.read_bytes() == manual
    assert ntfy_disclosure_status(path).state == "normal_admission"
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert not _disposition_path(path).exists()


def test_mismatch_rollback_response_loss_restores_manual_without_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    manual = _config_bytes(topic=None, extra="# rollback response loss")
    real_exchange = config_module._rename_exchange_at
    exchange_calls = 0
    replaced = False

    def replace_before_exchange(stage: str) -> None:
        nonlocal replaced
        if stage == "before_exchange" and not replaced:
            replacement = path.parent / "manual.toml"
            _write_bytes(replacement, manual)
            os.replace(replacement, path)
            replaced = True

    def lose_rollback_response(parent_fd: int, first: str, second: str) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        real_exchange(parent_fd, first, second)
        if exchange_calls == 2:
            raise OSError("injected rollback response loss")

    monkeypatch.setattr(config_module, "_transaction_probe", replace_before_exchange)
    monkeypatch.setattr(config_module, "_rename_exchange_at", lose_rollback_response)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["error"]["code"] == "conflict"
    assert path.read_bytes() == manual
    assert ntfy_disclosure_status(path).state == "normal_admission"
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert not _disposition_path(path).exists()


def test_tampered_mismatch_disposition_fails_closed_without_deleting_manual(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    manual = _config_bytes(topic=None, extra="# preserved manual")
    child = os.fork()
    if child == 0:
        replaced = False

        def replace_then_crash(stage: str) -> None:
            nonlocal replaced
            if stage == "before_exchange" and not replaced:
                replacement = path.parent / "manual.toml"
                _write_bytes(replacement, manual)
                os.replace(replacement, path)
                replaced = True
            if stage == "after_disposition_durable":
                os._exit(86)

        config_module._transaction_probe = replace_then_crash
        engine_api._response(
            _request(
                path,
                "config.ntfy_disclosure.acknowledge",
                {"expected_revision": revision},
            )
        )
        os._exit(87)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86
    marker, candidate = _transaction_paths(path)
    disposition = _disposition_path(path)
    disposition.write_bytes(b'{"tampered":true}\n')
    disposition.chmod(0o600)
    displaced = candidate.read_bytes()

    first = ntfy_disclosure_status(path)
    second = ntfy_disclosure_status(path)
    snapshot = engine_api._response(_request(path, "config.admission.snapshot"))

    assert first.state == "manual_repair_required"
    assert second.state == "manual_repair_required"
    assert snapshot["error"]["code"] == "configuration_error"
    assert marker.exists()
    assert candidate.read_bytes() == displaced == manual
    assert disposition.read_bytes() == b'{"tampered":true}\n'


def test_metadata_change_after_precommit_check_is_atomically_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    real_replace = config_module._durable_exchange_at

    def broaden_after_check(
        parent_fd,
        name,
        content,
        expected_stat,
        expected_content,
        *,
        parent_path,
        before_replace,
    ):
        def check_then_broaden() -> None:
            if before_replace is not None:
                before_replace()
            path.chmod(0o640)

        return real_replace(
            parent_fd,
            name,
            content,
            expected_stat,
            expected_content,
            parent_path=parent_path,
            before_replace=check_then_broaden,
        )

    monkeypatch.setattr(config_module, "_durable_exchange_at", broaden_after_check)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "conflict"
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_displaced_validation_failure_rolls_back_without_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    real_read = config_module._read_safe_file_at
    real_exchange = config_module._rename_exchange_at
    exchanged = False
    failed = False

    def observe_exchange(parent_fd: int, first: str, second: str) -> None:
        nonlocal exchanged
        real_exchange(parent_fd, first, second)
        exchanged = True

    def fail_displaced_read(parent_fd: int, name: str):
        nonlocal failed
        if exchanged and not failed and name.endswith(".ntfy-disclosure-candidate"):
            failed = True
            raise OSError("injected displaced validation failure")
        return real_read(parent_fd, name)

    monkeypatch.setattr(config_module, "_rename_exchange_at", observe_exchange)
    monkeypatch.setattr(config_module, "_read_safe_file_at", fail_displaced_read)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "conflict"
    assert path.read_bytes() == original
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_exchange_rollback_response_failure_retries_and_restores_manual_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    edited = original + b"# rollback failure canary\n"
    real_durable = config_module._durable_exchange_at
    real_exchange = config_module._rename_exchange_at
    exchange_calls = 0

    def edit_after_check(
        parent_fd,
        name,
        content,
        expected_stat,
        expected_content,
        *,
        parent_path,
        before_replace,
    ):
        def check_then_edit() -> None:
            if before_replace is not None:
                before_replace()
            _write_bytes(path, edited)

        return real_durable(
            parent_fd,
            name,
            content,
            expected_stat,
            expected_content,
            parent_path=parent_path,
            before_replace=check_then_edit,
        )

    def fail_rollback(parent_fd: int, first: str, second: str) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 2:
            raise OSError("injected rollback failure")
        real_exchange(parent_fd, first, second)

    monkeypatch.setattr(config_module, "_durable_exchange_at", edit_after_check)
    monkeypatch.setattr(config_module, "_rename_exchange_at", fail_rollback)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "conflict"
    assert path.read_bytes() == edited
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    rendered = repr(response) + caplog.text
    assert TOPIC not in rendered
    assert candidate.name not in rendered


def test_restart_recovers_crash_immediately_after_exchange(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    _crash_immediately_after_exchange(path, revision)

    assert load_config(path).ntfy_content_disclosure_acknowledged is True
    assert ntfy_disclosure_status(path).state == "normal_admission"
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_recovery_fails_closed_on_tampered_displaced_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    _crash_immediately_after_exchange(path, revision)
    marker, candidate = _transaction_paths(path)
    assert marker.exists()
    candidate.write_bytes(candidate.read_bytes() + b"# tampered\n")
    candidate.chmod(0o600)
    tampered = candidate.read_bytes()

    assert ntfy_disclosure_status(path).state == "acknowledgement_required"
    assert not marker.exists()
    assert not candidate.exists()
    assert path.read_bytes() == tampered


def test_recovery_fails_closed_on_tampered_transaction_marker(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    _crash_immediately_after_exchange(path, revision)
    marker, candidate = _transaction_paths(path)
    marker.write_bytes(b'{"version":1,"tampered":true}\n')
    marker.chmod(0o600)
    tampered = marker.read_bytes()
    displaced = candidate.read_bytes()

    assert ntfy_disclosure_status(path).state == "manual_repair_required"
    assert marker.read_bytes() == tampered
    assert candidate.read_bytes() == displaced
    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert str(raised.value) == "Configuration is unavailable or requires manual repair"
    assert str(path) not in str(raised.value)
    assert TOPIC not in str(raised.value)


def test_recovery_preserves_manual_concurrent_replacement(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    _crash_immediately_after_exchange(path, revision)
    marker, candidate = _transaction_paths(path)
    manual_content = _config_bytes(
        acknowledgement="ntfy_content_disclosure_acknowledged = true",
        extra="# manual concurrent replacement",
    )
    manual = path.parent / "manual.toml"
    _write_bytes(manual, manual_content)
    os.replace(manual, path)

    assert ntfy_disclosure_status(path).state == "normal_admission"
    assert path.read_bytes() == manual_content
    assert not marker.exists()
    assert not candidate.exists()


def test_recovery_cleans_displaced_original_behind_unsafe_manual_target(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    _crash_immediately_after_exchange(path, revision)
    marker, candidate = _transaction_paths(path)
    manual_content = _config_bytes(
        acknowledgement="ntfy_content_disclosure_acknowledged = true",
        extra="# unsafe manual replacement",
    )
    manual = path.parent / "manual.toml"
    _write_bytes(manual, manual_content)
    manual.chmod(0o640)
    os.replace(manual, path)

    assert ntfy_disclosure_status(path).state == "manual_repair_required"
    assert path.read_bytes() == manual_content
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not marker.exists()
    assert not candidate.exists()


def test_no_marker_equal_byte_candidate_is_preserved_as_manual_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    status, derived_candidate = config_module._classify_ntfy_disclosure(original, path)
    assert status.state == "acknowledgement_required"
    assert derived_candidate is not None
    marker, candidate = _transaction_paths(path)
    _write_bytes(candidate, derived_candidate)

    assert ntfy_disclosure_status(path).state == "manual_repair_required"
    assert not marker.exists()
    assert candidate.read_bytes() == derived_candidate
    assert path.read_bytes() == original


def test_marker_only_recovery_preserves_equal_byte_manual_candidate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    status, derived_candidate = config_module._classify_ntfy_disclosure(original, path)
    assert status.expected_revision is not None
    assert derived_candidate is not None
    _crash_at_transaction_stage(
        path,
        status.expected_revision,
        "after_marker_durable",
    )
    marker, candidate = _transaction_paths(path)
    assert marker.exists()
    _write_bytes(candidate, derived_candidate)

    assert ntfy_disclosure_status(path).state == "manual_repair_required"
    assert candidate.read_bytes() == derived_candidate
    assert path.read_bytes() == original


@pytest.mark.parametrize("crash_after_write", [1, 2, 3])
def test_crash_during_unnamed_candidate_short_write_leaves_no_named_secret(
    tmp_path: Path, crash_after_write: int
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    child = os.fork()
    if child == 0:
        real_write = config_module.os.write
        candidate_writes = 0

        def short_write_then_crash(fd: int, content) -> int:
            nonlocal candidate_writes
            fd_target = os.readlink(f"/proc/self/fd/{fd}")
            if (
                os.fstat(fd).st_nlink == 0
                or fd_target.endswith(".config.toml.ntfy-disclosure-candidate")
            ):
                written = real_write(fd, content[:17])
                candidate_writes += 1
                if candidate_writes == crash_after_write:
                    os._exit(86)
                return written
            return real_write(fd, content)

        config_module.os.write = short_write_then_crash
        engine_api._response(
            _request(
                path,
                "config.ntfy_disclosure.acknowledge",
                {"expected_revision": revision},
            )
        )
        os._exit(87)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86

    marker, candidate = _transaction_paths(path)
    assert not candidate.exists()
    assert path.read_bytes() == original
    assert ntfy_disclosure_status(path).state == "acknowledgement_required"
    assert not marker.exists()


def test_crash_after_candidate_inode_is_marked_but_before_link_recovers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    _crash_at_transaction_stage(
        path,
        revision,
        "after_candidate_marker_durable",
    )

    marker, candidate = _transaction_paths(path)
    assert marker.exists()
    assert not candidate.exists()
    assert ntfy_disclosure_status(path).state == "acknowledgement_required"
    assert path.read_bytes() == original
    assert not marker.exists()


def test_fixed_candidate_collision_at_link_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    _marker, candidate = _transaction_paths(path)
    manual = b"manual fixed-name collision"

    def create_collision(stage: str) -> None:
        if stage == "before_candidate_link":
            _write_bytes(candidate, manual)

    monkeypatch.setattr(config_module, "_transaction_probe", create_collision)
    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert path.read_bytes() == original
    assert candidate.read_bytes() == manual
    assert ntfy_disclosure_status(path).state == "manual_repair_required"


@pytest.mark.parametrize(
    ("primitive", "error_number"),
    [
        ("_open_unnamed_candidate_at", errno.EOPNOTSUPP),
        ("_link_unnamed_candidate_at", errno.ENOSYS),
    ],
)
def test_unnamed_candidate_primitive_unsupported_is_stable_manual_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primitive: str,
    error_number: int,
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    def unsupported(*_args, **_kwargs):
        raise OSError(error_number, "injected unsupported primitive")

    monkeypatch.setattr(config_module, primitive, unsupported, raising=False)
    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert path.read_bytes() == original
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert _unsupported_path(path).exists()
    assert ntfy_disclosure_status(path).state == "manual_repair_required"


@pytest.mark.parametrize(
    "stage",
    [
        "before_marker_create",
        "before_marker_directory_fsync",
        "before_candidate_link",
        "before_candidate_directory_fsync",
        "before_exchange",
    ],
)
def test_parent_path_replacement_stops_each_transaction_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    parent = tmp_path / "config-parent"
    path = parent / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    displaced = tmp_path / "displaced-parent"
    replaced = False

    def replace_parent(observed: str) -> None:
        nonlocal replaced
        if observed != stage or replaced:
            return
        replaced = True
        parent.rename(displaced)
        parent.mkdir(mode=0o700)
        _write_bytes(path, original)

    monkeypatch.setattr(config_module, "_transaction_probe", replace_parent)
    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert replaced is True
    assert response["ok"] is False
    assert path.read_bytes() == original
    assert (displaced / path.name).read_bytes() == original
    assert not any(displaced.glob(".config.toml.ntfy-disclosure-*"))


@pytest.mark.parametrize(
    "stage",
    [
        "marker",
        "marker_fchmod",
        "marker_write",
        "marker_fsync",
        "marker_directory_fsync",
        "candidate",
        "candidate_fchmod",
        "candidate_write",
        "candidate_fsync",
        "candidate_directory_fsync",
        "exchange",
    ],
)
def test_parent_mode_is_revalidated_before_each_transaction_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    boundaries = {
        "marker": "before_marker_create",
        "marker_fchmod": "before_marker_fchmod",
        "marker_write": "before_marker_write",
        "marker_fsync": "before_marker_fsync",
        "marker_directory_fsync": "before_marker_directory_fsync",
        "candidate": "before_unnamed_candidate_create",
        "candidate_fchmod": "before_unnamed_candidate_fchmod",
        "candidate_write": "before_unnamed_candidate_write",
        "candidate_fsync": "before_unnamed_candidate_fsync",
        "candidate_directory_fsync": "before_candidate_directory_fsync",
        "exchange": "before_exchange",
    }

    def chmod_probe(observed: str) -> None:
        if observed == boundaries[stage]:
            path.parent.chmod(0o750)

    monkeypatch.setattr(config_module, "_transaction_probe", chmod_probe)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert path.read_bytes() == original
    path.parent.chmod(0o700)
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert ntfy_disclosure_status(path).state == "acknowledgement_required"


def test_missing_exchange_symbol_never_offers_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    monkeypatch.setattr(
        config_module, "_rename_exchange_function", lambda: None
    )

    assert ntfy_disclosure_status(path).state == "manual_repair_required"
    assert path.read_bytes() == original


def test_unsupported_filesystem_persists_stable_manual_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    def unsupported_exchange(*_args) -> None:
        raise OSError(errno.EOPNOTSUPP, "injected unsupported filesystem")

    monkeypatch.setattr(config_module, "_rename_exchange_at", unsupported_exchange)
    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert path.read_bytes() == original
    assert ntfy_disclosure_status(path).state == "manual_repair_required"
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert _unsupported_path(path).exists()


def test_restart_recovers_crash_after_unsupported_marker_is_durable(
    tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    child = os.fork()
    if child == 0:
        def unsupported_exchange(*_args) -> None:
            raise OSError(errno.EOPNOTSUPP, "injected unsupported filesystem")

        def crash_probe(observed: str) -> None:
            if observed == "after_unsupported_marker_durable":
                os._exit(86)

        config_module._rename_exchange_at = unsupported_exchange
        config_module._transaction_probe = crash_probe
        engine_api._response(
            _request(
                path,
                "config.ntfy_disclosure.acknowledge",
                {"expected_revision": revision},
            )
        )
        os._exit(87)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86

    assert path.read_bytes() == original
    assert ntfy_disclosure_status(path).state == "manual_repair_required"
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert _unsupported_path(path).exists()


def test_unsupported_recovery_preserves_manual_target_and_cleans_exact_candidate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    child = os.fork()
    if child == 0:
        def unsupported_exchange(*_args) -> None:
            raise OSError(errno.EOPNOTSUPP, "injected unsupported filesystem")

        def crash_probe(observed: str) -> None:
            if observed == "after_unsupported_marker_durable":
                os._exit(86)

        config_module._rename_exchange_at = unsupported_exchange
        config_module._transaction_probe = crash_probe
        engine_api._response(
            _request(
                path,
                "config.ntfy_disclosure.acknowledge",
                {"expected_revision": revision},
            )
        )
        os._exit(87)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86

    manual = _config_bytes(
        acknowledgement="ntfy_content_disclosure_acknowledged = true",
        extra="# manual replacement after unsupported result",
    )
    replacement = path.parent / "manual.toml"
    _write_bytes(replacement, manual)
    os.replace(replacement, path)

    assert ntfy_disclosure_status(path).state == "manual_repair_required"
    assert path.read_bytes() == manual
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert _unsupported_path(path).exists()


def test_marker_is_private_durable_and_precedes_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    observed: dict[str, object] = {}

    def inspect_marker(stage: str) -> None:
        if stage != "after_marker_durable":
            return
        marker, candidate = _transaction_paths(path)
        marker_stat = marker.stat()
        marker_payload = json.loads(marker.read_bytes())
        observed.update(
            regular=stat.S_ISREG(marker_stat.st_mode),
            mode=stat.S_IMODE(marker_stat.st_mode),
            owner=marker_stat.st_uid,
            candidate_exists=candidate.exists(),
            candidate_inode=marker_payload["candidate"]["inode"],
            marker_contains_topic=TOPIC.encode() in marker.read_bytes(),
        )

    monkeypatch.setattr(config_module, "_transaction_probe", inspect_marker)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is True
    assert observed["regular"] is True
    assert observed["mode"] == 0o600
    assert observed["owner"] == os.geteuid()
    assert observed["candidate_exists"] is False
    assert isinstance(observed["candidate_inode"], int)
    assert observed["candidate_inode"] > 0
    assert observed["marker_contains_topic"] is False


@pytest.mark.parametrize(
    ("stage", "expected_state"),
    [
        ("after_marker_durable", "acknowledgement_required"),
        ("after_candidate_link", "acknowledgement_required"),
        ("after_candidate_durable", "acknowledgement_required"),
        ("after_exchange", "normal_admission"),
    ],
)
def test_restart_recovers_crash_at_each_reordered_transaction_step(
    tmp_path: Path, stage: str, expected_state: str
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    _crash_at_transaction_stage(path, revision, stage)

    assert ntfy_disclosure_status(path).state == expected_state
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()
    assert not _unsupported_path(path).exists()
    if expected_state == "acknowledgement_required":
        assert path.read_bytes() == original
    else:
        assert load_config(path).ntfy_content_disclosure_acknowledged is True


def test_transaction_create_collision_preserves_preexisting_private_file(
    tmp_path: Path,
) -> None:
    parent_fd = os.open(tmp_path, config_module._directory_open_flags())
    name = ".config.toml.ntfy-disclosure-candidate"
    collision = tmp_path / name
    private_content = b"manual reserved-name content"
    _write_bytes(collision, private_content)
    try:
        with pytest.raises(FileExistsError):
            config_module._create_private_file_at(parent_fd, name, b"candidate")
    finally:
        os.close(parent_fd)

    assert collision.read_bytes() == private_content
    assert stat.S_IMODE(collision.stat().st_mode) == 0o600


def test_recovery_resumes_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    _crash_immediately_after_exchange(path, revision)
    marker, candidate = _transaction_paths(path)
    real_fsync = config_module.os.fsync
    failed = False

    def fail_first_directory_fsync(fd: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError("injected interrupted recovery")
        real_fsync(fd)

    monkeypatch.setattr(config_module.os, "fsync", fail_first_directory_fsync)

    assert ntfy_disclosure_status(path).state == "manual_repair_required"
    assert marker.exists()
    monkeypatch.setattr(config_module.os, "fsync", real_fsync)
    assert ntfy_disclosure_status(path).state == "normal_admission"
    assert not marker.exists()
    assert not candidate.exists()


def test_exchange_response_loss_reconciles_exact_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    real_exchange = config_module._rename_exchange_at

    def exchange_then_lose_response(parent_fd: int, first: str, second: str) -> None:
        real_exchange(parent_fd, first, second)
        raise OSError("injected exchange response loss")

    monkeypatch.setattr(config_module, "_rename_exchange_at", exchange_then_lose_response)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is True
    assert response["data"] == {"acknowledged": True}
    assert ntfy_disclosure_status(path).state == "normal_admission"
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()


@pytest.mark.parametrize("failure", ["write", "file_fsync", "exchange"])
def test_pre_replace_failures_preserve_exact_old_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    if failure == "write":

        def fail_write(*_args):
            raise OSError("injected")

        monkeypatch.setattr(config_module.os, "write", fail_write)
    elif failure == "file_fsync":
        real_fsync = config_module.os.fsync

        def fail_file_fsync(fd: int) -> None:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("injected")
            real_fsync(fd)

        monkeypatch.setattr(config_module.os, "fsync", fail_file_fsync)
    else:
        monkeypatch.setattr(
            config_module,
            "_rename_exchange_at",
            lambda *_args: (_ for _ in ()).throw(OSError("injected")),
        )

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "configuration_error"
    assert path.read_bytes() == original
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_candidate_creation_failure_preserves_exact_old_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    real_open = config_module.os.open

    def fail_candidate_open(name, flags, mode=0o777, *, dir_fd=None):
        if isinstance(name, str) and name.startswith(".config.toml.") and flags & os.O_CREAT:
            raise OSError("injected")
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(config_module.os, "open", fail_candidate_open)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "configuration_error"
    assert path.read_bytes() == original


def test_transaction_marker_directory_fsync_failure_prevents_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    real_fsync = config_module.os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected")
        real_fsync(fd)

    monkeypatch.setattr(config_module.os, "fsync", fail_directory_fsync)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "configuration_error"
    assert path.read_bytes() == original
    marker, candidate = _transaction_paths(path)
    assert not marker.exists()
    assert not candidate.exists()


def test_final_validation_failure_reports_unknown_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    def fail_final_load(_path: Path, *, lock_held: bool = False):
        raise ConfigError("injected")

    monkeypatch.setattr(config_module, "_load_runtime_config", fail_final_load)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "outcome_unknown"
    assert b"ntfy_content_disclosure_acknowledged = true" in path.read_bytes()


def test_protocol_rejects_unknown_payload_and_malformed_revision(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)

    status_unknown = engine_api._response(
        _request(path, "config.ntfy_disclosure.status", {"extra": True})
    )
    ack_unknown = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": "sha256:" + "a" * 64, "extra": True},
        )
    )
    malformed = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": "sha256:NOT-HEX"},
        )
    )

    assert status_unknown["error"]["code"] == "invalid_request"
    assert ack_unknown["error"]["code"] == "invalid_request"
    assert malformed["error"]["code"] == "invalid_request"


def test_status_and_errors_never_disclose_canary_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.toml"
    canary = "TOPIC_CANARY_PRIVATE_123456789"
    _write_bytes(
        path,
        _config_bytes(topic=None, extra=f'ntfy_topic = "{canary}!"'),
    )

    status = engine_api._response(_request(path, "config.ntfy_disclosure.status"))
    conflict = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": "sha256:" + "a" * 64},
        )
    )
    captured = capsys.readouterr()
    rendered = repr(status) + repr(conflict) + caplog.text + captured.out + captured.err

    assert canary not in rendered
    assert status["data"] == {"state": "manual_repair_required"}
    assert conflict["error"]["code"] == "conflict"


def test_lock_failure_uses_fixed_secret_free_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "PRIVATE_PATH_CANARY" / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    class BrokenLock:
        def __enter__(self):
            raise OSError(f"PRIVATE_PATH_CANARY {TOPIC}")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(config_module, "_config_serialization_lock", BrokenLock)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )
    rendered = repr(response) + caplog.text

    assert response["error"] == {
        "code": "configuration_error",
        "message": "Disclosure acknowledgement was not written",
    }
    assert "PRIVATE_PATH_CANARY" not in rendered
    assert TOPIC not in rendered


@pytest.mark.skipif(os.name != "posix", reason="systemd writable-state contract is POSIX-only")
def test_config_serialization_lock_uses_private_state_not_config_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config" / "config.toml"
    original = _write_legacy_config(path)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    legacy_target = tmp_path / "legacy-lock-target"
    legacy_target.write_bytes(b"legacy")
    path.with_name(f"{path.name}.lock").symlink_to(legacy_target)

    class ReadOnlyAdjacentLock:
        def __enter__(self):
            raise OSError("the configuration mount is read-only")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(config_module, "FileLock", lambda _path: ReadOnlyAdjacentLock())

    status = ntfy_disclosure_status(path)

    lock_path = state_home / "eom-email-watcher" / "config-serialization.lock"
    assert status.state == "acknowledgement_required"
    assert path.read_bytes() == original
    assert path.with_name(f"{path.name}.lock").is_symlink()
    assert legacy_target.read_bytes() == b"legacy"
    assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert lock_path.stat().st_nlink == 1
    assert lock_path.read_bytes() == b""



@pytest.mark.skipif(os.name != "posix", reason="secure state lock is POSIX-only")
@pytest.mark.parametrize("unsafe_mode", [0o755, 0o500])
def test_config_serialization_lock_rejects_unsafe_state_before_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_mode: int,
) -> None:
    path = tmp_path / "config" / "config.toml"
    original = _write_legacy_config(path)
    state_home = tmp_path / "state"
    lock_parent = state_home / "eom-email-watcher"
    lock_parent.mkdir(parents=True, mode=unsafe_mode)
    lock_parent.chmod(unsafe_mode)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    status = ntfy_disclosure_status(path)

    assert status.state == "manual_repair_required"
    assert path.read_bytes() == original
    assert not (lock_parent / "config-serialization.lock").exists()



@pytest.mark.skipif(os.name != "posix", reason="secure state lock is POSIX-only")
def test_config_serialization_lock_blocks_on_one_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    lock_path = state_home / "eom-email-watcher" / "config-serialization.lock"
    holder_acquired = threading.Event()
    release_holder = threading.Event()
    waiter_entered = threading.Event()
    identities: list[tuple[int, int]] = []

    def holder() -> None:
        with config_module._config_serialization_lock():
            locked = lock_path.stat()
            identities.append((locked.st_dev, locked.st_ino))
            holder_acquired.set()
            assert release_holder.wait(timeout=5)

    def waiter() -> None:
        assert holder_acquired.wait(timeout=5)
        with config_module._config_serialization_lock():
            locked = lock_path.stat()
            identities.append((locked.st_dev, locked.st_ino))
            waiter_entered.set()

    holder_thread = threading.Thread(target=holder)
    waiter_thread = threading.Thread(target=waiter)
    holder_thread.start()
    waiter_thread.start()
    assert holder_acquired.wait(timeout=5)
    assert not waiter_entered.wait(timeout=0.1)
    release_holder.set()
    holder_thread.join(timeout=5)
    waiter_thread.join(timeout=5)

    assert not holder_thread.is_alive()
    assert not waiter_thread.is_alive()
    assert waiter_entered.is_set()
    assert len(identities) == 2
    assert identities[0] == identities[1]



@pytest.mark.skipif(os.name != "posix", reason="secure state lock is POSIX-only")
@pytest.mark.parametrize("attack", ["parent_symlink", "lock_symlink", "lock_hardlink"])
def test_config_serialization_lock_rejects_unsafe_lock_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    path = tmp_path / "config" / "config.toml"
    original = _write_legacy_config(path)
    state_home = tmp_path / "state"
    lock_parent = state_home / "eom-email-watcher"
    state_home.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    target_parent = tmp_path / "other-state"
    target_parent.mkdir(mode=0o700)

    if attack == "parent_symlink":
        lock_parent.symlink_to(target_parent, target_is_directory=True)
    else:
        lock_parent.mkdir(mode=0o700)
        lock_path = lock_parent / "config-serialization.lock"
        target = tmp_path / "lock-target"
        target.write_bytes(b"")
        target.chmod(0o600)
        if attack == "lock_symlink":
            lock_path.symlink_to(target)
        else:
            os.link(target, lock_path)

    status = ntfy_disclosure_status(path)

    assert status.state == "manual_repair_required"
    assert path.read_bytes() == original



@pytest.mark.skipif(os.name != "posix", reason="secure state lock is POSIX-only")
def test_config_serialization_lock_rejects_replacement_after_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config" / "config.toml"
    original = _write_legacy_config(path)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    replaced = False

    def replace_lock(stage: str) -> None:
        nonlocal replaced
        if stage != "after_acquire" or replaced:
            return
        replaced = True
        lock_path = state_home / "eom-email-watcher" / "config-serialization.lock"
        displaced = lock_path.with_name("displaced.lock")
        os.replace(lock_path, displaced)
        lock_path.write_bytes(b"")
        lock_path.chmod(0o600)

    monkeypatch.setattr(config_module, "_config_serialization_lock_probe", replace_lock)

    status = ntfy_disclosure_status(path)

    assert replaced
    assert status.state == "manual_repair_required"
    assert path.read_bytes() == original



@pytest.mark.skipif(os.name != "posix", reason="secure state lock is POSIX-only")
def test_custom_config_paths_share_the_state_lock_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    first = tmp_path / "first" / "config.toml"
    second = tmp_path / "second" / "config.toml"
    _write_legacy_config(first)
    _write_legacy_config(second)
    lock_path = state_home / "eom-email-watcher" / "config-serialization.lock"

    assert ntfy_disclosure_status(first).state == "acknowledgement_required"
    initial = lock_path.stat()
    assert ntfy_disclosure_status(second).state == "acknowledgement_required"
    completed = lock_path.stat()

    assert (initial.st_dev, initial.st_ino) == (completed.st_dev, completed.st_ino)
    assert lock_path.read_bytes() == b""


def test_non_posix_lock_fallback_preserves_publication_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NonPosixOs:
        name = "nt"

        def __getattr__(self, name: str):
            return getattr(os, name)

    monkeypatch.setattr(config_module, "os", NonPosixOs())
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    with (
        pytest.raises(OSError, match="publication failed"),
        config_module._config_serialization_lock(),
    ):
        raise OSError("publication failed")
