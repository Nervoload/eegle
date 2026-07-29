from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import numpy as np

from eegle.authoring import (
    BUILTIN_DESIGN_PRESETS,
    DESIGN_PRESET_VERSION,
    ExperimentDesign,
    reference_model_manifests,
)
from eegle.models import (
    ModelResult,
    ModelStateBehavior,
    SyntheticModelTestVector,
    check_model_package,
    pack_model,
)
from eegle.operations import (
    compile_project,
    create_project,
    open_project,
    read_project_authoring,
    run_project,
)
from eegle.plugins import PluginRegistry
from eegle.specs import DeploymentSpec


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_ROOT = ROOT / "reference_projects"
EXAMPLE_MODELS = (
    ROOT
    / "examples"
    / "plugins"
    / "eegle-example-models"
    / "src"
    / "eegle_example_models"
    / "__init__.py"
)


class Phase7ReferenceProjectTests(unittest.TestCase):
    def test_exact_compositional_presets_expand_deterministically(self) -> None:
        self.assertEqual(len(BUILTIN_DESIGN_PRESETS.presets), 6)
        for preset in BUILTIN_DESIGN_PRESETS.presets:
            self.assertEqual(preset.version, DESIGN_PRESET_VERSION)
            first = preset.expand(f"test.{preset.preset_id.rsplit('.', 1)[-1]}")
            second = preset.expand(f"test.{preset.preset_id.rsplit('.', 1)[-1]}")
            self.assertEqual(first.design_digest, second.design_digest)
            self.assertEqual(first.build().suite.spec_hash, second.build().suite.spec_hash)

    def test_cli_project_service_consumes_a_normalized_design(self) -> None:
        design = BUILTIN_DESIGN_PRESETS.resolve(
            "eegle.preset.recording",
            DESIGN_PRESET_VERSION,
        ).expand("normalized.project")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "design.json"
            source.write_text(json.dumps(design.to_payload()), encoding="utf-8")
            project = create_project(
                Path(directory) / "project",
                project_id=design.experiment_id,
                design=source,
            )
            authored = read_project_authoring(open_project(project.root))
            self.assertIsInstance(authored.design, ExperimentDesign)
            compiled = compile_project(project.root)
            run = run_project(project.root, session_id="session.normalized.project")
            self.assertEqual(compiled.plan.spec_hashes["suite"], authored.suite.spec_hash)
            self.assertTrue(run.successful)

    def test_six_reference_projects_are_normalized_and_self_indexed(self) -> None:
        projects = tuple(sorted(path for path in REFERENCE_ROOT.iterdir() if path.is_dir()))
        self.assertEqual(len(projects), 6)
        for project_root in projects:
            project = open_project(project_root)
            source = json.loads(project.path_for("authoring_source").read_text(encoding="utf-8"))
            self.assertEqual(source["schema"], "eegle.experiment_design.v1")
            self.assertTrue((project_root / "README.md").is_file())
            self.assertTrue(project.path_for("protocol").is_file())
            self.assertTrue(project.path_for("suite").is_file())
            self.assertTrue(project.path_for("simulation_deployment").is_file())

    def test_distinct_reference_implementations_survive_model_packaging(self) -> None:
        specification = importlib.util.spec_from_file_location(
            "eegle_phase7_example_models",
            EXAMPLE_MODELS,
        )
        if specification is None or specification.loader is None:
            self.fail("could not load the packaged example model distribution")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)

        registry = PluginRegistry()
        for descriptor in module.plugin_descriptors():
            registry.register(descriptor)

        manifests = {value.model_id: value for value in reference_model_manifests()}
        cases = (
            (
                "eegle.reference.mean-threshold",
                "eegle.example_models.mean_threshold",
                {"label": "negative", "score": -0.5, "threshold": 0.0},
            ),
            (
                "eegle.reference.peak-threshold",
                "eegle.example_models.peak_threshold",
                {"label": "positive", "score": 2.0, "threshold": 0.0},
            ),
        )
        observed_scores: list[float] = []
        with tempfile.TemporaryDirectory() as directory:
            for model_id, plugin_id, expected in cases:
                vector = SyntheticModelTestVector(
                    "vector.distinct-algorithm",
                    {"window": {"values": [[-2.0], [1.0]]}},
                    {"prediction": ModelResult(expected)},
                )
                package = pack_model(
                    manifests[model_id],
                    (),
                    Path(directory) / f"{model_id}.eegle-model",
                    test_vectors=(vector,),
                )
                report = check_model_package(
                    package.package_path,
                    registry=registry,
                    require_implementation=True,
                )
                self.assertTrue(report.ready, report.issues)
                self.assertEqual(report.test_vector_count, 1)
                self.assertEqual(report.compatible_plugins, (plugin_id,))

                implementation = registry.create(plugin_id, {"threshold": 0.0})
                result = implementation.predict(
                    SimpleNamespace(
                        values=np.array([[-2.0], [1.0]]),
                        validity_mask=None,
                    ),
                    SimpleNamespace(),
                )
                observed_scores.append(float(result.value["score"]))
        self.assertEqual(observed_scores, [-0.5, 2.0])

    def test_adaptation_reference_separates_stateful_intent_and_simulation_grant(self) -> None:
        project = open_project(REFERENCE_ROOT / "04-adaptation")
        authored = read_project_authoring(project)
        manifest = next(
            value
            for value in reference_model_manifests()
            if value.manifest_digest == authored.suite.model_uses[0].manifest_digest
        )
        self.assertEqual(
            manifest.contract.state.behavior,
            ModelStateBehavior.SNAPSHOT_RESTORE,
        )
        self.assertTrue(manifest.contract.state.adaptation_supported)
        self.assertTrue(manifest.contract.state.state_affects_predictions)
        deployment = DeploymentSpec.load(project.path_for("simulation_deployment"))
        self.assertEqual(
            tuple(
                (value.capability, value.component_ids)
                for value in deployment.permissions
            ),
            (("adaptation", ("model.primary",)),),
        )
        with tempfile.TemporaryDirectory() as directory:
            ordinary = create_project(
                Path(directory) / "adaptation",
                project_id="ordinary.adaptation",
                preset_id="eegle.preset.adaptation",
            )
            ordinary_deployment = DeploymentSpec.load(
                ordinary.path_for("simulation_deployment")
            )
            self.assertEqual(ordinary_deployment.permissions, ())


if __name__ == "__main__":
    unittest.main()
