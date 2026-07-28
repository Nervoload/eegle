from __future__ import annotations

import unittest

from eegle.authoring import (
    AuthoredExperiment,
    DraftLoweringError,
    ExperimentBuilder,
    SourceKind,
    SourceLocation,
)
from eegle.compiler import CompilationDiagnostic, CompilationError
from eegle.operations import (
    EXPERIMENT_DIFF_SCHEMA_ID,
    EXPERIMENT_EXPLANATION_SCHEMA_ID,
    DifferenceImpact,
    ExitCode,
    ExplanationViewKind,
    diagnose_authoring_failure,
    diagnose_compilation_failure,
    diff_authored_experiments,
    explain_authored_experiment,
    guide_compilation_diagnostics,
    validate_operation_error_payload,
)
from eegle.plugins import PluginRegistry
from eegle.specs import DeploymentSpec
from tests.fixtures.phase5_model_components import (
    phase5_model_manifest,
    register_phase5_plugins,
)
from tests.test_phase7_templates import _deployment, _expansion, _fixture_inventory


def _authored_fixture(profile: str) -> AuthoredExperiment:
    fixture = next(
        value
        for value in _fixture_inventory()["fixtures"]
        if value["template_id"] == f"eegle.template.{profile}"
    )
    return AuthoredExperiment(_expansion(fixture))


def _registry() -> PluginRegistry:
    value = PluginRegistry()
    value.register_builtins()
    register_phase5_plugins(value)
    return value


def _compile(authored: AuthoredExperiment, deployment: DeploymentSpec | None = None):
    manifest = phase5_model_manifest()
    return authored.compile(
        deployment
        or _deployment(
            authored.suite,
            "phase5_event_window_actions/deployment.json",
        ),
        _registry(),
        model_manifests={manifest.manifest_digest: manifest},
    )


class Phase7ExplanationTests(unittest.TestCase):
    def test_every_reference_template_has_all_readable_explanation_views(self) -> None:
        for fixture in _fixture_inventory()["fixtures"]:
            with self.subTest(template=fixture["template_id"]):
                authored = AuthoredExperiment(_expansion(fixture))
                explanation = explain_authored_experiment(authored)
                payload = explanation.to_payload()

                self.assertEqual(payload["schema"], EXPERIMENT_EXPLANATION_SCHEMA_ID)
                self.assertEqual(
                    {value.kind for value in explanation.views},
                    set(ExplanationViewKind),
                )
                self.assertTrue(all(value.summary.strip() for value in explanation.views))
                self.assertIsNone(explanation.compiler_projection)
                self.assertEqual(
                    explanation.canonical_hashes["protocol"],
                    authored.protocol.spec_hash,
                )

    def test_locked_plan_projection_joins_without_misattributing_compiler_values(self) -> None:
        authored = _authored_fixture("primary_shadow_comparison")
        compiled = _compile(authored)
        explanation = explain_authored_experiment(authored, plan=compiled.plan)

        self.assertEqual(
            explanation.compiler_projection["schema"],
            "eegle.plan_explanation.v1",
        )
        comparison = explanation.view(ExplanationViewKind.MODEL_COMPARISON)
        self.assertTrue(comparison.details["groups"])
        provenance = explanation.view(ExplanationViewKind.DEFAULTS_PROVENANCE)
        self.assertFalse(provenance.details["compiler_values_are_authoring_origins"])
        self.assertTrue(
            all(
                value["origin"] == "compiler_derived" and value["source"] is None
                for value in provenance.details["compiler_derived"]
            )
        )

        other = _authored_fixture("continuous_recording")
        with self.assertRaisesRegex(ValueError, "do not match"):
            explain_authored_experiment(other, plan=compiled.plan)

    def test_action_view_is_observe_only_until_a_matching_plan_grant_exists(self) -> None:
        authored = _authored_fixture("simulated_closed_loop")
        portable = explain_authored_experiment(authored).view(
            ExplanationViewKind.ACTION_INFLUENCE
        )
        self.assertTrue(portable.details["observe_only"])
        self.assertTrue(portable.details["authorization_requirements"])

        compiled = _compile(authored)
        locked = explain_authored_experiment(authored, plan=compiled.plan).view(
            ExplanationViewKind.ACTION_INFLUENCE
        )
        self.assertFalse(locked.details["observe_only"])
        self.assertTrue(locked.details["action_grants"])

    def test_diff_classifies_presentational_scientific_operational_and_replay_impacts(self) -> None:
        python_source = SourceLocation(SourceKind.PYTHON, symbol="builder.unit")
        yaml_source = SourceLocation(
            SourceKind.YAML,
            "experiment.yaml",
            line=8,
            column=5,
        )
        base = ExperimentBuilder.continuous_recording("diff.same")
        python = base.with_parameter("unit", "uV", source=python_source).build()
        yaml = base.with_parameter("unit", "uV", source=yaml_source).build()

        presentational = diff_authored_experiments(python, yaml)
        self.assertEqual(presentational.schema, EXPERIMENT_DIFF_SCHEMA_ID)
        self.assertTrue(presentational.scientifically_equivalent)
        self.assertTrue(presentational.replay_equivalent)
        self.assertEqual(len(presentational.changes), 1)
        self.assertEqual(
            presentational.changes[0].impacts,
            (DifferenceImpact.PRESENTATIONAL,),
        )

        changed = base.signal(channel_count=4).build()
        scientific = diff_authored_experiments(python, changed)
        self.assertFalse(scientific.scientifically_equivalent)
        self.assertFalse(scientific.replay_equivalent)
        self.assertTrue(
            any(
                DifferenceImpact.SCIENTIFIC in value.impacts
                and DifferenceImpact.REPLAY_AFFECTING in value.impacts
                for value in scientific.changes
            )
        )

        deployment = _deployment(
            python.suite,
            "phase5_event_window_actions/deployment.json",
        )
        alternative_payload = deployment.to_payload()
        alternative_payload["deployment_id"] = "deployment.diff-alternative"
        before_plan = _compile(python, deployment).plan
        after_plan = _compile(
            python,
            DeploymentSpec.from_payload(alternative_payload),
        ).plan
        operational = diff_authored_experiments(
            python,
            python,
            before_plan=before_plan,
            after_plan=after_plan,
        )
        self.assertTrue(operational.scientifically_equivalent)
        self.assertFalse(operational.replay_equivalent)
        self.assertTrue(
            any(
                DifferenceImpact.OPERATIONAL in value.impacts
                and DifferenceImpact.REPLAY_AFFECTING in value.impacts
                for value in operational.changes
            )
        )

    def test_common_compiler_failures_receive_source_aware_non_mutating_guidance(self) -> None:
        authored = _authored_fixture("simulated_closed_loop")
        before = authored.to_payload()
        stream_id = authored.suite.streams[0].stream_id
        diagnostics = (
            CompilationDiagnostic(
                "stream.capability",
                "$.deployment.stream_bindings[0].resource_id",
                "resource unit V does not match required unit uV",
                details={"stream_id": stream_id},
            ),
            CompilationDiagnostic(
                "stream.capability",
                "$.deployment.stream_bindings[0].resource_id",
                "sample rate 50.0 is below required sample rate 100.0",
                details={"stream_id": stream_id},
            ),
            CompilationDiagnostic(
                "stream.capability",
                "$.deployment.stream_bindings[0].resource_id",
                "channel count 1 is below required channel count 2",
                details={"stream_id": stream_id},
            ),
            CompilationDiagnostic(
                "clock.mapping_missing",
                "$.deployment.clock_mappings",
                "no mapping reaches the execution clock",
                details={"stream_id": stream_id},
            ),
            CompilationDiagnostic(
                "plugin.resolve",
                "$.suite.components[0].plugin_id",
                "selected plugin is unavailable",
            ),
            CompilationDiagnostic(
                "model.manifest_missing",
                "$.suite.model_uses[0].manifest_digest",
                "model manifest is unavailable",
            ),
            CompilationDiagnostic(
                "authorization.provider_reference",
                "$.deployment.permissions[0].authorization_ref",
                "authorization provider is unresolved",
            ),
        )

        guided = guide_compilation_diagnostics(diagnostics, authored)
        self.assertEqual(len(guided), len(diagnostics))
        self.assertEqual(
            {value.details["guidance_family"] for value in guided},
            {
                "signal.unit",
                "signal.rate",
                "signal.channels",
                "clock",
                "plugin",
                "model",
                "authorization",
            },
        )
        for value in guided:
            with self.subTest(code=value.code, message=value.message):
                self.assertIsNotNone(value.source)
                self.assertTrue(value.likely_cause)
                self.assertTrue(value.repairs)
                self.assertFalse(value.repairs[0].proposal["applied"])
                self.assertTrue(value.documentation)
        self.assertEqual(authored.to_payload(), before)

    def test_authoring_and_compilation_failures_use_shared_operation_envelope(self) -> None:
        invalid = ExperimentBuilder.continuous_recording("invalid").signal(
            channel_count=0
        )
        with self.assertRaises(DraftLoweringError) as authoring_error:
            invalid.build()
        authored_failure = diagnose_authoring_failure(authoring_error.exception)
        self.assertEqual(authored_failure.exit_code, ExitCode.INVALID_INPUT)
        self.assertIsNotNone(authored_failure.diagnostics[0].source)
        validate_operation_error_payload(authored_failure.to_payload())

        authored = _authored_fixture("eeg_events_recording")
        deployment = _deployment(
            authored.suite,
            "phase5_event_window_actions/deployment.json",
        ).to_payload()
        deployment["clock_mappings"] = []
        with self.assertRaises(CompilationError) as compilation_error:
            _compile(authored, DeploymentSpec.from_payload(deployment))
        compiled_failure = diagnose_compilation_failure(
            compilation_error.exception,
            authored,
        )
        self.assertEqual(compiled_failure.exit_code, ExitCode.REJECTED)
        clock = next(
            value
            for value in compiled_failure.diagnostics
            if value.code == "clock.mapping_missing"
        )
        self.assertIsNotNone(clock.source)
        self.assertEqual(clock.details["guidance_family"], "clock")
        validate_operation_error_payload(compiled_failure.to_payload())


if __name__ == "__main__":
    unittest.main()
