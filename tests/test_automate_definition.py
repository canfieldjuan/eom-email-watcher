from __future__ import annotations

import pytest

from connect_automate.automate.definition import (
    DefinitionError,
    RequestBindingError,
    Workflow,
    canonical_workflow,
    parse_workflow,
    render_connect_invoke_request,
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
    from connect_automate.automate import DefinitionError as PackageDefinitionError

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


ARTIFACT_ID = "5aaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
INSTANCE_ID = "11111111-1111-4111-8111-111111111111"


def _connect_invoke_request(**overrides: object) -> dict:
    request = {
        "capability": {"id": "onboarding.public-link.list", "version": "1.0"},
        "input": {
            "artifact_id": ARTIFACT_ID,
            "media_type": "application/json",
            "filename": "request.json",
        },
    }
    request.update(overrides)
    return request


def test_workflow_definition_accepts_a_connect_invoke_action() -> None:
    # connect.invoke is in the abstract action vocabulary, so a pack may declare it (the host
    # resolves it to a configured ConnectInvokeAdapter over a CapabilityInvoker). The request
    # is normalized to its materialized shape, so an absent provider is dropped and the
    # defaults for parameters and confirmed are filled.
    data = _workflow_data()
    data["definitions"][0]["actions"] = [
        {"action": "connect.invoke", "request": _connect_invoke_request()}
    ]
    workflow = parse_workflow(_json(data))
    action = workflow.definitions[0].actions[0]
    assert action.action == "connect.invoke"
    assert action.request == {
        "capability": {"id": "onboarding.public-link.list", "version": "1.0"},
        "input": {
            "artifact_id": ARTIFACT_ID,
            "media_type": "application/json",
            "filename": "request.json",
            "content_base64": "",
        },
        "parameters": {},
        "confirmed": False,
    }


def test_connect_invoke_normalized_request_round_trips_through_canonical_bytes() -> None:
    data = _workflow_data()
    data["definitions"][0]["actions"] = [
        {
            "action": "connect.invoke",
            "request": _connect_invoke_request(
                provider={"instance_id": INSTANCE_ID},
                parameters={"page-size": 50},
                confirmed=True,
            ),
        }
    ]
    workflow = parse_workflow(_json(data))
    # The normalized request is stable: re-parsing the canonical bytes yields identical bytes.
    assert canonical_workflow(parse_workflow(canonical_workflow(workflow))) == canonical_workflow(
        workflow
    )
    action = workflow.definitions[0].actions[0]
    assert action.request["provider"] == {"instance_id": INSTANCE_ID}
    assert action.request["parameters"] == {"page-size": 50}
    assert action.request["confirmed"] is True


def test_connect_invoke_rejects_a_malformed_request_at_parse_time() -> None:
    data = _workflow_data()
    # A flat request lacking the structured capability/input shape is rejected at parse/sign
    # time, not deferred to dispatch.
    data["definitions"][0]["actions"] = [
        {"action": "connect.invoke", "request": {"capability_id": "onboarding.public-link.list"}}
    ]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_connect_invoke_rejects_an_underscore_capability_id() -> None:
    data = _workflow_data()
    data["definitions"][0]["actions"] = [
        {
            "action": "connect.invoke",
            "request": _connect_invoke_request(
                capability={"id": "onboarding.public_link.list", "version": "1.0"}
            ),
        }
    ]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_connect_invoke_rejects_a_non_uuid4_artifact_id() -> None:
    data = _workflow_data()
    data["definitions"][0]["actions"] = [
        {
            "action": "connect.invoke",
            "request": _connect_invoke_request(
                input={
                    "artifact_id": "not-a-uuid",
                    "media_type": "application/json",
                    "filename": "request.json",
                }
            ),
        }
    ]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_simple_action_rejects_a_nested_request() -> None:
    data = _workflow_data()
    # A flat-scalar kind cannot carry a nested object: only connect.invoke may.
    data["definitions"][0]["actions"] = [
        {"action": "notify.local", "request": {"title": "Hi", "meta": {"nested": "no"}}}
    ]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_simple_action_accepts_a_flat_scalar_request() -> None:
    data = _workflow_data()
    data["definitions"][0]["actions"] = [
        {"action": "notify.local", "request": {"title": "Hi", "body": "There"}}
    ]
    workflow = parse_workflow(_json(data))
    assert workflow.definitions[0].actions[0].request == {"title": "Hi", "body": "There"}


def test_connect_invoke_accepts_an_overlay_bound_parameter() -> None:
    data = _workflow_data()
    data["definitions"][0]["actions"] = [
        {
            "action": "connect.invoke",
            "request": _connect_invoke_request(
                parameters={"lead-id": {"overlay": "lead_id"}, "dry-run": True}
            ),
        }
    ]
    workflow = parse_workflow(_json(data))
    action = workflow.definitions[0].actions[0]
    # The binding is preserved verbatim in the signed template; resolution happens at admission.
    assert action.request["parameters"] == {
        "lead-id": {"overlay": "lead_id"},
        "dry-run": True,
    }
    # And it round-trips stably through the canonical bytes.
    assert canonical_workflow(parse_workflow(canonical_workflow(workflow))) == canonical_workflow(
        workflow
    )


def test_connect_invoke_rejects_a_binding_with_an_unknown_member() -> None:
    data = _workflow_data()
    data["definitions"][0]["actions"] = [
        {
            "action": "connect.invoke",
            "request": _connect_invoke_request(
                parameters={"lead-id": {"overlay": "lead_id", "extra": "no"}}
            ),
        }
    ]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))


def test_render_resolves_an_overlay_bound_parameter() -> None:
    request = {
        "capability": {"id": "lead.customer-handoff", "version": "1.0"},
        "input": {"artifact_id": ARTIFACT_ID, "media_type": "application/json", "filename": "r"},
        "parameters": {"lead-id": {"overlay": "lead_id"}, "dry-run": True},
        "confirmed": False,
    }
    rendered = render_connect_invoke_request(request, {"lead_id": "L-42", "other": "x"})
    assert rendered["parameters"] == {"lead-id": "L-42", "dry-run": True}
    # The original request is not mutated.
    assert request["parameters"]["lead-id"] == {"overlay": "lead_id"}


def test_render_leaves_a_binding_free_request_unchanged() -> None:
    request = {
        "capability": {"id": "lead.customer-handoff", "version": "1.0"},
        "input": {"artifact_id": ARTIFACT_ID, "media_type": "application/json", "filename": "r"},
        "parameters": {"dry-run": True},
        "confirmed": False,
    }
    rendered = render_connect_invoke_request(request, {"lead_id": "L-42"})
    assert rendered["parameters"] == {"dry-run": True}


def test_render_raises_on_an_unset_bound_overlay() -> None:
    request = {
        "capability": {"id": "lead.customer-handoff", "version": "1.0"},
        "input": {"artifact_id": ARTIFACT_ID, "media_type": "application/json", "filename": "r"},
        "parameters": {"lead-id": {"overlay": "lead_id"}},
        "confirmed": False,
    }
    with pytest.raises(RequestBindingError):
        render_connect_invoke_request(request, {"other": "x"})


def test_connect_invoke_accepts_an_overlay_bound_input_content() -> None:
    data = _workflow_data()
    request = _connect_invoke_request()
    request["input"]["content_base64"] = {"overlay": "payload"}
    data["definitions"][0]["actions"] = [{"action": "connect.invoke", "request": request}]
    workflow = parse_workflow(_json(data))
    action = workflow.definitions[0].actions[0]
    # The binding is preserved verbatim in the signed template and round-trips.
    assert action.request["input"]["content_base64"] == {"overlay": "payload"}
    assert canonical_workflow(parse_workflow(canonical_workflow(workflow))) == canonical_workflow(
        workflow
    )


def test_render_resolves_and_encodes_bound_input_content() -> None:
    import base64

    request = {
        "capability": {"id": "lead.customer-handoff", "version": "1.0"},
        "input": {
            "artifact_id": ARTIFACT_ID,
            "media_type": "application/json",
            "filename": "r",
            "content_base64": {"overlay": "payload"},
        },
        "parameters": {},
        "confirmed": False,
    }
    rendered = render_connect_invoke_request(request, {"payload": '{"lead":"L-42"}'})
    # The overlay holds the raw payload; the host base64-encodes it into the input artifact.
    assert rendered["input"]["content_base64"] == base64.b64encode(
        b'{"lead":"L-42"}'
    ).decode("ascii")
    # The original request is not mutated.
    assert request["input"]["content_base64"] == {"overlay": "payload"}


def test_render_raises_on_an_unset_bound_input_content() -> None:
    request = {
        "capability": {"id": "lead.customer-handoff", "version": "1.0"},
        "input": {
            "artifact_id": ARTIFACT_ID,
            "media_type": "application/json",
            "filename": "r",
            "content_base64": {"overlay": "payload"},
        },
        "parameters": {},
        "confirmed": False,
    }
    with pytest.raises(RequestBindingError):
        render_connect_invoke_request(request, {"other": "x"})


def test_render_raises_when_bound_input_content_is_not_a_string() -> None:
    request = {
        "capability": {"id": "lead.customer-handoff", "version": "1.0"},
        "input": {
            "artifact_id": ARTIFACT_ID,
            "media_type": "application/json",
            "filename": "r",
            "content_base64": {"overlay": "count"},
        },
        "parameters": {},
        "confirmed": False,
    }
    with pytest.raises(RequestBindingError):
        render_connect_invoke_request(request, {"count": 7})


def test_workflow_definition_rejects_an_unknown_action_kind() -> None:
    data = _workflow_data()
    data["definitions"][0]["actions"] = [{"action": "connect.blast", "request": {}}]
    with pytest.raises(DefinitionError):
        parse_workflow(_json(data))
