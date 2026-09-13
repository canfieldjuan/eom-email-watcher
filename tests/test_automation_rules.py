import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eom_email_watcher.automation.rules import (
    ALLOWED_OPS,
    MAX_AUTOMATION_RULES,
    MAX_RULE_DEFINITION_BYTES,
    AutomationFanoutLimit,
    MatchRule,
    RuleValidationError,
    canonical_rule_definition,
    match_rules,
    parse_rule_definition,
)
from eom_email_watcher.db import (
    AutomationRuleLimitExceeded,
    AutomationRuleNotFound,
    AutomationRuleStale,
    AutomationRuleSystemProtected,
    MailboxIdentityChanged,
    Store,
)
from eom_email_watcher.mime import AttachmentDescriptor

MAILBOX_IDENTITY_KEY = "a" * 64
PROVIDER_INSTANCE_ID = "11111111-1111-4111-8111-111111111111"


def rule_definition(
    *,
    conditions: list[dict[str, object]] | None = None,
    scope: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "name": "Invoice PDFs",
        "scope": scope or {},
        "trigger": {"source_kind": "mail.message"},
        "conditions": conditions
        or [{"field": "attachment.media_type", "op": "equals", "value": "application/pdf"}],
        "action": {
            "kind": "connect.invoke",
            "capability": {"id": "invoice.extract", "version": "1.0"},
            "provider": {
                "app_id": "invoice-processor",
                "version": "1.0.0",
                "instance_id": PROVIDER_INSTANCE_ID,
            },
            "parameters": {},
        },
        "confirm_each": False,
    }


def analysis() -> dict[str, object]:
    return {
        "category": "invoice",
        "priority": "high",
        "summary": "An invoice arrived.",
        "action_required": True,
        "suggested_action": "Review the invoice.",
        "deadline_text": None,
        "deadline_iso": None,
        "confidence": 0.9,
    }


def initialized_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "watcher.sqlite3")
    store.initialize()
    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        MAILBOX_IDENTITY_KEY,
        legacy_status="replacement",
    )
    return store


def add_invoice_message(store: Store, message_id: str = "message-1") -> None:
    assert store.add_message(
        message_id=message_id,
        provider="gmail",
        account_id="gmail-default",
        provider_message_id=message_id,
        mailbox_identity_key=MAILBOX_IDENTITY_KEY,
        thread_id=None,
        sender="billing@example.com",
        sender_name="Billing",
        subject="Invoice 42",
        received_at="2026-09-12T12:00:00+00:00",
    )
    store.replace_attachments(
        message_id,
        (AttachmentDescriptor("2", "attachment-2", "invoice.pdf", "application/pdf", 42, 0),),
    )


def test_rule_parser_is_closed_and_canonicalizes_defaults() -> None:
    parsed = parse_rule_definition(rule_definition())
    encoded = canonical_rule_definition(parsed)

    assert b'"confirm_each":false' in encoded
    assert b'"scope":{"account_id":null,"provider":null}' in encoded

    with pytest.raises(RuleValidationError, match="unknown member"):
        parse_rule_definition({**rule_definition(), "dispatch": True})
    with pytest.raises(RuleValidationError, match="attachment.byte_size"):
        parse_rule_definition(
            rule_definition(
                conditions=[
                    {
                        "field": "attachment.media_type",
                        "op": "equals",
                        "value": "application/pdf",
                    },
                    {"field": "attachment.byte_size", "op": "lte", "value": True},
                ]
            )
        )
    with pytest.raises(RuleValidationError, match="missing member at action.capability"):
        value = rule_definition()
        value["action"] = {"kind": "connect.invoke"}
        parse_rule_definition(value)


@pytest.mark.parametrize(
    ("field", "op", "value"),
    [
        ("sender", "equals", "billing@example.com"),
        ("sender", "domain_equals", "example.com"),
        ("sender_name", "equals", "billing department"),
        ("sender_name", "contains", "bill"),
        ("subject", "contains", "invoice"),
        ("subject", "starts_with", "invoice"),
        ("category", "equals", "invoice"),
        ("category", "in", ["invoice", "other"]),
        ("priority", "equals", "high"),
        ("priority", "in", ["urgent", "high"]),
        ("action_required", "equals", True),
        ("attachment.media_type", "equals", "application/pdf"),
        ("attachment.filename", "glob", "invoice-?.pdf"),
        ("attachment.byte_size", "lte", 42),
        ("attachment.count", "gte", 1),
        ("attachment.count", "lte", 1),
    ],
)
def test_every_condition_pair_is_admitted_and_matches(
    field: str,
    op: str,
    value: object,
) -> None:
    condition = {"field": field, "op": op, "value": value}
    conditions = [condition]
    if field != "attachment.media_type":
        conditions.append(
            {
                "field": "attachment.media_type",
                "op": "equals",
                "value": "application/pdf",
            }
        )
    definition = parse_rule_definition(rule_definition(conditions=conditions))

    matches = match_rules(
        [MatchRule("rule-1", 1, None, definition)],
        provider="gmail",
        account_id="gmail-default",
        mailbox_identity_key=MAILBOX_IDENTITY_KEY,
        sender="billing@example.com",
        sender_name="Billing Department",
        subject="Invoice 42",
        result=analysis(),
        attachments=[
            AttachmentDescriptor(
                "2",
                "attachment-2",
                r"folder\INVOICE-1.PDF",
                "application/pdf",
                42,
                0,
            )
        ],
    )

    assert len(matches) == 1


@pytest.mark.parametrize(
    ("field", "op", "value"),
    [
        ("sender", "equals", "BILLING@example.com"),
        ("sender", "domain_equals", "Example.com"),
        ("sender_name", "equals", "Billing"),
        ("sender_name", "contains", ""),
        ("subject", "contains", "x" * 4_097),
        ("category", "equals", ["invoice"]),
        ("category", "in", "invoice"),
        ("category", "in", ["invoice", "invoice"]),
        ("priority", "equals", "critical"),
        ("action_required", "equals", 1),
        ("attachment.media_type", "equals", "Application/PDF"),
        ("attachment.filename", "glob", "../*.pdf"),
        ("attachment.byte_size", "lte", True),
        ("attachment.byte_size", "lte", -1),
        ("attachment.byte_size", "lte", 104_857_601),
        ("attachment.count", "gte", True),
        ("attachment.count", "gte", -1),
        ("attachment.count", "lte", 65),
    ],
)
def test_condition_operand_opposite_boundaries_fail_closed(
    field: str,
    op: str,
    value: object,
) -> None:
    conditions = [{"field": field, "op": op, "value": value}]
    if field != "attachment.media_type":
        conditions.append(
            {
                "field": "attachment.media_type",
                "op": "equals",
                "value": "application/pdf",
            }
        )

    with pytest.raises(RuleValidationError):
        parse_rule_definition(rule_definition(conditions=conditions))


@pytest.mark.parametrize(
    ("field", "op"),
    [
        (field, op)
        for field, admitted in ALLOWED_OPS.items()
        for op in sorted(set().union(*ALLOWED_OPS.values()) - admitted)[:1]
    ],
)
def test_each_condition_field_rejects_an_unsupported_operator(field: str, op: str) -> None:
    conditions = [{"field": field, "op": op, "value": "irrelevant"}]
    if field != "attachment.media_type":
        conditions.append(
            {
                "field": "attachment.media_type",
                "op": "equals",
                "value": "application/pdf",
            }
        )

    with pytest.raises(RuleValidationError, match="not permitted"):
        parse_rule_definition(rule_definition(conditions=conditions))


def test_action_scope_and_collection_boundaries_are_strict() -> None:
    accepted = rule_definition()
    accepted["name"] = "x" * 80
    accepted["conditions"] = [
        {"field": "attachment.count", "op": "gte", "value": 0},
        {"field": "attachment.count", "op": "lte", "value": 64},
        {"field": "attachment.byte_size", "op": "lte", "value": 104_857_600},
        {"field": "category", "op": "in", "value": list(analysis_category())},
        {"field": "priority", "op": "in", "value": ["urgent", "high", "normal", "low"]},
        {"field": "action_required", "op": "equals", "value": False},
        {"field": "attachment.filename", "op": "glob", "value": "*.pdf"},
        {"field": "attachment.media_type", "op": "equals", "value": "application/pdf"},
    ]
    accepted["action"]["parameters"] = {f"key-{index}": index for index in range(16)}  # type: ignore[index]
    parse_rule_definition(accepted)

    invalid_documents: list[dict[str, object]] = []
    invalid_documents.append({**rule_definition(), "name": ""})
    invalid_documents.append({**rule_definition(), "conditions": []})
    invalid_documents.append({**rule_definition(), "scope": {"account_id": "gmail-default"}})
    invalid_documents.append(
        {**rule_definition(), "scope": {"provider": "gmail", "account_id": " padded "}}
    )
    for member, value in (
        ("capability", {"id": "Invoice.Extract", "version": "1.0"}),
        ("capability", {"id": "invoice.extract", "version": "1"}),
        (
            "provider",
            {
                "app_id": "invoice-processor",
                "version": "1.0",
                "instance_id": PROVIDER_INSTANCE_ID,
            },
        ),
        (
            "provider",
            {
                "app_id": "invoice-processor",
                "version": "1.0.0",
                "instance_id": "11111111-1111-1111-8111-111111111111",
            },
        ),
    ):
        document = rule_definition()
        document["action"] = {**document["action"], member: value}  # type: ignore[misc]
        invalid_documents.append(document)
    for parameters in (
        {f"key-{index}": index for index in range(17)},
        {"nested": {"x": 1}},
        {"null": None},
        {"float": 1.5},
        {"too-large": 9_007_199_254_740_992},
    ):
        document = rule_definition()
        document["action"] = {**document["action"], "parameters": parameters}  # type: ignore[misc]
        invalid_documents.append(document)

    for document in invalid_documents:
        with pytest.raises(RuleValidationError):
            parse_rule_definition(document)


def analysis_category() -> tuple[str, ...]:
    return (
        "invoice",
        "scheduling",
        "customer_request",
        "automated_notice",
        "informational",
        "other",
    )


def _canonical_bytes_without_limit(document: dict[str, object]) -> bytes:
    definition = parse_rule_definition(document)
    return json.dumps(
        definition.model_dump(mode="json", exclude_defaults=False),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def test_canonical_bytes_materialize_defaults_and_enforce_exact_utf8_limit() -> None:
    omitted = rule_definition()
    omitted.pop("scope")
    omitted.pop("confirm_each")
    explicit = rule_definition()
    explicit["name"] = "Résumé invoices"
    omitted["name"] = "Résumé invoices"

    assert canonical_rule_definition(parse_rule_definition(omitted)) == canonical_rule_definition(
        parse_rule_definition(explicit)
    )

    maximum = rule_definition()
    maximum["action"]["parameters"] = {  # type: ignore[index]
        **{f"k{index}": "x" * 1_000 for index in range(1, 16)},
        "k0": "x" * 815,
    }
    assert len(_canonical_bytes_without_limit(maximum)) == MAX_RULE_DEFINITION_BYTES
    assert len(canonical_rule_definition(parse_rule_definition(maximum))) == 16_384

    maximum["action"]["parameters"]["k0"] += "x"  # type: ignore[index]
    assert len(_canonical_bytes_without_limit(maximum)) == MAX_RULE_DEFINITION_BYTES + 1
    with pytest.raises(RuleValidationError, match="exceeds"):
        canonical_rule_definition(parse_rule_definition(maximum))


def test_matcher_requires_known_size_and_exact_account_identity() -> None:
    definition = parse_rule_definition(
        rule_definition(
            scope={"provider": "gmail", "account_id": "gmail-default"},
            conditions=[
                {
                    "field": "attachment.media_type",
                    "op": "equals",
                    "value": "application/pdf",
                },
                {"field": "attachment.byte_size", "op": "lte", "value": 0},
            ],
        )
    )
    rule = MatchRule("rule-1", 1, MAILBOX_IDENTITY_KEY, definition)
    common = {
        "provider": "gmail",
        "account_id": "gmail-default",
        "sender": "billing@example.com",
        "sender_name": "Billing",
        "subject": "Invoice",
        "result": analysis(),
    }

    assert match_rules(
        [rule],
        mailbox_identity_key=MAILBOX_IDENTITY_KEY,
        attachments=[AttachmentDescriptor("1", None, "zero.pdf", "application/pdf", 0, 0)],
        **common,
    )
    assert not match_rules(
        [rule],
        mailbox_identity_key=MAILBOX_IDENTITY_KEY,
        attachments=[
            AttachmentDescriptor("1", None, "unknown.pdf", "application/pdf", 0, 0, False)
        ],
        **common,
    )
    assert not match_rules(
        [rule],
        mailbox_identity_key="b" * 64,
        attachments=[AttachmentDescriptor("1", None, "zero.pdf", "application/pdf", 0, 0)],
        **common,
    )


def test_matcher_accepts_1000_fires_and_rejects_1001_all_or_zero() -> None:
    rule = MatchRule("rule-1", 1, None, parse_rule_definition(rule_definition()))
    attachments = [
        AttachmentDescriptor(
            str(index), None, f"invoice-{index}.pdf", "application/pdf", index, index
        )
        for index in range(1_001)
    ]
    common = {
        "provider": "gmail",
        "account_id": "gmail-default",
        "mailbox_identity_key": MAILBOX_IDENTITY_KEY,
        "sender": "billing@example.com",
        "sender_name": "Billing",
        "subject": "Invoice",
        "result": analysis(),
    }

    assert len(match_rules([rule], attachments=attachments[:1_000], **common)) == 1_000
    with pytest.raises(AutomationFanoutLimit):
        match_rules([rule], attachments=attachments, **common)


def test_rule_cas_noop_and_atomic_analysis_create_one_durable_fire(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    created = store.put_automation_rule(rule_definition())
    disabled = store.set_automation_rule_enabled(
        created.summary.rule_id, created.summary.version, False
    )
    revision_after_disable, _rules = store.automation_rules_snapshot()
    no_op = store.set_automation_rule_enabled(
        disabled.summary.rule_id, disabled.summary.version, False
    )
    revision_after_noop, _rules = store.automation_rules_snapshot()

    assert no_op == disabled
    assert revision_after_noop == revision_after_disable
    with pytest.raises(AutomationRuleStale):
        store.set_automation_rule_enabled(created.summary.rule_id, 1, False)

    enabled = store.set_automation_rule_enabled(
        disabled.summary.rule_id, disabled.summary.version, True
    )
    add_invoice_message(store)
    store.mark_analyzed(
        "message-1",
        analysis(),
        mailbox_identity_key=MAILBOX_IDENTITY_KEY,
        now=datetime(2026, 9, 12, 12, 1, tzinfo=UTC),
    )

    fires = store.automation_fires_for_message("message-1")
    assert len(fires) == 1
    assert fires[0].rule_id == enabled.summary.rule_id
    assert fires[0].rule_version == enabled.summary.version
    assert fires[0].state == "pending_dispatch"
    attempts = store.automation_fire_attempts(fires[0].fire_id)
    assert len(attempts) == 1
    assert attempts[0].attempt_no == 1
    with store.connection() as db:
        row = db.execute(
            """SELECT status, rules_revision_at_analysis, rules_evaluation_error
            FROM messages WHERE message_id = 'message-1'"""
        ).fetchone()
        assert row is not None
        assert row["status"] == "analyzed"
        assert row["rules_revision_at_analysis"] == revision_after_noop + 1
        assert row["rules_evaluation_error"] is None
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 0


def test_store_rule_authority_is_immutable_system_protected_and_non_resurrecting(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    created_at = datetime(2026, 9, 12, 12, tzinfo=UTC)
    created = store.put_automation_rule(rule_definition(), now=created_at)
    rule_id = created.summary.rule_id
    encoded = canonical_rule_definition(parse_rule_definition(rule_definition()))

    with store.connection() as db:
        version = db.execute(
            "SELECT * FROM automation_rule_versions WHERE rule_id = ? AND version = 1",
            (rule_id,),
        ).fetchone()
        assert version is not None
        assert bytes(version["definition_json"]) == encoded
        assert version["definition_sha256"] == hashlib.sha256(encoded).hexdigest()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "UPDATE automation_rule_versions SET enabled = 0 WHERE rule_id = ?",
                (rule_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "DELETE FROM automation_rule_versions WHERE rule_id = ?",
                (rule_id,),
            )
        db.execute("UPDATE automation_rules SET system = 1 WHERE rule_id = ?", (rule_id,))

    with pytest.raises(AutomationRuleSystemProtected):
        store.set_automation_rule_enabled(rule_id, 1, False)
    with pytest.raises(AutomationRuleSystemProtected):
        store.put_automation_rule(rule_definition(), rule_id=rule_id, expected_version=1)
    with pytest.raises(AutomationRuleSystemProtected):
        store.delete_automation_rule(rule_id, 1)

    ordinary = store.put_automation_rule(rule_definition())
    tombstone_version = store.delete_automation_rule(
        ordinary.summary.rule_id,
        ordinary.summary.version,
    )
    assert tombstone_version == 2
    with pytest.raises(AutomationRuleNotFound):
        store.put_automation_rule(
            rule_definition(),
            rule_id=ordinary.summary.rule_id,
            expected_version=tombstone_version,
        )
    with pytest.raises(AutomationRuleNotFound):
        store.set_automation_rule_enabled(
            ordinary.summary.rule_id,
            tombstone_version,
            True,
        )
    with pytest.raises(AutomationRuleNotFound):
        store.delete_automation_rule(ordinary.summary.rule_id, tombstone_version)


def test_store_reparses_definitions_and_skips_corrupted_current_versions(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    with pytest.raises(RuleValidationError):
        store.put_automation_rule({**rule_definition(), "unknown": True})

    created = store.put_automation_rule(rule_definition())
    with store.connection() as db:
        db.execute("DROP TRIGGER automation_rule_versions_immutable_update")
        db.execute(
            "UPDATE automation_rule_versions SET definition_sha256 = ? WHERE rule_id = ?",
            ("0" * 64, created.summary.rule_id),
        )

    corrupted = store.automation_rule(created.summary.rule_id)
    assert corrupted.summary.valid is False
    assert corrupted.summary.name is None
    assert corrupted.summary.invalid_reason == "stored definition digest does not match"
    add_invoice_message(store)
    store.mark_analyzed(
        "message-1",
        analysis(),
        mailbox_identity_key=MAILBOX_IDENTITY_KEY,
    )
    assert store.automation_fires_for_message("message-1") == []


def test_rule_limit_counts_only_live_rules(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    rules = [store.put_automation_rule(rule_definition()) for _ in range(MAX_AUTOMATION_RULES)]

    revision, summaries = store.automation_rules_snapshot()
    assert revision == MAX_AUTOMATION_RULES
    assert len(summaries) == MAX_AUTOMATION_RULES
    with pytest.raises(AutomationRuleLimitExceeded):
        store.put_automation_rule(rule_definition())

    deleted = rules[0]
    store.delete_automation_rule(deleted.summary.rule_id, deleted.summary.version)
    replacement = store.put_automation_rule(rule_definition())
    assert replacement.summary.version == 1
    assert len(store.automation_rules_snapshot()[1]) == MAX_AUTOMATION_RULES


def test_account_scoped_toggle_keeps_old_key_and_definition_edit_rebinds(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    scoped = rule_definition(scope={"provider": "gmail", "account_id": "gmail-default"})
    first = store.put_automation_rule(
        scoped,
        expected_account_identity=MAILBOX_IDENTITY_KEY,
        now=datetime(2026, 9, 12, 12, tzinfo=UTC),
    )
    disabled = store.set_automation_rule_enabled(
        first.summary.rule_id,
        first.summary.version,
        False,
        now=datetime(2026, 9, 12, 12, 1, tzinfo=UTC),
    )
    revision_before_noop = store.automation_rules_snapshot()[0]
    noop = store.set_automation_rule_enabled(
        first.summary.rule_id,
        disabled.summary.version,
        False,
        now=datetime(2026, 9, 12, 12, 2, tzinfo=UTC),
    )
    assert noop.summary.updated_at == disabled.summary.updated_at
    assert store.automation_rules_snapshot()[0] == revision_before_noop

    replacement_identity = "b" * 64
    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        replacement_identity,
        legacy_status="replacement",
    )
    edited = store.put_automation_rule(
        scoped,
        rule_id=first.summary.rule_id,
        expected_version=disabled.summary.version,
        expected_account_identity=replacement_identity,
    )
    with store.connection() as db:
        keys = db.execute(
            """SELECT version, scope_mailbox_identity_key
            FROM automation_rule_versions WHERE rule_id = ? ORDER BY version""",
            (first.summary.rule_id,),
        ).fetchall()
    assert [(row["version"], row["scope_mailbox_identity_key"]) for row in keys] == [
        (1, MAILBOX_IDENTITY_KEY),
        (2, MAILBOX_IDENTITY_KEY),
        (3, replacement_identity),
    ]
    assert edited.summary.version == 3


def test_replacement_identity_reuses_provider_message_id_as_a_new_source(
    tmp_path: Path,
) -> None:
    store = initialized_store(tmp_path)
    add_invoice_message(store, "old-message")
    replacement_identity = "b" * 64
    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        replacement_identity,
        legacy_status="replacement",
    )

    assert not store.has_seen_message(
        "old-message",
        provider="gmail",
        account_id="gmail-default",
        mailbox_identity_key=replacement_identity,
    )
    assert store.add_message(
        message_id="replacement-message",
        provider="gmail",
        account_id="gmail-default",
        provider_message_id="old-message",
        mailbox_identity_key=replacement_identity,
        thread_id=None,
        sender="billing@example.com",
        sender_name="Billing",
        subject="Replacement mailbox invoice",
        received_at="2026-09-12T12:02:00+00:00",
    )
    assert not store.add_message(
        message_id="replacement-replay",
        provider="gmail",
        account_id="gmail-default",
        provider_message_id="old-message",
        mailbox_identity_key=replacement_identity,
        thread_id=None,
        sender="billing@example.com",
        sender_name="Billing",
        subject="Replay",
        received_at="2026-09-12T12:03:00+00:00",
    )
    with store.connection() as db:
        identities = db.execute(
            """SELECT mailbox_identity_key FROM messages
            WHERE provider_message_id = 'old-message' ORDER BY message_id"""
        ).fetchall()
    assert [row["mailbox_identity_key"] for row in identities] == [
        MAILBOX_IDENTITY_KEY,
        replacement_identity,
    ]


@pytest.mark.parametrize(
    ("attachment_count", "rule_count", "expected_error", "expected_fires"),
    [
        (1_000, 1, None, 1_000),
        (1_001, 1, "automation_fanout_limit", 0),
        (501, 2, "automation_fanout_limit", 0),
    ],
)
def test_store_fanout_bound_is_all_or_zero(
    tmp_path: Path,
    attachment_count: int,
    rule_count: int,
    expected_error: str | None,
    expected_fires: int,
) -> None:
    store = initialized_store(tmp_path)
    for _index in range(rule_count):
        store.put_automation_rule(rule_definition())
    add_invoice_message(store)
    store.replace_attachments(
        "message-1",
        (
            AttachmentDescriptor(
                str(index),
                None,
                f"invoice-{index}.pdf",
                "application/pdf",
                index,
                index,
            )
            for index in range(attachment_count)
        ),
    )

    store.mark_analyzed(
        "message-1",
        analysis(),
        mailbox_identity_key=MAILBOX_IDENTITY_KEY,
    )

    with store.connection() as db:
        message = db.execute(
            """SELECT status, rules_evaluation_error
            FROM messages WHERE message_id = 'message-1'"""
        ).fetchone()
        assert message is not None
        assert tuple(message) == ("analyzed", expected_error)
        assert db.execute("SELECT COUNT(*) FROM automation_fires").fetchone()[0] == expected_fires
        assert (
            db.execute("SELECT COUNT(*) FROM automation_fire_attempts").fetchone()[0]
            == expected_fires
        )


def test_fire_insert_failure_rolls_back_analysis_revision_and_attempt(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    store.put_automation_rule(rule_definition())
    add_invoice_message(store)
    with store.connection() as db:
        db.execute(
            """CREATE TRIGGER fail_automation_fire
            BEFORE INSERT ON automation_fires
            BEGIN
                SELECT RAISE(ABORT, 'injected fire failure');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected fire failure"):
        store.mark_analyzed(
            "message-1",
            analysis(),
            mailbox_identity_key=MAILBOX_IDENTITY_KEY,
        )

    with store.connection() as db:
        row = db.execute(
            """SELECT status, rules_revision_at_analysis
            FROM messages WHERE message_id = 'message-1'"""
        ).fetchone()
        assert row is not None
        assert tuple(row) == ("pending", None)
        assert db.execute("SELECT COUNT(*) FROM automation_fires").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM automation_fire_attempts").fetchone()[0] == 0


def test_rule_mutation_and_analysis_serialize_to_one_coherent_revision(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    created = store.put_automation_rule(rule_definition())
    add_invoice_message(store)
    replacement = rule_definition(
        conditions=[
            {
                "field": "attachment.media_type",
                "op": "equals",
                "value": "image/png",
            }
        ]
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        edit = executor.submit(
            store.put_automation_rule,
            replacement,
            rule_id=created.summary.rule_id,
            expected_version=created.summary.version,
        )
        analyze = executor.submit(
            store.mark_analyzed,
            "message-1",
            analysis(),
            mailbox_identity_key=MAILBOX_IDENTITY_KEY,
        )
        assert edit.result().summary.version == 2
        analyze.result()

    with store.connection() as db:
        message = db.execute(
            "SELECT rules_revision_at_analysis FROM messages WHERE message_id = 'message-1'"
        ).fetchone()
    assert message is not None
    fires = store.automation_fires_for_message("message-1")
    assert (message["rules_revision_at_analysis"], tuple(fire.rule_version for fire in fires)) in {
        (1, (1,)),
        (2, ()),
    }


def test_old_mailbox_session_cannot_mutate_after_identity_replacement(tmp_path: Path) -> None:
    store = initialized_store(tmp_path)
    add_invoice_message(store)
    store.set_state("100", mailbox_identity_key=MAILBOX_IDENTITY_KEY)
    replacement_identity = "b" * 64
    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        replacement_identity,
        legacy_status="replacement",
    )

    with pytest.raises(MailboxIdentityChanged, match="mailbox identity changed"):
        store.add_message(
            message_id="stale-session-message",
            provider="gmail",
            account_id="gmail-default",
            provider_message_id="stale-session-message",
            mailbox_identity_key=MAILBOX_IDENTITY_KEY,
            thread_id=None,
            sender="billing@example.com",
            sender_name="Billing",
            subject="Stale session",
            received_at="2026-09-12T12:05:00+00:00",
        )
    with pytest.raises(MailboxIdentityChanged, match="mailbox identity changed"):
        store.set_state("101", mailbox_identity_key=MAILBOX_IDENTITY_KEY)
    with pytest.raises(MailboxIdentityChanged, match="mailbox identity changed"):
        store.mark_analyzed(
            "message-1",
            analysis(),
            mailbox_identity_key=MAILBOX_IDENTITY_KEY,
        )
    assert store.state() is None
    assert store.message_source("message-1").mailbox_identity_key == MAILBOX_IDENTITY_KEY
    assert store.automation_fires_for_message("message-1") == []
