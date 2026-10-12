from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

MAX_STORED_BODY_CHARS = 200_000
MAX_STORED_BODY_INPUT_CHARS = 50 * 1024 * 1024


class StoredBodyInvalid(ValueError):
    """A storage source cannot be normalized safely or completely."""


_HTML_LINE_BREAK_TAGS = frozenset(
    {
        "article",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in _HTML_LINE_BREAK_TAGS:
            self._line_break()

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in _HTML_LINE_BREAK_TAGS:
            self._line_break()

    def _line_break(self) -> None:
        if self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    extracted = " ".join(parser.parts)
    extracted = extracted.replace(" \n ", "\n").replace(" \n", "\n").replace("\n ", "\n")
    return html.unescape(extracted)


@dataclass(frozen=True)
class AttachmentDescriptor:
    part_id: str
    attachment_id: str | None
    filename: str
    media_type: str
    byte_size: int
    position: int
    byte_size_known: bool = True
    # The source identity whose content listed this attachment (contract D-identity):
    # provider attachment ids belong to one copy, so its bytes are read from it.
    source_provider_message_id: str | None = None


def _decode(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def bounded_body_text(selected: str, limit: int) -> tuple[str, int]:
    """Return the normalized body cut to ``limit`` characters and its pre-cut length."""
    normalized = "\n".join(line.strip() for line in selected.splitlines() if line.strip())
    return normalized[:limit], len(normalized)


class _StoredTextExtractor(HTMLParser):
    """Render quote boundaries with constant-time ancestry queries."""

    def __init__(self) -> None:
        super().__init__()
        self.stack = []
        self.positions = {}
        self.quote_depth = self.hidden_depth = self.pre_depth = 0
        self.parts: list[str] = []
        self.lines: list[str] = []

    def _line(self) -> None:
        text = "".join(self.parts)
        lines = text.splitlines() if self.pre_depth else [" ".join(text.split())]
        for line in lines:
            if line.strip():
                self.lines.append(("> " if self.quote_depth else "") + line.strip())
        self.parts.clear()

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in _HTML_LINE_BREAK_TAGS:
            self._line()
        attributes = dict(attrs)
        quoted = tag == "blockquote" or (
            tag == "div"
            and (
                "gmail_quote" in (attributes.get("class") or "").split()
                or attributes.get("id") == "divRplyFwdMsg"
            )
        )
        if tag not in {"br", "hr", "img", "input", "meta", "link", "wbr"}:
            hidden, pre = tag in {"script", "style"}, tag == "pre"
            self.positions.setdefault(tag, []).append(len(self.stack))
            self.stack.append((tag, quoted, hidden, pre))
            self.quote_depth += quoted
            self.hidden_depth += hidden
            self.pre_depth += pre

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in _HTML_LINE_BREAK_TAGS:
            self._line()
        positions = self.positions.get(tag)
        if positions:
            target = positions[-1]
            while len(self.stack) > target:
                name, quoted, hidden, pre = self.stack.pop()
                self.positions[name].pop()
                self.quote_depth -= quoted
                self.hidden_depth -= hidden
                self.pre_depth -= pre

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def _validate_stored_input(selected: str) -> None:
    if len(selected) > MAX_STORED_BODY_INPUT_CHARS:
        raise StoredBodyInvalid("Message body exceeds the safe normalization input limit")


def stored_html_to_text(selected: str) -> str:
    _validate_stored_input(selected)
    parser = _StoredTextExtractor()
    parser.feed(selected)
    parser.close()
    parser._line()
    return "\n".join(parser.lines)


def stored_body_text(selected: str, *, is_html: bool) -> tuple[str, int]:
    _validate_stored_input(selected)
    if is_html:
        selected = stored_html_to_text(selected)
    return bounded_body_text(selected, MAX_STORED_BODY_CHARS)


def select_stored_alternative(alternatives):
    """Only usable rendered text can win an HTML preference."""
    usable = [[(text, rich) for text, rich in parts if text.strip()] for parts in alternatives]
    usable = [parts for parts in usable if parts]
    return next(
        (parts for parts in usable if any(rich for _, rich in parts)),
        usable[0] if usable else [],
    )


def extract_stored_body(payload: dict[str, Any], *, load_text=None) -> tuple[str, int]:
    total = 0

    def walk(part):
        nonlocal total
        if part.get("filename"):
            return []
        kind = str(part.get("mimeType", "")).casefold()
        children = [walk(child) for child in part.get("parts") or [] if isinstance(child, dict)]
        if kind == "multipart/alternative":
            return select_stored_alternative(children)
        if children:
            return [body for child in children for body in child]
        if kind not in {"text/plain", "text/html"}:
            return []
        body = part.get("body") or {}
        data = body.get("data")
        if not isinstance(data, str):
            if not body.get("attachmentId"):
                return []
            if load_text is None:
                raise StoredBodyInvalid("Message text part was not retrieved")
            data = load_text(part)
        text = _decode(data)
        total += len(text)
        if total > MAX_STORED_BODY_INPUT_CHARS:
            raise StoredBodyInvalid("Message body exceeds the safe normalization input limit")
        rich = kind == "text/html"
        return [(stored_html_to_text(text) if rich else text, rich)]

    return stored_body_text("\n".join(text for text, _ in walk(payload)), is_html=False)


def body_was_truncated(analyzed_chars: int | None, source_chars: int | None) -> bool | None:
    """Return whether an analysis read a strict prefix of the body; ``None`` when unknown."""
    if analyzed_chars is None or source_chars is None:
        return None
    return source_chars > analyzed_chars


def extract_body(
    payload: dict[str, Any], limit: int
) -> tuple[str, tuple[str, ...], tuple[AttachmentDescriptor, ...], int]:
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
                raw_size = body.get("size")
                byte_size_known = (
                    isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0
                )
                byte_size = raw_size if byte_size_known else 0
                attachments_by_part_id[part_id] = AttachmentDescriptor(
                    part_id=part_id,
                    attachment_id=attachment_id,
                    filename=filename,
                    media_type=str(part.get("mimeType", "")).casefold(),
                    byte_size=byte_size,
                    position=len(attachments_by_part_id),
                    byte_size_known=byte_size_known,
                )
            return
        mime_type = str(part.get("mimeType", "")).casefold()
        data = body.get("data")
        if isinstance(data, str):
            decoded = _decode(data)
            if mime_type == "text/plain":
                plain.append(decoded)
            elif mime_type == "text/html":
                rich.append(html_to_text(decoded))
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    selected = "\n\n".join(plain) if plain else "\n\n".join(rich)
    body, source_chars = bounded_body_text(selected, limit)
    names = tuple(dict.fromkeys(attachment_names))
    descriptors = tuple(attachments_by_part_id.values())
    return body, names, descriptors, source_chars
