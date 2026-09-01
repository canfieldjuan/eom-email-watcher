from datetime import UTC, datetime
from pathlib import Path

import pytest

from eom_email_watcher.model import SYSTEM_PROMPT, LocalModel, ModelError, validate_analysis


def valid_result() -> dict[str, object]:
    return {
        "category": "scheduling",
        "priority": "high",
        "summary": "A schedule change is requested.",
        "action_required": True,
        "suggested_action": "Confirm the new time.",
        "deadline_text": "by July 20",
        "deadline_iso": "2026-07-20",
        "confidence": 0.91,
    }


def test_valid_result_is_preserved() -> None:
    result = validate_analysis(valid_result(), "2026-07-18T12:00:00+00:00")
    assert result.deadline_iso == "2026-07-20"


def test_hallucinated_past_deadline_is_removed_but_text_retained() -> None:
    raw = valid_result()
    raw["deadline_iso"] = "2024-07-20"
    result = validate_analysis(raw, "2026-07-18T12:00:00+00:00")
    assert result.deadline_text == "by July 20"
    assert result.deadline_iso is None


def test_suggested_action_requires_action_even_when_model_says_false() -> None:
    raw = valid_result()
    raw["action_required"] = False
    result = validate_analysis(raw, "2026-07-18T12:00:00+00:00")
    assert result.action_required is True


@pytest.mark.parametrize("suggested_action", [None, "", "   "])
def test_required_action_without_usable_suggestion_is_rejected(
    suggested_action: str | None,
) -> None:
    raw = valid_result()
    raw["suggested_action"] = suggested_action
    raw["deadline_text"] = None
    raw["deadline_iso"] = None

    with pytest.raises(ModelError, match="requires a suggested action"):
        validate_analysis(raw, "2026-07-18T12:00:00+00:00")


def test_deadline_without_suggested_action_is_rejected() -> None:
    raw = valid_result()
    raw["action_required"] = False
    raw["suggested_action"] = None

    with pytest.raises(ModelError, match="requires a suggested action"):
        validate_analysis(raw, "2026-07-18T12:00:00+00:00")


@pytest.mark.parametrize("suggested_action", [None, "", "   "])
def test_no_action_with_empty_suggestion_is_normalized(
    suggested_action: str | None,
) -> None:
    raw = valid_result()
    raw["action_required"] = False
    raw["suggested_action"] = suggested_action
    raw["deadline_text"] = None
    raw["deadline_iso"] = None

    result = validate_analysis(raw, "2026-07-18T12:00:00+00:00")

    assert result.action_required is False
    assert result.suggested_action is None


def test_prompt_treats_email_as_untrusted() -> None:
    assert "UNTRUSTED DATA" in SYSTEM_PROMPT
    assert "never call tools" in SYSTEM_PROMPT
    assert datetime.now(UTC).tzinfo is UTC


def test_prompt_assigns_obligations_from_the_mailbox_owner_perspective() -> None:
    assert "mailbox owner" in SYSTEM_PROMPT
    assert "who owes whom" in SYSTEM_PROMPT
    assert "quoted history" in SYSTEM_PROMPT
    assert "building-access card" in SYSTEM_PROMPT
    assert "payment card" in SYSTEM_PROMPT


def test_unsupported_payment_card_semantics_are_rejected() -> None:
    raw = valid_result()
    raw["summary"] = "The sender requests payment card numbers."
    raw["suggested_action"] = "Provide the payment card details."
    raw["deadline_text"] = None
    raw["deadline_iso"] = None

    with pytest.raises(ModelError, match="unsupported payment-card semantics"):
        validate_analysis(
            raw,
            "2026-07-18T12:00:00+00:00",
            source_text="Please provide the building access card numbers.",
        )


@pytest.mark.parametrize(
    ("source_text", "suggested_action"),
    [
        (
            "Please provide the corporate credit-card number.",
            "Provide the requested payment card number.",
        ),
        (
            "Please provide the building access card numbers.",
            "Provide the requested building-access card numbers.",
        ),
        (
            "Please pay by card through the secure portal.",
            "Use the requested payment card through the secure portal.",
        ),
    ],
)
def test_supported_card_semantics_remain_valid(
    source_text: str, suggested_action: str
) -> None:
    raw = valid_result()
    raw["summary"] = suggested_action
    raw["suggested_action"] = suggested_action
    raw["deadline_text"] = None
    raw["deadline_iso"] = None

    result = validate_analysis(
        raw,
        "2026-07-18T12:00:00+00:00",
        source_text=source_text,
    )

    assert result.suggested_action == suggested_action


def test_required_api_token_is_loaded_from_private_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret-value\n", encoding="utf-8")
    model = LocalModel("http://127.0.0.1:1234/v1", "model", 60, token_file, True)
    assert model._headers() == {"Authorization": "Bearer secret-value"}


def test_required_api_token_fails_closed(tmp_path: Path) -> None:
    model = LocalModel("http://127.0.0.1:1234/v1", "model", 60, tmp_path / "missing", True)
    with pytest.raises(ModelError, match="token is missing"):
        model._headers()


def test_reasoning_field_used_when_content_empty(tmp_path: Path, monkeypatch) -> None:
    # Reasoning models (e.g. qwen3.5) leave content empty and put the JSON in
    # the reasoning field; analyze() must still recover it.
    token_file = tmp_path / "token"
    token_file.write_text("k", encoding="utf-8")
    model = LocalModel("http://127.0.0.1:1234/v1", "qwen3.5-4b", 60, token_file, True)

    class FakeResp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "", "reasoning": (
                '{"category":"invoice","priority":"normal","summary":"An invoice is due.",'
                '"action_required":true,"suggested_action":"Pay it","deadline_text":null,'
                '"deadline_iso":null,"confidence":0.9}')}}]}

    monkeypatch.setattr("eom_email_watcher.model.httpx.post", lambda *a, **k: FakeResp())
    result = model.analyze(
        sender="ap@vendor.com", subject="Invoice", received_at="2026-07-18T12:00:00+00:00",
        body="Please remit payment.", attachment_names=(),
        current_local_time=datetime(2026, 7, 18, tzinfo=UTC),
    )
    assert result.category == "invoice"
    assert result.action_required is True


def test_local_model_applies_source_grounding_to_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("k", encoding="utf-8")
    model = LocalModel("http://127.0.0.1:1234/v1", "model", 60, token_file, True)

    class FakeResp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"category":"customer_request","priority":"high",'
                                '"summary":"Payment card numbers are requested.",'
                                '"action_required":true,"suggested_action":'
                                '"Provide payment card details.","deadline_text":null,'
                                '"deadline_iso":null,"confidence":0.9}'
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr("eom_email_watcher.model.httpx.post", lambda *a, **k: FakeResp())

    with pytest.raises(ModelError, match="unsupported payment-card semantics"):
        model.analyze(
            sender="customer@example.com",
            subject="Building access",
            received_at="2026-08-19T18:34:00+00:00",
            body="Please provide the building access card numbers.",
            attachment_names=(),
            current_local_time=datetime(2026, 8, 19, tzinfo=UTC),
        )
