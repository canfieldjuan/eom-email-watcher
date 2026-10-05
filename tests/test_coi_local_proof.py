"""The executable proof must not report success on missing or false evidence."""

import copy
import importlib.util
import os
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "coi_local_proof", Path(__file__).resolve().parents[1] / "scripts" / "coi_local_proof.py"
)
proof = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(Path(spec.origin).parent))
try:
    spec.loader.exec_module(proof)
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
def cli(tmp_path, monkeypatch):
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
        proof.evidence_directory(output)
    assert not output.exists()


def test_external_evidence_destination_is_accepted(tmp_path):
    output = tmp_path / "new" / "evidence"
    assert proof.evidence_directory(output) == output
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
