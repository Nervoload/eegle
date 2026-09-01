"""Structured, non-blocking recording-quality issue reports."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic_ns, sleep
from typing import Any, Iterable


QUALITY_REPORT_SCHEMA = "eegle.recording_quality.v1"
MAX_ISSUE_DETAILS = 100


def quality_issue(
    code: str,
    domain: str,
    summary: str,
    *,
    phase: str | None = None,
    session_dir: str | Path | None = None,
    channels: Iterable[str] = (),
    trial_indices: Iterable[int | None] = (),
    details: Iterable[dict[str, Any]] = (),
    count: int | None = None,
    observed: Any = None,
    expected: Any = None,
    recoverability: str = "unknown",
    suggested_action: str | None = None,
    detector: str | None = None,
    details_truncated: bool = False,
) -> dict[str, Any]:
    """Build one stable warning record without changing acquisition status."""

    detail_rows = [dict(row) for row in details]
    retained_details = detail_rows[:MAX_ISSUE_DETAILS]
    trials = sorted({int(value) for value in trial_indices if value is not None})
    return {
        "code": str(code),
        "domain": str(domain),
        "severity": "warning",
        "summary": str(summary),
        "phase": phase,
        "session_dir": None if session_dir is None else str(session_dir),
        "channels": list(dict.fromkeys(str(value) for value in channels if str(value))),
        "trial_indices": trials,
        "count": int(count) if count is not None else None,
        "observed": observed,
        "expected": expected,
        "details": retained_details,
        "details_truncated": bool(details_truncated or len(detail_rows) > len(retained_details)),
        "recoverability": recoverability,
        "suggested_action": suggested_action,
        "detector": detector,
    }


def collect_recording_quality_issues(
    result: dict[str, Any],
    *,
    phase: str,
    session_dir: str | Path | None = None,
    trial_rows: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Translate existing DSART/XDF diagnostics into stable issue records."""

    session = session_dir or result.get("session_dir")
    validation = dict(result.get("validation") or result)
    raw = dict(validation.get("raw_integrity") or {})
    xdf = dict(raw.get("xdf_integrity") or validation.get("xdf_integrity") or {})
    issues: list[dict[str, Any]] = []
    if xdf:
        issues.extend(_xdf_quality_issues(xdf, phase=phase, session_dir=session))
    marker = dict(validation.get("marker_integrity") or {})
    if marker:
        issues.extend(_marker_quality_issues(marker, phase=phase, session_dir=session))
    for field, code, summary, expected in (
        (
            "stimulus_duration_warning_trials",
            "trial.stimulus_duration_deviation",
            "Stimulus duration differed from the plan by more than the configured frame tolerance",
            "planned digit duration within 1.5 display frames",
        ),
        (
            "soi_warning_trials",
            "trial.soi_duration_deviation",
            "Stimulus-onset interval differed from the plan by more than the configured frame tolerance",
            "planned SOI within 1.5 display frames",
        ),
    ):
        trials = list(marker.get(field) or [])
        if trials:
            issues.append(
                quality_issue(
                    code,
                    "trial",
                    summary,
                    phase=phase,
                    session_dir=session,
                    trial_indices=trials,
                    count=len(trials),
                    expected=expected,
                    recoverability="likely_correctable",
                    suggested_action="Use the recorded flip/LSL timestamps or exclude the listed trials in timing-sensitive analyses.",
                    detector="dsart.task_marker_integrity",
                )
            )
    duration_rows = list(validation.get("boundary_duration_alignment") or [])
    bad_duration_rows = [
        row
        for row in duration_rows
        if abs(float(row.get("difference_seconds") or 0.0)) > 0.05
    ]
    if bad_duration_rows:
        issues.append(
            quality_issue(
                "baseline.boundary_duration_misalignment",
                "baseline",
                "Baseline monotonic and LSL boundary durations differ",
                phase=phase,
                session_dir=session,
                details=bad_duration_rows,
                count=len(bad_duration_rows),
                expected="absolute duration difference <= 0.05 seconds",
                recoverability="likely_correctable",
                suggested_action="Prefer synchronized LSL boundary timestamps for EEG alignment and review the listed phase durations.",
                detector="dsart.baseline_validation",
            )
        )
    durability = list(result.get("durability_warnings") or [])
    if durability:
        issues.append(
            quality_issue(
                "io.durability_sync_failed",
                "artifact",
                "One or more best-effort durable flush operations were denied",
                phase=phase,
                session_dir=session,
                details=({"warning": warning} for warning in durability),
                count=len(durability),
                recoverability="unknown",
                suggested_action="Retain the completed files, rerun post-run validation, and copy the run to backup storage.",
                detector="dynamic_sart.artifact_checkpoint",
            )
        )
    retained = _deduplicate_issues(issues)
    if trial_rows is not None:
        _attribute_timestamp_issues_to_trials(retained, list(trial_rows))
    return retained


def electrode_quality_issues(
    report: dict[str, Any],
    *,
    phase: str,
    session_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    issues = []
    for row in list(report.get("channels") or []):
        if row.get("quality_status") in {"good", "excluded"}:
            continue
        channel = str(row.get("channel_name") or "unknown")
        category = str(row.get("quality_status_category") or row.get("quality_status") or "unknown")
        issues.append(
            quality_issue(
                f"electrode.{category}",
                "electrode",
                f"Electrode/contact quality requires operator review on {channel}",
                phase=phase,
                session_dir=session_dir,
                channels=[channel],
                observed={
                    "raw_supplied_status": row.get("raw_supplied_status"),
                    "reported_status": row.get("reported_quality_status"),
                    "normalized_status": row.get("quality_status"),
                    "recognized": row.get("quality_status_recognized"),
                    "value": row.get("quality_or_impedance_value"),
                    "standard_deviation_native_units": row.get("standard_deviation_native_units"),
                    "peak_to_peak_native_units": row.get("peak_to_peak_native_units"),
                    "source": row.get("contact_quality_source"),
                },
                recoverability="likely_correctable",
                suggested_action="Review the live acquisition display and adjust the named contact if the operator considers it necessary.",
                detector="dsart.electrode_report",
            )
        )
    return issues


def publish_recording_quality_report(
    result: dict[str, Any],
    *,
    phase: str,
) -> Path | None:
    """Publish a sidecar after recording; callers keep publication failures nonfatal."""

    session_value = result.get("session_dir")
    if not session_value:
        return None
    session_dir = Path(str(session_value)).expanduser()
    issues = collect_recording_quality_issues(
        result,
        phase=phase,
        session_dir=session_dir,
        trial_rows=_load_trial_rows(session_dir),
    )
    result["quality_issues"] = issues
    validation = result.get("validation")
    if isinstance(validation, dict):
        validation["quality_issues"] = issues
    report = {
        "schema": QUALITY_REPORT_SCHEMA,
        "created_at": _now(),
        "phase": phase,
        "session_dir": str(session_dir),
        "authoritative_raw_format": "xdf",
        "status": "warning" if issues else "ok",
        "issue_count": len(issues),
        "quality_issues": issues,
    }
    path = session_dir / "reports" / "recording_quality_warnings.json"
    _write_json_atomic(path, report)
    result["quality_report_file"] = str(path)
    return path


def format_quality_issue(issue: dict[str, Any]) -> str:
    details = []
    channels = list(issue.get("channels") or [])
    trials = list(issue.get("trial_indices") or [])
    if channels:
        details.append("channels=" + ",".join(channels[:12]))
    if trials:
        preview = ",".join(str(value) for value in trials[:20])
        if len(trials) > 20:
            preview += f",+{len(trials) - 20} more"
        details.append("trials=" + preview)
    if issue.get("count") is not None:
        details.append(f"count={issue['count']}")
    observed = issue.get("observed")
    if isinstance(observed, dict):
        for key in (
            "largest_gap_seconds",
            "sample_fraction",
            "effective_sample_rate_hz",
            "maximum_delivery_latency_seconds",
        ):
            if observed.get(key) is not None:
                details.append(f"{key}={observed[key]}")
    elif observed is not None:
        details.append(f"observed={observed}")
    details.append(f"recoverability={issue.get('recoverability', 'unknown')}")
    suffix = "; ".join(details)
    return f"[{issue.get('code')}] {issue.get('summary')}" + (f" ({suffix})" if suffix else "")


def _xdf_quality_issues(
    xdf: dict[str, Any],
    *,
    phase: str,
    session_dir: str | Path | None,
) -> list[dict[str, Any]]:
    eeg = dict(xdf.get("eeg") or {})
    issues = []
    findings = [str(value) for value in [*list(xdf.get("failures") or []), *list(xdf.get("warnings") or [])]]
    timestamp_details = list(eeg.get("timestamp_issues") or [])
    timestamp_specs = (
        (
            "nonfinite_timestamp_count",
            "nonfinite_timestamp",
            "timestamp.nonfinite",
            "EEG contains non-finite source timestamps",
            "partially_recoverable",
            "Drop the affected samples and verify that the remaining synchronized timebase covers the required markers.",
        ),
        (
            "nonmonotonic_timestamp_count",
            "nonmonotonic_timestamp",
            "timestamp.nonmonotonic",
            "EEG source timestamps are not strictly increasing",
            "likely_correctable",
            "Review clock correction, then de-duplicate, reorder, or exclude the affected samples in post-processing.",
        ),
        (
            "sampling_gap_warning_count",
            ("sampling_gap", "timestamp_gap"),
            "timestamp.sampling_gap",
            "EEG contains one or more sampling gaps",
            "partially_recoverable",
            "Exclude gap-crossing epochs; interpolate only short gaps when scientifically justified.",
        ),
    )
    for count_field, detail_code, code, summary, recoverability, action in timestamp_specs:
        count = int(eeg.get(count_field) or 0)
        if not count:
            continue
        accepted_codes = {detail_code} if isinstance(detail_code, str) else set(detail_code)
        details = [row for row in timestamp_details if row.get("code") in accepted_codes]
        issues.append(
            quality_issue(
                code,
                "timestamp",
                summary,
                phase=phase,
                session_dir=session_dir,
                details=details,
                count=count,
                observed={
                    "largest_gap_seconds": eeg.get("largest_timestamp_gap_seconds"),
                    "estimated_missing_samples": eeg.get("estimated_missing_samples"),
                },
                recoverability=recoverability,
                suggested_action=action,
                detector="xdf_integrity",
                details_truncated=bool(eeg.get("timestamp_issues_truncated")),
            )
        )
    sample_fraction = eeg.get("sample_fraction_of_expected")
    minimum_sample_fraction = float(eeg.get("minimum_sample_fraction_warning") or 0.98)
    if sample_fraction is not None and float(sample_fraction) < minimum_sample_fraction:
        issues.append(
            quality_issue(
                "eeg.sample_retention_low",
                "eeg",
                "EEG retained fewer samples than the configured warning threshold",
                phase=phase,
                session_dir=session_dir,
                observed={
                    "sample_fraction": float(sample_fraction),
                    "finite_sample_count": eeg.get("finite_timestamp_count"),
                    "expected_sample_count": eeg.get("expected_sample_count"),
                },
                expected={"minimum_sample_fraction": minimum_sample_fraction},
                recoverability="partially_recoverable",
                suggested_action="Review gap intervals and exclude trials whose analysis windows cross missing data.",
                detector="xdf_integrity",
            )
        )
    effective_rate = eeg.get("effective_sample_rate_hz")
    expected_rate = eeg.get("expected_sample_rate_hz")
    rate_tolerance = float(eeg.get("effective_rate_warning_tolerance_fraction") or 0.02)
    if (
        effective_rate is not None
        and expected_rate is not None
        and float(expected_rate) > 0
        and abs(float(effective_rate) - float(expected_rate)) / float(expected_rate) > rate_tolerance
    ):
        issues.append(
            quality_issue(
                "eeg.effective_sample_rate_out_of_range",
                "eeg",
                "Measured EEG sampling rate differs from the configured rate",
                phase=phase,
                session_dir=session_dir,
                observed={"effective_sample_rate_hz": float(effective_rate)},
                expected={
                    "sample_rate_hz": float(expected_rate),
                    "tolerance_fraction": rate_tolerance,
                },
                recoverability="likely_correctable",
                suggested_action="Use recorded timestamps for resampling and inspect the reported gap intervals.",
                detector="xdf_integrity",
            )
        )
    signal = dict(eeg.get("signal_quality") or {})
    for row in list(signal.get("channels") or []):
        if row.get("status") != "warning":
            continue
        channel = str(row.get("channel_name") or "unknown")
        for warning in list(row.get("warnings") or []):
            recoverability = "partially_recoverable" if warning == "non_finite_samples" else "unknown"
            issue_count = {
                "non_finite_samples": row.get("nonfinite_sample_count"),
                "flat_channel": row.get("finite_sample_count"),
                "flatline_run": row.get("longest_constant_run_samples"),
                "possible_clipping": row.get("extreme_repeat_count"),
            }.get(str(warning))
            issues.append(
                quality_issue(
                    f"channel.{warning}",
                    "channel",
                    f"Channel {channel} reported {str(warning).replace('_', ' ')}",
                    phase=phase,
                    session_dir=session_dir,
                    channels=[channel],
                    count=int(issue_count) if issue_count is not None else 1,
                    observed={
                        "finite_sample_count": row.get("finite_sample_count"),
                        "nonfinite_sample_count": row.get("nonfinite_sample_count"),
                        "finite_sample_fraction": row.get("finite_sample_fraction"),
                        "standard_deviation_native_units": row.get("standard_deviation_native_units"),
                        "minimum_native_units": row.get("minimum_native_units"),
                        "maximum_native_units": row.get("maximum_native_units"),
                        "longest_constant_run_seconds": row.get("longest_constant_run_seconds"),
                        "extreme_repeat_fraction": row.get("extreme_repeat_fraction"),
                    },
                    recoverability=recoverability,
                    suggested_action="Review the live acquisition trace and exclude or repair only the affected channel/epochs in post-processing.",
                    detector="xdf_integrity",
                )
            )
    if signal.get("shape_mismatch"):
        issues.append(
            quality_issue(
                "eeg.sample_width_mismatch",
                "eeg",
                "One or more EEG sample rows did not match the declared channel count",
                phase=phase,
                session_dir=session_dir,
                details=list(signal.get("shape_mismatch_details") or []),
                count=int(signal.get("shape_mismatch_count") or 1),
                observed={"declared_channel_count": signal.get("channel_count")},
                recoverability="partially_recoverable",
                suggested_action="Exclude malformed rows and confirm channel-order continuity around them.",
                detector="xdf_integrity",
                details_truncated=bool(signal.get("shape_mismatch_details_truncated")),
            )
        )
    if any("eeg channel" in message.casefold() for message in findings):
        mapped = list(eeg.get("mapped_channel_names") or [])
        expected_channels = list(eeg.get("expected_channel_names") or [])
        affected_channels = [
            expected_name
            for index, expected_name in enumerate(expected_channels)
            if index >= len(mapped) or mapped[index] != expected_name
        ]
        issues.append(
            quality_issue(
                "channel.identity_contract_mismatch",
                "channel",
                "XDF channel count, labels, or order differs from the configured contract",
                phase=phase,
                session_dir=session_dir,
                channels=affected_channels,
                observed={"mapped_channel_names": mapped},
                expected={"expected_channel_names": expected_channels},
                recoverability="unknown",
                suggested_action="Confirm physical channel order from the acquisition display and resolve mapping before channel-level analysis.",
                detector="xdf_integrity",
            )
        )
    if any("marker labels/order do not exactly match" in message.casefold() for message in findings):
        markers = dict(xdf.get("markers") or {})
        labels = list(markers.get("labels") or [])
        receipt_labels = list(markers.get("receipt_labels") or [])
        issues.append(
            quality_issue(
                "marker.xdf_receipt_sequence_mismatch",
                "marker",
                "XDF and independent marker-receipt sequences differ",
                phase=phase,
                session_dir=session_dir,
                count=abs(len(labels) - len(receipt_labels)) or 1,
                observed={"xdf_marker_count": len(labels), "receipt_marker_count": len(receipt_labels)},
                expected="identical marker labels and order",
                recoverability="partially_recoverable",
                suggested_action="Reconcile XDF, receipt, and event ledgers by marker label, timestamp, and order.",
                detector="xdf_integrity",
            )
        )
    if any(
        token in message.casefold()
        for message in findings
        for token in ("synchronized eeg/marker boundaries disagree", "lacks synchronized eeg/marker timestamps")
    ):
        issues.append(
            quality_issue(
                "timestamp.xdf_clock_alignment_unavailable",
                "timestamp",
                "XDF EEG/marker clock alignment could not be fully established",
                phase=phase,
                session_dir=session_dir,
                observed=dict(xdf.get("recording_coverage") or {}),
                recoverability="partially_recoverable",
                suggested_action="Use retained clock-correction evidence where valid; otherwise exclude timing-sensitive epochs.",
                detector="xdf_integrity",
            )
        )
    return issues


def _marker_quality_issues(
    marker: dict[str, Any],
    *,
    phase: str,
    session_dir: str | Path | None,
) -> list[dict[str, Any]]:
    issues = []
    trial_specs = (
        (
            "missing_lsl_timestamp_trials",
            "marker.missing_lsl_timestamp",
            "marker",
            "Stimulus markers are missing LSL timestamps",
            "not_recoverable",
            "Use monotonic flip timestamps for non-EEG behavioral analysis and exclude affected EEG-locked trials.",
        ),
        (
            "missing_source_id_trials",
            "marker.missing_source_id",
            "marker",
            "Stimulus markers are missing the run-specific source identity",
            "partially_recoverable",
            "Reconcile by exact ledger order only if the retained stream identity and neighboring markers are unambiguous.",
        ),
        (
            "unscheduled_flip_trials",
            "marker.not_scheduled_on_flip",
            "marker",
            "Stimulus-onset markers were not recorded from a display-flip callback",
            "partially_recoverable",
            "Use the retained flip timestamps and treat affected trials cautiously in timing-sensitive analyses.",
        ),
        (
            "invalid_stimulus_offset_trials",
            "marker.invalid_stimulus_offset",
            "marker",
            "Stimulus-offset markers lack required timing or source metadata",
            "partially_recoverable",
            "Use trial-ledger display timestamps to reconstruct durations where the correspondence is unambiguous.",
        ),
        (
            "invalid_trial_timing_trials",
            "trial.invalid_timing_order",
            "trial",
            "Trial onset, offset, and response-close timestamps are not ordered",
            "partially_recoverable",
            "Reconstruct from event and marker ledgers or exclude the listed trials.",
        ),
        (
            "overlapping_trial_indices",
            "trial.overlapping_response_windows",
            "trial",
            "One or more trial response windows overlap the following stimulus",
            "likely_correctable",
            "Assign key events with the recorded window policy and review the listed trials.",
        ),
    )
    for field, code, domain, summary, recoverability, action in trial_specs:
        trials = list(marker.get(field) or [])
        if trials:
            issues.append(
                quality_issue(
                    code,
                    domain,
                    summary,
                    phase=phase,
                    session_dir=session_dir,
                    trial_indices=trials,
                    count=len(trials),
                    recoverability=recoverability,
                    suggested_action=action,
                    detector="dsart.task_marker_integrity",
                )
            )
    onset_count = marker.get("stimulus_onset_count")
    trial_count = marker.get("trial_row_count")
    if onset_count is not None and trial_count is not None and int(onset_count) != int(trial_count):
        issues.append(
            quality_issue(
                "marker.onset_trial_count_mismatch",
                "marker",
                "Stimulus-onset marker and trial-ledger counts differ",
                phase=phase,
                session_dir=session_dir,
                observed={"stimulus_onset_count": int(onset_count)},
                expected={"trial_row_count": int(trial_count)},
                recoverability="partially_recoverable",
                suggested_action="Reconcile the independent receipt, event, XDF marker, and trial ledgers by order and trial identity.",
                detector="dsart.task_marker_integrity",
            )
        )
    offset_count = marker.get("stimulus_offset_count")
    if offset_count is not None and trial_count is not None and int(offset_count) != int(trial_count):
        issues.append(
            quality_issue(
                "marker.offset_trial_count_mismatch",
                "marker",
                "Stimulus-offset marker and trial-ledger counts differ",
                phase=phase,
                session_dir=session_dir,
                observed={"stimulus_offset_count": int(offset_count)},
                expected={"trial_row_count": int(trial_count)},
                recoverability="partially_recoverable",
                suggested_action="Reconcile event and trial ledgers and reconstruct durations only for unambiguous trials.",
                detector="dsart.task_marker_integrity",
            )
        )
    receipt = dict(marker.get("independent_marker_receipt") or {})
    if receipt:
        receipt_specs = (
            ("timestamp_mismatch_indices", "marker.receipt_timestamp_mismatch", "Independent marker receipt timestamps differ from emitted timestamps", "partially_recoverable"),
            ("missing_delivery_time_indices", "marker.receipt_time_missing", "Independent markers lack local delivery-time evidence", "unknown"),
            ("negative_delivery_latency_indices", "marker.negative_delivery_latency", "Marker receipt appears earlier than modeled emission", "likely_correctable"),
            ("late_delivery_indices", "marker.late_delivery", "Marker delivery exceeded the configured latency warning threshold", "likely_correctable"),
        )
        for field, code, summary, recoverability in receipt_specs:
            indices = list(receipt.get(field) or [])
            if indices:
                issues.append(
                    quality_issue(
                        code,
                        "marker",
                        summary,
                        phase=phase,
                        session_dir=session_dir,
                        details=({"marker_ordinal": value} for value in indices),
                        count=len(indices),
                        observed={
                            "maximum_delivery_latency_seconds": receipt.get("maximum_delivery_latency_seconds"),
                        },
                        expected={
                            "delivery_latency_warning_seconds": receipt.get("delivery_latency_warning_seconds"),
                        },
                        recoverability=recoverability,
                        suggested_action="Reconcile marker order and timestamps across the event ledger, receipt ledger, and XDF stream.",
                        detector="dsart.marker_receipt_integrity",
                    )
                )
    return issues


def _deduplicate_issues(issues: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = []
    seen = set()
    for issue in issues:
        key = json.dumps(issue, sort_keys=True, separators=(",", ":"), default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def _attribute_timestamp_issues_to_trials(
    issues: list[dict[str, Any]],
    trial_rows: list[dict[str, Any]],
) -> None:
    """Attach trials only where a retained LSL interval proves overlap."""

    windows = []
    for row in trial_rows:
        trial_index = row.get("global_trial_index")
        onset = _finite_float(row.get("stimulus_onset_lsl"))
        close = _finite_float(
            row.get("scheduled_response_window_close_lsl")
            if row.get("scheduled_response_window_close_lsl") is not None
            else row.get("response_window_close_lsl")
        )
        if trial_index is None or onset is None or close is None or close < onset:
            continue
        windows.append((int(trial_index), onset, close))
    if not windows:
        return
    for issue in issues:
        if issue.get("domain") != "timestamp":
            continue
        supported_trials = set(issue.get("trial_indices") or [])
        for detail in list(issue.get("details") or []):
            values = [
                value
                for value in (
                    _finite_float(detail.get("previous_timestamp")),
                    _finite_float(detail.get("timestamp")),
                )
                if value is not None
            ]
            if not values:
                continue
            interval_start = min(values)
            interval_end = max(values)
            supported_trials.update(
                trial_index
                for trial_index, trial_start, trial_end in windows
                if trial_start <= interval_end and trial_end >= interval_start
            )
        issue["trial_indices"] = sorted(supported_trials)


def _load_trial_rows(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "events" / "dynamic_sart_trials.jsonl"
    if not path.is_file():
        return []
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def _finite_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric == numeric and abs(numeric) != float("inf") else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{monotonic_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        _replace_atomic_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_atomic_file(source: Path, target: Path) -> None:
    last_error: OSError | None = None
    for delay in (0.0, 0.05, 0.1, 0.2):
        if delay:
            sleep(delay)
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in {5, 32, 33}
            ):
                raise
            last_error = exc
    if last_error is not None:
        raise last_error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
