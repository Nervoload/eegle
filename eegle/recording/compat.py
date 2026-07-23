"""Narrow compatibility view for historical recipe-shaped session paths."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from eegle.recording.artifacts import ArtifactStore

if TYPE_CHECKING:
    from eegle.recording.session import Session


# These names are compatibility aliases only. They are not artifact identities
# or target layout requirements.
LEGACY_PATH_ALIASES: dict[str, str] = {
    "raw": "raw",
    "events": "events",
    "calibration": "calibration",
    "logs": "logs",
    "reports": "reports",
    "realtime": "realtime",
    "process_logs": "logs/processes",
    "parameters": "parameters.json",
    "manifest": "manifest.json",
    "triggers": "triggers.txt",
    "behavior_csv": "events/behavior.csv",
    "events_jsonl": "events/events.jsonl",
    "telemetry_jsonl": "logs/telemetry.jsonl",
    "debug_jsonl": "logs/debug.jsonl",
    "calibration_events_jsonl": "calibration/events.jsonl",
    "calibration_metadata": "calibration/metadata.json",
    "calibration_eeg_csv": "calibration/eeg.csv",
    "calibration_result": "calibration/alpha_calibration.json",
    "calibration_psd_csv": "calibration/psd.csv",
    "calibration_spectral_model_json": "calibration/specparam.json",
    "calibration_plot": "calibration/alpha_calibration.svg",
    "eeg_csv": "raw/eeg.csv",
    "eeg_metadata": "raw/eeg_metadata.json",
    "realtime_windows_jsonl": "realtime/windows.jsonl",
    "realtime_decisions_jsonl": "realtime/decisions.jsonl",
    "realtime_model_predictions_jsonl": "realtime/model_predictions.jsonl",
    "realtime_markers_jsonl": "realtime/markers.jsonl",
    "realtime_feedback_jsonl": "realtime/feedback.jsonl",
    "realtime_alpha_jsonl": "realtime/alpha_power.jsonl",
    "realtime_event_features_jsonl": "realtime/event_features.jsonl",
    "realtime_engine_capture": "realtime/engine_input.bin",
    "realtime_engine_metadata": "realtime/engine_metadata.json",
    "realtime_epochs": "realtime/epochs",
    "realtime_epochs_jsonl": "realtime/epochs/epochs.jsonl",
    "realtime_epochs_npz": "realtime/epochs/epochs.npz",
    "realtime_epoch_manifest": "realtime/epochs/manifest.json",
    "realtime_model_snapshots": "realtime/models",
    "manager_summary": "logs/feedback_manager.json",
    "completion_summary": "session_summary.json",
}


class SessionPaths:
    """Resolve old path attributes through an ``ArtifactStore`` alias registry.

    New runtime code must use artifact namespace/identity, never this view. It
    remains available so selected old readers and recipes can address artifacts
    while Phase 4 replaces their fixed layout authority.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        artifact_store: ArtifactStore | None = None,
        **paths: str | Path,
    ) -> None:
        # Preserve the caller's lexical root (notably macOS /var versus
        # /private/var) because historical code compares displayed paths.
        self.root = Path(root).expanduser().absolute()
        if artifact_store is None:
            aliases = dict(LEGACY_PATH_ALIASES)
            for name, value in paths.items():
                path = Path(value).expanduser().absolute()
                try:
                    aliases[name] = path.relative_to(self.root).as_posix()
                except ValueError as exc:
                    raise ValueError(f"SessionPaths value {name} must be inside root") from exc
            artifact_store = ArtifactStore.compatibility_view(
                self.root,
                "legacy.compatibility-view",
                aliases,
            )
        self._artifact_store = artifact_store

    @classmethod
    def from_session(cls, session: "Session") -> "SessionPaths":
        store = session.artifacts
        missing = {
            name: uri
            for name, uri in LEGACY_PATH_ALIASES.items()
            if name not in store.manifest.aliases
        }
        if missing:
            if store.read_only:
                # Legacy discovery creates a complete in-memory alias registry.
                raise ValueError("read-only session lacks the legacy alias registry")
            store.declare_aliases(missing)
        return cls(session.root, artifact_store=store)

    @classmethod
    def from_legacy_layout(cls, root: str | Path) -> "SessionPaths":
        target = Path(root).expanduser().resolve()
        store = ArtifactStore.compatibility_view(
            target,
            "legacy.compatibility-view",
            LEGACY_PATH_ALIASES,
        )
        return cls(target, artifact_store=store)

    @property
    def artifact_store(self) -> ArtifactStore:
        return self._artifact_store

    def as_mapping(self) -> Mapping[str, Path]:
        return {name: self._artifact_store.alias_path(name) for name in LEGACY_PATH_ALIASES}

    def __getattr__(self, name: str) -> Any:
        if name in LEGACY_PATH_ALIASES:
            return self._artifact_store.alias_path(name)
        raise AttributeError(name)

    def __repr__(self) -> str:
        return f"SessionPaths(root={self.root!r}, aliases={len(LEGACY_PATH_ALIASES)})"
