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

from .actions import ACTION_KINDS, CONNECT_INVOKE
from .definition import (
    MAX_NAME_LENGTH,
    Condition,
    Workflow,
    WorkflowDefinition,
    render_connect_invoke_request,
)
from .host import AutomateHost
from .store import (
    OVERLAY_SET,
    OperationConflict,
    RecordView,
    StaleRecord,
    WorkflowStore,
    request_fingerprint,
)


class EngineError(RuntimeError):
    """Base class for workflow engine errors."""


class WorkflowMismatch(EngineError):
    """Raised when a record does not belong to the named workflow."""


class PackOwnershipError(EngineError):
    """Raised when a decision is submitted against a record owned by a different pack.

    The record's ``pack_id`` is the signed-pack boundary: a workflow name can collide across
    two independently signed packs, so name equality alone would let one pack's runtime drive
    another pack's records. This refuses that.
    """


class PackVersionError(EngineError):
    """Raised when a decision is submitted against a record frozen to a different pack version.

    A record is frozen to the pack version it was created under, so in-flight work is always
    driven by the version that started it. An upgraded pack applies only to new records; a
    runtime of a different version is refused rather than allowed to finish work mid-flight
    under changed semantics.
    """


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

    def create_record(
        self,
        workflow: Workflow,
        *,
        now: datetime,
        pack_id: str | None = None,
        pack_version: int | None = None,
    ) -> RecordView:
        """Create a record for a workflow at its initial stage, gated by the license.

        ``pack_id`` and ``pack_version`` bind the record to the signed pack and version that
        created it, so a later decision can be refused unless it comes from that same pack and
        version. Pass both or neither; ``None``/``None`` leaves it unbound (a bare-workflow
        record, as in the pre-pack slices).
        """
        self._host.require_license()
        return self._store.create_record(
            workflow.name,
            workflow.initial_stage,
            now=now,
            allowed_stages=frozenset(workflow.stages),
            pack_id=pack_id,
            pack_version=pack_version,
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
        expected_pack_id: str | None = None,
        expected_pack_version: int | None = None,
    ) -> DecisionOutcome:
        """Apply the definition matching ``decision`` to ``record_id``.

        Returns ``matched=False`` with no mutation when no definition handles the decision
        at the record's current stage. Raises :class:`AmbiguousDecision` when more than one
        matches.

        ``expected_pack_id``, when given, is the signed pack the caller is running: the
        record's own ``pack_id`` must equal it or the decision is refused with
        :class:`PackOwnershipError`, so a pack cannot drive a record another signed pack
        created merely because the two workflows share a name. ``None`` skips the check (a
        bare-workflow caller). The check precedes the replay lookup, so even a replay is
        refused for a foreign pack.

        ``expected_pack_version`` likewise freezes the record to the pack version it was
        created under: a mismatch is refused with :class:`PackVersionError`, so an upgraded
        pack never drives work that started under an earlier version. Both checks are
        write-once reads, so they are race-free, and both precede the replay lookup.

        Operation-key replay takes precedence over condition re-evaluation: an operator
        retrying the same decision after the record has advanced replays the recorded
        outcome (``applied=False``) rather than reporting a fresh no-match because the stage
        moved on, and the same key with a changed request is a conflict. A replayed outcome
        carries ``definition_name=None`` because the identity of a completed operation is
        its recorded event, not a re-match against the current definition set.
        """
        self._host.require_license()
        # Validate untrusted inputs before any lookup: a non-string operation_key would
        # otherwise reach lookup_operation and be matched by SQLite's TEXT affinity (int 7
        # against a stored "7"), bypassing the store's identity guard on the replay path.
        if not isinstance(operation_key, str) or not operation_key:
            raise ValueError("operation_key must be a non-empty string")
        if not isinstance(decision, str) or not decision:
            raise ValueError("decision must be a non-empty string")
        if len(decision) > MAX_NAME_LENGTH:
            # A decision longer than the trigger-name limit can never match a trigger, and
            # would otherwise be persisted verbatim as an unbounded operation name.
            raise ValueError(f"decision must be at most {MAX_NAME_LENGTH} characters")
        if not isinstance(request, Mapping):
            raise ValueError("request must be a mapping")
        record = self._store.get_record(record_id)
        if record.workflow != workflow.name:
            raise WorkflowMismatch(
                f"record {record_id!r} belongs to workflow {record.workflow!r}, "
                f"not {workflow.name!r}"
            )
        # Enforce the signed-pack boundary before anything else touches the operation log:
        # pack_id is write-once, so this read-then-check is race-free, and placing it before
        # the replay lookup means a foreign pack cannot even replay a recorded outcome.
        if expected_pack_id is not None and record.pack_id != expected_pack_id:
            raise PackOwnershipError(
                f"record {record_id!r} is owned by pack {record.pack_id!r}, "
                f"not {expected_pack_id!r}"
            )
        if expected_pack_version is not None and record.pack_version != expected_pack_version:
            raise PackVersionError(
                f"record {record_id!r} is frozen to pack version {record.pack_version!r}, "
                f"not {expected_pack_version!r}"
            )
        # Replay takes precedence over current-stage re-evaluation: a completed operation
        # must replay its recorded outcome even if a later workflow revision has advanced
        # the record into a stage this supplied workflow does not declare.
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
        # For a new operation, the supplied workflow must actually describe the record: a
        # revision reusing the name whose stage set omits the record's current stage cannot
        # legitimately transition it. Checked after the replay lookup so a completed
        # operation still replays regardless of the current stage.
        if record.stage not in set(workflow.stages):
            raise WorkflowMismatch(
                f"record {record_id!r} is at stage {record.stage!r}, which workflow "
                f"{workflow.name!r} does not declare"
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
            # Reserve the key so a retried no-match stays a no-match across stage changes,
            # compare-and-set on the same version the decision was matched against. If an
            # identical submission raced ahead and matched (or already reserved the same
            # no-match), reserve_no_match returns that recorded outcome to replay instead.
            replayed = self._store.reserve_no_match(
                record_id,
                operation_key,
                operation_name=decision,
                request=request,
                expected_version=expected_version,
                now=now,
            )
            if replayed is not None:
                return DecisionOutcome(
                    record=replayed.record,
                    matched=replayed.matched,
                    applied=False,
                    definition_name=None,
                    event_id=replayed.event_id,
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
        # The matched definition's declared actions are admitted to the outbox in the same
        # transaction as the ledger commit, so a decision's side-effect intents are durable
        # the moment the decision applies (the runtime then dispatches the pending rows).
        actions = self._render_actions(record_id, definition)
        outcome = self._store.apply_effects(
            record_id,
            effects,
            operation_key=operation_key,
            operation_name=decision,
            request=request,
            expected_version=expected_version,
            now=now,
            allowed_stages=frozenset(workflow.stages),
            actions=actions,
            allowed_action_kinds=ACTION_KINDS,
        )
        return DecisionOutcome(
            record=outcome.record,
            matched=True,
            applied=outcome.applied,
            definition_name=definition.name,
            event_id=outcome.event_id,
        )

    def _render_actions(
        self, record_id: str, definition: WorkflowDefinition
    ) -> list[dict[str, object]]:
        """Build the decision's action intents, resolving connect.invoke overlay bindings.

        A connect.invoke request may bind a parameter value to an overlay key; resolve those
        against the record's overlay projection merged with this decision's own overlay.set
        effects, so a value set and used in the same decision binds. The resolved request is
        what apply_effects freezes into the outbox row, so a retry replays the resolved values
        rather than re-resolving against later state (the queue-row-binding discipline). The
        merge is a read outside the ledger transaction, but apply_effects compare-and-sets on
        the same version this decision matched, so a concurrent overlay write is rejected there
        rather than admitting a request rendered against a stale projection.
        """
        needs_overlays = any(emit.action == CONNECT_INVOKE for emit in definition.actions)
        overlays: dict[str, object] = {}
        if needs_overlays:
            overlays = self._store.get_overlays(record_id)
            for effect in definition.effects:
                if effect.kind == OVERLAY_SET:
                    overlays[effect.key] = effect.value
        actions: list[dict[str, object]] = []
        for emit in definition.actions:
            request: object = emit.request
            if emit.action == CONNECT_INVOKE:
                request = render_connect_invoke_request(emit.request, overlays)
            actions.append({"kind": emit.action, "request": request})
        return actions
