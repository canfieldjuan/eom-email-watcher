"""Populated schema-34 upgrade through actual installed Windows IPC owners.

Test-authority installer only: no public release trust changes, host licences,
patched gateways/models, direct post-upgrade Store mutations or fake receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from prove_windows_cutover import build_old_actor, isolated_environment
from windows_cutover_fixture import (
    GREENMAIL_SHA256,
    MailFixture,
    reference_provider,
    write_private,
)
from windows_qualification_environment import powershell_environment

from eom_email_watcher.engine_api import ADMISSION_REQUIRED_OPERATIONS
from eom_email_watcher.source_locks import source_lock_path


class Engine:
    def __init__(self, binary, config, folder, environment):
        self.binary, self.config = binary, config
        self.folder, self.environment = folder, environment

    def request(self, operation, payload=None):
        value = {
            "protocol": 1,
            "operation": operation,
            "config_path": str(self.config),
            "payload": payload or {},
        }
        if operation in ADMISSION_REQUIRED_OPERATIONS:
            value["admission_token"] = self.call("config.admission.snapshot")["token"]
        return json.dumps(value)

    def start(self, operation, payload=None):
        command = self.request(operation, payload)
        process = subprocess.Popen(
            [str(self.binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.folder,
            env=self.environment,
        )
        process.stdin.write(command)
        process.stdin.close()
        process.stdin = None
        return process

    def call(self, operation, payload=None):
        process = self.start(operation, payload)
        stdout, stderr = process.communicate(timeout=150)
        value = json.loads(stdout)
        assert process.returncode == 0 and value["ok"], (operation, value, stderr)
        with (self.folder / "operations.jsonl").open("a", encoding="utf-8") as stream:
            (self.folder / "operations.jsonl").chmod(0o600)
            stream.write(json.dumps({"operation": operation, "result": value}) + "\n")
        return value["data"]


def scalar(database, sql, values=()):
    with sqlite3.connect(database) as db:
        return db.execute(sql, values).fetchone()[0]


def invocation(engine, message_id, part_id):
    catalog = engine.call(
        "connect.attachment.capabilities", {"message_id": message_id, "part_id": part_id}
    )
    matches = [
        item
        for item in catalog["items"]
        if item["provider"]["app_id"] == "connect-reference-provider"
        and item["capability"]["id"] == "document.summarize"
    ]
    assert len(matches) == 1, catalog
    item = matches[0]
    return {
        "request_id": str(uuid4()),
        "message_id": message_id,
        "part_id": part_id,
        "provider": {k: item["provider"][k] for k in ("app_id", "version", "instance_id")},
        "capability": {k: item["capability"][k] for k in ("id", "version")},
        "parameters": {},
        "confirmed": False,
    }


def attachment(database, subject):
    with sqlite3.connect(database) as db:
        result = db.execute(
            """SELECT m.message_id,a.part_id FROM messages m
            JOIN message_attachments a ON a.message_id=m.message_id WHERE m.subject=?""",
            (subject,),
        ).fetchall()
    assert len(result) == 1, result
    return result[0]


def prove_removal_recovery(new, database):
    """Retain the real pending receipt across restart before releasing its source."""
    expected_clear = scalar(database, "SELECT COUNT(*) FROM logical_messages")
    assert expected_clear >= 2, "The populated removal proof needs retained messages"
    with sqlite3.connect(database) as db:
        source_id = db.execute(
            "SELECT message_id FROM messages ORDER BY message_id LIMIT 1"
        ).fetchone()[0]

    def retained_storage():
        with sqlite3.connect(database) as db:
            return {
                table: db.execute(f"SELECT * FROM {table} ORDER BY message_id").fetchall()
                for table in (
                    "messages", "message_bodies", "message_body_unavailable", "message_locations",
                )
            }

    before = retained_storage()
    # Qualification has already completed: only the canonical mapped stripe,
    # not a legacy source filename, excludes the current completion owner.
    holder = subprocess.Popen(
        [sys.executable, "-c", """
import sys
from pathlib import Path
from connect_automate.locking import connect_operation_lock
with connect_operation_lock(Path(sys.argv[1]),'busy'):
 print('REMOVAL_SOURCE_HELD',flush=True)
 sys.stdin.read(1)
""", str(source_lock_path(database, source_id))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=new.environment,
    )
    try:
        assert holder.stdout.readline().strip() == "REMOVAL_SOURCE_HELD"
        # Persist only the opaque key before mutation and discard its response.
        prepared = new.call("inbox.removal.prepare", {"kind": "clear"})
        request_file = new.folder / "removal-request.json"
        write_private(request_file, json.dumps(prepared).encode())
        new.call("inbox.clear", prepared)
        recovered_key = json.loads(request_file.read_text())["request_id"]
        resumed = Engine(new.binary, new.config, new.folder, new.environment)
        frozen = resumed.call(
            "inbox.removal.status", {"request_id": recovered_key, "kind": "clear"}
        )
        assert holder.poll() is None, "Source holder ended before status recovery"
        assert frozen["outcome"] == "deferred", "Missing held-pending installed proof"
        assert (frozen["requested"], frozen["completed"], frozen["pending"]) == (
            expected_clear, 0, expected_clear
        ), frozen
        assert frozen["request_id"] == recovered_key and frozen["intent_ids"]
        assert retained_storage() == before, "Held acceptance/restart changed physical storage"
        assert scalar(database, "SELECT COUNT(*) FROM logical_messages") == 0
        return resumed, recovered_key, frozen, expected_clear
    finally:
        if holder.poll() is None:
            holder.stdin.write("x")
            holder.stdin.flush()
            assert holder.wait(timeout=10) == 0


def run(args):
    folder = args.output.resolve()
    assert os.name == "nt", "Native Windows required"
    inventory = json.loads((folder / "build-inventory.json").read_text())
    assert inventory["purpose"].startswith("TEST ONLY")
    assert inventory["parity"]["result"] == "PASS", "Measured payload parity required"
    assert (
        hashlib.sha256(args.installer.read_bytes()).hexdigest()
        == inventory["parity"]["installer_sha256"]
    )
    assert (
        hashlib.sha256(args.engine.read_bytes()).hexdigest()
        == inventory["qualification_engine_sha256"]
    )
    old_actor = build_old_actor(args.predecessor.resolve(), folder, folder / "fixture-keyring.json")
    private, installation = folder / "private", folder / "installed"
    private.mkdir(mode=0o700)
    installation.mkdir(mode=0o700)
    from connect_automate.connect_windows import _protect_windows_directory

    _protect_windows_directory(private)
    environment = isolated_environment(private)
    environment["XDG_RUNTIME_DIR"] = str(private)
    engine = installation / "eom-mail-engine.exe"
    shutil.copy2(old_actor, engine)
    database = private / "mail.sqlite3"
    config = private / "config.toml"
    actor = None
    holder = None
    with MailFixture(folder) as mail, reference_provider(private) as (provider, posted, release):
        write_private(
            config,
            (
                'timezone = "UTC"\n'
                f'database_file = "{database.as_posix()}"\n'
                f'model_base_url = "http://127.0.0.1:{mail.model.server_port}/v1"\n'
                'model_name = "cutover-fixture"\nmodel_require_auth = false\n'
                "notifications_enabled = false\nretention_days = 100\n"
            ).encode(),
        )
        # Native private storage and the unmodified signed verifier on both images.
        licence = private / "LocalConnect/entitlement-v1.json"
        from connect_automate.connect_windows import ensure_private_directory

        ensure_private_directory(licence.parent, root=private)
        write_private(licence, (folder / "fixture-entitlement.json").read_bytes())
        old = Engine(engine, config, folder, environment)
        account = old.call(
            "mail.accounts.connect", {"provider": "imap", "connection": mail.connection_payload}
        )["account"]
        vendor = old.call("vendors.create", {"display_name": "Public vendor"})["item"]
        old.call(
            "vendors.addresses.add",
            {"vendor_id": vendor["vendor_id"], "address": "vendor@example.test"},
        )
        mail.append("expired-root", old=True)
        mail.append("expired-reply", old=True, reply_to="expired-root")
        mail.append("retained-root", pdf=True)
        old.call("watcher.check")
        assert scalar(database, "PRAGMA user_version") == 34
        assert scalar(database, "SELECT COUNT(*) FROM messages") == 3
        assert scalar(database, "SELECT COUNT(*) FROM thread_follow WHERE followed=1") == 2
        # A real journal row becomes dispatching before the old authoritative POST.
        source = attachment(database, "retained-root")
        actor = old.start("connect.attachment.invoke", invocation(old, *source))
        try:
            assert posted.wait(30), "Old consumer never reached real provider POST"
            with sqlite3.connect(database) as db:
                row = db.execute("""SELECT j.job_id,d.state,d.submission_possible
                    FROM connect_attachment_jobs j
                    JOIN connect_job_dispatch d USING(job_id)""").fetchone()
            assert row and row[1:] == ("dispatching", 1), row
            old_job_id = row[0]
            assert provider.submission_count(old_job_id) == 1
            result = subprocess.run(
                [str(args.installer.resolve()), "/S", "/UPDATE", "/D=" + str(installation)],
                timeout=240,
                capture_output=True,
                env=powershell_environment(TEMP=str(folder), TMP=str(folder)),
            )
            write_private(folder / "installer.stdout", result.stdout)
            write_private(folder / "installer.stderr", result.stderr)
            assert result.returncode == 0, result.returncode
            actor.wait(timeout=10)
            assert actor.returncode != 0, "Old POST owner survived installer"
            release.set()
            assert (
                hashlib.sha256(engine.read_bytes()).hexdigest()
                == inventory["qualification_engine_sha256"]
            )
            receipt = json.loads(
                (installation / "windows-source-cutover-v1.json").read_text(encoding="utf-8-sig")
            )
            assert receipt["engine_sha256"] == inventory["qualification_engine_sha256"]
            new = Engine(engine, config, folder, environment)
            # Hold an actual native legacy lock outside the stopped installation.
            # The frozen receipt-gated engine must exhaust its real drain deadline.
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    """
import sys
from pathlib import Path
from connect_automate.locking import connect_operation_lock,connect_source_lock_path
with connect_operation_lock(connect_source_lock_path(Path(sys.argv[1]),sys.argv[2]),'busy'):
 print('NATIVE_LOCK_HELD',flush=True)
 sys.stdin.read(1)
""",
                    str(database),
                    source[0],
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            assert holder.stdout.readline().strip() == "NATIVE_LOCK_HELD"
            started = time.monotonic()
            view = new.call("mail.accounts.list")
            elapsed = time.monotonic() - started
            selected = next(a for a in view["accounts"] if a["account_id"] == account["account_id"])
            assert "upgrade pending" in selected["source_lock_notice"]
            assert 85 <= elapsed < 140, elapsed
            assert (
                scalar(
                    database,
                    "SELECT COUNT(*) FROM maintenance_state WHERE key='source_lock_protocol'",
                )
                == 0
            )
            holder.stdin.write("x")
            holder.stdin.flush()
            assert holder.wait(timeout=10) == 0
            holder = None
            selected = next(
                a
                for a in new.call("mail.accounts.list")["accounts"]
                if a["account_id"] == account["account_id"]
            )
            assert selected["source_lock_notice"] is None
            assert (
                scalar(
                    database, "SELECT value FROM maintenance_state WHERE key='source_lock_protocol'"
                )
                == "source-stripe-v1"
            )
            new.call("connect.queue.pump")
            assert (
                scalar(
                    database,
                    "SELECT status FROM connect_attachment_jobs WHERE job_id=?",
                    (old_job_id,),
                )
                == "completed"
            )
            assert provider.submission_count(old_job_id) == 1, "Orphan POST was resubmitted"
            # All subsequent effects are public IPC through the installed new image.
            new.call("settings.update", {"retention_days": 1})
            mail.append("retained-sent", sent=True, reply_to="retained-root")
            mail.append("new-attachment", pdf=True)
            check = new.call("watcher.check")
            assert (
                scalar(database, "SELECT COUNT(*) FROM messages WHERE subject LIKE 'expired-%'")
                == 0
            )
            assert (
                scalar(
                    database,
                    "SELECT COUNT(*) FROM messages "
                    "WHERE subject IN ('retained-root','retained-sent','new-attachment')",
                )
                == 3
            )
            assert scalar(database, "SELECT COUNT(*) FROM message_bodies") == 3
            assert (
                scalar(
                    database,
                    "SELECT COUNT(DISTINCT thread_key) FROM messages "
                    "WHERE subject IN ('retained-root','retained-sent')",
                )
                == 1
            )
            assert (
                scalar(database, "SELECT COUNT(*) FROM messages WHERE analysis_at IS NOT NULL") >= 2
            )
            assert provider.post_attempt_count() == 1
            new.call(
                "connect.attachment.invoke",
                invocation(new, *attachment(database, "new-attachment")),
            )
            assert provider.post_attempt_count() == 2
            assert (
                scalar(
                    database,
                    "SELECT COUNT(*) FROM connect_attachment_jobs WHERE status='completed'",
                )
                == 2
            )
            resumed, recovered_key, pending_receipt, expected_clear = prove_removal_recovery(
                new, database
            )
            # The holder is now released. Only the ordinary check resumes physical
            # completion; initialization/status above must never have done so.
            resumed.call("watcher.check")
            frozen = resumed.call(
                "inbox.removal.status", {"request_id": recovered_key, "kind": "clear"}
            )
            assert frozen["outcome"] == "completed", frozen
            assert (frozen["requested"], frozen["completed"], frozen["pending"]) == (
                expected_clear, expected_clear, 0
            ), frozen
            assert frozen["request_id"] == recovered_key
            assert frozen["intent_ids"], frozen
            assert scalar(database, "SELECT COUNT(*) FROM messages") == 0
            assert scalar(database, "SELECT COUNT(*) FROM message_bodies") == 0
            assert scalar(database, "SELECT COUNT(*) FROM message_body_unavailable") == 0
            empty_key = resumed.call("inbox.removal.prepare", {"kind": "clear"})
            empty = resumed.call("inbox.clear", empty_key)
            assert (empty["requested"], empty["completed"], empty["pending"]) == (0, 0, 0)
            assert empty["intent_ids"] == [] and empty["outcome"] == "completed"
            mail.append("after-frozen-clear")
            resumed.call("watcher.check")
            assert scalar(
                database, "SELECT COUNT(*) FROM messages WHERE subject=?", ("after-frozen-clear",)
            ) == 1
            assert scalar(database, "SELECT COUNT(*) FROM messages") == 1
            assert resumed.call("inbox.clear", {"request_id": recovered_key}) == {
                **frozen, "deleted": expected_clear
            }
            assert resumed.call("inbox.clear", empty_key) == empty
            assert scalar(
                database, "SELECT COUNT(*) FROM messages WHERE subject=?", ("after-frozen-clear",)
            ) == 1, "A retained request selected a later arrival"
            assert resumed.call(
                "inbox.removal.status", {"request_id": recovered_key}
            ) == frozen
            assert provider.post_attempt_count() == 2, "Receipt retry submitted source work"
            new.call("health.get")
            result = {
                "result": "PASS",
                "installed_engine_sha256": receipt["engine_sha256"],
                "greenmail_sha256": GREENMAIL_SHA256,
                "java_runtime": (folder / "java-version.txt").read_text(),
                "predecessor_schema": 34,
                "source_drain_seconds": elapsed,
                "orphan_posts": provider.submission_count(old_job_id),
                "total_posts": provider.post_attempt_count(),
                "poll": check,
                "removal_receipts": {
                    "held_restart": pending_receipt, "populated": frozen, "empty": empty,
                },
                "proof": [
                    "TLS IMAP Inbox/Sent",
                    "local analysis",
                    "body capture",
                    "whole followed purge",
                    "orphan GET without second POST",
                    "new installed Connect POST",
                    "receipt-gated timeout and retry",
                    "opaque removal key persisted before mutation",
                    "removal status after discarded response and client restart",
                    "mapped source held across deferred acceptance and status-only restart",
                    "held physical storage unchanged and logical targets invisible",
                    "ordinary check completes after source release",
                    "populated and zero-target retries exclude later arrivals",
                ],
                "build_inventory": inventory,
            }
            write_private(folder / "qualification.json", json.dumps(result, indent=2).encode())
            print(json.dumps(result, indent=2))
        finally:
            if actor is not None and actor.poll() is None:
                actor.kill()
                actor.wait(10)
            if holder is not None and holder.poll() is None:
                holder.kill()
                holder.wait(10)


def main():
    parser = argparse.ArgumentParser()
    for name in ("installer", "engine", "predecessor", "output"):
        parser.add_argument(name, type=Path)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
