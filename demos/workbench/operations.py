"""Workbench application services over EEGle's public operations surface."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from demos.workbench.profile import Study1Profile
from demos.workbench.state import (
    DetectedStreamSnapshot,
    PreflightCheckSnapshot,
    ReplaySnapshot,
    SessionSnapshot,
)
from eegle.authoring import SourceKind, SourceLocation
from eegle.integrations.lsl import (
    LSL_DENSE_SOURCE_PLUGIN_ID,
    LSL_SPARSE_SOURCE_PLUGIN_ID,
    LslDetection,
    detect_lsl,
)
from eegle.operations import (
    DeploymentSelection,
    DetectionReport,
    PreflightReport,
    ProjectCompilation,
    ProjectDeploymentProposal,
    SourceCapability,
    StorageCapability,
    compile_project,
    detect_capabilities,
    inspect_session,
    open_project,
    preflight_project,
    propose_project_deployment,
    signal_contract_from_stream,
)
from eegle.operations import (
    replay_session as replay_eegle_session,
)
from eegle.specs import StorageBinding
from eegle.streams import ChannelSpec, StreamSpec


@dataclass(frozen=True, slots=True)
class SiteOverlay:
    """Explicit operator review of positional Neuracle facts."""

    channel_order: tuple[str, ...]
    unit: str
    reference: str
    ground: str
    auxiliary_allocation: str
    trigger_status_position: int = 65

    def __post_init__(self) -> None:
        if len(self.channel_order) != 65:
            raise ValueError("the reviewed Neuracle overlay must contain exactly 65 values")
        if self.trigger_status_position != 65:
            raise ValueError("this prepared design requires TRIGGER_STATUS at LSL position 65")
        if not self.unit.strip():
            raise ValueError("the reviewed stream unit is required")
        for name in ("reference", "ground", "auxiliary_allocation"):
            if not getattr(self, name).strip():
                raise ValueError(f"the reviewed {name.replace('_', ' ')} is required")


@dataclass(frozen=True, slots=True)
class WorkbenchDetection:
    lsl: LslDetection
    report: DetectionReport
    streams: tuple[DetectedStreamSnapshot, ...]


@dataclass(frozen=True, slots=True)
class AcceptedDeployment:
    result: ProjectDeploymentProposal
    report: DetectionReport
    eeg_capability_id: str
    marker_capability_id: str
    overlay: SiteOverlay


@dataclass(frozen=True, slots=True)
class BuildResult:
    compilation: ProjectCompilation
    preflight: PreflightReport
    checks: tuple[PreflightCheckSnapshot, ...]
    deployment_role: str


@dataclass(frozen=True, slots=True)
class LiveRecheckResult:
    detection: WorkbenchDetection
    accepted: AcceptedDeployment
    preflight: PreflightReport
    checks: tuple[PreflightCheckSnapshot, ...]


class WorkbenchOperations:
    """Short, thread-safe calls used by the Qt controller."""

    def __init__(self, *, pylsl_module: Any | None = None) -> None:
        self._pylsl_module = pylsl_module

    def scan_lsl(self, project_root: Path, *, wait_time: float = 1.0) -> WorkbenchDetection:
        lsl = detect_lsl(
            wait_time=wait_time,
            boundary_clock_id="boundary.clock",
            logical_clock_id="device.clock",
            default_dense_unit="unknown",
            pylsl_module=self._pylsl_module,
        )
        storage = self._storage_capability(project_root)
        report = detect_capabilities(
            sources=lsl.sources,
            storage=(storage,),
            clocks=lsl.clocks,
            include_entry_points=True,
        )
        snapshots = tuple(self._stream_snapshot(value) for value in lsl.sources)
        return WorkbenchDetection(lsl, report, snapshots)

    def accept_live_deployment(
        self,
        project_root: Path,
        profile: Study1Profile,
        detection: WorkbenchDetection,
        *,
        eeg_capability_id: str,
        marker_capability_id: str,
        overlay: SiteOverlay,
    ) -> AcceptedDeployment:
        by_id = {value.capability_id: value for value in detection.lsl.sources}
        try:
            eeg = by_id[eeg_capability_id]
            markers = by_id[marker_capability_id]
        except KeyError as exc:
            raise ValueError("the selected LSL stream is no longer in the current scan") from exc
        for selected, role in ((eeg, "EEG"), (markers, "marker")):
            matches = tuple(
                value
                for value in detection.lsl.sources
                if dict(value.selector) == dict(selected.selector)
            )
            if len(matches) != 1:
                raise ValueError(
                    f"the selected {role} selector is ambiguous in the current LSL scan"
                )
        reviewed_eeg = self._reviewed_eeg_capability(eeg, profile, overlay)
        reviewed_markers = self._reviewed_marker_capability(markers, profile)
        storage = self._storage_capability(project_root)
        report = detect_capabilities(
            sources=(reviewed_eeg, reviewed_markers),
            storage=(storage,),
            clocks=detection.lsl.clocks,
            include_entry_points=True,
        )
        if not report.clocks:
            raise ValueError("the selected LSL streams have no device-to-boundary clock mapping")
        clock_id = report.clocks[0].capability_id
        selection = DeploymentSelection(
            source_capabilities={
                "requirement.source.eeg": reviewed_eeg.capability_id,
                "requirement.source.markers": reviewed_markers.capability_id,
            },
            storage_capabilities={
                "requirement.storage.evidence": storage.capability_id,
            },
            clock_capabilities={
                "requirement.clock.eeg": clock_id,
                "requirement.clock.markers": clock_id,
            },
        )
        result = propose_project_deployment(
            project_root,
            report,
            selection=selection,
            proposal_id="proposal.study1.live_lsl",
        )
        return AcceptedDeployment(
            result,
            report,
            reviewed_eeg.capability_id,
            reviewed_markers.capability_id,
            overlay,
        )

    def compile_and_preflight(
        self,
        project_root: Path,
        report: DetectionReport | None,
        *,
        simulation: bool = False,
    ) -> BuildResult:
        deployment_role = "simulation_deployment" if simulation else "deployment_proposal"
        compilation = compile_project(project_root, deployment_role=deployment_role)
        preflight = preflight_project(project_root, detection_report=report)
        checks = tuple(
            PreflightCheckSnapshot(
                value.check_id,
                value.capability,
                value.status.value,
                value.summary,
            )
            for value in preflight.checks
        )
        return BuildResult(compilation, preflight, checks, deployment_role)

    def recheck_live_deployment(
        self,
        project_root: Path,
        profile: Study1Profile,
        accepted: AcceptedDeployment,
    ) -> LiveRecheckResult:
        detection = self.scan_lsl(project_root)
        refreshed = self.accept_live_deployment(
            project_root,
            profile,
            detection,
            eeg_capability_id=accepted.eeg_capability_id.removesuffix(".reviewed"),
            marker_capability_id=accepted.marker_capability_id.removesuffix(".reviewed"),
            overlay=accepted.overlay,
        )
        if (
            refreshed.result.proposal.deployment.spec_hash
            != accepted.result.proposal.deployment.spec_hash
        ):
            raise ValueError(
                "the fresh LSL environment produces a different deployment; return to Apparatus"
            )
        preflight = preflight_project(project_root, detection_report=refreshed.report)
        checks = tuple(
            PreflightCheckSnapshot(
                value.check_id,
                value.capability,
                value.status.value,
                value.summary,
            )
            for value in preflight.checks
        )
        return LiveRecheckResult(detection, refreshed, preflight, checks)

    @staticmethod
    def inspect_project_sessions(project_root: Path) -> tuple[SessionSnapshot, ...]:
        project = open_project(project_root)
        values: list[SessionSnapshot] = []
        for uri in reversed(project.manifest.session_uris):
            root = project.root / uri
            inspection = inspect_session(root)
            payload = inspection.to_payload()
            bundles = tuple(inspection.bundles)
            bundle = bundles[0] if bundles else {}
            task_summary = WorkbenchOperations._task_summary(project.root, inspection.session.get("session_id"))
            marker_counts: dict[str, int] = {}
            for stream in inspection.source_health.get("streams", ()):
                for kind, count in dict(stream.get("event_kind_counts") or {}).items():
                    marker_counts[str(kind)] = marker_counts.get(str(kind), 0) + int(count)
            reconciled: bool | None = None
            if task_summary is not None:
                expected = int(task_summary.get("completed_trials", 0))
                reconciled = (
                    WorkbenchOperations._task_summary_valid(
                        task_summary,
                        str(inspection.session.get("session_id") or root.name),
                    )
                    and
                    marker_counts.get("dynamic_sart_stimulus_onset", 0) == expected
                    and marker_counts.get("dynamic_sart_task_end", 0) == 1
                )
            replay_rows = inspection.replay.get("bundles", ())
            replay_ready = any(bool(value.get("replay_ready")) for value in replay_rows)
            values.append(
                SessionSnapshot(
                    session_id=str(inspection.session.get("session_id") or root.name),
                    session_root=root,
                    session_status=str(inspection.summary.get("session_status") or "unknown"),
                    outcome=str(payload["status"]),
                    valid=inspection.valid,
                    bundle_id=None if not bundle else str(bundle.get("bundle_id")),
                    plan_hash=None if not bundle else str(bundle.get("plan_hash")),
                    engine_status=None if not bundle else str(bundle.get("engine_status")),
                    terminal_reason=None if not bundle or bundle.get("terminal_reason") is None else str(bundle["terminal_reason"]),
                    evidence_record_count=int(inspection.summary.get("evidence_record_count", 0)),
                    source_health=inspection.source_health,
                    phase_timeline=tuple(inspection.phase_timeline),
                    issues=tuple(value.to_payload() for value in inspection.issues),
                    replay_ready=replay_ready,
                    task_summary=task_summary,
                    task_reconciled=reconciled,
                )
            )
        return tuple(values)

    @staticmethod
    def replay_session(session: SessionSnapshot) -> ReplaySnapshot:
        result = replay_eegle_session(session.session_root, bundle_id=session.bundle_id)
        payload = result.to_payload()
        return ReplaySnapshot(
            session_id=session.session_id,
            status=str(payload["status"]),
            bundle_id=result.bundle_id,
            result_status=None if result.result_status is None else result.result_status.value,
            equivalent=result.equivalent,
            requested_level=result.requested_level,
            evaluated_level=result.evaluated_level,
            compared_record_count=result.compared_record_count,
            first_divergence=result.first_divergence,
            issues=tuple(value.to_payload() for value in result.issues),
        )

    @staticmethod
    def _task_summary(project_root: Path, session_id: Any) -> dict[str, Any] | None:
        if not session_id:
            return None
        path = project_root / "task_sessions" / str(session_id) / "study1_dsart_summary.json"
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _task_summary_valid(payload: dict[str, Any], session_id: str) -> bool:
        summary_hash = payload.get("summary_hash")
        plan_hash = str(payload.get("plan_hash") or "")
        if payload.get("session_id") != session_id or not plan_hash.startswith("sha256:"):
            return False
        basis = {key: value for key, value in payload.items() if key != "summary_hash"}
        digest = "sha256:" + hashlib.sha256(
            json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return summary_hash == digest

    @staticmethod
    def _storage_capability(project_root: Path) -> StorageCapability:
        destination = (project_root / "sessions").resolve()
        return StorageCapability(
            "capability.storage.workbench_sessions",
            StorageBinding("storage.evidence", "evidence", destination.as_uri()),
            ("local",),
            SourceLocation(SourceKind.DETECTION, locator="workbench-project-storage"),
        )

    @staticmethod
    def _stream_snapshot(source: SourceCapability) -> DetectedStreamSnapshot:
        lsl = source.stream.metadata.get("lsl", {})
        return DetectedStreamSnapshot(
            capability_id=source.capability_id,
            name=str(lsl.get("name", source.stream.stream_id)),
            stream_type=str(lsl.get("type", source.stream.modality)),
            channel_count=int(lsl.get("channel_count", len(source.stream.channels))),
            nominal_rate_hz=float(lsl.get("nominal_rate_hz", source.stream.sample_rate_hz or 0)),
            channel_format=str(lsl.get("channel_format", source.stream.sample_dtype or "unknown")),
            source_id=str(lsl.get("source_id", "")),
            uid=str(lsl.get("uid", "")),
            hostname=str(lsl.get("hostname", "")),
            content_kind=source.stream.content_kind.value,
            channel_labels=tuple(value.name or value.channel_id for value in source.stream.channels),
            channel_units=tuple(value.unit for value in source.stream.channels),
            channel_metadata_source=str(source.stream.metadata.get("channel_metadata_source"))
            if source.stream.metadata.get("channel_metadata_source") is not None
            else None,
            event_kinds=tuple(str(value) for value in source.stream.metadata.get("event_kinds", ())),
        )

    @staticmethod
    def _reviewed_eeg_capability(
        source: SourceCapability,
        profile: Study1Profile,
        overlay: SiteOverlay,
    ) -> SourceCapability:
        if source.plugin_id != LSL_DENSE_SOURCE_PLUGIN_ID:
            raise ValueError("the selected EEG capability is not a dense LSL stream")
        identity = source.stream.metadata.get("lsl", {})
        if int(identity.get("channel_count", 0)) != 65:
            raise ValueError("Study 1 requires exactly 65 Neuracle LSL values")
        if float(identity.get("nominal_rate_hz", 0.0)) != 1000.0:
            raise ValueError("Study 1 requires a 1000 Hz Neuracle LSL stream")
        channels = tuple(
            ChannelSpec(
                logical_id,
                "status" if logical_id == "TRIGGER_STATUS" else "eeg",
                overlay.unit,
                name=physical_name,
            )
            for logical_id, physical_name in zip(profile.logical_channels, overlay.channel_order)
        )
        payload = source.stream.to_payload()
        payload["channels"] = [value.to_payload() for value in channels]
        payload["missing_data_policy"] = "validity_mask"
        metadata = dict(payload.get("metadata") or {})
        metadata["site_overlay"] = {
            "schema": "eegle.workbench.neuracle64_site_overlay.v1",
            "reviewed": True,
            "physical_channel_order": list(overlay.channel_order),
            "logical_channel_order": list(profile.logical_channels),
            "unit": overlay.unit,
            "reference": overlay.reference,
            "ground": overlay.ground,
            "auxiliary_allocation": overlay.auxiliary_allocation,
            "trigger_status_position": overlay.trigger_status_position,
            "selector": dict(source.selector),
        }
        payload["metadata"] = metadata
        projected = StreamSpec.from_payload(payload)
        config = dict(source.config)
        config["stream_spec"] = projected.to_payload()
        return SourceCapability(
            capability_id=f"{source.capability_id}.reviewed",
            resource_id=f"{source.resource_id}.reviewed",
            resource_kind=source.resource_kind,
            stream=projected,
            contract=signal_contract_from_stream(projected),
            plugin_id=source.plugin_id,
            plugin_version=source.plugin_version,
            selector=dict(source.selector),
            capabilities=(*source.capabilities, "reviewed_site_overlay"),
            config=config,
            placement=source.placement,
            source=SourceLocation(
                SourceKind.DETECTION,
                locator="workbench-reviewed-neuracle-overlay",
                symbol=source.capability_id,
            ),
        )

    @staticmethod
    def _reviewed_marker_capability(
        source: SourceCapability,
        profile: Study1Profile,
    ) -> SourceCapability:
        if source.plugin_id != LSL_SPARSE_SOURCE_PLUGIN_ID:
            raise ValueError("the selected marker capability is not a sparse LSL stream")
        identity = source.stream.metadata.get("lsl", {})
        expected = profile.payload["markers"]
        if identity.get("name") != expected["stream_name"]:
            raise ValueError("select the marker stream armed by this Workbench task")
        metadata = dict(source.stream.metadata)
        metadata["event_kinds"] = list(profile.marker_kinds)
        metadata["task_contract_source"] = "eegle.workbench.study1_dsart.v1"
        payload = source.stream.to_payload()
        payload["metadata"] = metadata
        projected = StreamSpec.from_payload(payload)
        config = dict(source.config)
        config["stream_spec"] = projected.to_payload()
        return SourceCapability(
            capability_id=f"{source.capability_id}.reviewed",
            resource_id=f"{source.resource_id}.reviewed",
            resource_kind=source.resource_kind,
            stream=projected,
            contract=signal_contract_from_stream(projected),
            plugin_id=source.plugin_id,
            plugin_version=source.plugin_version,
            selector=dict(source.selector),
            capabilities=(*source.capabilities, "task_contract_declared"),
            config=config,
            placement=source.placement,
            source=SourceLocation(
                SourceKind.DETECTION,
                locator="workbench-armed-task-marker",
                symbol=source.capability_id,
            ),
        )
