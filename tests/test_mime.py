import base64

from eom_email_watcher.mime import AttachmentDescriptor, extract_body


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_prefers_plain_text_and_lists_attachment_names() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": encoded("Hello plain")}},
            {"mimeType": "text/html", "body": {"data": encoded("<b>Hello html</b>")}},
            {
                "mimeType": "application/pdf",
                "partId": "2",
                "filename": "invoice.pdf",
                "body": {"attachmentId": "x", "size": 1234},
            },
        ],
    }
    body, attachment_names, attachments = extract_body(payload, 20_000)
    assert body == "Hello plain"
    assert attachment_names == ("invoice.pdf",)
    assert attachments == (
        AttachmentDescriptor(
            part_id="2",
            attachment_id="x",
            filename="invoice.pdf",
            media_type="application/pdf",
            byte_size=1234,
            position=0,
        ),
    )


def test_html_fallback_is_text_only_and_truncated() -> None:
    body, attachment_names, attachments = extract_body(
        {"mimeType": "text/html", "body": {"data": encoded("<p>Hello &amp; goodbye</p>")}},
        8,
    )
    assert body == "Hello & "
    assert attachment_names == ()
    assert attachments == ()


def test_nested_attachment_names_survive_when_descriptor_identity_is_missing() -> None:
    body, attachment_names, attachments = extract_body(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "application/pdf",
                            "filename": "invoice.pdf",
                            "body": {"size": True},
                        },
                        {
                            "mimeType": "application/pdf",
                            "partId": "3",
                            "filename": "invoice.pdf",
                            "body": {"data": encoded("pdf"), "size": -1},
                        },
                    ],
                },
            ],
        },
        20_000,
    )

    assert body == ""
    assert attachment_names == ("invoice.pdf",)
    assert attachments == (
        AttachmentDescriptor(
            part_id="3",
            attachment_id=None,
            filename="invoice.pdf",
            media_type="application/pdf",
            byte_size=0,
            position=0,
        ),
    )


def test_duplicate_part_ids_keep_first_descriptor_and_stable_positions() -> None:
    _, attachment_names, attachments = extract_body(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "application/pdf",
                    "partId": "10",
                    "filename": "first.pdf",
                    "body": {"attachmentId": "first", "size": 10},
                },
                {
                    "mimeType": "application/pdf",
                    "partId": "10",
                    "filename": "duplicate.pdf",
                    "body": {"attachmentId": "duplicate", "size": 20},
                },
                {
                    "mimeType": "text/plain",
                    "partId": "2",
                    "filename": "notes.txt",
                    "body": {"attachmentId": "notes", "size": 30},
                },
            ],
        },
        20_000,
    )

    assert attachment_names == ("first.pdf", "duplicate.pdf", "notes.txt")
    assert [(item.part_id, item.filename, item.position) for item in attachments] == [
        ("10", "first.pdf", 0),
        ("2", "notes.txt", 1),
    ]
