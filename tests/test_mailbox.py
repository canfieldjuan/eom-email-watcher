from __future__ import annotations

from eom_email_watcher.mailbox import MAX_RECIPIENT_ADDRESS_BYTES, recipient_addresses


def test_recipient_addresses_drop_malformed_entries_and_keep_the_rest() -> None:
    header = (
        "Billing <Billing@Vendor.com>, bad\udcff@example.com, nobody, "
        + "x" * MAX_RECIPIENT_ADDRESS_BYTES
        + "@example.com, billing@vendor.com, Sales <sales@vendor.com>"
    )

    assert recipient_addresses(header) == ("billing@vendor.com", "sales@vendor.com")
    assert recipient_addresses("") == ()
    assert recipient_addresses(None) == ()
