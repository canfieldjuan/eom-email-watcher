"""The executable proof must not report success on missing or false evidence."""

import importlib.util
import os
import sys
import tomllib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "coi_local_proof", Path(__file__).resolve().parents[1] / "scripts" / "coi_local_proof.py"
)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


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
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from test_connect_entitlement_runtime import _install_bundle_keyring

    key = Ed25519PrivateKey.generate()
    source = tmp_path / "source"
    _install_bundle_keyring(source, key)
    monkeypatch.setattr(entitlement.sys, "_MEIPASS", "", raising=False)
    proof.stage_authority(tmp_path / "bundle", source / entitlement.BUNDLED_KEYRING)
    gate = entitlement.EntitlementGate.from_installation()
    assert dict(gate.keys) == {"test-key": key.public_key().public_bytes_raw()}


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
