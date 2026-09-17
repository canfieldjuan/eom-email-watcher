"""The workflow engine: the operator.decision trigger and the effect executor.

The engine is the consumer-side orchestration the Connect contract deliberately leaves to
the host (ADR-0006). It is stateless beyond the store it drives: given a workflow
definition and an operator's decision, it selects the matching definition, then applies its
effect batch through :meth:`WorkflowStore.apply_effects` so the whole batch commits under
one transaction, one ledger event, and one compare-and-set.

Every entry point revalidates the license through ``AutomateHost.require_license`` before
it touches the store, so a host that keeps running past its entitlement's expiry stops
admitting work at the next decision. Decision-role authorization (which principal may make
a given decision) is a later hardening item: this slice gates that the machine is licensed,
not who the caller is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .definition import Condition, Workflow, WorkflowDefinition
from .host import AutomateHost
from .store import OperationConflict, RecordView, StaleRecord, WorkflowStore, request_fingerprint


class EngineError(RuntimeError):
    """Base class for workflow engine errors."""


class WorkflowMismatch(EngineError):
    """Raised when a record does not belong to the named workflow."""


class AmbiguousDecision(EngineError):
    """Raised when more than one definition matches a single decision.

    Effects mutate shared record state, so two definitions firing on one decision is an
    authoring error rather than a fan-out: it is refused instead of applied in an arbitrary
    order.
    """


@dataclass(frozen=True)
class DecisionOutcome:
    record: RecordView
    matched: bool
    applied: bool
    definition_name: str | None
    event_id: str | None


def _condition_matches(condition: Condition, record: RecordView) -> bool:
    if condition.field == "record.stage":
        actual = record.stage
    else:  # pragma: no cover - the closed enum has no other field yet
        return False
    if condition.op == "equals":
        return actual == condition.value
    return actual in condition.value  # "in": value is a list


def _definition_matches(definition: WorkflowDefinition, decision: str, record: RecordView) -> bool:
    if definition.trigger.source_kind != "operator.decision":
        return False
    if definition.trigger.decision != decision:
        return False
    return all(_condition_matches(condition, record) for condition in definition.conditions)


class WorkflowEngine:
    """Runs operator decisions against a workflow definition and the durable store."""

    def __init__(self, *, store: WorkflowStore, host: AutomateHost):
        self._store = store
        self._host = host

    def create_record(self, workflow: Workflow, *, now: datetime) -> RecordView:
        """Create a record for a workflow at its initial stage, gated by the license."""
        self._host.require_license()
        return self._store.create_record(
            workflow.name,
            workflow.initial_stage,
            now=now,
            allowed_stages=frozenset(workflow.stages),
        )

    def submit_decision(
        self,
        workflow: Workflow,
        record_id: str,
        *,
        decision: str,
        operation_key: str,
        request: Mapping[str, object],
        expected_version: int,
        now: datetime,
    ) -> DecisionOutcome:
        """Apply the definition matching ``decision`` to ``record_id``.

        Returns ``matched=False`` with no mutation when no definition handles the decision
        at the record's current stage. Raises :class:`AmbiguousDecision` when more than one
        matches.

        Operation-key replay takes precedence over condition re-evaluation: an operator
        retrying the same decision after the record has advanced replays the recorded
        outcome (``applied=False``) rather than reporting a fresh no-match because the stage
        moved on, and the same key with a changed request is a conflict. A replayed outcome
        carries ``definition_name=None`` because the identity of a completed operation is
        its recorded event, not a re-match against the current definition set.
        """
        self._host.require_license()
        record = self._store.get_record(record_id)
        if record.workflow != workflow.name:
            raise WorkflowMismatch(
                f"record {record_id!r} belongs to workflow {record.workflow!r}, "
                f"not {workflow.name!r}"
            )
        prior = self._store.lookup_operation(record_id, operation_key)
        if prior is not None:
            if prior.operation_name != decision or prior.request_fingerprint != request_fingerprint(
                request
            ):
                raise OperationConflict(operation_key=operation_key)
            return DecisionOutcome(
                record=prior.record,
                matched=prior.matched,
                applied=False,
                definition_name=None,
                event_id=prior.event_id,
            )
        # Bind matching and the compare-and-set to one version: reject a stale caller here,
        # before matching, so a definition selected against this snapshot cannot be applied
        # against a version another writer advanced to in between. A concurrent write after
        # this point is still caught by the store's own compare-and-set.
        if expected_version != record.state_version:
            raise StaleRecord(expected=expected_version, actual=record.state_version)
        matches = [
            definition
            for definition in workflow.definitions
            if _definition_matches(definition, decision, record)
        ]
        if not matches:
            # Reserve the key so a retried no-match stays a no-match across stage changes.
            self._store.reserve_no_match(
                record_id,
                operation_key,
                operation_name=decision,
                request=request,
                now=now,
            )
            return DecisionOutcome(
                record=record,
                matched=False,
                applied=False,
                definition_name=None,
                event_id=None,
            )
        if len(matches) > 1:
            raise AmbiguousDecision(
                f"decision {decision!r} matched {len(matches)} definitions "
                f"at stage {record.stage!r}"
            )
        definition = matches[0]
        effects = [effect.model_dump(mode="json") for effect in definition.effects]
        outcome = self._store.apply_effects(
            record_id,
            effects,
            operation_key=operation_key,
            operation_name=decision,
            request=request,
            expected_version=expected_version,
            now=now,
            allowed_stages=frozenset(workflow.stages),
        )
        return DecisionOutcome(
            record=outcome.record,
            matched=True,
            applied=outcome.applied,
            definition_name=definition.name,
            event_id=outcome.event_id,
        )
