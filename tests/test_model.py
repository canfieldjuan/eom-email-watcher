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


def test_prompt_treats_email_as_untrusted() -> None:
    assert "UNTRUSTED DATA" in SYSTEM_PROMPT
    assert "never call tools" in SYSTEM_PROMPT
    assert datetime.now(UTC).tzinfo is UTC


def test_required_api_token_is_loaded_from_private_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret-value\n", encoding="utf-8")
    model = LocalModel("http://127.0.0.1:1234/v1", "model", 60, token_file, True)
    assert model._headers() == {"Authorization": "Bearer secret-value"}


def test_required_api_token_fails_closed(tmp_path: Path) -> None:
    model = LocalModel("http://127.0.0.1:1234/v1", "model", 60, tmp_path / "missing", True)
    with pytest.raises(ModelError, match="token is missing"):
        model._headers()
