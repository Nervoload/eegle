"""Realtime model adapters for window and marker-locked EEG inference."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from eegle.ml.calibration import binary_metrics_at_threshold, select_binary_threshold
from eegle.ml.contracts import contract_hash, normalize_input_contract, validate_supported_resampling
from eegle.ml.registry import get_model_spec, resolve_model_kind
from eegle.ml.targets import build_training_target
from eegle.realtime.classification import (
    DEFAULT_ROI_CONFIG,
    assess_epoch_quality,
    extract_erp_roi_features,
    feature_vector,
    file_sha256,
    load_model_bundle,
    prepare_classifier_epoch,
    write_model_bundle,
)


@dataclass
class ModelPrediction:
    """Normalized model output consumed by decision policies."""

    label: str
    score: float
    features: dict[str, float] = field(default_factory=dict)
    probability: float | None = None
    model_kind: str = "unknown"
    model_version: str | None = None
    latency_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = 1
        return payload


class ModelAdapter(Protocol):
    """Classifier interface for realtime and replayed EEG epochs."""

    kind: str

    def predict_epoch(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> ModelPrediction:
        ...

    def predict_window(self, data: np.ndarray, sample_rate_hz: float) -> ModelPrediction:
        ...


class PreparedEpochCache:
    """Reusable prepared views for one realtime epoch."""

    def __init__(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> None:
        self.epoch = np.asarray(epoch, dtype=float)
        self.sample_rate_hz = float(sample_rate_hz)
        self.channel_names = list(channel_names)
        self.metadata = dict(metadata)
        self._raw_channels_cache: dict[str, np.ndarray] = {}
        self._classifier_cache: dict[str, tuple[np.ndarray, list[str], np.ndarray]] = {}

    def raw_channels_samples(self, input_layout: str) -> np.ndarray:
        layout = str(input_layout or "auto")
        if layout not in self._raw_channels_cache:
            self._raw_channels_cache[layout] = epoch_to_channels_samples(self.epoch, self.channel_names, layout)
        return self._raw_channels_cache[layout]

    def raw_samples_channels(self, input_layout: str) -> np.ndarray:
        return self.raw_channels_samples(input_layout).T

    def classifier(
        self,
        contract: dict[str, Any],
        input_layout: str,
    ) -> tuple[np.ndarray, list[str], np.ndarray]:
        key = _prepared_epoch_key(contract, input_layout)
        if key not in self._classifier_cache:
            self._classifier_cache[key] = prepare_classifier_epoch(
                self.epoch,
                self.sample_rate_hz,
                self.channel_names,
                self.metadata,
                {**dict(contract), "input_layout": str(input_layout or "auto")},
            )
        return self._classifier_cache[key]

    def artifact(self, contract: dict[str, Any], input_layout: str) -> np.ndarray:
        if not contract:
            return self.raw_channels_samples(input_layout)
        return self.classifier(contract, input_layout)[0]

    def times(self, sample_count: int) -> np.ndarray:
        return relative_times(self.metadata, sample_count, self.sample_rate_hz)


class BaseModelAdapter:
    kind = "base"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.model_version = self.config.get("model_version")
        self.input_layout = str(self.config.get("input_layout", "auto"))
        self._artifact_cache: Any | None = None

    def predict_window(self, data: np.ndarray, sample_rate_hz: float) -> ModelPrediction:
        channel_names = [f"ch_{idx + 1:03d}" for idx in range(np.asarray(data).shape[1])]
        metadata = {"source": "rolling_window", "input_layout": "samples_x_channels"}
        return self.predict_epoch(data, sample_rate_hz, channel_names, metadata)

    def predict_prepared_epoch(self, prepared: PreparedEpochCache) -> ModelPrediction:
        return self.predict_epoch(
            prepared.epoch,
            prepared.sample_rate_hz,
            prepared.channel_names,
            prepared.metadata,
        )


class ERPPeakBaselineAdapter(BaseModelAdapter):
    """Sanity adapter using ROI baseline correction and P300 peak amplitude."""

    kind = "erp_peak_baseline"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.roi_channels = [str(name) for name in self.config.get("roi_channels", ["Pz", "Cz", "CPz", "P3", "P4"])]
        self.baseline_seconds = tuple(float(value) for value in self.config.get("baseline_seconds", [-0.2, 0.0]))
        self.p300_window_seconds = tuple(float(value) for value in self.config.get("p300_window_seconds", [0.25, 0.6]))
        self.amplitude_threshold_uv = float(self.config.get("amplitude_threshold_uv", 3.0))
        self.amplitude_slope_uv = max(1e-6, float(self.config.get("amplitude_slope_uv", 2.0)))
        self.decision_probability = float(self.config.get("decision_probability", 0.6))
        self.input_units = str(self.config.get("input_units", "microvolts")).lower()

    def predict_epoch(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> ModelPrediction:
        return self.predict_prepared_epoch(PreparedEpochCache(epoch, sample_rate_hz, channel_names, metadata))

    def predict_prepared_epoch(self, prepared: PreparedEpochCache) -> ModelPrediction:
        channels_samples = prepared.raw_channels_samples(self.input_layout)
        if channels_samples.size == 0:
            return ModelPrediction(
                label="no_p300",
                score=0.0,
                probability=0.0,
                features={"p300_amplitude_uv": 0.0, "p300_latency_ms": 0.0},
                model_kind=self.kind,
                model_version=self.model_version,
            )

        times = prepared.times(channels_samples.shape[1])
        data_uv = channels_samples.astype(float)
        if self.input_units in {"v", "volt", "volts"}:
            data_uv = data_uv * 1e6

        roi_indices = resolve_roi_indices(prepared.channel_names, self.roi_channels, channels_samples.shape[0])
        roi_wave = data_uv[roi_indices].mean(axis=0)
        baseline_mask = (times >= self.baseline_seconds[0]) & (times <= self.baseline_seconds[1])
        if baseline_mask.any():
            roi_wave = roi_wave - float(roi_wave[baseline_mask].mean())

        p300_mask = (times >= self.p300_window_seconds[0]) & (times <= self.p300_window_seconds[1])
        if not p300_mask.any():
            peak_idx = int(np.argmax(roi_wave))
        else:
            mask_indices = np.flatnonzero(p300_mask)
            peak_idx = int(mask_indices[np.argmax(roi_wave[p300_mask])])
        amplitude_uv = float(roi_wave[peak_idx])
        latency_ms = float(times[peak_idx] * 1000.0)
        probability = logistic_probability(amplitude_uv, self.amplitude_threshold_uv, self.amplitude_slope_uv)
        label = "p300_present" if probability >= self.decision_probability else "no_p300"
        features = {
            "p300_amplitude_uv": amplitude_uv,
            "p300_latency_ms": latency_ms,
            "roi_mean_uv": float(roi_wave.mean()),
            "roi_peak_to_peak_uv": float(np.ptp(roi_wave)),
        }
        return ModelPrediction(
            label=label,
            score=amplitude_uv,
            probability=probability,
            features=features,
            model_kind=self.kind,
            model_version=self.model_version,
            metadata={"roi_channels": [prepared.channel_names[index] for index in roi_indices if index < len(prepared.channel_names)]},
        )


class BandPowerThresholdModel(BaseModelAdapter):
    """Compatibility adapter for the older rolling-window scaffold."""

    kind = "band_power_threshold"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.bands = self.config.get("bands", {})
        self.threshold = float(self.config.get("threshold", 1.0))
        self.decision_feature = self.config.get("decision_feature", "alpha_theta_ratio")

    def predict(self, data: np.ndarray, sample_rate_hz: float) -> ModelPrediction:
        return self.predict_window(data, sample_rate_hz)

    def predict_epoch(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> ModelPrediction:
        return self.predict_prepared_epoch(PreparedEpochCache(epoch, sample_rate_hz, channel_names, metadata))

    def predict_prepared_epoch(self, prepared: PreparedEpochCache) -> ModelPrediction:
        samples_channels = prepared.raw_samples_channels(self.input_layout)
        features = band_power_features(samples_channels, prepared.sample_rate_hz, self.bands)
        theta = features.get("theta", 0.0)
        alpha = features.get("alpha", 0.0)
        ratio = alpha / theta if theta > 0 else 0.0
        features["alpha_theta_ratio"] = ratio
        probability = min(1.0, max(0.0, ratio / max(self.threshold * 2.0, 1e-6)))
        label = "increase_feedback" if ratio >= self.threshold else "decrease_feedback"
        return ModelPrediction(
            label=label,
            score=ratio,
            probability=probability,
            features=features,
            model_kind=self.kind,
            model_version=self.model_version,
        )


class SklearnXdawnLdaAdapter(BaseModelAdapter):
    """Backward-compatible alias for the flattened-epoch LDA baseline."""

    kind = "sklearn_xdawn_lda"

    def predict_epoch(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> ModelPrediction:
        return self.predict_prepared_epoch(PreparedEpochCache(epoch, sample_rate_hz, channel_names, metadata))

    def predict_prepared_epoch(self, prepared: PreparedEpochCache) -> ModelPrediction:
        artifact = self._load_artifact()
        contract = artifact.get("contract", {}) if isinstance(artifact, dict) else {}
        channels_samples = prepared.artifact(contract, self.input_layout)
        estimator = artifact.get("pipeline") if isinstance(artifact, dict) else artifact
        probability = estimator_probability(estimator, channels_samples.reshape(1, -1))
        label = classifier_label(probability, artifact if isinstance(artifact, dict) else self.config)
        return ModelPrediction(
            label=label,
            score=probability,
            probability=probability,
            features={"probability_no_go": probability},
            model_kind=self.kind,
            model_version=artifact.get("model_version") if isinstance(artifact, dict) else self.model_version,
            metadata=artifact_prediction_metadata(artifact),
        )

    def _load_artifact(self) -> Any:
        if self._artifact_cache is None:
            self._artifact_cache = load_joblib_artifact(self.config, self.kind)
        return self._artifact_cache


class SklearnFlattenLdaAdapter(SklearnXdawnLdaAdapter):
    kind = "sklearn_flatten_lda"


class ErpRoiLogisticRegressionAdapter(BaseModelAdapter):
    """Interpretable baseline-corrected ERP ROI logistic regression."""

    kind = "erp_roi_logreg"

    def predict_epoch(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> ModelPrediction:
        return self.predict_prepared_epoch(PreparedEpochCache(epoch, sample_rate_hz, channel_names, metadata))

    def predict_prepared_epoch(self, prepared: PreparedEpochCache) -> ModelPrediction:
        artifact = self._load_artifact()
        contract = artifact.get("contract", {}) if isinstance(artifact, dict) else {}
        corrected, selected_channels, times = prepared.classifier(contract, self.input_layout)
        roi_config = artifact.get("roi_config", DEFAULT_ROI_CONFIG) if isinstance(artifact, dict) else DEFAULT_ROI_CONFIG
        features = extract_erp_roi_features(corrected, times, selected_channels, roi_config)
        vector, _ = feature_vector(features, artifact.get("feature_names") if isinstance(artifact, dict) else None)
        estimator = artifact.get("pipeline") if isinstance(artifact, dict) else artifact
        probability = estimator_probability(estimator, vector.reshape(1, -1))
        return ModelPrediction(
            label=classifier_label(probability, artifact if isinstance(artifact, dict) else self.config),
            score=probability,
            probability=probability,
            features={**features, "probability_no_go": probability},
            model_kind=self.kind,
            model_version=artifact.get("model_version") if isinstance(artifact, dict) else self.model_version,
            metadata=artifact_prediction_metadata(artifact),
        )

    def _load_artifact(self) -> Any:
        if self._artifact_cache is None:
            self._artifact_cache = load_joblib_artifact(self.config, self.kind)
        return self._artifact_cache


class PyriemannErpCovAdapter(BaseModelAdapter):
    """ERP covariance classifier backed by a joblib pyriemann artifact."""

    kind = "pyriemann_erp_cov"

    def predict_epoch(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> ModelPrediction:
        return self.predict_prepared_epoch(PreparedEpochCache(epoch, sample_rate_hz, channel_names, metadata))

    def predict_prepared_epoch(self, prepared: PreparedEpochCache) -> ModelPrediction:
        artifact = self._load_artifact()
        contract = artifact.get("contract", {}) if isinstance(artifact, dict) else {}
        channels_samples = prepared.artifact(contract, self.input_layout)
        estimator = artifact.get("pipeline") if isinstance(artifact, dict) else artifact
        probability = estimator_probability(estimator, channels_samples[np.newaxis, :, :])
        label = classifier_label(probability, artifact if isinstance(artifact, dict) else self.config)
        return ModelPrediction(
            label=label,
            score=probability,
            probability=probability,
            features={"probability_no_go": probability},
            model_kind=self.kind,
            model_version=artifact.get("model_version") if isinstance(artifact, dict) else self.model_version,
            metadata=artifact_prediction_metadata(artifact),
        )

    def _load_artifact(self) -> Any:
        if self._artifact_cache is None:
            self._artifact_cache = load_joblib_artifact(self.config, self.kind)
        return self._artifact_cache


class TorchEpochAdapter(BaseModelAdapter):
    """TorchScript inference adapter for EEGNet/ShallowConvNet-style models."""

    kind = "torch_epoch"

    def predict_epoch(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> ModelPrediction:
        return self.predict_prepared_epoch(PreparedEpochCache(epoch, sample_rate_hz, channel_names, metadata))

    def predict_prepared_epoch(self, prepared: PreparedEpochCache) -> ModelPrediction:
        bundle = configured_bundle(self.config)
        artifact_path = Path(bundle["artifact_path"]) if bundle else require_artifact_path(self.config, self.kind)
        try:
            import torch
        except Exception as exc:
            raise RuntimeError(f"{self.kind} requires torch for inference") from exc

        contract = bundle.get("contract", {}) if bundle else {}
        channels_samples = prepared.artifact(contract, self.input_layout)
        tensor = torch.as_tensor(torch_input_array(channels_samples, self.config), dtype=torch.float32)
        model = self._load_torch_model(torch, artifact_path)
        model.eval()
        with torch.no_grad():
            output = model(tensor)
        probability = tensor_probability(output.detach().cpu().numpy())
        label_config = dict(self.config)
        if bundle is not None:
            label_config.update({"calibration": bundle.get("calibration"), "target": bundle.get("target")})
        label = classifier_label(probability, label_config)
        return ModelPrediction(
            label=label,
            score=probability,
            probability=probability,
            features={"probability_no_go": probability},
            model_kind=self.kind,
            model_version=self.model_version,
            metadata={} if bundle is None else bundle_prediction_metadata(bundle),
        )

    def _load_torch_model(self, torch: Any, artifact_path: Path) -> Any:
        if self._artifact_cache is None:
            self._artifact_cache = torch.jit.load(str(artifact_path), map_location="cpu")
            self._artifact_cache.eval()
        return self._artifact_cache


class TorchEEGNetAdapter(TorchEpochAdapter):
    kind = "torch_eegnet"


class TorchShallowConvNetAdapter(TorchEpochAdapter):
    kind = "torch_shallowconvnet"


class FoundationBendrAdapter(TorchEpochAdapter):
    kind = "foundation_bendr"


class FoundationLabramAdapter(TorchEpochAdapter):
    kind = "foundation_labram"


class SequenceExternalAdapter(TorchEpochAdapter):
    kind = "sequence_external"


class OnnxP300Adapter(BaseModelAdapter):
    """ONNX Runtime inference adapter for exported P300 classifiers."""

    kind = "onnx_p300"

    def predict_epoch(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> ModelPrediction:
        return self.predict_prepared_epoch(PreparedEpochCache(epoch, sample_rate_hz, channel_names, metadata))

    def predict_prepared_epoch(self, prepared: PreparedEpochCache) -> ModelPrediction:
        artifact_path = require_artifact_path(self.config, self.kind)
        try:
            import onnxruntime as ort
        except Exception as exc:
            raise RuntimeError(f"{self.kind} requires onnxruntime for inference") from exc

        channels_samples = prepared.raw_channels_samples(self.input_layout)
        array = torch_input_array(channels_samples, self.config).astype(np.float32)
        session = self._load_session(ort, artifact_path)
        input_name = str(self.config.get("input_name") or session.get_inputs()[0].name)
        outputs = session.run(None, {input_name: array})
        probability = tensor_probability(np.asarray(outputs[0]))
        label = "target" if probability >= float(self.config.get("decision_probability", 0.5)) else "non_target"
        return ModelPrediction(
            label=label,
            score=probability,
            probability=probability,
            features={"target_probability": probability},
            model_kind=self.kind,
            model_version=self.model_version,
        )

    def _load_session(self, ort: Any, artifact_path: Path) -> Any:
        if self._artifact_cache is None:
            self._artifact_cache = ort.InferenceSession(str(artifact_path), providers=["CPUExecutionProvider"])
        return self._artifact_cache


def make_model_adapter(kind: str, config: dict[str, Any] | None = None) -> BaseModelAdapter:
    raw_kind = str(kind or "default").lower()
    if raw_kind in {"default", "erp_peak_baseline"}:
        return ERPPeakBaselineAdapter(config)
    if raw_kind == "sklearn_xdawn_lda":
        return SklearnXdawnLdaAdapter(config)
    normalized = resolve_model_kind(raw_kind)
    if normalized == "band_power_threshold":
        return BandPowerThresholdModel(config)
    if normalized == "sklearn_flatten_lda":
        return SklearnFlattenLdaAdapter(config)
    if normalized == "erp_roi_logreg":
        return ErpRoiLogisticRegressionAdapter(config)
    if normalized == "pyriemann_erp_cov":
        return PyriemannErpCovAdapter(config)
    if normalized == "torch_eegnet":
        return TorchEEGNetAdapter(config)
    if normalized == "torch_shallowconvnet":
        return TorchShallowConvNetAdapter(config)
    if normalized == "foundation_bendr":
        return FoundationBendrAdapter(config)
    if normalized == "foundation_labram":
        return FoundationLabramAdapter(config)
    if normalized == "sequence_external":
        return SequenceExternalAdapter(config)
    if normalized == "onnx_p300":
        return OnnxP300Adapter(config)
    raise NotImplementedError(f"realtime model adapter '{kind}' is not implemented")


def train_epoch_model(
    kind: str,
    epochs_npz: str | Path | list[str | Path] | tuple[str | Path, ...],
    output_path: str | Path,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Train an epoch classifier and write a bare artifact or model bundle."""
    cfg = dict(config or {})
    spec = get_model_spec(kind)
    if not spec.trainable or spec.train_kind is None:
        raise NotImplementedError(f"training for model '{kind}' is not implemented")
    normalized = spec.train_kind
    cfg.setdefault("kind", normalized)
    cfg.setdefault("family", spec.family)
    data, source_paths = load_epoch_dataset(epochs_npz)
    x = np.asarray(data["X"], dtype=float)
    training_target = build_training_target(data, cfg)
    y = np.asarray(training_target.y, dtype=int)
    trials = np.asarray(npz_value(data, "trials", np.arange(y.size) + 1), dtype=int)
    valid = np.asarray(training_target.eligible, dtype=bool) & (y >= 0) & (trials >= 1)
    if not valid.any():
        raise ValueError(f"epochs.npz does not contain eligible non-practice training labels for {training_target.name}")
    x = x[valid]
    y = y[valid]
    trials = trials[valid]
    eligible_training_epochs = int(x.shape[0])
    quality_config = dict(cfg.get("quality_gate", {}))
    quality_valid = np.asarray([assess_epoch_quality(epoch.T, quality_config).valid for epoch in x], dtype=bool)
    x = x[quality_valid]
    y = y[quality_valid]
    trials = trials[quality_valid]
    if x.shape[0] == 0:
        raise ValueError("all training epochs were rejected by the quality gate")
    if len(set(y.tolist())) < 2:
        raise ValueError(f"training requires both classes for target {training_target.name}")
    contract = training_contract(data, cfg)
    times = np.asarray(npz_value(data, "times", []), dtype=float)
    source_channel_names = [str(value) for value in contract.get("source_channel_names", contract["channel_names"])]
    corrected = np.stack(
        [
            prepare_classifier_epoch(
                epoch,
                float(contract["sample_rate_hz"]),
                source_channel_names,
                {"relative_times": times.tolist(), "epoch_window_seconds": contract["epoch_window_seconds"]},
                contract,
            )[0]
            for epoch in x
        ],
        axis=0,
    )
    blocked = blocked_validation_metrics(normalized, corrected, y, trials, data, cfg, contract)
    final_config = dict(cfg)
    if normalized == "torch_eegnet" and blocked.get("best_epoch"):
        final_config["_fixed_epoch_count"] = int(blocked["best_epoch"])
    if normalized == "erp_roi_logreg":
        artifact = train_erp_roi_logreg(corrected, y, data, final_config, contract)
        extension = ".joblib"
        artifact_format = "joblib"
    elif normalized == "sklearn_flatten_lda":
        artifact = train_sklearn_flatten_lda(corrected, y, data, final_config, contract)
        extension = ".joblib"
        artifact_format = "joblib"
    elif normalized == "pyriemann_erp_cov":
        artifact = train_pyriemann_erp_cov(corrected, y, data, final_config, contract)
        extension = ".joblib"
        artifact_format = "joblib"
    elif normalized == "torch_eegnet":
        artifact = train_torch_eegnet(corrected, y, data, final_config, contract)
        extension = ".pt"
        artifact_format = "torchscript"
    else:
        raise NotImplementedError(f"training for model '{kind}' is not implemented")

    probabilities = training_probabilities(normalized, artifact, corrected, data, cfg, contract)
    calibration_cfg = dict(cfg.get("calibration", {}))
    positive_label = positive_label_for_target(training_target.name)
    threshold_calibration = _training_threshold_calibration(
        y,
        probabilities,
        blocked,
        metric=str(calibration_cfg.get("threshold_metric", "balanced_accuracy")),
        max_candidates=int(calibration_cfg.get("threshold_max_candidates", 512)),
        positive_label=positive_label,
    )
    selected_threshold = float(threshold_calibration.get("selected_threshold", 0.5))
    if isinstance(artifact, dict):
        artifact["decision_probability"] = selected_threshold
        artifact["target"] = training_target.name
        artifact["target_spec"] = training_target.metadata
        artifact["calibration"] = threshold_calibration
        artifact["model_spec"] = spec.payload()

    output_target = Path(output_path).expanduser().resolve()
    bundle_output = output_target.suffix == ""
    if bundle_output:
        output_target.mkdir(parents=True, exist_ok=True)
        artifact_target = output_target / f"model{extension}"
    else:
        artifact_target = output_target
        artifact_target.parent.mkdir(parents=True, exist_ok=True)
    if artifact_format == "joblib":
        try:
            import joblib
        except Exception as exc:
            raise RuntimeError(f"training {kind} requires joblib") from exc
        joblib.dump(artifact, artifact_target)
    else:
        artifact["scripted_model"].save(str(artifact_target))
    metrics = binary_classification_metrics(
        y,
        probabilities,
        threshold=selected_threshold,
        positive_label=positive_label,
    )
    metrics["default_threshold_metrics"] = binary_classification_metrics(
        y,
        probabilities,
        threshold=0.5,
        positive_label=positive_label,
    )
    metrics["permutation_p_value"] = prediction_permutation_p_value(
        y,
        probabilities,
        threshold=selected_threshold,
        permutations=int(cfg.get("permutations", 100)),
        seed=int(cfg.get("seed", 42)),
        positive_label=positive_label,
    )
    warning_source = blocked.get("metrics", metrics) if isinstance(blocked, dict) else metrics
    metrics.update(
        {
            "coverage": int(x.shape[0]) / max(1, eligible_training_epochs),
            "blocked_validation": blocked,
            "target": training_target.name,
            "target_spec": training_target.metadata,
            "evaluation_level": "training_fit",
            "threshold_source": threshold_calibration.get("source"),
            "threshold_calibration": threshold_calibration,
            "selected_threshold_metrics": threshold_calibration.get("selected_metrics"),
            "warnings": performance_warnings(warning_source),
        }
    )
    if normalized == "torch_eegnet":
        metrics["training_history"] = artifact.get("training_history", [])
        metrics["best_epoch"] = artifact.get("best_epoch")
    bundle_manifest = None
    if bundle_output:
        bundle_manifest = write_model_bundle(
            output_target,
            kind=normalized,
            artifact_path=artifact_target,
            artifact_format=artifact_format,
            contract=contract,
            metrics=metrics,
            training_source={
                **training_source_provenance(data, source_paths),
                "training_epochs": int(x.shape[0]),
                "eligible_training_epochs": eligible_training_epochs,
                "quality_rejected_epochs": eligible_training_epochs - int(x.shape[0]),
            },
            extra={
                "model_version": str(cfg.get("model_version", "trained")),
                "model_spec": spec.payload(),
                "model_family": spec.family,
                "target": training_target.name,
                "target_spec": training_target.metadata,
                "label_mapping": training_target.label_mapping,
                "calibration": threshold_calibration,
            },
        )
    return {
        "status": "ok",
        "model_kind": normalized,
        "model_family": spec.family,
        "target": training_target.name,
        "artifact_path": str(artifact_target),
        "bundle_path": str(output_target) if bundle_output else None,
        "bundle_hash": None if bundle_manifest is None else bundle_manifest["bundle_hash"],
        "training_epochs": int(x.shape[0]),
        "classes": sorted(int(value) for value in set(y.tolist())),
        "channel_names": [str(value) for value in contract["channel_names"]],
        "sample_rate_hz": float(contract["sample_rate_hz"]),
        "epoch_window_seconds": contract["epoch_window_seconds"],
        "label_mapping": training_target.label_mapping,
        "calibration": threshold_calibration,
        "metrics": metrics,
    }


def train_erp_roi_logreg(
    x: np.ndarray,
    y: np.ndarray,
    data: Any,
    config: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:
        raise RuntimeError("erp_roi_logreg training requires scikit-learn") from exc
    times = np.asarray(npz_value(data, "times", []), dtype=float)
    roi_config = {**DEFAULT_ROI_CONFIG, **dict(config.get("roi_features", {}))}
    feature_rows = [
        extract_erp_roi_features(epoch, times, list(contract["channel_names"]), roi_config)
        for epoch in x
    ]
    vectors = [feature_vector(row) for row in feature_rows]
    feature_names = vectors[0][1]
    matrix = np.stack([vector for vector, _ in vectors], axis=0)
    pipeline = Pipeline(
        [
            ("scale", StandardScaler()),
            (
                "classifier",
                LogisticRegression(
                    penalty="l2",
                    class_weight="balanced",
                    solver="liblinear",
                    max_iter=int(config.get("max_iter", 1000)),
                    random_state=int(config.get("seed", 42)),
                ),
            ),
        ]
    )
    pipeline.fit(matrix, y)
    return {
        "kind": "erp_roi_logreg",
        "model_version": str(config.get("model_version", "trained")),
        "pipeline": pipeline,
        "feature_names": feature_names,
        "roi_config": roi_config,
        "contract": contract,
    }


def train_sklearn_flatten_lda(
    x: np.ndarray,
    y: np.ndarray,
    data: Any,
    config: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    try:
        from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:
        raise RuntimeError("sklearn_flatten_lda training requires scikit-learn") from exc

    pipeline = Pipeline(
        [
            ("scale", StandardScaler()),
            ("lda", LinearDiscriminantAnalysis()),
        ]
    )
    pipeline.fit(x.reshape(x.shape[0], -1), y)
    return {
        "kind": "sklearn_flatten_lda",
        "model_version": str(config.get("model_version", "trained")),
        "pipeline": pipeline,
        "input_layout": "channels_x_samples",
        "channel_names": [str(value) for value in npz_value(data, "channel_names", [])],
        "contract": contract,
    }


def train_sklearn_xdawn_lda(x: np.ndarray, y: np.ndarray, data: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Compatibility wrapper for callers of the old misleading name."""
    return train_sklearn_flatten_lda(x, y, data, config, training_contract(data, config))


def train_pyriemann_erp_cov(
    x: np.ndarray,
    y: np.ndarray,
    data: Any,
    config: dict[str, Any],
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        from pyriemann.estimation import XdawnCovariances
        from pyriemann.tangentspace import TangentSpace
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
    except Exception as exc:
        raise RuntimeError("pyriemann_erp_cov training requires pyriemann and scikit-learn") from exc

    pipeline = Pipeline(
        [
            ("xdawn_cov", XdawnCovariances(nfilter=int(config.get("nfilter", 4)))),
            ("tangent", TangentSpace()),
            (
                "classifier",
                LogisticRegression(
                    max_iter=int(config.get("max_iter", 1000)),
                    class_weight=config.get("class_weight", "balanced"),
                    random_state=int(config.get("seed", 42)),
                ),
            ),
        ]
    )
    pipeline.fit(x, y)
    return {
        "kind": "pyriemann_erp_cov",
        "model_version": str(config.get("model_version", "trained")),
        "pipeline": pipeline,
        "input_layout": "channels_x_samples",
        "channel_names": [str(value) for value in npz_value(data, "channel_names", [])],
        "contract": contract or training_contract(data, config),
    }


def train_torch_eegnet(
    x: np.ndarray,
    y: np.ndarray,
    data: Any,
    config: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    try:
        import torch
        from torch import nn
    except Exception as exc:
        raise RuntimeError("torch_eegnet training requires torch") from exc

    seed = int(config.get("seed", 42))
    torch.manual_seed(seed)
    np.random.seed(seed)
    channels, samples = int(x.shape[1]), int(x.shape[2])
    f1 = int(config.get("F1", 8))
    depth = int(config.get("D", 2))
    f2 = int(config.get("F2", 16))
    kernel = int(config.get("temporal_kernel", 64))
    dropout = float(config.get("dropout", 0.5))

    class EEGNet(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.temporal = nn.Sequential(
                nn.Conv2d(1, f1, (1, kernel), padding=(0, kernel // 2), bias=False),
                nn.BatchNorm2d(f1),
            )
            self.spatial = nn.Sequential(
                nn.Conv2d(f1, f1 * depth, (channels, 1), groups=f1, bias=False),
                nn.BatchNorm2d(f1 * depth),
                nn.ELU(),
                nn.AvgPool2d((1, 4)),
                nn.Dropout(dropout),
            )
            self.separable = nn.Sequential(
                nn.Conv2d(f1 * depth, f1 * depth, (1, 16), padding=(0, 8), groups=f1 * depth, bias=False),
                nn.Conv2d(f1 * depth, f2, (1, 1), bias=False),
                nn.BatchNorm2d(f2),
                nn.ELU(),
                nn.AvgPool2d((1, 8)),
                nn.Dropout(dropout),
                nn.AdaptiveAvgPool2d((1, 8)),
            )
            self.classifier = nn.Linear(f2 * 8, 2)

        def forward(self, inputs: Any) -> Any:
            values = self.temporal(inputs)
            values = self.spatial(values)
            values = self.separable(values)
            return self.classifier(values.flatten(1))

    device = torch.device("cpu")
    values = torch.as_tensor(x[:, np.newaxis, :, :], dtype=torch.float32, device=device)
    labels = torch.as_tensor(y, dtype=torch.long, device=device)
    validation_indices = np.arange(max(1, int(round(len(y) * 0.8))), len(y))
    if validation_indices.size == 0 or len(set(y[validation_indices].tolist())) < 2:
        validation_indices = np.arange(0, len(y), max(2, len(y) // max(2, min(5, len(y)))))
    train_indices = np.asarray([index for index in range(len(y)) if index not in set(validation_indices.tolist())], dtype=int)
    if train_indices.size == 0:
        train_indices = np.arange(len(y))
    class_counts = np.bincount(y, minlength=2).astype(float)
    weights = torch.as_tensor(len(y) / np.maximum(2.0 * class_counts, 1.0), dtype=torch.float32, device=device)
    model = EEGNet().to(device)
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=float(config.get("learning_rate", 1e-3)),
        weight_decay=float(config.get("weight_decay", 1e-4)),
    )
    criterion = nn.CrossEntropyLoss(weight=weights)
    batch_size = int(config.get("batch_size", 32))
    max_epochs = int(config.get("max_epochs", 200))
    patience = int(config.get("patience", 20))
    history: list[dict[str, float]] = []
    fixed_epoch_count = config.get("_fixed_epoch_count")
    if fixed_epoch_count is None:
        best_loss = float("inf")
        epochs_without_improvement = 0
        rng = np.random.default_rng(seed)
        for epoch_index in range(max_epochs):
            model.train()
            shuffled = rng.permutation(train_indices)
            losses: list[float] = []
            for start in range(0, len(shuffled), batch_size):
                indices = shuffled[start : start + batch_size]
                optimizer.zero_grad()
                loss = criterion(model(values[indices]), labels[indices])
                loss.backward()
                optimizer.step()
                losses.append(float(loss.detach().cpu()))
            model.eval()
            with torch.no_grad():
                validation_loss = float(criterion(model(values[validation_indices]), labels[validation_indices]).cpu())
            history.append(
                {
                    "epoch": float(epoch_index + 1),
                    "training_loss": float(np.mean(losses)) if losses else 0.0,
                    "validation_loss": validation_loss,
                }
            )
            if validation_loss < best_loss - 1e-6:
                best_loss = validation_loss
                epochs_without_improvement = 0
            else:
                epochs_without_improvement += 1
                if epochs_without_improvement >= patience:
                    break
        best_epoch = int(np.argmin([row["validation_loss"] for row in history]) + 1)
    else:
        best_epoch = max(1, int(fixed_epoch_count))
    torch.manual_seed(seed)
    final_model = EEGNet().to(device)
    final_optimizer = torch.optim.Adam(
        final_model.parameters(),
        lr=float(config.get("learning_rate", 1e-3)),
        weight_decay=float(config.get("weight_decay", 1e-4)),
    )
    all_indices = np.arange(len(y))
    final_rng = np.random.default_rng(seed)
    for _ in range(best_epoch):
        final_model.train()
        shuffled = final_rng.permutation(all_indices)
        for start in range(0, len(shuffled), batch_size):
            indices = shuffled[start : start + batch_size]
            final_optimizer.zero_grad()
            loss = criterion(final_model(values[indices]), labels[indices])
            loss.backward()
            final_optimizer.step()
    final_model.eval()
    scripted = torch.jit.trace(final_model, torch.zeros((1, 1, channels, samples), dtype=torch.float32))
    return {
        "kind": "torch_eegnet",
        "model_version": str(config.get("model_version", "trained")),
        "scripted_model": scripted,
        "contract": contract,
        "training_history": history,
        "best_epoch": best_epoch,
    }


def training_contract(data: Any, config: dict[str, Any]) -> dict[str, Any]:
    times = np.asarray(npz_value(data, "times", []), dtype=float)
    sample_rate = float(np.asarray(npz_value(data, "sample_rate_hz", [0.0])).ravel()[0])
    if sample_rate <= 0:
        raise ValueError("epochs.npz does not contain a valid sample_rate_hz")
    if times.size == 0:
        raise ValueError("epochs.npz does not contain relative epoch times")
    source_channel_names = [str(value) for value in npz_value(data, "channel_names", [])]
    spec = get_model_spec(config.get("kind", "erp_roi_logreg"))
    input_contract = normalize_input_contract(config, fallback_channel_names=source_channel_names)
    validate_supported_resampling(input_contract)
    selected_channels = [name for name in input_contract["channel_order"] if name in source_channel_names]
    if not selected_channels:
        selected_channels = source_channel_names
    preprocessing = {
        "kind": "as_recorded_microvolts_then_baseline_correction",
        "resampling": input_contract.get("resampling", "none"),
        "resampling_supported": False,
    }
    return {
        "input_layout": "channels_x_samples",
        "input_units": str(input_contract.get("input_units", "microvolts")),
        "source_channel_names": source_channel_names,
        "channel_names": selected_channels,
        "channel_order": selected_channels,
        "required_channels": [str(value) for value in input_contract.get("required_channels", selected_channels)],
        "optional_channels": [str(value) for value in input_contract.get("optional_channels", [])],
        "channel_groups": dict(input_contract.get("channel_groups", {})),
        "missing_channel_policy": str(input_contract.get("missing_channel_policy", "error")),
        "sample_rate_hz": sample_rate,
        "sample_count": int(times.size),
        "epoch_window_seconds": [float(times[0]), float(times[-1])],
        "epoch_duration_seconds": float(times[-1] - times[0]) if times.size > 1 else 0.0,
        "baseline_seconds": [float(value) for value in config.get("baseline_seconds", [-0.2, 0.0])],
        "preprocessing": preprocessing,
        "preprocessing_hash": contract_hash(preprocessing),
        "tensor_layout": str(input_contract.get("tensor_layout", "batch_1_channels_samples")),
        "model_family": spec.family,
        "target": str(config.get("target", "condition")),
        "input_contract_version": 1,
    }


def training_probabilities(
    kind: str,
    artifact: dict[str, Any],
    x: np.ndarray,
    data: Any,
    config: dict[str, Any],
    contract: dict[str, Any],
) -> np.ndarray:
    if kind == "erp_roi_logreg":
        times = np.asarray(npz_value(data, "times", []), dtype=float)
        rows = [
            feature_vector(
                extract_erp_roi_features(epoch, times, list(contract["channel_names"]), artifact["roi_config"]),
                artifact["feature_names"],
            )[0]
            for epoch in x
        ]
        matrix = np.stack(rows, axis=0)
        return estimator_probabilities(artifact["pipeline"], matrix)
    if kind == "sklearn_flatten_lda":
        return estimator_probabilities(artifact["pipeline"], x.reshape(x.shape[0], -1))
    if kind == "pyriemann_erp_cov":
        return estimator_probabilities(artifact["pipeline"], x)
    if kind == "torch_eegnet":
        import torch

        with torch.no_grad():
            logits = artifact["scripted_model"](torch.as_tensor(x[:, np.newaxis, :, :], dtype=torch.float32))
            return torch.softmax(logits, dim=1)[:, 1].cpu().numpy().astype(float)
    raise NotImplementedError(kind)


def blocked_validation_metrics(
    kind: str,
    x: np.ndarray,
    y: np.ndarray,
    trials: np.ndarray,
    data: Any,
    config: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    block_ids = np.maximum(0, (np.asarray(trials, dtype=int) - 1) // 20)
    unique_blocks = sorted(set(block_ids.tolist()))
    fold_count = min(5, len(unique_blocks))
    if fold_count < 2:
        return {"status": "not_run", "reason": "fewer_than_two_sequential_trial_blocks"}
    folds = _stratified_block_folds(block_ids, y, unique_blocks, fold_count)
    probabilities = np.full(y.shape, np.nan, dtype=float)
    best_epochs: list[int] = []
    for validation_blocks in folds:
        validation = np.isin(block_ids, validation_blocks)
        training = ~validation
        if len(set(y[training].tolist())) < 2 or len(set(y[validation].tolist())) < 2:
            continue
        if kind == "erp_roi_logreg":
            artifact = train_erp_roi_logreg(x[training], y[training], data, config, contract)
        elif kind == "sklearn_flatten_lda":
            artifact = train_sklearn_flatten_lda(x[training], y[training], data, config, contract)
        elif kind == "pyriemann_erp_cov":
            artifact = train_pyriemann_erp_cov(x[training], y[training], data, config, contract)
        elif kind == "torch_eegnet":
            artifact = train_torch_eegnet(x[training], y[training], data, config, contract)
            best_epochs.append(int(artifact["best_epoch"]))
        else:
            continue
        probabilities[validation] = training_probabilities(kind, artifact, x[validation], data, config, contract)
    mask = np.isfinite(probabilities)
    if not mask.any():
        return {"status": "not_run", "reason": "blocked_folds_did_not_contain_both_classes"}
    calibration_cfg = dict(config.get("calibration", {}))
    positive_label = positive_label_for_target(str(config.get("target", "condition")))
    threshold_calibration = select_binary_threshold(
        y[mask],
        probabilities[mask],
        metric=str(calibration_cfg.get("threshold_metric", "balanced_accuracy")),
        max_candidates=int(calibration_cfg.get("threshold_max_candidates", 512)),
        positive_label=positive_label,
    )
    selected_threshold = float(threshold_calibration.get("selected_threshold", 0.5))
    metrics = binary_classification_metrics(
        y[mask],
        probabilities[mask],
        threshold=selected_threshold,
        positive_label=positive_label,
    )
    metrics["default_threshold_metrics"] = binary_classification_metrics(
        y[mask],
        probabilities[mask],
        threshold=0.5,
        positive_label=positive_label,
    )
    metrics["coverage"] = float(np.mean(mask))
    metrics["evaluation_level"] = "calibration_blocked_validation"
    metrics["threshold_source"] = "blocked_validation"
    metrics["permutation_p_value"] = prediction_permutation_p_value(
        y[mask],
        probabilities[mask],
        threshold=selected_threshold,
        permutations=int(config.get("permutations", 100)),
        seed=int(config.get("seed", 42)),
        positive_label=positive_label,
    )
    result = {
        "status": "ok",
        "fold_count": fold_count,
        "metrics": metrics,
        "threshold_calibration": {**threshold_calibration, "source": "blocked_validation"},
    }
    if best_epochs:
        result["fold_best_epochs"] = best_epochs
        result["best_epoch"] = max(1, int(round(float(np.median(best_epochs)))))
    return result


def _stratified_block_folds(
    block_ids: np.ndarray,
    labels: np.ndarray,
    unique_blocks: list[int],
    fold_count: int,
) -> list[list[int]]:
    """Assign intact sequential 20-trial blocks while balancing label counts."""
    folds: list[list[int]] = [[] for _ in range(fold_count)]
    counts = np.zeros((fold_count, 2), dtype=int)
    block_counts = {
        block: np.bincount(labels[block_ids == block], minlength=2).astype(int)
        for block in unique_blocks
    }
    ordered = sorted(unique_blocks, key=lambda block: (-int(np.sum(block_counts[block])), -abs(int(np.diff(block_counts[block])[0])), block))
    for block in ordered:
        values = block_counts[block]
        scores = [
            (int(np.sum(counts[index])), int(abs((counts[index][0] + values[0]) - (counts[index][1] + values[1]))), index)
            for index in range(fold_count)
        ]
        target = min(scores)[2]
        folds[target].append(block)
        counts[target] += values
    return folds


def binary_classification_metrics(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    *,
    threshold: float = 0.5,
    positive_label: str = "no_go",
) -> dict[str, Any]:
    return binary_metrics_at_threshold(
        y_true,
        probabilities,
        threshold,
        positive_label=positive_label,
    )


def performance_warnings(metrics: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if float(metrics.get("balanced_accuracy", 0.0)) < 0.60:
        warnings.append("balanced_accuracy_below_0.60")
    permutation_p = metrics.get("permutation_p_value")
    if permutation_p is not None and float(permutation_p) >= 0.05:
        warnings.append("permutation_p_not_below_0.05")
    return warnings


def positive_label_for_target(target: str) -> str:
    return "attention_lapse" if str(target).startswith("attention_lapse") else "no_go"


def _training_threshold_calibration(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    blocked: dict[str, Any],
    *,
    metric: str,
    max_candidates: int,
    positive_label: str,
) -> dict[str, Any]:
    blocked_calibration = dict(blocked.get("threshold_calibration") or {})
    if blocked.get("status") == "ok" and blocked_calibration.get("status") == "ok":
        return {**blocked_calibration, "source": "blocked_validation"}
    calibration = select_binary_threshold(
        y_true,
        probabilities,
        metric=metric,
        max_candidates=max_candidates,
        positive_label=positive_label,
    )
    return {
        **calibration,
        "source": "training_fit",
        "fallback_reason": blocked.get("reason", "blocked_validation_unavailable"),
    }


def prediction_permutation_p_value(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    *,
    threshold: float = 0.5,
    permutations: int = 100,
    seed: int = 42,
    positive_label: str = "no_go",
) -> float | None:
    if permutations < 1 or len(set(np.asarray(y_true, dtype=int).tolist())) < 2:
        return None
    observed = float(
        binary_classification_metrics(
            y_true,
            probabilities,
            threshold=threshold,
            positive_label=positive_label,
        )["balanced_accuracy"]
    )
    rng = np.random.default_rng(seed)
    null = [
        float(
            binary_classification_metrics(
                rng.permutation(y_true),
                probabilities,
                threshold=threshold,
                positive_label=positive_label,
            )["balanced_accuracy"]
        )
        for _ in range(permutations)
    ]
    return float((1 + sum(value >= observed for value in null)) / (permutations + 1))


def estimator_probabilities(estimator: Any, x: np.ndarray) -> np.ndarray:
    if hasattr(estimator, "predict_proba"):
        proba = np.asarray(estimator.predict_proba(x), dtype=float)
        classes = list(getattr(estimator, "classes_", []))
        index = classes.index(1) if 1 in classes else min(1, proba.shape[1] - 1)
        return proba[:, index]
    return np.asarray(estimator.predict(x), dtype=float).reshape(-1)


def band_power_features(data: np.ndarray, sample_rate_hz: float, bands: dict[str, list[float]]) -> dict[str, float]:
    from scipy import signal

    samples_channels = np.asarray(data, dtype=float)
    if samples_channels.size == 0:
        return {name: 0.0 for name in bands}
    freqs, psd = signal.welch(samples_channels, fs=sample_rate_hz, axis=0, nperseg=min(samples_channels.shape[0], 256))
    mean_psd = psd.mean(axis=1)
    features: dict[str, float] = {}
    for name, bounds in bands.items():
        low, high = float(bounds[0]), float(bounds[1])
        mask = (freqs >= low) & (freqs <= high)
        if not mask.any():
            features[name] = 0.0
            continue
        band_freqs = freqs[mask]
        band_psd = mean_psd[mask]
        if band_psd.size == 1:
            resolution = float(freqs[1] - freqs[0]) if freqs.size > 1 else 1.0
            features[name] = float(band_psd[0] * resolution)
        else:
            features[name] = float(np.trapezoid(band_psd, band_freqs))
    return features


def epoch_to_channels_samples(epoch: np.ndarray, channel_names: list[str], input_layout: str = "auto") -> np.ndarray:
    data = np.asarray(epoch, dtype=float)
    if data.ndim != 2:
        raise ValueError("epoch must be a 2D array")
    layout = input_layout.lower()
    channel_count = len(channel_names)
    if layout == "channels_x_samples":
        return data
    if layout == "samples_x_channels":
        return data.T
    if channel_count and data.shape[0] == channel_count:
        return data
    if channel_count and data.shape[1] == channel_count:
        return data.T
    return data.T if data.shape[0] > data.shape[1] else data


def epoch_to_samples_channels(epoch: np.ndarray, channel_names: list[str], input_layout: str = "auto") -> np.ndarray:
    return epoch_to_channels_samples(epoch, channel_names, input_layout).T


def relative_times(metadata: dict[str, Any], sample_count: int, sample_rate_hz: float) -> np.ndarray:
    if "relative_times" in metadata:
        values = np.asarray(metadata["relative_times"], dtype=float)
        if values.size == sample_count:
            return values
    window = metadata.get("epoch_window_seconds") or metadata.get("epoch_window") or [-0.2, 0.8]
    tmin = float(window[0])
    return np.arange(sample_count, dtype=float) / sample_rate_hz + tmin


def resolve_roi_indices(channel_names: list[str], roi_channels: list[str], channel_count: int) -> list[int]:
    selected = [channel_names.index(name) for name in roi_channels if name in channel_names]
    if selected:
        return selected
    return list(range(channel_count))


def logistic_probability(value: float, threshold: float, slope: float) -> float:
    z = max(-60.0, min(60.0, (value - threshold) / slope))
    return float(1.0 / (1.0 + math.exp(-z)))


def require_artifact_path(config: dict[str, Any], kind: str) -> Path:
    bundle = configured_bundle(config)
    if bundle is not None:
        return Path(bundle["artifact_path"])
    artifact = config.get("artifact_path")
    if not artifact:
        raise RuntimeError(f"{kind} requires model.artifact_path for inference")
    target = Path(str(artifact)).expanduser().resolve()
    if not target.exists():
        raise RuntimeError(f"{kind} artifact does not exist: {target}")
    return target


def load_joblib_artifact(config: dict[str, Any], kind: str) -> Any:
    artifact_path = require_artifact_path(config, kind)
    try:
        import joblib
    except Exception as exc:
        raise RuntimeError(f"{kind} requires joblib to load {artifact_path}") from exc
    artifact = joblib.load(artifact_path)
    bundle = configured_bundle(config)
    if bundle is not None and isinstance(artifact, dict):
        artifact = {
            **artifact,
            "bundle_hash": bundle.get("bundle_hash"),
            "bundle_path": bundle.get("bundle_dir"),
            "contract": dict(bundle.get("contract") or artifact.get("contract") or {}),
            "calibration": bundle.get("calibration") or artifact.get("calibration"),
            "target": bundle.get("target") or artifact.get("target"),
            "target_spec": bundle.get("target_spec") or artifact.get("target_spec"),
            "model_family": bundle.get("model_family") or artifact.get("model_family"),
            "model_spec": bundle.get("model_spec") or artifact.get("model_spec"),
        }
    return artifact


def configured_bundle(config: dict[str, Any]) -> dict[str, Any] | None:
    path = config.get("bundle_path")
    if not path:
        return None
    return load_model_bundle(path)


def artifact_prediction_metadata(artifact: Any) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        return {}
    payload = {
        key: artifact[key]
        for key in ("bundle_hash", "bundle_path")
        if artifact.get(key) is not None
    }
    payload.update(_model_artifact_metadata(artifact))
    return payload


def classifier_label(probability_no_go: float, config: dict[str, Any]) -> str:
    calibration = dict(config.get("calibration") or {})
    threshold = float(
        config.get(
            "decision_probability",
            calibration.get("selected_threshold", config.get("calibrated_threshold", 0.5)),
        )
    )
    target = str(config.get("target", dict(config.get("target_spec") or {}).get("target", "condition")))
    if target.startswith("attention_lapse"):
        return "attention_lapse" if probability_no_go >= threshold else "attentive"
    return "no_go" if probability_no_go >= threshold else "go"


def bundle_prediction_metadata(bundle: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "bundle_hash": bundle.get("bundle_hash"),
        "bundle_path": bundle.get("bundle_dir"),
    }
    payload.update(_model_artifact_metadata(bundle))
    return {key: value for key, value in payload.items() if value is not None}


def _model_artifact_metadata(value: dict[str, Any]) -> dict[str, Any]:
    calibration = dict(value.get("calibration") or {})
    model_spec = dict(value.get("model_spec") or {})
    payload = {
        "target": value.get("target", dict(value.get("target_spec") or {}).get("target", "condition")),
        "target_spec": value.get("target_spec"),
        "model_family": value.get("model_family", model_spec.get("family")),
        "model_spec": model_spec or None,
        "calibrated_threshold": calibration.get("selected_threshold", value.get("decision_probability")),
        "calibration_id": calibration.get("calibration_id"),
    }
    return {key: item for key, item in payload.items() if item is not None}


def prepare_artifact_epoch(
    epoch: np.ndarray,
    sample_rate_hz: float,
    channel_names: list[str],
    metadata: dict[str, Any],
    contract: dict[str, Any],
    input_layout: str,
) -> np.ndarray:
    """Apply declared preprocessing while preserving legacy bare artifacts."""
    if not contract:
        return epoch_to_channels_samples(epoch, channel_names, input_layout)
    return prepare_classifier_epoch(
        epoch,
        sample_rate_hz,
        channel_names,
        metadata,
        {**contract, "input_layout": input_layout},
    )[0]


def _prepared_epoch_key(contract: dict[str, Any], input_layout: str) -> str:
    return json.dumps(
        {"contract": dict(contract), "input_layout": str(input_layout or "auto")},
        sort_keys=True,
        default=str,
    )


def estimator_probability(estimator: Any, x: np.ndarray) -> float:
    if hasattr(estimator, "predict_proba"):
        proba = np.asarray(estimator.predict_proba(x), dtype=float)
        classes = list(getattr(estimator, "classes_", []))
        if classes and 1 in classes:
            return float(proba[0, classes.index(1)])
        if proba.shape[1] > 1:
            return float(proba[0, 1])
        return float(proba[0, 0])
    prediction = estimator.predict(x)
    return float(np.asarray(prediction).ravel()[0])


def torch_input_array(channels_samples: np.ndarray, config: dict[str, Any]) -> np.ndarray:
    layout = str(config.get("tensor_layout", "batch_1_channels_samples")).lower()
    data = channels_samples.astype(np.float32)
    if layout in {"batch_channels_samples", "b_c_t"}:
        return data[np.newaxis, :, :]
    if layout in {"batch_samples_channels", "b_t_c"}:
        return data.T[np.newaxis, :, :]
    return data[np.newaxis, np.newaxis, :, :]


def tensor_probability(output: np.ndarray) -> float:
    values = np.asarray(output, dtype=float).reshape(-1)
    if values.size == 0:
        return 0.0
    if values.size == 1:
        return logistic_probability(float(values[0]), 0.0, 1.0)
    shifted = values - np.max(values)
    exp_values = np.exp(shifted)
    probabilities = exp_values / np.sum(exp_values)
    return float(probabilities[1] if probabilities.size > 1 else probabilities[0])


def npz_value(data: Any, key: str, default: Any) -> Any:
    return data[key] if key in data.files else default


class EpochDataset:
    """In-memory npz-like container used for multi-session training."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = dict(values)
        self.files = tuple(self._values)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]


def load_epoch_dataset(
    epochs_npz: str | Path | list[str | Path] | tuple[str | Path, ...],
) -> tuple[Any, list[Path]]:
    paths = _epoch_dataset_paths(epochs_npz)
    if len(paths) == 1:
        return np.load(paths[0], allow_pickle=True), paths
    loaded = []
    for path in paths:
        with np.load(path, allow_pickle=True) as data:
            loaded.append({key: np.asarray(data[key]) for key in data.files})
    reference = loaded[0]
    sample_counts = [_epoch_sample_count(values, path) for values, path in zip(loaded, paths)]
    for values in loaded[1:]:
        for key in ("times", "channel_names", "sample_rate_hz"):
            if key in reference and key in values and not np.array_equal(reference[key], values[key]):
                raise ValueError(f"cannot combine epoch datasets with different {key}")
    combined: dict[str, Any] = {}
    sample_keys = {"X", "y", "trials", "conditions", "epoch_timestamps", "marker_timestamps", "attention_lapse_binary", "attention_lapse_score", "lapse_score"}
    for key in sample_keys:
        present = [key in values for values in loaded]
        if any(present) and not all(present):
            raise ValueError(f"cannot combine epoch datasets with partial per-epoch field {key}")
    for key in sorted(set().union(*(set(values) for values in loaded))):
        if key in {"source_session_index", "source_epoch_index"}:
            continue
        arrays = [values[key] for values in loaded if key in values]
        if key in sample_keys and len(arrays) == len(loaded):
            combined[key] = np.concatenate(arrays, axis=0)
        else:
            combined[key] = reference[key]
    combined["source_session_index"] = np.concatenate(
        [np.full(count, index, dtype=int) for index, count in enumerate(sample_counts)],
        axis=0,
    )
    combined["source_epoch_index"] = np.concatenate(
        [np.arange(count, dtype=int) for count in sample_counts],
        axis=0,
    )
    combined["source_epoch_counts"] = np.asarray(sample_counts, dtype=int)
    combined["source_epoch_path_sha256"] = np.asarray([_string_sha256(str(path)) for path in paths], dtype=object)
    return EpochDataset(combined), paths


def _epoch_dataset_paths(
    epochs_npz: str | Path | list[str | Path] | tuple[str | Path, ...],
) -> list[Path]:
    values = epochs_npz if isinstance(epochs_npz, (list, tuple)) else [epochs_npz]
    paths = [Path(value).expanduser().resolve() for value in values]
    if not paths:
        raise ValueError("at least one epochs.npz path is required")
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate epochs.npz inputs are not allowed for multi-session training")
    for path in paths:
        if not path.exists():
            raise ValueError(f"epochs.npz does not exist: {path}")
    return paths


def _epoch_sample_count(values: dict[str, Any], path: Path) -> int:
    for key in ("X", "y", "trials", "conditions", "epoch_timestamps", "marker_timestamps"):
        if key in values:
            return int(np.asarray(values[key]).shape[0])
    raise ValueError(f"epochs.npz does not contain per-epoch arrays: {path}")


def training_source_provenance(data: Any, source_paths: list[Path]) -> dict[str, Any]:
    """Return reproducible, path-redacted provenance for bundle manifests."""
    source_counts = np.asarray(npz_value(data, "source_epoch_counts", []), dtype=int)
    if source_counts.size == 0:
        x = np.asarray(data["X"]) if "X" in data.files else np.asarray([])
        source_counts = np.asarray([int(x.shape[0])], dtype=int)
    path_hashes = [_string_sha256(str(path)) for path in source_paths]
    return {
        "epochs_npz_count": len(source_paths),
        "epochs_npz_path_sha256": path_hashes,
        "epochs_npz_sha256": [file_sha256(path) for path in source_paths],
        "source_epoch_counts": [int(value) for value in source_counts.tolist()],
        "path_values_redacted": True,
        "source_index_contract": "per-epoch source_session_index maps to session_dirs order during target generation",
    }


def _string_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
