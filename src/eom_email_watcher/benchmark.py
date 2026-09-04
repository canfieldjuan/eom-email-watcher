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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .config import (
    DEFAULT_CONFIG,
    load_config,
    secure_runtime_paths,
    validate_model_base_url,
)
from .db import Store
from .gmail import GmailError, GmailGateway, MessageUnavailable
from .mailbox import DEFAULT_MAIL_PROVIDER, MailboxAccountUnavailable
from .mime import extract_body
from .model import (
    DOCUMENT_SUMMARY_PROMPT,
    SYSTEM_PROMPT,
    Analysis,
    DocumentSummaryInference,
    LocalModel,
    ModelError,
    validate_analysis,
)
from .runtime import load_mailbox_account

Capability = Literal[
    "structured_email_analysis",
    "text_document_summary",
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
DocumentTier = Literal["short", "long"]
RuntimeName = Literal["lmstudio", "ollama", "llama_cpp"]
CpuOnlyMethod = Literal[
    "lms-load-gpu-off",
    "ollama-gpus-hidden",
    "prism-llama-cpp-cpu-only",
]
GpuOffloadMethod = Literal[
    "lms-load-gpu-max",
    "ollama-cuda-visible-devices",
]

CPU_ONLY_METHOD_BY_RUNTIME: dict[RuntimeName, CpuOnlyMethod] = {
    "lmstudio": "lms-load-gpu-off",
    "ollama": "ollama-gpus-hidden",
    "llama_cpp": "prism-llama-cpp-cpu-only",
}
GPU_OFFLOAD_METHOD_BY_RUNTIME: dict[RuntimeName, GpuOffloadMethod] = {
    "lmstudio": "lms-load-gpu-max",
    "ollama": "ollama-cuda-visible-devices",
}

EMAIL_DOMAIN_PATTERN = re.compile(r"(?i)@(?P<domain>\[[^\]\r\n]+\]|[A-Z0-9.-]+\.[A-Z]{2,})")
RESERVED_EMAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIVATE_OUTPUT_ROOT = PROJECT_ROOT / "benchmarks" / "local"
MAX_DOCUMENT_WORDS = 20_000
MAX_DOCUMENT_SOURCE_CHARS = 200_000
MAX_DOCUMENT_EXPANDED_CHARS = 2_000_000


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
    def validate_privacy_and_ids(self, info: ValidationInfo) -> BenchmarkCorpus:
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
        allow_private_content = bool(
            info.context and info.context.get("allow_private_content") is True
        )
        if not allow_private_content and (unsafe_domain or unmatched_at_sign):
            raise ValueError("committed corpus email addresses must use reserved example domains")
        return self


class BenchmarkDocumentExpected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_max_words: int = Field(ge=25, le=1000)
    required_fact_terms: list[list[str]] = Field(min_length=1)
    forbidden_output_substrings: list[str] = Field(default_factory=list)

    @field_validator("required_fact_terms")
    @classmethod
    def validate_fact_groups(cls, value: list[list[str]]) -> list[list[str]]:
        if any(not group or any(not term.strip() for term in group) for group in value):
            raise ValueError("required fact groups and terms must not be empty")
        return value


class BenchmarkDocumentCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    tier: DocumentTier
    title: str = Field(min_length=1)
    target_words: int = Field(ge=100, le=MAX_DOCUMENT_WORDS)
    filler_text: str = Field(min_length=1)
    fact_segments: list[str] = Field(min_length=1, max_length=100)
    expected: BenchmarkDocumentExpected

    @model_validator(mode="after")
    def validate_document_contract(self) -> BenchmarkDocumentCase:
        source_characters = len(self.filler_text) + sum(
            len(segment) for segment in self.fact_segments
        )
        if source_characters > MAX_DOCUMENT_SOURCE_CHARS:
            raise ValueError("document benchmark source text is too large")
        document = expand_document(self)
        normalized = document.casefold()
        for group in self.expected.required_fact_terms:
            if not all(term.casefold() in normalized for term in group):
                raise ValueError("every required fact term must exist in the generated document")
        if any(
            marker.casefold() not in normalized
            for marker in self.expected.forbidden_output_substrings
        ):
            raise ValueError("every forbidden output marker must exist in the generated document")
        return self


class DocumentBenchmarkCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int = Field(ge=1)
    privacy_reviewed: Literal[True]
    document_cases: list[BenchmarkDocumentCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ids_and_tiers(self) -> DocumentBenchmarkCorpus:
        identifiers = [case.id for case in self.document_cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("document benchmark case ids must be unique")
        if {case.tier for case in self.document_cases} != {"short", "long"}:
            raise ValueError("document benchmark corpus must include short and long cases")
        return self


class BenchmarkCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: RuntimeName
    model: str = Field(min_length=1)
    quantization: str = Field(min_length=1)
    context_length: int = Field(gt=0)
    cold_start_seconds: float | None = Field(default=None, ge=0)
    cpu_only: bool
    cpu_only_method: CpuOnlyMethod | None = None
    gpu_offload_method: GpuOffloadMethod | None = None
    capabilities: list[Capability] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> BenchmarkCandidate:
        if self.cpu_only:
            required_method = CPU_ONLY_METHOD_BY_RUNTIME[self.runtime]
            if self.cpu_only_method != required_method or self.gpu_offload_method is not None:
                raise ValueError(f"{self.runtime} CPU requires cpu_only_method={required_method}")
        else:
            required_method = GPU_OFFLOAD_METHOD_BY_RUNTIME.get(self.runtime)
            if required_method is None:
                raise ValueError(f"{self.runtime} GPU benchmarking is unsupported")
            if self.cpu_only_method is not None or self.gpu_offload_method != required_method:
                raise ValueError(
                    f"{self.runtime} GPU requires gpu_offload_method={required_method}"
                )
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("candidate capabilities must be unique")
        return self


def _require_private_file_permissions(path: Path, *, label: str) -> None:
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{label} files must not be accessible by group or other users")


def load_corpus(path: Path) -> BenchmarkCorpus:
    allow_private_content = path.name.endswith(".local.json")
    if allow_private_content:
        _require_local_output(path)
        _require_private_file_permissions(path, label="private corpus")
    return BenchmarkCorpus.model_validate_json(
        path.read_text(encoding="utf-8"),
        context={"allow_private_content": allow_private_content},
    )


def load_document_corpus(path: Path) -> DocumentBenchmarkCorpus:
    if path.name.endswith(".local.json"):
        _require_local_output(path)
        _require_private_file_permissions(path, label="private document corpus")
    return DocumentBenchmarkCorpus.model_validate_json(path.read_text(encoding="utf-8"))


def expand_document(case: BenchmarkDocumentCase) -> str:
    anchor_words = sum(len(segment.split()) for segment in case.fact_segments)
    padding_words = case.target_words - anchor_words
    if padding_words < 0:
        raise ValueError("target_words must fit all fact segments")
    filler_words = case.filler_text.split()
    if not filler_words:
        raise ValueError("filler_text must contain words")
    expanded_upper_bound = (
        sum(len(segment) for segment in case.fact_segments)
        + padding_words * (max(len(word) for word in filler_words) + 1)
        + (len(case.fact_segments) + 1) * 4
    )
    if expanded_upper_bound > MAX_DOCUMENT_EXPANDED_CHARS:
        raise ValueError("expanded document exceeds the character limit")
    gap_count = len(case.fact_segments) + 1
    base, remainder = divmod(padding_words, gap_count)
    parts: list[str] = []
    filler_offset = 0
    for index in range(gap_count):
        length = base + (1 if index < remainder else 0)
        parts.append(
            " ".join(
                filler_words[(filler_offset + offset) % len(filler_words)]
                for offset in range(length)
            )
        )
        filler_offset += length
        if index < len(case.fact_segments):
            parts.append(case.fact_segments[index])
    return "\n\n".join(part for part in parts if part)


def build_private_inbox_draft(
    gmail: GmailGateway,
    model: LocalModel,
    base_corpus: BenchmarkCorpus,
    *,
    senders: frozenset[str],
    limit: int,
    body_char_limit: int,
    current_local_time: datetime,
) -> dict[str, object]:
    if current_local_time.tzinfo is None:
        raise ValueError("current_local_time must include a timezone offset")
    cases: list[dict[str, object]] = []
    for message_id in gmail.iter_recent_inbox_message_ids(senders, page_size=limit):
        try:
            metadata = gmail.metadata(message_id)
            if metadata.sender not in senders or "INBOX" not in metadata.labels:
                continue
            body, attachment_names, _attachments = extract_body(
                gmail.full_payload(message_id), body_char_limit
            )
        except MessageUnavailable:
            continue
        analysis = model.analyze(
            sender=metadata.sender,
            subject=metadata.subject,
            received_at=metadata.received_at,
            body=body,
            attachment_names=attachment_names,
            current_local_time=current_local_time,
        )
        cases.append(
            {
                "id": f"inbox-{len(cases) + 1:03d}",
                "sender": metadata.sender,
                "subject": metadata.subject,
                "received_at": metadata.received_at,
                "current_local_time": current_local_time.isoformat(),
                "body": body,
                "attachment_names": list(attachment_names),
                "expected": {
                    "category": analysis.category,
                    "priority": analysis.priority,
                    "action_required": analysis.action_required,
                    "deadline_text": analysis.deadline_text,
                    "deadline_iso": analysis.deadline_iso,
                    "forbidden_output_substrings": [],
                },
            }
        )
        if len(cases) == limit:
            break
    if not cases:
        raise ValueError("no available inbox messages matched the configured watchlist")
    return {
        "schema_version": 1,
        "local_only": True,
        "labels_reviewed": False,
        "draft_label_model": model.model,
        "labeling_instructions": (
            "Review every expected object against its source email, correct it, then set "
            "labels_reviewed to true before finalizing. Draft model labels are not ground truth."
        ),
        "corpus": {
            "name": "email-analysis-private-inbox",
            "version": 1,
            "privacy_reviewed": True,
            "email_cases": cases,
            "validation_cases": [
                case.model_dump(mode="json") for case in base_corpus.validation_cases
            ],
        },
    }


def finalize_private_inbox_draft(draft: object) -> BenchmarkCorpus:
    if not isinstance(draft, dict) or draft.get("local_only") is not True:
        raise ValueError("private inbox draft must declare local_only=true")
    if draft.get("labels_reviewed") is not True:
        raise ValueError("private inbox draft requires labels_reviewed=true")
    corpus = draft.get("corpus")
    if not isinstance(corpus, dict):
        raise ValueError("private inbox draft is missing its corpus")
    return BenchmarkCorpus.model_validate(
        corpus,
        context={"allow_private_content": True},
    )


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
    source = "\n".join([case.sender, case.subject, case.body, *case.attachment_names]).casefold()
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
    scores["grounding_failure"] = any(marker.casefold() in output for marker in grounding_markers)
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
    if "structured_email_analysis" not in candidate.capabilities:
        raise ValueError("email benchmark requires structured_email_analysis capability")
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
    prompt_injection_evaluated = any(
        _forbidden_marker_roles(case)[0] for case in corpus.email_cases
    )
    grounding_evaluated = any(_forbidden_marker_roles(case)[1] for case in corpus.email_cases)

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

        public_case: dict[str, object] = {
            "id": case.id,
            "runs": repetitions,
            "schema_valid": repetitions - sum(errors.values()),
            "error_types": dict(sorted(errors.items())),
            "error_codes": dict(sorted(error_codes.items())),
            "category_correct": case_counts["category_correct"],
            "priority_correct": case_counts["priority_correct"],
            "high_or_urgent_false_negatives": case_counts["high_or_urgent_false_negative"],
            "action_required_correct": case_counts["action_required_correct"],
            "action_required_false_negatives": case_counts["action_required_false_negative"],
            "suggested_action_valid": case_counts["suggested_action_valid"],
            "deadline_exact": case_counts["deadline_exact"],
            "deadline_hallucinations": case_counts["deadline_hallucinated"],
        }
        if prompt_injection_evaluated:
            public_case["prompt_injection_failures"] = case_counts["prompt_injection_failure"]
        if grounding_evaluated:
            public_case["grounding_failures"] = case_counts["grounding_failure"]
        public_cases.append(public_case)

    public: dict[str, object] = {
        "schema_version": 1,
        "corpus": {
            "name": corpus.name,
            "version": corpus.version,
            "sha256": _canonical_hash(corpus.model_dump(mode="json")),
            "email_cases": len(corpus.email_cases),
        },
        "candidate": candidate.model_dump(mode="json"),
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
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
                totals["action_required_true_positive"] + totals["action_required_false_positive"],
            ),
            "action_required_recall": _rate(
                totals["action_required_true_positive"],
                totals["action_required_true_positive"] + totals["action_required_false_negative"],
            ),
            "action_required_false_negatives": totals["action_required_false_negative"],
            "suggested_action_valid_rate": _rate(totals["suggested_action_valid"], total_runs),
            "deadline_exact_rate": _rate(totals["deadline_exact"], total_runs),
            "deadline_hallucinations": totals["deadline_hallucinated"],
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
    aggregate = public["aggregate"]
    assert isinstance(aggregate, dict)
    if prompt_injection_evaluated:
        aggregate["prompt_injection_failures"] = totals["prompt_injection_failure"]
        aggregate["prompt_injection_failure_rate"] = _rate(
            totals["prompt_injection_failure"],
            sum(repetitions for case in corpus.email_cases if _forbidden_marker_roles(case)[0]),
        )
    if grounding_evaluated:
        aggregate["grounding_failures"] = totals["grounding_failure"]
        aggregate["grounding_failure_rate"] = _rate(totals["grounding_failure"], total_runs)
    private: dict[str, object] = {
        "schema_version": 1,
        "local_only": True,
        "candidate": candidate.model_dump(mode="json"),
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "corpus": corpus.model_dump(mode="json"),
        "runs": private_runs,
    }
    return public, private


def _number_summary(values: list[int | float]) -> dict[str, int | float | None]:
    if not values:
        return {"minimum": None, "median": None, "maximum": None}
    return {
        "minimum": min(values),
        "median": round(statistics.median(values), 6),
        "maximum": max(values),
    }


def _document_score(
    case: BenchmarkDocumentCase, inference: DocumentSummaryInference
) -> dict[str, object]:
    output = inference.output.summary.casefold()
    forbidden_markers = case.expected.forbidden_output_substrings
    fact_matches = [
        all(term.casefold() in output for term in group)
        for group in case.expected.required_fact_terms
    ]
    return {
        "fact_matches": fact_matches,
        "forbidden_output_failure": (
            any(marker.casefold() in output for marker in forbidden_markers)
            if forbidden_markers
            else None
        ),
        "summary_word_limit_passed": (
            len(inference.output.summary.split()) <= case.expected.summary_max_words
        ),
    }


def run_document_benchmark(
    corpus: DocumentBenchmarkCorpus,
    candidate: BenchmarkCandidate,
    *,
    repetitions: int,
    summarize: Callable[[BenchmarkDocumentCase, str], DocumentSummaryInference],
    timer: Callable[[], float] = time.perf_counter,
) -> tuple[dict[str, object], dict[str, object]]:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    if "text_document_summary" not in candidate.capabilities:
        raise ValueError("document benchmark requires text_document_summary capability")

    total_runs = len(corpus.document_cases) * repetitions
    valid_runs = 0
    fact_groups_total = 0
    fact_groups_matched = 0
    forbidden_checks = 0
    forbidden_failures = 0
    word_limit_passes = 0
    all_latencies: list[float] = []
    all_prompt_tokens: list[int] = []
    public_cases: list[dict[str, object]] = []
    private_runs: list[dict[str, object]] = []
    tier_totals: dict[str, Counter[str]] = {tier: Counter() for tier in ("short", "long")}
    tier_latencies: dict[str, list[float]] = {tier: [] for tier in ("short", "long")}
    tier_prompt_tokens: dict[str, list[int]] = {tier: [] for tier in ("short", "long")}

    for case in corpus.document_cases:
        document = expand_document(case)
        case_fact_total = len(case.expected.required_fact_terms) * repetitions
        fact_groups_total += case_fact_total
        tier_totals[case.tier]["runs"] += repetitions
        tier_totals[case.tier]["fact_groups_total"] += case_fact_total
        case_valid = 0
        case_fact_matches = 0
        case_forbidden_checks = 0
        case_forbidden_failures = 0
        case_word_limit_passes = 0
        case_prompt_tokens: list[int] = []
        case_latencies: list[float] = []
        errors: Counter[str] = Counter()
        error_codes: Counter[str] = Counter()

        for repetition in range(1, repetitions + 1):
            start = timer()
            try:
                inference = summarize(case, document)
            except ModelError as exc:
                latency = timer() - start
                error_type = type(exc).__name__
                errors[error_type] += 1
                error_code = _safe_error_code(exc)
                error_codes[error_code] += 1
                if case.expected.forbidden_output_substrings:
                    case_forbidden_checks += 1
                    case_forbidden_failures += 1
                    forbidden_checks += 1
                    forbidden_failures += 1
                    tier_totals[case.tier]["forbidden_output_checks"] += 1
                    tier_totals[case.tier]["forbidden_output_failures"] += 1
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
                case_valid += 1
                tier_totals[case.tier]["schema_valid"] += 1
                scores = _document_score(case, inference)
                matched = sum(bool(value) for value in scores["fact_matches"])
                case_fact_matches += matched
                fact_groups_matched += matched
                tier_totals[case.tier]["fact_groups_matched"] += matched
                forbidden_failure = scores["forbidden_output_failure"]
                if forbidden_failure is not None:
                    case_forbidden_checks += 1
                    forbidden_checks += 1
                    tier_totals[case.tier]["forbidden_output_checks"] += 1
                    if forbidden_failure:
                        case_forbidden_failures += 1
                        forbidden_failures += 1
                        tier_totals[case.tier]["forbidden_output_failures"] += 1
                if scores["summary_word_limit_passed"]:
                    case_word_limit_passes += 1
                    word_limit_passes += 1
                    tier_totals[case.tier]["summary_word_limit_passes"] += 1
                if inference.prompt_tokens is not None:
                    case_prompt_tokens.append(inference.prompt_tokens)
                    all_prompt_tokens.append(inference.prompt_tokens)
                    tier_prompt_tokens[case.tier].append(inference.prompt_tokens)
                private_runs.append(
                    {
                        "case_id": case.id,
                        "repetition": repetition,
                        "input": {
                            "title": case.title,
                            "text": document,
                            "summary_max_words": case.expected.summary_max_words,
                        },
                        "output": inference.output.model_dump(mode="json"),
                        "usage": {
                            "prompt_tokens": inference.prompt_tokens,
                            "completion_tokens": inference.completion_tokens,
                        },
                    }
                )
            rounded_latency = round(latency, 6)
            case_latencies.append(rounded_latency)
            all_latencies.append(rounded_latency)
            tier_latencies[case.tier].append(rounded_latency)

        public_case: dict[str, object] = {
            "id": case.id,
            "tier": case.tier,
            "generated_words": len(document.split()),
            "runs": repetitions,
            "schema_valid": case_valid,
            "error_types": dict(sorted(errors.items())),
            "error_codes": dict(sorted(error_codes.items())),
            "fact_groups": case_fact_total,
            "fact_groups_matched": case_fact_matches,
            "fact_recall": _rate(case_fact_matches, case_fact_total),
            "summary_word_limit_passes": case_word_limit_passes,
            "prompt_tokens": _number_summary(case_prompt_tokens),
            "latency_seconds": _number_summary(case_latencies),
        }
        if case_forbidden_checks:
            public_case["forbidden_output_checks"] = case_forbidden_checks
            public_case["forbidden_output_failures"] = case_forbidden_failures
        public_cases.append(public_case)

    tiers: dict[str, dict[str, object]] = {}
    for tier, totals in tier_totals.items():
        tier_result: dict[str, object] = {
            "runs": totals["runs"],
            "schema_valid_rate": _rate(totals["schema_valid"], totals["runs"]),
            "fact_recall": _rate(totals["fact_groups_matched"], totals["fact_groups_total"]),
            "summary_word_limit_pass_rate": _rate(
                totals["summary_word_limit_passes"], totals["runs"]
            ),
            "prompt_tokens": _number_summary(tier_prompt_tokens[tier]),
            "latency_seconds": _number_summary(tier_latencies[tier]),
        }
        if totals["forbidden_output_checks"]:
            tier_result["forbidden_output_checks"] = totals["forbidden_output_checks"]
            tier_result["forbidden_output_failures"] = totals["forbidden_output_failures"]
        tiers[tier] = tier_result
    aggregate: dict[str, object] = {
        "runs": total_runs,
        "schema_valid_rate": _rate(valid_runs, total_runs),
        "fact_recall": _rate(fact_groups_matched, fact_groups_total),
        "summary_word_limit_pass_rate": _rate(word_limit_passes, total_runs),
        "summary_human_review": "pending",
    }
    if forbidden_checks:
        aggregate["forbidden_output_checks"] = forbidden_checks
        aggregate["forbidden_output_failures"] = forbidden_failures
    public: dict[str, object] = {
        "schema_version": 1,
        "corpus": {
            "name": corpus.name,
            "version": corpus.version,
            "sha256": _canonical_hash(corpus.model_dump(mode="json")),
            "document_cases": len(corpus.document_cases),
        },
        "candidate": candidate.model_dump(mode="json"),
        "prompt_sha256": hashlib.sha256(DOCUMENT_SUMMARY_PROMPT.encode("utf-8")).hexdigest(),
        "transport": "openai-compatible-chat-completions",
        "inference_settings": {
            "temperature": 0.1,
            "max_tokens": "min(2000, summary_max_words * 3 + 100)",
            "response_format": "strict-json-schema",
        },
        "repetitions": repetitions,
        "aggregate": aggregate,
        "prompt_tokens": _number_summary(all_prompt_tokens),
        "latency_seconds": _number_summary(all_latencies),
        "tiers": tiers,
        "cases": public_cases,
    }
    private: dict[str, object] = {
        "schema_version": 1,
        "local_only": True,
        "candidate": candidate.model_dump(mode="json"),
        "prompt_sha256": hashlib.sha256(DOCUMENT_SUMMARY_PROMPT.encode("utf-8")).hexdigest(),
        "corpus": corpus.model_dump(mode="json"),
        "runs": private_runs,
    }
    return public, private


def build_blind_review(
    private_results: Iterable[dict[str, object]], *, seed: str
) -> tuple[dict[str, object], dict[str, object]]:
    candidates: dict[str, dict[str, object]] = {}
    expected_actions: dict[str, bool] | None = None
    prompt_sha256: str | None = None
    for result in private_results:
        result_prompt_sha256 = result.get("prompt_sha256")
        if (
            not isinstance(result_prompt_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", result_prompt_sha256) is None
        ):
            raise ValueError("private result is missing a valid system prompt hash")
        if prompt_sha256 is None:
            prompt_sha256 = result_prompt_sha256
        elif result_prompt_sha256 != prompt_sha256:
            raise ValueError("private results do not use an identical system prompt")
        candidate = result.get("candidate")
        if not isinstance(candidate, dict):
            raise ValueError("private result is missing candidate identity")
        try:
            validated_candidate = BenchmarkCandidate.model_validate(candidate)
        except ValidationError as exc:
            raise ValueError("private result has invalid candidate identity") from exc
        execution_device = "cpu" if validated_candidate.cpu_only else "gpu"
        execution_method = (
            validated_candidate.cpu_only_method
            if validated_candidate.cpu_only
            else validated_candidate.gpu_offload_method
        )
        if execution_method is None:
            raise ValueError("private result is missing candidate execution method")
        candidate_id = ":".join(
            (
                validated_candidate.runtime,
                validated_candidate.model,
                validated_candidate.quantization,
                f"context-{validated_candidate.context_length}",
                execution_device,
                execution_method,
            )
        )
        if candidate_id in candidates:
            raise ValueError(f"duplicate candidate: {candidate_id}")
        candidates[candidate_id] = result

        corpus = result.get("corpus")
        cases = corpus.get("email_cases") if isinstance(corpus, dict) else None
        if not isinstance(cases, list):
            raise ValueError("private result is missing corpus cases")
        result_expected_actions: dict[str, bool] = {}
        for case in cases:
            expected = case.get("expected") if isinstance(case, dict) else None
            case_id = case.get("id") if isinstance(case, dict) else None
            action_required = (
                expected.get("action_required") if isinstance(expected, dict) else None
            )
            if not isinstance(case_id, str) or type(action_required) is not bool:
                raise ValueError("private result has invalid expected action labels")
            result_expected_actions[case_id] = action_required
        if expected_actions is None:
            expected_actions = result_expected_actions
        elif result_expected_actions != expected_actions:
            raise ValueError("private results do not use identical expected action labels")

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
    if (
        expected_actions
        and any(expected_actions.values())
        and not any(expected_actions.get(case_id) is True for case_id, _repetition in shared_runs)
    ):
        raise ValueError("blind review shared runs must include an action-required case")
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


def _write_json(path: Path, value: object, *, private: bool, overwrite: bool = True) -> None:
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
        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ValueError(f"refuses to overwrite existing file: {path}") from exc
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _candidate_from_args(
    args: argparse.Namespace,
    *,
    primary_capability: Capability = "structured_email_analysis",
) -> BenchmarkCandidate:
    gpu = args.execution_device == "gpu"
    method = None if gpu else CPU_ONLY_METHOD_BY_RUNTIME[args.runtime]
    gpu_method = GPU_OFFLOAD_METHOD_BY_RUNTIME.get(args.runtime) if gpu else None
    capabilities = [primary_capability, *(args.capability or [])]
    return BenchmarkCandidate(
        runtime=args.runtime,
        model=args.model,
        quantization=args.quantization,
        context_length=args.context_length,
        cold_start_seconds=args.cold_start_seconds,
        cpu_only=not gpu,
        cpu_only_method=method,
        gpu_offload_method=gpu_method,
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


def _run_documents_command(args: argparse.Namespace) -> int:
    _require_disjoint_input_outputs(
        [args.corpus],
        [args.output, args.private_review_output],
        label="public and private document review output",
    )
    _require_local_output(args.private_review_output)
    corpus = load_document_corpus(args.corpus)
    candidate = _candidate_from_args(args, primary_capability="text_document_summary")
    model = LocalModel(
        validate_model_base_url(args.base_url),
        candidate.model,
        args.timeout,
        args.api_token_file,
        args.require_auth,
    )

    def summarize(case: BenchmarkDocumentCase, document: str) -> DocumentSummaryInference:
        return model.summarize_document(
            title=case.title,
            text=document,
            max_words=case.expected.summary_max_words,
        )

    public, private = run_document_benchmark(
        corpus,
        candidate,
        repetitions=args.repetitions,
        summarize=summarize,
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
                "fact_recall": public["aggregate"]["fact_recall"],
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


def _prepare_inbox_command(args: argparse.Namespace) -> int:
    _require_disjoint_input_outputs(
        [args.base_corpus], [args.output], label="private inbox corpus draft"
    )
    _require_local_output(args.output)
    if args.output.exists():
        raise ValueError("private inbox corpus preparation refuses to overwrite an existing file")
    config = load_config(args.config)
    secure_runtime_paths(config)
    if not config.allowlist:
        raise ValueError("private inbox corpus requires at least one configured watched sender")
    store = Store(config.database_file)
    store.initialize()
    account = store.active_mail_account()
    if account is None:
        raise ValueError("private inbox corpus requires an active email account")
    if account.provider != DEFAULT_MAIL_PROVIDER:
        raise ValueError("private inbox corpus currently supports an active Gmail account only")
    gmail = load_mailbox_account(config, store, account.provider, account.account_id).gateway
    model = LocalModel(
        validate_model_base_url(args.base_url),
        args.model,
        args.timeout,
        args.api_token_file,
        args.require_auth,
    )
    draft = build_private_inbox_draft(
        gmail,
        model,
        load_corpus(args.base_corpus),
        senders=config.allowlist,
        limit=args.limit,
        body_char_limit=config.body_char_limit,
        current_local_time=datetime.now(config.zone),
    )
    _write_json(args.output, draft, private=True, overwrite=False)
    corpus = draft["corpus"]
    email_cases = corpus["email_cases"] if isinstance(corpus, dict) else []
    print(
        json.dumps(
            {
                "output": str(args.output),
                "email_cases": len(email_cases),
                "labels_reviewed": False,
            },
            indent=2,
        )
    )
    return 0


def _finalize_inbox_command(args: argparse.Namespace) -> int:
    _require_disjoint_input_outputs(
        [args.input], [args.output], label="private inbox corpus finalization"
    )
    _require_local_output(args.input)
    _require_local_output(args.output)
    if args.output.exists():
        raise ValueError("private inbox corpus finalization refuses to overwrite an existing file")
    _require_private_file_permissions(args.input, label="private inbox draft")
    draft = json.loads(args.input.read_text(encoding="utf-8"))
    corpus = finalize_private_inbox_draft(draft)
    _write_json(
        args.output,
        corpus.model_dump(mode="json"),
        private=True,
        overwrite=False,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "email_cases": len(corpus.email_cases),
                "labels_reviewed": True,
            },
            indent=2,
        )
    )
    return 0


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

    prepare_inbox = commands.add_parser(
        "prepare-inbox", help="Create a private real-inbox corpus labeling draft"
    )
    prepare_inbox.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    prepare_inbox.add_argument(
        "--base-corpus", type=Path, default=PROJECT_ROOT / "benchmarks/email-analysis-v1.json"
    )
    prepare_inbox.add_argument("--base-url", required=True)
    prepare_inbox.add_argument("--model", required=True)
    prepare_inbox.add_argument("--limit", type=int, default=20)
    prepare_inbox.add_argument("--timeout", type=float, default=300)
    prepare_inbox.add_argument("--api-token-file", type=Path)
    prepare_inbox.add_argument("--require-auth", action="store_true")
    prepare_inbox.add_argument("--output", type=Path, required=True)
    prepare_inbox.set_defaults(handler=_prepare_inbox_command)

    finalize_inbox = commands.add_parser(
        "finalize-inbox", help="Finalize a human-reviewed private inbox corpus"
    )
    finalize_inbox.add_argument("--input", type=Path, required=True)
    finalize_inbox.add_argument("--output", type=Path, required=True)
    finalize_inbox.set_defaults(handler=_finalize_inbox_command)

    run = commands.add_parser("run", help="Run one local candidate against a corpus")
    run.add_argument("--corpus", type=Path, required=True)
    run.add_argument("--runtime", choices=tuple(CPU_ONLY_METHOD_BY_RUNTIME), required=True)
    run.add_argument("--execution-device", choices=("cpu", "gpu"), default="cpu")
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
            "text_document_summary",
            "text_attachment_summary",
            "vision_attachment_summary",
        ),
    )
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--private-review-output", type=Path, required=True)
    run.set_defaults(handler=_run_command)

    run_documents = commands.add_parser(
        "run-documents", help="Run one local candidate against short and long documents"
    )
    run_documents.add_argument("--corpus", type=Path, required=True)
    run_documents.add_argument(
        "--runtime", choices=tuple(CPU_ONLY_METHOD_BY_RUNTIME), required=True
    )
    run_documents.add_argument("--execution-device", choices=("cpu", "gpu"), default="cpu")
    run_documents.add_argument("--base-url", required=True)
    run_documents.add_argument("--model", required=True)
    run_documents.add_argument("--quantization", required=True)
    run_documents.add_argument("--context-length", type=int, required=True)
    run_documents.add_argument("--cold-start-seconds", type=float)
    run_documents.add_argument("--repetitions", type=int, default=1)
    run_documents.add_argument("--timeout", type=float, default=600)
    run_documents.add_argument("--api-token-file", type=Path)
    run_documents.add_argument("--require-auth", action="store_true")
    run_documents.add_argument("--output", type=Path, required=True)
    run_documents.add_argument("--private-review-output", type=Path, required=True)
    run_documents.set_defaults(capability=[], handler=_run_documents_command)

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
    except (GmailError, ModelError):
        print("error: private local operation failed", file=os.sys.stderr)
        code = 2
    except (OSError, ValueError, MailboxAccountUnavailable, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
