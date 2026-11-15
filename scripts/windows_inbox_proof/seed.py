"""Prepare the isolated Windows Inbox smoke database and verify its data through engine requests."""
import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

from eom_email_watcher.config import exact_sender_selector_id
from eom_email_watcher.db import AdmissionProvenance, Store
from eom_email_watcher import engine_api, private_storage
from eom_email_watcher.mime import AttachmentDescriptor


parser = argparse.ArgumentParser()
parser.add_argument("profile", type=Path)
args = parser.parse_args()
profile = args.profile.resolve()
profile.mkdir(mode=0o700)
store = Store(profile / "inbox.sqlite3")
store.initialize()
account = "inbox-proof"
identity = "c" * 64
owner = "owner@example.test"
sender = "supplier@example.test"
store.register_mail_account("gmail", account, display_name="Synthetic Inbox")
store.reconcile_mailbox_identity("gmail", account, identity)
store.update_mail_account_identity("gmail", account, display_name="Synthetic Inbox", address=owner)
vendor = store.create_vendor("Synthetic Supplier")
store.add_vendor_address(vendor, sender)
now = datetime.now(UTC)
cases = (
    ("inbound-proof", "synthetic-chain", sender, owner, "INBOX qualification quote", "inbox", "quote-inbound.pdf"),
    ("sent-proof", "synthetic-chain", owner, sender, "SENT qualification reply", "sent", "reply-sent.pdf"),
    ("unrelated-proof", "other-chain", sender, owner, "Unrelated receipt", "inbox", None),
)
for index, (message_id, thread, author, recipient, subject, folder, attachment) in enumerate(cases):
    stamp = now - timedelta(minutes=len(cases) - index)
    store.add_message(
        provider="gmail", account_id=account, mailbox_identity_key=identity,
        message_id=message_id, provider_message_id=message_id, thread_id=thread,
        sender=author, sender_name=None, to=(recipient,), subject=subject,
        received_at=stamp.isoformat(), observed_at=stamp, locations=frozenset({folder}),
        rfc_message_id=f"{message_id}@example.test",
        admission=AdmissionProvenance(
            "exact_sender", exact_sender_selector_id(author), None, identity, stamp.isoformat()
        ),
    )
    if attachment:
        store.replace_attachments(message_id, (
            AttachmentDescriptor("1", message_id + "-attachment", attachment, "application/pdf", 11, 0),
        ))
        with store.connection() as db:
            db.execute("UPDATE messages SET summary=? WHERE message_id=?", (f"Analysis for {message_id}", message_id))

config = profile / "config.toml"
config_text = (
    'timezone = "UTC"\nmodel_name = "synthetic-inbox-proof"\n'
    'model_base_url = "http://127.0.0.1:9/v1"\nmodel_require_auth = false\n'
    'notifications_enabled = false\npoll_interval_minutes = 1440\n'
    f'database_file = {json.dumps(str(store.path))}\n'
    f'\n[[senders]]\nemail = "{sender}"\n\n[[senders]]\nemail = "{owner}"\n'
)
private_storage.write_text(config, config_text)


def request(operation, **payload):
    response = engine_api._response({
        "protocol": 1, "operation": operation, "config_path": str(config), "payload": payload,
    })
    if not response["ok"]:
        raise RuntimeError(response)
    return response["data"]


all_rows = request("inbox.conversations.query", limit=25)
filtered = request("inbox.conversations.query", keyword="qualification", limit=25)
assert all_rows["totals"]["conversations"] == 2, all_rows
(row,) = filtered["items"]
assert row["thread_ref"]["thread_key"] == "synthetic-chain"
anchor = row["latest"]["message_ref"]
resolved = request("threads.resolve", message_ref=anchor)["context"]
assert resolved["vendor"]["vendor_id"] == vendor, resolved
page = request(
    "threads.messages",
    vendor_id=resolved["vendor"]["vendor_id"],
    thread_ref=resolved["thread"]["thread_ref"],
    inbox_message=anchor,
    include_inbox_actions=True,
)
records = []
for item in page["items"]:
    action = item["inbox_action"]
    records.append({
        "message_id": item["message_id"], "subject": item["subject"], "direction": item["direction"],
        "action_message_id": action["message_id"],
        "attachments": [attachment["filename"] for attachment in action["attachments"]],
    })
assert [entry["message_id"] for entry in records] == ["sent-proof", "inbound-proof"], records
assert all(row["message_id"] == row["action_message_id"] for row in records), records
assert [row["attachments"] for row in records] == [["reply-sent.pdf"], ["quote-inbound.pdf"]], records
assert [row["direction"] for row in records] == ["outbound", "inbound"], records
with sqlite3.connect(store.path) as connection:
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

print(json.dumps({"unfiltered": all_rows["totals"], "filtered": filtered["totals"], "records": records}, indent=2))
