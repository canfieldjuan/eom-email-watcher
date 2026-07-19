import base64

from eom_email_watcher.mime import extract_body


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
                "filename": "invoice.pdf",
                "body": {"attachmentId": "x"},
            },
        ],
    }
    body, attachments = extract_body(payload, 20_000)
    assert body == "Hello plain"
    assert attachments == ("invoice.pdf",)


def test_html_fallback_is_text_only_and_truncated() -> None:
    body, attachments = extract_body(
        {"mimeType": "text/html", "body": {"data": encoded("<p>Hello &amp; goodbye</p>")}},
        8,
    )
    assert body == "Hello & "
    assert attachments == ()
