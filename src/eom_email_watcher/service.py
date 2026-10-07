from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from connect_automate.entitlement import (
    AUTOMATIONS_FEATURE_ID,
    CONNECT_FEATURE_ID,
    EntitlementDecision,
    connect_entitlement_decision,
    feature_entitlements_active,
)

from .config import (
    Config,
    admission_sender_display_name,
    exact_sender_selector_id,
    normalize_validated_address,
)
from .db import (
    AdmissionProvenance,
    AnalyzedMessage,
    AutomationCalendarWrite,
    AutomationExtractionPayload,
    AutomationProposalWork,
    AutomationRun,
    AutomationSourceChanged,
    AutomationWork,
    CalendarGrant,
    GmailRecoveryMessage,
    GmailRecoveryState,
    MailboxIdentityChanged,
    NotificationIntent,
    PendingMessage,
    Store,
)
from .gmail import (
    GmailAuthorizationRejected,
    GmailError,
    GmailLabelCatalogInvalid,
    GmailLabelCatalogUnavailable,
    GmailRecoveryPageInvalid,
    GmailRecoveryPageTokenInvalid,
    locations_from_labels,
)
from .imap import IMAP_PROVIDER, ImapGateway, imap_cursor_epoch
from .mailbox import (
    INBOX_LOCATION,
    MESSAGE_LOCATIONS,
    SENT_LOCATION,
    SENT_SCOPE_AVAILABLE,
    SENT_SCOPE_NOT_POLLED,
    FolderObservation,
    MailboxAccountUnavailable,
    MailboxChanges,
    MailboxError,
    MailboxGateway,
    MailboxMessageInvalid,
    MailboxMessageUnavailable,
    MailboxSession,
    MessageContent,
    MessageMetadata,
    StaleMailboxCursor,
    default_mailbox_session,
    folder_scope_key,
    mailbox_polling_session,
    mailbox_session_address,
    mailbox_session_identity_key,
    read_through_sources,
    scoped_message_id,
)
from .microsoft365 import (
    MICROSOFT365_PROVIDER,
    Microsoft365Error,
    MicrosoftAuthorizationRejected,
)
from .microsoft_calendar import (
    MAX_CALENDAR_SUBJECT_BYTES,
    CalendarProposalCandidate,
    MicrosoftCalendarProposalAuthorization,
    MicrosoftCalendarProposalRejected,
    MicrosoftCalendarWriteAuthorization,
    MicrosoftCalendarWriteRejected,
    create_calendar_event,
    find_calendar_event_by_transaction,
    find_meeting_time,
)
from .mime import body_was_truncated
from .model import (
    MAX_GATEWAY_BODY_CHARS,
    MAX_GATEWAY_SENDER_CHARS,
    MAX_GATEWAY_SUBJECT_CHARS,
    Analysis,
    GatewayModel,
    GatewayModelError,
    GatewayOutputRejected,
    ModelError,
    ModelRuntime,
    bounded_gateway_attachment_names,
    bounded_gateway_text,
)
from .notifications import NotificationError, send_analysis, send_fallback, send_review
from .runtime import (
    load_configured_mailbox,
    load_mailbox_account,
    mail_account_token_file,
    microsoft_calendar_token_file,
)
from .scheduling import (
    SchedulingExtraction,
    SchedulingSource,
    SchedulingViolation,
    scheduling_source_sha256,
)

logger = logging.getLogger(__name__)


class LegacyMailboxIdentityUnverified(MailboxError):
    """Stale recovery cannot safely cross retained pre-identity markers."""


class GmailLabelSelectorLike(Protocol):
    selector_id: str
    provider: str
    account_id: str
    mailbox_identity_key: str
    label_id: str
    selected_display_name: str


@dataclass(frozen=True)
class AdmissionDecision:
    kind: Literal["exact_sender", "gmail_user_label"]
    selector_id: str
    display_name: str | None
    mailbox_identity_key: str
    admitted_at: str

    def provenance(self) -> AdmissionProvenance:
        return AdmissionProvenance(
            kind=self.kind,
            selector_id=self.selector_id,
            display_name=self.display_name,
            mailbox_identity_key=self.mailbox_identity_key,
            admitted_at=self.admitted_at,
        )


@dataclass(frozen=True)
class RecoveryLabelGrant:
    selector_id: str
    provider: str
    account_id: str
    mailbox_identity_key: str
    label_id: str
    selected_display_name: str


def _recovery_since(last_success: str, retention_cutoff: datetime) -> datetime:
    """Where a stale cursor's recovery starts: shortly before the last success, within retention."""
    since = datetime.fromisoformat(last_success).astimezone(UTC) - timedelta(minutes=5)
    return max(since, retention_cutoff)


def _folders_in_scope(gated_allowed: bool) -> frozenset[str]:
    """Contract D-ops: Sent is in scope only while the gated class is allowed."""
    return MESSAGE_LOCATIONS if gated_allowed else frozenset({INBOX_LOCATION})


def _scope_gateway(gateway: object, folders: frozenset[str]) -> None:
    """Tell a gateway the folders in scope, so its own queries never read a gated one."""
    scoper = getattr(gateway, "scope_folders", None)
    if callable(scoper):
        scoper(folders)


def _content_from_sources(
    gateway: MailboxGateway, provider_message_ids: Sequence[str], body_char_limit: int
) -> tuple[str, MessageContent]:
    return read_through_sources(
        provider_message_ids, lambda source: gateway.content(source, body_char_limit)
    )


def _scope_complete(folders: frozenset[str]) -> bool:
    """Whether every admitted folder is in scope, so an observation is complete (plan step 5)."""
    return folders == MESSAGE_LOCATIONS


def _admitted_locations(
    metadata: object, labels: frozenset[str], folders: frozenset[str] = MESSAGE_LOCATIONS
) -> frozenset[str]:
    """Contract D-scope: the admitted folders a message is in, among those in scope.

    Providers that record locations say which. Gmail metadata without them names
    its folders through labels, with the mapping gmail.parse_metadata owns. The
    one answer decides admission and is what every insert stores, so a message
    is never admitted from a folder other than the one recorded for it, and a
    Sent-only message is admitted only while Sent is in scope (_folders_in_scope).
    """
    locations = getattr(metadata, "locations", None)
    if not (isinstance(locations, frozenset) and locations):
        locations = locations_from_labels(labels)
    return locations & folders


def match_mailbox_admission(
    *,
    metadata: object,
    provider: str,
    account_id: str,
    mailbox_identity_key: str,
    exact_senders: Mapping[str, str | None],
    label_selectors: Iterable[GmailLabelSelectorLike],
    admitted_at: datetime,
    folders: frozenset[str] = MESSAGE_LOCATIONS,
) -> AdmissionDecision | None:
    """Return the one deterministic admission grant for mailbox metadata."""
    labels = getattr(metadata, "labels", None)
    sender = getattr(metadata, "sender", None)
    if not isinstance(labels, frozenset) or not _admitted_locations(metadata, labels, folders):
        return None
    admitted_at_text = admitted_at.astimezone(UTC).isoformat()
    if isinstance(sender, str) and sender in exact_senders:
        return AdmissionDecision(
            kind="exact_sender",
            selector_id=exact_sender_selector_id(sender),
            display_name=admission_sender_display_name(exact_senders[sender]),
            mailbox_identity_key=mailbox_identity_key,
            admitted_at=admitted_at_text,
        )
    if provider != "gmail":
        return None
    matches = sorted(
        (
            selector
            for selector in label_selectors
            if selector.provider == provider
            and selector.account_id == account_id
            and selector.mailbox_identity_key == mailbox_identity_key
            and selector.label_id in labels
        ),
        key=lambda selector: selector.selector_id,
    )
    if not matches:
        return None
    winner = matches[0]
    return AdmissionDecision(
        kind="gmail_user_label",
        selector_id=winner.selector_id,
        display_name=winner.selected_display_name,
        mailbox_identity_key=mailbox_identity_key,
        admitted_at=admitted_at_text,
    )


def _acknowledge_gateway_result(
    model: ModelRuntime,
    request_id: str,
    disposition: Literal["persisted", "application_rejected"],
) -> None:
    if not isinstance(model, GatewayModel):
        return
    try:
        model.acknowledge(request_id, disposition)
    except ModelError as exc:
        logger.warning("Inference gateway result acknowledgement failed: %s", exc)


@dataclass(frozen=True)
class SchedulingProposalAccess:
    authorization: MicrosoftCalendarProposalAuthorization
    grant: CalendarGrant


@dataclass(frozen=True)
class SchedulingWriteAccess:
    authorization: MicrosoftCalendarWriteAuthorization
    grant: CalendarGrant


@dataclass(frozen=True)
class SchedulingDecision:
    run: AutomationRun
    write: AutomationCalendarWrite | None


def _scheduling_proposal_authorization(
    config: Config,
    store: Store,
    *,
    provider: str,
    account_id: str,
    expected_principal_key: str | None = None,
) -> SchedulingProposalAccess | None:
    if provider != MICROSOFT365_PROVIDER:
        return None
    if not feature_entitlements_active(CONNECT_FEATURE_ID, AUTOMATIONS_FEATURE_ID):
        return None
    account = store.mail_account(provider, account_id)
    if account is None or account.address is None:
        return None
    proposal_grant = store.calendar_grant(account_id, "proposal")
    if (
        proposal_grant is None
        or proposal_grant.state != "ready"
        or proposal_grant.principal_key is None
        or (
            expected_principal_key is not None
            and proposal_grant.principal_key != expected_principal_key
        )
    ):
        return None
    try:
        authorization = MicrosoftCalendarProposalAuthorization.from_matching_tokens(
            config.microsoft_credentials_file,
            microsoft_calendar_token_file(config, account, "proposal"),
            mail_account_token_file(config, account),
            proposal_grant.principal_key,
        )
    except MicrosoftAuthorizationRejected:
        store.revoke_calendar_grant_if_current(proposal_grant)
        return None
    except (MailboxAccountUnavailable, Microsoft365Error):
        return None
    if not feature_entitlements_active(CONNECT_FEATURE_ID, AUTOMATIONS_FEATURE_ID):
        return None
    if expected_principal_key is not None and authorization.principal.key != expected_principal_key:
        return None
    return SchedulingProposalAccess(authorization, proposal_grant)


def _scheduling_authorization_principal(
    config: Config,
    store: Store,
    *,
    provider: str,
    account_id: str,
    expected_principal_key: str | None = None,
) -> str | None:
    authorization = _scheduling_proposal_authorization(
        config,
        store,
        provider=provider,
        account_id=account_id,
        expected_principal_key=expected_principal_key,
    )
    return authorization.authorization.principal.key if authorization is not None else None


def _scheduling_write_authorization(
    config: Config,
    store: Store,
    *,
    provider: str,
    account_id: str,
    expected_principal_key: str,
    require_active_entitlements: bool = True,
) -> SchedulingWriteAccess | None:
    if provider != MICROSOFT365_PROVIDER:
        return None
    if require_active_entitlements and not feature_entitlements_active(
        CONNECT_FEATURE_ID, AUTOMATIONS_FEATURE_ID
    ):
        return None
    account = store.mail_account(provider, account_id)
    if account is None or account.address is None:
        return None
    write_grant = store.calendar_grant(account_id, "write")
    if (
        write_grant is None
        or write_grant.state != "ready"
        or write_grant.principal_key != expected_principal_key
    ):
        return None
    try:
        authorization = MicrosoftCalendarWriteAuthorization.from_matching_tokens(
            config.microsoft_credentials_file,
            microsoft_calendar_token_file(config, account, "write"),
            mail_account_token_file(config, account),
            expected_principal_key,
        )
    except MicrosoftAuthorizationRejected:
        store.revoke_calendar_grant_if_current(write_grant)
        return None
    except (MailboxAccountUnavailable, Microsoft365Error):
        return None
    if require_active_entitlements and not feature_entitlements_active(
        CONNECT_FEATURE_ID, AUTOMATIONS_FEATURE_ID
    ):
        return None
    if authorization.principal.key != expected_principal_key:
        return None
    return SchedulingWriteAccess(authorization, write_grant)


def _scheduling_automation_principal(
    config: Config,
    store: Store,
    message: PendingMessage,
) -> str | None:
    return _scheduling_authorization_principal(
        config,
        store,
        provider=message.provider,
        account_id=message.account_id,
    )


@dataclass(frozen=True)
class AutomationProcessing:
    processed: int
    review_required: int
    attempted_run_ids: frozenset[str]
    purged: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _retry_feedback(payloads: list[AutomationExtractionPayload]) -> tuple[SchedulingViolation, ...]:
    rejected = next(
        (payload for payload in reversed(payloads) if payload.status == "rejected"),
        None,
    )
    if rejected is None or rejected.violations_json is None:
        return ()
    try:
        values = json.loads(rejected.violations_json)
    except (UnicodeDecodeError, ValueError):
        logger.error("Stored scheduling validation feedback is unreadable")
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(
        SchedulingViolation(str(value.get("code", "")), str(value.get("path", "")))
        for value in values
        if isinstance(value, dict)
    )


def _accepted_extraction_outcome(intent: str) -> tuple[str, str | None]:
    if intent == "new_meeting":
        return "proposing", None
    if intent == "reschedule":
        return "manual_review", "reschedule_not_supported"
    if intent == "cancellation":
        return "manual_review", "cancellation_not_supported"
    return "ambiguous", "ambiguous_extraction"


def _transition_source_problem(
    store: Store,
    work: AutomationWork,
    *,
    next_state: str,
    failure_code: str,
) -> None:
    current = store.automation_run(work.run.run_id)
    if current is None or current.state not in {"detected", "extracting"}:
        return
    store.transition_automation_to_review(
        current.run_id,
        current.state_version,
        next_state=next_state,
        failure_code=failure_code,
    )


def process_scheduling_automations(
    config: Config,
    store: Store,
    model: ModelRuntime,
    *,
    exclude_run_ids: frozenset[str] = frozenset(),
    limit: int = 25,
    now: datetime | None = None,
) -> AutomationProcessing:
    observed_at = (now or _utc_now()).astimezone(UTC)
    purge_outcome = store.purge_with_outcome(config.retention_days, now=observed_at)
    processed = purge_outcome.automation_review_required
    review_required = purge_outcome.automation_review_required
    attempted: set[str] = set()
    capacity_used = 0
    cursor: tuple[str, str] | None = None
    while capacity_used < limit:
        page = store.recoverable_automation_runs(limit, after=cursor, now=observed_at)
        if not page:
            break
        for work in page:
            run = work.run
            cursor = (run.created_at, run.run_id)
            if run.run_id in exclude_run_ids:
                continue
            if capacity_used >= limit:
                break
            if (
                _scheduling_authorization_principal(
                    config,
                    store,
                    provider=run.provider,
                    account_id=run.account_id,
                    expected_principal_key=run.calendar_principal_key,
                )
                is None
            ):
                continue
            attempted.add(run.run_id)
            try:
                mailbox = load_mailbox_account(config, store, run.provider, run.account_id)
            except MailboxAccountUnavailable as exc:
                logger.info("Scheduling run %s mailbox unavailable: %s", run.run_id, exc)
                continue
            except MailboxError as exc:
                logger.warning(
                    "Scheduling run %s mailbox temporarily unavailable: %s",
                    run.run_id,
                    exc,
                )
                continue

            body_char_limit = run.extraction_body_char_limit or config.body_char_limit
            timezone = run.extraction_timezone or config.timezone
            try:
                if run.extraction_context_at is not None:
                    context_at = datetime.fromisoformat(run.extraction_context_at)
                else:
                    received_at = datetime.fromisoformat(work.received_at)
                    if received_at.tzinfo is None:
                        raise ValueError("Scheduling source time must be timezone-aware")
                    context_at = received_at.astimezone(config.zone)
                if context_at.tzinfo is None:
                    raise ValueError("Scheduling context must be timezone-aware")
                if run.state == "extracting" and work.extraction_organizer_address is None:
                    raise ValueError("Reserved extraction organizer is unavailable")
                organizer_address = normalize_validated_address(
                    work.extraction_organizer_address or work.organizer_address
                )
            except (OverflowError, ValueError):
                capacity_used += 1
                _transition_source_problem(
                    store,
                    work,
                    next_state="manual_review",
                    failure_code="extraction_context_invalid",
                )
                processed += 1
                review_required += 1
                continue
            try:
                with mailbox_polling_session(mailbox.gateway):
                    source_identity_key = mailbox_session_identity_key(mailbox)
                    account = store.mail_account(run.provider, run.account_id)
                    legacy_identity_matches = bool(
                        account is not None
                        and account.legacy_identity_status == "continuity_proven"
                        and account.legacy_identity_key == source_identity_key
                    )
                    microsoft_run_witness = bool(
                        run.provider == MICROSOFT365_PROVIDER
                        and run.calendar_principal_key == source_identity_key
                    )
                    if (
                        work.source_mailbox_identity_key != source_identity_key
                        if work.source_mailbox_identity_key is not None
                        else not (legacy_identity_matches or microsoft_run_witness)
                    ):
                        capacity_used += 1
                        _transition_source_problem(
                            store,
                            work,
                            next_state="source_unavailable",
                            failure_code="mailbox_identity_unverified",
                        )
                        processed += 1
                        review_required += 1
                        continue
                    metadata = mailbox.gateway.metadata(work.provider_message_id)
                    if (
                        metadata.message_id != work.provider_message_id
                        or "INBOX" not in metadata.labels
                    ):
                        raise MailboxMessageUnavailable(
                            "Scheduling source is no longer in the inbox"
                        )
                    content = mailbox.gateway.content(work.provider_message_id, body_char_limit)
            except MailboxMessageUnavailable as exc:
                capacity_used += 1
                logger.info("Scheduling run %s source unavailable: %s", run.run_id, exc)
                _transition_source_problem(
                    store,
                    work,
                    next_state="source_unavailable",
                    failure_code="source_unavailable",
                )
                processed += 1
                review_required += 1
                continue
            except MailboxMessageInvalid as exc:
                capacity_used += 1
                logger.warning("Scheduling run %s source invalid: %s", run.run_id, exc)
                _transition_source_problem(
                    store,
                    work,
                    next_state="manual_review",
                    failure_code="source_invalid",
                )
                processed += 1
                review_required += 1
                continue
            except MailboxError as exc:
                logger.warning(
                    "Scheduling run %s source temporarily unavailable: %s",
                    run.run_id,
                    exc,
                )
                continue

            capacity_used += 1
            source = SchedulingSource(
                sender=bounded_gateway_text(work.sender, MAX_GATEWAY_SENDER_CHARS),
                subject=bounded_gateway_text(work.subject, MAX_GATEWAY_SUBJECT_CHARS),
                received_at=work.received_at,
                body=bounded_gateway_text(content.body, MAX_GATEWAY_BODY_CHARS),
                attachment_names=bounded_gateway_attachment_names(content.attachment_names),
                organizer_address=organizer_address,
                configured_timezone=timezone,
                context_at=context_at,
            )
            source_sha256 = scheduling_source_sha256(source)
            current = run
            while current.state in {"detected", "extracting"}:
                try:
                    reservation = store.reserve_automation_extraction(
                        current.run_id,
                        current.state_version,
                        source_content_sha256=source_sha256,
                        context_at=context_at.isoformat(),
                        timezone=timezone,
                        body_char_limit=body_char_limit,
                        organizer_address=organizer_address,
                    )
                except AutomationSourceChanged:
                    _transition_source_problem(
                        store,
                        work,
                        next_state="manual_review",
                        failure_code="source_changed",
                    )
                    processed += 1
                    review_required += 1
                    break
                reserved_run = store.automation_run(current.run_id)
                if (
                    reserved_run is None
                    or reserved_run.current_payload_id != reservation.payload_id
                ):
                    raise RuntimeError("Scheduling extraction reservation was not current")
                feedback = _retry_feedback(store.automation_extraction_payloads(current.run_id))
                try:
                    result = model.extract_scheduling(
                        source=source,
                        feedback=feedback,
                        request_id=reservation.request_id,
                        request_started_at=datetime.fromisoformat(reservation.created_at),
                    )
                except GatewayModelError as exc:
                    logger.warning("Scheduling run %s extraction unavailable: %s", run.run_id, exc)
                    failed = store.record_automation_extraction_failure(
                        current.run_id,
                        reserved_run.state_version,
                        payload_id=reservation.payload_id,
                        error_code=exc.code,
                        retryable=exc.retryable,
                        retry_after_seconds=exc.retry_after_seconds,
                        now=_utc_now(),
                    )
                    if failed.state == "manual_review":
                        processed += 1
                        review_required += 1
                    break
                except ModelError as exc:
                    logger.warning("Scheduling run %s extraction unavailable: %s", run.run_id, exc)
                    store.record_automation_extraction_failure(
                        current.run_id,
                        reserved_run.state_version,
                        payload_id=reservation.payload_id,
                        error_code="model_unavailable",
                        retryable=True,
                        now=_utc_now(),
                    )
                    break
                accepted_state: str | None = None
                accepted_code: str | None = None
                if result.accepted:
                    assert result.extraction is not None
                    accepted_state, accepted_code = _accepted_extraction_outcome(
                        result.extraction.intent
                    )
                current = store.record_automation_extraction(
                    current.run_id,
                    reserved_run.state_version,
                    payload_id=reservation.payload_id,
                    result_sha256=result.result_sha256,
                    result_json=result.result_json,
                    violations=[
                        {"code": violation.code, "path": violation.path}
                        for violation in result.violations
                    ],
                    accepted_state=accepted_state,
                    accepted_code=accepted_code,
                )
                _acknowledge_gateway_result(
                    model,
                    reservation.request_id,
                    "persisted" if result.accepted else "application_rejected",
                )
                if current.state == "extracting":
                    continue
                processed += 1
                if current.state in {"ambiguous", "manual_review", "source_unavailable"}:
                    review_required += 1
                break
    return AutomationProcessing(
        processed,
        review_required,
        frozenset(attempted),
        purge_outcome.messages,
    )


def _proposal_request_sha256(
    attendees: tuple[str, ...],
    candidates: tuple[CalendarProposalCandidate, ...],
) -> str:
    document = {
        "attendees": list(attendees),
        "candidates": [
            {"start": item.start, "end": item.end, "timezone": item.timezone} for item in candidates
        ],
        "minimum_attendee_percentage": 100,
        "version": 1,
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _proposal_subject(subject: str) -> str:
    bounded = bounded_gateway_text(subject, MAX_GATEWAY_SUBJECT_CHARS)
    try:
        encoded = bounded.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Calendar proposal subject is invalid") from exc
    if len(encoded) <= MAX_CALENDAR_SUBJECT_BYTES:
        return bounded
    return encoded[:MAX_CALENDAR_SUBJECT_BYTES].decode("utf-8", errors="ignore")


def _proposal_extraction(work: AutomationProposalWork) -> SchedulingExtraction:
    result_json = work.extraction_payload.result_json
    if result_json is None:
        raise ValueError("Accepted scheduling extraction has no result")
    extraction = SchedulingExtraction.model_validate_json(result_json)
    if extraction.intent != "new_meeting":
        raise ValueError("Only new-meeting extraction can reach proposal")
    return extraction


def _proposal_candidates(
    extraction: SchedulingExtraction,
    *,
    observed_at: datetime,
) -> tuple[CalendarProposalCandidate, ...]:
    candidates = tuple(
        CalendarProposalCandidate(item.start, item.end, item.timezone)
        for item in extraction.proposed_times
    )
    try:
        starts = tuple(datetime.fromisoformat(candidate.start) for candidate in candidates)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Calendar proposal time is invalid") from exc
    if any(start.tzinfo is None or start.astimezone(UTC) <= observed_at for start in starts):
        raise ValueError("Calendar proposal time has passed")
    return candidates


def _transition_proposal_to_review(
    store: Store,
    work: AutomationProposalWork,
    *,
    observed_at: datetime,
) -> bool:
    current = store.automation_run(work.run.run_id)
    if current is None or (
        current.state != "proposing"
        or current.state_version != work.run.state_version
        or current.current_payload_id != work.extraction_payload.payload_id
    ):
        return False
    try:
        store.transition_automation_to_review(
            current.run_id,
            current.state_version,
            next_state="manual_review",
            failure_code="proposal_invalid",
            now=observed_at,
        )
    except RuntimeError:
        latest = store.automation_run(current.run_id)
        if latest is None or latest.state_version != current.state_version:
            return False
        raise
    return True


def process_scheduling_proposals(
    config: Config,
    store: Store,
    *,
    exclude_run_ids: frozenset[str] = frozenset(),
    limit: int = 25,
    now: datetime | None = None,
) -> AutomationProcessing:
    selected_now = now or _utc_now()
    if selected_now.tzinfo is None:
        raise ValueError("Calendar proposal processing time must be timezone-aware")
    fixed_now = selected_now.astimezone(UTC) if now is not None else None

    def current_time() -> datetime:
        return fixed_now or _utc_now().astimezone(UTC)

    processed = 0
    review_required = 0
    attempted: set[str] = set()
    capacity_used = 0
    cursor: tuple[str, str] | None = None
    while capacity_used < limit:
        page = store.proposable_automation_runs(limit, after=cursor)
        if not page:
            break
        for work in page:
            cursor = (work.run.created_at, work.run.run_id)
            if work.run.run_id in exclude_run_ids:
                continue
            if capacity_used >= limit:
                break
            access = _scheduling_proposal_authorization(
                config,
                store,
                provider=work.run.provider,
                account_id=work.run.account_id,
                expected_principal_key=work.run.calendar_principal_key,
            )
            if access is None:
                continue
            capacity_used += 1
            attempted.add(work.run.run_id)
            attempt_time = current_time()
            try:
                extraction = _proposal_extraction(work)
                attendees = tuple(item.email for item in extraction.attendees)
                candidates = _proposal_candidates(extraction, observed_at=attempt_time)
                request_sha256 = _proposal_request_sha256(attendees, candidates)
                result = find_meeting_time(access.authorization, attendees, candidates)
            except MicrosoftAuthorizationRejected as exc:
                logger.info(
                    "Scheduling run %s proposal authorization rejected: %s",
                    work.run.run_id,
                    exc,
                )
                store.revoke_calendar_grant_if_current(access.grant)
                continue
            except MicrosoftCalendarProposalRejected as exc:
                logger.warning(
                    "Scheduling run %s proposal rejected: %s",
                    work.run.run_id,
                    exc,
                )
                if _transition_proposal_to_review(store, work, observed_at=current_time()):
                    processed += 1
                    review_required += 1
                continue
            except Microsoft365Error as exc:
                logger.warning(
                    "Scheduling run %s proposal unavailable: %s",
                    work.run.run_id,
                    exc,
                )
                continue
            except ValueError as exc:
                logger.warning(
                    "Scheduling run %s proposal input invalid: %s",
                    work.run.run_id,
                    exc,
                )
                if _transition_proposal_to_review(store, work, observed_at=current_time()):
                    processed += 1
                    review_required += 1
                continue

            proposal = result.proposal
            result_time = current_time()
            try:
                store.record_automation_proposal(
                    work.run.run_id,
                    work.run.state_version,
                    extraction_payload_id=work.extraction_payload.payload_id,
                    request_sha256=request_sha256,
                    subject=_proposal_subject(work.subject),
                    attendees=attendees,
                    start=proposal.start if proposal is not None else None,
                    end=proposal.end if proposal is not None else None,
                    timezone=proposal.timezone if proposal is not None else None,
                    suggestion_reason=(
                        proposal.suggestion_reason if proposal is not None else None
                    ),
                    empty_reason=result.empty_reason,
                    observed_at=result_time,
                )
            except ValueError as exc:
                logger.warning(
                    "Scheduling run %s proposal could not be persisted safely: %s",
                    work.run.run_id,
                    exc,
                )
                if _transition_proposal_to_review(store, work, observed_at=result_time):
                    processed += 1
                    review_required += 1
                continue
            except RuntimeError:
                latest = store.automation_run(work.run.run_id)
                if latest is None or latest.state_version != work.run.state_version:
                    continue
                raise
            processed += 1
            if proposal is None:
                review_required += 1
    return AutomationProcessing(
        processed,
        review_required,
        frozenset(attempted),
        0,
    )


def process_scheduling_writes(
    config: Config,
    store: Store,
    *,
    run_id: str | None = None,
    limit: int = 25,
    now: datetime | None = None,
) -> AutomationProcessing:
    selected_now = now or _utc_now()
    if selected_now.tzinfo is None:
        raise ValueError("Calendar write processing time must be timezone-aware")
    stamp = selected_now.astimezone(UTC)
    processed = 0
    review_required = 0
    attempted: set[str] = set()
    work_items = store.pending_automation_calendar_writes(
        limit,
        run_id=run_id,
        now=stamp,
    )
    for work in work_items:
        attempted.add(work.run.run_id)
        current = store.automation_run(work.run.run_id)
        if current is None or current.state_version != work.run.state_version:
            continue
        if current.state in {"writing", "reconciling"}:
            code = (
                "write_outcome_unknown"
                if current.state == "writing"
                else "reconciliation_interrupted"
            )
            try:
                store.transition_automation_calendar_write(
                    current.run_id,
                    current.state_version,
                    next_state="unresolved",
                    failure_code=code,
                    now=stamp,
                )
            except RuntimeError:
                continue
            processed += 1
            continue

        access = _scheduling_write_authorization(
            config,
            store,
            provider=current.provider,
            account_id=current.account_id,
            expected_principal_key=current.calendar_principal_key,
            require_active_entitlements=False,
        )
        if access is None:
            continue

        if current.state == "write_authorized":
            try:
                writing = store.begin_automation_calendar_write(
                    current.run_id,
                    current.state_version,
                    now=stamp,
                )
            except RuntimeError:
                continue
            if writing.state == "source_unavailable":
                processed += 1
                review_required += 1
                continue
            proposal = work.proposal
            if proposal is None:
                store.transition_automation_calendar_write(
                    writing.run_id,
                    writing.state_version,
                    next_state="unresolved",
                    failure_code="write_payload_unavailable",
                    now=stamp,
                )
                processed += 1
                continue
            try:
                result = create_calendar_event(
                    access.authorization,
                    transaction_id=work.write.transaction_id,
                    subject=proposal.subject,
                    attendees=proposal.attendees,
                    start=work.write.start,
                    end=work.write.end,
                    timezone=work.write.timezone,
                )
            except MicrosoftAuthorizationRejected:
                store.revoke_calendar_grant_if_current(access.grant)
                store.transition_automation_calendar_write(
                    writing.run_id,
                    writing.state_version,
                    next_state="failed",
                    failure_code="write_authorization_rejected",
                    now=stamp,
                )
            except MicrosoftCalendarWriteRejected:
                store.transition_automation_calendar_write(
                    writing.run_id,
                    writing.state_version,
                    next_state="failed",
                    failure_code="write_rejected",
                    now=stamp,
                )
            except Microsoft365Error:
                store.transition_automation_calendar_write(
                    writing.run_id,
                    writing.state_version,
                    next_state="unresolved",
                    failure_code="write_outcome_unknown",
                    now=stamp,
                )
            except ValueError:
                store.transition_automation_calendar_write(
                    writing.run_id,
                    writing.state_version,
                    next_state="failed",
                    failure_code="write_payload_invalid",
                    now=stamp,
                )
            else:
                store.transition_automation_calendar_write(
                    writing.run_id,
                    writing.state_version,
                    next_state="completed",
                    graph_event_id=result.event_id,
                    now=stamp,
                )
            processed += 1
            continue

        if current.state != "unresolved":
            continue
        try:
            reconciling = store.transition_automation_calendar_write(
                current.run_id,
                current.state_version,
                next_state="reconciling",
                now=stamp,
            )
        except RuntimeError:
            continue
        try:
            result = find_calendar_event_by_transaction(
                access.authorization,
                transaction_id=work.write.transaction_id,
                window_start=work.write.start,
                window_end=work.write.end,
            )
        except MicrosoftAuthorizationRejected:
            store.revoke_calendar_grant_if_current(access.grant)
            store.transition_automation_calendar_write(
                reconciling.run_id,
                reconciling.state_version,
                next_state="unresolved",
                failure_code="reconciliation_authorization_rejected",
                now=stamp,
            )
        except Microsoft365Error:
            store.transition_automation_calendar_write(
                reconciling.run_id,
                reconciling.state_version,
                next_state="unresolved",
                failure_code="reconciliation_unavailable",
                now=stamp,
            )
        else:
            if result is None:
                store.transition_automation_calendar_write(
                    reconciling.run_id,
                    reconciling.state_version,
                    next_state="unresolved",
                    failure_code="event_not_found",
                    now=stamp,
                )
            else:
                store.transition_automation_calendar_write(
                    reconciling.run_id,
                    reconciling.state_version,
                    next_state="completed",
                    graph_event_id=result.event_id,
                    now=stamp,
                )
        processed += 1
    return AutomationProcessing(processed, review_required, frozenset(attempted), 0)


def decide_scheduling_proposal(
    config: Config,
    store: Store,
    *,
    run_id: str,
    expected_state_version: int,
    proposal_version: int,
    proposal_sha256: str,
    decision: str,
    now: datetime | None = None,
) -> SchedulingDecision:
    if not feature_entitlements_active(CONNECT_FEATURE_ID, AUTOMATIONS_FEATURE_ID):
        raise PermissionError("Scheduling automation requires an active entitlement")
    current = store.automation_run(run_id)
    if current is None:
        raise KeyError(run_id)
    if current.provider != MICROSOFT365_PROVIDER:
        raise ValueError("Scheduling automation requires a Microsoft 365 account")
    existing_write = store.automation_calendar_write(run_id) if decision == "confirm" else None
    decision_args = {
        "proposal_version": proposal_version,
        "proposal_sha256": proposal_sha256,
        "decision": decision,
        "now": now,
    }
    if decision == "confirm" and existing_write is None:
        try:
            updated = store.decide_automation_proposal(
                run_id,
                expected_state_version,
                write_authorized=False,
                **decision_args,
            )
        except PermissionError:
            access = _scheduling_write_authorization(
                config,
                store,
                provider=current.provider,
                account_id=current.account_id,
                expected_principal_key=current.calendar_principal_key,
            )
            if access is None:
                raise PermissionError(
                    "Microsoft calendar write permission is unavailable"
                ) from None
            updated = store.decide_automation_proposal(
                run_id,
                expected_state_version,
                write_authorized=True,
                **decision_args,
            )
    else:
        updated = store.decide_automation_proposal(
            run_id,
            expected_state_version,
            **decision_args,
        )
    if decision == "confirm" and updated.state == "write_authorized":
        process_scheduling_writes(config, store, run_id=run_id, limit=1, now=now)
        updated = store.automation_run(run_id)
        if updated is None:
            raise RuntimeError("Scheduling automation disappeared after confirmation")
    return SchedulingDecision(updated, store.automation_calendar_write(run_id))


def _received_at_or_none(value: str, *, observed_at: datetime) -> datetime | None:
    try:
        received = datetime.fromisoformat(value)
        if received.tzinfo is None:
            return None
        return min(received.astimezone(UTC), observed_at)
    except (OverflowError, ValueError):
        return None


def _copy_received_in_retention(
    metadata: MessageMetadata, *, checked_at: datetime, retention_cutoff: datetime
) -> datetime | None:
    """A fetched copy's received time if it may be captured (contract D-scope).

    The one copy-level rule every capture path applies: the time parses, is clamped
    to the check, and is at or after the cutoff. None otherwise. A stored logical
    message's retention is the store's rule (Store.retained_logical_messages).
    """
    received_at = _received_at_or_none(metadata.received_at, observed_at=checked_at)
    if received_at is None or received_at < retention_cutoff:
        return None
    return received_at


def _deliver_automation_review_intent(
    config: Config,
    store: Store,
    intent: NotificationIntent,
    sender_names: dict[str, str | None],
    *,
    dry_run: bool,
) -> bool:
    if not config.notifications_enabled:
        return False
    label = sender_names.get(intent.sender) or intent.sender_name or intent.sender
    try:
        send_review(
            label,
            intent.subject,
            intent.summary or "A scheduling mention needs manual review.",
            ntfy_topic=config.ntfy_topic,
            ntfy_url=config.ntfy_url,
            ntfy_content_disclosure_acknowledged=(config.ntfy_content_disclosure_acknowledged),
            dry_run=dry_run,
        )
        if not dry_run:
            store.acknowledge_notification(
                message_id=intent.message_id,
                kind=intent.kind,
                analysis_at=intent.analysis_at,
                subject_type=intent.subject_type,
                subject_id=intent.subject_id,
                revision=intent.revision,
            )
        return True
    except NotificationError as exc:
        logger.warning("Automation review notification unavailable: %s", exc)
        return False


def reconcile_mailbox_session_identity(
    store: Store,
    mailbox: MailboxSession,
    *,
    dry_run: bool,
) -> str:
    """Verify or atomically reconcile the identity of an open mailbox session."""
    account = store.mail_account(mailbox.provider, mailbox.account_id)
    if account is None:
        raise MailboxAccountUnavailable("The selected email account is not configured")
    authenticated_address = mailbox_session_address(mailbox)
    if authenticated_address is not None and authenticated_address != account.address:
        raise MailboxIdentityChanged("mailbox address changed")
    mailbox_identity_key = mailbox_session_identity_key(mailbox)
    if dry_run:
        store.require_mailbox_identity(
            mailbox.provider,
            mailbox.account_id,
            mailbox_identity_key,
        )
        return mailbox_identity_key

    state = store.state(provider=mailbox.provider, account_id=mailbox.account_id)
    legacy_status: str | None = None
    if account.mailbox_identity_key is None:
        legacy_status = "unresolved"
        if (
            mailbox.provider == IMAP_PROVIDER
            and isinstance(mailbox.gateway, ImapGateway)
            and state is not None
        ):
            try:
                legacy_epoch = imap_cursor_epoch(state[0])
            except MailboxError:
                legacy_epoch = None
            if legacy_epoch == mailbox.gateway.mailbox_epoch():
                legacy_status = "continuity_proven"
    preserve_cursor = account.mailbox_identity_key is None or mailbox.provider == IMAP_PROVIDER
    store.reconcile_mailbox_identity(
        mailbox.provider,
        mailbox.account_id,
        mailbox_identity_key,
        legacy_status=legacy_status,
        preserve_cursor=preserve_cursor,
    )
    return mailbox_identity_key


class Watcher:
    def __init__(
        self,
        config: Config,
        store: Store,
        mailbox: MailboxGateway | MailboxSession,
        model: ModelRuntime,
    ):
        self.config = config
        self.store = store
        self.mailbox = (
            mailbox if isinstance(mailbox, MailboxSession) else default_mailbox_session(mailbox)
        )
        self.gateway = self.mailbox.gateway
        self.model = model
        self.sender_names = {sender.email: sender.name for sender in config.senders}
        self.admission_sender_names = {
            address: admission_sender_display_name(name)
            for address, name in self.sender_names.items()
            if address in config.allowlist
        }

    def _active_gmail_label_selectors(
        self, mailbox_identity_key: str
    ) -> tuple[GmailLabelSelectorLike, ...]:
        if self.mailbox.provider != "gmail":
            return ()
        reader = getattr(self.store, "gmail_current_label_selectors", None)
        if not callable(reader):
            return ()
        selectors = tuple(reader(self.mailbox.account_id, mailbox_identity_key))
        if not selectors:
            return ()
        catalog_reader = getattr(self.gateway, "label_catalog", None)
        if not callable(catalog_reader):
            raise MailboxError("The Gmail adapter cannot validate selected labels")
        catalog = tuple(catalog_reader())
        active_labels = {
            item.label_id: item.display_name
            for item in catalog
            if getattr(item, "label_type", None) == "user"
        }
        return tuple(
            RecoveryLabelGrant(
                selector_id=selector.selector_id,
                provider=selector.provider,
                account_id=selector.account_id,
                mailbox_identity_key=selector.mailbox_identity_key,
                label_id=selector.label_id,
                selected_display_name=active_labels[selector.label_id],
            )
            for selector in selectors
            if selector.label_id in active_labels
        )

    def bootstrap(self) -> str:
        with mailbox_polling_session(self.gateway):
            mailbox_identity_key = mailbox_session_identity_key(self.mailbox)
            self.store.reconcile_mailbox_identity(
                self.mailbox.provider,
                self.mailbox.account_id,
                mailbox_identity_key,
                legacy_status="replacement",
                preserve_cursor=self.mailbox.provider == IMAP_PROVIDER,
            )
            cursor = self.gateway.initial_cursor()
            self.store.set_state(
                cursor,
                provider=self.mailbox.provider,
                account_id=self.mailbox.account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
        return cursor

    @staticmethod
    def inactive_result(
        config: Config,
        store: Store,
        *,
        dry_run: bool,
        reason: str | None = None,
    ) -> dict[str, int | bool | str]:
        purged = 0 if dry_run else store.purge(config.retention_days)
        result: dict[str, int | bool | str] = {
            "active": False,
            "discovered": 0,
            "summarized": 0,
            "fallback_notified": 0,
            "purged": purged,
            "stale_cursor_recovered": False,
        }
        if reason is not None:
            result["reason"] = reason
        return result

    @staticmethod
    def recovery_backoff_result(
        state: GmailRecoveryState,
    ) -> dict[str, int | bool | str]:
        result: dict[str, int | bool | str] = {
            "active": True,
            "discovered": 0,
            "summarized": 0,
            "fallback_notified": 0,
            "purged": 0,
            "stale_cursor_recovered": True,
            "recovery_pending": True,
            "recovery_state": state.state,
        }
        if state.failure_code is not None:
            result["recovery_failure_code"] = state.failure_code
        if state.next_retry_at is not None:
            result["recovery_next_retry_at"] = state.next_retry_at
        return result

    def _settle_sent(
        self,
        *,
        folders: frozenset[str],
        mailbox_identity_key: str | None,
        dry_run: bool,
        now: datetime | None = None,
    ) -> str | None:
        """Decide Sent once per check, before any Inbox work (contract D-scope, D-ops).

        Every path a check completes by runs this first, so the scope Health reports
        is never a previous check's. The first time Sent is available it also records
        the folder's current position: taken before the Inbox poll, so mail sent while
        the Inbox is polled is after it and reaches this check's Sent poll. Returns
        the scope, or None when Sent could not be read: a Sent error never stops the
        Inbox check, and the scope stays as last recorded. A dry run records nothing.
        """
        provider = self.mailbox.provider
        account_id = self.mailbox.account_id
        checked_at = now or datetime.now(UTC)
        if SENT_LOCATION not in folders:
            scope = SENT_SCOPE_NOT_POLLED
        elif provider == "gmail":
            # Gmail's one mailbox-wide history carries SENT events: no Sent cursor.
            scope = SENT_SCOPE_AVAILABLE
        else:
            scope_reader = getattr(self.gateway, "sent_scope", None)
            if not callable(scope_reader):
                return None
            try:
                scope = scope_reader()
            except MailboxError as exc:
                logger.warning("Sent folder could not be read; the Inbox goes on: %s", exc)
                return None
        if dry_run:
            return scope
        self.store.set_sent_scope(provider, account_id, scope, now=checked_at)
        if scope != SENT_SCOPE_AVAILABLE or provider == "gmail" or mailbox_identity_key is None:
            return scope
        folder_scope = {
            "provider": provider,
            "account_id": account_id,
            "mailbox_identity_key": mailbox_identity_key,
            "folder": SENT_LOCATION,
        }
        if self.store.folder_state(**folder_scope) is None:
            try:
                self.store.set_folder_state(
                    self.gateway.sent_initial_cursor(), at=checked_at, **folder_scope
                )
            except MailboxError as exc:
                logger.warning("Sent folder position unread; the next check retries: %s", exc)
        return scope

    def check(
        self, *, dry_run: bool = False, deliver_notifications: bool = True
    ) -> dict[str, int | bool | str]:
        account = self.store.mail_account(self.mailbox.provider, self.mailbox.account_id)
        recorded_identity = account.mailbox_identity_key if account is not None else None
        if self.mailbox.provider == "gmail" and dry_run:
            recovery_state = self.store.gmail_recovery_state(self.mailbox.account_id)
            checked_at = datetime.now(UTC)
            if (
                recovery_state is not None
                and recorded_identity == recovery_state.mailbox_identity_key
                and not self._retry_due(recovery_state.next_retry_at, checked_at)
            ):
                return self.recovery_backoff_result(recovery_state)
        pending_current_identity = (
            recorded_identity is not None
            and self.store.has_current_pending_mailbox_work(
                self.mailbox.provider,
                self.mailbox.account_id,
                recorded_identity,
            )
        )
        gmail_watch_configured = self.mailbox.provider == "gmail" and (
            _gmail_label_watch_configured(self.store)
        )
        if (
            not self.admission_sender_names
            and not gmail_watch_configured
            and not pending_current_identity
        ):
            self._settle_sent(folders=frozenset(), mailbox_identity_key=None, dry_run=dry_run)
            return self.inactive_result(self.config, self.store, dry_run=dry_run)
        with mailbox_polling_session(self.gateway):
            mailbox_identity_key = reconcile_mailbox_session_identity(
                self.store,
                self.mailbox,
                dry_run=dry_run,
            )
            recovery_state = (
                self.store.gmail_recovery_state(self.mailbox.account_id)
                if self.mailbox.provider == "gmail"
                else None
            )
            try:
                label_selectors = (
                    ()
                    if recovery_state is not None
                    else self._active_gmail_label_selectors(mailbox_identity_key)
                )
            except (GmailLabelCatalogInvalid, GmailLabelCatalogUnavailable) as catalog_error:
                checked_at = datetime.now(UTC)
                try:
                    self._process_pending(
                        dry_run=dry_run,
                        deliver_notifications=deliver_notifications,
                        extra=[],
                        retention_cutoff=checked_at
                        - timedelta(days=self.config.retention_days),
                        retention_observed_at=checked_at,
                        mailbox_identity_key=mailbox_identity_key,
                    )
                except GmailAuthorizationRejected:
                    raise
                except GmailError:
                    raise catalog_error from None
                raise
            if (
                not self.admission_sender_names
                and not label_selectors
                and recovery_state is None
            ):
                # Nothing is polled on this path, Sent included.
                self._settle_sent(
                    folders=frozenset(), mailbox_identity_key=None, dry_run=dry_run
                )
                pending_current_identity = self.store.has_current_pending_mailbox_work(
                    self.mailbox.provider,
                    self.mailbox.account_id,
                    mailbox_identity_key,
                )
                if pending_current_identity:
                    checked_at = datetime.now(UTC)
                    return self._finish_active_result(
                        added=0,
                        purged=0,
                        recovered=False,
                        dry_run=dry_run,
                        deliver_notifications=deliver_notifications,
                        checked_at=checked_at,
                        retention_cutoff=checked_at
                        - timedelta(days=self.config.retention_days),
                        mailbox_identity_key=mailbox_identity_key,
                    )
                current_label_rows = (
                    self.store.gmail_current_label_selectors(
                        self.mailbox.account_id,
                        mailbox_identity_key,
                    )
                    if self.mailbox.provider == "gmail"
                    else ()
                )
                return self.inactive_result(
                    self.config,
                    self.store,
                    dry_run=dry_run,
                    reason=(
                        "gmail_label_selectors_inactive"
                        if current_label_rows
                        else None
                    ),
                )
            if (
                not dry_run
                and self.store.state(
                    provider=self.mailbox.provider,
                    account_id=self.mailbox.account_id,
                )
                is None
            ):
                self.store.set_state(
                    self.gateway.initial_cursor(),
                    provider=self.mailbox.provider,
                    account_id=self.mailbox.account_id,
                    mailbox_identity_key=mailbox_identity_key,
                )
            return self._check_active(
                dry_run=dry_run,
                deliver_notifications=deliver_notifications,
                mailbox_identity_key=mailbox_identity_key,
                label_selectors=label_selectors,
            )

    @staticmethod
    def _retry_due(next_retry_at: str | None, now: datetime) -> bool:
        if next_retry_at is None:
            return True
        try:
            return datetime.fromisoformat(next_retry_at).astimezone(UTC) <= now
        except (TypeError, ValueError, OverflowError):
            return False

    def _record_recovery_backoff(
        self,
        state: object,
        mailbox_identity_key: str,
        failure_code: str,
        *,
        now: datetime,
    ) -> None:
        retry_count = int(getattr(state, "consecutive_retry_count", 0))
        delay_minutes = min(15, 2**min(retry_count, 4))
        self.store.record_gmail_recovery_backoff(
            self.mailbox.account_id,
            mailbox_identity_key,
            failure_code=failure_code,
            next_retry_at=(now + timedelta(minutes=delay_minutes)).isoformat(),
            now=now,
        )

    def _current_recovery_grants(
        self,
        state: GmailRecoveryState,
        mailbox_identity_key: str,
    ) -> tuple[dict[str, str | None], tuple[RecoveryLabelGrant, ...]]:
        frozen_senders = dict(state.sender_snapshot)
        current_senders = {
            address: frozen_senders[address]
            for address in self.admission_sender_names
            if address in frozen_senders
        }
        frozen_selectors = {
            selector.selector_id: selector
            for selector in state.selector_snapshot
        }
        current_selectors = self.store.gmail_current_label_selectors(
            self.mailbox.account_id,
            mailbox_identity_key,
        )
        label_grants = tuple(
            RecoveryLabelGrant(
                selector_id=selector.selector_id,
                provider="gmail",
                account_id=self.mailbox.account_id,
                mailbox_identity_key=mailbox_identity_key,
                label_id=selector.label_id,
                selected_display_name=frozen_selectors[selector.selector_id].display_name,
            )
            for selector in current_selectors
            if selector.selector_id in frozen_selectors
            and selector.label_id == frozen_selectors[selector.selector_id].label_id
        )
        return current_senders, label_grants

    @staticmethod
    def _recovery_retention_cutoff(state: GmailRecoveryState) -> datetime:
        try:
            cutoff = datetime.fromisoformat(state.retention_cutoff).astimezone(UTC)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("Gmail recovery retention cutoff is invalid") from exc
        return cutoff

    def _preview_gmail_recovery(
        self,
        *,
        state: GmailRecoveryState,
        mailbox_identity_key: str,
        checked_at: datetime,
        deliver_notifications: bool,
        folders: frozenset[str],
    ) -> dict[str, int | bool | str]:
        """Preview the frozen recovery window without changing durable progress."""
        deadline = time.monotonic() + 30.0
        if not self._retry_due(state.next_retry_at, checked_at):
            return self.recovery_backoff_result(state)
        same_scope = state.query_scope == folder_scope_key(folders)
        if state.page_loaded and same_scope:
            message_ids = state.current_page_ids[state.next_index :]
        else:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                message_ids = ()
            else:
                # A page saved under another folder scope is not previewed; the
                # window is read afresh under this one, without saving anything.
                message_ids, _next_page_token = self.gateway.recovery_page(
                    state.page_token if same_scope else None,
                    state.recovery_after_exclusive_epoch,
                    state.recovery_before_exclusive_epoch,
                    200,
                    timeout_seconds=remaining_seconds,
                )

        current_senders, current_selectors = self._current_recovery_grants(
            state,
            mailbox_identity_key,
        )
        retention_cutoff = self._recovery_retention_cutoff(state)
        dry_run_messages: list[PendingMessage] = []
        for provider_message_id in message_ids:
            if time.monotonic() >= deadline:
                break
            if self.store.has_seen_message(
                provider_message_id,
                provider=self.mailbox.provider,
                account_id=self.mailbox.account_id,
                mailbox_identity_key=mailbox_identity_key,
            ):
                continue
            try:
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    break
                metadata = self.gateway.metadata(
                    provider_message_id,
                    timeout_seconds=remaining_seconds,
                )
            except (MailboxMessageUnavailable, MailboxMessageInvalid):
                continue
            if metadata.message_id != provider_message_id:
                raise RuntimeError(
                    "Mailbox metadata identity did not match the recovery page"
                )
            admission = match_mailbox_admission(
                metadata=metadata,
                provider="gmail",
                account_id=self.mailbox.account_id,
                mailbox_identity_key=mailbox_identity_key,
                exact_senders=current_senders,
                label_selectors=current_selectors,
                admitted_at=checked_at,
                folders=folders,
            )
            received_at = _copy_received_in_retention(
                metadata, checked_at=checked_at, retention_cutoff=retention_cutoff
            )
            if admission is None or received_at is None:
                continue
            self._preview_candidate(
                metadata,
                {
                    "message_id": scoped_message_id(
                        "gmail",
                        self.mailbox.account_id,
                        metadata.message_id,
                        mailbox_identity_key,
                    ),
                    "provider": "gmail",
                    "account_id": self.mailbox.account_id,
                    "provider_message_id": metadata.message_id,
                    "thread_id": metadata.thread_id,
                    "sender": metadata.sender,
                    "sender_name": (
                        metadata.sender_name or self.sender_names.get(metadata.sender)
                    ),
                    "subject": metadata.subject,
                    "received_at": received_at.isoformat(),
                    "mailbox_identity_key": mailbox_identity_key,
                },
                mailbox_identity_key,
                dry_run_messages,
            )

        return self._finish_active_result(
            added=len(dry_run_messages),
            purged=0,
            recovered=True,
            dry_run=True,
            deliver_notifications=deliver_notifications,
            checked_at=checked_at,
            retention_cutoff=retention_cutoff,
            mailbox_identity_key=mailbox_identity_key,
            dry_run_messages=dry_run_messages,
            recovery_status=state,
        )

    def _run_gmail_recovery(
        self,
        *,
        mailbox_identity_key: str,
        checked_at: datetime,
        folders: frozenset[str],
    ) -> tuple[int, bool]:
        state = self.store.gmail_recovery_state(self.mailbox.account_id)
        if state is None:
            raise RuntimeError("Gmail recovery state was not initialized")
        if state.query_scope != folder_scope_key(folders):
            # The saved page belongs to another folder scope: restart paging under
            # this one (contract D-ops); captured ids are skipped as seen.
            state = self.store.restart_gmail_recovery_page(
                self.mailbox.account_id,
                mailbox_identity_key,
                folder_scope_key(folders),
                now=checked_at,
            )
        retention_cutoff = self._recovery_retention_cutoff(state)
        if not self._retry_due(state.next_retry_at, checked_at):
            return 0, False
        deadline = time.monotonic() + 30.0
        terminal = 0
        added = 0
        while terminal < 200 and time.monotonic() < deadline:
            state = self.store.gmail_recovery_state(self.mailbox.account_id)
            if state is None:
                return added, True
            if not state.page_loaded:
                try:
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        break
                    message_ids, next_page_token = self.gateway.recovery_page(
                        state.page_token,
                        state.recovery_after_exclusive_epoch,
                        state.recovery_before_exclusive_epoch,
                        200,
                        timeout_seconds=remaining_seconds,
                    )
                except GmailRecoveryPageTokenInvalid:
                    invalid_count = state.invalid_page_token_count + 1
                    delay_minutes = (
                        2 ** (invalid_count - 1) if invalid_count <= 4 else 60
                    )
                    self.store.record_gmail_recovery_invalid_page_token(
                        self.mailbox.account_id,
                        mailbox_identity_key,
                        next_retry_at=(
                            checked_at + timedelta(minutes=delay_minutes)
                        ).isoformat(),
                        now=checked_at,
                    )
                    return added, False
                except GmailRecoveryPageInvalid:
                    self._record_recovery_backoff(
                        state,
                        mailbox_identity_key,
                        "gmail_recovery_page_invalid",
                        now=checked_at,
                    )
                    return added, False
                except GmailAuthorizationRejected:
                    raise
                except MailboxError:
                    self._record_recovery_backoff(
                        state,
                        mailbox_identity_key,
                        "gmail_recovery_provider_unavailable",
                        now=checked_at,
                    )
                    return added, False
                state = self.store.store_gmail_recovery_page(
                    self.mailbox.account_id,
                    mailbox_identity_key,
                    message_ids,
                    next_page_token,
                    now=checked_at,
                )

            if state.next_index == len(state.current_page_ids):
                if self.store.finish_gmail_recovery_page(
                    self.mailbox.account_id,
                    mailbox_identity_key,
                    now=checked_at,
                ):
                    self.store.complete_gmail_recovery(
                        self.mailbox.account_id,
                        mailbox_identity_key,
                        now=checked_at,
                    )
                    return added, True
                continue

            provider_message_id = state.current_page_ids[state.next_index]
            if self.store.has_seen_message(
                provider_message_id,
                provider=self.mailbox.provider,
                account_id=self.mailbox.account_id,
                mailbox_identity_key=mailbox_identity_key,
            ):
                # The lost interval may have moved it, and the search does not say
                # where it is now: an incomplete observation clears its stamp, so
                # discovery observes it again once this recovery makes coverage stale.
                self.store.record_message_location(
                    provider=self.mailbox.provider,
                    account_id=self.mailbox.account_id,
                    mailbox_identity_key=mailbox_identity_key,
                    provider_message_id=provider_message_id,
                    locations=frozenset(),
                    scope_complete=False,
                    headers_observed=False,
                    now=checked_at,
                )
                self.store.finish_gmail_recovery_candidate(
                    self.mailbox.account_id,
                    mailbox_identity_key,
                    provider_message_id,
                    now=checked_at,
                )
                terminal += 1
                continue
            try:
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    break
                metadata = self.gateway.metadata(
                    provider_message_id,
                    timeout_seconds=remaining_seconds,
                )
            except (MailboxMessageUnavailable, MailboxMessageInvalid):
                self.store.finish_gmail_recovery_candidate(
                    self.mailbox.account_id,
                    mailbox_identity_key,
                    provider_message_id,
                    now=checked_at,
                )
                terminal += 1
                continue
            except GmailAuthorizationRejected:
                raise
            except MailboxError:
                self._record_recovery_backoff(
                    state,
                    mailbox_identity_key,
                    "gmail_recovery_provider_unavailable",
                    now=checked_at,
                )
                return added, False
            if metadata.message_id != provider_message_id:
                raise RuntimeError("Mailbox metadata identity did not match the recovery page")

            current_senders, current_selectors = self._current_recovery_grants(
                state,
                mailbox_identity_key,
            )
            admission = match_mailbox_admission(
                metadata=metadata,
                provider="gmail",
                account_id=self.mailbox.account_id,
                mailbox_identity_key=mailbox_identity_key,
                exact_senders=current_senders,
                label_selectors=current_selectors,
                admitted_at=checked_at,
                folders=folders,
            )
            received_at = _copy_received_in_retention(
                metadata, checked_at=checked_at, retention_cutoff=retention_cutoff
            )
            if admission is None or received_at is None:
                self.store.finish_gmail_recovery_candidate(
                    self.mailbox.account_id,
                    mailbox_identity_key,
                    provider_message_id,
                    now=checked_at,
                )
            else:
                inserted = self.store.finish_gmail_recovery_candidate(
                    self.mailbox.account_id,
                    mailbox_identity_key,
                    provider_message_id,
                    message=GmailRecoveryMessage(
                        message_id=scoped_message_id(
                            "gmail",
                            self.mailbox.account_id,
                            provider_message_id,
                            mailbox_identity_key,
                        ),
                        thread_id=metadata.thread_id,
                        sender=metadata.sender,
                        sender_name=(
                            metadata.sender_name or self.sender_names.get(metadata.sender)
                        ),
                        subject=metadata.subject,
                        received_at=received_at.isoformat(),
                        rfc_message_id=metadata.rfc_message_id,
                        reply_ids=metadata.reply_ids,
                        to=metadata.to,
                        cc=metadata.cc,
                        locations=_admitted_locations(metadata, metadata.labels, folders),
                        capture_timezone=self.config.timezone,
                        scope_complete=_scope_complete(folders),
                    ),
                    admission=admission.provenance(),
                    metadata_label_ids=metadata.labels,
                    now=checked_at,
                )
                added += int(inserted)
            terminal += 1
        return added, False

    def _finish_active_result(
        self,
        *,
        added: int,
        purged: int,
        recovered: bool,
        dry_run: bool,
        deliver_notifications: bool,
        checked_at: datetime,
        retention_cutoff: datetime,
        mailbox_identity_key: str,
        dry_run_messages: list[PendingMessage] | None = None,
        recovery_incomplete: bool = False,
        recovery_reason: str | None = None,
        recovery_status: GmailRecoveryState | None = None,
    ) -> dict[str, int | bool | str]:
        summarized, fallback = self._process_pending(
            dry_run=dry_run,
            deliver_notifications=deliver_notifications,
            extra=dry_run_messages or [],
            retention_cutoff=retention_cutoff,
            retention_observed_at=checked_at,
            mailbox_identity_key=mailbox_identity_key,
        )
        if not dry_run:
            purged += self.store.purge(self.config.retention_days, now=checked_at)
        result: dict[str, int | bool | str] = {
            "active": True,
            "discovered": added,
            "summarized": summarized,
            "fallback_notified": fallback,
            "purged": purged,
            "stale_cursor_recovered": recovered,
        }
        if recovery_incomplete:
            result["incomplete"] = True
            if recovery_reason is not None:
                result["reason"] = recovery_reason
        if recovery_status is not None:
            result["recovery_pending"] = True
            result["recovery_state"] = recovery_status.state
            if recovery_status.failure_code is not None:
                result["recovery_failure_code"] = recovery_status.failure_code
            if recovery_status.next_retry_at is not None:
                result["recovery_next_retry_at"] = recovery_status.next_retry_at
        return result

    def _check_active(
        self,
        *,
        dry_run: bool,
        deliver_notifications: bool,
        mailbox_identity_key: str,
        label_selectors: tuple[GmailLabelSelectorLike, ...],
    ) -> dict[str, int | bool | str]:
        checked_at = datetime.now(UTC)
        retention_cutoff = checked_at - timedelta(days=self.config.retention_days)
        gated_allowed = self._gated_class_allowed()
        folders = _folders_in_scope(gated_allowed)
        _scope_gateway(self.gateway, folders)
        sent_scope = self._settle_sent(
            folders=folders,
            mailbox_identity_key=mailbox_identity_key,
            dry_run=dry_run,
            now=checked_at,
        )
        self._preview_identities: set[tuple[str, str, str, str]] = set()
        purged = 0 if dry_run else self.store.purge(self.config.retention_days, now=checked_at)
        state = self.store.state(
            provider=self.mailbox.provider,
            account_id=self.mailbox.account_id,
        )
        if not state:
            raise RuntimeError("Watcher is not initialized. Run: eom-mail-watch setup")
        cursor, last_success = state
        recovered = False
        recovery_incomplete = False
        recovery_reason: str | None = None
        if self.mailbox.provider == "gmail" and not dry_run:
            recovery_state = self.store.gmail_recovery_state(self.mailbox.account_id)
            if recovery_state is not None:
                recovery_retention_cutoff = self._recovery_retention_cutoff(
                    recovery_state
                )
                added, completed = self._run_gmail_recovery(
                    mailbox_identity_key=mailbox_identity_key,
                    checked_at=checked_at,
                    folders=folders,
                )
                pending_recovery = (
                    None
                    if completed
                    else self.store.gmail_recovery_state(self.mailbox.account_id)
                )
                if not completed and pending_recovery is None:
                    raise RuntimeError(
                        "Gmail recovery stopped without durable state"
                    )
                return self._finish_active_result(
                    added=added,
                    purged=purged,
                    recovered=True,
                    dry_run=False,
                    deliver_notifications=deliver_notifications,
                    checked_at=checked_at,
                    retention_cutoff=recovery_retention_cutoff,
                    mailbox_identity_key=mailbox_identity_key,
                    recovery_status=pending_recovery,
                )
        if self.mailbox.provider == "gmail" and dry_run:
            recovery_state = self.store.gmail_recovery_state(self.mailbox.account_id)
            if recovery_state is not None:
                return self._preview_gmail_recovery(
                    state=recovery_state,
                    mailbox_identity_key=mailbox_identity_key,
                    checked_at=checked_at,
                    deliver_notifications=deliver_notifications,
                    folders=folders,
                )
        try:
            changes = self.gateway.changes_since(cursor)
        except StaleMailboxCursor as exc:
            account = self.store.mail_account(
                self.mailbox.provider,
                self.mailbox.account_id,
            )
            if (
                account is not None
                and account.legacy_identity_status == "unresolved"
                and self.store.has_unexpired_legacy_mailbox_markers(
                    self.mailbox.provider,
                    self.mailbox.account_id,
                    retention_cutoff=retention_cutoff,
                    now=checked_at,
                )
            ):
                raise LegacyMailboxIdentityUnverified(
                    "Stale recovery is blocked by unresolved legacy mailbox identity markers"
                ) from exc
            recovered = True
            since = _recovery_since(last_success, retention_cutoff)
            if not dry_run:
                # A gap in polling: folder changes may have gone unobserved, so every
                # observation of the account is incomplete until discovery looks.
                self.store.clear_location_stamps(
                    self.mailbox.provider, self.mailbox.account_id, mailbox_identity_key
                )
            if self.mailbox.provider == "gmail" and dry_run:
                replacement_cursor = self.gateway.initial_cursor()
                if not replacement_cursor.isdigit():
                    raise MailboxError(
                        "Gmail returned an invalid replacement history cursor"
                    ) from exc
                sampled_at = datetime.now(UTC)
                since = max(
                    datetime.fromisoformat(last_success).astimezone(UTC)
                    - timedelta(minutes=5),
                    sampled_at - timedelta(days=self.config.retention_days),
                )
                after_epoch = math.ceil(since.timestamp()) - 1
                before_epoch = math.floor(sampled_at.timestamp()) + 1
                if after_epoch < 0 or before_epoch <= after_epoch:
                    raise MailboxError("Gmail recovery window is invalid") from exc
                message_ids, next_page_token = self.gateway.recovery_page(
                    None,
                    after_epoch,
                    before_epoch,
                    200,
                )
                changes = MailboxChanges(message_ids, replacement_cursor)
                checked_at = sampled_at
                retention_cutoff = sampled_at - timedelta(
                    days=self.config.retention_days
                )
                recovery_incomplete = next_page_token is not None
                if recovery_incomplete:
                    recovery_reason = "recovery_truncated"
            elif self.mailbox.provider == "gmail":
                selector_set = self.store.gmail_label_selector_set(self.mailbox.account_id)
                if selector_set is None:
                    raise MailboxIdentityChanged("mailbox identity changed") from exc
                replacement_cursor = self.gateway.initial_cursor()
                if not replacement_cursor.isdigit():
                    raise MailboxError(
                        "Gmail returned an invalid replacement history cursor"
                    ) from exc
                sampled_at = datetime.now(UTC)
                retention_cutoff = sampled_at - timedelta(
                    days=self.config.retention_days
                )
                since = max(
                    datetime.fromisoformat(last_success).astimezone(UTC)
                    - timedelta(minutes=5),
                    retention_cutoff,
                )
                after_epoch = math.ceil(since.timestamp()) - 1
                before_epoch = math.floor(sampled_at.timestamp()) + 1
                if after_epoch < 0 or before_epoch <= after_epoch:
                    raise MailboxError("Gmail recovery window is invalid") from exc
                self.store.create_gmail_recovery_state(
                    self.mailbox.account_id,
                    mailbox_identity_key,
                    selector_set.revision,
                    tuple(sorted(self.admission_sender_names.items())),
                    label_selectors,
                    after_epoch,
                    before_epoch,
                    replacement_cursor,
                    retention_cutoff=retention_cutoff,
                    query_scope=folder_scope_key(folders),
                    now=sampled_at,
                )
                added, completed = self._run_gmail_recovery(
                    mailbox_identity_key=mailbox_identity_key,
                    checked_at=sampled_at,
                    folders=folders,
                )
                pending_recovery = (
                    None
                    if completed
                    else self.store.gmail_recovery_state(self.mailbox.account_id)
                )
                if not completed and pending_recovery is None:
                    raise RuntimeError(
                        "Gmail recovery stopped without durable state"
                    ) from exc
                return self._finish_active_result(
                    added=added,
                    purged=purged,
                    recovered=True,
                    dry_run=False,
                    deliver_notifications=deliver_notifications,
                    checked_at=sampled_at,
                    retention_cutoff=retention_cutoff,
                    mailbox_identity_key=mailbox_identity_key,
                    recovery_status=pending_recovery,
                )
            else:
                changes = self.gateway.recover_since(self.config.allowlist, since)

        dry_run_messages: list[PendingMessage] = []
        added = self._capture_ids(
            changes.message_ids,
            mailbox_identity_key=mailbox_identity_key,
            label_selectors=label_selectors,
            checked_at=checked_at,
            retention_cutoff=retention_cutoff,
            folders=folders,
            known_locations=changes.locations,
            dry_run=dry_run,
            dry_run_messages=dry_run_messages,
        )

        if not dry_run:
            self.store.set_state(
                changes.cursor,
                provider=self.mailbox.provider,
                account_id=self.mailbox.account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
        added += self._poll_sent_folder(
            mailbox_identity_key=mailbox_identity_key,
            label_selectors=label_selectors,
            checked_at=checked_at,
            retention_cutoff=retention_cutoff,
            folders=folders,
            sent_scope=sent_scope,
            dry_run=dry_run,
            dry_run_messages=dry_run_messages,
        )
        return self._finish_active_result(
            added=added,
            purged=purged,
            recovered=recovered,
            dry_run=dry_run,
            deliver_notifications=deliver_notifications,
            checked_at=checked_at,
            retention_cutoff=retention_cutoff,
            mailbox_identity_key=mailbox_identity_key,
            dry_run_messages=dry_run_messages,
            recovery_incomplete=recovery_incomplete,
            recovery_reason=recovery_reason,
        )

    @staticmethod
    def _gated_class_allowed() -> bool:
        """Contract D-ops: Sent polling (and later gated capture) need the paid entitlement."""
        return connect_entitlement_decision() is EntitlementDecision.ACTIVE

    def _capture_ids(
        self,
        message_ids: Iterable[str],
        *,
        mailbox_identity_key: str,
        label_selectors: tuple[GmailLabelSelectorLike, ...],
        checked_at: datetime,
        retention_cutoff: datetime,
        folders: frozenset[str],
        known_locations: Mapping[str, FolderObservation],
        dry_run: bool,
        dry_run_messages: list[PendingMessage],
    ) -> int:
        """Admit and capture one folder's changed messages; return how many were added.

        An id that arrives with a folder observation but outside the batch (a Gmail
        continuation replays its prefix) is handled like any other: known or new.
        """
        added = 0
        batch = set(message_ids)
        candidates = (*message_ids, *(i for i in known_locations if i not in batch))
        for provider_message_id in candidates:
            if self.store.has_seen_message(
                provider_message_id,
                provider=self.mailbox.provider,
                account_id=self.mailbox.account_id,
                mailbox_identity_key=mailbox_identity_key,
            ):
                # Gmail and Microsoft ids span folders, so a known id comes back
                # when its labels or folder change; IMAP copies have folder-tokened ids.
                hint = known_locations.get(provider_message_id)
                if self.mailbox.provider != IMAP_PROVIDER and not dry_run and hint is not None:
                    self._record_known_message_location(
                        provider_message_id,
                        mailbox_identity_key=mailbox_identity_key,
                        checked_at=checked_at,
                        folders=folders,
                        observed=hint,
                    )
                continue
            hint = known_locations.get(provider_message_id)
            if hint is not None and hint.complete and not (hint.locations & folders):
                # The record says the message is in no folder in scope: nothing is
                # fetched from the gated scope (contract D-ops); the cursor advances.
                continue
            try:
                metadata = self.gateway.metadata(provider_message_id)
            except MailboxMessageUnavailable as exc:
                logger.info(
                    "Skipping message %s (gone before fetch): %s",
                    provider_message_id,
                    exc,
                )
                continue
            except MailboxMessageInvalid as exc:
                logger.warning(
                    "Skipping message %s with unsafe metadata: %s",
                    provider_message_id,
                    exc,
                )
                continue
            if metadata.message_id != provider_message_id:
                raise RuntimeError("Mailbox metadata identity did not match the change record")
            admission = match_mailbox_admission(
                metadata=metadata,
                provider=self.mailbox.provider,
                account_id=self.mailbox.account_id,
                mailbox_identity_key=mailbox_identity_key,
                exact_senders=self.admission_sender_names,
                label_selectors=label_selectors,
                admitted_at=checked_at,
                folders=folders,
            )
            if admission is None:
                continue
            received_at = _copy_received_in_retention(
                metadata, checked_at=checked_at, retention_cutoff=retention_cutoff
            )
            if received_at is None:
                logger.info(
                    "Skipping message %s outside the configured retention window",
                    provider_message_id,
                )
                continue
            values = {
                "message_id": scoped_message_id(
                    self.mailbox.provider,
                    self.mailbox.account_id,
                    metadata.message_id,
                    mailbox_identity_key,
                ),
                "provider": self.mailbox.provider,
                "account_id": self.mailbox.account_id,
                "provider_message_id": metadata.message_id,
                "thread_id": metadata.thread_id,
                "sender": metadata.sender,
                "sender_name": metadata.sender_name or self.sender_names.get(metadata.sender),
                "subject": metadata.subject,
                "received_at": received_at.isoformat(),
                "mailbox_identity_key": mailbox_identity_key,
            }
            if dry_run:
                if self._preview_candidate(
                    metadata, values, mailbox_identity_key, dry_run_messages
                ):
                    added += 1
            elif self.store.add_message(
                **values,
                admission=admission.provenance(),
                rfc_message_id=metadata.rfc_message_id,
                reply_ids=metadata.reply_ids,
                to=metadata.to,
                cc=metadata.cc,
                locations=_admitted_locations(metadata, metadata.labels, folders),
                capture_timezone=self.config.timezone,
                scope_complete=_scope_complete(folders),
            ):
                added += 1
        return added

    def _preview_candidate(
        self,
        metadata: MessageMetadata,
        values: dict[str, object],
        mailbox_identity_key: str,
        dry_run_messages: list[PendingMessage],
    ) -> bool:
        """Append an admitted candidate to a dry run as capture would store it.

        A second copy of a stored or already previewed logical identity is a
        location, not a message (contract D-identity), in a preview too; the one
        rule serves polling and recovery previews alike.
        """
        if metadata.rfc_message_id is not None:
            identity = (
                self.mailbox.provider,
                self.mailbox.account_id,
                mailbox_identity_key,
                metadata.rfc_message_id,
            )
            if identity in self._preview_identities or (
                self.store.logical_message_id(*identity, other_than=metadata.message_id)
                is not None
            ):
                return False
            self._preview_identities.add(identity)
        dry_run_messages.append(
            PendingMessage(
                **values,
                attempts=0,
                fallback_notified_at=None,
                analysis_request_id=None,
                analysis_context_at=None,
                analysis_body_char_limit=None,
            )
        )
        return True

    def _record_known_message_location(
        self,
        provider_message_id: str,
        *,
        mailbox_identity_key: str,
        checked_at: datetime,
        folders: frozenset[str],
        observed: FolderObservation,
    ) -> None:
        """A known id came back through polling with its folders (contract D-identity).

        The change record says which folders the message is in, so nothing is fetched
        (plan step 6). A record that is not a whole folder set and names no admitted
        folder in scope (a star, a read) says nothing about folders and changes
        nothing. Otherwise the folders in scope are recorded; the observation is
        complete, and stamps the source's rows, only when the record carried the
        whole set and every admitted folder was in scope (plan step 5), and clears
        their stamp otherwise, so the message is observed again by discovery.
        """
        if not observed.complete and not (observed.locations & folders):
            return
        self.store.record_message_location(
            provider=self.mailbox.provider,
            account_id=self.mailbox.account_id,
            mailbox_identity_key=mailbox_identity_key,
            provider_message_id=provider_message_id,
            locations=observed.locations & folders,
            scope_complete=_scope_complete(folders) and observed.complete,
            headers_observed=False,
            now=checked_at,
        )

    def _poll_sent_folder(
        self,
        *,
        mailbox_identity_key: str,
        label_selectors: tuple[GmailLabelSelectorLike, ...],
        checked_at: datetime,
        retention_cutoff: datetime,
        folders: frozenset[str],
        sent_scope: str | None,
        dry_run: bool,
        dry_run_messages: list[PendingMessage],
    ) -> int:
        """Poll the Sent folder this check's _settle_sent found available (D-scope).

        Gmail's one mailbox-wide cursor already carries SENT events, so only the other
        providers keep a Sent cursor of their own. A dry run previews Sent as it
        previews the Inbox: it reads, and moves no cursor.
        """
        if sent_scope != SENT_SCOPE_AVAILABLE or self.mailbox.provider == "gmail":
            return 0
        provider = self.mailbox.provider
        account_id = self.mailbox.account_id
        folder_scope = {
            "provider": provider,
            "account_id": account_id,
            "mailbox_identity_key": mailbox_identity_key,
            "folder": SENT_LOCATION,
        }
        state = self.store.folder_state(**folder_scope)
        if state is None:
            # No position yet: a dry run records none, and _settle_sent retries one
            # it could not read on the next check.
            return 0
        # A Sent folder error never stops the Inbox check; the next check retries.
        try:
            try:
                changes = self.gateway.sent_changes_since(state[0])
            except StaleMailboxCursor as exc:
                # Like the Inbox: recover the interval since the last success first.
                logger.warning("Sent folder cursor expired (%s); recovering the gap", exc)
                if not dry_run:
                    self.store.clear_location_stamps(provider, account_id, mailbox_identity_key)
                changes = self.gateway.sent_recover_since(
                    _recovery_since(state[1], retention_cutoff)
                )
            added = self._capture_ids(
                changes.message_ids,
                mailbox_identity_key=mailbox_identity_key,
                label_selectors=label_selectors,
                checked_at=checked_at,
                retention_cutoff=retention_cutoff,
                folders=folders,
                known_locations=changes.locations,
                dry_run=dry_run,
                dry_run_messages=dry_run_messages,
            )
            if not dry_run:
                self.store.set_folder_state(changes.cursor, at=checked_at, **folder_scope)
            return added
        except MailboxError as exc:
            logger.warning("Sent folder poll failed; the Inbox check is unaffected: %s", exc)
            return 0

    def _label(self, message: PendingMessage | AnalyzedMessage | NotificationIntent) -> str:
        return self.sender_names.get(message.sender) or message.sender_name or message.sender

    @staticmethod
    def _stored_analysis(message: AnalyzedMessage) -> Analysis:
        return Analysis(
            category=message.category,
            priority=message.priority,
            summary=message.summary,
            action_required=bool(message.action_required),
            suggested_action=message.suggested_action,
            deadline_text=message.deadline_text,
            deadline_iso=message.deadline_iso,
            confidence=message.confidence,
        )

    def _send_fallback(
        self, message: PendingMessage | AnalyzedMessage | NotificationIntent, dry_run: bool
    ) -> int:
        if not self.config.notifications_enabled or getattr(message, "fallback_notified_at", None):
            return 0
        try:
            send_fallback(
                self._label(message),
                message.subject,
                ntfy_topic=self.config.ntfy_topic,
                ntfy_url=self.config.ntfy_url,
                ntfy_content_disclosure_acknowledged=(
                    self.config.ntfy_content_disclosure_acknowledged
                ),
                dry_run=dry_run,
            )
            if not dry_run:
                self.store.mark_fallback_notified(message.message_id)
            return 1
        except NotificationError as exc:
            logger.warning("Fallback notification unavailable: %s", exc)
            return 0

    def _deliver_automation_review(self, intent: NotificationIntent, dry_run: bool) -> int:
        return int(
            _deliver_automation_review_intent(
                self.config,
                self.store,
                intent,
                self.sender_names,
                dry_run=dry_run,
            )
        )

    def _deliver_analysis(
        self,
        message: PendingMessage | AnalyzedMessage,
        analysis: Analysis,
        dry_run: bool,
        attempts: int | None = None,
        *,
        partial_summary: bool,
    ) -> int:
        if not self.config.notifications_enabled:
            if not dry_run:
                self.store.mark_delivery_complete(message.message_id, notified=False)
            return 0
        try:
            send_analysis(
                self._label(message),
                message.subject,
                analysis,
                ntfy_topic=self.config.ntfy_topic,
                ntfy_url=self.config.ntfy_url,
                ntfy_content_disclosure_acknowledged=(
                    self.config.ntfy_content_disclosure_acknowledged
                ),
                partial_summary=partial_summary,
                dry_run=dry_run,
            )
        except NotificationError as exc:
            logger.warning("Message %s notification unavailable: %s", message.message_id, exc)
            fallback = self._send_fallback(message, dry_run)
            if not dry_run:
                self.store.record_failure(
                    message.message_id,
                    str(exc),
                    message.attempts if attempts is None else attempts,
                )
            return fallback
        if not dry_run:
            self.store.mark_delivery_complete(message.message_id, notified=True)
        return 0

    def _process_pending(
        self,
        *,
        dry_run: bool,
        deliver_notifications: bool,
        extra: list[PendingMessage] | None = None,
        retention_cutoff: datetime,
        retention_observed_at: datetime,
        mailbox_identity_key: str,
    ) -> tuple[int, int]:
        summarized = 0
        fallback = 0
        if deliver_notifications:
            for intent in self.store.notification_intents(kind="fallback"):
                fallback += self._send_fallback(intent, dry_run)
            for intent in self.store.notification_intents(kind="automation_review"):
                self._deliver_automation_review(intent, dry_run)
        delivery = self.store.pending_delivery()
        retained = self.store.retained_logical_messages(
            (message.message_id for message in delivery),
            cutoff=retention_cutoff,
            now=retention_observed_at,
        )
        for message in delivery:
            if message.message_id not in retained:
                continue
            if deliver_notifications:
                fallback += self._deliver_analysis(
                    message,
                    self._stored_analysis(message),
                    dry_run,
                    partial_summary=body_was_truncated(
                        message.analysis_body_chars, message.analysis_body_source_chars
                    )
                    is True,
                )
            elif not self.config.notifications_enabled and not dry_run:
                self.store.mark_delivery_complete(message.message_id, notified=False)
        stored = self.store.pending(
            provider=self.mailbox.provider,
            account_id=self.mailbox.account_id,
        )
        retained = self.store.retained_logical_messages(
            (message.message_id for message in stored),
            cutoff=retention_cutoff,
            now=retention_observed_at,
        )
        # The store judges stored messages by the purge's rule; a preview (extra)
        # is not stored, and this check admitted it under the same cutoff.
        for message in [
            *(message for message in stored if message.message_id in retained),
            *(extra or []),
        ]:
            try:
                if (
                    message.mailbox_identity_key is None
                    or message.mailbox_identity_key != mailbox_identity_key
                ):
                    if not dry_run:
                        self.store.record_analysis_failure(
                            message.message_id,
                            "Message mailbox identity could not be verified",
                            message.attempts,
                            retryable=False,
                            error_code="mailbox_identity_unverified",
                        )
                    continue
                if dry_run:
                    request_id = None
                    body_char_limit = self.config.body_char_limit
                    current_local_time = datetime.now(self.config.zone)
                else:
                    # Pin the local offset with the request so retries reuse identical content.
                    request = self.store.reserve_analysis_request(
                        message.message_id,
                        self.config.body_char_limit,
                        datetime.now(self.config.zone),
                    )
                    request_id = request.request_id
                    body_char_limit = request.body_char_limit
                    current_local_time = datetime.fromisoformat(request.context_at)
                # A stored message is read through any of its copies; a dry-run
                # message is not stored and has only the id polling saw.
                source_ids = (
                    (message.provider_message_id,)
                    if dry_run
                    else tuple(
                        s.provider_message_id
                        for s in self.store.message_sources(message.message_id)
                    )
                )
                served_by, content = _content_from_sources(
                    self.gateway, source_ids, body_char_limit
                )
                if not dry_run:
                    self.store.replace_attachments(
                        message.message_id,
                        content.attachments,
                        source_provider_message_id=served_by,
                    )
                analysis = self.model.analyze(
                    sender=message.sender,
                    subject=message.subject,
                    received_at=message.received_at,
                    body=content.body,
                    attachment_names=content.attachment_names,
                    current_local_time=current_local_time,
                    request_id=request_id,
                )
                if not dry_run:
                    scheduling_principal_key = (
                        _scheduling_automation_principal(self.config, self.store, message)
                        if analysis.category == "scheduling"
                        else None
                    )
                    self.store.mark_analyzed(
                        message.message_id,
                        analysis.model_dump(),
                        mailbox_identity_key=mailbox_identity_key,
                        scheduling_automation_principal_key=scheduling_principal_key,
                        body_chars=len(content.body),
                        body_source_chars=content.body_source_chars,
                    )
                    assert request_id is not None
                    _acknowledge_gateway_result(self.model, request_id, "persisted")
                summarized += 1
                if deliver_notifications:
                    fallback += self._deliver_analysis(
                        message,
                        analysis,
                        dry_run,
                        attempts=0,
                        partial_summary=body_was_truncated(
                            len(content.body), content.body_source_chars
                        )
                        is True,
                    )
                elif not self.config.notifications_enabled and not dry_run:
                    self.store.mark_delivery_complete(message.message_id, notified=False)
            except MailboxMessageUnavailable as exc:
                logger.info(
                    "Skipping pending message %s (gone before fetch): %s",
                    message.message_id,
                    exc,
                )
                if not dry_run:
                    self.store.mark_skipped(message.message_id)
                continue
            except MailboxMessageInvalid as exc:
                logger.warning("Message %s cannot be processed: %s", message.message_id, exc)
                if deliver_notifications:
                    fallback += self._send_fallback(message, dry_run)
                if not dry_run:
                    self.store.record_analysis_failure(
                        message.message_id,
                        str(exc),
                        message.attempts,
                        retryable=False,
                        error_code=exc.code,
                    )
            except GatewayOutputRejected as exc:
                logger.warning("Message %s summary was rejected: %s", message.message_id, exc)
                if deliver_notifications:
                    fallback += self._send_fallback(message, dry_run)
                if not dry_run:
                    self.store.record_analysis_failure(
                        message.message_id,
                        str(exc),
                        message.attempts,
                        retryable=False,
                        error_code=exc.code,
                    )
                    _acknowledge_gateway_result(
                        self.model,
                        exc.request_id,
                        "application_rejected",
                    )
            except GatewayModelError as exc:
                logger.warning("Message %s summary unavailable: %s", message.message_id, exc)
                if deliver_notifications:
                    fallback += self._send_fallback(message, dry_run)
                if not dry_run:
                    self.store.record_analysis_failure(
                        message.message_id,
                        str(exc),
                        message.attempts,
                        retryable=exc.retryable,
                        error_code=exc.code,
                        retry_after_seconds=exc.retry_after_seconds,
                    )
            except ModelError as exc:
                logger.warning("Message %s summary unavailable: %s", message.message_id, exc)
                if deliver_notifications:
                    fallback += self._send_fallback(message, dry_run)
                if not dry_run:
                    self.store.record_analysis_failure(
                        message.message_id,
                        str(exc),
                        message.attempts,
                        retryable=True,
                    )
        return summarized, fallback


def _gmail_label_watch_configured(store: Store) -> bool:
    account = store.active_mail_account()
    if account is None or account.provider != "gmail":
        return False
    if store.gmail_recovery_state(account.account_id) is not None:
        return True
    if account.mailbox_identity_key is None:
        return False
    reader = getattr(store, "gmail_current_label_selectors", None)
    if not callable(reader):
        return False
    try:
        return bool(reader(account.account_id, account.mailbox_identity_key))
    except MailboxIdentityChanged:
        return False


def _current_mailbox_pending_work(store: Store) -> bool:
    account = store.active_mail_account()
    if account is None or account.mailbox_identity_key is None:
        return False
    return store.has_current_pending_mailbox_work(
        account.provider,
        account.account_id,
        account.mailbox_identity_key,
    )


def _gmail_recovery_backoff_admission(store: Store) -> GmailRecoveryState | None:
    account = store.active_mail_account()
    if (
        account is None
        or account.provider != "gmail"
        or account.mailbox_identity_key is None
    ):
        return None
    recovery = store.gmail_recovery_state(account.account_id)
    if (
        recovery is None
        or recovery.mailbox_identity_key != account.mailbox_identity_key
        or Watcher._retry_due(recovery.next_retry_at, datetime.now(UTC))
    ):
        return None
    return recovery


def run_watcher_check(
    config: Config,
    store: Store,
    model: ModelRuntime,
    *,
    dry_run: bool = False,
    deliver_notifications: bool = True,
) -> dict[str, int | bool | str]:
    recovery_backoff = _gmail_recovery_backoff_admission(store)
    if recovery_backoff is not None:
        return {
            **Watcher.recovery_backoff_result(recovery_backoff),
            "automation_processed": 0,
            "automation_review_required": 0,
        }
    watch_configured = (
        bool(config.allowlist)
        or _gmail_label_watch_configured(store)
        or _current_mailbox_pending_work(store)
    )
    if dry_run:
        if not watch_configured:
            result = Watcher.inactive_result(config, store, dry_run=True)
        else:
            mailbox = load_configured_mailbox(config, store)
            result = Watcher(config, store, mailbox, model).check(
                dry_run=True,
                deliver_notifications=deliver_notifications,
            )
        return {
            **result,
            "automation_processed": 0,
            "automation_review_required": 0,
        }

    writes = process_scheduling_writes(config, store)
    before = process_scheduling_automations(config, store, model)
    before_proposals = process_scheduling_proposals(config, store)
    if watch_configured:
        mailbox = load_configured_mailbox(config, store)
        result = Watcher(config, store, mailbox, model).check(
            dry_run=False,
            deliver_notifications=deliver_notifications,
        )
    else:
        result = Watcher.inactive_result(config, store, dry_run=False)
    after = process_scheduling_automations(
        config,
        store,
        model,
        exclude_run_ids=before.attempted_run_ids,
    )
    after_proposals = process_scheduling_proposals(
        config,
        store,
        exclude_run_ids=before_proposals.attempted_run_ids,
        limit=25,
    )
    if deliver_notifications and config.notifications_enabled:
        sender_names = {sender.email: sender.name for sender in config.senders}
        for intent in store.notification_intents(kind="automation_review"):
            _deliver_automation_review_intent(
                config,
                store,
                intent,
                sender_names,
                dry_run=False,
            )
    return {
        **result,
        "purged": int(result["purged"]) + before.purged + after.purged,
        "automation_processed": (
            before.processed
            + writes.processed
            + before_proposals.processed
            + after.processed
            + after_proposals.processed
        ),
        "automation_review_required": (
            before.review_required
            + writes.review_required
            + before_proposals.review_required
            + after.review_required
            + after_proposals.review_required
        ),
    }
