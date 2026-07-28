from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from eegle.authoring import (
    AUTHORING_PROJECT_SCHEMA_ID,
    AuthoringOrigin,
    CanonicalArtifact,
    DraftLoweringError,
    ExperimentBuilder,
    RestrictedYamlError,
    RestrictedYamlLimits,
    SourceKind,
    load_yaml_experiment,
    parse_yaml_experiment,
    read_yaml_experiment,
    validate_template_authoring_payload,
)
from eegle.compiler import CompilationError, canonical_hash
from eegle.plugins import PluginRegistry
from eegle.runtime import ExecutionEngine
from eegle.specs import DeploymentSpec
from tests.fixtures.phase5_model_components import (
    phase5_model_manifest,
    register_phase5_plugins,
)
from tests.test_phase7_templates import _deployment


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "migration" / "phase7_authoring"
YAML_AVAILABLE = importlib.util.find_spec("ruamel") is not None


def _golden() -> dict:
    return json.loads((FIXTURE / "golden.json").read_text(encoding="utf-8"))


def _recording_builder() -> ExperimentBuilder:
    return ExperimentBuilder.continuous_recording("golden.recording").signal(
        unit="uV",
        channel_count=8,
        sample_rate_hz=250.0,
    )


def _comparison_builder() -> ExperimentBuilder:
    manifest = phase5_model_manifest()
    return (
        ExperimentBuilder.primary_shadow_comparison(
            "golden.comparison",
            model_plugin_id=manifest.implementations[0].plugin_id,
            model_manifest_digest=manifest.manifest_digest,
        )
        .signal(unit="uV", channel_count=2, sample_rate_hz=100.0)
        .window(event_kind="stimulus.target")
        .model(
            manifest.implementations[0].plugin_id,
            manifest.manifest_digest,
            threshold=0.25,
            shadow_threshold=0.5,
        )
    )


def _assert_golden(test: unittest.TestCase, name: str, authored) -> None:
    expected = _golden()["cases"][name]
    test.assertEqual(authored.protocol.spec_hash, expected["protocol_hash"])
    test.assertEqual(authored.suite.spec_hash, expected["suite_hash"])
    test.assertEqual(
        authored.requirements.requirements_hash,
        expected["deployment_requirements_hash"],
    )
    test.assertEqual(authored.expansion.expansion_digest, expected["expansion_digest"])


class Phase7PythonAuthoringTests(unittest.TestCase):
    def test_builder_is_persistent_typed_and_exposes_defaults_draft_and_requirements(self) -> None:
        original = ExperimentBuilder.continuous_recording("persistent")
        changed = original.signal(unit="mV", channel_count=4, sample_rate_hz=200.0)

        self.assertEqual(dict(original.parameters), {})
        self.assertEqual(changed.parameters["unit"], "mV")
        self.assertEqual(changed.defaults["unit"], "uV")
        self.assertIn("source:dense", changed.required_capabilities)
        self.assertEqual(
            changed.draft.template.manifest_digest,
            changed.template.manifest_digest,
        )
        validate_template_authoring_payload(changed.to_source_payload())
        authored = changed.build()
        self.assertEqual(authored.draft, changed.draft)
        self.assertTrue(authored.requirements.requirements)
        self.assertNotIn("run", dir(authored))

    def test_python_recording_and_comparison_match_canonical_goldens(self) -> None:
        _assert_golden(self, "recording", _recording_builder().build())
        _assert_golden(self, "comparison", _comparison_builder().build())

    def test_canonical_export_and_project_writing_are_inspectable_and_non_mutating(self) -> None:
        authored = _recording_builder().build()
        exported = json.loads(authored.canonical_json())

        self.assertEqual(exported["protocol"], authored.protocol.to_payload())
        self.assertEqual(exported["suite"], authored.suite.to_payload())
        with tempfile.TemporaryDirectory() as directory:
            project = authored.write_project(Path(directory) / "project")
            manifest = json.loads(
                project.files["authoring-project.json"].read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema"], AUTHORING_PROJECT_SCHEMA_ID)
            self.assertEqual(project.manifest_digest, canonical_hash(manifest))
            self.assertEqual(
                json.loads(project.files["protocol.json"].read_text(encoding="utf-8")),
                authored.protocol.to_payload(),
            )
            with self.assertRaises(FileExistsError):
                authored.write_project(project.root)

    def test_compile_facade_cannot_bypass_compiler_or_runtime_plan_boundary(self) -> None:
        authored = ExperimentBuilder.continuous_recording("compile-facade").build()
        registry = PluginRegistry()
        registry.register_builtins()
        deployment = _deployment(
            authored.suite,
            "phase5_event_window_actions/deployment.json",
        )

        compiled = authored.compile(deployment, registry)
        self.assertTrue(compiled.valid)
        invalid = deployment.to_payload()
        invalid["suite_id"] = "suite.not-this-experiment"
        with self.assertRaises(CompilationError):
            authored.compile(DeploymentSpec.from_payload(invalid), registry)
        with self.assertRaises(TypeError):
            ExecutionEngine.from_plan(authored, registry)

    def test_builder_rejects_out_of_profile_operations_and_maps_parameter_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown parameter"):
            ExperimentBuilder.continuous_recording("invalid").model(
                "fixture.model",
                "sha256:" + "a" * 64,
            )
        manifest = phase5_model_manifest()
        invalid = ExperimentBuilder.primary_shadow_comparison(
            "invalid-model",
            model_plugin_id=manifest.implementations[0].plugin_id,
            model_manifest_digest=manifest.manifest_digest,
        ).signal(channel_count=0)
        with self.assertRaises(DraftLoweringError) as raised:
            invalid.build()
        self.assertEqual(
            raised.exception.issues[0].path,
            "/template/parameters/channel_count",
        )
        self.assertEqual(raised.exception.issues[0].source.kind, SourceKind.PYTHON)


@unittest.skipUnless(YAML_AVAILABLE, "ruamel.yaml optional authoring extra unavailable")
class Phase7RestrictedYamlTests(unittest.TestCase):
    def test_yaml_and_python_surfaces_produce_identical_canonical_hashes(self) -> None:
        for name, python_builder in (
            ("recording", _recording_builder()),
            ("comparison", _comparison_builder()),
        ):
            with self.subTest(case=name):
                source = FIXTURE / _golden()["cases"][name]["source"]
                yaml_authored = read_yaml_experiment(source)
                python_authored = python_builder.build()
                _assert_golden(self, name, yaml_authored)
                self.assertEqual(
                    yaml_authored.protocol.spec_hash,
                    python_authored.protocol.spec_hash,
                )
                self.assertEqual(
                    yaml_authored.suite.spec_hash,
                    python_authored.suite.spec_hash,
                )
                self.assertEqual(
                    yaml_authored.requirements.requirements_hash,
                    python_authored.requirements.requirements_hash,
                )
                self.assertEqual(
                    json.loads(parse_yaml_experiment(
                        source.read_text(encoding="utf-8"),
                        locator=str(source),
                    ).canonical_source_json()),
                    python_builder.to_source_payload(),
                )

    def test_yaml_parameter_provenance_retains_line_and_column(self) -> None:
        source = FIXTURE / "recording.yaml"
        authored = read_yaml_experiment(source)
        entry = next(
            value
            for value in authored.provenance.entries
            if value.target_artifact == CanonicalArtifact.SUITE
            and value.target_path == "/streams/0/contract/unit"
        )

        self.assertEqual(entry.origin, AuthoringOrigin.USER_EXPLICIT)
        self.assertEqual(entry.source.kind, SourceKind.YAML)
        self.assertEqual(entry.source.locator, str(source))
        self.assertEqual(entry.source.line, 8)
        self.assertGreater(entry.source.column, 1)

    def test_unsafe_yaml_constructs_are_rejected_with_locations(self) -> None:
        cases = {
            "anchor": "schema: &s eegle.template_authoring.v1\n",
            "alias": "schema: &s eegle.template_authoring.v1\ndraft_id: *s\n",
            "tag": "schema: !!str eegle.template_authoring.v1\n",
            "merge": "template:\n  <<: {parameters: {}}\n",
            "duplicate": "draft_id: first\ndraft_id: second\n",
            "date": "value: 2026-07-28\n",
            "multiple_documents": "draft_id: first\n---\ndraft_id: second\n",
            "object": "value: !!python/name:os.system\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(RestrictedYamlError) as raised:
                    load_yaml_experiment(text, locator=f"{name}.yaml")
                self.assertEqual(raised.exception.source.kind, SourceKind.YAML)
                self.assertEqual(raised.exception.source.locator, f"{name}.yaml")
                self.assertIsNotNone(raised.exception.source.line)
                self.assertIsNotNone(raised.exception.source.column)

    def test_untyped_units_and_invalid_parameters_map_to_yaml_source(self) -> None:
        text = """schema: eegle.template_authoring.v1
draft_id: invalid-unit
template:
  template_id: eegle.template.continuous_recording
  version: 1.0.0
  parameters:
    sample_rate_hz: 250 Hz
"""
        with self.assertRaises(DraftLoweringError) as raised:
            load_yaml_experiment(text, locator="units.yaml")
        issue = raised.exception.issues[0]
        self.assertEqual(issue.path, "/template/parameters/sample_rate_hz")
        self.assertEqual(issue.source.kind, SourceKind.YAML)
        self.assertEqual(issue.source.locator, "units.yaml")
        self.assertEqual(issue.source.line, 7)

    def test_yaml_resource_limits_reject_before_authoring(self) -> None:
        cases = (
            (
                "yaml.limit_bytes",
                RestrictedYamlLimits(max_bytes=4),
            ),
            (
                "yaml.limit_depth",
                RestrictedYamlLimits(max_depth=1),
            ),
            (
                "yaml.limit_nodes",
                RestrictedYamlLimits(max_nodes=1),
            ),
            (
                "yaml.limit_scalar",
                RestrictedYamlLimits(max_scalar_characters=3),
            ),
        )
        for code, limits in cases:
            with self.subTest(code=code):
                with self.assertRaises(RestrictedYamlError) as raised:
                    load_yaml_experiment("draft_id: too-large\n", limits=limits)
                self.assertEqual(raised.exception.code, code)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "too-large.yaml"
            source.write_text("draft_id: too-large\n", encoding="utf-8")
            with self.assertRaises(RestrictedYamlError) as raised:
                read_yaml_experiment(source, limits=RestrictedYamlLimits(max_bytes=4))
            self.assertEqual(raised.exception.code, "yaml.limit_bytes")
            self.assertEqual(raised.exception.source.locator, str(source))


if __name__ == "__main__":
    unittest.main()
