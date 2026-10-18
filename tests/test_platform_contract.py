"""Exercise real pytest collection, including its log and JUnit boundary."""

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
ROOT = Path(__file__).resolve().parents[1]


def _owner(pytester, *, tags=("nt", "win32")):
    source = (ROOT / "tests/conftest.py").read_text()
    pytester.makeconftest(
        source + f"\n_PRODUCT_ROOT = Path({str(ROOT)!r})\n"
        f"def _platform_tags():\n    return {set(tags)!r}\n"
    )


def _inventory(pytester, result):
    lines = result.stdout.lines
    prefix = "platform-contract excluded: "
    logged = json.loads(next(line[len(prefix):] for line in lines if line.startswith(prefix)))
    document = ET.parse(pytester.path / "results.xml")
    properties = {
        p.attrib["name"]: p.attrib["value"]
        for p in document.findall(".//testsuite/properties/property")
    }
    assert json.loads(properties["platform_contract.excluded"]) == logged
    assert int(properties["platform_contract.excluded_count"]) == len(logged)
    return logged, document


def test_platform_contract_inventory_matches_log_and_junit(pytester):
    _owner(pytester)
    pytester.makepyfile('''
        import pytest
        @pytest.mark.platform_contract(
            platforms=("posix",), owner="src/eom_email_watcher/config.py:3145",
            reason="POSIX acknowledgement implementation")
        def test_posix():
            pass
        @pytest.mark.platform_contract(
            platforms=("nt",), owner="src/eom_email_watcher/config.py:1432",
            reason="Windows manual repair policy")
        def test_windows():
            pass
        def test_portable():
            pass
    ''')
    result = pytester.runpytest("-q", "--junitxml=results.xml")
    result.assert_outcomes(passed=2, deselected=1)
    inventory, document = _inventory(pytester, result)
    assert len(inventory) == 1
    assert inventory[0]["nodeid"].endswith("::test_posix")
    assert inventory[0]["contracts"][0]["owner"] == "src/eom_email_watcher/config.py:3145"
    assert document.find("testsuite").attrib["tests"] == "2"


@pytest.mark.parametrize("declaration", [
    'platforms=("posix",), reason="missing citation"',
    'platforms=("posix",), owner="", reason="empty citation"',
    'platforms=("posix",), owner="src/eom_email_watcher/missing.py:1", reason="bad path"',
    'platforms=("posix",), owner="src/eom_email_watcher/config.py:999999", reason="bad line"',
    'platforms=("posix",), owner="tests/conftest.py:1", reason="not product owner"',
    'platforms=(), owner="src/eom_email_watcher/config.py:3145", reason="empty platforms"',
    'platforms=("unknown",), owner="src/eom_email_watcher/config.py:3145", reason="bad OS"',
    'platforms=("posix",), owner="src/eom_email_watcher/config.py:3145", reason=""',
], ids=["missing", "empty", "missing-file", "line-range", "not-product", "no-os", "bad-os",
        "no-reason"])
def test_platform_contract_rejects_invalid_even_when_inapplicable(pytester, declaration):
    _owner(pytester)
    pytester.makepyfile(f'''
        import pytest
        @pytest.mark.platform_contract({declaration})
        def test_invalid():
            pytest.fail("invalid contracts must stop collection")
    ''')
    result = pytester.runpytest("-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*invalid platform contract*"])


@pytest.mark.parametrize("tags, passed, excluded", [
    (("nt", "win32"), 1, 2), (("posix", "linux"), 3, 0),
])
def test_platform_contract_inherits_fixture_once_through_transitive_consumers(
    pytester, tags, passed, excluded
):
    _owner(pytester, tags=tags)
    pytester.makepyfile('''
        import pytest
        @pytest.fixture
        def indirect(paired_deployment):
            return paired_deployment
        def test_direct(paired_deployment):
            assert callable(paired_deployment)
        def test_transitive(indirect):
            assert callable(indirect)
        def test_portable():
            pass
    ''')
    result = pytester.runpytest("-q", "--junitxml=results.xml")
    result.assert_outcomes(passed=passed, deselected=excluded)
    inventory, _ = _inventory(pytester, result)
    assert len(inventory) == excluded
    if excluded:
        assert {entry["nodeid"].split("::")[-1] for entry in inventory} == {
            "test_direct", "test_transitive",
        }
        assert all(len(entry["contracts"]) == 1 for entry in inventory)


@pytest.mark.parametrize("with_test", [False, True], ids=["empty", "all-excluded"])
def test_platform_contract_empty_and_all_excluded_are_reported(pytester, with_test):
    _owner(pytester)
    pytester.makepyfile('''
        import pytest
        @pytest.mark.platform_contract(
            platforms=("posix",), owner="src/eom_email_watcher/config.py:3145",
            reason="POSIX acknowledgement implementation")
        def test_posix():
            pytest.fail("not applicable on Windows")
    ''' if with_test else "# no tests")
    result = pytester.runpytest("-q", "--junitxml=results.xml")
    assert result.ret == pytest.ExitCode.NO_TESTS_COLLECTED
    inventory, document = _inventory(pytester, result)
    assert len(inventory) == int(with_test)
    assert document.find("testsuite").attrib["tests"] == "0"


def test_platform_contract_inventory_preserves_real_failure(pytester):
    _owner(pytester)
    pytester.makepyfile('''
        import pytest
        @pytest.mark.platform_contract(
            platforms=("posix",), owner="src/eom_email_watcher/config.py:3145",
            reason="POSIX acknowledgement implementation")
        def test_posix():
            pass
        def test_real_failure():
            assert False, "must remain a genuine failing testcase"
    ''')
    result = pytester.runpytest("-q", "--junitxml=results.xml")
    result.assert_outcomes(failed=1, deselected=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    inventory, document = _inventory(pytester, result)
    assert len(inventory) == 1
    suite = document.find("testsuite")
    assert suite.attrib["tests"] == suite.attrib["failures"] == "1"
    assert suite.find("testcase/failure") is not None


def test_platform_contract_rejects_uncited_fixture_owner(pytester):
    _owner(pytester)
    conftest = pytester.path / "conftest.py"
    with conftest.open("a") as stream:
        stream.write('''
@platform_fixture(platforms=("posix",), owner="", reason="uncited fixture")
def unused_fixture():
    pass
''')
    pytester.makepyfile("def test_portable(): pass")
    result = pytester.runpytest("-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*invalid platform contract*"])


def test_platform_contract_windows_selection_keeps_applicable_controls(pytester):
    _owner(pytester)
    # Collect the real modules with the real owner; this simulates collection
    # applicability only, never Windows execution or native qualification.
    modules = pytester.path / "tests"
    modules.mkdir()
    shutil.copytree(ROOT / "systemd", pytester.path / "systemd")
    for alias, filename in {
        "test_qualified_ntfy": "test_ntfy_disclosure_migration.py",
        "test_qualified_deployment": "test_positive_deployment.py",
        "test_qualified_source": "test_source_exclusion.py",
    }.items():
        (modules / f"{alias}.py").write_text((ROOT / "tests" / filename).read_text())
    result = pytester.runpytest("--collect-only", "-q", "--junitxml=results.xml")
    assert result.ret == pytest.ExitCode.OK
    inventory, _ = _inventory(pytester, result)
    collected = [line for line in result.stdout.lines if line.startswith("tests/test_qualified")]
    excluded = {entry["nodeid"] for entry in inventory}
    assert any("::test_native_windows_ntfy_manual_repair" in item for item in collected)
    assert any("::test_status_boundary_matrix" in item and "normal_admission" in item
               for item in collected)
    assert any("::test_status_reports_missing_without_creating_parent" in item
               for item in collected)
    assert any("::test_api_parses_and_bounds_before_admission[oversized]" in item
               for item in collected)
    assert any("::test_legacy_valid_topic_without_ack_requires_explicit_migration" in item
               for item in excluded)
    assert any("::test_graph_refusal_names_actual_repair" in item for item in excluded)
    assert all(entry["contracts"] for entry in inventory)
