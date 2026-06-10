#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re
import textwrap

ROOT = Path(__file__).resolve().parents[1]

CLASSIFICATION = ROOT / "reproduce" / "realtime" / "classification.py"
MODELS = ROOT / "reproduce" / "realtime" / "models.py"


def backup(path: Path) -> None:
    target = path.with_suffix(path.suffix + ".bak-classifier-preproc")
    if not target.exists():
        target.write_text(path.read_text())


def replace_regex(path: Path, pattern: str, replacement: str, label: str) -> None:
    text = path.read_text()
    new, count = re.subn(pattern, replacement, text, flags=re.S)
    if count != 1:
        raise SystemExit(f"FAILED: expected exactly one replacement for {label}, got {count}")
    path.write_text(new)


def replace_text(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text()
    if old not in text:
        raise SystemExit(f"FAILED: could not find text block for {label}")
    path.write_text(text.replace(old, new, 1))


backup(CLASSIFICATION)
backup(MODELS)

# ---------------------------------------------------------------------
# 1) Replace raw/global quality gate with centered, per-channel metrics.
#    Expected input layout remains samples x channels.
# ---------------------------------------------------------------------

new_quality_and_helpers = r'''
def assess_epoch_quality(epoch: np.ndarray, config: dict[str, Any] | None = None) -> EpochQualityResult:
    """Assess an epoch after removing stable per-channel offsets.

    The expected layout is samples x channels. This matches the realtime
    epoch payloads and the training call site, which passes channels x samples
    epochs as epoch.T.

    The old implementation measured max_abs and peak-to-peak over the raw
    full matrix. With Enobio/NIC2 raw exports, stable electrode/reference
    offsets can be tens of millivolts even when the event-locked EEG variation
    is normal. This gate therefore centers each channel before artifact checks.
    """

    cfg = dict(config or {})
    values = np.asarray(epoch, dtype=float)
    reasons: list[str] = []

    if values.ndim != 2 or values.size == 0:
        return EpochQualityResult(False, ("invalid_shape",), {})

    finite_fraction = float(np.mean(np.isfinite(values)))

    with np.errstate(invalid="ignore"):
        centered = values - np.nanmedian(values, axis=0, keepdims=True)
        finite_centered = centered[np.isfinite(centered)]
        channel_std = np.nanstd(centered, axis=0)
        channel_ptp = np.nanmax(centered, axis=0) - np.nanmin(centered, axis=0)

    metrics = {
        "finite_fraction": finite_fraction,
        "max_abs_uv": float(np.nanmax(np.abs(finite_centered))) if finite_centered.size else float("inf"),
        "peak_to_peak_uv": float(np.nanmax(channel_ptp)) if channel_ptp.size else float("inf"),
        "minimum_channel_std_uv": float(np.nanmin(channel_std)) if channel_std.size else 0.0,
    }

    if finite_fraction < float(cfg.get("minimum_finite_fraction", 1.0)):
        reasons.append("non_finite")
    if metrics["minimum_channel_std_uv"] < float(cfg.get("minimum_channel_std_uv", 0.01)):
        reasons.append("flatline")
    if metrics["max_abs_uv"] > float(cfg.get("max_abs_uv", 250.0)):
        reasons.append("max_abs_exceeded")
    if metrics["peak_to_peak_uv"] > float(cfg.get("max_peak_to_peak_uv", 400.0)):
        reasons.append("peak_to_peak_exceeded")

    return EpochQualityResult(not reasons, tuple(reasons), metrics)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _contract_float(contract: dict[str, Any], *keys: str, default: float | None = None) -> float | None:
    for key in keys:
        if key in contract and contract[key] is not None:
            value = contract[key]
            if value in {"", "none", "None"}:
                return None
            return float(value)
    return default


def _sosfiltfilt_safe(sos: np.ndarray, values: np.ndarray) -> np.ndarray:
    from scipy import signal

    sample_count = int(values.shape[1])
    if sample_count < 8:
        return values

    # scipy's default padlen can be too long for short ERP epochs.
    padlen = min(max(0, sample_count - 1), 3 * (2 * sos.shape[0] + 1))
    return signal.sosfiltfilt(sos, values, axis=1, padlen=padlen)


def _filtfilt_safe(b: np.ndarray, a: np.ndarray, values: np.ndarray) -> np.ndarray:
    from scipy import signal

    sample_count = int(values.shape[1])
    if sample_count < 8:
        return values

    padlen = min(max(0, sample_count - 1), 3 * (max(len(a), len(b)) - 1))
    return signal.filtfilt(b, a, values, axis=1, padlen=padlen)


def filter_classifier_epoch(
    channels_samples: np.ndarray,
    sample_rate_hz: float,
    contract: dict[str, Any],
) -> np.ndarray:
    """Apply optional zero-phase epoch filtering for post-window classification.

    This is used after the full stimulus-locked window is available. It is not
    a causal rolling filter. For classifier mode, that is acceptable because
    prediction only happens after tmax has elapsed.
    """

    values = np.asarray(channels_samples, dtype=float)
    if values.ndim != 2 or values.size == 0:
        return values

    try:
        from scipy import signal
    except Exception:
        # Keep the system usable if scipy is absent, although runtime/ml envs
        # should already provide it.
        return values

    fs = float(sample_rate_hz)
    nyquist = fs / 2.0
    output = np.asarray(values, dtype=float)

    notch_hz = _contract_float(contract, "notch_hz", "line_noise_hz", default=None)
    if notch_hz is not None and 0.0 < notch_hz < nyquist:
        q = float(contract.get("notch_q", 30.0))
        b, a = signal.iirnotch(w0=notch_hz, Q=q, fs=fs)
        output = _filtfilt_safe(b, a, output)

    low = _contract_float(contract, "filter_low_hz", "bandpass_low_hz", default=None)
    high = _contract_float(contract, "filter_high_hz", "bandpass_high_hz", default=None)
    order = int(contract.get("filter_order", 2))

    if low is not None or high is not None:
        if low is not None and high is not None and 0.0 < low < high < nyquist:
            sos = signal.butter(order, [low, high], btype="bandpass", fs=fs, output="sos")
            output = _sosfiltfilt_safe(sos, output)
        elif high is not None and 0.0 < high < nyquist:
            sos = signal.butter(order, high, btype="lowpass", fs=fs, output="sos")
            output = _sosfiltfilt_safe(sos, output)
        elif low is not None and 0.0 < low < nyquist:
            sos = signal.butter(order, low, btype="highpass", fs=fs, output="sos")
            output = _sosfiltfilt_safe(sos, output)

    return output


def average_reference(channels_samples: np.ndarray) -> np.ndarray:
    values = np.asarray(channels_samples, dtype=float)
    if values.ndim != 2 or values.shape[0] < 2:
        return values
    return values - np.nanmean(values, axis=0, keepdims=True)
'''

replace_regex(
    CLASSIFICATION,
    r"def assess_epoch_quality\(epoch: np\.ndarray, config: dict\[str, Any\] \| None = None\) -> EpochQualityResult:.*?return EpochQualityResult\(not reasons, tuple\(reasons\), metrics\)\n",
    new_quality_and_helpers + "\n",
    "classification.assess_epoch_quality_and_helpers",
)

# ---------------------------------------------------------------------
# 2) Make prepare_classifier_epoch apply filter -> baseline -> avg reference.
# ---------------------------------------------------------------------

old_prepare_tail = '''    baseline = contract.get("baseline_seconds", [-0.2, 0.0])
    return baseline_correct(values, times, baseline), required, times
'''

new_prepare_tail = '''    values = filter_classifier_epoch(values, sample_rate_hz, contract)
    baseline = contract.get("baseline_seconds", [-0.2, 0.0])
    values = baseline_correct(values, times, baseline)

    if _as_bool(contract.get("average_reference"), default=True):
        values = average_reference(values)

    if not np.all(np.isfinite(values)):
        raise ValueError("prepared classifier epoch contains non-finite values")

    return values, required, times
'''

replace_text(CLASSIFICATION, old_prepare_tail, new_prepare_tail, "classification.prepare_classifier_epoch_tail")

# ---------------------------------------------------------------------
# 3) Add missing artifact epoch helper in models.py if absent.
# ---------------------------------------------------------------------

models_text = MODELS.read_text()
if "def prepare_artifact_epoch(" not in models_text:
    insert_before = "\ndef make_model_adapter(kind: str, config: dict[str, Any] | None = None) -> BaseModelAdapter:\n"
    helper = r'''

def prepare_artifact_epoch(
    epoch: np.ndarray,
    sample_rate_hz: float,
    channel_names: list[str],
    metadata: dict[str, Any],
    contract: dict[str, Any],
    input_layout: str = "auto",
) -> np.ndarray:
    """Prepare one epoch using the model bundle contract.

    This keeps online inference aligned with train_epoch_model(), which uses
    prepare_classifier_epoch() before fitting classifiers.
    """

    prepared, _, _ = prepare_classifier_epoch(
        epoch,
        sample_rate_hz,
        channel_names,
        metadata,
        {**dict(contract or {}), "input_layout": input_layout},
    )
    return prepared
'''
    if insert_before not in models_text:
        raise SystemExit("FAILED: could not find insertion point for prepare_artifact_epoch")
    MODELS.write_text(models_text.replace(insert_before, helper + insert_before, 1))

# ---------------------------------------------------------------------
# 4) Move training quality gate after classifier preprocessing.
# ---------------------------------------------------------------------

old_train_block = '''    eligible_training_epochs = int(x.shape[0])
    quality_config = dict(cfg.get("quality_gate", {}))
    quality_valid = np.asarray([assess_epoch_quality(epoch.T, quality_config).valid for epoch in x], dtype=bool)
    x = x[quality_valid]
    y = y[quality_valid]
    trials = trials[quality_valid]
    if x.shape[0] == 0:
        raise ValueError("all training epochs were rejected by the quality gate")
    if len(set(y.tolist())) < 2:
        raise ValueError("training requires both GO and NO-GO labels")
    contract = training_contract(data, cfg)
    times = np.asarray(npz_value(data, "times", []), dtype=float)
    channel_names = [str(value) for value in contract["channel_names"]]
    corrected = np.stack(
        [
            prepare_classifier_epoch(
                epoch,
                float(contract["sample_rate_hz"]),
                channel_names,
                {"relative_times": times.tolist(), "epoch_window_seconds": contract["epoch_window_seconds"]},
                contract,
            )[0]
            for epoch in x
        ],
        axis=0,
    )
'''

new_train_block = '''    eligible_training_epochs = int(x.shape[0])
    contract = training_contract(data, cfg)
    times = np.asarray(npz_value(data, "times", []), dtype=float)
    channel_names = [str(value) for value in contract["channel_names"]]
    quality_config = dict(cfg.get("quality_gate", {}))

    prepared_epochs: list[np.ndarray] = []
    quality_valid: list[bool] = []
    for epoch in x:
        prepared = prepare_classifier_epoch(
            epoch,
            float(contract["sample_rate_hz"]),
            channel_names,
            {"relative_times": times.tolist(), "epoch_window_seconds": contract["epoch_window_seconds"]},
            contract,
        )[0]
        prepared_epochs.append(prepared)
        quality_valid.append(assess_epoch_quality(prepared.T, quality_config).valid)

    quality_mask = np.asarray(quality_valid, dtype=bool)
    corrected_all = np.stack(prepared_epochs, axis=0)
    corrected = corrected_all[quality_mask]
    x = x[quality_mask]
    y = y[quality_mask]
    trials = trials[quality_mask]

    if corrected.shape[0] == 0:
        raise ValueError("all training epochs were rejected by the quality gate")
    if len(set(y.tolist())) < 2:
        raise ValueError("training requires both GO and NO-GO labels")
'''

replace_text(MODELS, old_train_block, new_train_block, "models.train_epoch_model_quality_order")

# ---------------------------------------------------------------------
# 5) Harden pyRiemann: regularized covariance + lower default nfilter.
# ---------------------------------------------------------------------

old_pyriemann = '''            ("xdawn_cov", XdawnCovariances(nfilter=int(config.get("nfilter", 4)))),
'''

new_pyriemann = '''            (
                "xdawn_cov",
                XdawnCovariances(
                    nfilter=int(config.get("nfilter", 2)),
                    estimator=str(config.get("cov_estimator", config.get("estimator", "oas"))),
                    xdawn_estimator=str(config.get("xdawn_estimator", config.get("cov_estimator", "oas"))),
                ),
            ),
'''

replace_text(MODELS, old_pyriemann, new_pyriemann, "models.pyriemann_regularized_xdawn")

# ---------------------------------------------------------------------
# 6) Update training contract metadata and preprocessing defaults.
# ---------------------------------------------------------------------

old_contract_return = '''    return {
        "input_layout": "channels_x_samples",
        "input_units": str(config.get("input_units", "microvolts")),
        "channel_names": [str(value) for value in npz_value(data, "channel_names", [])],
        "required_channels": [str(value) for value in npz_value(data, "channel_names", [])],
        "sample_rate_hz": sample_rate,
        "epoch_window_seconds": [float(times[0]), float(times[-1])],
        "baseline_seconds": [float(value) for value in config.get("baseline_seconds", [-0.2, 0.0])],
        "preprocessing": {"kind": "as_recorded_microvolts_then_baseline_correction", "resampling": "none"},
    }
'''

new_contract_return = '''    notch_hz = config.get("notch_hz", 60.0)
    filter_low_hz = config.get("filter_low_hz", None)
    filter_high_hz = config.get("filter_high_hz", None)
    average_reference_enabled = bool(config.get("average_reference", True))
    baseline_seconds = [float(value) for value in config.get("baseline_seconds", [-0.2, 0.0])]

    return {
        "input_layout": "channels_x_samples",
        "input_units": str(config.get("input_units", "microvolts")),
        "channel_names": [str(value) for value in npz_value(data, "channel_names", [])],
        "required_channels": [str(value) for value in npz_value(data, "channel_names", [])],
        "sample_rate_hz": sample_rate,
        "epoch_window_seconds": [float(times[0]), float(times[-1])],
        "baseline_seconds": baseline_seconds,
        "notch_hz": None if notch_hz in {"", "none", "None"} else notch_hz,
        "notch_q": float(config.get("notch_q", 30.0)),
        "filter_low_hz": filter_low_hz,
        "filter_high_hz": filter_high_hz,
        "filter_order": int(config.get("filter_order", 2)),
        "average_reference": average_reference_enabled,
        "preprocessing": {
            "kind": "notch_then_baseline_correction_then_average_reference",
            "input_units": str(config.get("input_units", "microvolts")),
            "notch_hz": None if notch_hz in {"", "none", "None"} else notch_hz,
            "notch_q": float(config.get("notch_q", 30.0)),
            "filter_low_hz": filter_low_hz,
            "filter_high_hz": filter_high_hz,
            "filter_order": int(config.get("filter_order", 2)),
            "baseline_seconds": baseline_seconds,
            "average_reference": average_reference_enabled,
            "resampling": "none",
        },
    }
'''

replace_text(MODELS, old_contract_return, new_contract_return, "models.training_contract_preprocessing_metadata")

print("OK: patched classifier preprocessing and pyRiemann regularization.")
print("Backups:")
print(" ", CLASSIFICATION.with_suffix(CLASSIFICATION.suffix + ".bak-classifier-preproc"))
print(" ", MODELS.with_suffix(MODELS.suffix + ".bak-classifier-preproc"))
