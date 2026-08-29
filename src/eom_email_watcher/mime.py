from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@dataclass(frozen=True)
class AttachmentDescriptor:
    part_id: str
    attachment_id: str | None
    filename: str
    media_type: str
    byte_size: int
    position: int


def _decode(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def extract_body(
    payload: dict[str, Any], limit: int
) -> tuple[str, tuple[str, ...], tuple[AttachmentDescriptor, ...]]:
    plain: list[str] = []
    rich: list[str] = []
    attachment_names: list[str] = []
    attachments_by_part_id: dict[str, AttachmentDescriptor] = {}

    def walk(part: dict[str, Any]) -> None:
        filename = str(part.get("filename", "")).strip()
        body = part.get("body") or {}
        if filename:
            attachment_names.append(filename)
            raw_part_id = part.get("partId")
            part_id = raw_part_id.strip() if isinstance(raw_part_id, str) else None
            if part_id is not None and part_id not in attachments_by_part_id:
                raw_attachment_id = body.get("attachmentId")
                attachment_id = (
                    raw_attachment_id.strip()
                    if isinstance(raw_attachment_id, str) and raw_attachment_id.strip()
                    else None
                )
                raw_size = body.get("size", 0)
                byte_size = (
                    raw_size
                    if isinstance(raw_size, int)
                    and not isinstance(raw_size, bool)
                    and raw_size >= 0
                    else 0
                )
                attachments_by_part_id[part_id] = AttachmentDescriptor(
                    part_id=part_id,
                    attachment_id=attachment_id,
                    filename=filename,
                    media_type=str(part.get("mimeType", "")).casefold(),
                    byte_size=byte_size,
                    position=len(attachments_by_part_id),
                )
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
    names = tuple(dict.fromkeys(attachment_names))
    descriptors = tuple(attachments_by_part_id.values())
    return normalized[:limit], names, descriptors
