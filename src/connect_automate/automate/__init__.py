"""Licensed Automate host: a Local Connect consumer that runs signed workflow packs.

The Automate host is the generic runtime described in the build plan. It is a Connect
consumer, so it owns admission, ordering, and retry (ADR-0006), and it runs only while
the local machine holds an active ``connect.automations`` entitlement.

Slice 0 provides the host skeleton and the startup license gate, reusing the existing
entitlement substrate (:mod:`connect_automate.entitlement`). Later slices attach the
durable workflow record, the lifecycle ledger and stage engine, the effect executor and
outbox, the adapter registry, and the signed pack loader. Every one of those is admitted
only after :meth:`AutomateHost.require_license` passes.
"""

from __future__ import annotations

from .actions import (
    ACTION_KINDS,
    ActionContext,
    ActionDeliveryError,
    ActionOutcome,
    ActionRunner,
    Adapter,
    AdapterNotConfigured,
    AdapterRegistry,
    CapabilityInvoker,
    ConnectInvokeAdapter,
    LocalNotifyAdapter,
)
from .definition import (
    ActionEmit,
    ConnectInvokeCapability,
    ConnectInvokeInput,
    ConnectInvokeProvider,
    ConnectInvokeRequest,
    DefinitionError,
    OverlayBinding,
    RequestBindingError,
    Workflow,
    WorkflowDefinition,
    canonical_workflow,
    parse_workflow,
    render_connect_invoke_request,
)
from .engine import (
    AmbiguousDecision,
    DecisionOutcome,
    EngineError,
    PackOwnershipError,
    PackVersionError,
    WorkflowEngine,
    WorkflowMismatch,
)
from .host import REQUIRED_FEATURES, AutomateHost, AutomateLicenseError
from .pack import (
    MAX_GRANT_BYTES,
    MAX_PACK_BYTES,
    GrantView,
    LoadedPack,
    PackError,
    PackGrant,
    PackManifest,
    load_pack,
    verify_grant,
)
from .runtime import DecisionRun, PackRuntime
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
    "ActionContext",
    "ActionDeliveryError",
    "ActionEmit",
    "ActionOutcome",
    "ActionRunner",
    "ActionView",
    "AmbiguousDecision",
    "AutomateHost",
    "AutomateLicenseError",
    "CapabilityInvoker",
    "ConnectInvokeAdapter",
    "ConnectInvokeCapability",
    "ConnectInvokeInput",
    "ConnectInvokeProvider",
    "ConnectInvokeRequest",
    "DecisionOutcome",
    "DecisionRun",
    "DefinitionError",
    "EngineError",
    "GrantView",
    "InvalidEffect",
    "LoadedPack",
    "LocalNotifyAdapter",
    "MAX_GRANT_BYTES",
    "MAX_PACK_BYTES",
    "OperationConflict",
    "OperationReplay",
    "OverlayBinding",
    "RequestBindingError",
    "render_connect_invoke_request",
    "PackError",
    "PackGrant",
    "PackManifest",
    "PackOwnershipError",
    "PackRuntime",
    "PackVersionError",
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
    "load_pack",
    "parse_workflow",
    "verify_grant",
]
