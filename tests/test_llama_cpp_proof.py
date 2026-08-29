from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from eom_email_watcher.llama_cpp_proof import (
    DOCUMENT_SOURCE,
    DOCUMENT_SYSTEM_PROMPT,
    ProofError,
    _document_request,
    run_workload,
)


def _timer(values: list[float]) -> Iterator[float]:
    return iter(values)


def test_document_request_matches_current_plain_text_runtime_envelope() -> None:
    assert _document_request("proof-model") == {
        "model": "proof-model",
        "messages": [
            {"role": "system", "content": DOCUMENT_SYSTEM_PROMPT},
            {"role": "user", "content": DOCUMENT_SOURCE},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
        "stream": False,
    }


def test_mixed_workload_reports_only_aggregate_results() -> None:
    calls: list[str] = []
    clock = _timer([0.0, 0.1, 0.3, 0.4, 0.6, 0.7, 0.9, 1.0, 1.3, 1.4])

    result = run_workload(
        requests=4,
        concurrency=1,
        email_call=lambda: calls.append("private email output"),
        document_call=lambda: calls.append("private document output"),
        timer=clock.__next__,
    )

    assert calls == [
        "private email output",
        "private document output",
        "private email output",
        "private document output",
    ]
    assert result["requests"] == {
        "total": 4,
        "concurrency": 1,
        "email": 2,
        "document": 2,
    }
    assert result["results"] == {
        "passed": 4,
        "failed": 0,
        "passed_by_workload": {"document": 2, "email": 2},
        "error_types": {},
    }
    assert "private email output" not in json.dumps(result)
    assert "private document output" not in json.dumps(result)


def test_failure_records_exception_class_without_message() -> None:
    clock = _timer([0.0, 0.1, 0.2, 0.3, 0.4, 0.5])

    def fail() -> None:
        raise RuntimeError("private model response")

    result = run_workload(
        requests=2,
        concurrency=1,
        email_call=fail,
        document_call=lambda: None,
        timer=clock.__next__,
    )

    assert result["results"]["failed"] == 1
    assert result["results"]["error_types"] == {"RuntimeError": 1}
    assert "private model response" not in json.dumps(result)


@pytest.mark.parametrize(
    ("requests", "concurrency"),
    [(1, 1), (2, 0), (2, 3)],
)
def test_workload_rejects_unrepresentative_bounds(requests: int, concurrency: int) -> None:
    with pytest.raises(ProofError):
        run_workload(
            requests=requests,
            concurrency=concurrency,
            email_call=lambda: None,
            document_call=lambda: None,
        )
