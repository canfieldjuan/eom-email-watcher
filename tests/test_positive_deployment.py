import sys
from pathlib import Path

import pytest

from eom_email_watcher import deployment, engine_api

ROOT = Path(__file__).resolve().parents[1]
NAMES = tuple(sorted(path.name for path in (ROOT / "systemd").iterdir()))


@pytest.mark.parametrize(
    "field,value,advice",
    [
        ("NeedDaemonReload", "yes", "daemon-reload"),
        ("LoadState", "masked", "unmask"),
        ("LoadState", "not-found", "partial"),
        ("DropInPaths", "/public/override.conf", "drop-in"),
        ("FragmentPath", "/public/stale.service", "fragment"),
    ],
)
def test_graph_refusal_names_actual_repair(paired_deployment, field, value, advice):
    binary, _, _, _ = paired_deployment(changes={"eom-email-watcher.service": {field: value}})
    with pytest.raises(deployment.DeploymentError, match=advice):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("unit", NAMES)
@pytest.mark.parametrize(
    "field,value",
    [
        ("DropInPaths", "/public/activation.conf"),
        ("FragmentPath", "/public/alternate/unit.service"),
        ("NeedDaemonReload", "yes"),
    ],
)
def test_positive_graph_refuses_unpinned_unit(paired_deployment, unit, field, value):
    binary, _, _, _ = paired_deployment(changes={unit: {field: value}})
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def test_store_admission_uses_one_manager_read(tmp_path, paired_deployment):
    from eom_email_watcher.db import Store

    _, _, calls, _ = paired_deployment()
    with Store(tmp_path / "public.sqlite3").connection():
        pass
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["health.get", "config.initialize"])
def test_startup_refusal_uses_api_envelope(monkeypatch, operation):
    import io
    import json

    def refuse():
        raise deployment.DeploymentError("public startup refusal")

    monkeypatch.setattr(deployment, "verify_database_admission", refuse)
    monkeypatch.setattr(sys, "argv", ["/public/eom-mail-engine"])
    request = {"protocol": 1, "operation": operation, "payload": {}}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode())))
    output = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setitem(
        engine_api.OPERATIONS, operation, lambda _: pytest.fail("operation reached")
    )
    with pytest.raises(SystemExit) as exit_result:
        deployment.main()
    output.flush()
    assert exit_result.value.code == 2
    assert output.buffer.getvalue(), "startup refusal bypassed the API envelope"
    assert json.loads(output.buffer.getvalue()) == {
        "protocol": 1,
        "operation": operation,
        "ok": False,
        "error": {"code": "deployment_refused", "message": "public startup refusal"},
    }


@pytest.mark.parametrize("raw,code", [
    (b"", "invalid_json"),
    (b"{" + b" " * engine_api.MAX_REQUEST_BYTES, "request_too_large"),
    (b'{"protocol":99,"operation":"health.get"}', "unsupported_protocol"),
])
def test_api_parses_and_bounds_before_admission(monkeypatch, raw, code):
    import io
    import json

    monkeypatch.setattr(
        deployment, "verify_database_admission", lambda: pytest.fail("admission reached")
    )
    monkeypatch.setattr(sys, "argv", ["/public/eom-mail-engine"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    output = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdout", output)
    with pytest.raises(SystemExit) as result:
        deployment.main()
    output.flush()
    assert result.value.code == 2
    assert json.loads(output.buffer.getvalue())["error"]["code"] == code


@pytest.mark.parametrize(
    "cause", [RuntimeError("public write failure"), ValueError("public invalid write")]
)
def test_connect_persistence_wraps_other_errors(monkeypatch, cause):
    failure = engine_api.connect.ConnectError("PUBLIC", "public provider refusal", retryable=False)

    def refuse(*args):
        raise cause

    monkeypatch.setattr(engine_api, "_mark_connect_failed", refuse)
    with pytest.raises(
        RuntimeError, match="Connect failure could not be persisted safely"
    ) as observed:
        engine_api._persist_connect_failure(None, "public-job", None, failure)
    assert observed.value.__cause__ is failure


@pytest.mark.parametrize("path", ["legacy", "generic", "entitlement", "submission"])
def test_connect_persistence_keeps_deployment_identity(monkeypatch, path):
    import hashlib
    from contextlib import nullcontext
    from types import SimpleNamespace

    refusal = deployment.DeploymentError("public persistence refusal")
    failure = engine_api.connect.ConnectError("PUBLIC", "public provider refusal", retryable=False)

    def submit(*args):
        raise failure

    def persist(*args):
        raise refusal

    runtime = SimpleNamespace(
        store=SimpleNamespace(
            connect_dispatch=lambda _: SimpleNamespace(source_available=True),
        )
    )
    capability = SimpleNamespace()
    job = SimpleNamespace(
        job_id="public-job",
        artifact=SimpleNamespace(
            byte_size=1,
            sha256=hashlib.sha256(b"x").hexdigest(),
        ),
    )
    client = SimpleNamespace(submit=submit)
    monkeypatch.setattr(engine_api.connect, "ConnectClient", lambda _: client)
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", lambda _: client)
    monkeypatch.setattr(engine_api, "_mark_connect_failed", persist)
    monkeypatch.setattr(engine_api, "_require_generic_connect_source_lock", lambda *args: None)
    monkeypatch.setattr(engine_api, "connect_operation_lock", lambda *args: nullcontext())
    monkeypatch.setattr(engine_api, "_require_submission_authority_for_job", lambda *a, **k: True)
    with pytest.raises(deployment.DeploymentError) as observed:
        if path == "legacy":
            engine_api._run_connect_job(runtime, capability, job, b"x")
        elif path == "generic":
            engine_api._run_generic_connect_job(runtime, capability, job, b"x")
        elif path == "entitlement":
            engine_api._persist_connect_entitlement_failure(
                runtime, job.job_id, capability, failure
            )
        else:
            engine_api._submit_generic_connect_job(
                runtime,
                capability,
                job,
                "public-message",
                lambda: b"x",
                inactive_dispatch_state="waiting",
            )
    assert observed.value is refusal


def test_database_connection_checks_each_later_open(tmp_path, paired_deployment):
    from eom_email_watcher.db import Store

    _, records, calls, _ = paired_deployment()
    store = Store(tmp_path / "public.sqlite3")
    with store.connection() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    records["eom-email-watcher.service"]["NeedDaemonReload"] = "yes"
    with pytest.raises(deployment.DeploymentError), store.connection():
        pytest.fail("cached admission allowed another open")
    assert calls == ["snapshot", "snapshot"]


def test_public_schema_28_refusal_precedes_migration(tmp_path, monkeypatch, paired_deployment):
    import hashlib
    import io
    import json
    import sqlite3
    from contextlib import closing

    from test_db import _reset_to_schema_28

    from eom_email_watcher.db import SCHEMA_VERSION, Store

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    config = tmp_path / "config.toml"
    response = engine_api._response(
        {
            "protocol": 1,
            "operation": "config.initialize",
            "config_path": str(config),
            "payload": {
                "model_base_url": "http://127.0.0.1:9/v1",
                "model_name": "public-proof",
                "timezone": "America/Chicago",
            },
        }
    )
    assert response["ok"] is True
    database = state / "eom-email-watcher/watcher.sqlite3"
    store = Store(database)
    store.initialize()
    _reset_to_schema_28(store)
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 28
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    binary, records, _, _ = paired_deployment(
        changes={
            "eom-email-watcher.timer": {"LoadState": "not-found"},
        }
    )
    opened = []
    connect = sqlite3.connect

    def record_open(*args, **kwargs):
        opened.append(args)
        return connect(*args, **kwargs)

    with monkeypatch.context() as boundary:
        boundary.setattr(sqlite3, "connect", record_open)
        with pytest.raises(deployment.DeploymentError):
            store.initialize()
    assert opened == [], "migration must refuse before acquiring the database"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 28
    request = {"protocol": 1, "operation": "health.get", "config_path": str(config), "payload": {}}

    def invoke(argv, payload):
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
        output = io.TextIOWrapper(io.BytesIO())
        monkeypatch.setattr(sys, "stdout", output)
        with pytest.raises(SystemExit) as result:
            deployment.main()
        output.flush()
        return result.value.code, output.buffer.getvalue()

    status, output = invoke([str(binary)], json.dumps(request).encode())
    assert status == 2
    assert json.loads(output)["error"]["code"] == "deployment_refused"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 28
    records["eom-email-watcher.timer"]["LoadState"] = "loaded"
    status, output = invoke([str(binary)], json.dumps(request).encode())
    assert status == 0, output
    assert json.loads(output)["ok"] is True
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    status, output = invoke(
        [str(binary), "--cli", "--config", str(config), "recent", "--limit", "1"], b""
    )
    assert status == 0
    assert json.loads(output) == []


def test_deployment_refusal_has_distinct_type():
    assert not issubclass(deployment.DeploymentError, RuntimeError)


def test_api_keeps_deployment_refusal(monkeypatch):
    def refuse(request):
        raise deployment.DeploymentError("Scheduled deployment requires repair")

    monkeypatch.setitem(engine_api.OPERATIONS, "doctor", refuse)
    result = engine_api._response({"protocol": 1, "operation": "doctor"})
    assert result["error"] == {
        "code": "deployment_refused",
        "message": "Scheduled deployment requires repair",
    }


@pytest.mark.parametrize("verb", ["check", "send-hours"])
def test_scheduled_alias_checks_admission_before_dispatch(monkeypatch, verb):
    from eom_email_watcher import cli

    monkeypatch.setattr(sys, "argv", ["/public/eom-mail-watch", verb])

    def refuse():
        raise deployment.DeploymentError("public startup refusal")

    monkeypatch.setattr(deployment, "verify_database_admission", refuse)
    reached = []
    monkeypatch.setattr(cli, "main", lambda args: reached.append(args))
    with pytest.raises(SystemExit, match="2"):
        deployment.main()
    assert reached == []


def test_scheduled_argv_name_cannot_be_another_alias(paired_deployment):
    binary, _, _, _ = paired_deployment(wrong_argv=True)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def test_stale_fire_handler_propagates_deployment_type(monkeypatch):
    from types import SimpleNamespace

    error = deployment.DeploymentError("public deployment refusal")

    def refuse(**kwargs):
        raise error

    store = SimpleNamespace(decide_automation_fire=refuse)
    monkeypatch.setattr(engine_api, "_runtime", lambda request: SimpleNamespace(store=store))
    request = {
        "payload": {
            "fire_id": "00000000-0000-4000-8000-000000000001",
            "expected_version": 1,
            "prepared_identity_sha256": "0" * 64,
            "decision": "confirmed",
        }
    }
    with pytest.raises(deployment.DeploymentError) as observed:
        engine_api._automation_fire_decide(request)
    assert observed.value is error


@pytest.mark.parametrize("handler", ["update", "failure"])
def test_connect_concurrent_handlers_do_not_retry_deployment(monkeypatch, handler):
    from types import SimpleNamespace

    error = deployment.DeploymentError("public deployment refusal")
    calls = []

    def refuse(**kwargs):
        calls.append(kwargs)
        raise error

    store = SimpleNamespace(
        connect_job=lambda job: SimpleNamespace(status="requested"), transition_connect_job=refuse
    )
    update = SimpleNamespace(
        job_id="public-job",
        status="accepted",
        provider_app_id="public-provider",
        provider_instance_id="public-instance",
        result=None,
        error=None,
    )
    with pytest.raises(deployment.DeploymentError) as observed:
        if handler == "update":
            engine_api._apply_connect_update(store, update)
        else:
            engine_api._mark_connect_failed(
                store,
                "public-job",
                SimpleNamespace(app_id="public-provider", instance_id="public-instance"),
                engine_api.connect.ConnectError("PUBLIC", "public error"),
            )
    assert observed.value is error
    assert len(calls) == 1


@pytest.mark.parametrize("state", ["writing", "write_authorized", "unresolved"])
def test_service_concurrent_handlers_do_not_skip_deployment(monkeypatch, state):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from eom_email_watcher import service

    error = deployment.DeploymentError("public deployment refusal")
    current = SimpleNamespace(
        run_id="public-run",
        state_version=1,
        state=state,
        provider="microsoft365",
        account_id="public-account",
        calendar_principal_key="public-principal",
    )
    work = SimpleNamespace(run=current)
    calls = []

    def refuse(*args, **kwargs):
        calls.append((args, kwargs))
        raise error

    store = SimpleNamespace(
        pending_automation_calendar_writes=lambda *args, **kwargs: (work,),
        automation_run=lambda run: current,
        transition_automation_calendar_write=refuse,
        begin_automation_calendar_write=refuse,
    )
    monkeypatch.setattr(
        service, "_scheduling_write_authorization", lambda *args, **kwargs: SimpleNamespace()
    )
    with pytest.raises(deployment.DeploymentError) as observed:
        service.process_scheduling_writes(
            SimpleNamespace(), store, now=datetime(2026, 10, 7, tzinfo=UTC)
        )
    assert observed.value is error
    assert len(calls) == 1


def test_deployment_rules_have_one_source_owner():
    import ast

    callers = {}
    for source in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(source.read_text())
        for owner in ast.walk(tree):
            if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(owner):
                if not isinstance(call, ast.Call):
                    continue
                name = ast.unparse(call.func)
                if name in {"sqlite3.connect", "subprocess.Popen", "_unit_payloads"}:
                    callers.setdefault(name, []).append(
                        (source.relative_to(ROOT).as_posix(), owner.name)
                    )
    assert callers["sqlite3.connect"] == [("src/eom_email_watcher/db.py", "connection")]
    assert callers["subprocess.Popen"] == [
        ("src/eom_email_watcher/deployment.py", "_read_manager_command")
    ]
    assert callers["_unit_payloads"] == [
        ("src/eom_email_watcher/deployment.py", "deployment_description")
    ]
    builder = ast.parse((ROOT / "scripts/build_desktop_sidecar.py").read_text())
    assert (
        sum(
            isinstance(node, ast.Call) and ast.unparse(node.func) == "_unit_payloads"
            for node in ast.walk(builder)
        )
        == 1
    )


def test_store_admission_launches_one_actual_manager_process(
    tmp_path, monkeypatch, paired_deployment
):
    import subprocess

    from packaged_proof_environment import render_unit_records

    from eom_email_watcher.db import Store

    output = deployment._manager_output
    python = sys.executable
    popen = subprocess.Popen
    _, records, _, _ = paired_deployment()
    raw = render_unit_records(records)
    commands = []

    def launch(command, **kwargs):
        commands.append(command)
        return popen([python, "-c", "import sys;sys.stdout.write(sys.argv[1])", raw], **kwargs)

    monkeypatch.setattr(deployment, "_manager_output", output)
    monkeypatch.setattr(subprocess, "Popen", launch)
    with Store(tmp_path / "public.sqlite3").connection():
        pass
    assert commands == [
        [
            "systemctl",
            "--user",
            "show",
            "--all",
            "--no-pager",
            "--property=" + ",".join(deployment._MANAGER_FIELDS),
            *deployment.UNIT_NAMES, *deployment._PLATFORM_SELECTORS,
        ]
    ]


def test_deployment_description_uses_manager_home(paired_deployment, monkeypatch, tmp_path):
    binary, _, _, directory = paired_deployment()
    manager_home = directory.parents[2]
    monkeypatch.setattr(
        deployment, "_manager_environment", lambda: {"HOME": str(manager_home)}, raising=False
    )
    monkeypatch.setenv("HOME", str(tmp_path / "invoking-home"))
    assert deployment.deployment_description(binary).alias == (
        manager_home / ".local/bin/eom-mail-watch"
    )


def test_selected_engine_cannot_be_alias(paired_deployment):
    binary, _, _, _ = paired_deployment()
    alias = deployment.deployment_description(binary).alias
    with pytest.raises(deployment.DeploymentError, match="sidecar"):
        deployment.deployment_description(alias)


def test_publisher_refuses_a_same_process_open_connection(paired_deployment, monkeypatch, tmp_path):
    from eom_email_watcher.db import Store

    paired_deployment()
    actions = []
    monkeypatch.setattr(deployment, "_manager_action", lambda args: actions.append(args))
    with Store(tmp_path / "public.sqlite3").connection(), pytest.raises(
        deployment.DeploymentError, match="connection"
    ):
        deployment.install_user_services()
    assert actions == []
