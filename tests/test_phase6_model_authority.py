from __future__ import annotations

from copy import deepcopy
import unittest

from eegle._domain import EquivalenceLevel, ExecutionMode
from eegle.models import (
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
    ModelStateBehavior,
    ModelStateContract,
    PreprocessingOwnership,
    PreprocessingRequirement,
)
from eegle.recording import ArtifactReference, Sensitivity


def _contract() -> ModelContract:
    return ModelContract(
        inputs=(
            ModelInputContract(
                port_name="observations",
                type_id="eegle.dense_window.v1",
                requirements={
                    "content_kind": "dense_samples",
                    "rate_model": "irregular",
                    "channel_ids": ["sensor.oxy", "sensor.deoxy"],
                    "units": {"sensor.oxy": "mol/m2", "sensor.deoxy": "mol/m2"},
                    "minimum_duration_seconds": 12.0,
                    "missing_data_policy": "validity_mask",
                },
                preprocessing=(
                    PreprocessingRequirement(
                        requirement_id="requirement.detrend",
                        operation="detrend",
                        ownership=PreprocessingOwnership.UPSTREAM,
                        parameters={"kind": "linear"},
                        required_lineage=("component.detrend",),
                    ),
                    PreprocessingRequirement(
                        requirement_id="requirement.normalization",
                        operation="normalization",
                        ownership=PreprocessingOwnership.MODEL_INTERNAL,
                    ),
                ),
            ),
            ModelInputContract(
                port_name="events",
                type_id="eegle.sparse_event_batch.v1",
                requirements={"event_kinds": ["behavior.response"]},
            ),
        ),
        outputs=(
            ModelOutputContract(
                port_name="estimate",
                type_id="eegle.model_result.v1",
                value_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "number"}},
                    "additionalProperties": False,
                },
                uncertainty_schema={
                    "type": "object",
                    "required": ["lower", "upper"],
                    "properties": {
                        "lower": {"type": "number"},
                        "upper": {"type": "number"},
                    },
                },
                abstention_supported=True,
            ),
        ),
        state=ModelStateContract(
            behavior=ModelStateBehavior.SNAPSHOT_RESTORE,
            state_schema_id="example.slow_model_state.v1",
            initial_state_required=True,
            adaptation_supported=True,
            state_affects_predictions=True,
            replay_equivalence=EquivalenceLevel.NUMERIC,
        ),
        supported_modes=frozenset({ExecutionMode.CAUSAL, ExecutionMode.RETROSPECTIVE}),
        metadata={"description": "synthetic slow-signal contract"},
    )


def _manifest() -> ModelManifest:
    return ModelManifest(
        model_id="model.slow-estimator",
        model_version="1.2.0",
        contract=_contract(),
        artifacts=(
            ArtifactReference(
                artifact_id="artifact.weights",
                role="model_parameters",
                uri="artifact://artifact.weights",
                digest="sha256:" + "1" * 64,
                media_type="application/octet-stream",
                size_bytes=1024,
                sensitivity=Sensitivity.INTERNAL,
            ),
            ArtifactReference(
                artifact_id="artifact.initial-state",
                role="model_initial_state",
                uri="artifact://artifact.initial-state",
                digest="sha256:" + "2" * 64,
                media_type="application/json",
                size_bytes=128,
                sensitivity=Sensitivity.INTERNAL,
            ),
        ),
        implementations=(
            ModelImplementationRequirement(
                plugin_id="example.models.slow-estimator",
                version_spec="~=1.2",
            ),
        ),
        initial_state_artifact_id="artifact.initial-state",
        training_provenance={"dataset_digest": "sha256:" + "3" * 64},
        evaluation_provenance={"protocol_id": "protocol.slow-evaluation"},
        license="MIT",
    )


class Phase6ModelAuthorityTests(unittest.TestCase):
    def test_modality_neutral_contract_round_trips_and_is_content_addressed(self) -> None:
        contract = _contract()

        restored = ModelContract.from_payload(contract.to_payload())

        self.assertEqual(restored, contract)
        self.assertEqual(restored.contract_digest, contract.contract_digest)
        self.assertEqual(
            restored.inputs[0].requirements["rate_model"],
            "irregular",
        )
        self.assertNotIn("target", contract.to_payload())
        self.assertNotIn("confidence", contract.to_payload())

    def test_contract_digest_rejects_semantic_tampering(self) -> None:
        payload = _contract().to_payload()
        payload["outputs"][0]["value_schema"]["properties"]["value"]["type"] = "integer"

        with self.assertRaisesRegex(ValueError, "contract digest mismatch"):
            ModelContract.from_payload(payload)

        payload = _contract().to_payload()
        del payload["contract_digest"]
        with self.assertRaisesRegex(ValueError, "requires contract_digest"):
            ModelContract.from_payload(payload)

    def test_output_contract_rejects_an_invalid_json_schema(self) -> None:
        with self.assertRaises(ValueError):
            ModelOutputContract(
                port_name="output",
                type_id="eegle.model_result.v1",
                value_schema={"type": "definitely-not-a-json-schema-type"},
            )

    def test_model_manifest_is_path_free_canonical_data(self) -> None:
        manifest = _manifest()

        restored = ModelManifest.from_payload(manifest.to_payload())

        self.assertEqual(restored, manifest)
        self.assertEqual(restored.manifest_digest, manifest.manifest_digest)
        self.assertTrue(all(value.uri.startswith("artifact://") for value in restored.artifacts))
        payload_text = str(restored.to_payload())
        self.assertNotIn("bundle_dir", payload_text)
        self.assertNotIn("artifact_path", payload_text)
        self.assertNotIn("software", payload_text)

    def test_procedural_model_manifest_does_not_require_a_file_artifact(self) -> None:
        contract = ModelContract(
            inputs=(
                ModelInputContract(
                    port_name="input",
                    type_id="eegle.scalar.v1",
                ),
            ),
            outputs=(
                ModelOutputContract(
                    port_name="output",
                    type_id="eegle.model_result.v1",
                    value_schema={"type": "number"},
                ),
            ),
        )
        manifest = ModelManifest(
            model_id="model.procedural-threshold",
            model_version="1.0.0",
            contract=contract,
            artifacts=(),
            implementations=(
                ModelImplementationRequirement(
                    plugin_id="eegle.models.procedural-threshold",
                    version_spec="~=1.0",
                ),
            ),
        )

        self.assertEqual(ModelManifest.from_payload(manifest.to_payload()), manifest)

    def test_model_manifest_rejects_tampering_and_site_paths(self) -> None:
        payload = deepcopy(_manifest().to_payload())
        payload["model_version"] = "1.2.1"
        with self.assertRaisesRegex(ValueError, "manifest digest mismatch"):
            ModelManifest.from_payload(payload)

        manifest = _manifest()
        local = ArtifactReference(
            artifact_id="artifact.weights",
            role="model_parameters",
            uri="file:///tmp/weights.bin",
            digest="sha256:" + "4" * 64,
            media_type="application/octet-stream",
            size_bytes=1,
        )
        with self.assertRaisesRegex(ValueError, "logical artifact://"):
            ModelManifest(
                model_id=manifest.model_id,
                model_version=manifest.model_version,
                contract=manifest.contract,
                artifacts=(local,),
                implementations=manifest.implementations,
                initial_state_artifact_id=None,
            )

    def test_state_contract_rejects_hidden_state_on_stateless_models(self) -> None:
        with self.assertRaisesRegex(ValueError, "stateless"):
            ModelStateContract(
                behavior=ModelStateBehavior.STATELESS,
                adaptation_supported=True,
            )


if __name__ == "__main__":
    unittest.main()
