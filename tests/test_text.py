from __future__ import annotations

from eom_email_watcher.text import utf8_size, within_utf8_bytes


def test_utf8_size_counts_bytes_and_refuses_unencodable_text() -> None:
    assert utf8_size("abc") == 3
    assert utf8_size("\u00e9") == 2
    assert utf8_size("bad\udcff") is None


def test_within_utf8_bytes_fails_closed() -> None:
    assert within_utf8_bytes("abc", 3)
    assert not within_utf8_bytes("abcd", 3)
    assert not within_utf8_bytes("\udcff", 1_000_000)
