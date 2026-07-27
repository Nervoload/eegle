from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from eegle.analysis.classification import _prediction_differences
from eegle.models.contracts import (
    ModelContract,
    ModelInputContract,
    ModelOutputContract,
    ModelStateBehavior,
    ModelStateContract,
    PreprocessingOwnership,
    PreprocessingRequirement,
)
from eegle.realtime.classification import sanitize_model_metadata
from eegle.realtime.epoching import MarkerEvent
from eegle.realtime.event_features import EngineInputCaptureWriter, read_engine_capture
from eegle.realtime.online_adaptation import (
    AdaptationUpdateResult,
    OnlineAdaptationState,
    apply_delayed_adaptation_update,
)


FIXTURE_MANIFEST = Path(__file__).parent / "fixtures" / "migration" / "phase1_invariants.json"


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
    def test_model_contract_nested_payload_round_trips_without_semantic_loss(self) -> None:
        contract = ModelContract(
            inputs=(
                ModelInputContract(
                    port_name="window",
                    type_id="eegle.dense_window.v1",
                    requirements={
                        "channel_ids": ["sensor.fz", "sensor.cz"],
                        "required_channel_ids": ["sensor.cz"],
                        "sample_rate_hz": 250.0,
                        "window_seconds": [-1.0, 0.0],
                    },
                    preprocessing=(
                        PreprocessingRequirement(
                            requirement_id="requirement.bandpass",
                            operation="sos_bandpass",
                            ownership=PreprocessingOwnership.UPSTREAM,
                            parameters={"low_hz": 1.0, "high_hz": 40.0},
                            required_lineage=("component.bandpass",),
                        ),
                    ),
                ),
            ),
            outputs=(
                ModelOutputContract(
                    port_name="prediction",
                    type_id="eegle.model_result.v1",
                    value_schema={
                        "type": "object",
                        "required": ["label"],
                        "properties": {"label": {"enum": ["attentive", "lapse"]}},
                    },
                    uncertainty_schema={
                        "type": "object",
                        "properties": {"probability": {"type": "number"}},
                    },
                ),
            ),
            state=ModelStateContract(
                behavior=ModelStateBehavior.SNAPSHOT_RESTORE,
                state_schema_id="example.attention_state.v1",
                adaptation_supported=True,
                state_affects_predictions=True,
            ),
            metadata={"prediction_horizon_seconds": [0.0, 0.2]},
        )

        self.assertEqual(ModelContract.from_payload(contract.to_payload()), contract)

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
