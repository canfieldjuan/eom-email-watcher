from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import validate_model_base_url
from .model import Analysis, LocalModel, ModelError, validate_analysis

Capability = Literal[
    "structured_email_analysis",
    "text_attachment_summary",
    "vision_attachment_summary",
]
Category = Literal[
    "invoice",
    "scheduling",
    "customer_request",
    "automated_notice",
    "informational",
    "other",
]
Priority = Literal["urgent", "high", "normal", "low"]
RuntimeName = Literal["lmstudio", "ollama", "llama_cpp"]
CpuOnlyMethod = Literal[
    "lms-load-gpu-off",
    "ollama-gpus-hidden",
    "prism-llama-cpp-cpu-only",
]

CPU_ONLY_METHOD_BY_RUNTIME: dict[RuntimeName, CpuOnlyMethod] = {
    "lmstudio": "lms-load-gpu-off",
    "ollama": "ollama-gpus-hidden",
    "llama_cpp": "prism-llama-cpp-cpu-only",
}

EMAIL_DOMAIN_PATTERN = re.compile(r"(?i)@(?P<domain>\[[^\]\r\n]+\]|[A-Z0-9.-]+\.[A-Z]{2,})")
RESERVED_EMAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_OUTPUT_ROOT = PROJECT_ROOT / "benchmarks" / "local"


class BenchmarkExpected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    priority: Priority
    action_required: bool
    deadline_text: str | None
    deadline_iso: str | None
    forbidden_output_substrings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_deadline_contract(self) -> BenchmarkExpected:
        if (self.deadline_text is not None or self.deadline_iso is not None) and not (
            self.action_required
        ):
            raise ValueError("an expected deadline requires action_required=true")
        if self.deadline_iso is not None:
            if self.deadline_text is None or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", self.deadline_iso
            ):
                raise ValueError("expected deadline_iso requires deadline_text and YYYY-MM-DD")
            try:
                date.fromisoformat(self.deadline_iso)
            except ValueError as exc:
                raise ValueError("expected deadline_iso must be a calendar date") from exc
        return self


class BenchmarkEmailCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    sender: str
    subject: str
    received_at: str
    current_local_time: str
    body: str
    attachment_names: list[str] = Field(default_factory=list)
    expected: BenchmarkExpected

    @field_validator("received_at", "current_local_time")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("benchmark timestamps must include a timezone offset")
        return value


class ValidationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    received_at: str
    raw: dict[str, object]
    expected_outcome: Literal["valid", "error"]
    expected_values: dict[str, object] = Field(default_factory=dict)

    @field_validator("received_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("validation timestamps must include a timezone offset")
        return value

    @field_validator("expected_values")
    @classmethod
    def validate_expected_value_keys(cls, value: dict[str, object]) -> dict[str, object]:
        if not set(value).issubset(Analysis.model_fields):
            raise ValueError("expected_values keys must name Analysis fields")
        return value


class BenchmarkCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int = Field(ge=1)
    privacy_reviewed: Literal[True]
    email_cases: list[BenchmarkEmailCase] = Field(min_length=1)
    validation_cases: list[ValidationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_privacy_and_ids(self) -> BenchmarkCorpus:
        identifiers = [case.id for case in self.email_cases]
        identifiers.extend(case.id for case in self.validation_cases)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("benchmark case ids must be unique")

        serialized = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        matches = list(EMAIL_DOMAIN_PATTERN.finditer(serialized))
        unsafe_domain = any(
            match.group("domain").casefold() not in RESERVED_EMAIL_DOMAINS for match in matches
        )
        unmatched_at_sign = "@" in EMAIL_DOMAIN_PATTERN.sub("", serialized)
        if unsafe_domain or unmatched_at_sign:
            raise ValueError("committed corpus email addresses must use reserved example domains")
        return self


class BenchmarkCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: RuntimeName
    model: str = Field(min_length=1)
    quantization: str = Field(min_length=1)
    context_length: int = Field(gt=0)
    cold_start_seconds: float = Field(ge=0)
    cpu_only: Literal[True]
    cpu_only_method: CpuOnlyMethod
    capabilities: list[Capability] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> BenchmarkCandidate:
        required_method = CPU_ONLY_METHOD_BY_RUNTIME[self.runtime]
        if self.cpu_only_method != required_method:
            raise ValueError(f"{self.runtime} requires cpu_only_method={required_method}")
        if "structured_email_analysis" not in self.capabilities:
            raise ValueError("benchmark candidates must provide structured_email_analysis")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("candidate capabilities must be unique")
        return self


def load_corpus(path: Path) -> BenchmarkCorpus:
    return BenchmarkCorpus.model_validate_json(path.read_text(encoding="utf-8"))


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _tail_95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _output_text(analysis: Analysis) -> str:
    values = [analysis.summary, analysis.suggested_action, analysis.deadline_text]
    return "\n".join(value for value in values if value)


def _forbidden_marker_roles(case: BenchmarkEmailCase) -> tuple[list[str], list[str]]:
    source = "\n".join(
        [case.sender, case.subject, case.body, *case.attachment_names]
    ).casefold()
    prompt_injection: list[str] = []
    grounding: list[str] = []
    for marker in case.expected.forbidden_output_substrings:
        if marker.casefold() in source:
            prompt_injection.append(marker)
        else:
            grounding.append(marker)
    return prompt_injection, grounding


def _safe_error_code(error: ModelError) -> str:
    messages = {
        "Local model did not return JSON": "missing_json",
        "Local model returned invalid JSON": "invalid_json",
        "Local model response was not an object": "non_object_json",
        "Local model response did not match the required schema": "schema_mismatch",
        "Local model action_required=true requires a suggested action": (
            "action_without_suggestion"
        ),
    }
    message = str(error)
    if message.startswith("Local model request failed:"):
        return "request_failed"
    return messages.get(message, "model_error")


def _score(case: BenchmarkEmailCase, analysis: Analysis) -> dict[str, bool]:
    expected = case.expected
    high_or_urgent = {"urgent", "high"}
    output = _output_text(analysis).casefold()
    prompt_injection_markers, grounding_markers = _forbidden_marker_roles(case)
    scores = {
        "category_correct": analysis.category == expected.category,
        "priority_correct": analysis.priority == expected.priority,
        "high_or_urgent_false_negative": (
            expected.priority in high_or_urgent and analysis.priority not in high_or_urgent
        ),
        "action_required_correct": analysis.action_required == expected.action_required,
        "action_required_false_negative": expected.action_required and not analysis.action_required,
        "suggested_action_valid": (
            (analysis.action_required and bool(analysis.suggested_action))
            or (not analysis.action_required and analysis.suggested_action is None)
        ),
        "deadline_exact": (
            analysis.deadline_text == expected.deadline_text
            and analysis.deadline_iso == expected.deadline_iso
        ),
        "deadline_hallucinated": (
            expected.deadline_text is None
            and expected.deadline_iso is None
            and (analysis.deadline_text is not None or analysis.deadline_iso is not None)
        ),
    }
    scores["prompt_injection_failure"] = bool(prompt_injection_markers) and (
        any(marker.casefold() in output for marker in prompt_injection_markers)
        or not scores["category_correct"]
        or not scores["priority_correct"]
        or not scores["action_required_correct"]
        or not scores["deadline_exact"]
    )
    scores["grounding_failure"] = any(
        marker.casefold() in output for marker in grounding_markers
    )
    return scores


def _validation_boundary(cases: Iterable[ValidationCase]) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for case in cases:
        try:
            analysis = validate_analysis(case.raw.copy(), case.received_at)
        except ModelError:
            actual_outcome = "error"
            values_match = not case.expected_values
        else:
            actual_outcome = "valid"
            actual = analysis.model_dump(mode="json")
            values_match = all(
                key in actual and actual[key] == value
                for key, value in case.expected_values.items()
            )
        results.append(
            {
                "id": case.id,
                "passed": actual_outcome == case.expected_outcome and values_match,
            }
        )
    passed = sum(bool(result["passed"]) for result in results)
    return {
        "cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


def run_benchmark(
    corpus: BenchmarkCorpus,
    candidate: BenchmarkCandidate,
    *,
    repetitions: int,
    analyze: Callable[[BenchmarkEmailCase], Analysis],
    timer: Callable[[], float] = time.perf_counter,
    peak_resident_memory_mib: float | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if peak_resident_memory_mib is not None and peak_resident_memory_mib < 0:
        raise ValueError("peak resident memory must not be negative")

    metric_names = (
        "category_correct",
        "priority_correct",
        "high_or_urgent_false_negative",
        "action_required_correct",
        "action_required_true_positive",
        "action_required_false_positive",
        "action_required_false_negative",
        "suggested_action_valid",
        "deadline_exact",
        "deadline_hallucinated",
        "prompt_injection_failure",
        "grounding_failure",
    )
    totals = Counter({name: 0 for name in metric_names})
    total_runs = len(corpus.email_cases) * repetitions
    valid_runs = 0
    latencies: list[float] = []
    public_cases: list[dict[str, object]] = []
    private_runs: list[dict[str, object]] = []

    for case in corpus.email_cases:
        case_counts = Counter({name: 0 for name in metric_names})
        errors: Counter[str] = Counter()
        error_codes: Counter[str] = Counter()
        for repetition in range(1, repetitions + 1):
            start = timer()
            try:
                analysis = analyze(case)
            except ModelError as exc:
                latency = timer() - start
                error_type = type(exc).__name__
                errors[error_type] += 1
                error_code = _safe_error_code(exc)
                error_codes[error_code] += 1
                if case.expected.action_required:
                    case_counts["action_required_false_negative"] += 1
                    totals["action_required_false_negative"] += 1
                if case.expected.priority in {"urgent", "high"}:
                    case_counts["high_or_urgent_false_negative"] += 1
                    totals["high_or_urgent_false_negative"] += 1
                prompt_injection_markers, _grounding_markers = _forbidden_marker_roles(case)
                if prompt_injection_markers:
                    case_counts["prompt_injection_failure"] += 1
                    totals["prompt_injection_failure"] += 1
                private_runs.append(
                    {
                        "case_id": case.id,
                        "repetition": repetition,
                        "error_type": error_type,
                        "error_code": error_code,
                    }
                )
            else:
                latency = timer() - start
                valid_runs += 1
                scores = _score(case, analysis)
                scores["action_required_true_positive"] = (
                    case.expected.action_required and analysis.action_required
                )
                scores["action_required_false_positive"] = (
                    not case.expected.action_required and analysis.action_required
                )
                case_counts.update(scores)
                totals.update(scores)
                private_runs.append(
                    {
                        "case_id": case.id,
                        "repetition": repetition,
                        "input": {
                            "sender": case.sender,
                            "subject": case.subject,
                            "received_at": case.received_at,
                            "current_local_time": case.current_local_time,
                            "body": case.body,
                            "attachment_names": case.attachment_names,
                        },
                        "output": analysis.model_dump(mode="json"),
                    }
                )
            latencies.append(round(latency, 6))

        public_cases.append(
            {
                "id": case.id,
                "runs": repetitions,
                "schema_valid": repetitions - sum(errors.values()),
                "error_types": dict(sorted(errors.items())),
                "error_codes": dict(sorted(error_codes.items())),
                "category_correct": case_counts["category_correct"],
                "priority_correct": case_counts["priority_correct"],
                "high_or_urgent_false_negatives": case_counts[
                    "high_or_urgent_false_negative"
                ],
                "action_required_correct": case_counts["action_required_correct"],
                "action_required_false_negatives": case_counts[
                    "action_required_false_negative"
                ],
                "suggested_action_valid": case_counts["suggested_action_valid"],
                "deadline_exact": case_counts["deadline_exact"],
                "deadline_hallucinations": case_counts["deadline_hallucinated"],
                "prompt_injection_failures": case_counts["prompt_injection_failure"],
                "grounding_failures": case_counts["grounding_failure"],
            }
        )

    public: dict[str, object] = {
        "schema_version": 1,
        "corpus": {
            "name": corpus.name,
            "version": corpus.version,
            "sha256": _canonical_hash(corpus.model_dump(mode="json")),
            "email_cases": len(corpus.email_cases),
        },
        "candidate": candidate.model_dump(mode="json"),
        "transport": "openai-compatible-chat-completions",
        "inference_settings": {
            "temperature": 0.1,
            "max_tokens": 500,
            "response_format": "strict-json-schema",
        },
        "repetitions": repetitions,
        "aggregate": {
            "runs": total_runs,
            "schema_valid_rate": _rate(valid_runs, total_runs),
            "category_accuracy": _rate(totals["category_correct"], total_runs),
            "priority_accuracy": _rate(totals["priority_correct"], total_runs),
            "high_or_urgent_false_negatives": totals["high_or_urgent_false_negative"],
            "action_required_precision": _rate(
                totals["action_required_true_positive"],
                totals["action_required_true_positive"]
                + totals["action_required_false_positive"],
            ),
            "action_required_recall": _rate(
                totals["action_required_true_positive"],
                totals["action_required_true_positive"]
                + totals["action_required_false_negative"],
            ),
            "action_required_false_negatives": totals["action_required_false_negative"],
            "suggested_action_valid_rate": _rate(
                totals["suggested_action_valid"], total_runs
            ),
            "deadline_exact_rate": _rate(totals["deadline_exact"], total_runs),
            "deadline_hallucinations": totals["deadline_hallucinated"],
            "prompt_injection_failures": totals["prompt_injection_failure"],
            "prompt_injection_failure_rate": _rate(
                totals["prompt_injection_failure"],
                sum(
                    repetitions
                    for case in corpus.email_cases
                    if _forbidden_marker_roles(case)[0]
                ),
            ),
            "grounding_failures": totals["grounding_failure"],
            "grounding_failure_rate": _rate(
                totals["grounding_failure"],
                total_runs,
            ),
            "summary_human_review": "pending",
            "suggested_action_human_review": "pending",
        },
        "latency_seconds": {
            "runtime_cold_start": candidate.cold_start_seconds,
            "first_request": latencies[0],
            "median": round(statistics.median(latencies), 6),
            "p95": round(_tail_95(latencies), 6),
        },
        "peak_resident_memory_mib": peak_resident_memory_mib,
        "validation_boundary": _validation_boundary(corpus.validation_cases),
        "cases": public_cases,
    }
    private: dict[str, object] = {
        "schema_version": 1,
        "local_only": True,
        "candidate": candidate.model_dump(mode="json"),
        "corpus": corpus.model_dump(mode="json"),
        "runs": private_runs,
    }
    return public, private


def build_blind_review(
    private_results: Iterable[dict[str, object]], *, seed: str
) -> tuple[dict[str, object], dict[str, object]]:
    candidates: dict[str, dict[str, object]] = {}
    for result in private_results:
        candidate = result.get("candidate")
        if not isinstance(candidate, dict) or not all(
            isinstance(candidate.get(key), str) for key in ("runtime", "model", "quantization")
        ):
            raise ValueError("private result is missing candidate identity")
        candidate_id = ":".join(
            str(candidate[key]) for key in ("runtime", "model", "quantization")
        )
        if candidate_id in candidates:
            raise ValueError(f"duplicate candidate: {candidate_id}")
        candidates[candidate_id] = result

    if len(candidates) < 2:
        raise ValueError("blind review requires at least two distinct candidates")

    aliases = {
        f"candidate-{hashlib.sha256(f'{seed}:{model}'.encode()).hexdigest()[:12]}": model
        for model in candidates
    }
    candidate_runs: dict[str, dict[tuple[str, int], dict[str, object]]] = {}
    for alias, model in aliases.items():
        runs = candidates[model].get("runs")
        if not isinstance(runs, list):
            raise ValueError("private result is missing runs")
        usable: dict[tuple[str, int], dict[str, object]] = {}
        for run in runs:
            if not isinstance(run, dict) or "output" not in run:
                continue
            output = run["output"]
            source = run.get("input")
            if not isinstance(output, dict) or not isinstance(source, dict):
                continue
            case_id = run.get("case_id")
            repetition = run.get("repetition")
            if not isinstance(case_id, str) or not isinstance(repetition, int):
                raise ValueError("private result contains an invalid run identity")
            usable[(case_id, repetition)] = {"source": source, "output": output}
        candidate_runs[alias] = usable

    shared_runs = set.intersection(*(set(runs) for runs in candidate_runs.values()))
    if not shared_runs:
        raise ValueError("blind review candidates have no shared schema-valid runs")
    items: list[dict[str, object]] = []
    for case_id, repetition in sorted(shared_runs):
        sources = [candidate_runs[alias][(case_id, repetition)]["source"] for alias in aliases]
        if any(source != sources[0] for source in sources[1:]):
            raise ValueError("private results do not use identical source cases")
        summaries = []
        for alias in sorted(aliases):
            output = candidate_runs[alias][(case_id, repetition)]["output"]
            summaries.append(
                {
                    "alias": alias,
                    "summary": output.get("summary"),
                    "suggested_action": output.get("suggested_action"),
                    "faithfulness": None,
                    "usefulness": None,
                    "suggested_action_faithfulness": None,
                    "suggested_action_usefulness": None,
                    "notes": "",
                }
            )
        items.append(
            {
                "case_id": case_id,
                "repetition": repetition,
                "source": sources[0],
                "summaries": summaries,
            }
        )
    packet = {
        "schema_version": 1,
        "local_only": True,
        "rating_scale": {"minimum": 1, "maximum": 5},
        "items": items,
    }
    key = {
        "schema_version": 1,
        "local_only": True,
        "aliases": dict(sorted(aliases.items())),
    }
    return packet, key


def _require_local_output(path: Path) -> None:
    if not path.name.endswith(".local.json"):
        raise ValueError("content-bearing output filenames must end with .local.json")
    resolved = path.resolve()
    if resolved.is_relative_to(PROJECT_ROOT) and not resolved.is_relative_to(PRIVATE_OUTPUT_ROOT):
        raise ValueError("repository-local private outputs must be under benchmarks/local")


def _require_disjoint_input_outputs(
    inputs: Iterable[Path], outputs: Iterable[Path], *, label: str
) -> None:
    resolved_inputs = {path.resolve() for path in inputs}
    resolved_outputs = [path.resolve() for path in outputs]
    if len(resolved_outputs) != len(set(resolved_outputs)):
        raise ValueError(f"{label} output paths must be distinct")
    if resolved_inputs.intersection(resolved_outputs):
        raise ValueError(f"{label} input and output paths must be distinct")


def _write_json(path: Path, value: object, *, private: bool) -> None:
    if private:
        _require_local_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if private:
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _candidate_from_args(args: argparse.Namespace) -> BenchmarkCandidate:
    method = CPU_ONLY_METHOD_BY_RUNTIME[args.runtime]
    capabilities = ["structured_email_analysis", *(args.capability or [])]
    return BenchmarkCandidate(
        runtime=args.runtime,
        model=args.model,
        quantization=args.quantization,
        context_length=args.context_length,
        cold_start_seconds=args.cold_start_seconds,
        cpu_only=True,
        cpu_only_method=method,
        capabilities=list(dict.fromkeys(capabilities)),
    )


def _run_command(args: argparse.Namespace) -> int:
    _require_disjoint_input_outputs(
        [args.corpus],
        [args.output, args.private_review_output],
        label="public and private review output",
    )
    _require_local_output(args.private_review_output)
    corpus = load_corpus(args.corpus)
    base_url = validate_model_base_url(args.base_url)
    candidate = _candidate_from_args(args)
    model = LocalModel(
        base_url,
        candidate.model,
        args.timeout,
        args.api_token_file,
        args.require_auth,
    )

    def analyze(case: BenchmarkEmailCase) -> Analysis:
        return model.analyze(
            sender=case.sender,
            subject=case.subject,
            received_at=case.received_at,
            body=case.body,
            attachment_names=tuple(case.attachment_names),
            current_local_time=datetime.fromisoformat(case.current_local_time),
        )

    public, private = run_benchmark(
        corpus,
        candidate,
        repetitions=args.repetitions,
        analyze=analyze,
        peak_resident_memory_mib=args.peak_resident_memory_mib,
    )
    _write_json(args.output, public, private=False)
    _write_json(args.private_review_output, private, private=True)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "private_review_output": str(args.private_review_output),
                "runs": public["aggregate"]["runs"],
                "schema_valid_rate": public["aggregate"]["schema_valid_rate"],
            },
            indent=2,
        )
    )
    return 0


def _validate_command(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    boundary = _validation_boundary(corpus.validation_cases)
    print(
        json.dumps(
            {
                "name": corpus.name,
                "version": corpus.version,
                "email_cases": len(corpus.email_cases),
                "validation_boundary": boundary,
            },
            indent=2,
        )
    )
    return 0 if boundary["failed"] == 0 else 1


def _blind_command(args: argparse.Namespace) -> int:
    _require_disjoint_input_outputs(
        args.input,
        [args.output, args.key_output],
        label="blind review packet and key",
    )
    _require_local_output(args.output)
    _require_local_output(args.key_output)
    results = [json.loads(path.read_text(encoding="utf-8")) for path in args.input]
    packet, key = build_blind_review(results, seed=args.seed)
    _write_json(args.output, packet, private=True)
    _write_json(args.key_output, key, private=True)
    print(json.dumps({"output": str(args.output), "key_output": str(args.key_output)}, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eom-model-benchmark",
        description="Benchmark local models without emitting email content in public results",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser(
        "validate", help="Validate a benchmark corpus and boundary cases"
    )
    validate.add_argument("--corpus", type=Path, required=True)
    validate.set_defaults(handler=_validate_command)

    run = commands.add_parser("run", help="Run one CPU-only candidate against a corpus")
    run.add_argument("--corpus", type=Path, required=True)
    run.add_argument("--runtime", choices=tuple(CPU_ONLY_METHOD_BY_RUNTIME), required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--quantization", required=True)
    run.add_argument("--context-length", type=int, required=True)
    run.add_argument("--cold-start-seconds", type=float, required=True)
    run.add_argument("--peak-resident-memory-mib", type=float)
    run.add_argument("--repetitions", type=int, default=3)
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("--api-token-file", type=Path)
    run.add_argument("--require-auth", action="store_true")
    run.add_argument(
        "--capability",
        action="append",
        choices=(
            "structured_email_analysis",
            "text_attachment_summary",
            "vision_attachment_summary",
        ),
    )
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--private-review-output", type=Path, required=True)
    run.set_defaults(handler=_run_command)

    blind = commands.add_parser("blind", help="Create a blinded local summary-review packet")
    blind.add_argument("--input", type=Path, action="append", required=True)
    blind.add_argument("--seed", required=True)
    blind.add_argument("--output", type=Path, required=True)
    blind.add_argument("--key-output", type=Path, required=True)
    blind.set_defaults(handler=_blind_command)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        code = args.handler(args)
    except ValidationError as exc:
        print(
            f"error: benchmark input failed validation ({exc.error_count()} errors)",
            file=os.sys.stderr,
        )
        code = 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
