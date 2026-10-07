"""Byte-size checks on text that arrives from outside the program.

One owner: every bound on a request field, a file, or a provider value compares
through here, so text that cannot be encoded (an unpaired surrogate smuggled in
through JSON) fails the bound instead of raising UnicodeEncodeError somewhere
the caller did not expect.
"""

from __future__ import annotations


def utf8_size(value: str) -> int | None:
    """The UTF-8 size of value, or None when it cannot be encoded."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def within_utf8_bytes(value: str, maximum: int) -> bool:
    """True when value encodes to at most maximum UTF-8 bytes."""
    size = utf8_size(value)
    return size is not None and size <= maximum
