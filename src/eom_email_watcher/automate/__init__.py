"""Licensed Automate host: a Local Connect consumer that runs signed workflow packs.

The Automate host is the generic runtime described in the build plan. It is a Connect
consumer, so it owns admission, ordering, and retry (ADR-0006), and it runs only while
the local machine holds an active ``connect.automations`` entitlement.

Slice 0 provides the host skeleton and the startup license gate, reusing the existing
entitlement substrate (:mod:`eom_email_watcher.entitlement`). Later slices attach the
durable workflow record, the lifecycle ledger and stage engine, the effect executor and
outbox, the adapter registry, and the signed pack loader. Every one of those is admitted
only after :meth:`AutomateHost.require_license` passes.
"""

from __future__ import annotations

from .actions import (
    ACTION_KINDS,
    ActionDeliveryError,
    ActionOutcome,
    ActionRunner,
    Adapter,
    AdapterNotConfigured,
    AdapterRegistry,
    LocalNotifyAdapter,
)
from .definition import (
    DefinitionError,
    Workflow,
    WorkflowDefinition,
    canonical_workflow,
    parse_workflow,
)
from .engine import (
    AmbiguousDecision,
    DecisionOutcome,
    EngineError,
    WorkflowEngine,
    WorkflowMismatch,
)
from .host import REQUIRED_FEATURES, AutomateHost, AutomateLicenseError
from .store import (
    ActionAdmission,
    ActionConflict,
    ActionView,
    InvalidEffect,
    OperationConflict,
    OperationReplay,
    RecordView,
    StaleRecord,
    TransitionOutcome,
    UnknownRecord,
    UnresolvedAction,
    WorkflowStore,
    WorkflowStoreError,
)

__all__ = [
    "ACTION_KINDS",
    "REQUIRED_FEATURES",
    "Adapter",
    "AdapterNotConfigured",
    "AdapterRegistry",
    "ActionAdmission",
    "ActionConflict",
    "ActionDeliveryError",
    "ActionOutcome",
    "ActionRunner",
    "ActionView",
    "AmbiguousDecision",
    "AutomateHost",
    "AutomateLicenseError",
    "DecisionOutcome",
    "DefinitionError",
    "EngineError",
    "InvalidEffect",
    "LocalNotifyAdapter",
    "OperationConflict",
    "OperationReplay",
    "RecordView",
    "StaleRecord",
    "TransitionOutcome",
    "UnknownRecord",
    "UnresolvedAction",
    "Workflow",
    "WorkflowDefinition",
    "WorkflowEngine",
    "WorkflowMismatch",
    "WorkflowStore",
    "WorkflowStoreError",
    "canonical_workflow",
    "parse_workflow",
]
