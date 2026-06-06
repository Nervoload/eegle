"""Reanalysis helpers for existing alpha calibration sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reproduce.analysis.alpha import run_alpha_validation
from reproduce.analysis.html_summary import generate_experiment_html_report
from reproduce.analysis.reports import analyze_session
from reproduce.calibration.posterior_alpha import AlphaCalibrationAnalyzer, PosteriorAlphaCalibrationSuite
from reproduce.session import paths_for_existing_session


def rerun_alpha_analysis(
    session_dir: str | Path,
    config: dict[str, Any] | None = None,
    *,
    update_parameters: bool = True,
    write_html: bool = True,
    max_raw_points: int = 120000,
    max_alpha_points: int = 12000,
) -> dict[str, Any]:
    """Rerun calibration and alpha reports from already-collected session data."""
    root = Path(session_dir).expanduser().resolve()
    paths = paths_for_existing_session(root)
    effective_config = _load_json(paths.parameters) or dict(config or {})
    analyzer = AlphaCalibrationAnalyzer(effective_config, paths)
    calibration = analyzer.run()

    updated_config = effective_config
    if update_parameters:
        suite = PosteriorAlphaCalibrationSuite(
            effective_config,
            paths,
            mode="analysis-only",
            record_eeg=paths.eeg_csv.exists(),
        )
        updated_config = suite.apply_result_to_config(calibration)

    alpha_summary = run_alpha_validation(root, updated_config)
    analysis_summary = analyze_session(root)
    html_summary = None
    if write_html:
        html_summary = generate_experiment_html_report(
            root,
            updated_config,
            max_raw_points=max_raw_points,
            max_alpha_points=max_alpha_points,
        )

    alpha_ok = alpha_summary.get("status") in {"ok", "ok_staged_event_features"}
    status = "ok" if alpha_ok and analysis_summary.get("processes", {}).get("status") != "failed" else "degraded"
    return {
        "schema_version": 1,
        "status": status,
        "session_dir": str(root),
        "parameters_updated": update_parameters,
        "calibration": {
            "status": calibration.get("status"),
            "online_band": calibration.get("online_band"),
            "accepted_peak": calibration.get("accepted_peak"),
            "fallback_used": calibration.get("fallback_used"),
            "fallback_reason": calibration.get("fallback_reason"),
            "summary_file": str(paths.calibration_result),
            "spectral_model_file": str(paths.calibration_spectral_model_json),
            "psd_file": str(paths.calibration_psd_csv),
        },
        "alpha_summary_file": str(root / "reports" / "alpha" / "alpha_summary.json"),
        "trial_alpha_file": str(root / "reports" / "alpha" / "trial_alpha.csv"),
        "offline_alpha_file": str(root / "reports" / "alpha" / "offline_alpha_timeseries.csv"),
        "analysis_summary_file": str(root / "reports" / "summary.json"),
        "html_report_file": None if html_summary is None else html_summary.get("html_file"),
        "alpha_status": alpha_summary.get("status"),
        "analysis_status": analysis_summary.get("analysis_status"),
        "display_timebase": None if html_summary is None else html_summary.get("display_timebase"),
    }


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
