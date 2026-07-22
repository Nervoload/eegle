from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any

import numpy as np

from eegle.analysis.classification import _prediction_differences
from eegle.realtime.classification import sanitize_model_metadata
from eegle.realtime.epoching import MarkerEvent
from eegle.realtime.event_features import EngineInputCaptureWriter, read_engine_capture
from eegle.realtime.models import ModelPrediction
from eegle.realtime.online_adaptation import (
    AdaptationUpdateResult,
    OnlineAdaptationState,
    apply_delayed_adaptation_update,
)
from eegle.realtime.performance import RealtimePerformanceConfig, RealtimePerformanceStats
from eegle.workers.realtime_processor import InferenceWorkItem, _process_inference_item


FIXTURE_MANIFEST = Path(__file__).parent / "fixtures" / "migration" / "phase1_invariants.json"


class _MemoryWriter:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write(self, payload: dict[str, Any]) -> None:
        self.rows.append(payload)


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **payload: Any) -> None:
        self.events.append((event, payload))


class _Emitter:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def emit(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


class _ObserveOnlyPolicy:
    def decide(self, prediction: ModelPrediction, metadata: dict[str, Any]) -> list[Any]:
        return []


class _RecordingAdapter:
    def __init__(self, name: str, calls: list[dict[str, Any]]) -> None:
        self.name = name
        self.calls = calls

    def predict_prepared_epoch(self, prepared: Any) -> ModelPrediction:
        self.calls.append(
            {
                "name": self.name,
                "prepared_id": id(prepared),
                "epoch": np.asarray(prepared.epoch).copy(),
                "metadata": dict(prepared.metadata),
            }
        )
        probability = 0.8 if self.name == "primary" else 0.7
        return ModelPrediction(
            label="positive",
            score=probability,
            probability=probability,
            model_kind="synthetic_binary",
        )


class _AdaptiveAdapter:
    supports_online_update = True

    def __init__(self) -> None:
        self.state = OnlineAdaptationState(
            model_kind="synthetic_adaptive",
            target="binary_outcome",
            label_mode="delayed",
        )
        self.last_metadata: dict[str, Any] | None = None

    def snapshot_adaptation_state(self) -> dict[str, Any]:
        return self.state.payload()

    def update_after_trial(self, prepared: Any, label: int, metadata: dict[str, Any]) -> AdaptationUpdateResult:
        before = self.state.payload()
        self.state.total_update_count += 1
        self.state.accepted_update_count += 1
        self.state.support_size += 1
        self.state.class_counts[str(label)] += 1
        self.state.last_updated_trial = int(metadata["trial"])
        self.last_metadata = dict(metadata)
        after = self.state.payload()
        return AdaptationUpdateResult(status="updated", state_before=before, state_after=after, metadata=metadata)


class Phase1MigrationInvariantTests(unittest.TestCase):
    def test_fixture_manifest_is_complete_and_non_sensitive(self) -> None:
        manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
        fixtures = manifest["fixtures"]

        self.assertEqual(manifest["schema"], "eegle.migration_invariant_manifest.v1")
        self.assertEqual(manifest["phase"], 1)
        self.assertEqual(len({fixture["id"] for fixture in fixtures}), len(fixtures))
        self.assertEqual({fixture["task_id"] for fixture in fixtures}, {f"P1-{index:03d}" for index in range(1, 8)})
        for fixture in fixtures:
            self.assertEqual(fixture["sensitivity"], "non_sensitive")
            self.assertTrue(fixture["invariant"])
            self.assertTrue(fixture["provenance"])
            self.assertTrue(fixture["equivalence"])
            self.assertTrue(fixture["tests"])
            self.assertTrue(fixture["current_sources"])
            self.assertTrue(fixture["future_destination"])
            self.assertTrue(fixture["regeneration"])

    def test_causal_model_metadata_is_label_blind(self) -> None:
        sanitized = sanitize_model_metadata(
            {
                "relative_times": [-0.2, 0.0, 0.2],
                "sample_rate_hz": 100.0,
                "channel_names": ["sensor-a", "sensor-b"],
                "epoch_window_seconds": [-0.2, 0.8],
                "ground_truth": 1,
                "label": "positive",
                "condition": "target",
                "response_correct": True,
                "outcome": {"available_later": True},
                "stimulus": {"identity": "secret-target"},
                "metadata": {"trial_label": 1},
            }
        )

        self.assertEqual(
            sanitized,
            {
                "relative_times": [-0.2, 0.0, 0.2],
                "sample_rate_hz": 100.0,
                "channel_names": ["sensor-a", "sensor-b"],
                "epoch_window_seconds": [-0.2, 0.8],
            },
        )

    def test_primary_and_shadow_share_admitted_input_and_are_accounted(self) -> None:
        calls: list[dict[str, Any]] = []
        prediction_writer = _MemoryWriter()
        decision_writer = _MemoryWriter()
        pending: dict[int, list[dict[str, Any]]] = {}
        epoch_data = np.arange(12, dtype=float).reshape(6, 2)
        metadata = {
            "relative_times": np.linspace(-0.2, 0.8, 6).tolist(),
            "sample_rate_hz": 5.0,
            "channel_names": ["sensor-a", "sensor-b"],
            "epoch_window_seconds": [-0.2, 0.8],
        }
        item = InferenceWorkItem(
            epoch_payload={
                "epoch_index": 3,
                "trial": 7,
                "epoch_window_seconds": [-0.2, 0.8],
                "marker": {"timestamp": 12.5},
            },
            epoch=SimpleNamespace(epoch_index=3, data=epoch_data),
            model_metadata=metadata,
            quality={"valid": True, "reasons": [], "metrics": {}},
            queued_at_monotonic=monotonic(),
        )

        result = _process_inference_item(
            item,
            queue_depth=0,
            model_entries=[
                {"id": "shadow", "role": "shadow", "kind": "synthetic_binary", "adapter": _RecordingAdapter("shadow", calls)},
                {"id": "primary", "role": "primary", "kind": "synthetic_binary", "adapter": _RecordingAdapter("primary", calls)},
            ],
            sample_rate=5.0,
            channel_names=["sensor-a", "sensor-b"],
            classifier_mode=True,
            model_prediction_writer=prediction_writer,
            decision_writer=decision_writer,
            emitter=_Emitter(),
            policy=_ObserveOnlyPolicy(),
            sample_count=6,
            processed_count=6,
            marker_count=1,
            epoch_count=1,
            performance_config=RealtimePerformanceConfig(primary_latency_budget_ms=10000.0),
            performance_stats=RealtimePerformanceStats(),
            telemetry=_Telemetry(),
            adaptation_config={"enabled": True},
            pending_predictions=pending,
        )

        self.assertEqual([call["name"] for call in calls], ["primary", "shadow"])
        self.assertEqual(calls[0]["prepared_id"], calls[1]["prepared_id"])
        np.testing.assert_array_equal(calls[0]["epoch"], calls[1]["epoch"])
        self.assertEqual(calls[0]["metadata"], calls[1]["metadata"])
        self.assertEqual(result.prediction_count, 2)
        self.assertEqual(result.primary_prediction_count, 1)
        self.assertEqual(result.skipped_shadow_count, 0)
        self.assertEqual(
            [(row["status"], row["model_role"]) for row in prediction_writer.rows],
            [("predicted", "primary"), ("predicted", "shadow")],
        )
        self.assertEqual(len(pending[7]), 2)
        self.assertIs(pending[7][0]["prepared"], pending[7][1]["prepared"])
        self.assertEqual(len(decision_writer.rows), 1)

    def test_exact_capture_and_prediction_divergence_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.bin"
            timestamps = np.asarray([1.0, 1.25, 1.5], dtype=np.float64)
            samples = np.asarray([[0.1, 10.0], [0.2, 20.0], [0.3, 30.0]], dtype=np.float64)
            marker = MarkerEvent("event-1", 1.25, timebase="source-clock", source="synthetic-source")
            writer = EngineInputCaptureWriter(
                path,
                {"mode": "migration-fixture", "channel_names": ["sensor-a", "sensor-b"], "clock_id": "source-clock"},
            )
            writer.write_eeg(timestamps, samples)
            writer.write_marker(marker)
            writer.close()

            header, records = read_engine_capture(path)
            captured = list(records)

        self.assertEqual(header["clock_id"], "source-clock")
        self.assertEqual([kind for kind, _ in captured], ["eeg", "marker"])
        captured_timestamps, captured_samples = captured[0][1]
        np.testing.assert_array_equal(captured_timestamps, timestamps)
        np.testing.assert_array_equal(captured_samples, samples)
        self.assertEqual(captured[1][1], marker)

        online = {
            "status": "predicted",
            "trial": 7,
            "model_id": "primary",
            "epoch_index": 3,
            "model_kind": "synthetic_binary",
            "prediction_label": "positive",
            "probability_no_go": 0.25,
        }
        self.assertEqual(_prediction_differences([online], [dict(online)]), [])
        changed = {**online, "probability_no_go": 0.3}
        self.assertEqual(_prediction_differences([online], [changed])[0]["reason"], "probability_difference")

    def test_delayed_outcome_state_transition_round_trips(self) -> None:
        adapter = _AdaptiveAdapter()
        result = apply_delayed_adaptation_update(
            adapter,
            {
                "model_id": "primary",
                "model_role": "primary",
                "model_kind": "synthetic_adaptive",
                "prepared": object(),
                "prediction_row": {"probability_attention_lapse": 0.6},
            },
            {"status": "labeled", "label": 1, "label_mode": "delayed"},
            {"trial": 11, "event_time": 20.0, "available_time": 22.0},
            {"update_primary": True, "allowed_model_kinds": ["synthetic_adaptive"]},
        )

        self.assertEqual(result.status, "updated")
        self.assertNotEqual(
            result.state_before["calibration_state_hash"],
            result.state_after["calibration_state_hash"],
        )
        self.assertEqual(adapter.last_metadata["trial"], 11)
        self.assertEqual(adapter.last_metadata["pre_update_probability"], 0.6)
        self.assertEqual(adapter.state.last_updated_trial, 11)

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            written = adapter.state.snapshot(state_path)
            restored = OnlineAdaptationState.load(state_path)

        self.assertEqual(restored.state_hash(), adapter.state.state_hash())
        self.assertEqual(written["calibration_state_hash"], restored.payload()["calibration_state_hash"])


if __name__ == "__main__":
    unittest.main()
