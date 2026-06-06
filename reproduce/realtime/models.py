"""Realtime model adapters for window and marker-locked EEG inference."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np


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
        channels_samples = epoch_to_channels_samples(epoch, channel_names, self.input_layout)
        if channels_samples.size == 0:
            return ModelPrediction(
                label="no_p300",
                score=0.0,
                probability=0.0,
                features={"p300_amplitude_uv": 0.0, "p300_latency_ms": 0.0},
                model_kind=self.kind,
                model_version=self.model_version,
            )

        times = relative_times(metadata, channels_samples.shape[1], sample_rate_hz)
        data_uv = channels_samples.astype(float)
        if self.input_units in {"v", "volt", "volts"}:
            data_uv = data_uv * 1e6

        roi_indices = resolve_roi_indices(channel_names, self.roi_channels, channels_samples.shape[0])
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
            metadata={"roi_channels": [channel_names[index] for index in roi_indices if index < len(channel_names)]},
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
        samples_channels = epoch_to_samples_channels(epoch, channel_names, self.input_layout)
        features = band_power_features(samples_channels, sample_rate_hz, self.bands)
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
    """Classical ERP baseline backed by a joblib sklearn artifact."""

    kind = "sklearn_xdawn_lda"

    def predict_epoch(
        self,
        epoch: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
        metadata: dict[str, Any],
    ) -> ModelPrediction:
        artifact = self._load_artifact()
        channels_samples = epoch_to_channels_samples(epoch, channel_names, self.input_layout)
        estimator = artifact.get("pipeline") if isinstance(artifact, dict) else artifact
        probability = estimator_probability(estimator, channels_samples.reshape(1, -1))
        label = "target" if probability >= float(self.config.get("decision_probability", 0.5)) else "non_target"
        return ModelPrediction(
            label=label,
            score=probability,
            probability=probability,
            features={"target_probability": probability},
            model_kind=self.kind,
            model_version=artifact.get("model_version") if isinstance(artifact, dict) else self.model_version,
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
        artifact = self._load_artifact()
        channels_samples = epoch_to_channels_samples(epoch, channel_names, self.input_layout)
        estimator = artifact.get("pipeline") if isinstance(artifact, dict) else artifact
        probability = estimator_probability(estimator, channels_samples[np.newaxis, :, :])
        label = "target" if probability >= float(self.config.get("decision_probability", 0.5)) else "non_target"
        return ModelPrediction(
            label=label,
            score=probability,
            probability=probability,
            features={"target_probability": probability},
            model_kind=self.kind,
            model_version=artifact.get("model_version") if isinstance(artifact, dict) else self.model_version,
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
        artifact_path = require_artifact_path(self.config, self.kind)
        try:
            import torch
        except Exception as exc:
            raise RuntimeError(f"{self.kind} requires torch for inference") from exc

        channels_samples = epoch_to_channels_samples(epoch, channel_names, self.input_layout)
        tensor = torch.as_tensor(torch_input_array(channels_samples, self.config), dtype=torch.float32)
        model = self._load_torch_model(torch, artifact_path)
        model.eval()
        with torch.no_grad():
            output = model(tensor)
        probability = tensor_probability(output.detach().cpu().numpy())
        label = "target" if probability >= float(self.config.get("decision_probability", 0.5)) else "non_target"
        return ModelPrediction(
            label=label,
            score=probability,
            probability=probability,
            features={"target_probability": probability},
            model_kind=self.kind,
            model_version=self.model_version,
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
        artifact_path = require_artifact_path(self.config, self.kind)
        try:
            import onnxruntime as ort
        except Exception as exc:
            raise RuntimeError(f"{self.kind} requires onnxruntime for inference") from exc

        channels_samples = epoch_to_channels_samples(epoch, channel_names, self.input_layout)
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
    normalized = (kind or "default").lower()
    if normalized in {"default", "erp_peak_baseline"}:
        return ERPPeakBaselineAdapter(config)
    if normalized == "band_power_threshold":
        return BandPowerThresholdModel(config)
    if normalized == "sklearn_xdawn_lda":
        return SklearnXdawnLdaAdapter(config)
    if normalized == "pyriemann_erp_cov":
        return PyriemannErpCovAdapter(config)
    if normalized == "torch_eegnet":
        return TorchEEGNetAdapter(config)
    if normalized == "torch_shallowconvnet":
        return TorchShallowConvNetAdapter(config)
    if normalized == "onnx_p300":
        return OnnxP300Adapter(config)
    raise NotImplementedError(f"realtime model adapter '{kind}' is not implemented")


def train_epoch_model(
    kind: str,
    epochs_npz: str | Path,
    output_path: str | Path,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Train a classical epoch classifier and write a joblib artifact."""
    cfg = dict(config or {})
    normalized = kind.lower()
    data = np.load(Path(epochs_npz), allow_pickle=True)
    x = np.asarray(data["X"], dtype=float)
    y = np.asarray(data["y"], dtype=int)
    valid = y >= 0
    if not valid.any():
        raise ValueError("epochs.npz does not contain any non-negative training labels")
    x = x[valid]
    y = y[valid]
    if normalized == "sklearn_xdawn_lda":
        artifact = train_sklearn_xdawn_lda(x, y, data, cfg)
    elif normalized == "pyriemann_erp_cov":
        artifact = train_pyriemann_erp_cov(x, y, data, cfg)
    else:
        raise NotImplementedError(f"training for model '{kind}' is not implemented")

    try:
        import joblib
    except Exception as exc:
        raise RuntimeError(f"training {kind} requires joblib") from exc

    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, target)
    return {
        "status": "ok",
        "model_kind": normalized,
        "artifact_path": str(target),
        "training_epochs": int(x.shape[0]),
        "classes": sorted(int(value) for value in set(y.tolist())),
        "channel_names": [str(value) for value in npz_value(data, "channel_names", [])],
        "sample_rate_hz": float(np.asarray(npz_value(data, "sample_rate_hz", [0.0])).ravel()[0]),
    }


def train_sklearn_xdawn_lda(x: np.ndarray, y: np.ndarray, data: Any, config: dict[str, Any]) -> dict[str, Any]:
    try:
        from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:
        raise RuntimeError("sklearn_xdawn_lda training requires scikit-learn") from exc

    pipeline = Pipeline(
        [
            ("scale", StandardScaler()),
            ("lda", LinearDiscriminantAnalysis()),
        ]
    )
    pipeline.fit(x.reshape(x.shape[0], -1), y)
    return {
        "kind": "sklearn_xdawn_lda",
        "model_version": str(config.get("model_version", "trained")),
        "pipeline": pipeline,
        "input_layout": "channels_x_samples",
        "channel_names": [str(value) for value in npz_value(data, "channel_names", [])],
    }


def train_pyriemann_erp_cov(x: np.ndarray, y: np.ndarray, data: Any, config: dict[str, Any]) -> dict[str, Any]:
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
            ("classifier", LogisticRegression(max_iter=int(config.get("max_iter", 1000)))),
        ]
    )
    pipeline.fit(x, y)
    return {
        "kind": "pyriemann_erp_cov",
        "model_version": str(config.get("model_version", "trained")),
        "pipeline": pipeline,
        "input_layout": "channels_x_samples",
        "channel_names": [str(value) for value in npz_value(data, "channel_names", [])],
    }


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
    return joblib.load(artifact_path)


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
