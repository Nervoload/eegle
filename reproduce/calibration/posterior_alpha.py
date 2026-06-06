"""Posterior alpha calibration suite for Go/No-go sessions."""

from __future__ import annotations

import csv
import json
import math
import os
import random
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import numpy as np

from reproduce.config import merged_config, write_config
from reproduce.hardware.enobio import expected_profile, mapped_channel_names
from reproduce.lsl import LslMarkerOutlet, NullMarkerOutlet, lsl_local_clock, session_marker_source_id
from reproduce.realtime.alpha import (
    AlphaPeakCandidate,
    DEFAULT_POSTERIOR_CHANNELS,
    accept_alpha_candidate,
    bounded_alpha_band,
    fallback_alpha_band,
    sliding_window_psd_alpha_power,
    spectral_peak_candidates,
)
from reproduce.runtime import apply_pyglet_macos_notification_patch, ensure_runtime_environment
from reproduce.session import SessionPaths
from reproduce.tasks.go_nogo import _random_go_stimulus, _resolve_no_go, _star_vertices
from reproduce.telemetry import Telemetry


@dataclass(frozen=True)
class CalibrationPhase:
    name: str
    display_name: str
    duration_seconds: float
    instructions: str


class PosteriorAlphaCalibrationSuite:
    """Run and analyze a posterior alpha calibration protocol."""

    name = "posterior_alpha"

    def __init__(
        self,
        config: dict[str, Any],
        paths: SessionPaths,
        *,
        mode: str,
        record_eeg: bool,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.mode = mode
        self.record_eeg = record_eeg
        self.telemetry = telemetry or Telemetry.from_config(config, paths, component="calibration.posterior_alpha")
        self.cfg = _posterior_alpha_config(config)
        self.phases = _calibration_phases(self.cfg)

    def run(self) -> dict[str, Any]:
        self.paths.calibration.mkdir(parents=True, exist_ok=True)
        self._write_metadata()
        self.telemetry.emit(
            "calibration.start",
            level="default",
            message="Posterior alpha calibration starting",
            metadata={"suite": self.name, "mode": self.mode, "record_eeg": self.record_eeg},
        )
        if self.mode == "psychopy" and self.record_eeg:
            self._run_psychopy_protocol()
        else:
            self._run_synthetic_protocol()
        analyzer = AlphaCalibrationAnalyzer(self.config, self.paths, telemetry=self.telemetry)
        result = analyzer.run()
        self.telemetry.emit(
            "calibration.complete",
            level="default",
            message=f"Posterior alpha calibration complete: {result.get('status')}",
            metadata=result,
        )
        return result

    def apply_result_to_config(self, result: dict[str, Any]) -> dict[str, Any]:
        """Return a config with realtime alpha measurement enabled for the task."""
        online_band = dict(result.get("online_band") or fallback_alpha_band("missing_calibration_result").as_dict())
        realtime_enabled = bool(self.record_eeg)
        event_features = dict(self.config.get("realtime", {}).get("event_features", {}))
        event_features_requested = bool(event_features.get("enabled", False))
        if event_features_requested:
            event_features.update(
                {
                    "enabled": realtime_enabled,
                    "alpha_band": online_band,
                    "fixed_reference_channels": result.get("fixed_reference_channels", []),
                    "calibration_id": str(self.paths.calibration_result),
                }
            )
        updates = {
            "hardware": {
                "markers": {
                    "required_for_realtime": realtime_enabled,
                },
            },
            "experiment": {
                "components": {
                    "realtime_processor": "lsl" if realtime_enabled else "disabled",
                    "feedback": "disabled",
                }
            },
            "processes": {
                "realtime_processor": {
                    "enabled": realtime_enabled,
                    "backend": "lsl" if realtime_enabled else "disabled",
                    "preprocessor": "causal_bandpass_notch",
                    "model": self.config.get("realtime", {}).get("model", {}).get("kind", "erp_peak_baseline"),
                },
                "feedback": {
                    "enabled": False,
                    "backend": "disabled",
                },
            },
            "realtime": {
                "enabled": realtime_enabled,
                "event_features": event_features,
                "alpha": {
                    "enabled": realtime_enabled and not event_features_requested,
                    "method": "hilbert",
                    "calibration_result_path": str(self.paths.calibration_result),
                    "band": online_band,
                    "posterior_channels": result.get("posterior_channels", list(DEFAULT_POSTERIOR_CHANNELS)),
                    "baseline_mean_power": result.get("baseline_mean_power"),
                    "baseline_std_power": result.get("baseline_std_power"),
                    "calibration_status": result.get("status"),
                    "window_seconds": self.cfg.get("online_window_seconds", 1.0),
                    "step_seconds": self.cfg.get("online_step_seconds", 0.1),
                    "smoothing_seconds": self.cfg.get("online_smoothing_seconds", 0.2),
                    "artifact_gate": self.cfg.get("online_artifact_gate", {}),
                },
                "decision_policy": {
                    "kind": "observe_only",
                    "enabled": True,
                    "allow_task_adaptation": False,
                },
                "feedback": {
                    "allow_task_adaptation": False,
                    "client": {
                        "enabled": False,
                    },
                },
            },
        }
        updated = _normalize_specparam_config(merged_config(self.config, updates))
        write_config(updated, self.paths.parameters)
        return updated

    def _run_psychopy_protocol(self) -> None:
        ensure_runtime_environment(self.config.get("runtime", {}).get("runtime_cache_dir", ".runtime"))
        from psychopy import core, event, visual

        apply_pyglet_macos_notification_patch()
        display = self.config.get("hardware", {}).get("display", {})
        marker_outlet = _make_marker_outlet(self.config.get("hardware", {}).get("markers", {}), self.paths)
        win = visual.Window(
            fullscr=bool(display.get("full_screen", False)),
            screen=int(display.get("screen_index", 0)),
            size=tuple(display.get("size", [1000, 700])),
            winType=str(display.get("win_type", "pyglet")),
            units=display.get("units", "height"),
            color=display.get("background_color", "black"),
            allowGUI=bool(display.get("allow_gui", True)),
        )
        try:
            for phase in self.phases:
                if phase.name == "go_nogo_practice":
                    self._run_go_nogo_practice(win, visual, event, core, marker_outlet, phase)
                else:
                    self._run_rest_phase(win, visual, event, core, marker_outlet, phase)
        finally:
            marker_outlet.close()
            win.close()

    def _run_rest_phase(self, win: Any, visual: Any, event: Any, core: Any, marker_outlet: Any, phase: CalibrationPhase) -> None:
        if not _show_instruction_screen(win, visual, event, core, phase.instructions):
            self._write_event("instruction_abort", phase.name)
            return
        self._write_event("instruction_confirmed", phase.name)
        _countdown(win, visual, phase.name, self.cfg.get("countdown_seconds", 5), self._write_event, marker_outlet)
        if phase.name == "eyes_open_fixation":
            visual.TextStim(win, text="+", height=0.12, color="white").draw()
        win.callOnFlip(self._write_event, "recording_start", phase.name, duration_seconds=phase.duration_seconds)
        win.callOnFlip(_push_marker_now, marker_outlet, f"calibration_{phase.name}_start")
        win.flip()
        start = monotonic()
        while monotonic() - start < phase.duration_seconds:
            if phase.name == "eyes_open_fixation":
                visual.TextStim(win, text="+", height=0.12, color="white").draw()
                win.flip()
            else:
                win.flip()
            if event.getKeys(keyList=["escape", "q"]):
                self._write_event("phase_abort", phase.name)
                break
            sleep(0.02)
        self._write_event("recording_end", phase.name)
        _push_marker_now(marker_outlet, f"calibration_{phase.name}_end")

    def _run_go_nogo_practice(self, win: Any, visual: Any, event: Any, core: Any, marker_outlet: Any, phase: CalibrationPhase) -> None:
        if not _show_instruction_screen(win, visual, event, core, phase.instructions):
            self._write_event("instruction_abort", phase.name)
            return
        self._write_event("instruction_confirmed", phase.name)
        _countdown(win, visual, phase.name, self.cfg.get("countdown_seconds", 5), self._write_event, marker_outlet)
        trials = int(self.cfg.get("go_nogo_practice_trials", 100))
        task_cfg = dict(self.config.get("tasks", {}).get("go_nogo", {}))
        no_go = _resolve_no_go(task_cfg)
        response_keys = list(task_cfg.get("response_keys", ["space"]))
        escape_keys = list(dict.fromkeys([*task_cfg.get("escape_keys", ["escape"]), "q"]))
        stimulus_seconds = float(task_cfg.get("stimulus_seconds", 0.8))
        isi_seconds = float(task_cfg.get("isi_seconds", 0.7))
        self._write_event("recording_start", phase.name, duration_seconds=phase.duration_seconds, trials=trials)
        _push_marker_now(marker_outlet, f"calibration_{phase.name}_start")
        for index in range(1, trials + 1):
            stimulus = _practice_stimulus(task_cfg, no_go)
            _draw_practice_stimulus(win, visual, stimulus)
            onset_holder: dict[str, float] = {}
            rt_clock = core.Clock()
            event.clearEvents(eventType="keyboard")
            win.callOnFlip(rt_clock.reset)
            win.callOnFlip(
                _write_practice_stimulus_event,
                self._write_event,
                marker_outlet,
                onset_holder,
                "stimulus_onset",
                phase.name,
                index,
                stimulus,
            )
            win.flip()
            presses: list[dict[str, Any]] = []
            aborted = False
            timer = core.CountdownTimer(stimulus_seconds)
            while timer.getTime() > 0:
                for key, rt in event.getKeys(timeStamped=rt_clock):
                    if key in escape_keys:
                        aborted = True
                    press = {
                        "key": key,
                        "rt_seconds": float(rt),
                        "is_response_key": key in response_keys,
                    }
                    presses.append(press)
                    self._write_event(
                        "button_press",
                        phase.name,
                        trial=index,
                        stimulus=stimulus,
                        key=key,
                        rt_seconds=float(rt),
                        is_response_key=key in response_keys,
                    )
                    _push_marker_now(marker_outlet, f"calibration_go_nogo_practice_button_press_{index}_{key}")
                sleep(0.002)
            win.callOnFlip(
                _write_practice_stimulus_event,
                self._write_event,
                marker_outlet,
                onset_holder,
                "stimulus_offset",
                phase.name,
                index,
                stimulus,
            )
            win.flip()
            correct = bool((stimulus["is_no_go"] and not presses) or ((not stimulus["is_no_go"]) and presses))
            self._write_event(
                "trial_complete",
                phase.name,
                trial=index,
                stimulus=stimulus,
                correct_press=int(correct),
                button_press_count=len(presses),
                onset_lsl_timestamp=onset_holder.get("stimulus_onset_lsl_timestamp"),
                offset_lsl_timestamp=onset_holder.get("stimulus_offset_lsl_timestamp"),
            )
            if aborted:
                self._write_event("phase_abort", phase.name, trial=index)
                break
            _wait_or_abort(event, isi_seconds)
        self._write_event("recording_end", phase.name, trials=trials)
        _push_marker_now(marker_outlet, f"calibration_{phase.name}_end")

    def _run_synthetic_protocol(self) -> None:
        sample_rate = float(self.config.get("hardware", {}).get("eeg", {}).get("expected_sample_rate_hz", 500.0))
        channels = list(_channel_protocol(self.config, self.cfg))
        rows: list[list[float]] = []
        local_time = monotonic()
        lsl_time = 1000.0
        self.paths.calibration_events_jsonl.unlink(missing_ok=True)
        for phase in self.phases:
            duration = min(float(phase.duration_seconds), float(self.cfg.get("synthetic_phase_seconds", 20.0)))
            self._write_event("recording_start", phase.name, timestamp_monotonic=local_time, lsl_timestamp=lsl_time, duration_seconds=duration)
            samples = int(round(duration * sample_rate))
            alpha_amp = 8.0 if phase.name == "eyes_closed_rest" else 3.5
            if phase.name == "go_nogo_practice":
                alpha_amp = 5.0
            for sample_index in range(samples):
                t = sample_index / sample_rate
                local_sample = local_time + t
                lsl_sample = lsl_time + t
                signal = alpha_amp * math.sin(2.0 * math.pi * 10.4 * t)
                noise = np.random.default_rng(sample_index).normal(0, 0.8, size=len(channels))
                rows.append([lsl_sample, local_sample, *[signal + float(value) for value in noise]])
            local_time += duration
            lsl_time += duration
            self._write_event("recording_end", phase.name, timestamp_monotonic=local_time, lsl_timestamp=lsl_time, duration_seconds=duration)
            local_time += 1.0
            lsl_time += 1.0
        with self.paths.calibration_eeg_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["lsl_timestamp", "local_received_time", *channels])
            writer.writerows(rows)

    def _write_metadata(self) -> None:
        payload = {
            "schema_version": 1,
            "suite": self.name,
            "mode": self.mode,
            "record_eeg": self.record_eeg,
            "channel_protocol": list(_channel_protocol(self.config, self.cfg)),
            "phases": [phase.__dict__ for phase in self.phases],
            "config": self.cfg,
        }
        self.paths.calibration_metadata.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.calibration_metadata.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def _write_event(
        self,
        event_name: str,
        phase: str,
        timestamp_monotonic: float | None = None,
        lsl_timestamp: float | None = None,
        **metadata: Any,
    ) -> None:
        payload = {
            "schema_version": 1,
            "event": event_name,
            "phase": phase,
            "timestamp_monotonic": monotonic() if timestamp_monotonic is None else float(timestamp_monotonic),
            "lsl_timestamp": lsl_local_clock() if lsl_timestamp is None else float(lsl_timestamp),
            "metadata": metadata,
        }
        self.paths.calibration_events_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.calibration_events_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


class AlphaCalibrationAnalyzer:
    """Offline alpha calibration analysis for recorded calibration phases."""

    def __init__(self, config: dict[str, Any], paths: SessionPaths, telemetry: Telemetry | None = None) -> None:
        self.config = config
        self.paths = paths
        self.telemetry = telemetry or Telemetry.from_config(config, paths, component="calibration.posterior_alpha")
        self.cfg = _posterior_alpha_config(config)

    def run(self) -> dict[str, Any]:
        self.telemetry.emit("calibration.analysis.start", level="default", message="Alpha calibration analysis starting")
        try:
            result = self._run_analysis()
        except Exception as exc:
            result = self._fallback_result(f"analysis_failed:{type(exc).__name__}:{exc}")
        self._write_result(result)
        self.telemetry.emit(
            "calibration.analysis.complete",
            level="default",
            message=f"Alpha calibration analysis complete: {result.get('status')}",
            metadata=result,
        )
        return result

    def _run_analysis(self) -> dict[str, Any]:
        self._materialize_calibration_eeg()
        eeg = self._load_eeg()
        if eeg["data"].size == 0:
            return self._fallback_result("no_calibration_eeg")
        eeg, cleaning_summary = _offline_prepare_eeg(eeg, self.cfg)
        phases = self._load_phase_windows()
        psd_by_phase = {}
        clean_by_phase: dict[str, np.ndarray] = {}
        phase_quality: dict[str, Any] = {}
        for phase in ("eyes_open_fixation", "eyes_closed_rest", "go_nogo_practice"):
            segment = _segment_for_phase(eeg, phases.get(phase))
            if segment.size == 0:
                continue
            clean = _clean_segment(segment, self.cfg)
            clean_by_phase[phase] = clean
            phase_quality[phase] = {
                "input_samples": int(segment.shape[0]),
                "clean_samples": int(clean.shape[0]),
                "clean_fraction": None if segment.shape[0] == 0 else float(clean.shape[0] / segment.shape[0]),
            }
            freqs, power = _posterior_welch(clean, eeg["sample_rate_hz"], eeg["channel_names"], self.cfg)
            psd_by_phase[phase] = {"freqs": freqs, "power": power}
        if not psd_by_phase:
            return self._fallback_result("no_usable_phase_data")
        combined_freqs, combined_power = _combined_psd(psd_by_phase)
        self._write_psd(psd_by_phase)
        spectral_summary, candidates = _fit_specparam(combined_freqs, combined_power, self.cfg)
        psd_fallback_candidates: list[Any] = []
        if not candidates and bool(self.cfg.get("allow_psd_peak_fallback", True)):
            psd_fallback_candidates = _psd_alpha_peak_candidates(combined_freqs, combined_power, self.cfg)
            if psd_fallback_candidates:
                candidates = psd_fallback_candidates
                spectral_summary["psd_peak_fallback"] = {
                    "status": "used",
                    "reason": "specparam_peak_unavailable",
                    "candidates": [candidate.as_dict() for candidate in psd_fallback_candidates],
                }
            else:
                spectral_summary["psd_peak_fallback"] = {"status": "not_used", "reason": "no_plausible_psd_alpha_peak"}
        _write_json(self.paths.calibration_spectral_model_json, spectral_summary)
        candidate = candidates[0] if candidates else None
        eyes_ratio = _eyes_closed_open_ratio(psd_by_phase, self.cfg)
        accepted, rejection_reasons = accept_alpha_candidate(
            candidate,
            min_peak_power=float(self.cfg.get("min_peak_power", 0.05)),
            min_bandwidth_hz=float(self.cfg.get("min_bandwidth_hz", 0.5)),
            max_bandwidth_hz=float(self.cfg.get("max_bandwidth_hz", 8.0)),
            eyes_closed_open_ratio=eyes_ratio,
            min_eyes_closed_open_ratio=float(self.cfg.get("min_eyes_closed_open_ratio", 1.05)),
        )
        if accepted and candidate is not None:
            if candidate.source == "specparam":
                status = "accepted"
                confidence = "accepted"
            else:
                status = "low_confidence_psd_peak"
                confidence = "low_confidence_psd_peak"
                rejection_reasons = [*(rejection_reasons or []), "specparam_unavailable_or_no_alpha_peak"]
            band = bounded_alpha_band(candidate.center_hz, candidate.bandwidth_hz, source=candidate.source, confidence=confidence)
        else:
            band = fallback_alpha_band("no_accepted_specparam_peak")
            status = "low_confidence_fallback"
        baseline_band = (float(band.low_hz), float(band.high_hz))
        baseline = _baseline_stats(
            _alpha_window_powers(
                clean_by_phase.get("eyes_open_fixation", np.empty((0, 0))),
                eeg["sample_rate_hz"],
                eeg["channel_names"],
                self.cfg,
                baseline_band,
            ),
            band,
        )
        result = {
            "schema_version": 1,
            "suite": "posterior_alpha",
            "status": status,
            "calibration_status": status,
            "fallback_used": status != "accepted",
            "fallback_reason": None if status == "accepted" else ";".join(rejection_reasons or [spectral_summary.get("status", "unknown")]),
            "posterior_channels": list(_posterior_channels(self.cfg)),
            "clean_channels": [
                channel
                for channel in expected_profile(str(self.config.get("hardware", {}).get("eeg", {}).get("profile", "enobio8"))).channel_names
                if channel not in set(cleaning_summary.get("bad_channels", []))
            ],
            "fixed_reference_channels": [
                channel
                for channel in expected_profile(str(self.config.get("hardware", {}).get("eeg", {}).get("profile", "enobio8"))).channel_names
                if channel not in set(cleaning_summary.get("bad_channels", []))
            ],
            "online_band": band.as_dict(),
            "accepted_peak": None if candidate is None else candidate.as_dict(),
            "candidate_count": len(candidates),
            "spectral_model": spectral_summary,
            "eyes_closed_open_alpha_ratio": eyes_ratio,
            "baseline_mean_power": baseline["mean"],
            "baseline_std_power": baseline["std"],
            "quality": {
                "offline_cleaning": cleaning_summary,
                "phase_quality": phase_quality,
            },
            "files": {
                "metadata": str(self.paths.calibration_metadata),
                "events": str(self.paths.calibration_events_jsonl),
                "psd_csv": str(self.paths.calibration_psd_csv),
                "spectral_model_json": str(self.paths.calibration_spectral_model_json),
                "summary_json": str(self.paths.calibration_result),
                "plot": str(self.paths.calibration_plot),
            },
        }
        if bool(self.cfg.get("write_plot", True)):
            _write_plot(self.paths.calibration_plot, psd_by_phase, band.as_dict())
        return result

    def _load_eeg(self) -> dict[str, Any]:
        raw_path = self.paths.calibration_eeg_csv if self.paths.calibration_eeg_csv.exists() else self.paths.eeg_csv
        if not raw_path.exists():
            return {"data": np.empty((0, 0)), "timestamps": np.empty((0,)), "channel_names": [], "sample_rate_hz": 0.0}
        with raw_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            rows = [[float(value) for value in row] for row in reader if row]
        if not rows:
            return {"data": np.empty((0, 0)), "timestamps": np.empty((0,)), "channel_names": header[2:], "sample_rate_hz": 0.0}
        array = np.asarray(rows, dtype=float)
        timestamps = array[:, 1] if "local_received_time" in header else array[:, 0]
        data = array[:, 2:]
        channel_names = _infer_channel_names(header[2:], self.config)
        return {
            "data": data,
            "timestamps": timestamps,
            "channel_names": channel_names,
            "sample_rate_hz": _infer_sample_rate(array[:, 0], self.config),
        }

    def _materialize_calibration_eeg(self) -> None:
        if self.paths.calibration_eeg_csv.exists() or not self.paths.eeg_csv.exists():
            return
        windows = self._load_phase_windows()
        if not windows:
            return
        with self.paths.eeg_csv.open("r", encoding="utf-8", newline="") as source:
            reader = csv.reader(source)
            header = next(reader, [])
            rows = [row for row in reader if row]
        if not header or not rows:
            return
        try:
            time_index = header.index("local_received_time")
        except ValueError:
            time_index = 0
        selected = []
        for row in rows:
            try:
                timestamp = float(row[time_index])
            except (TypeError, ValueError, IndexError):
                continue
            if any(start <= timestamp <= end for start, end in windows.values()):
                selected.append(row)
        if not selected:
            return
        self.paths.calibration_eeg_csv.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.calibration_eeg_csv.open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target)
            writer.writerow(header)
            writer.writerows(selected)

    def _load_phase_windows(self) -> dict[str, tuple[float, float]]:
        starts: dict[str, float] = {}
        windows: dict[str, tuple[float, float]] = {}
        if not self.paths.calibration_events_jsonl.exists():
            return windows
        with self.paths.calibration_events_jsonl.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                phase = str(row.get("phase"))
                event = str(row.get("event"))
                timestamp = float(row.get("timestamp_monotonic"))
                if event == "recording_start":
                    starts[phase] = timestamp
                elif event == "recording_end" and phase in starts:
                    windows[phase] = (starts[phase], timestamp)
        return windows

    def _write_psd(self, psd_by_phase: dict[str, dict[str, np.ndarray]]) -> None:
        self.paths.calibration_psd_csv.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.calibration_psd_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["phase", "frequency_hz", "posterior_power"])
            for phase, values in psd_by_phase.items():
                for freq, power in zip(values["freqs"], values["power"]):
                    writer.writerow([phase, f"{float(freq):.6f}", f"{float(power):.12g}"])

    def _fallback_result(self, reason: str) -> dict[str, Any]:
        band = fallback_alpha_band(reason)
        return {
            "schema_version": 1,
            "suite": "posterior_alpha",
            "status": "low_confidence_fallback",
            "calibration_status": "low_confidence_fallback",
            "fallback_used": True,
            "fallback_reason": reason,
            "posterior_channels": list(_posterior_channels(self.cfg)),
            "clean_channels": [],
            "fixed_reference_channels": [],
            "online_band": band.as_dict(),
            "accepted_peak": None,
            "candidate_count": 0,
            "spectral_model": {"status": "not_run", "reason": reason},
            "eyes_closed_open_alpha_ratio": None,
            "baseline_mean_power": None,
            "baseline_std_power": None,
            "files": {
                "metadata": str(self.paths.calibration_metadata),
                "events": str(self.paths.calibration_events_jsonl),
                "summary_json": str(self.paths.calibration_result),
            },
        }

    def _write_result(self, result: dict[str, Any]) -> None:
        _write_json(self.paths.calibration_result, result)


def _posterior_alpha_config(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("calibration", {}).get("posterior_alpha", {}))


def _normalize_specparam_config(config: dict[str, Any]) -> dict[str, Any]:
    calibration = config.get("calibration", {}).get("posterior_alpha", {})
    if not isinstance(calibration, dict):
        return config
    aliases = {
        "fooof_fit_range_hz": "specparam_fit_range_hz",
        "fooof_peak_width_limits_hz": "specparam_peak_width_limits_hz",
        "fooof_max_n_peaks": "specparam_max_n_peaks",
        "fooof_min_peak_height": "specparam_min_peak_height",
    }
    for old_key, new_key in aliases.items():
        if old_key in calibration and new_key not in calibration:
            calibration[new_key] = calibration[old_key]
        calibration.pop(old_key, None)
    return config


def _calibration_phases(cfg: dict[str, Any]) -> list[CalibrationPhase]:
    eyes_open = float(cfg.get("eyes_open_seconds", 120.0))
    eyes_closed = float(cfg.get("eyes_closed_seconds", 120.0))
    task_duration = float(cfg.get("go_nogo_practice_seconds", 150.0))
    return [
        CalibrationPhase(
            "eyes_open_fixation",
            "Eyes Open Fixation",
            eyes_open,
            "Eyes Open Fixation\n\nKeep your eyes open and fixate on the central point.\nTry to relax your face and avoid blinking when possible.\n\nPress SPACE if these instructions are clear.",
        ),
        CalibrationPhase(
            "eyes_closed_rest",
            "Eyes Closed Rest",
            eyes_closed,
            "Eyes Closed Rest\n\nAfter the countdown, close your eyes and rest quietly until the next screen.\nKeep still and relaxed.\n\nPress SPACE if these instructions are clear.",
        ),
        CalibrationPhase(
            "go_nogo_practice",
            "Go/No-go Calibration Practice",
            task_duration,
            "Go/No-go Calibration Practice\n\nPress SPACE for GO stimuli. Do not press for the NO-GO stimulus.\nThis short block estimates task-state posterior alpha.\n\nPress SPACE if these instructions are clear.",
        ),
    ]


def _channel_protocol(config: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, ...]:
    channels = cfg.get("channel_protocol")
    if channels:
        return tuple(str(value) for value in channels)
    profile_name = config.get("hardware", {}).get("eeg", {}).get("profile")
    if profile_name:
        try:
            return expected_profile(str(profile_name)).channel_names
        except Exception:
            pass
    return DEFAULT_POSTERIOR_CHANNELS


def _posterior_channels(cfg: dict[str, Any]) -> tuple[str, ...]:
    channels = cfg.get("posterior_channels") or DEFAULT_POSTERIOR_CHANNELS
    return tuple(str(channel) for channel in channels)


def _make_marker_outlet(markers: dict[str, Any], paths: SessionPaths) -> Any:
    try:
        return LslMarkerOutlet(
            name=markers.get("lsl_stream_name", "ClosedLoopMarkers"),
            stream_type=markers.get("lsl_stream_type", "Markers"),
            source_id=markers.get("source_id") or session_marker_source_id(paths.root),
        )
    except Exception as exc:
        if bool(markers.get("required_for_realtime", False)):
            raise RuntimeError(f"required LSL marker outlet could not be created: {type(exc).__name__}: {exc}") from exc
        return NullMarkerOutlet(f"{type(exc).__name__}: {exc}")


def _push_marker_now(marker_outlet: Any, label: str) -> None:
    marker_outlet.push(label, timestamp=lsl_local_clock())


def _write_practice_stimulus_event(
    write_event: Any,
    marker_outlet: Any,
    holder: dict[str, float],
    event_name: str,
    phase: str,
    trial: int,
    stimulus: dict[str, Any],
) -> None:
    lsl_timestamp = lsl_local_clock()
    holder[f"{event_name}_lsl_timestamp"] = lsl_timestamp
    write_event(event_name, phase, lsl_timestamp=lsl_timestamp, trial=trial, stimulus=stimulus)
    marker_outlet.push(f"calibration_go_nogo_practice_{event_name}_{trial}", timestamp=lsl_timestamp)


def _show_instruction_screen(win: Any, visual: Any, event: Any, core: Any, text_value: str) -> bool:
    text = visual.TextStim(win, text=text_value, height=0.04, color="white", wrapWidth=1.5)
    text.draw()
    win.flip()
    event.clearEvents(eventType="keyboard")
    while True:
        keys = event.getKeys(keyList=["space", "escape", "q"])
        if "space" in keys:
            return True
        if "escape" in keys or "q" in keys:
            return False
        sleep(0.02)


def _countdown(win: Any, visual: Any, phase: str, seconds: int | float, write_event: Any, marker_outlet: Any) -> None:
    for value in range(int(seconds), 0, -1):
        visual.TextStim(win, text=str(value), height=0.16, color="white").draw()
        win.callOnFlip(write_event, "countdown", phase, value=value)
        win.callOnFlip(_push_marker_now, marker_outlet, f"calibration_{phase}_countdown_{value}")
        win.flip()
        sleep(1.0)
    visual.TextStim(win, text="Go", height=0.12, color="white").draw()
    win.callOnFlip(write_event, "countdown_go", phase)
    win.callOnFlip(_push_marker_now, marker_outlet, f"calibration_{phase}_countdown_go")
    win.flip()
    sleep(0.2)


def _draw_practice_stimulus(win: Any, visual: Any, stimulus: dict[str, Any]) -> None:
    shape = stimulus["shape"]
    color = stimulus["color"]
    if shape == "x":
        visual.TextStim(win, text="X", height=0.24, color=color, bold=True).draw()
    elif shape == "circle":
        visual.Circle(win, radius=0.12, fillColor=color, lineColor=color).draw()
    elif shape == "square":
        visual.Rect(win, width=0.22, height=0.22, fillColor=color, lineColor=color).draw()
    elif shape == "triangle":
        visual.Polygon(win, edges=3, radius=0.15, fillColor=color, lineColor=color).draw()
    elif shape == "hexagon":
        visual.Polygon(win, edges=6, radius=0.14, fillColor=color, lineColor=color).draw()
    elif shape == "star":
        visual.ShapeStim(win, vertices=_star_vertices(0.15, 0.065), fillColor=color, lineColor=color).draw()
    else:
        visual.TextStim(win, text=str(shape).upper(), height=0.14, color=color).draw()


def _practice_stimulus(config: dict[str, Any], no_go: dict[str, Any]) -> dict[str, Any]:
    if random.random() < float(config.get("no_go_probability", 0.3)):
        return {"shape": str(no_go.get("shape", "x")), "color": str(no_go.get("color", "white")), "is_no_go": True}
    return _random_go_stimulus({"shape": str(no_go.get("shape", "x")), "color": str(no_go.get("color", "white"))}, config)


def _wait_or_abort(event: Any, seconds: float) -> bool:
    start = monotonic()
    while monotonic() - start < seconds:
        if event.getKeys(keyList=["escape", "q"]):
            return True
        sleep(0.002)
    return False


def _infer_channel_names(columns: list[str], config: dict[str, Any]) -> list[str]:
    names, _source = mapped_channel_names([str(column) for column in columns], config.get("hardware", {}).get("eeg", {}))
    return names


def _infer_sample_rate(lsl_timestamps: np.ndarray, config: dict[str, Any]) -> float:
    diffs = np.diff(np.asarray(lsl_timestamps, dtype=float))
    diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if diffs.size:
        return float(1.0 / np.median(diffs))
    return float(config.get("hardware", {}).get("eeg", {}).get("expected_sample_rate_hz", 500.0))


def _offline_prepare_eeg(eeg: dict[str, Any], cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply offline-quality MNE cleaning before phase-level artifact rejection."""
    data = np.asarray(eeg["data"], dtype=float)
    channel_names = list(eeg.get("channel_names") or [])
    sample_rate = float(eeg.get("sample_rate_hz") or 0.0)
    if data.size == 0 or sample_rate <= 0 or not channel_names:
        return eeg, {"status": "skipped", "reason": "empty_eeg"}

    line_noise_hz = float(cfg.get("line_noise_hz", 60.0))
    channel_medians = np.nanmedian(data, axis=0, keepdims=True)
    centered_data = data - channel_medians
    bad_channels = _detect_bad_channels(centered_data, channel_names, cfg)
    keep_indices = [index for index, name in enumerate(channel_names) if name not in bad_channels]
    if not keep_indices:
        return eeg, {
            "status": "skipped",
            "reason": "all_channels_marked_bad",
            "bad_channels": bad_channels,
            "median_reference_removed": True,
            "channel_medians": [float(value) for value in channel_medians.reshape(-1)],
            "line_noise": _line_noise_ratio(centered_data, sample_rate, line_noise_hz),
        }

    try:
        _ensure_mne_runtime_cache()
        import mne
    except Exception as exc:
        return {
            **eeg,
            "data": centered_data[:, keep_indices],
            "channel_names": [channel_names[index] for index in keep_indices],
        }, {
            "status": "numpy_fallback",
            "reason": f"mne_unavailable:{type(exc).__name__}",
            "bad_channels": bad_channels,
            "median_reference_removed": True,
            "line_noise": _line_noise_ratio(centered_data[:, keep_indices], sample_rate, line_noise_hz),
        }

    kept_names = [channel_names[index] for index in keep_indices]
    kept_data = centered_data[:, keep_indices]
    before_line_noise = _line_noise_ratio(kept_data, sample_rate, line_noise_hz)
    cleaning_summary: dict[str, Any] = {
        "status": "mne_cleaned",
        "backend": "mne",
        "bad_channels": bad_channels,
        "dropped_channel_count": len(bad_channels),
        "median_reference_removed": True,
        "channel_medians": [float(channel_medians[0, index]) for index in keep_indices],
        "reference": "average",
        "line_noise_hz": line_noise_hz,
        "line_noise_before": before_line_noise,
        "ica": {
            "enabled": False,
            "reason": "disabled_by_default",
        },
    }
    info = mne.create_info(ch_names=kept_names, sfreq=sample_rate, ch_types="eeg")
    raw = mne.io.RawArray((kept_data.T * 1e-6), info, verbose=False)
    raw.set_eeg_reference("average", projection=False, verbose=False)
    filter_low = _optional_filter_frequency(cfg.get("offline_filter_low_hz", 1.0))
    filter_high = _optional_filter_frequency(cfg.get("offline_filter_high_hz", 40.0))
    if filter_high is not None and filter_high >= sample_rate / 2.0:
        filter_high = None
    if filter_low is not None or filter_high is not None:
        try:
            raw.filter(l_freq=filter_low, h_freq=filter_high, method="iir", verbose=False)
            cleaning_summary["filter"] = {
                "status": "applied",
                "low_hz": filter_low,
                "high_hz": filter_high,
                "method": "iir",
            }
        except Exception as exc:
            cleaning_summary["filter"] = {"status": "skipped", "reason": f"{type(exc).__name__}:{exc}"}
    else:
        cleaning_summary["filter"] = {"status": "skipped", "reason": "disabled"}
    if 0.0 < line_noise_hz < sample_rate / 2.0:
        try:
            raw.notch_filter(freqs=[line_noise_hz], method="iir", verbose=False)
            cleaning_summary["notch_filter"] = "applied"
        except Exception as exc:
            cleaning_summary["notch_filter"] = f"skipped:{type(exc).__name__}:{exc}"
    else:
        cleaning_summary["notch_filter"] = "skipped_outside_nyquist"

    ica_requested = bool(cfg.get("ica_enabled", False))
    ica_min_channels = int(cfg.get("ica_min_channels", 16))
    if ica_requested and len(kept_names) >= ica_min_channels:
        cleaning_summary["ica"] = _fit_optional_ica(raw, cfg)
    elif ica_requested:
        cleaning_summary["ica"] = {
            "enabled": False,
            "reason": f"requires_at_least_{ica_min_channels}_channels",
            "channel_count": len(kept_names),
        }

    cleaned = raw.get_data().T * 1e6
    cleaning_summary["line_noise_after"] = _line_noise_ratio(cleaned, sample_rate, line_noise_hz)
    return {
        **eeg,
        "data": np.asarray(cleaned, dtype=float),
        "channel_names": kept_names,
    }, cleaning_summary


def _fit_optional_ica(raw: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        _ensure_mne_runtime_cache()
        import mne

        n_components = min(int(cfg.get("ica_n_components", 15)), len(raw.ch_names) - 1)
        if n_components < 2:
            return {"enabled": False, "reason": "not_enough_components", "channel_count": len(raw.ch_names)}
        fit_raw = raw.copy().filter(l_freq=1.0, h_freq=None, method="iir", verbose=False)
        ica = mne.preprocessing.ICA(
            n_components=n_components,
            random_state=int(cfg.get("ica_random_state", 97)),
            max_iter="auto",
            verbose=False,
        )
        ica.fit(fit_raw, verbose=False)
        exclude = [int(value) for value in cfg.get("ica_exclude_components", [])]
        if exclude:
            ica.exclude = exclude
            ica.apply(raw, verbose=False)
        return {
            "enabled": True,
            "n_components": n_components,
            "excluded_components": exclude,
            "note": "no automatic component exclusion without EOG/ECG labels",
        }
    except Exception as exc:
        return {"enabled": False, "reason": f"{type(exc).__name__}:{exc}"}


def _ensure_mne_runtime_cache() -> None:
    cache = Path.cwd() / ".runtime" / "matplotlib"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))


def _optional_filter_frequency(value: Any) -> float | None:
    if value in {None, "", False}:
        return None
    number = float(value)
    return number if number > 0.0 else None


def _detect_bad_channels(data: np.ndarray, channel_names: list[str], cfg: dict[str, Any]) -> list[str]:
    values = np.asarray(data, dtype=float)
    bad: list[str] = []
    max_abs_uv = float(cfg.get("offline_bad_channel_max_abs_uv", max(25000.0, float(cfg.get("offline_max_abs_uv", 150.0)) * 100.0)))
    min_std_uv = float(cfg.get("offline_bad_channel_min_std_uv", 1e-6))
    max_std_uv = float(cfg.get("offline_bad_channel_max_std_uv", max(10000.0, float(cfg.get("offline_max_abs_uv", 150.0)) * 100.0)))
    for index, name in enumerate(channel_names):
        channel = values[:, index]
        finite = channel[np.isfinite(channel)]
        if finite.size == 0:
            bad.append(name)
            continue
        if float(np.max(np.abs(finite))) > max_abs_uv:
            bad.append(name)
            continue
        std = float(np.std(finite))
        if std < min_std_uv or std > max_std_uv:
            bad.append(name)
    return bad


def _line_noise_ratio(data: np.ndarray, sample_rate_hz: float, line_noise_hz: float) -> dict[str, Any]:
    from scipy import signal

    values = np.asarray(data, dtype=float)
    if values.size == 0 or sample_rate_hz <= 0 or not (0.0 < line_noise_hz < sample_rate_hz / 2.0):
        return {"status": "skipped"}
    nperseg = min(values.shape[0], max(32, int(round(sample_rate_hz * 2.0))))
    freqs, psd = signal.welch(values, fs=sample_rate_hz, nperseg=nperseg, axis=0)
    mean_psd = np.nanmean(psd, axis=1)
    line_mask = (freqs >= line_noise_hz - 1.0) & (freqs <= line_noise_hz + 1.0)
    neighbor_mask = (freqs >= max(1.0, line_noise_hz - 6.0)) & (freqs <= line_noise_hz + 6.0) & ~line_mask
    if not line_mask.any() or not neighbor_mask.any():
        return {"status": "skipped"}
    neighbor = float(np.nanmean(mean_psd[neighbor_mask]))
    line = float(np.nanmean(mean_psd[line_mask]))
    return {
        "status": "ok",
        "line_power": line,
        "neighbor_power": neighbor,
        "line_to_neighbor_ratio": None if neighbor <= 0 else float(line / neighbor),
    }


def _segment_for_phase(eeg: dict[str, Any], window: tuple[float, float] | None) -> np.ndarray:
    data = np.asarray(eeg["data"], dtype=float)
    if window is None:
        return data
    timestamps = np.asarray(eeg["timestamps"], dtype=float)
    mask = (timestamps >= window[0]) & (timestamps <= window[1])
    return data[mask]


def _clean_segment(segment: np.ndarray, cfg: dict[str, Any]) -> np.ndarray:
    values = np.asarray(segment, dtype=float)
    if values.size == 0:
        return values
    values = values[np.all(np.isfinite(values), axis=1)]
    if values.size == 0:
        return values
    values = values - np.nanmedian(values, axis=0, keepdims=True)
    max_abs = float(cfg.get("offline_max_abs_uv", 150.0))
    peak_to_peak = float(cfg.get("offline_epoch_peak_to_peak_uv", 250.0))
    keep = np.max(np.abs(values), axis=1) <= max_abs
    centered = values[keep]
    if centered.shape[0] < 2:
        return centered
    p2p_keep = np.max(centered, axis=1) - np.min(centered, axis=1) <= peak_to_peak
    cleaned = centered[p2p_keep]
    min_fraction = float(cfg.get("offline_min_clean_fraction", 0.05))
    if values.shape[0] and cleaned.shape[0] / values.shape[0] >= min_fraction:
        return cleaned

    row_abs = np.max(np.abs(values), axis=1)
    row_p2p = np.max(values, axis=1) - np.min(values, axis=1)
    adaptive_abs = max(max_abs, float(np.nanpercentile(row_abs, 99.0)))
    adaptive_p2p = max(peak_to_peak, float(np.nanpercentile(row_p2p, 99.0)))
    adaptive_keep = (row_abs <= adaptive_abs) & (row_p2p <= adaptive_p2p)
    return values[adaptive_keep]


def _posterior_welch(segment: np.ndarray, sample_rate_hz: float, channel_names: list[str], cfg: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    from scipy import signal

    values = np.asarray(segment, dtype=float)
    if values.size == 0:
        return np.empty((0,)), np.empty((0,))
    indices = [channel_names.index(name) for name in _posterior_channels(cfg) if name in channel_names]
    if not indices:
        raise ValueError("no configured posterior channels remain for calibration PSD")
    posterior = values[:, indices]
    window_seconds = float(cfg.get("welch_window_seconds", 4.0))
    nperseg = max(8, min(values.shape[0], int(round(window_seconds * sample_rate_hz))))
    noverlap = int(round(nperseg * float(cfg.get("welch_overlap_fraction", 0.5))))
    freqs, psd = signal.welch(posterior, fs=sample_rate_hz, nperseg=nperseg, noverlap=noverlap, axis=0)
    return freqs, np.nanmean(psd, axis=1)


def _alpha_window_powers(
    segment: np.ndarray,
    sample_rate_hz: float,
    channel_names: list[str],
    cfg: dict[str, Any],
    band_hz: tuple[float, float] | None = None,
) -> list[float]:
    values = np.asarray(segment, dtype=float)
    if values.size == 0:
        return []
    indices = [channel_names.index(name) for name in _posterior_channels(cfg) if name in channel_names]
    if not indices:
        raise ValueError("no configured posterior channels remain for alpha baseline")
    window = max(8, int(round(float(cfg.get("welch_window_seconds", 4.0)) * sample_rate_hz)))
    step = max(1, window // 2)
    powers = []
    selected_band = band_hz or (8.0, 12.0)
    for start in range(0, max(1, values.shape[0] - window + 1), step):
        chunk = values[start : start + window, :][:, indices]
        if chunk.shape[0] >= 8:
            powers.append(sliding_window_psd_alpha_power(chunk, sample_rate_hz, selected_band))
    return powers


def _combined_psd(psd_by_phase: dict[str, dict[str, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    first = next(iter(psd_by_phase.values()))
    freqs = first["freqs"]
    powers = []
    for values in psd_by_phase.values():
        if values["freqs"].shape == freqs.shape and np.allclose(values["freqs"], freqs):
            powers.append(values["power"])
    return freqs, np.nanmean(np.stack(powers, axis=0), axis=0)


def _fit_specparam(freqs: np.ndarray, power: np.ndarray, cfg: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    freq_range = tuple(float(value) for value in cfg.get("specparam_fit_range_hz", [3.0, 35.0]))
    mask = (freqs >= freq_range[0]) & (freqs <= freq_range[1]) & np.isfinite(power) & (power > 0)
    if not mask.any():
        return {"status": "failed", "reason": "no_positive_psd_values", "fit_range_hz": list(freq_range)}, []
    try:
        from specparam import SpectralModel

        model = SpectralModel(
            peak_width_limits=tuple(cfg.get("specparam_peak_width_limits_hz", [0.5, 8.0])),
            max_n_peaks=int(cfg.get("specparam_max_n_peaks", 6)),
            min_peak_height=float(cfg.get("specparam_min_peak_height", 0.05)),
            verbose=False,
        )
    except Exception as specparam_exc:
        return {
            "status": "missing_dependency",
            "specparam_error": f"{type(specparam_exc).__name__}: {specparam_exc}",
            "fit_range_hz": list(freq_range),
        }, []
    model.fit(freqs[mask], power[mask], freq_range)
    peak_params = _specparam_periodic_params(model)
    aperiodic_params = _specparam_aperiodic_params(model)
    r_squared = _specparam_r_squared(model)
    candidates = spectral_peak_candidates(
        {"peak_params": peak_params, "r_squared": r_squared, "source": "specparam"},
        tuple(cfg.get("alpha_candidate_range_hz", [7.0, 14.0])),
    )
    summary = {
        "status": "ok",
        "backend": "specparam",
        "fit_range_hz": list(freq_range),
        "r_squared": r_squared,
        "peak_params": peak_params,
        "aperiodic_params": aperiodic_params,
    }
    return summary, candidates


def _specparam_periodic_params(model: Any) -> list[list[float]]:
    params = model.results.get_params("periodic", version="converted")
    array = np.asarray(params, dtype=float)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.size == 0 or np.all(np.isnan(array)):
        return []
    return array.tolist()


def _specparam_aperiodic_params(model: Any) -> list[float]:
    params = model.results.get_params("aperiodic", version="fit")
    array = np.asarray(params, dtype=float)
    if array.size == 0 or np.all(np.isnan(array)):
        return []
    return array.reshape(-1).tolist()


def _specparam_r_squared(model: Any) -> float | None:
    try:
        value = model.results.get_metrics("gof", "rsquared")
    except Exception:
        return None
    array = np.asarray(value, dtype=float)
    if array.size != 1 or not np.isfinite(array.reshape(-1)[0]):
        return None
    return float(array.reshape(-1)[0])


def _psd_alpha_peak_candidates(freqs: np.ndarray, power: np.ndarray, cfg: dict[str, Any]) -> list[AlphaPeakCandidate]:
    values = np.asarray(power, dtype=float)
    frequency = np.asarray(freqs, dtype=float)
    low, high = tuple(float(value) for value in cfg.get("alpha_candidate_range_hz", [7.0, 14.0]))
    mask = (frequency >= low) & (frequency <= high) & np.isfinite(values) & (values > 0)
    if not mask.any():
        return []
    alpha_freqs = frequency[mask]
    alpha_power = values[mask]
    peak_index = int(np.nanargmax(alpha_power))
    peak_freq = float(alpha_freqs[peak_index])
    peak_power = float(alpha_power[peak_index])
    shoulder = float(np.nanmedian(alpha_power))
    prominence = max(0.0, peak_power - shoulder)
    if prominence <= float(cfg.get("psd_peak_min_prominence", 0.01)):
        return []
    half_height = shoulder + prominence / 2.0
    left = peak_index
    while left > 0 and alpha_power[left] >= half_height:
        left -= 1
    right = peak_index
    while right < alpha_power.size - 1 and alpha_power[right] >= half_height:
        right += 1
    bandwidth = float(max(0.5, alpha_freqs[right] - alpha_freqs[left]))
    if bandwidth > float(cfg.get("max_bandwidth_hz", 8.0)):
        return []
    return [
        AlphaPeakCandidate(
            center_hz=peak_freq,
            power=prominence,
            bandwidth_hz=bandwidth,
            fit_r_squared=None,
            source="welch_psd_peak_fallback",
        )
    ]


def _eyes_closed_open_ratio(psd_by_phase: dict[str, dict[str, np.ndarray]], cfg: dict[str, Any]) -> float | None:
    open_psd = psd_by_phase.get("eyes_open_fixation")
    closed_psd = psd_by_phase.get("eyes_closed_rest")
    if not open_psd or not closed_psd:
        return None
    low, high = tuple(float(value) for value in cfg.get("alpha_candidate_range_hz", [7.0, 14.0]))
    open_power = _integrate_band(open_psd["freqs"], open_psd["power"], low, high)
    closed_power = _integrate_band(closed_psd["freqs"], closed_psd["power"], low, high)
    if open_power <= 0:
        return None
    return float(closed_power / open_power)


def _integrate_band(freqs: np.ndarray, power: np.ndarray, low: float, high: float) -> float:
    mask = (freqs >= low) & (freqs <= high)
    if not mask.any():
        return 0.0
    return float(np.trapezoid(power[mask], freqs[mask]))


def _baseline_stats(window_powers: list[float], band: Any) -> dict[str, float | None]:
    values = np.asarray(window_powers, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {"mean": None, "std": None}
    return {"mean": float(np.mean(values)), "std": float(np.std(values, ddof=1)) if values.size > 1 else 1.0}


def _write_plot(path: Path, psd_by_phase: dict[str, dict[str, np.ndarray]], band: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    width = 760
    height = 360
    left = 58
    right = 24
    top = 22
    bottom = 44
    plot_w = width - left - right
    plot_h = height - top - bottom
    x_min, x_max = 2.0, 40.0
    powers = []
    for values in psd_by_phase.values():
        mask = (values["freqs"] >= x_min) & (values["freqs"] <= x_max)
        if mask.any():
            powers.extend(float(value) for value in values["power"][mask] if np.isfinite(value))
    y_max = max(powers) if powers else 1.0
    y_min = min(powers) if powers else 0.0
    if y_max <= y_min:
        y_max = y_min + 1.0

    def x_pos(freq: float) -> float:
        return left + (freq - x_min) / (x_max - x_min) * plot_w

    def y_pos(power: float) -> float:
        return top + (1.0 - (power - y_min) / (y_max - y_min)) * plot_h

    colors = {
        "eyes_open_fixation": "#2f6fba",
        "eyes_closed_rest": "#229954",
        "go_nogo_practice": "#b45f06",
    }
    band_x = x_pos(float(band["low_hz"]))
    band_w = max(1.0, x_pos(float(band["high_hz"])) - band_x)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<rect x="{band_x:.2f}" y="{top}" width="{band_w:.2f}" height="{plot_h}" fill="#b7e1cd" opacity="0.45"/>',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#333" stroke-width="1"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#333" stroke-width="1"/>',
        f'<text x="{left + plot_w / 2:.1f}" y="{height - 12}" text-anchor="middle" font-size="13" fill="#333">Frequency (Hz)</text>',
        f'<text x="14" y="{top + plot_h / 2:.1f}" transform="rotate(-90 14 {top + plot_h / 2:.1f})" text-anchor="middle" font-size="13" fill="#333">Posterior PSD</text>',
    ]
    for tick in (2, 8, 12, 20, 30, 40):
        x = x_pos(float(tick))
        lines.append(f'<line x1="{x:.2f}" y1="{top + plot_h}" x2="{x:.2f}" y2="{top + plot_h + 5}" stroke="#333" stroke-width="1"/>')
        lines.append(f'<text x="{x:.2f}" y="{top + plot_h + 20}" text-anchor="middle" font-size="11" fill="#333">{tick}</text>')
    legend_y = top + 14
    for phase, values in psd_by_phase.items():
        mask = (values["freqs"] >= x_min) & (values["freqs"] <= x_max)
        if not mask.any():
            continue
        points = " ".join(
            f"{x_pos(float(freq)):.2f},{y_pos(float(power)):.2f}"
            for freq, power in zip(values["freqs"][mask], values["power"][mask])
            if np.isfinite(power)
        )
        color = colors.get(phase, "#555")
        lines.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
        lines.append(f'<rect x="{left + plot_w - 190}" y="{legend_y - 9}" width="12" height="8" fill="{color}"/>')
        lines.append(f'<text x="{left + plot_w - 172}" y="{legend_y}" font-size="12" fill="#333">{phase}</text>')
        legend_y += 18
    lines.append(f'<text x="{band_x + band_w / 2:.1f}" y="{top + 16}" text-anchor="middle" font-size="12" fill="#1d6f42">alpha band</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
