from __future__ import annotations

import hashlib
import os
import stat
import threading
from pathlib import Path

import pytest

import eom_email_watcher.config as config_module
from eom_email_watcher import engine_api
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


def test_legacy_valid_topic_without_ack_requires_explicit_migration(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)

    with pytest.raises(ConfigError, match="ntfy_content_disclosure_acknowledged"):
        load_config(path)

    response = engine_api._response(
        _request(path, "config.ntfy_disclosure.status")
    )
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
            _config_bytes(
                acknowledgement="ntfy_content_disclosure_acknowledged = false"
            ),
            "acknowledgement_required",
        ),
        (
            _config_bytes(
                acknowledgement="ntfy_content_disclosure_acknowledged = true"
            ),
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
            _config_bytes(
                acknowledgement='ntfy_content_disclosure_acknowledged = "false"'
            ),
            "manual_repair_required",
        ),
        (
            _config_bytes(
                acknowledgement="ntfy_content_disclosure_acknowledged = 0"
            ),
            "manual_repair_required",
        ),
        (
            _config_bytes(
                acknowledgement="ntfy_content_disclosure_acknowledged = [false]"
            ),
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
        {"state", "expected_revision"}
        if state == "acknowledgement_required"
        else {"state"}
    )
    assert path.read_bytes() == original


def test_status_reports_missing_without_creating_parent(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "config.toml"

    assert ntfy_disclosure_status(path).state == "missing"
    assert not path.parent.exists()


@pytest.mark.parametrize("unsafe", ["file_mode", "parent_mode", "hardlink", "fifo"])
def test_status_rejects_unsafe_filesystem_boundaries(
    tmp_path: Path, unsafe: str
) -> None:
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
    assert (
        ntfy_disclosure_status(parent_link / "config.toml").state
        == "manual_repair_required"
    )
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
                    "[[senders]]\nemail = \"person@example.com\""
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
    assert path.read_bytes() == (
        b"ntfy_content_disclosure_acknowledged = true\r\n" + original
    )


def test_quoted_root_key_and_no_final_newline_preserve_exact_suffix(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = _write_bytes(
        path,
        _config_bytes().rstrip(b"\n")
        + b'\n"ntfy_content_disclosure_acknowledged" = false',
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
    assert sum(
        response.get("error", {}).get("code") == "conflict"  # type: ignore[union-attr]
        for response in responses
    ) == 1
    assert ntfy_disclosure_status(path).state == "normal_admission"


def test_manual_edit_during_precommit_wins_without_lost_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    edited = original + b"# edit outside lock\n"
    real_replace = config_module._durable_replace_at

    def edit_then_replace(
        parent_fd,
        name,
        content,
        *,
        before_replace=None,
        validate_displaced=None,
    ):
        _write_bytes(path, edited)
        return real_replace(
            parent_fd,
            name,
            content,
            before_replace=before_replace,
            validate_displaced=validate_displaced,
        )

    monkeypatch.setattr(config_module, "_durable_replace_at", edit_then_replace)

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
    real_replace = config_module._durable_replace_at

    def edit_after_check(
        parent_fd,
        name,
        content,
        *,
        before_replace=None,
        validate_displaced=None,
    ):
        def check_then_edit() -> None:
            if before_replace is not None:
                before_replace()
            _write_bytes(path, edited)

        return real_replace(
            parent_fd,
            name,
            content,
            before_replace=check_then_edit,
            validate_displaced=validate_displaced,
        )

    monkeypatch.setattr(config_module, "_durable_replace_at", edit_after_check)

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


def test_metadata_change_after_precommit_check_is_atomically_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    real_replace = config_module._durable_replace_at

    def broaden_after_check(
        parent_fd,
        name,
        content,
        *,
        before_replace=None,
        validate_displaced=None,
    ):
        def check_then_broaden() -> None:
            if before_replace is not None:
                before_replace()
            path.chmod(0o640)

        return real_replace(
            parent_fd,
            name,
            content,
            before_replace=check_then_broaden,
            validate_displaced=validate_displaced,
        )

    monkeypatch.setattr(config_module, "_durable_replace_at", broaden_after_check)

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


def test_displaced_validation_failure_rolls_back_and_reports_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    real_read = config_module._read_safe_file_at

    def fail_displaced_read(parent_fd: int, name: str):
        if name.startswith(f".{path.name}."):
            raise OSError("injected displaced validation failure")
        return real_read(parent_fd, name)

    monkeypatch.setattr(config_module, "_read_safe_file_at", fail_displaced_read)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "outcome_unknown"
    assert path.read_bytes() == original
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_exchange_rollback_failure_restores_manual_bytes_without_temp_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.toml"
    original = _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None
    edited = original + b"# rollback failure canary\n"
    real_durable = config_module._durable_replace_at
    real_exchange = config_module._rename_exchange_at
    exchange_calls = 0

    def edit_after_check(
        parent_fd,
        name,
        content,
        *,
        before_replace=None,
        validate_displaced=None,
    ):
        def check_then_edit() -> None:
            if before_replace is not None:
                before_replace()
            _write_bytes(path, edited)

        return real_durable(
            parent_fd,
            name,
            content,
            before_replace=check_then_edit,
            validate_displaced=validate_displaced,
        )

    def fail_rollback(parent_fd: int, first: str, second: str) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 2:
            raise OSError("injected rollback failure")
        real_exchange(parent_fd, first, second)

    monkeypatch.setattr(config_module, "_durable_replace_at", edit_after_check)
    monkeypatch.setattr(config_module, "_rename_exchange_at", fail_rollback)

    response = engine_api._response(
        _request(
            path,
            "config.ntfy_disclosure.acknowledge",
            {"expected_revision": revision},
        )
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "outcome_unknown"
    assert path.read_bytes() == edited
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    assert TOPIC not in repr(response) + caplog.text


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
        if (
            isinstance(name, str)
            and name.startswith(".config.toml.")
            and flags & os.O_CREAT
        ):
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


def test_directory_fsync_failure_reports_unknown_with_valid_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
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
    assert response["error"]["code"] == "outcome_unknown"
    assert load_config(path).ntfy_content_disclosure_acknowledged is True


def test_final_validation_failure_reports_unknown_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    _write_legacy_config(path)
    revision = ntfy_disclosure_status(path).expected_revision
    assert revision is not None

    def fail_final_load(_path: Path | None = None):
        raise ConfigError("injected")

    monkeypatch.setattr(config_module, "load_config", fail_final_load)

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

    monkeypatch.setattr(config_module, "FileLock", lambda _path: BrokenLock())

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
