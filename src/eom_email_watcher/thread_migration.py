"""The schema-33 messages rebuild and private pre-upgrade backup."""

import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

ADMISSION_KINDS = (
    "exact_sender",
    "vendor_address",
    "vendor_domain",
    "sent_to_vendor",
    "thread_follow",
    "gmail_user_label",
)


def write_backup(path: Path, db: sqlite3.Connection, *, target_schema: int = 33) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(f"{path.name}.pre-v{target_schema}.{stamp}.{uuid.uuid4().hex}.bak")
    # VACUUM INTO accepts an empty file. Exclusive creation establishes privacy
    # before SQLite writes any data and cannot overwrite an existing backup.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        db.execute("VACUUM INTO ?", (str(target),))
    except Exception:
        target.unlink()
        raise
    return target


def cleanup_backups(path: Path, *, success: bool, target_schema: int = 33) -> None:
    prefix = f"{path.name}.pre-v{target_schema}."
    backups = sorted(
        entry
        for entry in path.parent.iterdir()
        if entry.name.startswith(prefix) and entry.name.endswith(".bak") and entry.is_file()
    )
    for backup in backups if success else backups[:-1]:
        backup.unlink()


def rebuild_messages(db: sqlite3.Connection) -> None:
    """Preserve all columns, explicit indexes, triggers and views atomically."""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    old_sql = str(row[0])
    old_check = "admission_kind IN ('exact_sender', 'gmail_user_label')"
    new_check = "admission_kind IN (" + ", ".join(repr(kind) for kind in ADMISSION_KINDS) + ")"
    # Repeated opens never repeat the migration. A future schema cannot silently
    # bypass an unrecognized source CHECK.
    if old_check not in old_sql and new_check not in old_sql:
        raise RuntimeError("messages admission CHECK is not the supported source schema")
    new_sql = re.sub(
        r'CREATE TABLE (?:"messages"|messages)(?=\s|\()',
        "CREATE TABLE messages_v33",
        old_sql.replace(old_check, new_check),
        count=1,
    )
    if new_sql == old_sql:
        raise RuntimeError("messages table declaration is not the supported source schema")
    schema = db.execute("""SELECT type, name, sql FROM sqlite_master
        WHERE sql IS NOT NULL AND (type IN ('trigger', 'view')
            OR (type='index' AND tbl_name='messages'))
        ORDER BY CASE type WHEN 'index' THEN 0 WHEN 'view' THEN 1 ELSE 2 END, rowid""").fetchall()
    columns = [str(row["name"]) for row in db.execute("PRAGMA table_info(messages)")]
    names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    before = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    for entry in schema:
        if entry["type"] in ("trigger", "view"):
            name = str(entry["name"]).replace('"', '""')
            db.execute(f'DROP {entry["type"]} "{name}"')
    db.execute(new_sql)
    db.execute(f"INSERT INTO messages_v33({names}) SELECT {names} FROM messages")
    db.execute("DROP TABLE messages")
    db.execute("ALTER TABLE messages_v33 RENAME TO messages")
    for entry in schema:
        db.execute(str(entry["sql"]))
    if db.execute("SELECT count(*) FROM messages").fetchone()[0] != before:
        raise RuntimeError("messages rebuild changed the row count")
    check_database(db)


def check_database(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("schema-33 foreign key check failed")
    if [str(row[0]) for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
        raise RuntimeError("schema-33 integrity check failed")
