from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import eegle
from eegle.models import (
    CalibrationState,
    ModelContract,
    ModelSpec,
    get_model_spec,
    import_runtime_bundle,
    load_model_bundle_object,
    read_calibration_state,
    register_model_spec,
    resolve_model_kind,
    unregister_model_spec,
    write_calibration_state,
    write_model_bundle,
)
from eegle.protocols import ProtocolTarget, ScientificProtocol, attention_lapse_protocol, load_protocol, write_protocol


class PublicApiTests(unittest.TestCase):
    def test_top_level_public_imports_are_available(self) -> None:
        self.assertIs(eegle.SessionPaths.__name__, "SessionPaths")
        self.assertIs(eegle.EpochingConfig.__name__, "EpochingConfig")
        self.assertIs(eegle.ModelPrediction.__name__, "ModelPrediction")
        self.assertIs(eegle.TaskAction.__name__, "TaskAction")

    def test_model_contract_v2_accepts_legacy_bundle_payload(self) -> None:
        contract = ModelContract.from_payload(
            {
                "channel_names": ["Cz", "Pz"],
                "required_channels": ["Cz"],
                "optional_channels": ["Pz"],
                "sample_rate_hz": 100.0,
                "epoch_window_seconds": [-0.2, 0.8],
                "target": "attention_lapse_binary",
                "label_mapping": {"attentive": 0, "attention_lapse": 1},
                "latency_budget_ms": 50,
            }
        )

        contract.validate()
        payload = contract.payload()

        self.assertEqual(payload["schema"], "eegle.model_contract.v2")
        self.assertEqual(payload["channel_names"], ["Cz", "Pz"])
        self.assertEqual(payload["target"]["name"], "attention_lapse_binary")
        self.assertEqual(payload["latency_budget_ms"], 50.0)

    def test_dynamic_model_registry_accepts_plugin_specs(self) -> None:
        spec = ModelSpec(
            kind="fixture_external_model",
            family="fixture",
            description="Fixture external adapter.",
            adapter_kind="fixture_external",
            train_kind=None,
            trainable=False,
            realtime_supported=True,
            primary_realtime_allowed=False,
            dependencies=(),
            artifact_format="external_checkpoint",
            checkpoint_format="fixture",
            supported_targets=("condition",),
            latency_budget_ms=100.0,
            aliases=("fixture_alias",),
            external_checkpoint=True,
        )
        try:
            register_model_spec(spec)

            self.assertEqual(resolve_model_kind("fixture_alias"), "fixture_external_model")
            self.assertEqual(get_model_spec("fixture_external_model").family, "fixture")
        finally:
            unregister_model_spec("fixture_external_model")

    def test_calibration_state_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calibration_state.json"
            state = CalibrationState(
                kind="normalization",
                source="support_set",
                parameters={"mean": [0.1, 0.2], "std": [1.0, 1.1]},
                update_count=2,
            )

            write_calibration_state(state, path)
            loaded = read_calibration_state(path)

        self.assertEqual(loaded.kind, "normalization")
        self.assertEqual(loaded.update_count, 2)
        self.assertEqual(loaded.parameters["std"], [1.0, 1.1])

    def test_model_bundle_public_loader_validates_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "model.joblib"
            artifact.write_bytes(b"fixture-model")
            write_model_bundle(
                root / "bundle",
                kind="fixture_model",
                artifact_path=artifact,
                artifact_format="joblib",
                contract={"channel_names": ["Cz"], "sample_rate_hz": 100.0},
                metrics={"accuracy": 1.0},
                training_source={"source": "synthetic"},
            )

            bundle = load_model_bundle_object(root / "bundle")

        self.assertEqual(bundle.kind, "fixture_model")
        self.assertEqual(bundle.contract["channel_names"], ["Cz"])
        self.assertTrue(bundle.bundle_hash)

    def test_runtime_zip_import_validates_and_hashes_workbench_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "runtime.zip"
            runtime = {
                "kind": "foundation_head_logreg",
                "model_version": "fixture",
                "contract": {
                    "channel_names": ["Fz", "Cz"],
                    "sample_rate_hz": 100.0,
                    "sample_count": 200,
                    "epoch_window_seconds": [-2.0, 0.0],
                },
                "target": "attention_lapse_binary",
                "metrics": {"auprc": 0.6},
                "license": {"spdx": "MIT"},
                "provenance": {"source": "fixture"},
            }
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("runtime.json", json.dumps(runtime))
                archive.writestr("encoder.ts", b"fixture")

            manifest = import_runtime_bundle(source, root / "bundle")
            bundle = load_model_bundle_object(root / "bundle")

        self.assertEqual(manifest["artifact_format"], "eegle_runtime_zip")
        self.assertEqual(bundle.kind, "foundation_head_logreg")
        self.assertEqual(bundle.contract["epoch_window_seconds"], [-2.0, 0.0])
        self.assertEqual(bundle.manifest["runtime_export"]["license"]["spdx"], "MIT")

    def test_scientific_protocol_round_trips(self) -> None:
        protocol = ScientificProtocol(
            name="sart_attention_lapse_v1",
            task="sart",
            primary_endpoint="pre_stimulus_attention_lapse_auc_pr",
            prediction_window_seconds=(-4.0, 0.0),
            prediction_horizon="next_trial",
            targets=(ProtocolTarget("attention_lapse_binary", positive=("commission_error", "omission_error")),),
            splits=("within_session", "leave_session_out"),
            baselines=("behavior_only", "erp_roi_logreg"),
            metrics=("auc_roc", "auc_pr", "false_positives_per_minute"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "protocol.json"
            write_protocol(protocol, path)
            loaded = load_protocol(path)

        self.assertEqual(loaded.name, "sart_attention_lapse_v1")
        self.assertEqual(loaded.prediction_window_seconds, (-4.0, 0.0))
        self.assertEqual(loaded.targets[0].positive, ("commission_error", "omission_error"))

    def test_attention_lapse_protocol_declares_go_nogo_pre_stimulus_endpoint(self) -> None:
        protocol = attention_lapse_protocol()

        self.assertEqual(protocol.task, "go_nogo")
        self.assertEqual(protocol.primary_endpoint, "attention_lapse_risk")
        self.assertEqual(protocol.prediction_window_seconds, (-2.0, 0.0))
        self.assertIn("temporal_support_query", protocol.splits)
        self.assertIn("auprc", protocol.metrics)


if __name__ == "__main__":
    unittest.main()
