"""Reusable behavioral conformance for independently packaged plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping

from eegle._domain import ComponentKind, Determinism, ExecutionMode
from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.models.results import ModelResult
from eegle.plugins.construction import ModelConstructionContext
from eegle.plugins.contracts import ExecutionContext
from eegle.plugins.registry import (
    PluginDescriptor,
    PluginRegistry,
    StateBehavior,
)
from eegle.streams.packets import DenseSampleBatch

PLUGIN_CONFORMANCE_REPORT_SCHEMA_ID = "eegle.plugin_conformance_report.v1"


class ConformanceStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class PluginConformanceCheck:
    check_id: str
    status: ConformanceStatus
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", require_identifier(self.check_id, "check_id"))
        object.__setattr__(self, "status", ConformanceStatus(self.status))
        if not self.summary.strip():
            raise ValueError("plugin conformance check summary cannot be empty")
        object.__setattr__(self, "details", freeze_json(self.details))

    def to_payload(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "summary": self.summary,
            "details": thaw_json(self.details),
        }


@dataclass(frozen=True, slots=True)
class PluginExercise:
    """One explicit component call used by the reusable conformance harness."""

    exercise_id: str
    invoke: Callable[[object], Any]
    expected_abstained: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exercise_id",
            require_identifier(self.exercise_id, "exercise_id"),
        )
        if not callable(self.invoke):
            raise TypeError("plugin conformance exercise invoke must be callable")
        if self.expected_abstained is not None and not isinstance(
            self.expected_abstained, bool
        ):
            raise TypeError("expected_abstained must be boolean or None")


@dataclass(frozen=True, slots=True)
class PluginConformanceReport:
    plugin_id: str
    plugin_version: str
    descriptor_hash: str
    constructed: bool
    checks: tuple[PluginConformanceCheck, ...]
    schema: str = PLUGIN_CONFORMANCE_REPORT_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != PLUGIN_CONFORMANCE_REPORT_SCHEMA_ID:
            raise ValueError(f"unsupported plugin conformance schema: {self.schema}")
        object.__setattr__(self, "plugin_id", require_identifier(self.plugin_id, "plugin_id"))
        checks = tuple(self.checks)
        if len({value.check_id for value in checks}) != len(checks):
            raise ValueError("plugin conformance check identities must be unique")
        object.__setattr__(self, "checks", checks)

    @property
    def ready(self) -> bool:
        return all(value.status != ConformanceStatus.FAIL for value in self.checks)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "descriptor_hash": self.descriptor_hash,
            "constructed": self.constructed,
            "ready": self.ready,
            "checks": [value.to_payload() for value in self.checks],
        }


def check_plugin_conformance(
    descriptor: PluginDescriptor,
    *,
    config: Mapping[str, Any] | None = None,
    construct: bool = False,
    construction_context: ModelConstructionContext | None = None,
    lifecycle_context: ExecutionContext | None = None,
    exercises: tuple[PluginExercise, ...] = (),
    mode: ExecutionMode | None = None,
) -> PluginConformanceReport:
    """Check one descriptor and, only when requested, construct and exercise it.

    Descriptor-only use never invokes the plugin factory. Construction, live
    I/O, and behavioral calls are explicit caller choices. Exercise callbacks
    should use fresh deterministic contexts so replay comparison observes
    component behavior rather than test-harness counters.
    """

    if not isinstance(descriptor, PluginDescriptor):
        raise TypeError("plugin conformance requires a PluginDescriptor")
    cases = tuple(exercises)
    if len({value.exercise_id for value in cases}) != len(cases):
        raise ValueError("plugin conformance exercise identities must be unique")
    checks = [
        PluginConformanceCheck(
            "descriptor.canonical",
            ConformanceStatus.PASS,
            "Descriptor is schema-valid and has a stable canonical identity.",
            {
                "descriptor_hash": descriptor.descriptor_hash,
                "construction_api": descriptor.construction_api.value,
            },
        ),
        PluginConformanceCheck(
            "descriptor.factory_not_invoked",
            ConformanceStatus.PASS,
            "Descriptor inspection completed without invoking the component factory.",
        ),
    ]
    if not construct:
        checks.append(
            PluginConformanceCheck(
                "construction.explicit",
                ConformanceStatus.NOT_APPLICABLE,
                "Component construction was not requested.",
            )
        )
        return PluginConformanceReport(
            descriptor.plugin_id,
            descriptor.version,
            descriptor.descriptor_hash,
            False,
            tuple(checks),
        )

    selected_mode = mode or sorted(
        descriptor.capabilities.supported_modes,
        key=lambda value: value.value,
    )[0]
    registry = PluginRegistry()
    registry.register(descriptor)

    def create_component() -> object:
        return registry.create(
            descriptor.plugin_id,
            config or {},
            f"=={descriptor.version}",
            mode=selected_mode,
            construction_context=construction_context,
        )

    try:
        component = create_component()
    except Exception as exc:
        checks.append(
            PluginConformanceCheck(
                "construction.component",
                ConformanceStatus.FAIL,
                "Explicit component construction failed.",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
        )
        return PluginConformanceReport(
            descriptor.plugin_id,
            descriptor.version,
            descriptor.descriptor_hash,
            False,
            tuple(checks),
        )
    checks.append(
        PluginConformanceCheck(
            "construction.component",
            ConformanceStatus.PASS,
            "Explicit construction produced a component satisfying its declared role.",
        )
    )

    initial_state: Mapping[str, Any] | None = None
    if descriptor.capabilities.state_behavior == StateBehavior.SNAPSHOT_RESTORE:
        try:
            initial_state = thaw_json(freeze_json(component.snapshot_state()))
        except Exception as exc:
            checks.append(
                PluginConformanceCheck(
                    "state.snapshot",
                    ConformanceStatus.FAIL,
                    "The declared snapshot state could not be captured.",
                    {"error_type": type(exc).__name__, "message": str(exc)},
                )
            )
        else:
            checks.append(
                PluginConformanceCheck(
                    "state.snapshot",
                    ConformanceStatus.PASS,
                    "The declared snapshot state is explicit finite JSON.",
                    {"state_hash": canonical_hash(initial_state)},
                )
            )
    else:
        checks.append(
            PluginConformanceCheck(
                "state.snapshot",
                ConformanceStatus.NOT_APPLICABLE,
                "The descriptor does not declare snapshot/restore state.",
            )
        )

    first = _exercise_component(
        component,
        descriptor,
        cases,
        lifecycle_context,
        checks,
        prefix="execution",
    )
    if not cases or first is None:
        return PluginConformanceReport(
            descriptor.plugin_id,
            descriptor.version,
            descriptor.descriptor_hash,
            True,
            tuple(checks),
        )

    try:
        replay_component = create_component()
        if initial_state is not None:
            replay_component.restore_state(initial_state)
    except Exception as exc:
        checks.append(
            PluginConformanceCheck(
                "replay.restore",
                ConformanceStatus.FAIL,
                "Fresh-component construction or state restoration failed.",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
        )
    else:
        if initial_state is not None:
            restored_hash = canonical_hash(replay_component.snapshot_state())
            expected_hash = canonical_hash(initial_state)
            checks.append(
                PluginConformanceCheck(
                    "replay.restore",
                    ConformanceStatus.PASS
                    if restored_hash == expected_hash
                    else ConformanceStatus.FAIL,
                    "Fresh-component state restoration reproduced the recorded state."
                    if restored_hash == expected_hash
                    else "Fresh-component state restoration changed the recorded state.",
                    {"expected_state_hash": expected_hash, "observed_state_hash": restored_hash},
                )
            )
        else:
            checks.append(
                PluginConformanceCheck(
                    "replay.restore",
                    ConformanceStatus.NOT_APPLICABLE,
                    "Stateless replay requires only fresh construction.",
                )
            )
        replayed = _exercise_component(
            replay_component,
            descriptor,
            cases,
            lifecycle_context,
            checks,
            prefix="replay",
        )
        if replayed is not None:
            equivalent = first == replayed
            status = (
                ConformanceStatus.PASS
                if equivalent or descriptor.capabilities.determinism != Determinism.DETERMINISTIC
                else ConformanceStatus.FAIL
            )
            checks.append(
                PluginConformanceCheck(
                    "replay.equivalence",
                    status,
                    "Fresh-component exercise results match the first execution."
                    if equivalent
                    else "Fresh-component exercise results differ from deterministic execution.",
                    {"first": first, "replayed": replayed},
                )
            )

    return PluginConformanceReport(
        descriptor.plugin_id,
        descriptor.version,
        descriptor.descriptor_hash,
        True,
        tuple(checks),
    )


def _exercise_component(
    component: object,
    descriptor: PluginDescriptor,
    exercises: tuple[PluginExercise, ...],
    context: ExecutionContext | None,
    checks: list[PluginConformanceCheck],
    *,
    prefix: str,
) -> Mapping[str, str] | None:
    start = getattr(component, "start", None)
    stop = getattr(component, "stop", None)
    started = False
    failed = False
    observations: dict[str, str] = {}
    try:
        if callable(start) or callable(stop):
            if not callable(start) or not callable(stop) or context is None:
                checks.append(
                    PluginConformanceCheck(
                        f"{prefix}.lifecycle",
                        ConformanceStatus.FAIL,
                        "Lifecycle components require callable start/stop and an explicit context.",
                    )
                )
                return None
            start(context)
            started = True
        else:
            checks.append(
                PluginConformanceCheck(
                    f"{prefix}.lifecycle",
                    ConformanceStatus.NOT_APPLICABLE,
                    "The component does not declare lifecycle hooks.",
                )
            )
        for case in exercises:
            try:
                result = case.invoke(component)
                if descriptor.kind == ComponentKind.MODEL:
                    if not isinstance(result, ModelResult):
                        raise TypeError("model exercise did not return ModelResult")
                    if (
                        case.expected_abstained is not None
                        and result.abstained != case.expected_abstained
                    ):
                        raise AssertionError(
                            f"expected abstained={case.expected_abstained}, "
                            f"observed {result.abstained}"
                        )
                observations[case.exercise_id] = canonical_hash(_observation_payload(result))
            except Exception as exc:
                failed = True
                checks.append(
                    PluginConformanceCheck(
                        f"{prefix}.{case.exercise_id}",
                        ConformanceStatus.FAIL,
                        "Plugin exercise failed.",
                        {"error_type": type(exc).__name__, "message": str(exc)},
                    )
                )
            else:
                checks.append(
                    PluginConformanceCheck(
                        f"{prefix}.{case.exercise_id}",
                        ConformanceStatus.PASS,
                        "Plugin exercise returned a canonical contract value.",
                        {"result_hash": observations[case.exercise_id]},
                    )
                )
    finally:
        if started:
            try:
                stop(context)
            except Exception as exc:
                failed = True
                checks.append(
                    PluginConformanceCheck(
                        f"{prefix}.cleanup",
                        ConformanceStatus.FAIL,
                        "Lifecycle cleanup failed.",
                        {"error_type": type(exc).__name__, "message": str(exc)},
                    )
                )
            else:
                checks.append(
                    PluginConformanceCheck(
                        f"{prefix}.cleanup",
                        ConformanceStatus.PASS,
                        "Lifecycle cleanup completed after the exercise path.",
                    )
                )
    return None if failed else freeze_json(observations)


def _observation_payload(value: Any) -> Any:
    to_payload = getattr(value, "to_payload", None)
    if callable(to_payload):
        return to_payload()
    if isinstance(value, tuple):
        return [_observation_payload(item) for item in value]
    if isinstance(value, list):
        return [_observation_payload(item) for item in value]
    return thaw_json(freeze_json(value))


def exercise_dense_transform(
    component: object,
    packet: DenseSampleBatch,
    context: ExecutionContext,
) -> DenseSampleBatch:
    """Exercise the common dense-transform contract or raise ``AssertionError``."""

    update = getattr(component, "update", None)
    if not callable(update):
        raise AssertionError("transform does not expose callable update")
    output = update(packet, context)
    if not isinstance(output, DenseSampleBatch):
        raise AssertionError("transform did not return DenseSampleBatch")
    if output.available_time != context.current_time:
        raise AssertionError("transform output availability does not match execution time")
    if output.lineage is None:
        raise AssertionError("transform output is missing lineage")
    if output.lineage.component_id != context.component_id:
        raise AssertionError("transform lineage has the wrong component identity")
    if output.lineage.input_ids != (packet.batch_id,):
        raise AssertionError("transform lineage does not identify the admitted input")
    if output.lineage.latest_input_available_time != packet.available_time:
        raise AssertionError("transform lineage lost the input availability frontier")
    if output.lineage.stream_revisions.get(packet.stream_id) != packet.stream_revision:
        raise AssertionError("transform lineage lost the input stream revision")
    return output


__all__ = [
    "PLUGIN_CONFORMANCE_REPORT_SCHEMA_ID",
    "ConformanceStatus",
    "PluginConformanceCheck",
    "PluginConformanceReport",
    "PluginExercise",
    "check_plugin_conformance",
    "exercise_dense_transform",
]
