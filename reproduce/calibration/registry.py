"""Calibration suite factory."""

from __future__ import annotations

from typing import Any

from reproduce.calibration.posterior_alpha import PosteriorAlphaCalibrationSuite
from reproduce.session import SessionPaths
from reproduce.telemetry import Telemetry


def make_calibration_suite(
    name: str,
    config: dict[str, Any],
    paths: SessionPaths,
    *,
    mode: str,
    record_eeg: bool,
    telemetry: Telemetry | None = None,
) -> PosteriorAlphaCalibrationSuite:
    normalized = (name or "none").lower()
    if normalized == "posterior_alpha":
        return PosteriorAlphaCalibrationSuite(config, paths, mode=mode, record_eeg=record_eeg, telemetry=telemetry)
    raise NotImplementedError(f"calibration suite '{name}' is not implemented")
