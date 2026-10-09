"""Seam check for docs/THREAD_VIEW_CONTRACT.md.

Every rule in the thread-view contract is stated once, in its definition.
These tests fail when a definition's own text is copied verbatim into another
normative section, when a listed key phrase of a rule appears outside its
definition, or when a reference to a definition no longer resolves. They
cannot detect a paraphrase; the contract says so.
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

# Shortest definition line, in words, that the verbatim-copy check compares.
MIN_COPY_WORDS = 8

# Key phrases of each rule, owned by exactly one definition. They catch the
# short restatements that the verbatim-copy check is too coarse to see.
OWNED_PHRASES = {
    "D-vendor": [
        r"public-provider list",
        r"at most one vendor",
        r"`vendor_of\(address\)`\*\* returns",
        r"Suggestion candidates",
        r"Add <address> to <vendor>",
    ],
    "D-attribution": [r"stored header order", r"listed as linked"],
    "D-scope": [
        r"retention cutoff\*\* is",
        r"`INBOX` or `SENT` label",
        r"`\\Sent`",
        r"secure_delete",
        r"Sent mail unavailable",
    ],
    "D-capture": [
        r"`exact_sender`, `vendor_address`, `vendor_domain`, `sent_to_vendor`, `thread_follow`,"
        r" `gmail_user_label`",
        r"leaves no trace",
    ],
    "D-follow": [r"is followed exactly while", r"earliest-received such message"],
    "D-identity": [
        r"UIDVALIDITY:UID",
        r"logical identity",
        r"is their source identity in byte order",
        r"smallest member under the canonical order",
        r"depend on a key's value",
    ],
    "D-derived": [
        r"\*\*Its inputs\*\* are exactly",
        r"by any path",
        r"That list is illustrative",
    ],
    "D-reconcile": [
        r"coverage record",
        r"synced_through",
        r"retries that unit with backoff",
        r"no change has to remember to trigger it",
        r"coverage generation\*\* is a counter",
        r"exists only while its thread is followed",
    ],
    "D-body": [r"Body not stored", r"Partial body", r"summary-only storage"],
    "D-ops": [
        r"Connect required to update",
        r"capability_exchange`? \(decision D3",
        r"`vendors\.[a-z.]+`",
        r"`watchlist\.remove`",
        r"Also stop watching",
        r"no operation changes it and the database",
    ],
    "D-claims": [
        r"compare manually",
        r"authored text",
        r"`comparable\(a, b\)`",
        r"Claims unavailable",
        r"later than promised",
        r"reference profile\*\* is computed in code",
        r"never pair through a third message",
        r"the anchor condition for where the two claims sit",
        r"selected record is its valid `invoice.extract` record",
        r"the vendor's own words say this PDF is their invoice",
        r"\*\*record pairing\*\*, instead of both",
    ],
}


def _sections() -> list[tuple[str, int, str]]:
    """Return (heading, level, body) for every level-2 and level-3 section."""
    sections: list[tuple[str, int, list[str]]] = [("", 0, [])]
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(#{2,3}) (.+)$", line)
        if match:
            sections.append((match.group(2).strip(), len(match.group(1)), []))
        else:
            sections[-1][2].append(line)
    return [(heading, level, "\n".join(body)) for heading, level, body in sections]


def _definition_of(heading: str) -> str | None:
    match = re.match(r"^(D-[a-z]+):", heading)
    return match.group(1) if match else None


def _normative() -> list[tuple[str, str]]:
    """Return (heading, body) of normative sections, tracking level-2 parents."""
    located = []
    parent = ""
    for heading, level, body in _sections():
        if level == 2:
            parent = heading
        if heading not in NON_NORMATIVE_SECTIONS and parent not in NON_NORMATIVE_SECTIONS:
            located.append((heading, body))
    return located


def _plain(text: str) -> str:
    """Drop list markers, emphasis, and links' targets; collapse whitespace."""
    text = re.sub(r"\]\(#[^)]*\)", "]", text)
    text = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", text, flags=re.M)
    text = text.replace("**", "").replace("*", "")
    return re.sub(r"\s+", " ", text).strip()


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-")


def test_every_definition_is_present_once() -> None:
    owners = [_definition_of(heading) for heading, _level, _body in _sections()]
    for owner in OWNED_PHRASES:
        assert owners.count(owner) == 1, f"{owner} must be defined exactly once"


@pytest.mark.parametrize(
    ("owner", "pattern"),
    [(owner, pattern) for owner, patterns in OWNED_PHRASES.items() for pattern in patterns],
)
def test_owned_phrase_appears_only_in_its_definition(owner: str, pattern: str) -> None:
    hits = [heading for heading, body in _normative() if re.search(pattern, body)]
    assert any(_definition_of(heading) == owner for heading in hits), (
        f"{pattern!r} is missing from {owner}"
    )
    strays = [heading for heading in hits if _definition_of(heading) != owner]
    assert not strays, f"{pattern!r} is owned by {owner} but restated in: {strays}"


def test_no_definition_line_is_copied_into_another_section() -> None:
    normative = _normative()
    plain = {heading: _plain(body) for heading, body in normative}
    copies = []
    for heading, body in normative:
        owner = _definition_of(heading)
        if owner is None:
            continue
        for line in body.splitlines():
            rule = _plain(line)
            if len(rule.split()) < MIN_COPY_WORDS:
                continue
            # Every other normative section, including the other definitions.
            copies.extend(
                f"{owner} line copied into {other}: {rule[:80]}"
                for other, text in plain.items()
                if other != heading and rule in text
            )
    assert not copies, "\n".join(copies)


def test_every_definition_reference_resolves() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    anchors = {_slug(m.group(1).strip()) for m in re.finditer(r"^#{1,3} (.+)$", text, re.M)}
    references = set(re.findall(r"\]\(#([a-z0-9_-]+)\)", text))
    assert references, "the contract must reference its definitions"
    missing = sorted(references - anchors)
    assert not missing, f"references to missing headings: {missing}"
