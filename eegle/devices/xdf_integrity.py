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

from eegle.devices.xdf_clock import (
    build_affine_xdf_clock_normalization,
    build_direct_xdf_clock_normalization,
)
from eegle.hardware.eeg_device import matching_eeg_streams
from eegle.hardware.profiles import configured_channel_types, expected_profile, mapped_channel_names


MAX_REPORTED_TIMESTAMP_ISSUES = 100


def validate_xdf_recording(
    session_dir: str | Path,
    *,
    required: bool,
    persist_report: bool = True,
) -> dict[str, Any]:
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
        return _finish(result, metadata_path, persist_report=persist_report)
    if xdf_path.stat().st_size <= 4:
        failures.append("authoritative XDF recording is empty")
        return _finish(result, metadata_path, persist_report=persist_report)
    try:
        import pyxdf
    except Exception as exc:
        failures.append(f"PyXDF import failed: {type(exc).__name__}: {exc}")
        return _finish(result, metadata_path, persist_report=persist_report)

    try:
        stream_infos = list(pyxdf.resolve_streams(str(xdf_path)))
    except Exception as exc:
        failures.append(f"XDF structure could not be read: {type(exc).__name__}: {exc}")
        return _finish(result, metadata_path, persist_report=persist_report)
    result["captured_streams"] = stream_infos
    if not stream_infos:
        failures.append("XDF does not contain any stream headers")
        return _finish(result, metadata_path, persist_report=persist_report)

    recorder_status = _load_json(root / "logs" / "processes" / "recorder.status.json") or {}
    recorder_summary = dict(recorder_status.get("summary") or {})
    selected = dict(
        metadata.get("selected_eeg_stream")
        or recorder_summary.get("stream")
        or {}
    )
    if selected:
        eeg_matches = [
            stream
            for stream in stream_infos
            if _matches_selected_eeg(stream, selected, eeg_config)
        ]
    else:
        configured_matches = matching_eeg_streams(stream_infos, eeg_config)
        eeg_matches = [stream for stream in stream_infos if stream in configured_matches]
        if len(eeg_matches) == 1:
            selected = dict(eeg_matches[0])
            warnings.append(
                "XDF selected-stream metadata was unavailable; the unique configured EEG "
                "identity was recovered from the XDF header"
            )
    metadata_warning = str(
        metadata.get("xdf_metadata_warning")
        or recorder_summary.get("xdf_metadata_warning")
        or ""
    ).strip()
    if metadata_warning:
        warnings.append(metadata_warning)
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
        return _finish(result, metadata_path, persist_report=persist_report)

    eeg_id = int(eeg_matches[0]["stream_id"])
    marker_id = int(marker_matches[0]["stream_id"])
    stats = {
        eeg_id: _new_stream_stats(),
        marker_id: _new_stream_stats(),
    }
    signal_stats = _new_signal_stats(int(eeg_matches[0].get("channel_count") or 0))
    marker_labels: list[str] = []
    maximum_gap = float(eeg_config.get("maximum_timestamp_gap_seconds", 0.1))
    expected_rate = float(eeg_config.get("expected_sample_rate_hz") or 0.0)
    quality_config = dict(eeg_config.get("quality_check", {}) or {})
    gap_warning_samples = max(
        1.0,
        float(quality_config.get("maximum_timestamp_gap_samples_warning", 5.0)),
    )

    def on_chunk(values: Any, stamps: Any, info: dict[str, Any], stream_id: int) -> tuple[Any, Any, dict[str, Any]]:
        stream_id = int(stream_id)
        timestamps = np.asarray(stamps, dtype=float)
        if stream_id in stats:
            _update_stats(
                stats[stream_id],
                timestamps,
                maximum_gap if stream_id == eeg_id else None,
                expected_rate=expected_rate if stream_id == eeg_id else None,
                gap_warning_samples=gap_warning_samples,
            )
        if stream_id == marker_id:
            marker_labels.extend(_marker_values(values))
            return values, stamps, info
        if stream_id == eeg_id and timestamps.size > 2:
            _update_signal_stats(signal_stats, values)
            indices = np.asarray([0, timestamps.size - 1], dtype=int)
            return values[indices], timestamps[indices], info
        if stream_id == eeg_id:
            _update_signal_stats(signal_stats, values)
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
        mapping_source = str(
            positional_evidence.get("mapping_source")
            or "operator_confirmed_position+xdf_stream_identity"
        )
        warnings.append(
            "XDF channel descriptor labels differed from the operator-confirmed positional names; "
            "the canonical value order was retained from the operator-confirmed positional contract "
            f"using {mapping_source}"
        )
    mapped_types = configured_channel_types(mapped_labels, eeg_config)
    expected_counts = [int(value) for value in eeg_config.get("expected_channel_counts", [])]
    observed_count = int(eeg_matches[0].get("channel_count") or 0)
    observed_rate = float(eeg_matches[0].get("nominal_srate") or 0.0)
    if expected_counts and observed_count not in expected_counts:
        failures.append(f"XDF EEG channel count is {observed_count}; expected one of {expected_counts}")
    if expected_rate and abs(observed_rate - expected_rate) >= 1.0:
        warnings.append(f"XDF nominal EEG rate is {observed_rate:g} Hz; expected {expected_rate:g} Hz")
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
        warnings.append("XDF EEG contains non-finite source timestamps")
    if eeg_stats["nonmonotonic_timestamp_count"]:
        warnings.append("XDF EEG contains nonmonotonic source timestamps")
    if eeg_stats["timestamp_gap_count"]:
        warnings.append(
            f"XDF EEG contains {eeg_stats['timestamp_gap_count']} timestamp gap(s) above "
            f"{maximum_gap:.6f} seconds"
        )
    if eeg_stats["sampling_gap_warning_count"]:
        warnings.append(
            f"XDF EEG contains {eeg_stats['sampling_gap_warning_count']} sampling gap(s), "
            f"about {eeg_stats['estimated_missing_samples']} missing sample(s)"
        )
    effective_rate = _effective_sample_rate(eeg_stats)
    expected_sample_count = (
        float((eeg_stats["last_timestamp"] - eeg_stats["first_timestamp"]) * expected_rate + 1.0)
        if expected_rate > 0
        and eeg_stats.get("first_timestamp") is not None
        and eeg_stats.get("last_timestamp") is not None
        and eeg_stats["last_timestamp"] > eeg_stats["first_timestamp"]
        else None
    )
    sample_fraction = (
        float(eeg_stats["finite_timestamp_count"] / expected_sample_count)
        if expected_sample_count
        else None
    )
    rate_tolerance_fraction = max(
        0.001,
        float(quality_config.get("effective_sample_rate_warning_tolerance_fraction", 0.02)),
    )
    minimum_sample_fraction = min(
        1.0,
        max(0.0, float(quality_config.get("minimum_sample_fraction_warning", 0.98))),
    )
    if (
        expected_rate > 0
        and effective_rate is not None
        and abs(effective_rate - expected_rate) / expected_rate > rate_tolerance_fraction
    ):
        warnings.append(
            f"XDF measured EEG rate is {effective_rate:.1f} Hz; expected about {expected_rate:g} Hz"
        )
    if sample_fraction is not None and sample_fraction < minimum_sample_fraction:
        warnings.append(f"XDF retained {sample_fraction:.1%} of the expected EEG samples")

    signal_quality = _finish_signal_stats(
        signal_stats,
        mapped_labels,
        sample_rate_hz=effective_rate or observed_rate or expected_rate,
        eeg_config=eeg_config,
    )
    if signal_quality.get("shape_mismatch"):
        warnings.append("XDF EEG sample rows did not consistently match the declared channel count")
    warning_channels = list(signal_quality.get("warning_channels") or [])
    if warning_channels:
        warning_rows = [
            row for row in signal_quality.get("channels", []) if row.get("status") == "warning"
        ]
        preview = ", ".join(
            f"{row.get('channel_name')} ({', '.join(row.get('warnings') or ['quality warning'])})"
            for row in warning_rows[:6]
        ) or ", ".join(warning_channels[:6])
        suffix = "" if len(warning_channels) <= 6 else f" (+{len(warning_channels) - 6} more)"
        warnings.append(
            f"XDF signal quality on {len(warning_channels)} channel(s): {preview}{suffix}"
        )

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
        "clock_alignment_mode": None,
        "clock_normalization": None,
        "clock_bridge_evidence": None,
        "csv_clock_bridge_evidence": None,
    }
    if eeg_synced.size and marker_synced.size:
        tolerance = 0.5
        first_eeg = float(eeg_synced[0])
        last_eeg = float(eeg_synced[-1])
        first_marker = float(marker_synced[0])
        last_marker = float(marker_synced[-1])
        start_lag = first_eeg - first_marker
        end_shortfall = last_marker - last_eeg
        synchronized_separation = max(
            abs(first_marker - first_eeg),
            abs(last_marker - last_eeg),
        )
        maximum_warning_shortfall = max(
            tolerance,
            float(recorder_config.get("maximum_xdf_tail_shortfall_warning_seconds", 2.0)),
        )
        coverage.update(
            {
                "status": "pass",
                "clock_alignment_mode": "pyxdf_synchronized_timestamps",
                "start_lag_seconds": start_lag,
                "end_shortfall_seconds": max(0.0, end_shortfall),
                "maximum_warning_shortfall_seconds": maximum_warning_shortfall,
            }
        )
        if (
            synchronized_separation > 60.0
            or start_lag > tolerance
            or end_shortfall > tolerance
        ):
            clock_bridge = _source_preserving_csv_coverage_evidence(
                root,
                eeg_matches[0],
                marker_matches[0],
                marker_sequence_matches=marker_labels == receipt_labels,
                xdf_first_source_timestamp=_optional_float(eeg_stats.get("first_timestamp")),
                xdf_last_source_timestamp=_optional_float(eeg_stats.get("last_timestamp")),
                marker_first_reference_timestamp=first_marker,
                marker_last_reference_timestamp=last_marker,
                boundary_tolerance_seconds=tolerance,
                maximum_warning_shortfall_seconds=maximum_warning_shortfall,
            )
            coverage["clock_bridge_evidence"] = clock_bridge
            coverage["csv_clock_bridge_evidence"] = clock_bridge
            # Retain the former result key for downstream readers while making its
            # broader clock-bridge role explicit above.
            coverage["csv_tail_evidence"] = clock_bridge
            if clock_bridge["status"] in {"pass", "warning"}:
                coverage["status"] = str(clock_bridge["status"])
                coverage["clock_alignment_mode"] = "recording_origin_normalized"
                coverage["clock_normalization"] = clock_bridge.get("clock_normalization")
                coverage["pyxdf_start_lag_seconds"] = start_lag
                coverage["pyxdf_end_shortfall_seconds"] = max(0.0, end_shortfall)
                coverage["start_lag_seconds"] = clock_bridge.get("xdf_start_lag_seconds")
                coverage["end_shortfall_seconds"] = clock_bridge.get("xdf_end_shortfall_seconds")
                coverage["pyxdf_synchronized_clock_status"] = (
                    "incompatible_origins"
                    if synchronized_separation > 60.0
                    else "boundary_mismatch"
                )
                coverage["synchronized_clock_separation_seconds"] = synchronized_separation
                evidence_warnings = list(clock_bridge.get("warnings") or [])
                if evidence_warnings:
                    evidence_label = (
                        "CSV mirror"
                        if clock_bridge.get("evidence_source") == "csv_mirror"
                        else "non-writing EEG heartbeat"
                    )
                    warnings.append(
                        "XDF timestamps were normalized to the first required marker using the "
                        f"{evidence_label}; " + "; ".join(evidence_warnings)
                    )
            else:
                coverage["status"] = "fail"
                evidence_detail = "; ".join(clock_bridge.get("reasons") or [])
                failures.append(
                    "XDF synchronized EEG/marker boundaries disagree "
                    f"(start lag {max(0.0, start_lag):.6f} seconds; end shortfall "
                    f"{max(0.0, end_shortfall):.6f} seconds; warning limit "
                    f"{maximum_warning_shortfall:.3f}). Independent clock-bridge evidence failed: "
                    f"{evidence_detail or clock_bridge.get('status')}"
                )
        else:
            coverage["clock_normalization"] = _direct_clock_normalization(
                first_eeg=first_eeg,
                last_eeg=last_eeg,
                first_marker=first_marker,
                last_marker=last_marker,
            )
    else:
        failures.append("XDF lacks synchronized EEG/marker timestamps needed to verify recording coverage")
        coverage["status"] = "fail"

    clock_normalization = dict(coverage.get("clock_normalization") or {})
    result["eeg"] = {
        "stream": eeg_matches[0],
        "sample_count": eeg_stats["sample_count"],
        "finite_timestamp_count": eeg_stats["finite_timestamp_count"],
        "first_source_timestamp": eeg_stats["first_timestamp"],
        "last_source_timestamp": eeg_stats["last_timestamp"],
        "nonfinite_timestamp_count": eeg_stats["nonfinite_timestamp_count"],
        "nonmonotonic_timestamp_count": eeg_stats["nonmonotonic_timestamp_count"],
        "timestamp_gap_count": eeg_stats["timestamp_gap_count"],
        "largest_timestamp_gap_seconds": eeg_stats["largest_timestamp_gap_seconds"],
        "sampling_gap_warning_count": eeg_stats["sampling_gap_warning_count"],
        "estimated_missing_samples": eeg_stats["estimated_missing_samples"],
        "timestamp_issues": list(eeg_stats["timestamp_issues"]),
        "timestamp_issue_count": int(eeg_stats["timestamp_issue_count"]),
        "timestamp_issues_truncated": bool(eeg_stats["timestamp_issues_truncated"]),
        "effective_sample_rate_hz": effective_rate,
        "expected_sample_rate_hz": expected_rate,
        "effective_rate_warning_tolerance_fraction": rate_tolerance_fraction,
        "expected_sample_count": expected_sample_count,
        "sample_fraction_of_expected": sample_fraction,
        "minimum_sample_fraction_warning": minimum_sample_fraction,
        "signal_quality": signal_quality,
        "original_channel_names": original_labels,
        "mapped_channel_names": mapped_labels,
        "expected_channel_names": expected_labels,
        "mapped_channel_types": mapped_types,
        "channel_mapping_source": mapping_source,
        "confirmed_positional_mapping_evidence": positional_evidence,
        "synchronized_first_timestamp": None if not eeg_synced.size else float(eeg_synced[0]),
        "synchronized_last_timestamp": None if not eeg_synced.size else float(eeg_synced[-1]),
        "normalized_first_timestamp": clock_normalization.get("normalized_eeg_start_seconds"),
        "normalized_last_timestamp": clock_normalization.get("normalized_eeg_end_seconds"),
    }
    result["markers"] = {
        "stream": marker_matches[0],
        "sample_count": stats[marker_id]["sample_count"],
        "labels": marker_labels,
        "receipt_labels": receipt_labels,
        "sequence_matches_receipt": marker_labels == receipt_labels,
        "synchronized_first_timestamp": None if not marker_synced.size else float(marker_synced[0]),
        "synchronized_last_timestamp": None if not marker_synced.size else float(marker_synced[-1]),
        "normalized_first_timestamp": clock_normalization.get("normalized_marker_start_seconds"),
        "normalized_last_timestamp": clock_normalization.get("normalized_marker_end_seconds"),
    }
    result["recording_coverage"] = coverage
    result["pyxdf_errors"] = log_messages
    return _finish(result, metadata_path, persist_report=persist_report)


def _new_stream_stats() -> dict[str, Any]:
    return {
        "sample_count": 0,
        "finite_timestamp_count": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "nonfinite_timestamp_count": 0,
        "nonmonotonic_timestamp_count": 0,
        "timestamp_gap_count": 0,
        "largest_timestamp_gap_seconds": 0.0,
        "sampling_gap_warning_count": 0,
        "estimated_missing_samples": 0,
        "timestamp_issues": [],
        "timestamp_issue_count": 0,
        "timestamp_issues_truncated": False,
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


def _update_stats(
    stats: dict[str, Any],
    timestamps: np.ndarray,
    maximum_gap: float | None,
    *,
    expected_rate: float | None = None,
    gap_warning_samples: float = 5.0,
) -> None:
    if timestamps.size == 0:
        return
    sample_offset = int(stats["sample_count"])
    finite = np.isfinite(timestamps)
    stats["sample_count"] += int(timestamps.size)
    stats["nonfinite_timestamp_count"] += int(np.sum(~finite))
    for index in np.flatnonzero(~finite):
        _record_timestamp_issue(
            stats,
            {
                "code": "nonfinite_timestamp",
                "sample_index": sample_offset + int(index),
                "timestamp": None,
            },
        )
    finite_indices = np.flatnonzero(finite)
    values = timestamps[finite]
    stats["finite_timestamp_count"] += int(values.size)
    if values.size == 0:
        return
    previous = stats.get("last_timestamp")
    if stats.get("first_timestamp") is None:
        stats["first_timestamp"] = float(values[0])
    transitions: list[tuple[float, float, int]] = []
    if previous is not None:
        transitions.append((float(previous), float(values[0]), sample_offset + int(finite_indices[0])))
    transitions.extend(
        (
            float(values[index - 1]),
            float(values[index]),
            sample_offset + int(finite_indices[index]),
        )
        for index in range(1, int(values.size))
    )
    differences = np.asarray(
        [current - prior for prior, current, _sample_index in transitions],
        dtype=float,
    )
    stats["nonmonotonic_timestamp_count"] += int(np.sum(differences <= 0))
    for prior, current, sample_index in transitions:
        difference = current - prior
        if difference <= 0:
            _record_timestamp_issue(
                stats,
                {
                    "code": "nonmonotonic_timestamp",
                    "sample_index": sample_index,
                    "previous_timestamp": prior,
                    "timestamp": current,
                    "difference_seconds": difference,
                },
            )
    if maximum_gap is not None:
        gaps = differences[differences > maximum_gap]
        stats["timestamp_gap_count"] += int(gaps.size)
        if gaps.size:
            stats["largest_timestamp_gap_seconds"] = max(
                float(stats["largest_timestamp_gap_seconds"]),
                float(np.max(gaps)),
            )
    if expected_rate is not None and expected_rate > 0:
        positive_differences = differences[differences > 0]
        missing = np.maximum(
            0,
            np.rint(positive_differences * expected_rate).astype(int) - 1,
        )
        stats["estimated_missing_samples"] += int(np.sum(missing))
        stats["sampling_gap_warning_count"] += int(
            np.sum(positive_differences > float(gap_warning_samples) / expected_rate)
        )
        warning_threshold = float(gap_warning_samples) / expected_rate
        for prior, current, sample_index in transitions:
            difference = current - prior
            if difference <= warning_threshold:
                continue
            _record_timestamp_issue(
                stats,
                {
                    "code": (
                        "timestamp_gap"
                        if maximum_gap is not None and difference > maximum_gap
                        else "sampling_gap"
                    ),
                    "sample_index": sample_index,
                    "previous_timestamp": prior,
                    "timestamp": current,
                    "gap_seconds": difference,
                    "estimated_missing_samples": max(
                        0,
                        int(round(difference * expected_rate)) - 1,
                    ),
                },
            )
    stats["last_timestamp"] = float(values[-1])


def _record_timestamp_issue(stats: dict[str, Any], issue: dict[str, Any]) -> None:
    stats["timestamp_issue_count"] = int(stats.get("timestamp_issue_count") or 0) + 1
    issues = stats.setdefault("timestamp_issues", [])
    if len(issues) < MAX_REPORTED_TIMESTAMP_ISSUES:
        issues.append(issue)
    else:
        stats["timestamp_issues_truncated"] = True


def _effective_sample_rate(stats: dict[str, Any]) -> float | None:
    first = stats.get("first_timestamp")
    last = stats.get("last_timestamp")
    count = int(stats.get("finite_timestamp_count") or 0)
    if first is None or last is None or count <= 1 or float(last) <= float(first):
        return None
    return float((count - 1) / (float(last) - float(first)))


def _new_signal_stats(channel_count: int) -> dict[str, Any]:
    count = max(0, int(channel_count))
    return {
        "channel_count": count,
        "row_count": 0,
        "nonfinite_value_count": 0,
        "finite_counts": np.zeros(count, dtype=np.int64),
        "means": np.zeros(count, dtype=float),
        "m2": np.zeros(count, dtype=float),
        "minimums": np.full(count, np.inf, dtype=float),
        "maximums": np.full(count, -np.inf, dtype=float),
        "minimum_counts": np.zeros(count, dtype=np.int64),
        "maximum_counts": np.zeros(count, dtype=np.int64),
        "last_values": np.full(count, np.nan, dtype=float),
        "current_constant_runs": np.zeros(count, dtype=np.int64),
        "longest_constant_runs": np.zeros(count, dtype=np.int64),
        "shape_mismatch": False,
        "shape_mismatch_count": 0,
        "shape_mismatch_details": [],
        "shape_mismatch_details_truncated": False,
    }


def _update_signal_stats(stats: dict[str, Any], values: Any) -> None:
    channel_count = int(stats.get("channel_count") or 0)
    if channel_count <= 0:
        return
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2 or matrix.shape[1] != channel_count:
        stats["shape_mismatch"] = True
        affected_rows = int(matrix.shape[0]) if matrix.ndim >= 1 else 1
        stats["shape_mismatch_count"] += affected_rows
        details = stats["shape_mismatch_details"]
        if len(details) < MAX_REPORTED_TIMESTAMP_ISSUES:
            details.append(
                {
                    "valid_row_offset": int(stats.get("row_count") or 0),
                    "observed_shape": list(matrix.shape),
                    "declared_channel_count": channel_count,
                    "affected_rows": affected_rows,
                }
            )
        else:
            stats["shape_mismatch_details_truncated"] = True
        return
    stats["row_count"] += int(matrix.shape[0])
    finite = np.isfinite(matrix)
    stats["nonfinite_value_count"] += int(np.sum(~finite))
    for index in range(channel_count):
        column = matrix[:, index]
        finite_values = column[np.isfinite(column)]
        if finite_values.size:
            old_count = int(stats["finite_counts"][index])
            chunk_count = int(finite_values.size)
            chunk_mean = float(np.mean(finite_values))
            chunk_m2 = float(np.sum((finite_values - chunk_mean) ** 2))
            total_count = old_count + chunk_count
            delta = chunk_mean - float(stats["means"][index])
            if old_count:
                stats["m2"][index] += chunk_m2 + delta * delta * old_count * chunk_count / total_count
                stats["means"][index] += delta * chunk_count / total_count
            else:
                stats["means"][index] = chunk_mean
                stats["m2"][index] = chunk_m2
            stats["finite_counts"][index] = total_count
            chunk_minimum = float(np.min(finite_values))
            chunk_maximum = float(np.max(finite_values))
            minimum_count = int(np.sum(finite_values == chunk_minimum))
            maximum_count = int(np.sum(finite_values == chunk_maximum))
            if chunk_minimum < stats["minimums"][index]:
                stats["minimums"][index] = chunk_minimum
                stats["minimum_counts"][index] = minimum_count
            elif chunk_minimum == stats["minimums"][index]:
                stats["minimum_counts"][index] += minimum_count
            if chunk_maximum > stats["maximums"][index]:
                stats["maximums"][index] = chunk_maximum
                stats["maximum_counts"][index] = maximum_count
            elif chunk_maximum == stats["maximums"][index]:
                stats["maximum_counts"][index] += maximum_count
        longest, current, last = _constant_run_stats(
            column,
            last_value=float(stats["last_values"][index]),
            current_run=int(stats["current_constant_runs"][index]),
        )
        stats["longest_constant_runs"][index] = max(
            int(stats["longest_constant_runs"][index]),
            longest,
        )
        stats["current_constant_runs"][index] = current
        stats["last_values"][index] = last


def _constant_run_stats(values: np.ndarray, *, last_value: float, current_run: int) -> tuple[int, int, float]:
    if values.size == 0:
        return current_run, current_run, last_value
    finite = np.isfinite(values)
    same = np.zeros(values.size, dtype=bool)
    if values.size > 1:
        same[1:] = finite[1:] & finite[:-1] & (values[1:] == values[:-1])
    starts = np.flatnonzero(~same)
    lengths = np.diff(np.append(starts, values.size))
    longest = int(np.max(lengths[finite[starts]])) if np.any(finite[starts]) else 0
    first_length = int(lengths[0]) if finite[0] else 0
    if current_run and np.isfinite(last_value) and finite[0] and values[0] == last_value:
        longest = max(longest, current_run + first_length)
    if not finite[-1]:
        next_current = 0
    elif len(starts) == 1 and finite[0] and np.isfinite(last_value) and values[0] == last_value:
        next_current = current_run + values.size
    else:
        next_current = int(lengths[-1])
    return longest, next_current, float(values[-1])


def _finish_signal_stats(
    stats: dict[str, Any],
    channel_names: list[str],
    *,
    sample_rate_hz: float,
    eeg_config: dict[str, Any],
) -> dict[str, Any]:
    quality_config = dict(eeg_config.get("quality_check", {}) or {})
    minimum_std = float(quality_config.get("minimum_channel_std", 1e-12))
    maximum_abs_value = quality_config.get("maximum_absolute_value")
    maximum_abs = None if maximum_abs_value is None else float(maximum_abs_value)
    flatline_seconds = max(0.0, float(quality_config.get("flatline_duration_warning_seconds", 1.0)))
    clipping_fraction_warning = min(
        1.0,
        max(0.0, float(quality_config.get("clipping_fraction_warning", 0.01))),
    )
    clipping_minimum_repeated = max(
        2,
        int(quality_config.get("clipping_minimum_repeated_samples", 10)),
    )
    excluded = {str(name) for name in eeg_config.get("quality_excluded_channel_names", [])}
    row_count = int(stats.get("row_count") or 0)
    channels = []
    for index in range(int(stats.get("channel_count") or 0)):
        name = channel_names[index] if index < len(channel_names) else f"channel_{index + 1}"
        finite_count = int(stats["finite_counts"][index])
        std = (
            float(np.sqrt(stats["m2"][index] / finite_count))
            if finite_count
            else None
        )
        minimum = float(stats["minimums"][index]) if finite_count else None
        maximum = float(stats["maximums"][index]) if finite_count else None
        max_abs = max(abs(minimum), abs(maximum)) if minimum is not None and maximum is not None else None
        flat = std is not None and std < minimum_std
        longest_run = int(stats["longest_constant_runs"][index])
        longest_run_seconds = longest_run / sample_rate_hz if sample_rate_hz > 0 else None
        long_flatline = bool(
            longest_run_seconds is not None
            and flatline_seconds > 0
            and longest_run_seconds >= flatline_seconds
        )
        extreme_repeats = (
            int(stats["minimum_counts"][index]) + int(stats["maximum_counts"][index])
            if minimum != maximum
            else finite_count
        )
        clipping_fraction = float(extreme_repeats / finite_count) if finite_count else None
        clipping = bool(
            not flat
            and extreme_repeats >= clipping_minimum_repeated
            and clipping_fraction is not None
            and clipping_fraction >= clipping_fraction_warning
        )
        channel_warnings = []
        if finite_count < row_count:
            channel_warnings.append("non_finite_samples")
        if flat:
            channel_warnings.append("flat_channel")
        elif long_flatline:
            channel_warnings.append("flatline_run")
        if clipping:
            channel_warnings.append("possible_clipping")
        if maximum_abs is not None and max_abs is not None and max_abs > maximum_abs:
            channel_warnings.append("extreme_amplitude")
        is_excluded = name in excluded
        channels.append(
            {
                "channel_index": index + 1,
                "channel_name": name,
                "finite_sample_count": finite_count,
                "nonfinite_sample_count": max(0, row_count - finite_count),
                "finite_sample_fraction": float(finite_count / row_count) if row_count else 0.0,
                "standard_deviation_native_units": std,
                "minimum_native_units": minimum,
                "maximum_native_units": maximum,
                "longest_constant_run_samples": longest_run,
                "longest_constant_run_seconds": longest_run_seconds,
                "possible_clipping": clipping,
                "extreme_repeat_count": extreme_repeats,
                "extreme_repeat_fraction": clipping_fraction,
                "status": "excluded" if is_excluded else ("warning" if channel_warnings else "good"),
                "warnings": [] if is_excluded else channel_warnings,
            }
        )
    return {
        "sample_count": row_count,
        "channel_count": int(stats.get("channel_count") or 0),
        "nonfinite_value_count": int(stats.get("nonfinite_value_count") or 0),
        "shape_mismatch": bool(stats.get("shape_mismatch", False)),
        "shape_mismatch_count": int(stats.get("shape_mismatch_count") or 0),
        "shape_mismatch_details": list(stats.get("shape_mismatch_details") or []),
        "shape_mismatch_details_truncated": bool(
            stats.get("shape_mismatch_details_truncated", False)
        ),
        "channels": channels,
        "warning_channels": [row["channel_name"] for row in channels if row["status"] == "warning"],
    }


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
    csv_evidence_available = (
        raw_metadata.get("status") == "stopped"
        and list(raw_stream.get("channel_names") or []) == expected_labels
        and raw_stream.get("channel_value_order_changed") is False
        and contract.get("channel_value_order_modified") is False
    )
    identity_fields = ("name", "type", "source_id", "hostname", "channel_count")
    identity_mismatches = []
    for field in identity_fields:
        selected_value = selected_xdf_stream.get(field)
        if selected_value in {None, ""}:
            continue
        if str(raw_stream.get(field)) != str(selected_value):
            identity_mismatches.append(field)
    if csv_evidence_available and identity_mismatches:
        reasons.append(
            "XDF and CSV mirror LSL identities differ in " + ", ".join(identity_mismatches)
        )
    selected_identity_available = bool(
        str(selected_xdf_stream.get("name") or "").strip()
        and str(selected_xdf_stream.get("type") or "").strip()
        and str(selected_xdf_stream.get("source_id") or "").strip()
    )
    if not csv_evidence_available and not selected_identity_available:
        reasons.append("neither CSV evidence nor a stable selected XDF stream identity is available")
    mapping_source = (
        "operator_confirmed_position+csv_mirror_identity"
        if csv_evidence_available
        else "operator_confirmed_position+xdf_stream_identity"
    )
    return {
        "status": "fail" if reasons else "pass",
        "configured_mapping_source": configured_source,
        "xdf_selected_stream": {
            field: selected_xdf_stream.get(field) for field in identity_fields
        },
        "csv_mirror_stream": {field: raw_stream.get(field) for field in identity_fields},
        "csv_evidence_available": csv_evidence_available,
        "mapping_source": mapping_source,
        "warnings": (
            []
            if csv_evidence_available
            else ["CSV mirror evidence was unavailable; positional mapping used the selected XDF stream identity"]
        ),
        "reasons": reasons,
    }


def _source_preserving_csv_coverage_evidence(
    root: Path,
    selected_xdf_stream: dict[str, Any],
    selected_marker_stream: dict[str, Any],
    *,
    marker_sequence_matches: bool,
    xdf_first_source_timestamp: float | None,
    xdf_last_source_timestamp: float | None,
    marker_first_reference_timestamp: float,
    marker_last_reference_timestamp: float,
    boundary_tolerance_seconds: float,
    maximum_warning_shortfall_seconds: float,
) -> dict[str, Any]:
    """Bridge an EEG source clock to one recording-relative PC-local clock."""

    reasons: list[str] = []
    evidence_warnings: list[str] = []
    raw_metadata, evidence_source = _clock_bridge_metadata(root)
    raw_stream = dict(raw_metadata.get("stream") or {})
    contract = dict(raw_metadata.get("raw_sample_contract") or {})
    marker_metadata = _load_json(root / "raw" / "lsl_markers_received_metadata.json") or {}
    raw_first_source = _optional_float(raw_metadata.get("first_source_lsl_timestamp"))
    raw_last_source = _optional_float(raw_metadata.get("last_source_lsl_timestamp"))
    raw_first_local = _optional_float(raw_metadata.get("first_local_received_lsl_timestamp"))
    raw_last_local = _optional_float(raw_metadata.get("last_local_received_lsl_timestamp"))
    marker_span = _marker_receipt_clock_span(root / "raw" / "lsl_markers_received.csv")
    marker_first_source = marker_span["first_lsl_timestamp"]
    marker_last_source = marker_span["last_lsl_timestamp"]
    marker_first_local = marker_span["first_local_received_lsl_timestamp"]
    marker_last_local = marker_span["last_local_received_lsl_timestamp"]
    if raw_metadata.get("status") != "stopped":
        reasons.append("EEG clock-bridge observer did not stop cleanly")
    if int(raw_metadata.get("sample_count") or 0) <= 0:
        reasons.append("EEG clock-bridge observer contains no samples")
    if int(raw_metadata.get("timestamp_gap_count") or 0) != 0:
        reasons.append("EEG clock-bridge observer contains timestamp gaps")
    if int(raw_metadata.get("nonmonotonic_timestamp_count") or 0) != 0:
        reasons.append("EEG clock-bridge observer contains nonmonotonic timestamps")
    if evidence_source == "csv_mirror":
        if contract.get("source_timestamp_retained") is not True:
            reasons.append("CSV EEG mirror is not source-preserving")
        if contract.get("amplitude_samples_modified") is not False:
            reasons.append("CSV EEG mirror does not prove unmodified amplitudes")
        if contract.get("channel_value_order_modified") is not False:
            reasons.append("CSV EEG mirror does not prove unmodified channel order")
    if not marker_sequence_matches:
        reasons.append("XDF and independent marker sequences differ")
    required_clock_values = {
        "EEG first source timestamp": raw_first_source,
        "EEG last source timestamp": raw_last_source,
        "EEG first local LSL receipt timestamp": raw_first_local,
        "EEG last local LSL receipt timestamp": raw_last_local,
        "first marker LSL timestamp": marker_first_source,
        "last marker LSL timestamp": marker_last_source,
        "first synchronized marker timestamp": marker_first_reference_timestamp,
        "last synchronized marker timestamp": marker_last_reference_timestamp,
        "first marker local LSL receipt timestamp": marker_first_local,
        "last marker local LSL receipt timestamp": marker_last_local,
        "XDF first source timestamp": xdf_first_source_timestamp,
        "XDF last source timestamp": xdf_last_source_timestamp,
    }
    missing_clock_values = [name for name, value in required_clock_values.items() if value is None]
    if missing_clock_values:
        reasons.append("clock-bridge timestamps are unavailable: " + ", ".join(missing_clock_values))
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
            "XDF and EEG clock-bridge identities differ in " + ", ".join(identity_mismatches)
        )

    marker_start_source_estimate: float | None = None
    marker_end_source_estimate: float | None = None
    xdf_start_lag: float | None = None
    xdf_end_shortfall: float | None = None
    source_to_local_scale: float | None = None
    source_to_local_offset: float | None = None
    clock_normalization: dict[str, Any] | None = None
    if not missing_clock_values:
        assert raw_first_source is not None
        assert raw_last_source is not None
        assert raw_first_local is not None
        assert raw_last_local is not None
        assert marker_first_source is not None
        assert marker_last_source is not None
        assert marker_first_local is not None
        assert marker_last_local is not None
        assert xdf_first_source_timestamp is not None
        assert xdf_last_source_timestamp is not None
        if raw_first_local > marker_first_local + boundary_tolerance_seconds:
            reasons.append(
                "EEG local receipt begins after the first marker by "
                f"{raw_first_local - marker_first_local:.6f} seconds"
            )
        if raw_last_local < marker_last_local - boundary_tolerance_seconds:
            reasons.append(
                "EEG local receipt ends before the last marker by "
                f"{marker_last_local - raw_last_local:.6f} seconds"
            )
        source_span = raw_last_source - raw_first_source
        local_span = raw_last_local - raw_first_local
        if source_span <= 0.0 or local_span <= 0.0:
            reasons.append("EEG source/local clock anchors do not have a positive span")
        else:
            source_to_local_scale = local_span / source_span
            source_to_local_offset = raw_first_local - source_to_local_scale * raw_first_source
            marker_start_source_estimate = (
                marker_first_reference_timestamp - source_to_local_offset
            ) / source_to_local_scale
            marker_end_source_estimate = (
                marker_last_reference_timestamp - source_to_local_offset
            ) / source_to_local_scale
            xdf_first_local = (
                source_to_local_scale * xdf_first_source_timestamp + source_to_local_offset
            )
            xdf_last_local = (
                source_to_local_scale * xdf_last_source_timestamp + source_to_local_offset
            )
            xdf_start_lag = xdf_first_local - marker_first_reference_timestamp
            xdf_end_shortfall = marker_last_reference_timestamp - xdf_last_local
            clock_normalization = build_affine_xdf_clock_normalization(
                evidence_source=evidence_source,
                origin_local_lsl_timestamp=marker_first_reference_timestamp,
                scale=source_to_local_scale,
                offset_seconds=source_to_local_offset,
                eeg_start_source_timestamp=xdf_first_source_timestamp,
                eeg_end_source_timestamp=xdf_last_source_timestamp,
                marker_end_local_lsl_timestamp=marker_last_reference_timestamp,
            )
            if xdf_start_lag > boundary_tolerance_seconds:
                reasons.append(
                    "XDF EEG source data starts after the first required marker by "
                    f"{xdf_start_lag:.6f} seconds"
                )
            if xdf_end_shortfall > maximum_warning_shortfall_seconds:
                reasons.append(
                    "XDF EEG source data ends before the last required marker by "
                    f"{xdf_end_shortfall:.6f} seconds"
                )
            elif xdf_end_shortfall > boundary_tolerance_seconds:
                evidence_warnings.append(
                    "XDF finalization omitted a short source-data tail of "
                    f"{xdf_end_shortfall:.6f} seconds retained by the clock-bridge observer"
                )
    status = "fail" if reasons else ("warning" if evidence_warnings else "pass")
    return {
        "status": status,
        "evidence_source": evidence_source,
        "clock_normalization": clock_normalization,
        "eeg_first_source_lsl_timestamp": raw_first_source,
        "eeg_last_source_lsl_timestamp": raw_last_source,
        "eeg_first_local_received_lsl_timestamp": raw_first_local,
        "eeg_last_local_received_lsl_timestamp": raw_last_local,
        "first_marker_lsl_timestamp": marker_first_source,
        "last_marker_lsl_timestamp": marker_last_source,
        "first_marker_reference_timestamp": marker_first_reference_timestamp,
        "last_marker_reference_timestamp": marker_last_reference_timestamp,
        "eeg_source_to_local_scale": source_to_local_scale,
        "eeg_source_to_local_offset_seconds": source_to_local_offset,
        # Compatibility aliases retained for existing validation readers.
        "csv_first_source_lsl_timestamp": raw_first_source,
        "csv_last_source_lsl_timestamp": raw_last_source,
        "csv_first_local_received_lsl_timestamp": raw_first_local,
        "csv_last_local_received_lsl_timestamp": raw_last_local,
        "first_marker_local_received_lsl_timestamp": marker_first_local,
        "last_marker_local_received_lsl_timestamp": marker_last_local,
        "estimated_first_marker_source_timestamp": marker_start_source_estimate,
        "estimated_last_marker_source_timestamp": marker_end_source_estimate,
        "xdf_first_source_timestamp": xdf_first_source_timestamp,
        "xdf_last_source_timestamp": xdf_last_source_timestamp,
        "xdf_start_lag_seconds": None if xdf_start_lag is None else max(0.0, xdf_start_lag),
        "xdf_end_shortfall_seconds": (
            None if xdf_end_shortfall is None else max(0.0, xdf_end_shortfall)
        ),
        "marker_receipt_status": marker_metadata.get("status"),
        "xdf_stream": {field: selected_xdf_stream.get(field) for field in identity_fields},
        "xdf_marker_stream": {
            field: selected_marker_stream.get(field) for field in identity_fields
        },
        "csv_stream": {field: raw_stream.get(field) for field in identity_fields},
        "reasons": reasons,
        "warnings": evidence_warnings,
    }


def _clock_bridge_metadata(root: Path) -> tuple[dict[str, Any], str]:
    csv_metadata = _load_json(root / "raw" / "eeg_metadata.json") or {}
    if csv_metadata.get("status") == "stopped" and int(csv_metadata.get("sample_count") or 0) > 0:
        return csv_metadata, "csv_mirror"

    xdf_metadata = _load_json(root / "raw" / "xdf_metadata.json") or {}
    heartbeat = dict(xdf_metadata.get("lsl_sample_heartbeat") or {})
    if heartbeat:
        heartbeat["stream"] = dict(
            heartbeat.get("stream")
            or xdf_metadata.get("selected_eeg_stream")
            or {}
        )
        return heartbeat, "lsl_sample_heartbeat"
    return csv_metadata, "unavailable"


def _direct_clock_normalization(
    *,
    first_eeg: float,
    last_eeg: float,
    first_marker: float,
    last_marker: float,
) -> dict[str, Any]:
    return build_direct_xdf_clock_normalization(
        first_eeg=first_eeg,
        last_eeg=last_eeg,
        first_marker=first_marker,
        last_marker=last_marker,
    )


def _marker_receipt_clock_span(path: Path) -> dict[str, float | None]:
    result: dict[str, float | None] = {
        "first_lsl_timestamp": None,
        "last_lsl_timestamp": None,
        "first_local_received_lsl_timestamp": None,
        "last_local_received_lsl_timestamp": None,
    }
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                marker_timestamp = _optional_float(row.get("lsl_timestamp"))
                local_received = _optional_float(row.get("local_received_lsl_timestamp"))
                if marker_timestamp is not None:
                    if result["first_lsl_timestamp"] is None:
                        result["first_lsl_timestamp"] = marker_timestamp
                    result["last_lsl_timestamp"] = marker_timestamp
                if local_received is not None:
                    if result["first_local_received_lsl_timestamp"] is None:
                        result["first_local_received_lsl_timestamp"] = local_received
                    result["last_local_received_lsl_timestamp"] = local_received
    except OSError:
        pass
    return result


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


def _finish(
    result: dict[str, Any],
    metadata_path: Path,
    *,
    persist_report: bool,
) -> dict[str, Any]:
    failures = list(result.get("failures") or [])
    warnings = list(result.get("warnings") or [])
    result["status"] = "fail" if failures else ("warning" if warnings else "pass")
    metadata = _load_json(metadata_path) or {}
    metadata["validation"] = {
        key: value
        for key, value in result.items()
        if key not in {"metadata"}
    }
    if persist_report:
        try:
            _write_json_atomic(metadata_path, metadata)
        except Exception as exc:
            warnings.append(
                f"XDF validation report could not be written ({type(exc).__name__}: {exc}); "
                "the raw XDF was retained"
            )
            result["status"] = "fail" if failures else "warning"
    result["validation_report_persisted"] = bool(persist_report)
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
