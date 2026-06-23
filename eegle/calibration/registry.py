"""Calibration suite factory."""

from __future__ import annotations

from typing import Any

from eegle.calibration.posterior_alpha import PosteriorAlphaCalibrationSuite
from eegle.session import SessionPaths
from eegle.telemetry import Telemetry


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
