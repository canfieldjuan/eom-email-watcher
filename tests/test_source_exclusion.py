"""Public source-exclusion and mixed-protocol cutover regressions."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from eom_email_watcher.db import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source_connect_entitlement(monkeypatch):
    from connect_automate import connect

    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: connect.entitlement.EntitlementDecision.ACTIVE,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor limit")
def test_source_cleanup_large_unit_has_bounded_descriptors(tmp_path):
    script = """
import resource,sys
from pathlib import Path
from eom_email_watcher.db import Store
store=Store(Path(sys.argv[1]));store.initialize()
resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
with store._source_cleanup_locks(str(i) for i in range(2000)):
 print('BOUNDED_SOURCE_SET',flush=True)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "mail.sqlite3")],
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "BOUNDED_SOURCE_SET"


def test_sorted_deduplicated_collision_and_nested_subset(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from eom_email_watcher import source_locks as locks

    store = Store(tmp_path / "mail.db")
    store.initialize()
    ids = [str(i) for i in range(300)]
    events = []

    @contextmanager
    def record(path, message, *, timeout_seconds):
        events.append(path.name)
        yield

    monkeypatch.setattr(locks, "connect_operation_lock", record)
    with store._source_cleanup_locks(ids), store._source_cleanup_locks(ids[:2]):
        pass
    assert events == [
        f"source-stripe-v1-{i:02d}.lock" for i in sorted({locks.source_lock_stripe(i) for i in ids})
    ]
    assert len(events) <= locks.SOURCE_LOCK_STRIPE_COUNT
    assert not list((tmp_path / ".connect-locks").glob("source-" + "?" * 64 + ".lock"))
    with store._source_cleanup_locks(["0"]):
        other = next(i for i in ids if locks.source_lock_stripe(i) != locks.source_lock_stripe("0"))
        with (
            pytest.raises(RuntimeError, match="frozen by the outer"),
            store._source_cleanup_locks([other]),
        ):
            pass


def test_legacy_drain_timeout_keeps_marker_unset_and_retry_adopts(tmp_path, monkeypatch):
    from connect_automate.locking import connect_source_lock_path

    from eom_email_watcher import source_locks as locks

    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
    legacy = connect_source_lock_path(store.path, "same-message")
    script = """
import sys
from pathlib import Path
from connect_automate.locking import connect_operation_lock
with connect_operation_lock(Path(sys.argv[1]), 'busy'):
 print('HELD',flush=True);sys.stdin.readline()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(legacy)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "HELD"
        monkeypatch.setattr(locks, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
        with pytest.raises(locks.SourceProtocolUnavailable, match="deferred"):
            locks.qualify_protocol(store, fresh=False)
        with store.connection() as db:
            assert not locks.protocol_ready(db)
        with (
            pytest.raises(locks.OperationLockBusy),
            store._source_cleanup_locks(["same-message"]),
        ):
            pytest.fail("unqualified cleanup entered")
        assert not list(legacy.parent.glob("source-stripe-v1-*.lock"))
    finally:
        output, error = holder.communicate("release\n", timeout=5)
        assert holder.returncode == 0, output + error
    locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert locks.protocol_ready(db)
    assert legacy.exists(), "legacy files must remain inert"
    with store._source_cleanup_locks(["same-message"]):
        pass


def test_cutover_holds_exclusive_admission_and_refuses_replaced_engine(
    paired_deployment, tmp_path, monkeypatch
):
    from test_deployment_lease import start_holder, stop_holder

    from eom_email_watcher import deployment
    from eom_email_watcher import source_locks as locks

    binary, _, _, directory = paired_deployment()
    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
    monkeypatch.setattr(deployment, "LEASE_TIMEOUT_SECONDS", 0.05)
    holder = start_holder(binary, directory, tmp_path, "shared")
    try:
        with pytest.raises(deployment.DeploymentError, match="Close the app"):
            locks.qualify_protocol(store, fresh=False)
    finally:
        stop_holder(holder)
    locks.qualify_protocol(store, fresh=False)
    monkeypatch.setattr(deployment, "_same_executable", lambda *_: False)
    with pytest.raises(deployment.DeploymentError, match="replaced"), store.connection():
        pytest.fail("legacy engine could open a connection after replacement")


def test_native_backend_refusal_and_invalid_timeouts(tmp_path, monkeypatch):
    from eom_email_watcher import source_locks as locks

    store = Store(tmp_path / "mail.db")
    store.initialize()
    for timeout in (False, -1, float("inf"), float("nan")):
        with (
            pytest.raises(ValueError),
            locks.source_locks(store.path, ["m"], timeout_seconds=timeout),
        ):
            pass
    monkeypatch.setattr(locks, "operation_lock_supported", lambda _: False)
    with (
        pytest.raises(locks.SourceProtocolUnavailable, match="native"),
        store._source_cleanup_locks(["m"]),
    ):
        pytest.fail("no native exclusion")
    assert not list((tmp_path / ".connect-locks").glob("source-stripe-v1-*.lock"))


@pytest.mark.parametrize("cleanup_before_reconcile", [False, True])
def test_post_with_refused_outcome_reconciles_without_second_post(
    tmp_path, monkeypatch, cleanup_before_reconcile, source_connect_entitlement
):
    from contextlib import contextmanager

    from connect_automate import connect
    from test_connect_v2_engine_api import (
        PDF,
        SeededMailboxGateway,
        api_request,
        capability,
        invocation_payload,
        patch_runtime,
        registered_capability,
        seeded_runtime,
        update,
    )

    from eom_email_watcher import db, deployment, engine_api

    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    patch_runtime(monkeypatch, runtime)
    monkeypatch.setattr(
        connect, "discover_capabilities", lambda **_: connect.CapabilityCatalog((selected,))
    )
    monkeypatch.setattr(
        connect,
        "registered_capability_for_reconciliation",
        lambda **_: (registered_capability(selected), None),
    )
    replaced = False
    real_admission = db.database_admission

    @contextmanager
    def admission():
        if replaced:
            raise deployment.DeploymentError(
                "The running engine was replaced; restart it before database access"
            )
        with real_admission():
            yield

    monkeypatch.setattr(db, "database_admission", admission)
    submissions, queries = [], []

    class Gateway(SeededMailboxGateway):
        def attachment_bytes(self, *args):
            assert not queries, "reconciliation must not refetch removed source"
            return PDF

    class Client:
        def __init__(self, _):
            pass

        def submit(self, job, content):
            nonlocal replaced
            submissions.append(job)
            replaced = True
            return update(job, "accepted")

        def get(self, job):
            queries.append(job.job_id)
            return update(job, "completed", payload=b"Reconciled provider result")

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *_: Gateway())
    monkeypatch.setattr(connect, "ConnectV2Client", Client)
    response = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    assert response["error"]["code"] == "deployment_refused", response
    replaced = False
    job_id = submissions[0].job_id
    before = runtime.store.connect_dispatch(job_id)
    assert before.state == "dispatching" and before.submission_possible
    if cleanup_before_reconcile:
        assert runtime.store.delete_message("message-1")
        assert runtime.store.connect_job(job_id) is not None
    response = engine_api._response(api_request(config_path, "connect.queue.pump"))
    assert response["ok"], response
    assert len(submissions) == 1
    assert queries == [job_id]
    if cleanup_before_reconcile:
        assert runtime.store.connect_job(job_id) is None
        assert runtime.store.connect_dispatch(job_id) is None
    else:
        assert runtime.store.connect_job(job_id).status == "completed"


@pytest.mark.parametrize("single_thread", [False, True])
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file limit")
def test_real_followed_purge_large_unit_under_service_file_limit(tmp_path, single_thread):
    script = """
import resource,sys
from datetime import timedelta
from pathlib import Path
sys.path.insert(0,sys.argv[2])
from test_thread_bodies import followed
from test_thread_reconcile import NOW,seed
store,state,_=followed(Path(sys.argv[1]))
for i in range(1100):
 seed(store,str(i),thread_id='vendor-thread' if sys.argv[3]=='one' else 'thread-'+str(i))
later=NOW+timedelta(days=35)
progress=state.load_or_start(later,30,{'units':[]})
for thread in state.threads():state.watermark(progress,thread)
state.complete(progress)
resource.setrlimit(resource.RLIMIT_NOFILE,(1024,1024))
scope=('gmail','mailbox',state.scope[2])
assert store.purge(30,now=later,after_poll=True,polled_scope=scope)==1101
assert store.recent(1)==[]
print('REAL_PURGE_1101_ROWS_RLIMIT_1024',flush=True)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            str(ROOT / "tests"),
            "one" if single_thread else "many",
        ],
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "REAL_PURGE_1101_ROWS_RLIMIT_1024"


def test_unknown_protocol_never_gets_overwritten(tmp_path):
    from eom_email_watcher import source_locks as locks

    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        db.execute(
            "UPDATE maintenance_state SET value='future-protocol' WHERE key='source_lock_protocol'"
        )
    with pytest.raises(locks.SourceProtocolUnavailable, match="unsupported"):
        locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT value FROM maintenance_state WHERE key='source_lock_protocol'"
            ).fetchone()[0]
            == "future-protocol"
        )


def test_packaged_non_linux_upgrade_refuses_unproven_cutover(tmp_path, monkeypatch):
    from eom_email_watcher import source_locks as locks

    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(locks.SourceProtocolUnavailable, match="installer update"):
        locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert not locks.protocol_ready(db)
    with store._source_cleanup_locks(["m"]) as acquired:
        assert acquired
    assert "upgrade pending" in store.source_lock_notice()


def test_closed_cutover_grant_cannot_be_reused(paired_deployment, tmp_path):
    from contextvars import copy_context

    from eom_email_watcher import deployment

    paired_deployment()
    store = Store(tmp_path / "mail.db")
    store.initialize()
    with deployment.database_admission(exclusive=True):
        with store.connection() as db:
            assert db.execute("SELECT 1").fetchone()[0] == 1
        copied = copy_context()

    def open_in_copied_context():
        with pytest.raises(deployment.DeploymentError, match="expired"), store.connection():
            pytest.fail("expired cutover context opened an unlocked database")

    copied.run(open_in_copied_context)
    with store.connection() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1


def test_cutover_grant_is_not_transferable_to_another_thread(paired_deployment, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from eom_email_watcher import deployment

    paired_deployment()
    store = Store(tmp_path / "mail.db")
    store.initialize()
    with deployment.database_admission(exclusive=True):
        copied = copy_context()

        def foreign_connection():
            with (
                pytest.raises(deployment.DeploymentError, match="another execution"),
                store.connection(),
            ):
                pytest.fail("foreign thread borrowed exclusive admission")

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(copied.run, foreign_connection).result(timeout=5)


def test_selected_legacy_operation_excludes_drain_until_mutation_finishes(tmp_path, monkeypatch):
    import threading
    from contextlib import contextmanager

    from eom_email_watcher import source_locks as locks

    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
    selected, resume, completed = threading.Event(), threading.Event(), threading.Event()
    original = locks.connect_operation_lock
    errors = []

    @contextmanager
    def paused(path, message, **kwargs):
        if locks.LEGACY_NAME.fullmatch(path.name):
            selected.set()
            assert resume.wait(5)
        with original(path, message, **kwargs):
            yield

    monkeypatch.setattr(locks, "connect_operation_lock", paused)

    def cleanup():
        try:
            with store._source_cleanup_locks(["m"]) as acquired:
                assert acquired
                with store.connection() as db:
                    assert not locks.protocol_ready(db)
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(target=cleanup)
    worker.start()
    try:
        assert selected.wait(5)
        monkeypatch.setattr(locks, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
        with pytest.raises(locks.SourceProtocolUnavailable, match="deferred"):
            locks.qualify_protocol(store, fresh=False)
        with store.connection() as db:
            assert not locks.protocol_ready(db)
    finally:
        resume.set()
        worker.join(5)
    assert completed.is_set() and not errors, errors
    locks.qualify_protocol(store, fresh=False)
    assert store.source_lock_notice() is None


def test_legacy_cleanup_batches_are_bounded_and_do_not_split_units(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from eom_email_watcher import source_locks as locks
    from eom_email_watcher.db import SOURCE_CLEANUP_LOCK_BATCH_SIZE

    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
    limit = SOURCE_CLEANUP_LOCK_BATCH_SIZE
    units = {str(i): [str(i)] for i in range(limit * 3 + 1)}
    units["large"] = [f"large-{i}" for i in range(limit + 1)]
    units["last"] = ["last"]
    real_lock = locks.connect_operation_lock
    active = peak = 0

    @contextmanager
    def tracked(*args, **kwargs):
        nonlocal active, peak
        with real_lock(*args, **kwargs):
            active += 1
            peak = max(peak, active)
            try:
                yield
            finally:
                active -= 1

    monkeypatch.setattr(locks, "connect_operation_lock", tracked)
    preserved, deleted = [], []
    for keys, rows in store._source_cleanup_groups(units):
        with store._source_cleanup_locks(rows) as acquired:
            if acquired:
                deleted.extend(keys)
            else:
                preserved.extend(keys)
    assert peak == limit
    assert active == 0
    assert preserved == ["large"]
    assert set(deleted) == set(units) - {"large"}
    assert "oversized thread deletion" in store.source_lock_notice()
    with (
        pytest.raises(locks.SourceProtocolUnavailable, match="deferred"),
        store.source_locks(["m"]),
    ):
        pytest.fail("legacy Connect must defer")
    locks.qualify_protocol(store, fresh=False)
    with store._source_cleanup_locks(units["large"]) as acquired:
        assert acquired
    assert store.source_lock_notice() is None


def test_legacy_cleanup_context_does_not_authorize_nested_connect(tmp_path):
    from eom_email_watcher import source_locks as locks

    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
    with store._source_cleanup_locks(["m"]) as acquired:
        assert acquired
        with (
            pytest.raises(locks.SourceProtocolUnavailable, match="deferred"),
            store.source_locks(["m"]),
        ):
            pytest.fail("Nested Connect bypassed qualification")
