"""The executable proof must not report success on missing or false evidence."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "coi_local_proof", Path(__file__).resolve().parents[1] / "scripts" / "coi_local_proof.py"
)
proof = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(Path(spec.origin).parent))
try:
    spec.loader.exec_module(proof)
    import coi_evidence
finally:
    sys.path.pop(0)


def complete_checks():
    return dict.fromkeys(
        (
            "one_fire",
            "one_attempt",
            "one_job",
            "expected_terminal",
            "expected_projection",
            "replay_unchanged",
            "restart_unchanged",
        ),
        True,
    )


def test_complete_proof_is_accepted():
    proof.require_checks(complete_checks())


@pytest.mark.parametrize("value", [False, None, 0, 1, "", "true"])
def test_non_boolean_or_false_proof_is_rejected(value):
    checks = complete_checks()
    checks["expected_projection"] = value
    with pytest.raises(RuntimeError, match="Incomplete or failed"):
        proof.require_checks(checks)


@pytest.mark.parametrize("key", list(complete_checks()))
def test_missing_proof_is_rejected(key):
    checks = complete_checks()
    del checks[key]
    with pytest.raises(RuntimeError, match="Incomplete or failed"):
        proof.require_checks(checks)


def test_staged_authority_uses_shared_package_namespace(tmp_path, monkeypatch):
    from connect_automate import entitlement
    from test_desktop_packaging import _write_entitlement_keyring

    source = tmp_path / "release-keyring.json"
    _write_entitlement_keyring(source)
    monkeypatch.setattr(entitlement.sys, "_MEIPASS", "", raising=False)
    digest = proof.stage_authority(tmp_path / "bundle", source)
    gate = entitlement.EntitlementGate.from_installation()
    assert frozenset(gate.keys.items()) == entitlement.APPROVED_RELEASE_AUTHORITIES
    assert digest == proof.hashlib.sha256(source.read_bytes()).hexdigest()
    assert (tmp_path / "bundle" / entitlement.BUNDLED_KEYRING).read_bytes() == source.read_bytes()



class ReachedRuntime(Exception):
    """Stop after proof admission/configuration, before external services."""


@pytest.fixture
def identity_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    files = {
        "src/eom_email_watcher/__init__.py": "",
        "src/eom_email_watcher/engine_api.py": "# engine\n",
        "scripts/coi_local_proof.py": "# proof\n",
        "scripts/build_desktop_sidecar.py": "# approved validator\n",
        "scripts/coi_evidence.py": "# evidence owner\n",
        "uv.lock": "# dependency lock\n",
        ".gitignore": "ignored-output/\n",
    }
    for name, content in files.items():
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for args in (
        ["init", "--quiet"], ["add", "."],
        ["-c", "user.name=Proof Test", "-c", "user.email=fixture@example.invalid",
         "commit", "--quiet", "-m", "clean fixture"],
    ):
        subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True)
    monkeypatch.setattr(proof, "CHECKOUT", checkout)
    monkeypatch.setattr(proof, "__file__", str(checkout / "scripts/coi_local_proof.py"))
    monkeypatch.setattr(proof.engine_api, "__file__",
                        str(checkout / "src/eom_email_watcher/engine_api.py"))
    monkeypatch.setattr(proof, "sys", SimpleNamespace(modules={
        "eom_email_watcher": SimpleNamespace(
            __file__=str(checkout / "src/eom_email_watcher/__init__.py")
        ),
        "eom_email_watcher.engine_api": proof.engine_api,
        **{name: module for name, module in sys.modules.items()
           if name == "connect_automate" or name.startswith("connect_automate.")},
    }))
    return checkout


@pytest.mark.parametrize("name,change", [
    ("scripts/build_desktop_sidecar.py", "modified"),
    ("scripts/build_desktop_sidecar.py", "staged"),
    ("scripts/build_desktop_sidecar.py", "deleted"),
    ("scripts/coi_evidence.py", "modified"),
    ("scripts/coi_local_proof.py", "modified"),
    ("src/eom_email_watcher/engine_api.py", "modified"),
    ("uv.lock", "modified"),
    ("scripts/future_proof_helper.py", "untracked"),
])
def test_identity_rejects_checkout_changes(identity_checkout, name, change):
    path = identity_checkout / name
    if change == "deleted":
        path.unlink()
    else:
        path.write_text("# changed admission behavior\n")
    if change == "staged":
        subprocess.run(["git", "-C", str(identity_checkout), "add", name], check=True)
    if change == "untracked":
        subprocess.run(
            ["git", "-C", str(identity_checkout), "config", "status.showUntrackedFiles", "no"],
            check=True,
        )
    with pytest.raises(RuntimeError, match="uncommitted"):
        proof.watcher_identity()


def test_identity_accepts_clean_checkout_with_ignored_output(identity_checkout):
    expected = subprocess.check_output(
        ["git", "-C", str(identity_checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    assert proof.watcher_identity()["watcher_head"] == expected
    output = identity_checkout / "ignored-output"
    output.mkdir()
    (output / "scratch.txt").write_text("build artifact")
    assert proof.watcher_identity()["watcher_head"] == expected


def test_identity_records_executed_connect_sources(identity_checkout):
    identity = proof.watcher_identity()
    dependency = identity["connect_dependency"]
    assert dependency["version"]
    source = Path(proof.connect.__file__)
    assert dependency["sources"]["connect.py"] == proof.hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    assert "entitlement.py" in dependency["sources"]


@pytest.mark.parametrize("filename", ["connect.py", "entitlement.py", "lazy_module.py"])
def test_package_identity_changes_with_executable_code(tmp_path, monkeypatch, filename):
    (tmp_path / "__init__.py").write_text("")
    module = tmp_path / filename
    module.write_text("original = True\n")
    monkeypatch.setattr(proof, "sys", SimpleNamespace(modules={}))
    before = proof.package_sources("dependency", tmp_path)
    module.write_text("original = False\n")
    after = proof.package_sources("dependency", tmp_path)
    assert before[filename] != after[filename]


def test_package_identity_rejects_foreign_loaded_module(tmp_path, monkeypatch):
    package = tmp_path / "dependency"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text("")
    module = SimpleNamespace(__file__=str(source))
    monkeypatch.setattr(proof, "sys", SimpleNamespace(modules={"dependency": module}))
    assert proof.package_sources("dependency", package)
    foreign = tmp_path / "foreign.py"
    foreign.write_text("foreign = True\n")
    module.__file__ = str(foreign)
    with pytest.raises(RuntimeError, match="outside the recorded"):
        proof.package_sources("dependency", package)


def test_checkout_status_has_one_owner_and_no_path_filter():
    import ast

    tree = ast.parse(Path(spec.origin).read_text())
    owners = []
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        for call in ast.walk(function):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            command = call.args[0]
            if not isinstance(command, ast.List):
                continue
            literals = [node.value for node in command.elts if isinstance(node, ast.Constant)]
            if "git" in literals and "status" in literals:
                owners.append(function.name)
                assert "--" not in literals, "Source admission must cover the whole checkout"
    assert owners == ["watcher_identity"]


@pytest.fixture
def cli(tmp_path, monkeypatch, identity_checkout):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    old_umask = os.umask(0o077)
    monkeypatch.setattr(proof, "stage_authority", lambda *_: None)

    def stop_at_runtime(_):
        raise ReachedRuntime

    monkeypatch.setattr(proof, "load_runtime", stop_at_runtime)

    def configure(*, model_kind="fixture", directory="evidence"):
        output = tmp_path / directory
        argv = [
            "coi_local_proof.py", "--pdf", str(pdf), "--evidence-dir", str(output),
            "--keyring", str(tmp_path / "public-keyring.json"),
            "--expect-policy-count", "0", "--today", "2026-09-20",
            "--expect-provider-instance-id", PROVIDER["instance_id"],
            "--expect-provider-app-version", PROVIDER["app_version"],
            "--expect-capability-version", PROVIDER["capability_version"],
        ]
        if model_kind is not None:
            argv += ["--model-kind", model_kind]
        monkeypatch.setattr(sys, "argv", argv)
        return output

    yield configure
    os.umask(old_umask)


def test_model_kind_omission_is_rejected_before_io(cli, capsys):
    output = cli(model_kind=None)
    with pytest.raises(SystemExit) as error:
        proof.main()
    assert error.value.code == 2
    assert "--model-kind" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("model_kind", ["real", "fixture"])
def test_explicit_model_declarations_reach_runtime(cli, model_kind):
    cli(model_kind=model_kind)
    with pytest.raises(ReachedRuntime):
        proof.main()


def test_admitted_input_is_retained_with_its_oracles(cli, tmp_path):
    output = cli()
    original = tmp_path / "input.pdf"
    content = original.read_bytes()
    with pytest.raises(ReachedRuntime):
        proof.main()
    original.unlink()
    retained = output / "input.pdf"
    assert retained.is_file(), "Proof must retain the admitted PDF bytes"
    assert retained.read_bytes() == content
    assert retained.stat().st_mode & 0o777 == 0o600
    inputs = json.loads((output / "run-inputs.json").read_text())
    assert inputs["input_sha256"] == proof.hashlib.sha256(content).hexdigest()
    assert inputs["expected_policy_count"] == 0
    assert inputs["today"] == "2026-09-20"


@pytest.fixture
def completed_main_run(cli, tmp_path, monkeypatch):
    from test_certificate_expiry_ledger import (
        _capability_result,
        _certificate_fire_job,
        _record,
    )
    from test_connect_v2_engine_api import PDF, capability, seeded_runtime

    output = cli()
    Path(sys.argv[sys.argv.index("--pdf") + 1]).write_bytes(PDF)
    sys.argv[sys.argv.index("--expect-policy-count") + 1] = "4"
    seed = tmp_path / "seed"
    seed.mkdir()
    _, runtime = seeded_runtime(seed)
    _, job_id = _certificate_fire_job(runtime.store)
    selected = capability(app_id="invoice-processor", app_version="0.1.0",
                          capability_id="certificate.extract",
                          produces=("application/vnd.local-connect.certificate+json",),
                          parameters=())
    update = proof.connect.CapabilityJobUpdate(
        job_id=job_id, status="completed", provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id, result=_capability_result(_record()), error=None,
    )
    get = Mock(return_value=update)
    reconcile = Mock(wraps=runtime.store.reconcile_certificate_completed_replay)
    monkeypatch.setattr(runtime.store, "reconcile_certificate_completed_replay", reconcile)
    monkeypatch.setattr(proof, "MESSAGE_ID", "message-1")
    monkeypatch.setattr(proof, "load_runtime", lambda _: runtime)
    monkeypatch.setattr(proof.StagedMailbox, "seed_message", lambda *_: None)
    monkeypatch.setattr(proof.StagedMailbox, "seed_analysis", lambda *_: None)
    monkeypatch.setattr(proof.connect, "discover_capabilities",
                        lambda: SimpleNamespace(items=[selected]))
    monkeypatch.setattr(proof, "select_provider", lambda *_: selected)
    monkeypatch.setattr(proof.connect, "ConnectV2Client", lambda *_: SimpleNamespace(get=get))

    def ledger_response():
        return {"ok": True, "data": {"items": runtime.store.list_certificate_expiry_ledger(
            today="2026-09-20", limit=100
        )}}

    def response(request):
        if request["operation"] == "certificate.expiry_ledger.list":
            return ledger_response()
        if request["operation"] == "connect.queue.pump":
            if runtime.store.connect_job(job_id).status == "requested":
                proof.engine_api._apply_connect_update(runtime.store, update)
            else:
                assert runtime.store.due_connect_lane_heads() == ()
        else:
            assert request["operation"] == "automation.rules.put"
        return {"ok": True}

    monkeypatch.setattr(proof.engine_api, "_response", response)
    monkeypatch.setattr(proof.sys, "executable", sys.executable, raising=False)
    monkeypatch.setattr(proof, "subprocess", SimpleNamespace(
        check_output=subprocess.check_output,
        run=lambda *_, **__: SimpleNamespace(stdout=json.dumps(ledger_response())),
    ))
    proof.main()
    return output, get, reconcile


def test_main_replays_completed_terminal_update(completed_main_run):
    output, get, reconcile = completed_main_run
    summary = json.loads((output / "watcher-result-summary.json").read_text())
    assert summary["checks"]["replay_unchanged"] is True
    get.assert_called_once()
    reconcile.assert_called_once()


def test_summary_records_input_oracles(completed_main_run):
    output, _, _ = completed_main_run
    summary = json.loads((output / "watcher-result-summary.json").read_text())
    assert summary["expected_policy_count"] == 4
    assert summary["today"] == "2026-09-20"


@pytest.fixture
def failed_terminal_replay(tmp_path, monkeypatch):
    from test_certificate_expiry_ledger import _certificate_fire_job
    from test_connect_v2_engine_api import capability, seeded_runtime

    _, runtime = seeded_runtime(tmp_path)
    _, job_id = _certificate_fire_job(runtime.store)
    selected = capability(app_id="invoice-processor", app_version="0.1.0",
                          capability_id="certificate.extract",
                          produces=("application/vnd.local-connect.certificate+json",),
                          parameters=())
    update = proof.connect.CapabilityJobUpdate(
        job_id=job_id, status="failed", provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id, result=None,
        error=proof.connect.ConnectError("DOCUMENT_UNREADABLE", "Cannot read PDF"),
    )
    job = proof.engine_api._apply_connect_update(runtime.store, update)
    get = Mock(return_value=update)
    monkeypatch.setattr(proof.connect, "ConnectV2Client", lambda *_: SimpleNamespace(get=get))
    monkeypatch.setattr(proof, "ROOT", tmp_path, raising=False)
    return runtime, selected, job, get, update


@pytest.mark.parametrize("change", [
    {"code": "INTERNAL_ERROR"}, {"message": "Different failure"}, {"retryable": True}, None,
])
def test_failed_replay_rejects_changed_error(failed_terminal_replay, change):
    runtime, selected, job, get, update = failed_terminal_replay
    error = None if change is None else proof.connect.ConnectError(**{
        "code": "DOCUMENT_UNREADABLE", "message": "Cannot read PDF", "retryable": False,
        **change,
    })
    get.return_value = replace(update, error=error)
    with pytest.raises(RuntimeError, match="recorded terminal error"):
        proof.replay_terminal_update(runtime, selected, job)
    assert runtime.store.connect_job(job.job_id) == job
    assert not (proof.ROOT / "replayed-terminal-update.json").exists()


def test_failed_replay_retains_matching_error(failed_terminal_replay):
    runtime, selected, job, get, _ = failed_terminal_replay
    proof.replay_terminal_update(runtime, selected, job)
    get.assert_called_once()
    assert runtime.store.connect_job(job.job_id) == job
    receipt = json.loads((proof.ROOT / "replayed-terminal-update.json").read_text())
    assert receipt["error"] == receipt["recorded_error"] == {
        "code": "DOCUMENT_UNREADABLE", "message": "Cannot read PDF", "retryable": False,
    }


def test_foreign_loaded_package_is_rejected_before_io(cli, monkeypatch, tmp_path):
    output = cli()
    monkeypatch.setattr(proof.engine_api, "__file__", str(tmp_path / "installed/engine_api.py"))
    with pytest.raises(RuntimeError, match="checkout"):
        proof.main()
    assert not output.exists()


@pytest.mark.parametrize("directory", ["ordinary", 'quote"path', "back\\slash", "line\nbreak"])
def test_evidence_paths_round_trip_through_config(cli, monkeypatch, directory):
    output = cli(directory=directory)

    def check_config(path):
        config = tomllib.loads(path.read_text())
        for key, filename in {
            "gmail_credentials_file": "credentials.json",
            "gmail_token_file": "token.json",
            "gmail_send_token_file": "send-token.json",
            "database_file": "watcher.sqlite3",
        }.items():
            assert config[key] == str(output / "watcher-state" / filename)
        raise ReachedRuntime

    monkeypatch.setattr(proof, "load_runtime", check_config)
    with pytest.raises(ReachedRuntime):
        proof.main()


PROVIDER = {
    "app_id": "invoice-processor", "app_version": "0.1.0",
    "capability_id": "certificate.extract", "capability_version": "1.0",
    "instance_id": "5eed6fd7-964e-4ab0-8952-8ab448ae51c0",
}


@pytest.mark.parametrize("field", list(PROVIDER))
def test_stale_provider_is_rejected(field):
    stale = {**PROVIDER, field: "stale"}
    with pytest.raises(RuntimeError, match="provider"):
        proof.select_provider([SimpleNamespace(**stale)], PROVIDER)


def test_expected_provider_is_accepted():
    item = SimpleNamespace(**PROVIDER)
    assert proof.select_provider([item], PROVIDER) is item


@pytest.mark.parametrize("marker", ["directory", "file"])
@pytest.mark.parametrize("symlink", [False, True])
def test_git_evidence_destination_is_rejected(tmp_path, marker, symlink):
    repo = tmp_path / "repo"
    repo.mkdir()
    if marker == "directory":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: /private/worktree-metadata")
    output = repo / "nested" / "evidence"
    if symlink:
        link = tmp_path / "alias"
        link.symlink_to(repo, target_is_directory=True)
        output = link / "nested" / "evidence"
    with pytest.raises(RuntimeError, match="Git"):
        coi_evidence.evidence_directory(output)
    assert not output.exists()


def test_external_evidence_destination_is_accepted(tmp_path):
    output = tmp_path / "new" / "evidence"
    assert coi_evidence.evidence_directory(output) == output
    assert not output.exists()


def test_development_authority_is_rejected_before_staging(tmp_path, monkeypatch):
    from connect_automate import entitlement
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from test_connect_entitlement_runtime import _install_bundle_keyring

    source = tmp_path / "source"
    _install_bundle_keyring(source, Ed25519PrivateKey.generate())
    bundle = tmp_path / "bundle"
    monkeypatch.setattr(entitlement.sys, "_MEIPASS", "", raising=False)
    with pytest.raises(RuntimeError, match="non-production|approved production"):
        proof.stage_authority(bundle, source / entitlement.BUNDLED_KEYRING)
    assert not bundle.exists()


def projection_case():
    parent = {"certificate_id": "certificate-1", "source_message_id": "message-1",
              "source_part_id": "part-1", "connect_job_id": "job-1"}
    children = [
        {"certificate_id": "certificate-1", "policy_id": "policy-0", "ordinal": 0,
         "expiration_date_iso": "2028-01-01"},
        {"certificate_id": "certificate-1", "policy_id": "policy-1", "ordinal": 1,
         "expiration_date_iso": "2027-01-01"},
    ]
    rows = [{**parent, "policy_id": child["policy_id"], "policy_ordinal": child["ordinal"]}
            for child in reversed(children)]
    return {"ok": True, "data": {"items": rows}}, [parent], children


@pytest.mark.parametrize("defect", ["duplicate", "wrong_parent", "wrong_policy", "missing",
                                     "reverse", "wrong_ordinal", "bool_ordinal", "wrong_source"])
def test_corrupt_projection_is_rejected(defect):
    ledger, parents, children = projection_case()
    rows = ledger["data"]["items"]
    if defect == "duplicate":
        rows[1] = copy.deepcopy(rows[0])
    elif defect == "wrong_parent":
        rows[0]["certificate_id"] = "other"
    elif defect == "wrong_policy":
        rows[0]["policy_id"] = "other"
    elif defect == "missing":
        del rows[0]["policy_id"]
    elif defect == "reverse":
        rows.reverse()
    elif defect == "wrong_ordinal":
        rows[0]["policy_ordinal"] = 9
    elif defect == "bool_ordinal":
        rows[0]["policy_ordinal"] = True
    else:
        rows[0]["connect_job_id"] = "other"
    assert not proof.projection_matches(ledger, parents, children, 2, None)


def test_correct_projection_is_accepted():
    ledger, parents, children = projection_case()
    assert proof.projection_matches(ledger, parents, children, 2, None)


@pytest.mark.parametrize("items", [[], [PROVIDER, PROVIDER]])
def test_missing_or_duplicate_expected_provider_is_rejected(items):
    with pytest.raises(RuntimeError, match="provider"):
        proof.select_provider([SimpleNamespace(**item) for item in items], PROVIDER)


def test_matching_provider_is_selected_among_other_instances():
    matching = SimpleNamespace(**PROVIDER)
    stale = SimpleNamespace(**{**PROVIDER, "instance_id": "stale"})
    assert proof.select_provider([stale, matching], PROVIDER) is matching


def test_zero_policy_placeholder_and_empty_error_projection():
    ledger, parents, _ = projection_case()
    ledger["data"]["items"] = [{**parents[0], "policy_id": None, "policy_ordinal": None}]
    assert proof.projection_matches(ledger, parents, [], 0, None)
    del ledger["data"]["items"][0]["policy_ordinal"]
    assert not proof.projection_matches(ledger, parents, [], 0, None)
    empty = {"ok": True, "data": {"items": []}}
    assert proof.projection_matches(empty, [], [], 0, "DOCUMENT_UNREADABLE")
    assert not proof.projection_matches(empty, parents, [], 0, None)


def test_projection_date_ties_and_unknown_dates_use_policy_order():
    ledger, parents, children = projection_case()
    for expiration in (None, "2027-01-01"):
        for child in children:
            child["expiration_date_iso"] = expiration
        ledger["data"]["items"].sort(key=lambda row: row["policy_ordinal"])
        assert proof.projection_matches(ledger, parents, children, 2, None)
        ledger["data"]["items"].reverse()
        assert not proof.projection_matches(ledger, parents, children, 2, None)


def test_projection_known_date_sorts_before_unknown():
    ledger, parents, children = projection_case()
    children[0]["expiration_date_iso"] = None
    assert proof.projection_matches(ledger, parents, children, 2, None)
    ledger["data"]["items"].reverse()
    assert not proof.projection_matches(ledger, parents, children, 2, None)


def test_receipt_names_every_seeded_boundary():
    from unittest.mock import Mock
    runtime = SimpleNamespace(store=Mock())
    staged = proof.StagedMailbox(b"public fixture")
    assert set(staged.substitutions) == {"attachment_retrieval"}
    staged.seed_message(runtime)
    runtime.store.add_message.assert_called_once()
    runtime.store.replace_attachments.assert_called_once()
    assert "message_analysis" not in staged.substitutions
    staged.seed_analysis(runtime)
    runtime.store.mark_analyzed.assert_called_once()
    assert set(staged.substitutions) == {
        "message_ingestion", "attachment_discovery", "message_analysis", "attachment_retrieval",
    }


def test_proof_tools_use_one_evidence_directory_owner():
    import ast
    root = Path(proof.__file__).parent
    tree = ast.parse((root / "coi_local_proof.py").read_text())
    assignments = [node for node in ast.walk(tree)
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "ROOT"
                           for target in node.targets)]
    assert len(assignments) == 1
    assert isinstance(assignments[0].value, ast.Call)
    assert isinstance(assignments[0].value.func, ast.Name)
    assert assignments[0].value.func.id == "create_evidence_directory"
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and isinstance(node.func.value, ast.Name) and node.func.value.id == "ROOT"
                   and node.func.attr == "mkdir" for node in ast.walk(tree))
    assert not any(isinstance(node, ast.FunctionDef) and node.name == "evidence_directory"
                   for node in ast.walk(tree))
    assert proof.create_evidence_directory is coi_evidence.create_evidence_directory
