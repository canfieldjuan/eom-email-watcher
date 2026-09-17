from __future__ import annotations

import pytest

from eom_email_watcher.automate.definition import (
    DefinitionError,
    Workflow,
    canonical_workflow,
    parse_workflow,
)


def _workflow_data() -> dict:
    return {
        "name": "lead-funnel",
        "stages": ["captured", "reviewing", "converted"],
        "initial_stage": "captured",
        "definitions": [
            {
                "name": "start-review",
                "trigger": {"source_kind": "operator.decision", "decision": "start_review"},
                "conditions": [{"field": "record.stage", "op": "equals", "value": "captured"}],
                "effects": [
                    {"kind": "record.transition", "to_stage": "reviewing"},
                    {"kind": "overlay.set", "key": "assignee", "value": "unassigned"},
                ],
            },
            {
                "name": "convert",
                "trigger": {"source_kind": "operator.decision", "decision": "convert"},
                "conditions": [{"field": "record.stage", "op": "in", "value": ["reviewing"]}],
                "effects": [{"kind": "record.transition", "to_stage": "converted"}],
            },
        ],
    }


def test_valid_workflow_parses_and_round_trips() -> None:
    workflow = parse_workflow(_json(_workflow_data()))
    assert workflow.name == "lead-funnel"
    assert [definition.name for definition in workflow.definitions] == ["start-review", "convert"]


def test_canonical_bytes_are_stable_and_key_order_independent() -> None:
    data = _workflow_data()
    reordered = {
        "definitions": data["definitions"],
        "initial_stage": data["initial_stage"],
        "stages": data["stages"],
        "name": data["name"],
    }
    assert canonical_workflow(Workflow.model_validate(data)) == canonical_workflow(
        Workflow.model_validate(reordered)
    )


def test_canonical_bytes_change_when_an_effect_changes() -> None:
    data = _workflow_data()
    changed = _workflow_data()
    changed["definitions"][1]["effects"][0]["to_stage"] = "captured"
    changed["definitions"][1]["conditions"][0]["value"] = ["reviewing", "captured"]
    assert canonical_workflow(Workflow.model_validate(data)) != canonical_workflow(
        Workflow.model_validate(changed)
    )


def test_extra_fields_are_forbidden() -> None:
    data = _workflow_data()
    data["definitions"][0]["surprise"] = True
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_unknown_effect_kind_is_rejected() -> None:
    data = _workflow_data()
    data["definitions"][0]["effects"] = [{"kind": "record.delete"}]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_transition_to_unknown_stage_is_rejected() -> None:
    data = _workflow_data()
    data["definitions"][0]["effects"][0]["to_stage"] = "nowhere"
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_initial_stage_must_be_a_declared_stage() -> None:
    data = _workflow_data()
    data["initial_stage"] = "nowhere"
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_duplicate_definition_names_are_rejected() -> None:
    data = _workflow_data()
    data["definitions"][1]["name"] = "start-review"
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_duplicate_stages_are_rejected() -> None:
    data = _workflow_data()
    data["stages"] = ["captured", "captured", "converted"]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_condition_rejects_operator_not_allowed_for_field() -> None:
    data = _workflow_data()
    data["definitions"][0]["conditions"][0]["op"] = "contains"
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_in_operator_requires_a_non_empty_list() -> None:
    data = _workflow_data()
    data["definitions"][1]["conditions"][0]["value"] = []
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_two_transition_effects_in_one_definition_are_rejected() -> None:
    data = _workflow_data()
    data["definitions"][0]["effects"] = [
        {"kind": "record.transition", "to_stage": "reviewing"},
        {"kind": "record.transition", "to_stage": "converted"},
    ]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_invalid_json_is_a_definition_error() -> None:
    with pytest.raises(DefinitionError):
        parse_workflow("{not json")


def test_condition_operand_must_be_a_declared_stage() -> None:
    data = _workflow_data()
    data["definitions"][0]["conditions"][0]["value"] = "caputred"  # typo, never fires
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_condition_in_operand_members_must_be_declared_stages() -> None:
    data = _workflow_data()
    data["definitions"][1]["conditions"][0]["value"] = ["reviewing", "nowhere"]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_oversized_workflow_is_rejected_at_parse() -> None:
    data = _workflow_data()
    data["definitions"][0]["effects"] = [
        {"kind": "overlay.set", "key": "blob", "value": "x" * 20000}
    ]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_invalid_utf8_bytes_are_a_definition_error() -> None:
    with pytest.raises(DefinitionError):
        parse_workflow(b"\xff")


def test_canonical_workflow_rejects_an_oversized_integer_via_direct_model() -> None:
    data = _workflow_data()
    data["definitions"][0]["effects"] = [{"kind": "overlay.set", "key": "k", "value": 10**5000}]
    workflow = Workflow.model_validate(data)
    # Building the model directly bypasses parse_workflow's json.loads guard, so
    # canonical_workflow itself must translate the json.dumps ValueError.
    with pytest.raises(DefinitionError):
        canonical_workflow(workflow)


def test_definition_error_is_importable_from_the_package() -> None:
    from eom_email_watcher.automate import DefinitionError as PackageDefinitionError

    assert PackageDefinitionError is DefinitionError


def test_lone_surrogate_string_is_a_definition_error() -> None:
    data = _workflow_data()
    # A lone surrogate parses and validates as a str but is not UTF-8 encodable.
    data["definitions"][0]["effects"] = [{"kind": "overlay.set", "key": "k", "value": "\ud800"}]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_mutually_exclusive_stage_conditions_are_rejected() -> None:
    data = _workflow_data()
    # Two record.stage equals conditions with different declared operands can never both
    # hold, so the definition is dead and must be rejected at parse.
    data["definitions"][0]["conditions"] = [
        {"field": "record.stage", "op": "equals", "value": "captured"},
        {"field": "record.stage", "op": "equals", "value": "reviewing"},
    ]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_duplicate_json_member_is_rejected() -> None:
    with pytest.raises(DefinitionError):
        parse_workflow('{"name": "a", "name": "b"}')


def test_integer_over_the_digit_limit_is_a_definition_error() -> None:
    # A syntactically valid but enormous integer trips CPython's int-string digit limit in
    # json.loads (a plain ValueError), which must still surface as DefinitionError.
    raw = '{"value": ' + "1" * 5000 + "}"
    with pytest.raises(DefinitionError):
        parse_workflow(raw)


def _json(data: dict) -> str:
    import json

    return json.dumps(data)
