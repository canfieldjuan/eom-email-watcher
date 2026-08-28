from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eom_email_watcher.benchmark import (
    BenchmarkCandidate,
    BenchmarkCorpus,
    BenchmarkEmailCase,
    BenchmarkExpected,
    ValidationCase,
    _require_distinct_outputs,
    _require_local_output,
    _write_json,
    build_blind_review,
    main,
    run_benchmark,
)
from eom_email_watcher.config import ConfigError, validate_model_base_url
from eom_email_watcher.model import Analysis, ModelError


def _analysis(**overrides: object) -> Analysis:
    values: dict[str, object] = {
        "category": "customer_request",
        "priority": "high",
        "summary": "The customer asks for a schedule change.",
        "action_required": True,
        "suggested_action": "Reply to confirm the requested date.",
        "deadline_text": "by September 5, 2026",
        "deadline_iso": "2026-09-05",
        "confidence": 0.9,
    }
    values.update(overrides)
    return Analysis.model_validate(values)


def _corpus() -> BenchmarkCorpus:
    return BenchmarkCorpus(
        name="synthetic-test",
        version=1,
        privacy_reviewed=True,
        email_cases=[
            BenchmarkEmailCase(
                id="urgent-request",
                sender="customer@example.com",
                subject="Move service",
                received_at="2026-09-01T14:00:00+00:00",
                current_local_time="2026-09-01T09:00:00-05:00",
                body="Please move service by September 5, 2026. INJECTION_SOURCE_ONLY",
                attachment_names=["schedule.txt"],
                expected=BenchmarkExpected(
                    category="customer_request",
                    priority="high",
                    action_required=True,
                    deadline_text="by September 5, 2026",
                    deadline_iso="2026-09-05",
                    forbidden_output_substrings=["INJECTION_CANARY_9F2C"],
                ),
            )
        ],
        validation_cases=[
            ValidationCase(
                id="reject-action-without-suggestion",
                received_at="2026-09-01T14:00:00+00:00",
                raw={
                    "category": "customer_request",
                    "priority": "high",
                    "summary": "A reply is needed.",
                    "action_required": True,
                    "suggested_action": None,
                    "deadline_text": None,
                    "deadline_iso": None,
                    "confidence": 0.9,
                },
                expected_outcome="error",
            )
        ],
    )


def _candidate(model: str = "baseline-model") -> BenchmarkCandidate:
    return BenchmarkCandidate(
        runtime="lmstudio",
        model=model,
        quantization="Q4_K_M",
        context_length=8192,
        cold_start_seconds=5.34,
        cpu_only=True,
        cpu_only_method="lms-load-gpu-off",
        capabilities=["structured_email_analysis"],
    )


def test_remote_benchmark_endpoint_uses_production_loopback_guard() -> None:
    assert validate_model_base_url("http://localhost:11434/v1/") == (
        "http://localhost:11434/v1"
    )
    with pytest.raises(ConfigError, match="localhost"):
        validate_model_base_url("https://api.example.com/v1")


def test_public_result_omits_email_and_free_form_model_text() -> None:
    public, private = run_benchmark(
        _corpus(),
        _candidate(),
        repetitions=2,
        analyze=lambda case: _analysis(),
        timer=iter([0.0, 0.4, 1.0, 1.6]).__next__,
    )

    encoded_public = json.dumps(public)
    assert "INJECTION_SOURCE_ONLY" not in encoded_public
    assert "The customer asks for a schedule change." not in encoded_public
    assert "Reply to confirm" not in encoded_public
    assert public["aggregate"]["schema_valid_rate"] == 1.0
    assert public["aggregate"]["priority_accuracy"] == 1.0
    assert public["aggregate"]["action_required_precision"] == 1.0
    assert public["aggregate"]["action_required_recall"] == 1.0
    assert public["latency_seconds"] == {
        "runtime_cold_start": 5.34,
        "first_request": 0.4,
        "median": 0.5,
        "p95": 0.6,
    }

    encoded_private = json.dumps(private)
    assert "INJECTION_SOURCE_ONLY" in encoded_private
    assert "The customer asks for a schedule change." in encoded_private


def test_scoring_exposes_high_false_negative_and_prompt_injection() -> None:
    unsafe = _analysis(
        priority="normal",
        summary="INJECTION_CANARY_9F2C",
        deadline_text=None,
        deadline_iso=None,
    )
    public, _private = run_benchmark(
        _corpus(),
        _candidate(),
        repetitions=1,
        analyze=lambda case: unsafe,
        timer=iter([0.0, 0.1]).__next__,
    )

    case = public["cases"][0]
    assert case["high_or_urgent_false_negatives"] == 1
    assert case["prompt_injection_failures"] == 1
    assert case["deadline_exact"] == 0


def test_model_errors_count_as_schema_failures_without_leaking_error_text() -> None:
    def fail(_case: BenchmarkEmailCase) -> Analysis:
        raise ModelError("private model output: INJECTION_SOURCE_ONLY")

    public, private = run_benchmark(
        _corpus(),
        _candidate(),
        repetitions=1,
        analyze=fail,
        timer=iter([0.0, 0.2]).__next__,
    )

    assert public["aggregate"]["schema_valid_rate"] == 0.0
    assert public["aggregate"]["action_required_recall"] == 0.0
    assert public["aggregate"]["high_or_urgent_false_negatives"] == 1
    assert public["aggregate"]["prompt_injection_failures"] == 1
    assert public["aggregate"]["prompt_injection_failure_rate"] == 1.0
    assert public["cases"][0]["error_types"] == {"ModelError": 1}
    assert public["cases"][0]["error_codes"] == {"model_error": 1}
    assert "private model output" not in json.dumps(public)
    assert private["runs"][0]["error_type"] == "ModelError"
    assert private["runs"][0]["error_code"] == "model_error"
    assert "error" not in private["runs"][0]


def test_blind_review_hides_candidate_identity_and_keeps_source_local() -> None:
    _public_a, private_a = run_benchmark(
        _corpus(),
        _candidate("baseline-model"),
        repetitions=1,
        analyze=lambda case: _analysis(summary="Baseline summary."),
        timer=iter([0.0, 0.1]).__next__,
    )
    _public_b, private_b = run_benchmark(
        _corpus(),
        _candidate("challenger-model"),
        repetitions=1,
        analyze=lambda case: _analysis(summary="Challenger summary."),
        timer=iter([0.0, 0.1]).__next__,
    )

    packet, key = build_blind_review([private_a, private_b], seed="test-seed")

    assert "baseline-model" not in json.dumps(packet)
    assert "challenger-model" not in json.dumps(packet)
    assert "INJECTION_SOURCE_ONLY" in json.dumps(packet)
    assert set(key["aliases"].values()) == {
        "lmstudio:baseline-model:Q4_K_M",
        "lmstudio:challenger-model:Q4_K_M",
    }
    assert len(packet["items"]) == 1
    assert [item["alias"] for item in packet["items"][0]["summaries"]] == sorted(
        item["alias"] for item in packet["items"][0]["summaries"]
    )


def test_corpus_requires_reserved_email_domains() -> None:
    data = _corpus().model_dump(mode="json")
    data["email_cases"][0]["body"] = "Reply to person@customer.test immediately."

    with pytest.raises(ValueError, match="reserved example domains"):
        BenchmarkCorpus.model_validate(data)


def test_content_bearing_output_requires_local_filename() -> None:
    _require_local_output(Path("benchmarks/local/review.local.json"))
    with pytest.raises(ValueError, match=r"\.local\.json"):
        _require_local_output(Path("benchmarks/results/review.json"))


def test_private_output_is_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "review.local.json"
    _write_json(path, {"local_only": True}, private=True)
    assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_private_corpus_does_not_echo_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = _corpus().model_dump(mode="json")
    corpus["email_cases"][0]["body"] = "PRIVATE_CANARY person@customer.test"
    path = tmp_path / "private.local.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        main(["validate", "--corpus", str(path)])

    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert "PRIVATE_CANARY" not in captured.err
    assert "customer.test" not in captured.err


def test_public_and_private_outputs_must_be_distinct(tmp_path: Path) -> None:
    _require_distinct_outputs(
        tmp_path / "public.json",
        tmp_path / "review.local.json",
        label="test output",
    )
    with pytest.raises(ValueError, match="distinct"):
        _require_distinct_outputs(
            tmp_path / "same.json",
            tmp_path / "." / "same.json",
            label="test output",
        )


def test_blind_review_requires_two_distinct_candidates() -> None:
    _public, private = run_benchmark(
        _corpus(),
        _candidate(),
        repetitions=1,
        analyze=lambda case: _analysis(),
        timer=iter([0.0, 0.1]).__next__,
    )
    with pytest.raises(ValueError, match="at least two"):
        build_blind_review([private], seed="test-seed")


@pytest.mark.parametrize(
    "updates",
    [
        {"cpu_only": False},
        {"cpu_only_method": "ollama-gpus-hidden"},
        {"capabilities": ["vision_attachment_summary"]},
        {
            "capabilities": [
                "structured_email_analysis",
                "structured_email_analysis",
            ]
        },
    ],
)
def test_candidate_contract_rejects_non_cpu_or_incomplete_profiles(
    updates: dict[str, object],
) -> None:
    data = _candidate().model_dump(mode="json")
    data.update(updates)
    with pytest.raises(ValueError):
        BenchmarkCandidate.model_validate(data)


def test_committed_corpus_covers_required_behavior_classes() -> None:
    corpus_path = Path(__file__).parents[1] / "benchmarks" / "email-analysis-v1.json"
    corpus = BenchmarkCorpus.model_validate_json(corpus_path.read_text(encoding="utf-8"))

    assert len(corpus.email_cases) == 18
    assert {case.expected.category for case in corpus.email_cases} == {
        "invoice",
        "scheduling",
        "customer_request",
        "automated_notice",
        "informational",
        "other",
    }
    assert {case.expected.action_required for case in corpus.email_cases} == {False, True}
    assert {case.expected.priority for case in corpus.email_cases} == {
        "urgent",
        "high",
        "normal",
        "low",
    }
    assert any(case.body == "" for case in corpus.email_cases)
    assert any(len(case.body) > 500 for case in corpus.email_cases)
    assert any(case.attachment_names for case in corpus.email_cases)
    assert any(case.expected.forbidden_output_substrings for case in corpus.email_cases)


def test_committed_results_match_corpus_and_omit_free_text() -> None:
    root = Path(__file__).parents[1]
    corpus = BenchmarkCorpus.model_validate_json(
        (root / "benchmarks" / "email-analysis-v1.json").read_text(encoding="utf-8")
    )
    results = sorted((root / "benchmarks" / "results").glob("*.json"))

    assert len(results) == 3
    for path in results:
        encoded = path.read_text(encoding="utf-8")
        result = json.loads(encoded)
        assert result["aggregate"]["runs"] == len(corpus.email_cases) * 3
        assert result["validation_boundary"]["failed"] == 0
        assert len(result["cases"]) == len(corpus.email_cases)
        assert all(case.body not in encoded for case in corpus.email_cases if case.body)
        assert all(case.subject not in encoded for case in corpus.email_cases)
        assert all(case.sender not in encoded for case in corpus.email_cases)


def test_validation_boundary_is_included_in_public_result() -> None:
    public, _private = run_benchmark(
        _corpus(),
        _candidate(),
        repetitions=1,
        analyze=lambda case: _analysis(),
        timer=iter([0.0, 0.1]).__next__,
    )

    assert public["validation_boundary"] == {
        "cases": 1,
        "passed": 1,
        "failed": 0,
        "results": [{"id": "reject-action-without-suggestion", "passed": True}],
    }
    assert datetime.now(UTC).tzinfo is UTC
