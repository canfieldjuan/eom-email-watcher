"""Seam check for docs/THREAD_VIEW_CONTRACT.md.

Every rule in the thread-view contract is stated once, in its definition.
These tests fail when a rule's key phrase is restated in another normative
section, or when a reference to a definition no longer resolves. A fix then
has to change the one definition instead of adding a divergent copy.
"""

import re
from pathlib import Path

import pytest

CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "THREAD_VIEW_CONTRACT.md"

# Background, decision records, history, and test scenarios may quote rules.
NON_NORMATIVE_SECTIONS = {
    "Why this arc exists",
    "Operator decisions already made (2026-10-05)",
    "Operator decisions (accepted 2026-10-05, as recommended)",
    "Acceptance evidence",
    "Revision log",
}

# Key phrases of each rule, owned by exactly one definition.
OWNED_PHRASES = {
    "D-vendor": [
        r"public-provider list",
        r"at most one vendor",
        r"`vendor_of\(address\)`\*\* returns",
    ],
    "D-attribution": [r"stored header order"],
    "D-scope": [
        r"retention cutoff\*\* is",
        r"`INBOX` or `SENT` label",
        r"`\\Sent`",
        r"secure_delete",
    ],
    "D-capture": [
        r"`exact_sender`, `vendor_domain`, `sent_to_vendor`, `thread_follow`, `gmail_user_label`",
        r"leaves no trace",
    ],
    "D-follow": [r"becomes followed"],
    "D-identity": [r"UIDVALIDITY:UID", r"logical identity", r"earliest-created"],
    "D-reconcile": [r"coverage record", r"synced_through"],
    "D-body": [r"Body not stored", r"bounded_body_text"],
    "D-ops": [r"Connect required to update", r"capability_exchange"],
    "D-claims": [r"compare manually", r"authored text", r"`comparable\(a, b\)`"],
}


def _sections() -> list[tuple[str, str]]:
    """Return (heading, body) for every level-2 and level-3 section."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{2,3} (.+)$", line)
        if match:
            sections.append((match.group(1).strip(), []))
        else:
            sections[-1][1].append(line)
    return [(heading, "\n".join(body)) for heading, body in sections]


def _normative(heading: str, parent: str) -> bool:
    return heading not in NON_NORMATIVE_SECTIONS and parent not in NON_NORMATIVE_SECTIONS


def _definition_of(heading: str) -> str | None:
    match = re.match(r"^(D-[a-z]+):", heading)
    return match.group(1) if match else None


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-")


def _located() -> list[tuple[str, str, str]]:
    """Return (heading, owning definition or "", body) with level-2 parents tracked."""
    located = []
    parent = ""
    text = CONTRACT.read_text(encoding="utf-8")
    headings = re.finditer(r"^(#{2,3}) (.+)$", text, re.M)
    level = {m.group(2).strip(): len(m.group(1)) for m in headings}
    for heading, body in _sections():
        if level.get(heading) == 2:
            parent = heading
        if _normative(heading, parent):
            located.append((heading, _definition_of(heading) or "", body))
    return located


def test_every_definition_is_present_once() -> None:
    owners = [_definition_of(heading) for heading, _body in _sections()]
    for owner in OWNED_PHRASES:
        assert owners.count(owner) == 1, f"{owner} must be defined exactly once"


@pytest.mark.parametrize(
    ("owner", "pattern"),
    [(owner, pattern) for owner, patterns in OWNED_PHRASES.items() for pattern in patterns],
)
def test_owned_phrase_appears_only_in_its_definition(owner: str, pattern: str) -> None:
    hits = [
        heading
        for heading, definition, body in _located()
        if re.search(pattern, body)
    ]
    assert any(_definition_of(heading) == owner for heading in hits), (
        f"{pattern!r} is missing from {owner}"
    )
    strays = [heading for heading in hits if _definition_of(heading) != owner]
    assert not strays, f"{pattern!r} is owned by {owner} but restated in: {strays}"


def test_every_definition_reference_resolves() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    anchors = {_slug(m.group(1).strip()) for m in re.finditer(r"^#{1,3} (.+)$", text, re.M)}
    references = set(re.findall(r"\]\(#([a-z0-9_-]+)\)", text))
    assert references, "the contract must reference its definitions"
    missing = sorted(references - anchors)
    assert not missing, f"references to missing headings: {missing}"
