"""Field-level semantic-to-canonical provenance mapping for composed designs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from eegle.authoring.contracts import (
    AuthoringOrigin,
    CanonicalArtifact,
    ScientificMateriality,
    SourceKind,
    SourceLocation,
)
from eegle.authoring.provenance import DraftSourceMap

if TYPE_CHECKING:
    from eegle.authoring.composition import ExperimentDesign


def _semantic_sources(payload: Mapping[str, Any], source_map: DraftSourceMap | None) -> dict[str, SourceLocation]:
    if source_map is None:
        return {}
    result = {"study": source_map.source_for("/study"), "recording": source_map.source_for("/recording")}
    for semantic, prefix, value in (
        ("study", "/study", payload.get("study", {})),
        ("recording", "/recording", payload.get("recording", {})),
    ):
        for relative in _leaf_paths(value):
            result[semantic + relative] = source_map.source_for(prefix + relative)
    if payload.get("initial_phase") is not None:
        result["initial_phase"] = source_map.source_for("/initial_phase")
    categories = {
        "signals": ("signal_id", "signal"),
        "events": ("event_id", "event"),
        "processing": ("chain_id", "processing"),
        "windows": ("window_id", "window"),
        "quality_gates": ("gate_id", "quality"),
        "models": ("model_id", "model"),
        "comparisons": ("comparison_id", "comparison"),
        "outcomes": ("outcome_id", "outcome"),
        "adaptations": ("adaptation_id", "adaptation"),
        "calibrations": ("calibration_id", "calibration"),
        "policies": ("policy_id", "policy"),
        "actions": ("action_id", "action"),
        "phases": ("phase_id", "phase"),
        "acceptance": ("criterion_id", "acceptance"),
    }
    for category, (field_name, prefix) in categories.items():
        for index, value in enumerate(payload.get(category, ())):
            semantic = f"{prefix}.{value[field_name]}"
            item_path = f"/{category}/{index}"
            result[semantic] = source_map.source_for(item_path)
            for relative in _leaf_paths(value):
                result[semantic + relative] = source_map.source_for(item_path + relative)
    return result


def _semantic_origins(payload: Mapping[str, Any]) -> dict[str, AuthoringOrigin]:
    result: dict[str, AuthoringOrigin] = {}
    for semantic, value in (
        ("study", payload.get("study", {})),
        ("recording", payload.get("recording", {})),
    ):
        for relative in _leaf_paths(value):
            result[semantic + relative] = AuthoringOrigin.USER_EXPLICIT
    if payload.get("initial_phase") is not None:
        result["initial_phase"] = AuthoringOrigin.USER_EXPLICIT
    categories = {
        "signals": ("signal_id", "signal"),
        "events": ("event_id", "event"),
        "processing": ("chain_id", "processing"),
        "windows": ("window_id", "window"),
        "quality_gates": ("gate_id", "quality"),
        "models": ("model_id", "model"),
        "comparisons": ("comparison_id", "comparison"),
        "outcomes": ("outcome_id", "outcome"),
        "adaptations": ("adaptation_id", "adaptation"),
        "calibrations": ("calibration_id", "calibration"),
        "policies": ("policy_id", "policy"),
        "actions": ("action_id", "action"),
        "phases": ("phase_id", "phase"),
        "acceptance": ("criterion_id", "acceptance"),
    }
    for category, (field_name, prefix) in categories.items():
        for value in payload.get(category, ()):
            semantic = f"{prefix}.{value[field_name]}"
            for relative in _leaf_paths(value):
                result[semantic + relative] = AuthoringOrigin.USER_EXPLICIT
    return result


def _semantic_draft_path(design: ExperimentDesign, semantic: str | None) -> str | None:
    if semantic is None:
        return None
    categories: tuple[tuple[str, Sequence[Any]], ...] = (
        ("signals", design.signals),
        ("events", design.events),
        ("processing", design.processing),
        ("windows", design.windows),
        ("quality_gates", design.quality_gates),
        ("models", design.models),
        ("comparisons", design.comparisons),
        ("outcomes", design.outcomes),
        ("adaptations", design.adaptations),
        ("calibrations", design.calibrations),
        ("policies", design.policies),
        ("actions", design.actions),
        ("phases", design.phases),
    )
    for category, values in categories:
        for index, value in enumerate(values):
            if value.semantic_id == semantic:
                return f"/intent/{category}/{index}"
    if semantic == "recording":
        return "/intent/recording"
    return None


def _semantic_key_for_draft_path(design: ExperimentDesign, path: str) -> str | None:
    if not path.startswith("/intent/"):
        return None
    tail = path[len("/intent/") :]
    if tail == "study" or tail.startswith("study/"):
        return "study" + tail[len("study") :]
    if tail == "recording" or tail.startswith("recording/"):
        return "recording" + tail[len("recording") :]
    if tail == "initial_phase":
        return "initial_phase"
    categories: tuple[tuple[str, Sequence[Any], str], ...] = (
        ("signals", design.signals, "semantic_id"),
        ("events", design.events, "semantic_id"),
        ("processing", design.processing, "semantic_id"),
        ("windows", design.windows, "semantic_id"),
        ("quality_gates", design.quality_gates, "semantic_id"),
        ("models", design.models, "semantic_id"),
        ("comparisons", design.comparisons, "semantic_id"),
        ("outcomes", design.outcomes, "semantic_id"),
        ("adaptations", design.adaptations, "semantic_id"),
        ("calibrations", design.calibrations, "semantic_id"),
        ("policies", design.policies, "semantic_id"),
        ("actions", design.actions, "semantic_id"),
        ("phases", design.phases, "semantic_id"),
    )
    for category, values, identity in categories:
        prefix = category + "/"
        if not tail.startswith(prefix):
            continue
        remainder = tail[len(prefix) :]
        index_text, _, relative = remainder.partition("/")
        if not index_text.isdigit() or int(index_text) >= len(values):
            return None
        semantic = getattr(values[int(index_text)], identity)
        return semantic + ("/" + relative if relative else "")
    if tail.startswith("acceptance/"):
        remainder = tail[len("acceptance/") :]
        index_text, _, relative = remainder.partition("/")
        if index_text.isdigit() and int(index_text) < len(design.acceptance):
            semantic = f"acceptance.{design.acceptance[int(index_text)].criterion_id}"
            return semantic + ("/" + relative if relative else "")
    return None


def _indexed_identity_path(path: str, prefix: str, identities: tuple[str, ...]) -> str | None:
    if not path.startswith(prefix + "/"):
        return None
    tail = path[len(prefix) + 1 :]
    index_text = tail.split("/", 1)[0]
    if not index_text.isdigit():
        return None
    index = int(index_text)
    return identities[index] if index < len(identities) else None


def _materiality(artifact: CanonicalArtifact, path: str) -> ScientificMateriality:
    if artifact == CanonicalArtifact.PROTOCOL:
        return ScientificMateriality.OPERATIONAL if path in {"/schema", "/protocol_id"} else ScientificMateriality.SCIENTIFIC
    if path.startswith(("/streams", "/components", "/routes", "/model_uses", "/outcome_expectations", "/adaptations", "/clock_policy", "/validation")):
        return ScientificMateriality.SCIENTIFIC
    return ScientificMateriality.OPERATIONAL


def _leaf_paths(value: Any, path: str = "") -> Iterable[str]:
    if isinstance(value, Mapping):
        if not value:
            yield path
        for key in sorted(value):
            yield from _leaf_paths(value[key], f"{path}/{_escape_pointer(str(key))}")
    elif isinstance(value, (list, tuple)):
        if not value:
            yield path
        for index, item in enumerate(value):
            yield from _leaf_paths(item, f"{path}/{index}")
    else:
        yield path


def _generated_source(symbol: str) -> SourceLocation:
    return SourceLocation(SourceKind.GENERATED, symbol=symbol)


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


__all__ = []
