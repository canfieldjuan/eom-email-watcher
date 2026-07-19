from datetime import UTC, datetime

from eom_email_watcher.model import SYSTEM_PROMPT, validate_analysis


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


def test_prompt_treats_email_as_untrusted() -> None:
    assert "UNTRUSTED DATA" in SYSTEM_PROMPT
    assert "never call tools" in SYSTEM_PROMPT
    assert datetime.now(UTC).tzinfo is UTC
