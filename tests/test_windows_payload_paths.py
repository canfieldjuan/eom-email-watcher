"""The qualification oracle interprets NSIS output aliases without losing paths."""

from pathlib import Path

import prove_windows_cutover as proof
import pytest

ROOT = Path(__file__).resolve().parents[1]


def product_owner():
    owner = getattr(proof, "package_program_paths", None)
    assert callable(owner), "The proof compares raw archive aliases with installed paths"
    return owner


def test_package_paths_follow_actual_output_directory_alias():
    hooks = (ROOT / "desktop/src-tauri/windows/hooks.nsh").read_text()
    payload = {
        "$8/eom-email-watcher-desktop.exe": {},
        "$8/eom-mail-engine.exe": {},
        "uninstall.exe": {},
        "$PLUGINSDIR/compiler-helper.exe": {},
    }
    assert product_owner()(payload, hooks) == {
        "eom-email-watcher-desktop.exe", "eom-mail-engine.exe", "uninstall.exe"
    }


def test_package_paths_preserve_subdirectories_and_new_programs():
    hooks = 'StrCpy $9 "$OUTDIR"\nSetOutPath "$9"\n'
    assert product_owner()({"$9/tools/new.EXE": {}, "other/program.exe": {}}, hooks) == {
        "tools/new.exe", "other/program.exe"
    }


@pytest.mark.parametrize("name", ["$7/a.exe", "$8/../a.exe", "/a.exe", "C:/a.exe", "$8/$7/a.exe"])
def test_package_paths_refuse_unauthenticated_destinations(name):
    hooks = 'StrCpy $8 "$OUTDIR"\nSetOutPath "$8"\n'
    with pytest.raises(ValueError):
        product_owner()({name: {}}, hooks)


def test_package_paths_require_save_and_restore_of_same_alias():
    with pytest.raises(ValueError):
        product_owner()({"$8/new.exe": {}}, 'StrCpy $8 "$OUTDIR"\nSetOutPath "$9"\n')


def test_package_paths_do_not_collapse_distinct_payload_programs():
    hooks = 'StrCpy $8 "$OUTDIR"\nSetOutPath "$8"\n'
    with pytest.raises(ValueError):
        product_owner()({"$8/program.exe": {}, "program.exe": {}}, hooks)


@pytest.mark.parametrize("change", ["same", "extra", "missing"])
def test_catalog_oracle_keeps_exact_payload_set(tmp_path, change):
    hooks = (ROOT / "desktop/src-tauri/windows/hooks.nsh").read_text()
    payload = {"$8/desktop.exe": {}, "$8/engine.exe": {}, "uninstall.exe": {}}
    if change == "extra":
        payload["$8/new-program.exe"] = {}
    if change == "missing":
        payload.pop("$8/engine.exe")
    catalog = ["EW-PACKAGE-FILES-V1", "desktop.exe", "engine.exe", "uninstall.exe"]
    if change == "same":
        proof.verify_package_programs(payload, catalog, hooks, tmp_path / "programs.json")
    else:
        with pytest.raises(AssertionError, match="Owned programs differ"):
            proof.verify_package_programs(payload, catalog, hooks, tmp_path / "programs.json")
