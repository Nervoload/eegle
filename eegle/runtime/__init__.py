"""Integrated records and the sole plan-owned semantic execution engine."""

from eegle._domain import WorkStatus
from eegle.runtime.action_broker import ActionBroker, BrokerOutcome, PendingAuthorization
from eegle.runtime.context import DeterministicIdSource, RuntimeExecutionContext
from eegle.runtime.checkpoints import EngineCheckpoint, read_checkpoint, write_checkpoint
from eegle.runtime.graph import (
    GraphEmission,
    GraphInput,
    GraphPhaseResult,
    GraphRunStatus,
    PlanGraphExecutor,
)
from eegle.runtime.phases import (
    ConfirmSingleOperatorTransition,
    AcceptanceResult,
    EngineRunResult,
    EngineStatus,
    ExecutionEngine,
    OperatorController,
    PhaseAttempt,
    PhaseTransitionRecord,
)
from eegle.runtime.plan_runtime import (
    ComponentProxyFactory,
    PlanConstructionError,
    PlanRuntime,
    PlanRuntimeSnapshot,
    RuntimeNode,
    construct_plan_runtime,
)
from eegle.runtime.model_admission import (
    ArtifactResolver,
    LocalFileArtifactResolver,
    ModelAdmissionReceipt,
)
from eegle.runtime.model_runtime import (
    ModelComparison,
    ModelComparisonStatus,
    ModelResultDisposition,
    ModelResultDispositionStatus,
    ModelResultRejected,
)
from eegle.runtime.state import (
    AdaptationEligibilityDecision,
    AdaptationEligibilityStatus,
    AdaptationResult,
    Rejection,
    StateTransition,
    TransitionStatus,
    WorkRecord,
)
from eegle.runtime.outcomes import (
    Outcome,
    OutcomeDisposition,
    OutcomeDispositionStatus,
    OutcomeReference,
    OutcomeReferenceKind,
    OutcomeRoutingPolicy,
    OutcomeUse,
)
from eegle.runtime.scheduling import ScheduledTrigger, StateTriggerRule, TriggerResult


__all__ = [
    "ActionBroker",
    "ArtifactResolver",
    "DeterministicIdSource",
    "AcceptanceResult",
    "AdaptationEligibilityDecision",
    "AdaptationEligibilityStatus",
    "AdaptationResult",
    "BrokerOutcome",
    "EngineCheckpoint",
    "EngineRunResult",
    "EngineStatus",
    "ExecutionEngine",
    "GraphEmission",
    "GraphInput",
    "GraphPhaseResult",
    "GraphRunStatus",
    "OperatorController",
    "ModelResultDisposition",
    "ModelAdmissionReceipt",
    "ModelResultDispositionStatus",
    "ModelResultRejected",
    "ModelComparison",
    "ModelComparisonStatus",
    "Outcome",
    "OutcomeDisposition",
    "OutcomeDispositionStatus",
    "OutcomeReference",
    "OutcomeReferenceKind",
    "OutcomeRoutingPolicy",
    "OutcomeUse",
    "PendingAuthorization",
    "LocalFileArtifactResolver",
    "PhaseAttempt",
    "PhaseTransitionRecord",
    "PlanConstructionError",
    "PlanGraphExecutor",
    "PlanRuntime",
    "PlanRuntimeSnapshot",
    "Rejection",
    "RuntimeExecutionContext",
    "RuntimeNode",
    "StateTransition",
    "StateTriggerRule",
    "ScheduledTrigger",
    "TransitionStatus",
    "TriggerResult",
    "WorkRecord",
    "WorkStatus",
    "ComponentProxyFactory",
    "ConfirmSingleOperatorTransition",
    "construct_plan_runtime",
    "read_checkpoint",
    "write_checkpoint",
]
