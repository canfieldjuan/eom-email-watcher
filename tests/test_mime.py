import base64

from eom_email_watcher.mime import AttachmentDescriptor, extract_body, html_to_text


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


def test_html_text_flushes_unfinished_reference_and_tag_fragments() -> None:
    extracted = html_to_text("AT&T and Total <")

    assert "AT&T" in extracted
    assert extracted.endswith("<")


def test_html_text_preserves_block_boundaries_around_reply_history() -> None:
    extracted = html_to_text(
        "<div>Please send copies.</div>"
        "<div>-----Original Message-----</div>"
        "<div>Invoice 2042 is due September 5, 2026.</div>"
    )

    assert extracted.splitlines() == [
        "Please send copies.",
        "-----Original Message-----",
        "Invoice 2042 is due September 5, 2026.",
    ]


def test_html_text_keeps_inline_text_readable_without_inventing_a_line_break() -> None:
    assert html_to_text("<p>Hello <strong>there</strong>.</p>").strip() == "Hello  there ."


def test_explicit_empty_root_part_id_is_a_valid_attachment_identity() -> None:
    body, attachment_names, attachments = extract_body(
        {
            "mimeType": "application/pdf",
            "partId": "",
            "filename": "root.pdf",
            "body": {"attachmentId": "root-attachment", "size": 42},
        },
        20_000,
    )

    assert body == ""
    assert attachment_names == ("root.pdf",)
    assert attachments == (
        AttachmentDescriptor(
            part_id="",
            attachment_id="root-attachment",
            filename="root.pdf",
            media_type="application/pdf",
            byte_size=42,
            position=0,
        ),
    )


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
                            "partId": None,
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
