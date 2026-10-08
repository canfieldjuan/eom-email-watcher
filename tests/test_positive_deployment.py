import sys
from pathlib import Path

import pytest

from eom_email_watcher import deployment, engine_api

ROOT = Path(__file__).resolve().parents[1]
NAMES = tuple(sorted(path.name for path in (ROOT / "systemd").iterdir()))


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
        ("src/eom_email_watcher/deployment.py", "_manager_output")
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
            *deployment.UNIT_NAMES,
        ]
    ]
