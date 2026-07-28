from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from eegle._domain import ComponentKind
from eegle.authoring import (
    BUILTIN_TEMPLATE_CATALOG,
    TEMPLATE_EXPANSION_LOCK_SCHEMA_ID,
    AuthoringOrigin,
    CanonicalArtifact,
    DraftLoweringError,
    DraftSourceMap,
    ExperimentDraft,
    SourceKind,
    SourceLocation,
    TemplateCatalog,
    TemplateDefinition,
    TemplateExpansionLock,
    TemplateMigrationPolicy,
    TemplateProfile,
    builtin_templates,
    expand_template,
    lower_experiment_draft,
    validate_deployment_requirements_payload,
)
from eegle.compiler import compile_suite
from eegle.plugins import PluginRegistry
from eegle.specs import DeploymentSpec, SchemaValidationError
from tests.fixtures.phase5_model_components import (
    phase5_model_manifest,
    register_phase5_plugins,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    ROOT / "tests" / "fixtures" / "migration" / "phase7_template_fixtures.json"
)
DEPLOYMENT_ROOT = (
    ROOT / "tests" / "fixtures" / "migration"
)


def _fixture_inventory() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _expansion(fixture: dict):
    return expand_template(
        fixture["template_id"],
        fixture["version"],
        draft_id=fixture["draft_id"],
        parameters=fixture["parameters"],
    )


def _deployment(suite, fixture_name: str) -> DeploymentSpec:
    payload = json.loads(
        (DEPLOYMENT_ROOT / fixture_name).read_text(encoding="utf-8")
    )
    dense_binding = next(
        value
        for value in payload["component_bindings"]
        if value["component_id"] == "source.neural"
    )
    sparse_binding = next(
        value
        for value in payload["component_bindings"]
        if value["component_id"] == "source.markers"
    )
    stream_by_id = {value.stream_id: value for value in suite.streams}
    bindings = []
    resources = []
    stream_bindings = []
    for component in suite.components:
        if component.kind != ComponentKind.SOURCE:
            continue
        stream = stream_by_id[component.stream_id]
        contract = stream.contract.to_payload()
        sparse = contract["type_id"] == "eegle.sparse_event_batch.v1"
        binding = deepcopy(sparse_binding if sparse else dense_binding)
        resource_id = "resource." + component.component_id.split(".", 1)[-1]
        binding["component_id"] = component.component_id
        binding["resource_ids"] = [resource_id]
        stream_spec = binding["config"]["stream_spec"]
        stream_spec["stream_id"] = stream.stream_id
        stream_spec["modality"] = stream.modality
        stream_spec["content_kind"] = (
            "sparse_events" if sparse else "dense_samples"
        )
        stream_spec["rate_model"] = "event" if sparse else "regular"
        stream_spec["clock_id"] = stream.clock_id
        if sparse:
            stream_spec["channels"] = []
            stream_spec["sample_rate_hz"] = None
            stream_spec["sample_dtype"] = None
        else:
            channels = deepcopy(dense_binding["config"]["stream_spec"]["channels"])
            stream_spec["channels"] = channels[: int(contract["channel_count"])]
            stream_spec["sample_rate_hz"] = contract["nominal_rate_hz"]
            for channel in stream_spec["channels"]:
                channel["unit"] = contract["unit"]
        bindings.append(binding)
        resources.append(
            {
                "resource_id": resource_id,
                "kind": "simulator",
                "selector": {"generator": "packet_sequence"},
                "capabilities": ["deterministic"],
                "contract": contract,
            }
        )
        stream_bindings.append(
            {
                "stream_id": stream.stream_id,
                "resource_id": resource_id,
                "selector": {},
            }
        )

    actuator_ids = {
        value.component_id
        for value in suite.components
        if value.kind == ComponentKind.ACTUATOR
    }
    adaptive_ids = {
        value.component_id
        for value in suite.components
        if value.required_outcome_use == "adaptation"
    }
    permissions = []
    if actuator_ids:
        permissions.append(
            {
                **deepcopy(payload["permissions"][0]),
                "component_ids": sorted(actuator_ids),
            }
        )
    if adaptive_ids:
        permissions.append(
            {
                "permission_id": "permission.adaptation",
                "capability": "adaptation",
                "component_ids": sorted(adaptive_ids),
                "authorization_ref": "authorization.simulation",
                "operator_confirmation": False,
            }
        )
    payload.update(
        deployment_id=f"deployment.{suite.suite_id.split('.', 1)[-1]}",
        suite_id=suite.suite_id,
        component_bindings=bindings,
        resources=resources,
        stream_bindings=stream_bindings,
        storage=[
            {
                "storage_id": "storage.evidence",
                "kind": "evidence",
                "uri": "memory://phase7/template-fixture",
            }
        ],
        permissions=permissions,
        authorization_providers=(
            deepcopy(payload["authorization_providers"])
            if actuator_ids or adaptive_ids
            else []
        ),
        secrets=[],
        clock_mappings=[
            {
                "source_clock": clock,
                "target_clock": "boundary.clock",
                "strategy": "declared_affine",
                "maximum_uncertainty_seconds": 0.001,
            }
            for clock in sorted(
                {
                    value.clock_id
                    for value in suite.streams
                    if value.clock_id != "boundary.clock"
                }
            )
        ],
        model_artifacts=[],
    )
    return DeploymentSpec.from_payload(payload)


class Phase7TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = _fixture_inventory()
        cls.fixtures = {
            value["fixture_id"]: value for value in cls.inventory["fixtures"]
        }

    def test_catalog_declares_the_eight_bounded_non_executable_profiles(self) -> None:
        templates = builtin_templates()

        self.assertEqual(len(templates), 8)
        self.assertEqual({value.profile for value in templates}, set(TemplateProfile))
        self.assertEqual({value.version for value in templates}, {"1.0.0"})
        self.assertEqual(
            {value.synthetic_fixture for value in templates},
            set(self.fixtures),
        )
        for template in templates:
            with self.subTest(template=template.template_id):
                self.assertEqual(
                    TemplateDefinition.from_payload(template.to_payload()),
                    template,
                )
                self.assertEqual(
                    template.migration_policy,
                    TemplateMigrationPolicy.EXPLICIT_REEXPANSION,
                )
                self.assertTrue(template.parameter_schema["properties"])
                self.assertTrue(template.generated_schemas)
                self.assertTrue(template.required_capabilities)
                self.assertTrue(template.deployment_requirement_kinds)
                self.assertTrue(template.scientific_defaults)
                self.assertNotIn("factory", template.to_payload())
                self.assertNotIn("implementation", template.to_payload())

    def test_synthetic_expansions_are_deterministic_digest_locked_and_round_trip(self) -> None:
        self.assertEqual(
            self.inventory["schema"],
            "eegle.migration.phase7_template_fixtures.v1",
        )
        for fixture in self.fixtures.values():
            with self.subTest(fixture=fixture["fixture_id"]):
                first = _expansion(fixture)
                second = _expansion(fixture)
                self.assertEqual(first.expansion_digest, second.expansion_digest)
                self.assertEqual(
                    first.expansion_digest,
                    first.template.synthetic_expansion_digest,
                )
                self.assertEqual(
                    {
                        value.kind
                        for value in first.lowered.deployment_requirements.requirements
                    },
                    set(first.template.deployment_requirement_kinds),
                )
                self.assertEqual(
                    TemplateExpansionLock.from_payload(first.lock.to_payload()),
                    first.lock,
                )
                self.assertEqual(first.lock.schema, TEMPLATE_EXPANSION_LOCK_SCHEMA_ID)
                self.assertEqual(
                    ExperimentDraft.from_payload(first.draft.to_payload()),
                    first.draft,
                )
                lowered_again = lower_experiment_draft(first.draft)
                self.assertEqual(
                    lowered_again.protocol.to_payload(),
                    first.lowered.protocol.to_payload(),
                )
                self.assertEqual(
                    lowered_again.suite.to_payload(),
                    first.lowered.suite.to_payload(),
                )

    def test_every_synthetic_expansion_compiles_through_the_normal_compiler(self) -> None:
        registry = PluginRegistry()
        registry.register_builtins()
        register_phase5_plugins(registry)
        manifest = phase5_model_manifest()

        for fixture in self.fixtures.values():
            with self.subTest(fixture=fixture["fixture_id"]):
                expansion = _expansion(fixture)
                result = compile_suite(
                    expansion.lowered.protocol,
                    expansion.lowered.suite,
                    _deployment(
                        expansion.lowered.suite,
                        self.inventory["deployment_fixture"],
                    ),
                    registry,
                    model_manifests={manifest.manifest_digest: manifest},
                )
                self.assertTrue(result.valid)
                self.assertEqual(
                    result.plan.spec_hashes["suite"],
                    expansion.lowered.suite.spec_hash,
                )

    def test_parameter_provenance_distinguishes_explicit_from_template_default(self) -> None:
        fixture = self.fixtures["phase7.fixture.continuous_recording.v1"]
        source = SourceLocation(SourceKind.YAML, "experiment.yaml", line=8, column=5)
        expansion = expand_template(
            fixture["template_id"],
            fixture["version"],
            draft_id=fixture["draft_id"],
            parameters={"unit": "mV"},
            source_map=DraftSourceMap({"/template/parameters/unit": source}),
        )
        by_target = {
            (value.target_artifact, value.target_path): value
            for value in expansion.lowered.provenance.entries
        }

        explicit = by_target[(CanonicalArtifact.SUITE, "/streams/0/contract/unit")]
        default = by_target[
            (CanonicalArtifact.SUITE, "/streams/0/contract/channel_count")
        ]
        self.assertEqual(explicit.origin, AuthoringOrigin.USER_EXPLICIT)
        self.assertEqual(explicit.source, source)
        self.assertEqual(default.origin, AuthoringOrigin.TEMPLATE_DEFAULT)
        self.assertEqual(default.source.kind, SourceKind.TEMPLATE)
        self.assertEqual(default.template.parameter_path, "/parameters/channel_count")

    def test_exact_revision_and_manifest_are_required(self) -> None:
        fixture = self.fixtures["phase7.fixture.continuous_recording.v1"]
        expansion = _expansion(fixture)
        tampered_selection = replace(
            expansion.draft.template,
            manifest_digest="sha256:" + "f" * 64,
        )
        tampered_draft = replace(expansion.draft, template=tampered_selection)

        with self.assertRaises(DraftLoweringError) as raised:
            lower_experiment_draft(tampered_draft)
        self.assertEqual(
            raised.exception.issues[0].code,
            "authoring.template_manifest",
        )
        with self.assertRaises(KeyError):
            BUILTIN_TEMPLATE_CATALOG.resolve(fixture["template_id"], "latest")

    def test_explicit_new_version_cannot_change_a_locked_old_expansion(self) -> None:
        fixture = self.fixtures["phase7.fixture.continuous_recording.v1"]
        template = BUILTIN_TEMPLATE_CATALOG.resolve(
            fixture["template_id"], fixture["version"]
        )
        before = _expansion(fixture)
        new_defaults = {**template.parameter_defaults, "channel_count": 4}
        new_scientific = {**template.scientific_defaults, "channel_count": 4}
        revision = replace(
            template,
            version="1.1.0",
            parameter_defaults=new_defaults,
            scientific_defaults=new_scientific,
            synthetic_expansion_digest="sha256:" + "1" * 64,
        )
        catalog = BUILTIN_TEMPLATE_CATALOG.with_template(revision)
        after = expand_template(
            template.template_id,
            template.version,
            draft_id=fixture["draft_id"],
            parameters=fixture["parameters"],
            catalog=catalog,
        )
        upgraded = expand_template(
            revision.template_id,
            revision.version,
            draft_id=fixture["draft_id"],
            parameters=fixture["parameters"],
            catalog=catalog,
        )

        self.assertEqual(before.expansion_digest, after.expansion_digest)
        self.assertEqual(before.template.manifest_digest, after.template.manifest_digest)
        self.assertNotEqual(before.expansion_digest, upgraded.expansion_digest)
        changed_same_revision = replace(
            template,
            title="Changed in place",
        )
        with self.assertRaisesRegex(ValueError, "cannot be replaced"):
            BUILTIN_TEMPLATE_CATALOG.with_template(changed_same_revision)
        self.assertIsInstance(catalog, TemplateCatalog)

    def test_model_parameters_are_required_and_unknown_parameters_are_rejected(self) -> None:
        with self.assertRaises(SchemaValidationError):
            expand_template(
                "eegle.template.event_locked_model",
                "1.0.0",
                draft_id="fixture.invalid",
            )
        with self.assertRaises(SchemaValidationError):
            expand_template(
                "eegle.template.continuous_recording",
                "1.0.0",
                draft_id="fixture.invalid",
                parameters={"arbitrary_graph": []},
            )

    def test_portable_model_and_authorization_needs_are_typed_not_site_grants(self) -> None:
        fixture = self.fixtures["phase7.fixture.simulated_closed_loop.v1"]
        requirements = _expansion(fixture).lowered.deployment_requirements.to_payload()
        kinds = {value["kind"] for value in requirements["requirements"]}

        self.assertIn("model_artifact", kinds)
        self.assertIn("authorization", kinds)
        self.assertNotIn("uri", json.dumps(requirements))
        self.assertNotIn("authorization_ref", json.dumps(requirements))
        invalid = deepcopy(requirements)
        model = next(
            value
            for value in invalid["requirements"]
            if value["kind"] == "model_artifact"
        )
        model["manifest_digest"] = "not-a-digest"
        with self.assertRaises(SchemaValidationError):
            validate_deployment_requirements_payload(invalid)


if __name__ == "__main__":
    unittest.main()
