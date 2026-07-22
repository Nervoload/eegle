"""Public model-contract types for reproducible EEG inference."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from eegle.ml.contracts import (
    contract_hash,
    normalize_input_contract,
    resampling_mode,
    select_contract_channels,
    validate_supported_resampling,
)


MODEL_CONTRACT_SCHEMA = "eegle.model_contract.v2"


@dataclass(frozen=True)
class PreprocessingContract:
    """Preprocessing assumptions that must be satisfied before inference."""

    reference: str = "none"
    filters: tuple[dict[str, Any], ...] = ()
    baseline_seconds: tuple[float, float] | None = None
    artifact_policy: str = "declared_upstream"


@dataclass(frozen=True)
class TargetContract:
    """Target semantics for model predictions and operating thresholds."""

    name: str = "condition"
    positive_label: str = "no_go"
    label_mapping: dict[str, int] = field(default_factory=lambda: {"go": 0, "no_go": 1})
    learning_problem: str = "binary_classification"


@dataclass(frozen=True)
class ModelContract:
    """Versioned, typed declaration of model input and runtime constraints."""

    schema: str = MODEL_CONTRACT_SCHEMA
    input_kind: Literal["epoch", "rolling_window", "sequence"] = "epoch"
    channel_names: tuple[str, ...] = ()
    required_channels: tuple[str, ...] = ()
    optional_channels: tuple[str, ...] = ()
    missing_channel_policy: Literal["error", "drop", "zero_fill"] = "error"
    sample_rate_hz: float | None = None
    sample_rate_tolerance_hz: float = 0.01
    input_units: Literal["microvolts", "volts"] = "microvolts"
    epoch_window_seconds: tuple[float, float] | None = None
    prediction_horizon_seconds: tuple[float, float] | None = None
    preprocessing: PreprocessingContract = field(default_factory=PreprocessingContract)
    tensor_layout: str = "batch_1_channels_samples"
    target: TargetContract = field(default_factory=TargetContract)
    causal: bool = True
    latency_budget_ms: float | None = None
    adaptation_permissions: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ModelContract":
        """Build a v2 contract from a new or legacy bundle payload."""
        normalized = normalize_input_contract(payload, fallback_channel_names=payload.get("channel_names", ()))
        preprocessing = dict(payload.get("preprocessing") or {})
        raw_target = payload.get("target")
        target = dict(payload.get("target_contract") or payload.get("target_spec") or {})
        if isinstance(raw_target, dict):
            target.update(raw_target)
        target_name = raw_target if isinstance(raw_target, str) else target.get("name", target.get("target", "condition"))
        label_mapping = payload.get("label_mapping") or target.get("label_mapping") or {"go": 0, "no_go": 1}
        epoch_window = _optional_pair(normalized.get("epoch_window_seconds"))
        horizon = _optional_pair(payload.get("prediction_horizon_seconds"))
        return cls(
            schema=str(payload.get("schema", MODEL_CONTRACT_SCHEMA)),
            input_kind=str(payload.get("input_kind", "epoch")),  # type: ignore[arg-type]
            channel_names=tuple(str(value) for value in (payload.get("channel_names") or normalized.get("channel_names", ()))),
            required_channels=tuple(str(value) for value in normalized.get("required_channels", ())),
            optional_channels=tuple(str(value) for value in normalized.get("optional_channels", ())),
            missing_channel_policy=str(normalized.get("missing_channel_policy", "error")),  # type: ignore[arg-type]
            sample_rate_hz=None if normalized.get("sample_rate_hz") is None else float(normalized["sample_rate_hz"]),
            sample_rate_tolerance_hz=float(normalized.get("sample_rate_tolerance_hz", 0.01)),
            input_units=str(normalized.get("input_units", "microvolts")),  # type: ignore[arg-type]
            epoch_window_seconds=epoch_window,
            prediction_horizon_seconds=horizon,
            preprocessing=PreprocessingContract(
                reference=str(preprocessing.get("reference", payload.get("reference", "none"))),
                filters=tuple(dict(item) for item in preprocessing.get("filters", ())),
                baseline_seconds=_optional_pair(preprocessing.get("baseline_seconds", normalized.get("baseline_seconds"))),
                artifact_policy=str(preprocessing.get("artifact_policy", "declared_upstream")),
            ),
            tensor_layout=str(normalized.get("tensor_layout", "batch_1_channels_samples")),
            target=TargetContract(
                name=str(target_name),
                positive_label=str(target.get("positive_label", "no_go")),
                label_mapping={str(key): int(value) for key, value in dict(label_mapping).items()},
                learning_problem=str(target.get("learning_problem", "binary_classification")),
            ),
            causal=bool(payload.get("causal", True)),
            latency_budget_ms=None if payload.get("latency_budget_ms") is None else float(payload["latency_budget_ms"]),
            adaptation_permissions=tuple(str(value) for value in payload.get("adaptation_permissions", ())),
        )

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["channel_names"] = list(self.channel_names)
        data["required_channels"] = list(self.required_channels)
        data["optional_channels"] = list(self.optional_channels)
        data["adaptation_permissions"] = list(self.adaptation_permissions)
        if self.epoch_window_seconds is not None:
            data["epoch_window_seconds"] = list(self.epoch_window_seconds)
        if self.prediction_horizon_seconds is not None:
            data["prediction_horizon_seconds"] = list(self.prediction_horizon_seconds)
        if self.preprocessing.baseline_seconds is not None:
            data["preprocessing"]["baseline_seconds"] = list(self.preprocessing.baseline_seconds)
        data["preprocessing"]["filters"] = list(self.preprocessing.filters)
        return data

    def validate(self) -> None:
        if self.input_kind not in {"epoch", "rolling_window", "sequence"}:
            raise ValueError(f"unsupported input_kind '{self.input_kind}'")
        if self.missing_channel_policy not in {"error", "drop", "zero_fill"}:
            raise ValueError(f"unsupported missing_channel_policy '{self.missing_channel_policy}'")
        if self.sample_rate_hz is not None and self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.epoch_window_seconds is not None and self.epoch_window_seconds[1] <= self.epoch_window_seconds[0]:
            raise ValueError("epoch_window_seconds must be increasing")
        if self.latency_budget_ms is not None and self.latency_budget_ms <= 0:
            raise ValueError("latency_budget_ms must be positive")


def _optional_pair(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    values = list(value)
    if len(values) != 2:
        raise ValueError("expected a two-value range")
    return (float(values[0]), float(values[1]))
