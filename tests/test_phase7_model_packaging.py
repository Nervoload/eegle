from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZIP_STORED, ZipFile

import numpy as np

from eegle._domain import EquivalenceLevel, ExecutionMode
from eegle.compiler import compile_suite
from eegle.models import (
    ModelArtifactSource,
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
    ModelPackageCheckReport,
    ModelPackageIntegrityError,
    ModelResult,
    ModelStateBehavior,
    ModelStateContract,
    SyntheticModelTestVector,
    SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID,
    build_model_manifest,
    build_model_package,
    check_model_package,
    materialize_model_package,
    open_model_package,
)
from eegle.operations.cli import main as cli_main
from eegle.operations import OperationOutcome, compare_session_models
from eegle.plugins import PluginRegistry
from eegle.recording import EvidenceReader, Session, persist_engine_run
from eegle.replay import BundleReplayRunner
from eegle.runtime import EngineStatus, ExecutionEngine
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import DenseSampleBatch, TimePoint
from tests.fixtures.build_phase7_sklearn_wheel import (
    build_phase7_sklearn_wheel,
)
from tests.fixtures.phase5_model_components import (
    bind_phase5_models,
    phase5_model_manifest,
    register_phase5_plugins,
)


ROOT = Path(__file__).resolve().parents[1]
PHASE5 = ROOT / "tests" / "fixtures" / "migration" / "phase5_simulated"


def _contract(*, stateful: bool = False) -> ModelContract:
    state = (
        ModelStateContract(
            behavior=ModelStateBehavior.SNAPSHOT_RESTORE,
            state_schema_id="fixture.phase7.state.v1",
            initial_state_required=True,
            state_affects_predictions=True,
            replay_equivalence=EquivalenceLevel.NUMERIC,
        )
        if stateful
        else ModelStateContract(replay_equivalence=EquivalenceLevel.NUMERIC)
    )
    return ModelContract(
        inputs=(ModelInputContract("features", "fixture.phase7.features.v1"),),
        outputs=(
            ModelOutputContract(
                "prediction",
                "eegle.prediction.v2",
                value_schema={
                    "type": "object",
                    "required": ["label", "score"],
                    "properties": {
                        "label": {"type": "string"},
                        "score": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
            ),
        ),
        state=state,
        supported_modes=frozenset({ExecutionMode.CAUSAL}),
    )


def _vector() -> SyntheticModelTestVector:
    return SyntheticModelTestVector(
        "vector.nominal",
        inputs={"features": {"values": [0.25, 0.75]}},
        expected_outputs={
            "prediction": ModelResult({"label": "positive", "score": 1.0})
        },
    )


def _phase5_payload(name: str) -> dict:
    return json.loads((PHASE5 / name).read_text(encoding="utf-8"))


def _packet() -> DenseSampleBatch:
    return DenseSampleBatch(
        batch_id="batch.phase7-replacement",
        stream_id="stream.neural",
        stream_revision=1,
        sequence_start=0,
        channel_ids=("channel.c3", "channel.c4"),
        values=np.asarray(
            [[-1.0, 0.0], [0.5, 1.0], [1.5, 2.0], [2.0, 3.0]],
            dtype=np.float64,
        ),
        received_time=TimePoint(0.099, "boundary.clock"),
        available_time=TimePoint(0.1, "boundary.clock"),
        first_sample_time=TimePoint(0.0, "device.clock"),
        sample_period_seconds=0.01,
    )


class Phase7ModelPackagingTests(unittest.TestCase):
    def test_deterministic_package_initial_state_materialization_and_tamper_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            weights = root / "weights.bin"
            weights.write_bytes(b"phase7-model-parameters")
            source = ModelArtifactSource.from_file(
                "artifact.weights",
                "model_parameters",
                "application/octet-stream",
                weights,
            )
            arguments = {
                "model_id": "model.phase7-packaging",
                "model_version": "1.0.0",
                "contract": _contract(stateful=True),
                "implementations": (
                    ModelImplementationRequirement("fixture.phase7.model", "~=1.0"),
                ),
                "artifacts": (source,),
                "initial_state": {
                    "schema": "fixture.phase7.state.v1",
                    "offset": 2.0,
                },
                "test_vectors": (_vector(),),
                "annotations": {"created_by": "external-model-pipeline"},
            }
            first = build_model_package(root / "first.eegle-model", **arguments)
            second = build_model_package(root / "second.eegle-model", **arguments)

            self.assertEqual(first.package_digest, second.package_digest)
            self.assertEqual(
                first.package_path.read_bytes(), second.package_path.read_bytes()
            )
            verified = open_model_package(first.package_path)
            self.assertEqual(verified.index.manifest, first.index.manifest)
            self.assertEqual(
                verified.index.manifest.initial_state_artifact_id,
                "artifact.initial-state",
            )
            materialized = materialize_model_package(
                first.package_path, root / "materialized"
            )
            self.assertEqual(
                {value.artifact_id for value in materialized.deployment_bindings()},
                {"artifact.weights", "artifact.initial-state"},
            )
            repeated = materialize_model_package(
                first.package_path, root / "materialized"
            )
            self.assertEqual(repeated.root, materialized.root)

            tampered = root / "tampered.eegle-model"
            with ZipFile(first.package_path, "r") as source_archive:
                members = {
                    info.filename: source_archive.read(info)
                    for info in source_archive.infolist()
                }
            artifact_member = next(
                value
                for value in verified.index.artifact_members.values()
                if members[value] == b"phase7-model-parameters"
            )
            members[artifact_member] = b"tampered-model-parameters"
            with ZipFile(tampered, "w", compression=ZIP_STORED) as target_archive:
                for name, content in members.items():
                    target_archive.writestr(name, content)
            with self.assertRaisesRegex(
                ModelPackageIntegrityError, "digest or size mismatch"
            ):
                open_model_package(tampered)

            python_source = root / "model.py"
            python_source.write_text("raise RuntimeError('never execute')\n")
            with self.assertRaisesRegex(ValueError, "Python source"):
                ModelArtifactSource.from_file(
                    "artifact.source",
                    "model_parameters",
                    "text/x-python",
                    python_source,
                )

            hierarchical = ModelArtifactSource(
                "artifact/../../logical",
                "model_parameters",
                "application/octet-stream",
                data=b"logical-identity",
            )
            hierarchical_package = build_model_package(
                root / "hierarchical.eegle-model",
                model_id="model.phase7-logical-id",
                model_version="1.0.0",
                contract=_contract(),
                implementations=(
                    ModelImplementationRequirement("fixture.phase7.model", "~=1.0"),
                ),
                artifacts=(hierarchical,),
            )
            hierarchical_materialization = materialize_model_package(
                hierarchical_package.package_path,
                root / "hierarchical-materialized",
            )
            materialized_path = hierarchical_materialization.artifact_paths[
                hierarchical.artifact_id
            ]
            self.assertEqual(
                materialized_path.parent,
                hierarchical_materialization.root / "artifacts",
            )

    def test_static_conformance_and_cli_pack_check_share_the_same_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifact = root / "parameters.bin"
            artifact.write_bytes(b"parameters")
            source = ModelArtifactSource.from_file(
                "artifact.parameters",
                "model_parameters",
                "application/octet-stream",
                artifact,
            )
            manifest = build_model_manifest(
                model_id="model.phase7-cli",
                model_version="1.0.0",
                contract=_contract(),
                implementations=(
                    ModelImplementationRequirement("fixture.uninstalled", "~=1.0"),
                ),
                artifacts=(source,),
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest.to_payload()), encoding="utf-8"
            )
            vectors_path = root / "vectors.json"
            vectors_path.write_text(
                json.dumps(
                    {
                        "schema": SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID,
                        "vectors": [_vector().to_payload()],
                    }
                ),
                encoding="utf-8",
            )
            package_path = root / "cli.eegle-model"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "--json",
                        "model",
                        "pack",
                        str(manifest_path),
                        str(package_path),
                        "--artifact",
                        f"artifact.parameters={artifact}",
                        "--vectors",
                        str(vectors_path),
                    ]
                )
            packed = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(packed["ok"])
            self.assertEqual(packed["operation"], "model")
            self.assertEqual(
                packed["result"]["manifest_digest"], manifest.manifest_digest
            )
            self.assertEqual(packed["result"]["test_vector_count"], 1)

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "--json",
                        "model",
                        "pack",
                        str(manifest_path),
                        str(package_path),
                        "--artifact",
                        f"artifact.parameters={artifact}",
                    ]
                )
            refused = json.loads(output.getvalue())
            self.assertEqual(exit_code, 4)
            self.assertEqual(
                refused["diagnostics"][0]["code"], "model.package_exists"
            )

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "--json",
                        "model",
                        "check",
                        str(package_path),
                        "--no-entry-points",
                    ]
                )
            checked = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(checked["result"]["ready"])
            self.assertEqual(
                checked["result"]["issues"][0]["severity"], "warning"
            )

            corrupt_package = root / "corrupt.eegle-model"
            corrupt_package.write_bytes(b"not-a-model-package")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    ["--json", "model", "check", str(corrupt_package)]
                )
            rejected = json.loads(output.getvalue())
            self.assertEqual(exit_code, 7)
            self.assertEqual(
                rejected["diagnostics"][0]["code"],
                "model.package_integrity",
            )

            registry = PluginRegistry()
            report = check_model_package(package_path, registry=registry)
            self.assertTrue(report.ready)
            self.assertEqual(report.artifact_count, 1)
            self.assertEqual(
                ModelPackageCheckReport.from_payload(report.to_payload()),
                report,
            )
            required = check_model_package(
                package_path, registry=registry, require_implementation=True
            )
            self.assertFalse(required.ready)

    @unittest.skipUnless(
        importlib.util.find_spec("sklearn")
        and importlib.util.find_spec("joblib"),
        "scikit-learn and joblib are optional adapter-test dependencies",
    )
    def test_independently_installed_sklearn_adapter_runs_normal_runtime(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "phase7_sklearn_model_dist"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wheel = build_phase7_sklearn_wheel(fixture, root / "wheel")
            target = root / "installed"
            installed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--target",
                    str(target),
                    str(wheel),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            script = (fixture / "exercise.py").read_text(encoding="utf-8").replace(
                "__TARGET__", repr(str(target))
            )
            executed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertIn("phase7-sklearn-adapter-ok", executed.stdout)

    def test_replacement_model_replays_recorded_inputs_without_framework_engine(
        self,
    ) -> None:
        protocol = ProtocolSpec.from_payload(_phase5_payload("protocol.json"))
        suite = bind_phase5_models(
            SuiteSpec.from_payload(_phase5_payload("suite.json"))
        )
        deployment_payload = _phase5_payload("deployment.json")
        deployment_payload["component_bindings"][0]["config"]["packets"] = [
            _packet().to_payload()
        ]
        deployment = DeploymentSpec.from_payload(deployment_payload)
        original_manifest = phase5_model_manifest()
        registry = PluginRegistry()
        registry.register_builtins()
        register_phase5_plugins(registry)
        original = compile_suite(
            protocol,
            suite,
            deployment,
            registry,
            model_manifests={
                original_manifest.manifest_digest: original_manifest
            },
        )
        engine = ExecutionEngine.from_plan(original.plan, registry)
        run = engine.run()
        self.assertEqual(run.status, EngineStatus.COMPLETE, run.failure)
        stream = engine.runtime.node("source.neural").component.stream_spec

        candidate_manifest = ModelManifest(
            model_id="model.phase7-replacement",
            model_version="2.0.0",
            contract=original_manifest.contract,
            artifacts=(),
            implementations=original_manifest.implementations,
            annotations={"replacement_for": original_manifest.manifest_digest},
        )
        candidate_payload = deepcopy(suite.to_payload())
        model_component = next(
            value
            for value in candidate_payload["components"]
            if value["component_id"] == "model.primary"
        )
        model_component["config"]["positive_label"] = "candidate-positive"
        model_component["config"]["negative_label"] = "candidate-negative"
        candidate_payload["model_uses"][0][
            "manifest_digest"
        ] = candidate_manifest.manifest_digest
        candidate = compile_suite(
            protocol,
            SuiteSpec.from_payload(candidate_payload),
            deployment,
            registry,
            model_manifests={
                candidate_manifest.manifest_digest: candidate_manifest
            },
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            session = Session.create(
                Path(temporary_directory) / "session",
                session_id="session.phase7-replacement",
            )
            bundle = persist_engine_run(
                session,
                run,
                plan=original.plan,
                streams=(stream,),
            )
            replacement = BundleReplayRunner(registry).run_with_model_replacements(
                EvidenceReader.open(session, bundle.bundle_id),
                candidate.plan,
                replaced_components=("model.primary",),
            )
            public_comparison = compare_session_models(
                session.root,
                candidate.plan,
                replaced_components=("model.primary",),
                registry=registry,
            )
            with self.assertRaisesRegex(ValueError, "preserve plan identity"):
                BundleReplayRunner(registry).run_with_model_replacements(
                    EvidenceReader.open(session, bundle.bundle_id),
                    replace(candidate.plan, plan_id="plan.changed"),
                    replaced_components=("model.primary",),
                )

        self.assertEqual(replacement.original_plan_hash, original.plan.plan_hash)
        self.assertEqual(replacement.replacement_plan_hash, candidate.plan.plan_hash)
        self.assertEqual(replacement.replaced_components, ("model.primary",))
        self.assertEqual(replacement.execution.result.status, EngineStatus.COMPLETE)
        self.assertFalse(replacement.execution.equivalence.equivalent)
        self.assertEqual(public_comparison.outcome, OperationOutcome.DIVERGED)
        self.assertEqual(public_comparison.result_status, EngineStatus.COMPLETE)
        self.assertIsNotNone(public_comparison.first_divergence)
        self.assertFalse(public_comparison.to_payload()["operation_safety"]["data_deleted"])
        prediction = replacement.execution.result.phase_results[0].emissions_from(
            "model.primary", "prediction"
        )[0]
        self.assertIn("candidate-", prediction.value["label"])


if __name__ == "__main__":
    unittest.main()
