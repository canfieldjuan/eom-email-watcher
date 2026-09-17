"""The pack runtime: load a signed pack, then run its workflow end to end.

This is the composition that makes a signed workflow pack runnable on a licensed host. It
verifies the pack's publisher signature and the per-PC grant (:mod:`eom_email_watcher.
automate.pack`), then drives the verified workflow through the record/stage engine and the
action outbox: an operator decision applies the matching definition's record-ledger effects
(the transition) and emits its declared abstract actions (``notify.local`` and friends)
through the adapter registry. No vendor is named; the registry resolves each abstract action
kind to whatever adapter the operator configured, and ``notify.local`` runs with none.

Every entry point is gated by :meth:`AutomateHost.require_license`, and pack loading is gated
twice more: the publisher signature proves the workflow semantics, and the per-PC grant
authorizes this subject to run this pack. Revalidating the grant at every decision (not only
at load) is deferred hardening.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .actions import ActionOutcome, ActionRunner, AdapterRegistry
from .definition import Workflow, WorkflowDefinition
from .engine import DecisionOutcome, WorkflowEngine
from .host import AutomateHost
from .pack import LoadedPack, load_pack, verify_grant
from .store import RecordView, WorkflowStore


@dataclass(frozen=True)
class DecisionRun:
    """A decision's record outcome plus the action outcomes it dispatched."""

    outcome: DecisionOutcome
    actions: list[ActionOutcome]


class PackRuntime:
    """Runs a verified workflow pack against the durable store and the action outbox."""

    def __init__(
        self,
        *,
        pack: LoadedPack,
        store: WorkflowStore,
        host: AutomateHost,
        registry: AdapterRegistry,
    ):
        self._pack = pack
        self._host = host
        self._engine = WorkflowEngine(store=store, host=host)
        self._runner = ActionRunner(store=store, registry=registry, host=host)

    @property
    def pack(self) -> LoadedPack:
        return self._pack

    @property
    def workflow(self) -> Workflow:
        return self._pack.workflow

    @classmethod
    def load(
        cls,
        pack_bytes: bytes,
        grant_bytes: bytes,
        *,
        store: WorkflowStore,
        host: AutomateHost,
        registry: AdapterRegistry,
        keys: Mapping[str, bytes],
        subject: str,
        now: datetime,
    ) -> PackRuntime:
        """Verify a pack and its per-PC grant, then return a runtime for its workflow.

        Gated by the license first: an unlicensed host cannot load a pack at all.
        :func:`load_pack` verifies the publisher signature over the pack bytes;
        :func:`verify_grant` authorizes this ``subject`` (the licensed PC/customer) to run
        this pack. A tampered pack, an untrusted key, or a missing/mismatched grant raises
        :class:`PackError` here, before any record is created.
        """
        host.require_license()
        pack = load_pack(pack_bytes, keys=keys)
        verify_grant(grant_bytes, keys=keys, pack_id=pack.pack_id, subject=subject, now=now)
        return cls(pack=pack, store=store, host=host, registry=registry)

    def create_record(self, *, now: datetime) -> RecordView:
        """Create a record for the pack's workflow at its initial stage."""
        return self._engine.create_record(self._pack.workflow, now=now)

    def submit_decision(
        self,
        record_id: str,
        *,
        decision: str,
        operation_key: str,
        request: Mapping[str, object],
        expected_version: int,
        now: datetime,
    ) -> DecisionRun:
        """Apply a decision and dispatch the matched definition's declared actions.

        The record-ledger effects (the transition) commit through the engine first. Then,
        only on the call that actually applied them (``matched and applied``), the matched
        definition's actions are emitted through the outbox. A replay of the same
        ``operation_key`` returns ``applied=False`` and dispatches nothing, so the side
        effect runs exactly once. Each action's dedupe key is derived from the operation key,
        so the outbox deduplicates as a backstop even if an applied decision is re-run.
        """
        outcome = self._engine.submit_decision(
            self._pack.workflow,
            record_id,
            decision=decision,
            operation_key=operation_key,
            request=request,
            expected_version=expected_version,
            now=now,
        )
        actions: list[ActionOutcome] = []
        if outcome.matched and outcome.applied and outcome.definition_name is not None:
            definition = self._definition(outcome.definition_name)
            for index, emit in enumerate(definition.actions):
                actions.append(
                    self._runner.run(
                        record_id,
                        kind=emit.action,
                        dedupe_key=f"{operation_key}:{index}",
                        request=emit.request,
                        now=now,
                    )
                )
        return DecisionRun(outcome=outcome, actions=actions)

    def _definition(self, name: str) -> WorkflowDefinition:
        for definition in self._pack.workflow.definitions:
            if definition.name == name:
                return definition
        # Unreachable: the engine only returns a definition_name from this workflow.
        raise KeyError(name)
