import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from eom_email_watcher.scheduling import (
    MAX_SCHEDULING_ATTENDEES,
    SCHEDULING_SYSTEM_PROMPT,
    SchedulingSource,
    SchedulingViolation,
    scheduling_prompt,
    scheduling_source_sha256,
    validate_scheduling_output,
)


def source(
    *,
    body: str = "Please meet Tuesday, September 8 from 10:00 to 10:30 with me.",
) -> SchedulingSource:
    return SchedulingSource(
        sender="sender@example.com",
        subject="Planning meeting",
        received_at="2026-09-07T12:00:00+00:00",
        body=body,
        attachment_names=("agenda.pdf",),
        organizer_address="owner@example.com",
        configured_timezone="America/Chicago",
        context_at=datetime(2026, 9, 7, 8, tzinfo=ZoneInfo("America/Chicago")),
    )


def valid_result() -> dict[str, object]:
    return {
        "intent": "new_meeting",
        "intent_evidence": {
            "source": "body",
            "quote": "Please meet Tuesday, September 8",
        },
        "proposed_times": [
            {
                "start": "2026-09-08T10:00:00-05:00",
                "end": "2026-09-08T10:30:00-05:00",
                "timezone": "America/Chicago",
                "evidence": [
                    {
                        "source": "body",
                        "quote": "Tuesday, September 8 from 10:00 to 10:30",
                    }
                ],
            }
        ],
        "attendees": [
            {
                "email": "sender@example.com",
                "evidence": {"source": "sender", "quote": "sender@example.com"},
            }
        ],
        "referenced_event": None,
        "confidence": 0.94,
        "ambiguity_reasons": [],
    }


def validate(value: object, *, scheduling_source: SchedulingSource | None = None):
    return validate_scheduling_output(
        json.dumps(value),
        scheduling_source or source(),
    )


def codes(result) -> set[str]:
    return {item.code for item in result.violations}


def test_valid_new_meeting_is_accepted_with_normalized_evidence() -> None:
    result = validate(valid_result())

    assert result.accepted is True
    assert result.extraction is not None
    assert result.extraction.intent == "new_meeting"
    assert len(result.result_json) < 32 * 1024
    assert result.result_sha256 == hashlib.sha256(result.result_json).hexdigest()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda value: value.update({"invented": True}), "schema_unknown_field"),
        (lambda value: value.pop("confidence"), "schema_missing_field"),
        (
            lambda value: value["attendees"][0].update(  # type: ignore[index]
                {"email": "invented@example.com"}
            ),
            "attendee_unsupported",
        ),
        (
            lambda value: value["attendees"][0].update(  # type: ignore[index]
                {"email": "SENDER@example.com"}
            ),
            "attendee_not_normalized",
        ),
        (
            lambda value: value["intent_evidence"].update(  # type: ignore[union-attr]
                {"quote": "not in the source"}
            ),
            "evidence_not_found",
        ),
    ],
)
def test_schema_and_source_boundaries_fail_closed(mutation, expected: str) -> None:
    value = valid_result()
    mutation(value)

    assert expected in codes(validate(value))


@pytest.mark.parametrize("confidence", [True, "1"])
def test_scheduling_scalar_types_are_strict(confidence: object) -> None:
    value = valid_result()
    value["confidence"] = confidence

    result = validate(value)

    assert result.extraction is None
    assert SchedulingViolation("schema_invalid_field", "confidence") in result.violations
    assert json.loads(result.result_json)["confidence"] == confidence


def test_organizer_and_duplicate_attendees_fail_closed() -> None:
    value = valid_result()
    value["attendees"] = [
        {
            "email": "owner@example.com",
            "evidence": {"source": "body", "quote": "owner@example.com"},
        },
        {
            "email": "sender@example.com",
            "evidence": {"source": "sender", "quote": "sender@example.com"},
        },
        {
            "email": "sender@example.com",
            "evidence": {"source": "sender", "quote": "sender@example.com"},
        },
    ]
    scheduling_source = source(body=source().body + " owner@example.com")

    assert codes(validate(value, scheduling_source=scheduling_source)) == {
        "attendee_duplicate",
        "organizer_is_attendee",
    }


@pytest.mark.parametrize(
    ("start", "end", "zone", "expected"),
    [
        (
            "2026-09-08T10:00:00-05:00",
            "2026-09-08T10:30:00-05:00",
            "Europe/Berlin",
            "timezone_offset_mismatch",
        ),
        (
            "2026-03-08T02:15:00-06:00",
            "2026-03-08T03:15:00-05:00",
            "America/Chicago",
            "timezone_nonexistent",
        ),
        (
            "2026-11-01T01:15:00-05:00",
            "2026-11-01T01:45:00-05:00",
            "America/Chicago",
            "timezone_ambiguous",
        ),
        (
            "2026-09-08T10:30:00-05:00",
            "2026-09-08T10:00:00-05:00",
            "America/Chicago",
            "time_range_invalid",
        ),
        (
            "2026-09-08T10:00:00",
            "2026-09-08T10:30:00",
            "America/Chicago",
            "time_naive",
        ),
    ],
)
def test_time_zone_and_range_boundaries_fail_closed(
    start: str,
    end: str,
    zone: str,
    expected: str,
) -> None:
    value = valid_result()
    proposed = value["proposed_times"][0]  # type: ignore[index]
    proposed.update({"start": start, "end": end, "timezone": zone})

    assert expected in codes(validate(value))


def test_proposed_date_and_clock_values_must_match_source_evidence() -> None:
    wrong_date = valid_result()
    wrong_date["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-09T10:00:00-05:00",
            "end": "2026-09-09T10:30:00-05:00",
        }
    )
    wrong_time = valid_result()
    wrong_time["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-08T11:00:00-05:00",
            "end": "2026-09-08T11:30:00-05:00",
        }
    )

    assert "time_date_unsupported" in codes(validate(wrong_date))
    assert "time_value_unsupported" in codes(validate(wrong_time))


def test_hour_only_evidence_cannot_support_invented_nonzero_minutes() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-08T10:30:00-05:00",
            "end": "2026-09-08T11:00:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": "Tuesday, September 8 from 10 AM to 11 AM",
                }
            ],
        }
    )
    scheduling_source = source(body="Please meet Tuesday, September 8 from 10 AM to 11 AM.")

    result = validate(value, scheduling_source=scheduling_source)

    assert SchedulingViolation("time_value_unsupported", "proposed_times.0.start") in (
        result.violations
    )


def test_seconds_require_exact_source_evidence() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {"start": "2026-09-08T10:00:59-05:00"}
    )

    assert SchedulingViolation("time_value_unsupported", "proposed_times.0.start") in (
        validate(value).violations
    )


def test_meridiem_qualified_clocks_do_not_match_24_hour_values() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-08T10:00:00-05:00",
            "end": "2026-09-08T11:00:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": "September 8 from 10:00 to 11:00 PM",
                }
            ],
        }
    )
    scheduling_source = source(body="September 8 from 10:00 to 11:00 PM")

    assert SchedulingViolation("time_value_unsupported", "proposed_times.0.end") in (
        validate(value, scheduling_source=scheduling_source).violations
    )


def test_trailing_meridiem_cannot_change_only_the_range_end() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-08T10:00:00-05:00",
            "end": "2026-09-08T23:00:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": "September 8 from 10:00 to 11:00 PM",
                }
            ],
        }
    )
    scheduling_source = source(body="September 8 from 10:00 to 11:00 PM")

    assert "time_range_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )

    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-08T22:00:00-05:00",
            "end": "2026-09-08T23:00:00-05:00",
        }
    )
    corrected_codes = codes(validate(value, scheduling_source=scheduling_source))
    assert "time_range_unsupported" not in corrected_codes
    assert "time_value_unsupported" not in corrected_codes


def test_unqualified_12_hour_start_requires_a_shared_meridiem() -> None:
    value = valid_result()
    quote = "September 8 from 10:00 to 22:30"
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-08T22:00:00-05:00",
            "end": "2026-09-08T22:30:00-05:00",
            "evidence": [{"source": "body", "quote": quote}],
        }
    )

    assert "time_range_unsupported" in codes(
        validate(value, scheduling_source=source(body=quote))
    )


def test_range_endpoints_cannot_borrow_from_separate_alternatives() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "end": "2026-09-09T15:00:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": (
                        "September 8, 2026 from 10:00 to 11:00, "
                        "or September 9, 2026 from 14:00 to 15:00"
                    ),
                }
            ],
        }
    )
    scheduling_source = source(
        body=(
            "September 8, 2026 from 10:00 to 11:00, "
            "or September 9, 2026 from 14:00 to 15:00"
        )
    )

    assert "time_range_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )

    split_evidence = valid_result()
    split_evidence["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "end": "2026-09-09T10:30:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": "September 8, 2026 from 10:00 to 10:30",
                },
                {"source": "body", "quote": "September 9, 2026"},
            ],
        }
    )
    split_source = source(
        body=(
            "September 8, 2026 from 10:00 to 10:30. "
            "A separate option is September 9, 2026."
        )
    )
    assert "time_range_unsupported" in codes(
        validate(split_evidence, scheduling_source=split_source)
    )


def test_time_pair_cannot_borrow_a_date_from_another_option() -> None:
    value = valid_result()
    proposed = value["proposed_times"][0]  # type: ignore[index]
    proposed.update(
        {
            "start": "2026-09-08T14:00:00-05:00",
            "end": "2026-09-08T15:00:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": (
                        "September 8 from 10:00 to 11:00, "
                        "or September 9 from 14:00 to 15:00"
                    ),
                }
            ],
        }
    )
    scheduling_source = source(
        body=(
            "September 8 from 10:00 to 11:00, "
            "or September 9 from 14:00 to 15:00"
        )
    )

    assert "time_range_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )

    proposed["start"] = "2026-09-09T14:00:00-05:00"
    proposed["end"] = "2026-09-09T15:00:00-05:00"
    assert "time_range_unsupported" not in codes(
        validate(value, scheduling_source=scheduling_source)
    )


def test_comma_separated_options_keep_dates_bound_to_their_time_pairs() -> None:
    value = valid_result()
    proposed = value["proposed_times"][0]  # type: ignore[index]
    proposed.update(
        {
            "start": "2026-09-08T14:00:00-05:00",
            "end": "2026-09-08T15:00:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": (
                        "September 8 from 10:00 to 11:00, "
                        "September 9 from 14:00 to 15:00"
                    ),
                }
            ],
        }
    )
    scheduling_source = source(
        body=(
            "September 8 from 10:00 to 11:00, "
            "September 9 from 14:00 to 15:00"
        )
    )

    assert "time_range_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )

    proposed["start"] = "2026-09-09T14:00:00-05:00"
    proposed["end"] = "2026-09-09T15:00:00-05:00"
    assert "time_range_unsupported" not in codes(
        validate(value, scheduling_source=scheduling_source)
    )


@pytest.mark.parametrize(
    (
        "quote",
        "unsupported_start",
        "unsupported_end",
        "supported_start",
        "supported_end",
    ),
    [
        (
            "tomorrow from 10:00 to 11:00, 2026-09-09 from 14:00 to 15:00",
            "2026-09-08T14:00:00-05:00",
            "2026-09-08T15:00:00-05:00",
            "2026-09-09T14:00:00-05:00",
            "2026-09-09T15:00:00-05:00",
        ),
        (
            "2026-09-09 from 14:00 to 15:00, tomorrow from 10:00 to 11:00",
            "2026-09-09T10:00:00-05:00",
            "2026-09-09T11:00:00-05:00",
            "2026-09-08T10:00:00-05:00",
            "2026-09-08T11:00:00-05:00",
        ),
    ],
)
def test_relative_and_iso_comma_options_keep_dates_bound_to_their_time_pairs(
    quote: str,
    unsupported_start: str,
    unsupported_end: str,
    supported_start: str,
    supported_end: str,
) -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": unsupported_start,
            "end": unsupported_end,
            "evidence": [{"source": "body", "quote": quote}],
        }
    )

    scheduling_source = source(body=quote)
    assert "time_range_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )

    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {"start": supported_start, "end": supported_end}
    )
    assert "time_range_unsupported" not in codes(
        validate(value, scheduling_source=scheduling_source)
    )


def test_explicit_source_zone_overrides_configured_default() -> None:
    value = valid_result()
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {
            "source": "body",
            "quote": "September 8, 2026 from 10:00 to 10:30 UTC",
        }
    ]
    scheduling_source = source(body="September 8, 2026 from 10:00 to 10:30 UTC")

    assert "timezone_unsupported" in codes(validate(value, scheduling_source=scheduling_source))


def test_spelled_source_zone_overrides_configured_default() -> None:
    value = valid_result()
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {
            "source": "body",
            "quote": "September 8, 2026 from 10:00 to 10:30 Eastern Time",
        }
    ]
    scheduling_source = source(
        body="September 8, 2026 from 10:00 to 10:30 Eastern Time"
    )

    assert "timezone_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )

    value["proposed_times"][0]["timezone"] = "America/New_York"  # type: ignore[index]
    value["proposed_times"][0]["start"] = "2026-09-08T10:00:00-04:00"  # type: ignore[index]
    value["proposed_times"][0]["end"] = "2026-09-08T10:30:00-04:00"  # type: ignore[index]
    assert "timezone_unsupported" not in codes(
        validate(value, scheduling_source=scheduling_source)
    )


def test_timezone_must_come_from_the_matching_time_option() -> None:
    quote = (
        "September 8 from 10:00 to 10:30 Central, or "
        "September 9 from 10:00 to 10:30 Eastern"
    )
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-08T10:00:00-04:00",
            "end": "2026-09-08T10:30:00-04:00",
            "timezone": "America/New_York",
            "evidence": [{"source": "body", "quote": quote}],
        }
    )

    assert "timezone_unsupported" in codes(
        validate(value, scheduling_source=source(body=quote))
    )

    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-08T10:00:00-05:00",
            "end": "2026-09-08T10:30:00-05:00",
            "timezone": "America/Chicago",
        }
    )
    assert "timezone_unsupported" not in codes(
        validate(value, scheduling_source=source(body=quote))
    )


def test_competing_timezones_in_one_option_fail_closed() -> None:
    value = valid_result()
    quote = (
        "September 8 from 10:00 to 10:30 "
        "America/Chicago (America/New_York)"
    )
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {"source": "body", "quote": quote}
    ]

    assert "timezone_unsupported" in codes(
        validate(value, scheduling_source=source(body=quote))
    )

    single_zone_quote = "September 8 from 10:00 to 10:30 America/Chicago"
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {"source": "body", "quote": single_zone_quote}
    ]
    assert "timezone_unsupported" not in codes(
        validate(value, scheduling_source=source(body=single_zone_quote))
    )


def test_time_validation_restores_qualifiers_omitted_from_the_model_quote() -> None:
    value = valid_result()
    full_source = (
        "September 8 from 10:00 to 10:30 "
        "America/Chicago (America/New_York)."
    )
    truncated_quote = "September 8 from 10:00 to 10:30 America/Chicago"
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {"source": "body", "quote": truncated_quote}
    ]

    assert "timezone_unsupported" in codes(
        validate(value, scheduling_source=source(body=full_source))
    )


def test_unsupported_composite_zone_is_not_treated_as_us_central() -> None:
    value = valid_result()
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {
            "source": "body",
            "quote": "September 8, 2026 from 10:00 to 10:30 Central European Time",
        }
    ]
    scheduling_source = source(
        body="September 8, 2026 from 10:00 to 10:30 Central European Time"
    )

    assert "timezone_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )


@pytest.mark.parametrize("zone_label", ["Japan Standard Time", "British Summer Time"])
def test_unsupported_explicit_zone_labels_fail_closed(zone_label: str) -> None:
    value = valid_result()
    quote = f"September 8, 2026 from 10:00 to 10:30 {zone_label}"
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {"source": "body", "quote": quote}
    ]

    assert "timezone_unsupported" in codes(
        validate(value, scheduling_source=source(body=quote))
    )


@pytest.mark.parametrize("zone_label", ["JST", "BST", "CET", "(JST)"])
def test_unsupported_explicit_zone_abbreviations_fail_closed(zone_label: str) -> None:
    value = valid_result()
    quote = f"September 8, 2026 from 10:00 to 10:30 {zone_label}"
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {"source": "body", "quote": quote}
    ]

    assert "timezone_unsupported" in codes(
        validate(value, scheduling_source=source(body=quote))
    )


def test_conflicting_weekday_and_calendar_date_fail_closed() -> None:
    value = valid_result()
    conflicting = "Monday, September 8, 2026 from 10:00 to 10:30"
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {"source": "body", "quote": conflicting}
    ]

    assert "time_date_unsupported" in codes(
        validate(value, scheduling_source=source(body=conflicting))
    )

    consistent = "Tuesday, September 8, 2026 from 10:00 to 10:30"
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {"source": "body", "quote": consistent}
    ]
    assert "time_date_unsupported" not in codes(
        validate(value, scheduling_source=source(body=consistent))
    )

    reverse_conflict = "Tuesday, September 9, 2026 from 10:00 to 10:30"
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {"source": "body", "quote": reverse_conflict}
    ]
    assert "time_date_unsupported" in codes(
        validate(value, scheduling_source=source(body=reverse_conflict))
    )


def test_day_after_tomorrow_is_not_treated_as_tomorrow() -> None:
    value = valid_result()
    proposed = value["proposed_times"][0]  # type: ignore[index]
    proposed["evidence"] = [
        {
            "source": "body",
            "quote": "day after tomorrow from 10:00 to 10:30",
        }
    ]
    scheduling_source = source(body="Please meet day after tomorrow from 10:00 to 10:30.")

    assert "time_date_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )

    proposed["start"] = "2026-09-09T10:00:00-05:00"
    proposed["end"] = "2026-09-09T10:30:00-05:00"
    assert "time_date_unsupported" not in codes(
        validate(value, scheduling_source=scheduling_source)
    )


def test_next_weekday_is_not_treated_as_the_nearest_bare_weekday() -> None:
    value = valid_result()
    proposed = value["proposed_times"][0]  # type: ignore[index]
    proposed.update(
        {
            "start": "2026-09-07T10:00:00-05:00",
            "end": "2026-09-07T10:30:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": "next Monday from 10:00 to 10:30",
                }
            ],
        }
    )
    scheduling_source = source(body="Please meet next Monday from 10:00 to 10:30.")

    assert "time_date_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )

    proposed["start"] = "2026-09-14T10:00:00-05:00"
    proposed["end"] = "2026-09-14T10:30:00-05:00"
    assert "time_date_unsupported" not in codes(
        validate(value, scheduling_source=scheduling_source)
    )


def test_relative_date_words_require_token_boundaries() -> None:
    value = valid_result()
    value["proposed_times"][0]["evidence"] = [  # type: ignore[index]
        {
            "source": "body",
            "quote": "discuss Tomorrowland from 10:00 to 10:30",
        }
    ]
    scheduling_source = source(body="Please discuss Tomorrowland from 10:00 to 10:30.")

    assert "time_date_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )


def test_zone_conversion_overflow_is_a_typed_validation_rejection() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "9999-12-31T22:00:00-12:00",
            "end": "9999-12-31T23:00:00-12:00",
            "timezone": "Pacific/Kiritimati",
        }
    )

    assert "time_invalid" in codes(validate(value))


def test_source_backed_offset_selects_a_dst_fold() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-11-01T01:15:00-05:00",
            "end": "2026-11-01T01:45:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": "November 1, 2026 from 1:15 AM to 1:45 AM -05:00",
                }
            ],
        }
    )
    scheduling_source = source(
        body="Please meet November 1, 2026 from 1:15 AM to 1:45 AM -05:00."
    )

    assert "timezone_ambiguous" not in codes(
        validate(value, scheduling_source=scheduling_source)
    )


def test_dst_fold_offset_must_share_the_matching_range_evidence() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-11-01T01:15:00-05:00",
            "end": "2026-11-01T01:45:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": "November 1, 2026 from 1:15 AM to 1:45 AM.",
                },
                {"source": "body", "quote": "footer offset -05:00"},
            ],
        }
    )
    scheduling_source = source(
        body=(
            "November 1, 2026 from 1:15 AM to 1:45 AM. "
            "Unrelated footer offset -05:00."
        )
    )

    assert "timezone_ambiguous" in codes(
        validate(value, scheduling_source=scheduling_source)
    )


def test_end_date_must_independently_match_source_evidence() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {"end": "2026-09-09T10:30:00-05:00"}
    )

    result = validate(value)

    assert SchedulingViolation("time_date_unsupported", "proposed_times.0.end") in result.violations


def test_new_meeting_time_must_be_future_relative_to_persisted_context() -> None:
    value = valid_result()
    value["proposed_times"][0].update(  # type: ignore[index,union-attr]
        {
            "start": "2026-09-06T10:00:00-05:00",
            "end": "2026-09-06T10:30:00-05:00",
            "evidence": [
                {
                    "source": "body",
                    "quote": "September 6, 2026 from 10:00 to 10:30",
                }
            ],
        }
    )
    scheduling_source = source(body="Please meet September 6, 2026 from 10:00 to 10:30.")

    assert "time_range_past" in codes(validate(value, scheduling_source=scheduling_source))


def test_event_reference_value_must_appear_in_its_evidence_quote() -> None:
    value = valid_result()
    value["intent"] = "reschedule"
    value["referenced_event"] = {
        "provider_event_id": None,
        "human_reference": "quarterly planning call",
        "evidence": {"source": "body", "quote": "Please move the meeting"},
    }
    scheduling_source = source(body=source().body + " Please move the meeting.")

    assert "event_reference_unsupported" in codes(
        validate(value, scheduling_source=scheduling_source)
    )


def test_new_meeting_requires_high_confidence_and_no_existing_event_reference() -> None:
    value = valid_result()
    value["confidence"] = 0.79
    value["referenced_event"] = {
        "provider_event_id": None,
        "human_reference": "our existing planning call",
        "evidence": {"source": "body", "quote": "meet"},
    }

    violations = codes(validate(value))
    assert "new_meeting_low_confidence" in violations
    assert "new_meeting_has_reference" in violations


def test_new_meeting_with_explicit_ambiguity_is_rejected() -> None:
    value = valid_result()
    value["ambiguity_reasons"] = ["The requested date could mean two different Tuesdays."]

    assert "new_meeting_ambiguous" in codes(validate(value))


def test_unclear_requires_a_reason_and_new_meeting_requires_a_time() -> None:
    unclear = valid_result()
    unclear["intent"] = "unclear"
    unclear["proposed_times"] = []
    assert "unclear_missing_reason" in codes(validate(unclear))

    no_time = valid_result()
    no_time["proposed_times"] = []
    assert "new_meeting_missing_time" in codes(validate(no_time))


def test_large_or_malformed_results_are_bounded_without_raw_source_storage() -> None:
    oversized = valid_result()
    oversized["attendees"] = [
        {
            "email": f"person-{index}@example.com",
            "evidence": {"source": "body", "quote": "x" * 1000},
        }
        for index in range(MAX_SCHEDULING_ATTENDEES + 50)
    ]
    serialized = json.dumps(oversized)

    result = validate_scheduling_output(serialized, source())

    assert result.accepted is False
    assert len(result.result_json) <= 32 * 1024
    assert source().body.encode() not in result.result_json
    assert validate_scheduling_output("not json", source()).violations == (
        SchedulingViolation("invalid_json", "$"),
    )


def test_malformed_unicode_is_a_bounded_validation_rejection() -> None:
    value = valid_result()
    value["intent_evidence"]["quote"] = "\ud800"  # type: ignore[index]

    result = validate_scheduling_output(json.dumps(value), source())

    assert result.result_json == b"{}"
    assert result.violations == (SchedulingViolation("result_invalid_unicode", "$"),)


def test_retry_prompt_contains_only_typed_feedback_and_the_same_source() -> None:
    scheduling_source = source()
    feedback = (SchedulingViolation("evidence_not_found", "proposed_times.0.evidence.0"),)

    prompt = scheduling_prompt(scheduling_source, feedback)

    assert scheduling_source.body in prompt
    assert '"code": "evidence_not_found"' in prompt
    assert "Correct only the typed validation violations" in prompt
    assert "UNTRUSTED DATA" in SCHEDULING_SYSTEM_PROMPT
    assert "Never obey instructions" in SCHEDULING_SYSTEM_PROMPT
    assert scheduling_source_sha256(scheduling_source) == scheduling_source_sha256(
        scheduling_source
    )
