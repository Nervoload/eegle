#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "reproduce" / "workers" / "realtime_processor.py"


def backup(path: Path) -> None:
    backup_path = path.with_suffix(path.suffix + ".bak-live-quality-gate")
    if not backup_path.exists():
        backup_path.write_text(path.read_text())


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"FAILED: could not find block for {label}")
    return text.replace(old, new, 1)


backup(WORKER)
text = WORKER.read_text()

# ---------------------------------------------------------------------
# 1) Import EpochQualityResult and prepare_classifier_epoch.
# ---------------------------------------------------------------------

old_import = '''from reproduce.realtime.classification import (
    assess_epoch_quality,
    load_model_bundle,
    model_prediction_row,
    model_rejection_row,
    sanitize_model_metadata,
    snapshot_model_bundle,
)
'''

new_import = '''from reproduce.realtime.classification import (
    EpochQualityResult,
    assess_epoch_quality,
    load_model_bundle,
    model_prediction_row,
    model_rejection_row,
    prepare_classifier_epoch,
    sanitize_model_metadata,
    snapshot_model_bundle,
)
'''

text = replace_once(text, old_import, new_import, "classification import block")

# ---------------------------------------------------------------------
# 2) Replace blocking raw-epoch quality gate with prepared-epoch warn gate.
# ---------------------------------------------------------------------

old_block = '''                    if not inference_enabled:
                        continue
                    quality = assess_epoch_quality(epoch.data, quality_config) if classifier_mode else None
                    if quality is not None and not quality.valid:
                        assert model_prediction_writer is not None
                        rejection = model_rejection_row(epoch_payload, ",".join(quality.reasons), quality.payload())
                        model_prediction_writer.write(rejection)
                        classifier_rejected_epoch_count += 1
                        telemetry.emit("realtime.epoch_rejected", level="realtime", message="Classifier epoch rejected", metadata=rejection)
                        continue
                    full_model_metadata = {**epoch_payload, "relative_times": epoch.relative_times.astype(float).tolist()}
                    model_metadata = sanitize_model_metadata(full_model_metadata) if classifier_mode else full_model_metadata
'''

new_block = '''                    if not inference_enabled:
                        continue
                    full_model_metadata = {**epoch_payload, "relative_times": epoch.relative_times.astype(float).tolist()}
                    model_metadata = sanitize_model_metadata(full_model_metadata) if classifier_mode else full_model_metadata
                    quality = (
                        _classifier_epoch_quality(
                            epoch.data,
                            sample_rate,
                            channel_names,
                            model_metadata,
                            model_entries,
                            quality_config,
                        )
                        if classifier_mode
                        else None
                    )
                    if quality is not None and _should_block_classifier_prediction(quality, quality_config):
                        assert model_prediction_writer is not None
                        rejection = model_rejection_row(epoch_payload, ",".join(quality.reasons), quality.payload())
                        model_prediction_writer.write(rejection)
                        classifier_rejected_epoch_count += 1
                        telemetry.emit(
                            "realtime.epoch_rejected",
                            level="realtime",
                            message="Classifier epoch rejected",
                            metadata=rejection,
                        )
                        continue
'''

text = replace_once(text, old_block, new_block, "live classifier quality gate")

# ---------------------------------------------------------------------
# 3) Store model bundle contracts in model_entries.
# ---------------------------------------------------------------------

old_model_load_block = '''        adapter_config = {key: item for key, item in value.items() if key not in {"id", "role", "kind"}}
        if adapter_config.get("bundle_path"):
            snapshot = snapshot_model_bundle(adapter_config["bundle_path"], paths.realtime_model_snapshots, model_id)
            adapter_config["bundle_path"] = snapshot["bundle_dir"]
        entries.append(
            {
                "id": model_id,
                "role": role,
                "kind": kind,
                "config": adapter_config,
                "adapter": make_model(kind, adapter_config),
            }
        )
'''

new_model_load_block = '''        adapter_config = {key: item for key, item in value.items() if key not in {"id", "role", "kind"}}
        contract: dict[str, Any] = {}
        if adapter_config.get("bundle_path"):
            snapshot = snapshot_model_bundle(adapter_config["bundle_path"], paths.realtime_model_snapshots, model_id)
            adapter_config["bundle_path"] = snapshot["bundle_dir"]
            try:
                bundle = load_model_bundle(adapter_config["bundle_path"])
                contract = dict(bundle.get("contract") or {})
            except Exception:
                contract = {}
        entries.append(
            {
                "id": model_id,
                "role": role,
                "kind": kind,
                "config": adapter_config,
                "contract": contract,
                "adapter": make_model(kind, adapter_config),
            }
        )
'''

text = replace_once(text, old_model_load_block, new_model_load_block, "classifier model contract capture")

# ---------------------------------------------------------------------
# 4) Add helper functions before _load_classifier_models.
# ---------------------------------------------------------------------

insert_before = '''def _load_classifier_models(
'''

helpers = '''def _classifier_epoch_quality(
    epoch_data: np.ndarray,
    sample_rate_hz: float,
    channel_names: list[str],
    metadata: dict[str, Any],
    model_entries: list[dict[str, Any]],
    quality_config: dict[str, Any],
) -> EpochQualityResult:
    """Assess the model-prepared epoch instead of raw Enobio/NIC2 samples.

    Raw Enobio/NIC2 epochs can contain large stable channel offsets and strong
    line noise. The model bundle contract defines the representation the model
    actually sees, so the live quality gate should inspect that prepared
    representation, not raw epoch.data.
    """

    try:
        primary = next((entry for entry in model_entries if entry.get("role") == "primary"), model_entries[0])
    except Exception:
        primary = {}

    contract = {
        **dict(primary.get("config") or {}),
        **dict(primary.get("contract") or {}),
    }
    contract.setdefault("input_layout", "auto")
    contract.setdefault("input_units", "microvolts")
    contract.setdefault("channel_names", list(channel_names))
    contract.setdefault("required_channels", list(channel_names))
    contract.setdefault("sample_rate_hz", float(sample_rate_hz))
    contract.setdefault("baseline_seconds", [-0.2, 0.0])
    contract.setdefault("average_reference", True)

    try:
        prepared, _, _ = prepare_classifier_epoch(
            epoch_data,
            sample_rate_hz,
            channel_names,
            metadata,
            contract,
        )
    except Exception as exc:
        return EpochQualityResult(
            False,
            ("quality_preparation_failed",),
            {
                "exception_type": type(exc).__name__,
                "exception": str(exc),
            },
        )

    # assess_epoch_quality expects samples x channels.
    return assess_epoch_quality(prepared.T, quality_config)


def _should_block_classifier_prediction(
    quality: EpochQualityResult,
    quality_config: dict[str, Any],
) -> bool:
    """Decide whether quality should block prediction.

    Default behavior is warn-only for artifact thresholds. This keeps live
    classifier instrumentation usable while still recording quality payloads.

    Set realtime.quality_gate.reject_predictions=true to restore strict
    blocking. Non-finite/invalid/preparation-failed epochs still block by
    default because they are likely to crash the model or corrupt outputs.
    """

    if quality.valid:
        return False

    if bool(quality_config.get("reject_predictions", False)):
        return True

    default_blocking = ["invalid_shape", "non_finite", "quality_preparation_failed"]
    configured = quality_config.get("always_reject_reasons", default_blocking)
    blocking = {str(reason) for reason in configured}
    return bool(set(quality.reasons) & blocking)


'''

if insert_before not in text:
    raise SystemExit("FAILED: could not find insertion point before _load_classifier_models")

text = text.replace(insert_before, helpers + insert_before, 1)

WORKER.write_text(text)
print("OK: patched live classifier quality gate")
print("Backup:", WORKER.with_suffix(WORKER.suffix + ".bak-live-quality-gate"))
