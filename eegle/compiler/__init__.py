"""Typed specification compiler, immutable plans, locks, diagnostics, and diffing."""

from eegle.compiler.diagnostics import (
    CompilationDiagnostic,
    CompilationError,
    DiagnosticSeverity,
)
from eegle.compiler.lock import (
    CanonicalizationError,
    canonical_hash,
    canonical_json_bytes,
    content_hash,
    verify_hash,
)
from eegle.compiler.plan import (
    ExecutionPlan,
    LockedPlugin,
    PlannedActionGrant,
    PlannedArtifact,
    PlannedAdaptation,
    PlannedAuthorizationProvider,
    PlannedComponent,
    PlannedModelArtifactBinding,
    PlannedModelBinding,
    PlannedModelRole,
    PlannedOutcomeExpectation,
    PlannedPhase,
    PlannedPlacement,
    PlannedScheduledTrigger,
    PlannedStateTrigger,
    PlannedTransition,
)


def __getattr__(name: str):
    """Load Phase 5 compiler APIs lazily to keep plugin registration acyclic."""

    if name in {"CompilationResult", "compile_suite"}:
        from eegle.compiler.compiler import CompilationResult, compile_suite

        return {"CompilationResult": CompilationResult, "compile_suite": compile_suite}[name]
    if name in {
        "ChangeMateriality",
        "PlanChange",
        "PlanDiff",
        "PlanExplanation",
        "diff_plans",
        "explain_plan",
    }:
        from eegle.compiler.explain import (
            ChangeMateriality,
            PlanChange,
            PlanDiff,
            PlanExplanation,
            diff_plans,
            explain_plan,
        )

        return locals()[name]
    if name in {"CompiledGraph", "CompiledPort", "CompiledRoute", "PortDirection"}:
        from eegle.compiler.graph import (
            CompiledGraph,
            CompiledPort,
            CompiledRoute,
            PortDirection,
        )

        return locals()[name]
    if name == "ExecutionLock":
        from eegle.compiler.lockfile import ExecutionLock

        return ExecutionLock
    if name in {"read_lock", "read_plan", "write_lock", "write_plan"}:
        from eegle.compiler.io import read_lock, read_plan, write_lock, write_plan

        return locals()[name]
    raise AttributeError(name)


__all__ = [
    "CanonicalizationError",
    "ChangeMateriality",
    "CompilationDiagnostic",
    "CompilationError",
    "CompilationResult",
    "CompiledGraph",
    "CompiledPort",
    "CompiledRoute",
    "DiagnosticSeverity",
    "ExecutionLock",
    "ExecutionPlan",
    "LockedPlugin",
    "PlanChange",
    "PlanDiff",
    "PlanExplanation",
    "PlannedActionGrant",
    "PlannedAuthorizationProvider",
    "PlannedComponent",
    "PlannedAdaptation",
    "PlannedModelArtifactBinding",
    "PlannedModelBinding",
    "PlannedModelRole",
    "PlannedOutcomeExpectation",
    "PlannedArtifact",
    "PlannedPhase",
    "PlannedPlacement",
    "PlannedScheduledTrigger",
    "PlannedStateTrigger",
    "PlannedTransition",
    "PortDirection",
    "canonical_hash",
    "canonical_json_bytes",
    "compile_suite",
    "content_hash",
    "diff_plans",
    "explain_plan",
    "read_lock",
    "read_plan",
    "verify_hash",
    "write_lock",
    "write_plan",
]
