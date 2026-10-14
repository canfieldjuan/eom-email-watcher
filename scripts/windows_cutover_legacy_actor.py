"""Frozen schema-34 actor. Built against the pinned predecessor, never shipped."""

import argparse
import os
import sys
import time
import urllib.request
from pathlib import Path

from connect_automate.locking import connect_operation_lock, connect_source_lock_path

from eom_email_watcher.db import SCHEMA_VERSION, Store
from eom_email_watcher.deployment import main as engine_main


def main():
    if len(sys.argv) == 1:
        # Exercise the pinned predecessor's production IPC/dispatch owners.
        engine_main()
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("ready", type=Path)
    parser.add_argument("trigger", type=Path)
    parser.add_argument("endpoint")
    parser.add_argument("mode", choices=("seed", "held", "idle"))
    args = parser.parse_args()
    assert SCHEMA_VERSION == 34, SCHEMA_VERSION
    store = Store(args.database)
    store.initialize()
    if store.mail_account("gmail", "public-fixture") is None:
        store.register_mail_account("gmail", "public-fixture", display_name="Public fixture")
    with store.connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS cutover_probe_outcomes(value TEXT)")
    if args.mode == "seed":
        return
    # Both modes open the old database first. Idle creates its legacy lock only
    # after the trigger, covering an owner absent from the initial drain list.
    if args.mode == "idle":
        args.ready.with_suffix(".pid").write_text(str(os.getpid()), encoding="ascii")
        args.ready.write_text("OLD_DATABASE_OPEN", encoding="ascii")
        deadline = time.monotonic() + 180
        while not args.trigger.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Fixture trigger did not arrive")
            time.sleep(0.02)
    with connect_operation_lock(connect_source_lock_path(store.path, "public-source"), "busy"):
        with store.connection() as db:
            db.execute("SELECT 1").fetchone()
        args.ready.write_text("OLD_POST_STARTED", encoding="ascii")
        urllib.request.urlopen(
            urllib.request.Request(args.endpoint, data=b"public-fixture", method="POST"),
            timeout=150,
        ).read()
        with store.connection() as db:
            db.execute("INSERT INTO cutover_probe_outcomes VALUES('old-completion')")


if __name__ == "__main__":
    main()
