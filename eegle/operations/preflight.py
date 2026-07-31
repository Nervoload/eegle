"""Capability-based preflight and fail-closed rehearsal evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from eegle._domain import ComponentKind
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler import ExecutionLock, ExecutionPlan, canonical_hash
from eegle.operations.discovery import DetectionReport
from eegle.plugins import PluginRegistry
from eegle.specs import DeploymentSpec, Placement


PREFLIGHT_REPORT_SCHEMA_ID = "eegle.preflight_report.v1"
REHEARSAL_REPORT_SCHEMA_ID = "eegle.rehearsal_report.v1"


class PreflightStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    check_id: str
    capability: str
    status: PreflightStatus
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", require_identifier(self.check_id, "check_id"))
        object.__setattr__(
            self, "capability", require_identifier(self.capability, "capability")
        )
        object.__setattr__(self, "status", PreflightStatus(self.status))
        if not self.summary.strip():
            raise ValueError("preflight check summary cannot be empty")
        object.__setattr__(self, "details", freeze_json(self.details))

    def to_payload(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "capability": self.capability,
            "status": self.status.value,
            "summary": self.summary,
            "details": thaw_json(self.details),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PreflightCheck":
        return cls(
            str(payload["check_id"]),
            str(payload["capability"]),
            PreflightStatus(str(payload["status"])),
            str(payload["summary"]),
            dict(payload.get("details") or {}),
        )


@dataclass(frozen=True, slots=True)
class PreflightReport:
    preflight_id: str
    plan_hash: str
    lock_hash: str
    deployment_hash: str
    checks: tuple[PreflightCheck, ...]
    detection_report_hash: str | None = None
    schema: str = PREFLIGHT_REPORT_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != PREFLIGHT_REPORT_SCHEMA_ID:
            raise ValueError(f"unsupported preflight schema: {self.schema}")
        object.__setattr__(
            self, "preflight_id", require_identifier(self.preflight_id, "preflight_id")
        )
        for name in ("plan_hash", "lock_hash", "deployment_hash"):
            object.__setattr__(self, name, require_digest(getattr(self, name), name))
        if self.detection_report_hash is not None:
            object.__setattr__(
                self,
                "detection_report_hash",
                require_digest(self.detection_report_hash, "detection_report_hash"),
            )
        checks = tuple(sorted(self.checks, key=lambda value: value.check_id))
        if not checks or len({value.check_id for value in checks}) != len(checks):
            raise ValueError("preflight checks must be non-empty and unique")
        object.__setattr__(self, "checks", checks)

    @property
    def ready(self) -> bool:
        return all(value.status != PreflightStatus.FAIL for value in self.checks)

    @property
    def report_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "preflight_id": self.preflight_id,
            "plan_hash": self.plan_hash,
            "lock_hash": self.lock_hash,
            "deployment_hash": self.deployment_hash,
            "detection_report_hash": self.detection_report_hash,
            "ready": self.ready,
            "checks": [value.to_payload() for value in self.checks],
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["report_hash"] = self.report_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PreflightReport":
        value = cls(
            preflight_id=str(payload["preflight_id"]),
            plan_hash=str(payload["plan_hash"]),
            lock_hash=str(payload["lock_hash"]),
            deployment_hash=str(payload["deployment_hash"]),
            detection_report_hash=None
            if payload.get("detection_report_hash") is None
            else str(payload["detection_report_hash"]),
            checks=tuple(PreflightCheck.from_payload(item) for item in payload["checks"]),
            schema=str(payload["schema"]),
        )
        if bool(payload.get("ready")) != value.ready:
            raise ValueError("preflight ready value does not match its checks")
        if payload.get("report_hash") != value.report_hash:
            raise ValueError("preflight report hash mismatch")
        return value


class FaultOutcome(str, Enum):
    CONTAINED = "contained"
    OBSERVE_ONLY = "observe_only"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class FaultScenarioOutcome:
    scenario_id: str
    outcome: FaultOutcome
    mechanism: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scenario_id", require_identifier(self.scenario_id, "scenario_id")
        )
        object.__setattr__(self, "outcome", FaultOutcome(self.outcome))
        if not self.mechanism.strip():
            raise ValueError("fault outcome mechanism cannot be empty")
        object.__setattr__(self, "evidence", freeze_json(self.evidence))

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "outcome": self.outcome.value,
            "mechanism": self.mechanism,
            "evidence": thaw_json(self.evidence),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FaultScenarioOutcome":
        return cls(
            str(payload["scenario_id"]),
            FaultOutcome(str(payload["outcome"])),
            str(payload["mechanism"]),
            dict(payload.get("evidence") or {}),
        )


@dataclass(frozen=True, slots=True)
class RehearsalReport:
    rehearsal_id: str
    portable_protocol_hash: str
    portable_suite_hash: str
    simulation_plan_hash: str
    simulation_lock_hash: str
    preflight_report_hash: str
    session_id: str
    bundle_id: str
    scenarios: tuple[FaultScenarioOutcome, ...]
    physical_action_authorized: bool = False
    live_plan_hash: str | None = None
    schema: str = REHEARSAL_REPORT_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != REHEARSAL_REPORT_SCHEMA_ID:
            raise ValueError(f"unsupported rehearsal schema: {self.schema}")
        object.__setattr__(
            self, "rehearsal_id", require_identifier(self.rehearsal_id, "rehearsal_id")
        )
        object.__setattr__(self, "session_id", require_identifier(self.session_id, "session_id"))
        object.__setattr__(self, "bundle_id", require_identifier(self.bundle_id, "bundle_id"))
        for name in (
            "portable_protocol_hash",
            "portable_suite_hash",
            "simulation_plan_hash",
            "simulation_lock_hash",
            "preflight_report_hash",
        ):
            object.__setattr__(self, name, require_digest(getattr(self, name), name))
        if self.live_plan_hash is not None:
            object.__setattr__(
                self, "live_plan_hash", require_digest(self.live_plan_hash, "live_plan_hash")
            )
            if self.live_plan_hash == self.simulation_plan_hash:
                raise ValueError("rehearsal cannot impersonate a distinct live plan identity")
        if self.physical_action_authorized:
            raise ValueError("rehearsal cannot authorize physical action")
        values = tuple(sorted(self.scenarios, key=lambda value: value.scenario_id))
        if len(values) != len({value.scenario_id for value in values}):
            raise ValueError("rehearsal fault scenario identities must be unique")
        object.__setattr__(self, "scenarios", values)

    @property
    def report_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "rehearsal_id": self.rehearsal_id,
            "portable_protocol_hash": self.portable_protocol_hash,
            "portable_suite_hash": self.portable_suite_hash,
            "simulation_plan_hash": self.simulation_plan_hash,
            "simulation_lock_hash": self.simulation_lock_hash,
            "preflight_report_hash": self.preflight_report_hash,
            "session_id": self.session_id,
            "bundle_id": self.bundle_id,
            "scenarios": [value.to_payload() for value in self.scenarios],
            "physical_action_authorized": False,
            "live_plan_hash": self.live_plan_hash,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["report_hash"] = self.report_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RehearsalReport":
        value = cls(
            rehearsal_id=str(payload["rehearsal_id"]),
            portable_protocol_hash=str(payload["portable_protocol_hash"]),
            portable_suite_hash=str(payload["portable_suite_hash"]),
            simulation_plan_hash=str(payload["simulation_plan_hash"]),
            simulation_lock_hash=str(payload["simulation_lock_hash"]),
            preflight_report_hash=str(payload["preflight_report_hash"]),
            session_id=str(payload["session_id"]),
            bundle_id=str(payload["bundle_id"]),
            scenarios=tuple(
                FaultScenarioOutcome.from_payload(item) for item in payload["scenarios"]
            ),
            physical_action_authorized=bool(payload.get("physical_action_authorized", False)),
            live_plan_hash=None
            if payload.get("live_plan_hash") is None
            else str(payload["live_plan_hash"]),
            schema=str(payload["schema"]),
        )
        if payload.get("report_hash") != value.report_hash:
            raise ValueError("rehearsal report hash mismatch")
        return value


def preflight(
    plan: ExecutionPlan,
    lock: ExecutionLock,
    deployment: DeploymentSpec,
    registry: PluginRegistry,
    *,
    detection_report: DetectionReport | None = None,
    operator_confirmations: Iterable[str] = (),
    safe_state_reports: Iterable[str] = (),
    available_secret_providers: Iterable[str] = (),
    preflight_id: str = "preflight.current",
) -> PreflightReport:
    """Verify a lock against only declared and observed capabilities.

    The function performs no transport probing. Optional integrations feed a
    hash-verified ``DetectionReport`` into this pure decision boundary.
    """

    if not all(
        isinstance(value, expected)
        for value, expected in (
            (plan, ExecutionPlan),
            (lock, ExecutionLock),
            (deployment, DeploymentSpec),
            (registry, PluginRegistry),
        )
    ):
        raise TypeError("preflight requires plan, lock, deployment, and plugin registry")
    confirmations = frozenset(operator_confirmations)
    safe_states = frozenset(safe_state_reports)
    secret_providers = frozenset(available_secret_providers)
    checks: list[PreflightCheck] = []

    try:
        lock.verify_plan(plan)
        integrity_status = PreflightStatus.PASS
        integrity_summary = "Execution lock matches the compiled plan"
    except ValueError as exc:
        integrity_status = PreflightStatus.FAIL
        integrity_summary = str(exc)
    checks.append(_check("lock.integrity", "locked_plan", integrity_status, integrity_summary))
    checks.append(
        _check(
            "deployment.identity",
            "deployment",
            PreflightStatus.PASS
            if plan.spec_hashes.get("deployment") == deployment.spec_hash
            else PreflightStatus.FAIL,
            "Deployment matches the plan's compiled site binding"
            if plan.spec_hashes.get("deployment") == deployment.spec_hash
            else "Deployment does not match the plan's compiled site binding",
        )
    )

    for plugin in plan.plugins:
        try:
            descriptor = registry.resolve(plugin.plugin_id, f"=={plugin.version}")
            matches = descriptor.descriptor_hash == plugin.descriptor_hash
        except (KeyError, ValueError):
            matches = False
        checks.append(
            _check(
                f"plugin.{plugin.plugin_id}",
                "locked_plugin",
                PreflightStatus.PASS if matches else PreflightStatus.FAIL,
                "Locked plugin descriptor is installed"
                if matches
                else "Locked plugin descriptor is unavailable or changed",
                {"plugin_id": plugin.plugin_id, "version": plugin.version},
            )
        )

    observed_sources: dict[str, list[Any]] = {}
    for value in (() if detection_report is None else detection_report.sources):
        observed_sources.setdefault(value.resource_id, []).append(value)
    resources = {value.resource_id: value for value in deployment.resources}
    bindings = {value.component_id: value for value in deployment.component_bindings}
    for component in plan.components:
        if _plugin_kind(plan, component.plugin_id, component.plugin_version) != ComponentKind.SOURCE:
            continue
        binding = bindings[component.component_id]
        for resource_id in binding.resource_ids:
            resource = resources[resource_id]
            simulated = resource.kind == "simulator"
            candidates = observed_sources.get(resource_id, ())
            observed = candidates[0] if len(candidates) == 1 else None
            available = simulated or observed is not None
            if observed is not None:
                available = (
                    observed.resource_kind == resource.kind
                    and observed.selector == resource.selector
                    and observed.plugin_id == component.plugin_id
                    and observed.plugin_version == component.plugin_version
                    and observed.contract == resource.contract
                )
            checks.append(
                _check(
                    f"stream.{component.component_id}.available",
                    "stream",
                    PreflightStatus.PASS if available else PreflightStatus.FAIL,
                    "Declared simulation stream is available"
                    if simulated
                    else "Exact live stream was detected"
                    if available
                    else "Exact live stream was not detected",
                    {
                        "resource_id": resource_id,
                        "candidate_count": len(candidates),
                        "selector": thaw_json(resource.selector),
                    },
                )
            )
            expected = component.config.get("stream_spec")
            if isinstance(expected, Mapping):
                actual = observed.stream if observed is not None else None
                expected_channels = tuple(
                    str(value["channel_id"]) for value in expected.get("channels", ())
                )
                channel_match = simulated or (
                    available
                    and actual is not None
                    and expected_channels
                    == tuple(value.channel_id for value in actual.channels)
                )
                unit_match = simulated or (
                    available
                    and actual is not None
                    and tuple(
                        str(value["unit"]) for value in expected.get("channels", ())
                    )
                    == tuple(value.unit for value in actual.channels)
                )
                expected_rate = expected.get("sample_rate_hz")
                rate_match = simulated or (
                    available
                    and actual is not None
                    and (
                        expected_rate is None
                        and actual.sample_rate_hz is None
                        or expected_rate is not None
                        and actual.sample_rate_hz is not None
                        and abs(float(expected_rate) - actual.sample_rate_hz)
                        <= max(0.01 * float(expected_rate), 1e-9)
                    )
                )
                for suffix, capability, matches in (
                    ("channels", "channels", channel_match),
                    ("units", "units", unit_match),
                    ("rate", "measured_rate", rate_match),
                ):
                    checks.append(
                        _check(
                            f"stream.{component.component_id}.{suffix}",
                            capability,
                            PreflightStatus.PASS if matches else PreflightStatus.FAIL,
                            f"Observed stream {suffix} match the locked stream"
                            if matches
                            else f"Observed stream {suffix} differ from the locked stream",
                        )
                    )

    observed_clocks = {
        (value.binding.source_clock, value.binding.target_clock): value
        for value in (() if detection_report is None else detection_report.clocks)
    }
    entirely_simulated = bool(deployment.resources) and all(
        resource.kind == "simulator" for resource in deployment.resources
    )
    for value in deployment.clock_mappings:
        observed = observed_clocks.get((value.source_clock, value.target_clock))
        status = (
            PreflightStatus.PASS
            if entirely_simulated or observed is not None
            else PreflightStatus.FAIL
        )
        if observed is not None:
            status = (
                PreflightStatus.PASS
                if observed.binding.maximum_uncertainty_seconds
                <= value.maximum_uncertainty_seconds
                else PreflightStatus.FAIL
            )
        checks.append(
            _check(
                f"clock.{value.source_clock}.{value.target_clock}",
                "clock_mapping",
                status,
                "Clock mapping is declared and within its uncertainty bound"
                if status == PreflightStatus.PASS
                else "Clock mapping was not observed within its uncertainty bound",
            )
        )

    observed_storage = {
        value.binding.storage_id: value
        for value in (() if detection_report is None else detection_report.storage)
    }
    for value in deployment.storage:
        parsed = urlsplit(value.uri)
        if parsed.scheme == "memory":
            status = PreflightStatus.PASS
            summary = "In-memory rehearsal storage is available"
        elif parsed.scheme in {"", "file"}:
            path = Path(unquote(parsed.path if parsed.scheme else value.uri)).expanduser()
            target = path if path.is_dir() else path.parent
            status = (
                PreflightStatus.PASS
                if target.exists() and os.access(target, os.W_OK)
                else PreflightStatus.FAIL
            )
            summary = "Local storage is writable" if status == PreflightStatus.PASS else "Local storage is not writable"
        else:
            status = (
                PreflightStatus.PASS
                if value.storage_id in observed_storage
                else PreflightStatus.FAIL
            )
            summary = "Storage endpoint was detected" if status == PreflightStatus.PASS else "Storage endpoint was not detected"
        checks.append(_check(f"storage.{value.storage_id}", "storage", status, summary))

    observed_placements = {
        (value.plugin_id, value.plugin_version, value.placement.value, value.endpoint_id)
        for value in (() if detection_report is None else detection_report.placements)
    }
    for placement in plan.placements:
        if placement.placement == Placement.IN_PROCESS.value:
            status = PreflightStatus.PASS
        else:
            component = next(value for value in plan.components if value.component_id == placement.component_id)
            status = (
                PreflightStatus.PASS
                if (
                    component.plugin_id,
                    component.plugin_version,
                    placement.placement,
                    placement.endpoint_id,
                )
                in observed_placements
                else PreflightStatus.FAIL
            )
        checks.append(
            _check(
                f"endpoint.{placement.component_id}",
                "endpoint",
                status,
                "Component placement endpoint is available"
                if status == PreflightStatus.PASS
                else "Component placement endpoint is unavailable",
            )
        )
        missing_providers = sorted(
            {
                next(value.provider for value in deployment.secrets if value.secret_id == secret_id)
                for secret_id in placement.secret_refs.values()
            }
            - secret_providers
        )
        if placement.secret_refs:
            checks.append(
                _check(
                    f"secrets.{placement.component_id}",
                    "secret_provider",
                    PreflightStatus.PASS if not missing_providers else PreflightStatus.FAIL,
                    "All secret-reference providers are available"
                    if not missing_providers
                    else "One or more secret-reference providers are unavailable",
                    {"missing_providers": missing_providers},
                )
            )

    for model in plan.model_bindings:
        artifact_statuses = tuple(
            _artifact_status(value.uri, value.digest) for value in model.artifacts
        )
        if PreflightStatus.FAIL in artifact_statuses:
            model_status = PreflightStatus.FAIL
            summary = "A local locked model artifact is unavailable or has changed"
        elif PreflightStatus.WARNING in artifact_statuses:
            model_status = PreflightStatus.WARNING
            summary = "Remote model artifacts remain digest-locked but were not probed"
        else:
            model_status = PreflightStatus.PASS
            summary = "Model manifest, implementation, and local artifacts are locked"
        checks.append(
            _check(
                f"model.{model.component_id}",
                "model_compatibility",
                model_status,
                summary,
                {"manifest_digest": model.manifest_digest},
            )
        )
    if not plan.model_bindings:
        checks.append(
            _check(
                "model.not_required",
                "model_compatibility",
                PreflightStatus.NOT_APPLICABLE,
                "The locked plan declares no model",
            )
        )

    for artifact in plan.artifacts:
        if artifact.external:
            locked = (
                artifact.expected_digest is not None
                and lock.artifact_hashes.get(artifact.artifact_id)
                == artifact.expected_digest
            )
            status = PreflightStatus.PASS if locked else PreflightStatus.FAIL
            summary = (
                "External artifact digest is locked"
                if locked
                else "External artifact has no matching lock digest"
            )
        else:
            status = PreflightStatus.NOT_APPLICABLE
            summary = "Artifact is produced during execution"
        checks.append(
            _check(
                f"artifact.{artifact.artifact_id}",
                "locked_artifact",
                status,
                summary,
            )
        )
    if not plan.artifacts:
        checks.append(
            _check(
                "artifact.not_required",
                "locked_artifact",
                PreflightStatus.NOT_APPLICABLE,
                "The locked plan declares no external or produced artifacts",
            )
        )

    observed_providers = {
        (value.provider_id, value.plugin_id, value.plugin_version)
        for value in (() if detection_report is None else detection_report.authorization_providers)
    }
    if not plan.authorization_providers:
        checks.append(
            _check(
                "authorization.absent",
                "authorization",
                PreflightStatus.PASS,
                "No provider is locked; action influence remains observe-only",
            )
        )
    for provider in plan.authorization_providers:
        present = provider.simulation_only or (
            provider.provider_id,
            provider.plugin_id,
            provider.plugin_version,
        ) in observed_providers
        checks.append(
            _check(
                f"authorization.{provider.provider_id}",
                "authorization",
                PreflightStatus.PASS if present else PreflightStatus.FAIL,
                "Authorization provider is available"
                if present
                else f"Authorization provider is unavailable; disposition is {provider.failure_disposition}",
                {"failure_disposition": provider.failure_disposition},
            )
        )

    gates = {
        f"phase.{value.phase_id}" for value in plan.phases if value.operator_confirmation
    }
    gates.update(
        f"permission.{value.permission_id}"
        for value in deployment.permissions
        if value.operator_confirmation
    )
    missing_gates = sorted(gates - confirmations)
    checks.append(
        _check(
            "operator.gates",
            "operator_gate",
            PreflightStatus.PASS if not missing_gates else PreflightStatus.FAIL,
            "All required operator gates are confirmed"
            if not missing_gates
            else "Required operator gates are not confirmed",
            {"required": sorted(gates), "missing": missing_gates},
        )
    )

    actuators = [
        value
        for value in plan.components
        if _plugin_kind(plan, value.plugin_id, value.plugin_version) == ComponentKind.ACTUATOR
    ]
    missing_safe = []
    for value in actuators:
        descriptor = registry.resolve(value.plugin_id, f"=={value.plugin_version}")
        if not descriptor.capabilities.simulation_only and value.component_id not in safe_states:
            missing_safe.append(value.component_id)
    checks.append(
        _check(
            "actuator.safe_state",
            "safe_state",
            PreflightStatus.PASS if not missing_safe else PreflightStatus.FAIL,
            "Actuator safe-state reporting is available"
            if not missing_safe
            else "Physical actuators have no safe-state report",
            {"missing_components": sorted(missing_safe)},
        )
    )
    return PreflightReport(
        preflight_id,
        plan.plan_hash,
        lock.lock_hash,
        deployment.spec_hash,
        tuple(checks),
        None if detection_report is None else detection_report.report_hash,
    )


def rehearsal_fault_outcomes(plan: ExecutionPlan, registry: PluginRegistry) -> tuple[FaultScenarioOutcome, ...]:
    """Describe the deterministic containment exercised by rehearsal evidence."""

    kinds = {
        value.component_id: _plugin_kind(plan, value.plugin_id, value.plugin_version)
        for value in plan.components
    }
    has_sparse = any(
        isinstance(value.config.get("stream_spec"), Mapping)
        and value.config["stream_spec"].get("content_kind") == "sparse_events"
        for value in plan.components
        if kinds[value.component_id] == ComponentKind.SOURCE
    )
    has_quality = ComponentKind.QUALITY in set(kinds.values())
    has_model = ComponentKind.MODEL in set(kinds.values())
    has_action = ComponentKind.ACTUATOR in set(kinds.values())
    scenarios = (
        ("missing_markers", has_sparse, "event windows remain unavailable without marker evidence"),
        ("late_packets", True, "the engine applies its locked lateness and watermark policy"),
        ("clock_drift", bool(plan.clock_policy), "clock revisions and uncertainty remain explicit"),
        ("low_quality", has_quality, "the quality gate suppresses inadmissible downstream work"),
        ("model_latency", has_model, "the locked deadline policy records delayed model work"),
        ("queue_overflow", has_model, "bounded runtime queues apply their locked overflow disposition"),
        ("authorization_denial", has_action, "denied or absent authority cannot construct an authorized command"),
        ("actuator_rejection", has_action, "a simulated rejection is a receipt, never physical delivery"),
    )
    values = []
    for scenario_id, applicable, mechanism in scenarios:
        if not applicable:
            outcome = FaultOutcome.NOT_APPLICABLE
        elif scenario_id == "authorization_denial" and not plan.authorization_providers:
            outcome = FaultOutcome.OBSERVE_ONLY
        else:
            outcome = FaultOutcome.CONTAINED
        values.append(
            FaultScenarioOutcome(
                scenario_id,
                outcome,
                mechanism,
                {
                    "plan_hash": plan.plan_hash,
                    "applicable": applicable,
                    "qualification_scope": "initial_disposition_probe",
                    "evidence_path": "normal_engine_bundle",
                },
            )
        )
    return tuple(values)


def assert_rehearsal_safe(plan: ExecutionPlan, registry: PluginRegistry) -> None:
    """Reject any rehearsal lock that could reach a non-simulation action authority."""

    for provider in plan.authorization_providers:
        if not provider.simulation_only:
            raise ValueError(
                f"rehearsal provider {provider.provider_id} is not simulation-only"
            )
    for component in plan.components:
        if _plugin_kind(plan, component.plugin_id, component.plugin_version) != ComponentKind.ACTUATOR:
            continue
        descriptor = registry.resolve(component.plugin_id, f"=={component.plugin_version}")
        if not descriptor.capabilities.simulation_only:
            raise ValueError(
                f"rehearsal actuator {component.component_id} is not simulation-only"
            )


def _plugin_kind(plan: ExecutionPlan, plugin_id: str, version: str) -> ComponentKind:
    return next(
        value.kind
        for value in plan.plugins
        if value.plugin_id == plugin_id and value.version == version
    )


def _artifact_status(uri: str, expected_digest: str) -> PreflightStatus:
    parsed = urlsplit(uri)
    if parsed.scheme not in {"", "file"}:
        return PreflightStatus.WARNING
    path = Path(unquote(parsed.path if parsed.scheme else uri)).expanduser()
    if not path.is_file():
        return PreflightStatus.FAIL
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return (
        PreflightStatus.PASS
        if f"sha256:{digest.hexdigest()}" == expected_digest
        else PreflightStatus.FAIL
    )


def _check(
    check_id: str,
    capability: str,
    status: PreflightStatus,
    summary: str,
    details: Mapping[str, Any] | None = None,
) -> PreflightCheck:
    return PreflightCheck(check_id, capability, status, summary, details or {})
