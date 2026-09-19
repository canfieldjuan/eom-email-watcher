"""Provider-agnostic actions: the abstract action-kind vocabulary, the adapter registry,
and the license-gated action lane over the durable outbox.

A workflow names an abstract action *kind* (``notify.local``, ``notify``, ``notify.ntfy``,
``mail.send``, ``calendar.write``), never a concrete integration. The host resolves each
kind to whichever :class:`Adapter` the operator has configured, through an
:class:`AdapterRegistry`, so an integration is swappable without touching the workflow. No
pack, workflow, or adapter here names a vendor.

``notify.local`` is the one host-guaranteed, zero-config kind: its adapter is built in and
records the notification durably in the action outbox, so a workflow can run with no
external accounts configured. The bundled Gmail, notifier, and Microsoft calendar clients
are wrapped as adapters behind ``mail.send`` / ``notify`` / ``calendar.write`` at the host
composition layer (a later slice), not inside this generic package.

The action lane is the durable outbox in :mod:`connect_automate.automate.store`: an action
is admitted (deduplicated) before its side effect runs, then settled with the adapter's
result. Accept-then-crash reconciliation of a stuck-pending action, per-kind queue-admission
policy, and retry are deferred hardening; this slice provides admit, dispatch, and settle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from .host import AutomateHost
from .store import ACTION_PENDING, ActionView, InvalidEffect, WorkflowStore

NOTIFY_LOCAL = "notify.local"
NOTIFY = "notify"
NOTIFY_NTFY = "notify.ntfy"
MAIL_SEND = "mail.send"
CALENDAR_WRITE = "calendar.write"
CONNECT_INVOKE = "connect.invoke"

# The closed set of abstract action kinds. A workflow references only these; the registry
# resolves each to a configured adapter. ``connect.invoke`` is the generic Connect v2
# capability-invocation kind: its adapter hands the frozen request to a configured
# CapabilityInvoker under a stable job identity, so a pack can drive any local Connect
# provider without naming one.
ACTION_KINDS: frozenset[str] = frozenset(
    {NOTIFY_LOCAL, NOTIFY, NOTIFY_NTFY, MAIL_SEND, CALENDAR_WRITE, CONNECT_INVOKE}
)


@dataclass(frozen=True)
class ActionContext:
    """The durable identity of an admitted action, passed to adapters that need it.

    A simple effect adapter (``notify.local``) needs only the request. An adapter that
    invokes an external, idempotent-by-identity operation -- a Connect v2 capability -- needs a
    stable identifier that survives retries and crash recovery, so a re-dispatch re-POSTs the
    *same* job rather than starting a new one (ADR-0002 caller-minted stable job identity,
    idempotent re-POST). ``action_id`` is that stable, durable, per-record identifier.
    """

    action_id: str
    dedupe_key: str
    record_id: str
    kind: str


class AdapterNotConfigured(RuntimeError):
    """Raised when no adapter is registered for an action kind."""

    def __init__(self, kind: str):
        super().__init__(f"no adapter is configured for action kind {kind!r}")
        self.kind = kind


class ActionDeliveryError(RuntimeError):
    """Raised when an adapter fails to deliver an action's side effect."""


@runtime_checkable
class Adapter(Protocol):
    """A configured integration for one abstract action kind.

    ``deliver`` performs the side effect for a single admitted action and returns a JSON
    object describing the outcome (for example a transport identity). It must be effect-only
    on success; idempotency and dedupe are handled by the outbox around it.
    """

    def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]: ...

    # An adapter may declare ``idempotent = True`` to opt a stranded ``pending`` row of its
    # kind into automatic crash recovery (:meth:`ActionRunner.recover_pending`). It means
    # redelivering the request after an ambiguous crash -- one where the side effect may have
    # run before the settle committed -- repeats no external effect, so a duplicate dispatch is
    # harmless. This is a stronger guarantee than the dedupe-by-key idempotency the outbox
    # already provides. An adapter that does not declare it is treated as non-idempotent (the
    # safe default), and the sweep leaves its stranded rows pending for the deferred
    # ambiguous-state reconciliation rather than risk a duplicate external send.
    #
    # An adapter may also define ``deliver_with_context(request, context)`` instead of (or as
    # well as) ``deliver``: the runner calls it when present, passing the :class:`ActionContext`
    # for the admitted row, so the adapter can bind its side effect to the durable action
    # identity (for example to mint a stable job id). Simple adapters implement only
    # ``deliver``; the runner falls back to it when ``deliver_with_context`` is absent.


@runtime_checkable
class CapabilityInvoker(Protocol):
    """Invokes one Connect v2 capability job under a caller-minted stable job id.

    Provider-agnostic: it takes the frozen action request and a stable ``job_id`` and returns
    the terminal result as a JSON object, or raises on failure. The concrete implementation
    (which selects the discovered capability, prepares the input artifact and parameters, and
    drives submit/poll through the v2 client) lives at the host composition layer, not in this
    generic package, so no vendor or transport is named here. Repeated invocation with the
    same ``job_id`` and request must be idempotent (ADR-0002 idempotent re-POST): it re-drives
    the existing job to its recorded terminal outcome rather than starting a second one.
    """

    def invoke(self, request: Mapping[str, object], *, job_id: str) -> Mapping[str, object]: ...


class ConnectInvokeAdapter:
    """The ``connect.invoke`` adapter: drive a Connect v2 capability from the action lane.

    It bridges the abstract ``connect.invoke`` kind to a configured
    :class:`CapabilityInvoker`, binding the invocation to the durable action identity: the
    stable ``job_id`` is the admitted action's ``action_id``, so a retry or crash-recovery
    re-dispatch re-POSTs the *same* job (ADR-0002 idempotent re-POST) rather than starting a
    second invocation. That stable-identity idempotency is exactly what a raw external send
    lacks, so this adapter declares ``idempotent = True`` and is safe for
    :meth:`ActionRunner.recover_pending` to re-drive: the provider replays the recorded
    outcome for a job it already accepted.
    """

    # Safe to auto-recover: re-dispatch re-POSTs the same job_id, which the provider replays
    # idempotently rather than performing the side effect twice.
    idempotent = True

    def __init__(self, invoker: CapabilityInvoker):
        self._invoker = invoker

    def deliver_with_context(
        self, request: Mapping[str, object], context: ActionContext
    ) -> Mapping[str, object]:
        # The stable job identity is the durable action id, not a fresh value per dispatch, so
        # a recovery sweep or retry re-drives the same job instead of duplicating it.
        result = self._invoker.invoke(request, job_id=context.action_id)
        if not isinstance(result, Mapping):
            raise ActionDeliveryError("connect.invoke invoker returned a non-mapping result")
        return result


class LocalNotifyAdapter:
    """The host-guaranteed, zero-config ``notify.local`` adapter.

    A local notification is a durable record the operator reads, so this adapter has no
    external dependency: it validates the notification, and the outbox row it settles is the
    delivered notification (the title and body live on the durable request, and the request
    and result are surfaced together via ``WorkflowStore.list_actions``).
    """

    # A local notification is a durable outbox row, not an external send: redelivering a
    # stranded pending row after a crash only re-settles the same fixed metadata onto the same
    # row (under a status CAS), repeating no side effect, so it is safe to auto-recover.
    idempotent = True

    def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
        title = request.get("title")
        body = request.get("body")
        if not isinstance(title, str) or not title:
            raise ActionDeliveryError("notify.local requires a non-empty 'title'")
        if not isinstance(body, str):
            raise ActionDeliveryError("notify.local requires a string 'body'")
        # Return delivery metadata only, never echoing title/body: the durable request row
        # already carries them, and echoing them would make the result grow with the request,
        # so a valid notification admitted just under the size bound could not settle its
        # (larger) result and would be wrongly marked failed.
        return {"channel": "local", "delivered": True}


class AdapterRegistry:
    """Maps each abstract action kind to a configured adapter."""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    @classmethod
    def with_defaults(cls) -> AdapterRegistry:
        """A registry with the host-guaranteed ``notify.local`` adapter pre-registered."""
        registry = cls()
        registry.register(NOTIFY_LOCAL, LocalNotifyAdapter())
        return registry

    def register(self, kind: str, adapter: Adapter) -> None:
        if kind not in ACTION_KINDS:
            raise ValueError(f"unknown action kind {kind!r}")
        self._adapters[kind] = adapter

    def resolve(self, kind: str) -> Adapter:
        try:
            return self._adapters[kind]
        except KeyError:
            raise AdapterNotConfigured(kind) from None


@dataclass(frozen=True)
class ActionOutcome:
    action_id: str
    kind: str
    status: str
    result: dict[str, object] | None
    # True when this call performed the side effect; False when it replayed a recorded
    # outcome for an already-terminal dedupe key.
    delivered: bool


class ActionRunner:
    """Runs an abstract action through the adapter registry and the durable outbox."""

    def __init__(self, *, store: WorkflowStore, registry: AdapterRegistry, host: AutomateHost):
        self._store = store
        self._registry = registry
        self._host = host

    def run(
        self,
        record_id: str,
        *,
        kind: str,
        dedupe_key: str,
        request: Mapping[str, object],
        now: datetime,
    ) -> ActionOutcome:
        """Admit, dispatch, and settle an action, gated by the license.

        Idempotent by ``dedupe_key``: an already-settled or already-failed action replays its
        recorded outcome without re-dispatching, and without needing the adapter still
        configured. The adapter is resolved only for a newly admitted action; if none is
        configured the pending row is released so its dedupe key is not poisoned.
        """
        self._host.require_license()
        if kind not in ACTION_KINDS:
            raise ValueError(f"unknown action kind {kind!r}")
        admission = self._store.admit_action(
            record_id, kind=kind, dedupe_key=dedupe_key, request=request, now=now
        )
        if not admission.admitted:
            # A terminal (settled or failed) action replays its recorded outcome; a replay
            # must not depend on the current adapter configuration.
            return _outcome(admission.view, delivered=False)
        action_id = admission.view.action_id
        try:
            adapter = self._registry.resolve(kind)
        except AdapterNotConfigured:
            self._store.release_action(action_id)
            raise
        try:
            # Dispatch the committed canonical snapshot, not the caller's mapping: the durable
            # row and the dedupe identity are this snapshot, so the adapter must act on exactly
            # what was recorded. Feeding it the still-mutable caller object could let the side
            # effect diverge from the recorded intent, and a retry would then conflict.
            result = _invoke_adapter(adapter, admission.view.request, _context(admission.view))
        except Exception as exc:
            self._store.fail_action(action_id, error=str(exc), now=now)
            raise ActionDeliveryError(f"adapter for {kind!r} failed: {exc}") from exc
        if not isinstance(result, Mapping):
            self._store.fail_action(
                action_id, error="adapter returned a non-mapping result", now=now
            )
            raise ActionDeliveryError(f"adapter for {kind!r} returned a non-mapping result")
        try:
            settled = self._store.settle_action(action_id, result=result, now=now)
        except InvalidEffect as exc:
            # The side effect ran but its result cannot be persisted (oversized or not
            # canonical JSON); terminalize as failed rather than leave the action pending.
            self._store.fail_action(
                action_id, error=f"adapter result could not be persisted: {exc}", now=now
            )
            raise ActionDeliveryError(
                f"adapter for {kind!r} returned an unpersistable result: {exc}"
            ) from exc
        return _outcome(settled, delivered=True)

    def dispatch(self, view: ActionView, *, now: datetime) -> ActionOutcome:
        """Deliver and settle an already-admitted pending action: the resume path.

        Unlike :meth:`run`, this does not admit -- the intent was durably admitted with the
        decision -- so it only resolves the adapter, delivers the committed request, and
        settles or fails. It never raises for a per-action failure (a batch of a decision's
        actions must not be aborted by one), reporting the outcome instead:

        - a terminal row replays its recorded outcome with no delivery;
        - a pending row whose kind has no configured adapter is left pending (durable intent)
          and reported as pending, so a later dispatch once the adapter is configured resumes
          it -- it is never released;
        - a delivery error, a non-mapping result, or an unpersistable result terminalizes the
          row as ``failed``.
        """
        self._host.require_license()
        # Reload the persisted row and act only on the durable kind/request/status. A caller
        # cannot dispatch a mutated in-memory ActionView and have the adapter run a different
        # side effect than the one the outbox records as settled.
        view = self._store.get_action(view.action_id)
        if view.status != ACTION_PENDING:
            return _outcome(view, delivered=False)
        try:
            adapter = self._registry.resolve(view.kind)
        except AdapterNotConfigured:
            return _outcome(view, delivered=False)
        try:
            result = _invoke_adapter(adapter, view.request, _context(view))
        except Exception as exc:
            failed = self._store.fail_action(view.action_id, error=str(exc), now=now)
            return _outcome(failed, delivered=False)
        if not isinstance(result, Mapping):
            failed = self._store.fail_action(
                view.action_id, error="adapter returned a non-mapping result", now=now
            )
            return _outcome(failed, delivered=False)
        try:
            settled = self._store.settle_action(view.action_id, result=result, now=now)
        except InvalidEffect as exc:
            failed = self._store.fail_action(
                view.action_id, error=f"adapter result could not be persisted: {exc}", now=now
            )
            return _outcome(failed, delivered=False)
        return _outcome(settled, delivered=True)

    def recover_pending(self, *, now: datetime) -> list[ActionOutcome]:
        """Re-drive every safely recoverable ``pending`` action stranded by a crash: the sweep.

        A decision admits its action intents durably in the same transaction as its ledger
        event and dispatches them only afterwards, so a crash between that commit and the
        dispatch leaves ``pending`` rows with no in-flight dispatcher. Run this once at host
        start, before normal operation resumes: it lists the pending rows in admission order
        (:meth:`WorkflowStore.list_pending_actions`) and, for each whose kind is safe to
        auto-recover, dispatches it through :meth:`dispatch`, which reloads the durable row and
        settles it, fails it, or (no adapter configured) leaves it pending for a later sweep.
        It never raises for a per-action failure -- one bad row must not abort recovery of the
        rest -- and returns an outcome per row swept.

        Gated by the license first, like :meth:`run` and :meth:`dispatch`, so an unlicensed
        host refuses even to enumerate recovery work (the empty-feed case included).

        Only an intrinsically idempotent kind is auto-recovered (see :meth:`_auto_recoverable`).
        A crash after ``adapter.deliver`` ran but before ``settle_action`` committed leaves a
        row indistinguishable from one whose delivery never started, so redelivering it is safe
        only when it repeats no external effect (``notify.local``, an adapter declaring
        ``idempotent = True``). A non-idempotent external adapter's stranded row is left pending
        here, untouched, for the deferred ambiguous-state reconciliation (a provider-side lookup
        or claim identity); this sweep is the recovery primitive, not that provider-idempotency
        guarantee. Safe to repeat: :meth:`dispatch` only delivers a row still ``pending`` and
        settles under a status CAS, so a row completed by another path is replayed, not
        re-delivered.
        """
        self._host.require_license()
        outcomes: list[ActionOutcome] = []
        for view in self._store.list_pending_actions():
            if not self._auto_recoverable(view.kind):
                # No adapter configured for the kind yet, or a configured but non-idempotent
                # one: leave the row pending (never released) rather than risk a duplicate
                # external send. A later sweep resumes it once an adapter is configured, or the
                # deferred reconciliation settles it with a provider identity.
                outcomes.append(_outcome(view, delivered=False))
                continue
            outcomes.append(self.dispatch(view, now=now))
        return outcomes

    def _auto_recoverable(self, kind: str) -> bool:
        """Whether a stranded ``pending`` row of this kind is safe to auto-redeliver.

        True only for a configured adapter that declares ``idempotent = True``; a kind with no
        configured adapter, and an adapter that does not declare it, are both treated as not
        recoverable (the safe default), so the sweep never auto-repeats a non-idempotent
        external effect.
        """
        try:
            adapter = self._registry.resolve(kind)
        except AdapterNotConfigured:
            return False
        return bool(getattr(adapter, "idempotent", False))


def _outcome(view: ActionView, *, delivered: bool) -> ActionOutcome:
    return ActionOutcome(
        action_id=view.action_id,
        kind=view.kind,
        status=view.status,
        result=view.result,
        delivered=delivered,
    )


def _context(view: ActionView) -> ActionContext:
    return ActionContext(
        action_id=view.action_id,
        dedupe_key=view.dedupe_key,
        record_id=view.record_id,
        kind=view.kind,
    )


def _invoke_adapter(
    adapter: Adapter, request: Mapping[str, object], context: ActionContext
) -> object:
    """Call the adapter, passing the durable action context when it accepts one.

    An adapter that binds its side effect to the action identity (``connect.invoke``) defines
    ``deliver_with_context``; a simple effect adapter defines only ``deliver``. Preferring the
    context form keeps the durable-identity plumbing off the common path.
    """
    deliver_with_context = getattr(adapter, "deliver_with_context", None)
    if deliver_with_context is not None:
        return deliver_with_context(request, context)
    return adapter.deliver(request)
