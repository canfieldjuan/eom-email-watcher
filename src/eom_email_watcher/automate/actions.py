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

The action lane is the durable outbox in :mod:`eom_email_watcher.automate.store`: an action
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

# The closed set of abstract action kinds. A workflow references only these; the registry
# resolves each to a configured adapter.
ACTION_KINDS: frozenset[str] = frozenset(
    {NOTIFY_LOCAL, NOTIFY, NOTIFY_NTFY, MAIL_SEND, CALENDAR_WRITE}
)


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


class LocalNotifyAdapter:
    """The host-guaranteed, zero-config ``notify.local`` adapter.

    A local notification is a durable record the operator reads, so this adapter has no
    external dependency: it validates the notification, and the outbox row it settles is the
    delivered notification (the title and body live on the durable request, and the request
    and result are surfaced together via ``WorkflowStore.list_actions``).
    """

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
            result = adapter.deliver(admission.view.request)
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
        if view.status != ACTION_PENDING:
            return _outcome(view, delivered=False)
        try:
            adapter = self._registry.resolve(view.kind)
        except AdapterNotConfigured:
            return _outcome(view, delivered=False)
        try:
            result = adapter.deliver(view.request)
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


def _outcome(view: ActionView, *, delivered: bool) -> ActionOutcome:
    return ActionOutcome(
        action_id=view.action_id,
        kind=view.kind,
        status=view.status,
        result=view.result,
        delivered=delivered,
    )
