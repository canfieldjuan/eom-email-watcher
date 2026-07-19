from eom_email_watcher.gmail import parse_metadata


def test_parse_metadata_uses_internal_date_and_normalized_from() -> None:
    parsed = parse_metadata(
        {
            "id": "m1",
            "threadId": "t1",
            "internalDate": "1784383200000",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Person <TRUSTED@Example.com>"},
                    {"name": "Subject", "value": "Test"},
                ]
            },
        }
    )
    assert parsed.sender == "trusted@example.com"
    assert parsed.sender_name == "Person"
    assert parsed.subject == "Test"
    assert parsed.labels == frozenset({"INBOX", "UNREAD"})
