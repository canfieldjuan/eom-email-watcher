import copy

import pytest

from eom_email_watcher import engine_api
from eom_email_watcher.automation.rules import RuleValidationError, parse_rule_definition


def definition(subject="Straße Σ COI"):
    return {
        "name": "COI",
        "scope": {"provider": "gmail", "account_id": "mailbox-a"},
        "trigger": {"source_kind": "mail.message"},
        "conditions": [
            {"field": "sender", "op": "equals", "value": "broker@example.com"},
            {"field": "subject", "op": "contains", "value": subject},
            {"field": "attachment.media_type", "op": "equals", "value": "application/pdf"},
        ],
        "action": {
            "kind": "connect.invoke",
            "capability": {"id": "certificate.extract", "version": "1.0"},
            "provider": {
                "app_id": "invoice-processor",
                "version": "0.1.0",
                "instance_id": "11111111-1111-4111-8111-111111111111",
            },
            "parameters": {},
        },
        "confirm_each": True,
    }


def prepare(value):
    return engine_api._response(
        {"protocol": 1, "operation": "automation.rules.prepare", "payload": {"definition": value}}
    )


def test_human_subject_preparation_uses_python_owner_without_mutating_input():
    original = definition()
    before = copy.deepcopy(original)
    result = prepare(original)
    assert result["ok"], result
    prepared = result["data"]["definition"]
    assert prepared["conditions"][1]["value"] == "strasse σ coi"
    assert parse_rule_definition(prepared).confirm_each is True
    assert original == before
    with pytest.raises(RuleValidationError):
        parse_rule_definition(original)  # Strict put admission has not widened.


@pytest.mark.parametrize("subject", ["", False, 0, "x" * 4097])
def test_preparation_preserves_subject_boundaries(subject):
    result = prepare(definition(subject))
    assert result["error"]["code"] == "invalid_rule"


def test_preparation_accepts_boundary_and_explicit_false():
    value = definition("x" * 4096)
    value["confirm_each"] = False
    assert prepare(value)["data"]["definition"]["confirm_each"] is False
    value["conditions"] = [value["conditions"][0], value["conditions"][2]]
    assert prepare(value)["ok"] is True


@pytest.mark.parametrize("value", [None, [], False, {"conditions": [False]}, {"unknown": True}])
def test_preparation_does_not_repair_invalid_mixed_definitions(value):
    assert prepare(value)["error"]["code"] == "invalid_rule"
