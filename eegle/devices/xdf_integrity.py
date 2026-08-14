"""Bounded-memory integrity checks for managed LabRecorder XDF files."""

from __future__ import annotations

import csv
import json
import logging
import os
from pathlib import Path
from time import sleep
from typing import Any

import numpy as np

from eegle.hardware.profiles import configured_channel_types, expected_profile, mapped_channel_names


def validate_xdf_recording(session_dir: str | Path, *, required: bool) -> dict[str, Any]:
    root = Path(session_dir)
    xdf_path = root / "raw" / "recording.xdf"
    metadata_path = root / "raw" / "xdf_metadata.json"
    parameters = _load_json(root / "parameters.json") or {}
    metadata = _load_json(metadata_path) or {}
    eeg_config = dict(parameters.get("hardware", {}).get("eeg", {}) or {})
    marker_config = dict(parameters.get("hardware", {}).get("markers", {}) or {})
    recorder_config = dict(parameters.get("processes", {}).get("recorder", {}) or {})
    failures: list[str] = []
    warnings: list[str] = []
    result: dict[str, Any] = {
        "status": "pass",
        "required": bool(required),
        "raw_file": str(xdf_path),
        "metadata_file": str(metadata_path),
        "metadata": metadata,
        "captured_streams": [],
        "eeg": {},
        "markers": {},
        "failures": failures,
        "warnings": warnings,
    }
    if not required and not xdf_path.exists():
        result.update(
            {
                "status": "skipped",
                "skipped": True,
                "skip_reason": "XDF recording was not requested for this software-only run",
            }
        )
        return result
    if not xdf_path.exists():
        failures.append("authoritative XDF recording is missing")
        return _finish(result, metadata_path)
    if xdf_path.stat().st_size <= 4:
        failures.append("authoritative XDF recording is empty")
        return _finish(result, metadata_path)
    try:
        import pyxdf
    except Exception as exc:
        failures.append(f"PyXDF import failed: {type(exc).__name__}: {exc}")
        return _finish(result, metadata_path)

    try:
        stream_infos = list(pyxdf.resolve_streams(str(xdf_path)))
    except Exception as exc:
        failures.append(f"XDF structure could not be read: {type(exc).__name__}: {exc}")
        return _finish(result, metadata_path)
    result["captured_streams"] = stream_infos
    if not stream_infos:
        failures.append("XDF does not contain any stream headers")
        return _finish(result, metadata_path)

    selected = dict(metadata.get("selected_eeg_stream") or {})
    eeg_matches = [
        stream
        for stream in stream_infos
        if _matches_selected_eeg(stream, selected, eeg_config)
    ]
    marker_source_id = str(marker_config.get("source_id") or "")
    marker_matches = [
        stream
        for stream in stream_infos
        if marker_source_id and str(stream.get("source_id") or "") == marker_source_id
    ]
    if len(eeg_matches) != 1:
        failures.append(
            f"XDF contains {len(eeg_matches)} streams matching the selected EEG identity; expected exactly one"
        )
    if len(marker_matches) != 1:
        failures.append(
            f"XDF contains {len(marker_matches)} streams matching the run-specific marker source ID; expected exactly one"
        )
    selected_ids = [
        int(stream["stream_id"])
        for stream in [*eeg_matches, *marker_matches]
        if stream.get("stream_id") is not None
    ]
    required_ids = set(selected_ids)
    unexpected = [stream for stream in stream_infos if int(stream.get("stream_id", -1)) not in required_ids]
    if unexpected:
        warnings.append(f"XDF contains {len(unexpected)} additional LSL stream(s)")
    if failures:
        return _finish(result, metadata_path)

    eeg_id = int(eeg_matches[0]["stream_id"])
    marker_id = int(marker_matches[0]["stream_id"])
    stats = {
        eeg_id: _new_stream_stats(),
        marker_id: _new_stream_stats(),
    }
    marker_labels: list[str] = []
    maximum_gap = float(eeg_config.get("maximum_timestamp_gap_seconds", 0.1))

    def on_chunk(values: Any, stamps: Any, info: dict[str, Any], stream_id: int) -> tuple[Any, Any, dict[str, Any]]:
        stream_id = int(stream_id)
        timestamps = np.asarray(stamps, dtype=float)
        if stream_id in stats:
            _update_stats(stats[stream_id], timestamps, maximum_gap if stream_id == eeg_id else None)
        if stream_id == marker_id:
            marker_labels.extend(_marker_values(values))
            return values, stamps, info
        if stream_id == eeg_id and timestamps.size > 2:
            indices = np.asarray([0, timestamps.size - 1], dtype=int)
            return values[indices], timestamps[indices], info
        return values, stamps, info

    log_messages: list[str] = []
    logger = logging.getLogger("pyxdf.pyxdf")
    handler = _CollectErrors(log_messages)
    logger.addHandler(handler)
    try:
        loaded_streams, _ = pyxdf.load_xdf(
            str(xdf_path),
            select_streams=selected_ids,
            on_chunk=on_chunk,
            synchronize_clocks=True,
            dejitter_timestamps=False,
            verbose=False,
        )
    except Exception as exc:
        failures.append(f"XDF sample chunks could not be read: {type(exc).__name__}: {exc}")
        loaded_streams = []
    finally:
        logger.removeHandler(handler)
    if log_messages:
        failures.append("PyXDF reported corrupt or unreadable chunks: " + "; ".join(log_messages[:3]))

    loaded_by_id = {
        int(stream.get("info", {}).get("stream_id")): stream
        for stream in loaded_streams
        if stream.get("info", {}).get("stream_id") is not None
    }
    eeg_loaded = loaded_by_id.get(eeg_id, {})
    marker_loaded = loaded_by_id.get(marker_id, {})
    eeg_info = dict(eeg_loaded.get("info") or {})
    original_labels = _channel_labels(eeg_info, int(eeg_matches[0].get("channel_count") or 0))
    mapped_labels, mapping_source = mapped_channel_names(original_labels, eeg_config)
    expected_labels = list(eeg_config.get("expected_channel_names") or [])
    if not expected_labels and eeg_config.get("profile"):
        try:
            expected_labels = list(
                expected_profile(str(eeg_config["profile"]), eeg_config.get("family")).channel_names
            )
        except KeyError:
            expected_labels = []
    positional_evidence = _confirmed_positional_xdf_evidence(
        root,
        selected,
        eeg_config,
        expected_labels,
        observed_count=int(eeg_matches[0].get("channel_count") or 0),
    )
    if (
        expected_labels
        and mapped_labels != expected_labels
        and positional_evidence["status"] == "pass"
    ):
        mapped_labels = list(expected_labels)
        mapping_source = "operator_confirmed_position+csv_mirror_identity"
        warnings.append(
            "XDF channel descriptor labels differed from the operator-confirmed positional names; "
            "the canonical 65-value order was retained because the independent source-preserving "
            "CSV mirror recorded the same LSL stream identity and value order"
        )
    mapped_types = configured_channel_types(mapped_labels, eeg_config)
    expected_counts = [int(value) for value in eeg_config.get("expected_channel_counts", [])]
    observed_count = int(eeg_matches[0].get("channel_count") or 0)
    observed_rate = float(eeg_matches[0].get("nominal_srate") or 0.0)
    expected_rate = float(eeg_config.get("expected_sample_rate_hz") or 0.0)
    if expected_counts and observed_count not in expected_counts:
        failures.append(f"XDF EEG channel count is {observed_count}; expected one of {expected_counts}")
    if expected_rate and abs(observed_rate - expected_rate) >= 1.0:
        failures.append(f"XDF EEG sample rate is {observed_rate:g} Hz; expected {expected_rate:g} Hz")
    if expected_labels and mapped_labels != expected_labels:
        mismatch_detail = _channel_mismatch_detail(original_labels, mapped_labels, expected_labels)
        failures.append(
            "XDF EEG channel labels/order do not map to the configured physical device order"
            f" (mapping={mapping_source}; {mismatch_detail})"
        )
    eeg_stats = stats[eeg_id]
    if eeg_stats["sample_count"] <= 0:
        failures.append("XDF EEG stream does not contain samples")
    if eeg_stats["nonfinite_timestamp_count"]:
        failures.append("XDF EEG contains non-finite source timestamps")
    if eeg_stats["nonmonotonic_timestamp_count"]:
        failures.append("XDF EEG contains nonmonotonic source timestamps")
    if eeg_stats["timestamp_gap_count"]:
        failures.append("XDF EEG contains one or more timestamp gaps above the configured acquisition limit")

    receipt_labels = _marker_receipt_labels(root / "raw" / "lsl_markers_received.csv")
    if not receipt_labels:
        failures.append("independent LSL marker receipt is missing or empty")
    if marker_labels != receipt_labels:
        failures.append("XDF marker labels/order do not exactly match the independent LSL marker receipt")
    if stats[marker_id]["sample_count"] <= 0:
        failures.append("XDF marker stream does not contain samples")

    eeg_synced = np.asarray(eeg_loaded.get("time_stamps", []), dtype=float)
    marker_synced = np.asarray(marker_loaded.get("time_stamps", []), dtype=float)
    coverage: dict[str, Any] = {
        "status": "unavailable",
        "boundary_tolerance_seconds": 0.5,
        "end_shortfall_seconds": None,
        "csv_tail_evidence": None,
    }
    if eeg_synced.size and marker_synced.size:
        tolerance = 0.5
        first_eeg = float(eeg_synced[0])
        last_eeg = float(eeg_synced[-1])
        first_marker = float(marker_synced[0])
        last_marker = float(marker_synced[-1])
        start_lag = first_eeg - first_marker
        end_shortfall = last_marker - last_eeg
        maximum_warning_shortfall = max(
            tolerance,
            float(recorder_config.get("maximum_xdf_tail_shortfall_warning_seconds", 2.0)),
        )
        coverage.update(
            {
                "status": "pass",
                "start_lag_seconds": start_lag,
                "end_shortfall_seconds": max(0.0, end_shortfall),
                "maximum_warning_shortfall_seconds": maximum_warning_shortfall,
            }
        )
        if start_lag > tolerance:
            coverage["status"] = "fail"
            failures.append(
                "XDF EEG starts after the first required marker "
                f"by {start_lag:.6f} seconds (limit {tolerance:.3f})"
            )
        if end_shortfall > tolerance:
            tail_evidence = _source_preserving_csv_tail_evidence(
                root,
                eeg_matches[0],
                marker_matches[0],
                marker_sequence_matches=marker_labels == receipt_labels,
            )
            coverage["csv_tail_evidence"] = tail_evidence
            if tail_evidence["status"] == "pass" and end_shortfall <= maximum_warning_shortfall:
                coverage["status"] = "warning" if coverage["status"] != "fail" else "fail"
                warnings.append(
                    "XDF finalized before its last synchronized marker boundary by "
                    f"{end_shortfall:.6f} seconds; the complete source-preserving CSV mirror "
                    "covers the marker with the same EEG stream identity, so the acquisition tail "
                    "is retained outside the primary XDF"
                )
            else:
                coverage["status"] = "fail"
                evidence_detail = "; ".join(tail_evidence.get("reasons") or [])
                failures.append(
                    "XDF EEG ends before the last required marker "
                    f"by {end_shortfall:.6f} seconds (warning limit {maximum_warning_shortfall:.3f}; "
                    f"CSV fallback evidence: {evidence_detail or tail_evidence.get('status')})"
                )
    else:
        failures.append("XDF lacks synchronized EEG/marker timestamps needed to verify recording coverage")
        coverage["status"] = "fail"

    result["eeg"] = {
        "stream": eeg_matches[0],
        "sample_count": eeg_stats["sample_count"],
        "first_source_timestamp": eeg_stats["first_timestamp"],
        "last_source_timestamp": eeg_stats["last_timestamp"],
        "nonfinite_timestamp_count": eeg_stats["nonfinite_timestamp_count"],
        "nonmonotonic_timestamp_count": eeg_stats["nonmonotonic_timestamp_count"],
        "timestamp_gap_count": eeg_stats["timestamp_gap_count"],
        "largest_timestamp_gap_seconds": eeg_stats["largest_timestamp_gap_seconds"],
        "original_channel_names": original_labels,
        "mapped_channel_names": mapped_labels,
        "mapped_channel_types": mapped_types,
        "channel_mapping_source": mapping_source,
        "confirmed_positional_mapping_evidence": positional_evidence,
        "synchronized_first_timestamp": None if not eeg_synced.size else float(eeg_synced[0]),
        "synchronized_last_timestamp": None if not eeg_synced.size else float(eeg_synced[-1]),
    }
    result["markers"] = {
        "stream": marker_matches[0],
        "sample_count": stats[marker_id]["sample_count"],
        "labels": marker_labels,
        "receipt_labels": receipt_labels,
        "sequence_matches_receipt": marker_labels == receipt_labels,
        "synchronized_first_timestamp": None if not marker_synced.size else float(marker_synced[0]),
        "synchronized_last_timestamp": None if not marker_synced.size else float(marker_synced[-1]),
    }
    result["recording_coverage"] = coverage
    result["pyxdf_errors"] = log_messages
    return _finish(result, metadata_path)


def _new_stream_stats() -> dict[str, Any]:
    return {
        "sample_count": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "nonfinite_timestamp_count": 0,
        "nonmonotonic_timestamp_count": 0,
        "timestamp_gap_count": 0,
        "largest_timestamp_gap_seconds": 0.0,
    }


def _matches_selected_eeg(
    stream: dict[str, Any],
    selected: dict[str, Any],
    eeg_config: dict[str, Any],
) -> bool:
    """Match the full stable identity retained by the independent CSV selector."""
    expected_name = str(selected.get("name") or "").strip()
    expected_source_id = str(selected.get("source_id") or "").strip()
    expected_hostname = str(selected.get("hostname") or "").strip()
    expected_type = str(selected.get("type") or eeg_config.get("lsl_stream_type", "EEG")).strip()
    if not expected_name:
        return False
    if str(stream.get("name") or "") != expected_name:
        return False
    if expected_source_id and str(stream.get("source_id") or "") != expected_source_id:
        return False
    if expected_hostname and str(stream.get("hostname") or "") != expected_hostname:
        return False
    return str(stream.get("type") or "").lower() == expected_type.lower()


def _update_stats(stats: dict[str, Any], timestamps: np.ndarray, maximum_gap: float | None) -> None:
    if timestamps.size == 0:
        return
    finite = np.isfinite(timestamps)
    stats["sample_count"] += int(timestamps.size)
    stats["nonfinite_timestamp_count"] += int(np.sum(~finite))
    values = timestamps[finite]
    if values.size == 0:
        return
    previous = stats.get("last_timestamp")
    if stats.get("first_timestamp") is None:
        stats["first_timestamp"] = float(values[0])
    differences = np.diff(values)
    if previous is not None:
        differences = np.concatenate((np.asarray([float(values[0]) - float(previous)]), differences))
    stats["nonmonotonic_timestamp_count"] += int(np.sum(differences <= 0))
    if maximum_gap is not None:
        gaps = differences[differences > maximum_gap]
        stats["timestamp_gap_count"] += int(gaps.size)
        if gaps.size:
            stats["largest_timestamp_gap_seconds"] = max(
                float(stats["largest_timestamp_gap_seconds"]),
                float(np.max(gaps)),
            )
    stats["last_timestamp"] = float(values[-1])


def _marker_values(values: Any) -> list[str]:
    labels: list[str] = []
    for row in values:
        if isinstance(row, (list, tuple, np.ndarray)):
            if len(row):
                labels.append(str(row[0]))
        else:
            labels.append(str(row))
    return labels


def _channel_mismatch_detail(
    original: list[str],
    mapped: list[str],
    expected: list[str],
    *,
    limit: int = 6,
) -> str:
    mismatches = []
    for index, expected_name in enumerate(expected):
        mapped_name = mapped[index] if index < len(mapped) else "<missing>"
        if mapped_name == expected_name:
            continue
        original_name = original[index] if index < len(original) else "<missing>"
        mismatches.append(
            f"value {index + 1}: XDF={original_name!r}, mapped={mapped_name!r}, expected={expected_name!r}"
        )
        if len(mismatches) >= limit:
            break
    if len(mapped) != len(expected):
        mismatches.append(f"mapped count={len(mapped)}, expected count={len(expected)}")
    return "; ".join(mismatches) or "labels differ"


def _confirmed_positional_xdf_evidence(
    root: Path,
    selected_xdf_stream: dict[str, Any],
    eeg_config: dict[str, Any],
    expected_labels: list[str],
    *,
    observed_count: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    configured_source = str(eeg_config.get("mapping_source") or "")
    if not configured_source.startswith("operator_confirmed"):
        reasons.append("device mapping is not operator-confirmed positional")
    if not expected_labels or observed_count != len(expected_labels):
        reasons.append("XDF value count does not match the confirmed positional contract")
    raw_metadata = _load_json(root / "raw" / "eeg_metadata.json") or {}
    raw_stream = dict(raw_metadata.get("stream") or {})
    contract = dict(raw_metadata.get("raw_sample_contract") or {})
    if raw_metadata.get("status") != "stopped":
        reasons.append("independent CSV mirror did not stop cleanly")
    if list(raw_stream.get("channel_names") or []) != expected_labels:
        reasons.append("independent CSV mirror did not retain the canonical channel header")
    if raw_stream.get("channel_value_order_changed") is not False:
        reasons.append("independent CSV mirror does not prove source value-order preservation")
    if contract.get("channel_value_order_modified") is not False:
        reasons.append("raw sample contract does not prove source value-order preservation")
    identity_fields = ("name", "type", "source_id", "hostname", "channel_count")
    identity_mismatches = []
    for field in identity_fields:
        selected_value = selected_xdf_stream.get(field)
        if selected_value in {None, ""}:
            continue
        if str(raw_stream.get(field)) != str(selected_value):
            identity_mismatches.append(field)
    if identity_mismatches:
        reasons.append(
            "XDF and CSV mirror LSL identities differ in " + ", ".join(identity_mismatches)
        )
    return {
        "status": "fail" if reasons else "pass",
        "configured_mapping_source": configured_source,
        "xdf_selected_stream": {
            field: selected_xdf_stream.get(field) for field in identity_fields
        },
        "csv_mirror_stream": {field: raw_stream.get(field) for field in identity_fields},
        "reasons": reasons,
    }


def _source_preserving_csv_tail_evidence(
    root: Path,
    selected_xdf_stream: dict[str, Any],
    selected_marker_stream: dict[str, Any],
    *,
    marker_sequence_matches: bool,
) -> dict[str, Any]:
    """Prove that a short XDF finalization tail exists intact in the CSV mirror."""

    reasons: list[str] = []
    raw_metadata = _load_json(root / "raw" / "eeg_metadata.json") or {}
    raw_stream = dict(raw_metadata.get("stream") or {})
    contract = dict(raw_metadata.get("raw_sample_contract") or {})
    marker_metadata = _load_json(root / "raw" / "lsl_markers_received_metadata.json") or {}
    raw_last = _optional_float(raw_metadata.get("last_lsl_timestamp"))
    marker_last = _optional_float(marker_metadata.get("last_lsl_timestamp"))
    if raw_metadata.get("status") != "stopped":
        reasons.append("CSV EEG mirror did not stop cleanly")
    if int(raw_metadata.get("sample_count") or 0) <= 0:
        reasons.append("CSV EEG mirror contains no samples")
    if int(raw_metadata.get("timestamp_gap_count") or 0) != 0:
        reasons.append("CSV EEG mirror contains timestamp gaps")
    if int(raw_metadata.get("nonmonotonic_timestamp_count") or 0) != 0:
        reasons.append("CSV EEG mirror contains nonmonotonic timestamps")
    if contract.get("source_timestamp_retained") is not True:
        reasons.append("CSV EEG mirror is not source-preserving")
    if contract.get("amplitude_samples_modified") is not False:
        reasons.append("CSV EEG mirror does not prove unmodified amplitudes")
    if contract.get("channel_value_order_modified") is not False:
        reasons.append("CSV EEG mirror does not prove unmodified channel order")
    if marker_metadata.get("status") != "stopped":
        reasons.append("independent marker receipt did not stop cleanly")
    if not marker_sequence_matches:
        reasons.append("XDF and independent marker sequences differ")
    eeg_hostname = str(selected_xdf_stream.get("hostname") or "").strip()
    marker_hostname = str(selected_marker_stream.get("hostname") or "").strip()
    if not eeg_hostname or not marker_hostname:
        reasons.append("XDF EEG/marker host clock identity is unavailable")
    elif eeg_hostname != marker_hostname:
        reasons.append("XDF EEG and marker streams originate from different host clocks")
    if raw_last is None or marker_last is None:
        reasons.append("CSV EEG/marker tail timestamps are unavailable")
    elif raw_last < marker_last:
        reasons.append(
            f"CSV EEG mirror ends {marker_last - raw_last:.6f} seconds before the last marker"
        )
    identity_fields = ("name", "type", "source_id", "hostname", "channel_count")
    identity_mismatches = []
    for field in identity_fields:
        xdf_value = selected_xdf_stream.get(field)
        if xdf_value in {None, ""}:
            continue
        if str(raw_stream.get(field)) != str(xdf_value):
            identity_mismatches.append(field)
    if identity_mismatches:
        reasons.append(
            "XDF and CSV EEG identities differ in " + ", ".join(identity_mismatches)
        )
    return {
        "status": "fail" if reasons else "pass",
        "csv_last_lsl_timestamp": raw_last,
        "last_marker_lsl_timestamp": marker_last,
        "xdf_stream": {field: selected_xdf_stream.get(field) for field in identity_fields},
        "xdf_marker_stream": {
            field: selected_marker_stream.get(field) for field in identity_fields
        },
        "csv_stream": {field: raw_stream.get(field) for field in identity_fields},
        "reasons": reasons,
    }


def _channel_labels(info: dict[str, Any], channel_count: int) -> list[str]:
    try:
        rows = info["desc"][0]["channels"][0]["channel"]
        labels = [str(row.get("label", [""])[0]) for row in rows]
    except (KeyError, IndexError, TypeError):
        labels = []
    if len(labels) != channel_count or any(not label for label in labels):
        return [f"ch_{index + 1:03d}" for index in range(channel_count)]
    return labels


def _marker_receipt_labels(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [str(row.get("marker_label") or "") for row in csv.DictReader(handle)]
    except OSError:
        return []


def _optional_float(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if np.isfinite(converted) else None


def _finish(result: dict[str, Any], metadata_path: Path) -> dict[str, Any]:
    failures = list(result.get("failures") or [])
    warnings = list(result.get("warnings") or [])
    result["status"] = "fail" if failures else ("warning" if warnings else "pass")
    metadata = _load_json(metadata_path) or {}
    metadata["validation"] = {
        key: value
        for key, value in result.items()
        if key not in {"metadata"}
    }
    try:
        _write_json_atomic(metadata_path, metadata)
    except OSError as exc:
        failures.append(f"XDF validation metadata could not be written: {type(exc).__name__}: {exc}")
        result["status"] = "fail"
    result["failures"] = failures
    result["warnings"] = warnings
    result["metadata"] = metadata
    return result


class _CollectErrors(logging.Handler):
    def __init__(self, messages: list[str]) -> None:
        super().__init__(level=logging.ERROR)
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        last_error: OSError | None = None
        for delay in (0.0, 0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8):
            if delay:
                sleep(delay)
            try:
                temporary.replace(path)
                return
            except OSError as exc:
                if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in {5, 32, 33}:
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
    finally:
        temporary.unlink(missing_ok=True)
