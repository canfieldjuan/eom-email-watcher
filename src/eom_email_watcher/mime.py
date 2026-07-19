from __future__ import annotations

import base64
import html
from html.parser import HTMLParser
from typing import Any


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _decode(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def extract_body(payload: dict[str, Any], limit: int) -> tuple[str, tuple[str, ...]]:
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        filename = str(part.get("filename", "")).strip()
        body = part.get("body") or {}
        if filename:
            attachments.append(filename)
            return
        mime_type = str(part.get("mimeType", "")).casefold()
        data = body.get("data")
        if isinstance(data, str):
            decoded = _decode(data)
            if mime_type == "text/plain":
                plain.append(decoded)
            elif mime_type == "text/html":
                parser = _TextExtractor()
                parser.feed(decoded)
                rich.append(html.unescape(" ".join(parser.parts)))
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    selected = "\n\n".join(plain) if plain else "\n\n".join(rich)
    normalized = "\n".join(line.strip() for line in selected.splitlines() if line.strip())
    return normalized[:limit], tuple(dict.fromkeys(attachments))
