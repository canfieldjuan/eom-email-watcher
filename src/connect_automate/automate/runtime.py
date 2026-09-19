"""The pack runtime: load a signed pack, then run its workflow end to end.

This is the composition that makes a signed workflow pack runnable on a licensed host. It
verifies the pack's publisher signature and the per-PC grant (:mod:`connect_automate.
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
from .definition import Workflow
from .engine import DecisionOutcome, WorkflowEngine
from .host import AutomateHost
from .pack import LoadedPack, load_pack, verify_grant
from .store import ACTION_DEDUPE_PREFIX, RecordView, WorkflowStore


@dataclass(frozen=True)
class DecisionRun:
    """A decision's record outcome plus the action outcomes it dispatched."""

    outcome: DecisionOutcome
    actions: list[ActionOutcome]


# A private construction token. PackRuntime can only be built through load(), which performs
# signature and grant verification; a direct construction that skipped it would defeat the
# whole point of the signed-pack boundary.
_VERIFIED = object()


class PackRuntime:
    """Runs a verified workflow pack against the durable store and the action outbox.

    Construct only through :meth:`load`, which verifies the publisher signature and the per-PC
    grant. The constructor rejects any other caller so a runtime cannot exist for a pack that
    was not verified.
    """

    def __init__(
        self,
        *,
        pack: LoadedPack,
        store: WorkflowStore,
        host: AutomateHost,
        registry: AdapterRegistry,
        _token: object = None,
    ):
        if _token is not _VERIFIED:
            raise TypeError(
                "PackRuntime must be created through PackRuntime.load(); direct construction "
                "would bypass pack-signature and grant verification"
            )
        self._pack = pack
        self._host = host
        self._store = store
        self._engine = WorkflowEngine(store=store, host=host)
        self._runner = ActionRunner(store=store, registry=registry, host=host)

    @property
    def pack(self) -> LoadedPack:
        # A defensive deep copy: the verified workflow the engine executes is private, so a
        # caller cannot mutate an effect or append an action after load() and have
        # submit_decision run unsigned semantics.
        return LoadedPack(
            pack_id=self._pack.pack_id,
            pack_version=self._pack.pack_version,
            workflow=self._pack.workflow.model_copy(deep=True),
        )

    @property
    def workflow(self) -> Workflow:
        # A defensive deep copy, for the same reason as :attr:`pack`.
        return self._pack.workflow.model_copy(deep=True)

    @classmethod
    def load(
        cls,
        pack_bytes: bytes,
        grant_bytes: bytes,
        *,
        store: WorkflowStore,
        host: AutomateHost,
        registry: AdapterRegistry,
        publisher_keys: Mapping[str, bytes],
        grant_keys: Mapping[str, bytes],
        subject: str,
        now: datetime,
    ) -> PackRuntime:
        """Verify a pack and its per-PC grant, then return a runtime for its workflow.

        Gated by the license first: an unlicensed host cannot load a pack at all.
        :func:`load_pack` verifies the publisher signature over the pack bytes against
        ``publisher_keys``; :func:`verify_grant` authorizes this ``subject`` (the licensed
        PC/customer) against the *separate* ``grant_keys``. The two trust stores are kept
        distinct so a pack publisher cannot self-issue a grant and a grant issuer cannot
        sign an executable pack; pass the same mapping for both only when one authority holds
        both roles. A tampered pack, an untrusted key, or a missing/mismatched grant raises
        :class:`PackError` here, before any record is created.
        """
        host.require_license()
        pack = load_pack(pack_bytes, keys=publisher_keys)
        verify_grant(grant_bytes, keys=grant_keys, pack_id=pack.pack_id, subject=subject, now=now)
        return cls(pack=pack, store=store, host=host, registry=registry, _token=_VERIFIED)

    def create_record(self, *, now: datetime) -> RecordView:
        """Create a record for the pack's workflow at its initial stage.

        The record is bound to this pack's identity, so only this pack's runtime can later
        submit decisions against it, even if another signed pack declares a same-named
        workflow.
        """
        return self._engine.create_record(
            self._pack.workflow,
            now=now,
            pack_id=self._pack.pack_id,
            pack_version=self._pack.pack_version,
        )

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

        The engine commits the record-ledger effects (the transition) and admits the matched
        definition's action intents to the outbox in the *same* transaction, so the moment a
        decision applies its declared side effects are durable pending rows. This method then
        dispatches (delivers and settles) those pending rows.

        This is idempotent and crash-recoverable: a replay of the same ``operation_key``
        returns ``applied=False`` and re-admits nothing, and the dispatch here re-drives only
        rows still ``pending`` from an earlier crash mid-dispatch, so each declared action
        runs at most once and is never lost. An action whose kind has no configured adapter
        stays pending and resumable rather than being lost or blocking the others.
        """
        outcome = self._engine.submit_decision(
            self._pack.workflow,
            record_id,
            decision=decision,
            operation_key=operation_key,
            request=request,
            expected_version=expected_version,
            now=now,
            expected_pack_id=self._pack.pack_id,
            expected_pack_version=self._pack.pack_version,
        )
        actions: list[ActionOutcome] = []
        if outcome.matched and outcome.event_id is not None:
            # The decision's action rows were admitted (pending) in the same transaction as
            # its ledger event, keyed by that event id. Dispatch them in admission order;
            # dispatch() delivers a pending row, replays a terminal one, and leaves an
            # unconfigured-adapter row pending for a later resume.
            prefix = f"{ACTION_DEDUPE_PREFIX}{outcome.event_id}:"
            for view in self._store.list_actions(record_id):
                if view.dedupe_key.startswith(prefix):
                    actions.append(self._runner.dispatch(view, now=now))
        return DecisionRun(outcome=outcome, actions=actions)
