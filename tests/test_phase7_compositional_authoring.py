from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from eegle.actions import StructuredActionPolicy
from eegle.authoring import (
    DraftLoweringError,
    ExperimentDesign,
    ProcessingStep,
    SourceKind,
    load_yaml_design,
    lower_experiment_draft,
)
from eegle.plugins import PluginRegistry
from eegle.operations import ExplanationViewKind, explain_composed_experiment
from eegle.specs import DeploymentSpec
from eegle.streams import TimePoint
from tests.fixtures.phase5_model_components import (
    phase5_model_manifest,
    register_phase5_plugins,
)
from tests.test_phase7_templates import _deployment


def _comparison_design():
    primary = phase5_model_manifest()
    shadow = replace(
        primary,
        model_id="fixture.phase7.distinct-shadow",
        model_version="0.2.0",
    )
    design = (
        ExperimentDesign.create(
            "composed.comparison",
            "Compare independently declared primary and shadow models.",
        )
        .dense_signal(
            "neural",
            modality="eeg",
            channels=("C3", "C4"),
            unit="uV",
            rate_hz=250.0,
        )
        .event_stream("markers", kinds=("stimulus",))
        .processing_chain(
            "clean",
            input="signal.neural",
            steps=(
                ProcessingStep(
                    "identity",
                    "eegle.processing.identity",
                ),
            ),
        )
        .event_window(
            "prestimulus",
            input="processing.clean",
            event_stream="event.markers",
            event_kind="stimulus",
            start_seconds=-0.2,
            end_seconds=0.8,
            max_buffer_samples=2_000,
        )
        .quality_gate(
            "input-quality",
            input="processing.clean",
            config={"minimum_valid_fraction": 0.9},
        )
        .model(
            "primary",
            plugin_id=primary.implementations[0].plugin_id,
            manifest_digest=primary.manifest_digest,
            role="primary",
            inputs={"window": "window.prestimulus"},
            config={
                "model_id": "model.primary",
                "threshold": 0.0,
                "positive_label": "positive",
                "negative_label": "negative",
            },
        )
        .model(
            "shadow",
            plugin_id=shadow.implementations[0].plugin_id,
            manifest_digest=shadow.manifest_digest,
            role="shadow",
            inputs={"window": "window.prestimulus"},
            config={
                "model_id": "model.shadow",
                "threshold": 1.0,
                "positive_label": "positive",
                "negative_label": "negative",
            },
        )
        .comparison_group(
            "decoders",
            members=("model.primary", "model.shadow"),
        )
        .phase(
            "compare",
            active=(
                "signal.neural",
                "event.markers",
                "processing.clean",
                "window.prestimulus",
                "quality.input-quality",
                "model.primary",
                "model.shadow",
            ),
        )
        .record("signal.neural", "event.markers")
    )
    return design, primary, shadow


class Phase7CompositionalAuthoringTests(unittest.TestCase):
    def test_named_pipeline_lowers_without_positional_public_references(self) -> None:
        design, _, _ = _comparison_design()
        authored = design.build()

        neural = next(
            value for value in authored.suite.streams if value.stream_id == "stream.neural"
        )
        self.assertEqual(neural.contract.channel_ids, ("C3", "C4"))
        self.assertEqual(neural.contract.units, {"C3": "uV", "C4": "uV"})
        event_window = next(
            value
            for value in authored.suite.components
            if value.component_id == "window.prestimulus"
        )
        self.assertEqual(event_window.config["start_offset_seconds"], -0.2)
        self.assertEqual(event_window.config["end_offset_seconds"], 0.8)
        self.assertEqual(event_window.config["max_buffer_samples"], 2_000)
        self.assertEqual(
            {value.manifest_digest for value in authored.suite.model_uses},
            {value.manifest_digest for value in (_comparison_design()[1], _comparison_design()[2])},
        )
        self.assertNotIn("components", design.to_payload())
        self.assertNotIn("routes", design.to_payload())

    def test_design_and_draft_entrypoints_share_one_lowering_result(self) -> None:
        design, _, _ = _comparison_design()
        direct = design.build().lowered
        via_draft = lower_experiment_draft(design.to_draft())

        self.assertEqual(direct.protocol.spec_hash, via_draft.protocol.spec_hash)
        self.assertEqual(direct.suite.spec_hash, via_draft.suite.spec_hash)
        self.assertEqual(
            direct.deployment_requirements.requirements_hash,
            via_draft.deployment_requirements.requirements_hash,
        )

    def test_composed_explanation_uses_researcher_facing_views(self) -> None:
        authored = _comparison_design()[0].build()

        explanation = explain_composed_experiment(authored)

        dataflow = explanation.view(ExplanationViewKind.DATAFLOW)
        comparison = explanation.view(ExplanationViewKind.MODEL_COMPARISON)
        self.assertIn("stream", dataflow.summary)
        self.assertEqual(
            [value["component_id"] for value in comparison.details["groups"]["decoders"]],
            ["model.primary", "model.shadow"],
        )
        self.assertEqual(explanation.template["kind"], "composed_design")

    def test_declaration_order_does_not_change_canonical_specs(self) -> None:
        first = (
            ExperimentDesign.create("ordered", "Record two named streams.")
            .dense_signal(
                "zeta",
                modality="eeg",
                channels=("Cz",),
                unit="uV",
                rate_hz=100.0,
            )
            .dense_signal(
                "alpha",
                modality="eog",
                channels=("EOG",),
                unit="uV",
                rate_hz=100.0,
            )
            .record("signal.zeta", "signal.alpha")
        )
        second = (
            ExperimentDesign.create("ordered", "Record two named streams.")
            .dense_signal(
                "alpha",
                modality="eog",
                channels=("EOG",),
                unit="uV",
                rate_hz=100.0,
            )
            .dense_signal(
                "zeta",
                modality="eeg",
                channels=("Cz",),
                unit="uV",
                rate_hz=100.0,
            )
            .record("signal.zeta", "signal.alpha")
        )

        self.assertEqual(first.build().protocol.spec_hash, second.build().protocol.spec_hash)
        self.assertEqual(first.build().suite.spec_hash, second.build().suite.spec_hash)

    def test_missing_named_reference_fails_before_canonical_lowering(self) -> None:
        manifest = phase5_model_manifest()
        invalid = ExperimentDesign.create("invalid", "Reject a missing window.").model(
            "observer",
            plugin_id=manifest.implementations[0].plugin_id,
            manifest_digest=manifest.manifest_digest,
            role="observer",
            inputs={"window": "window.missing"},
        )

        with self.assertRaises(DraftLoweringError) as raised:
            invalid.build()
        self.assertEqual(raised.exception.issues[0].code, "authoring.missing_reference")

    def test_multi_phase_design_requires_an_explicit_entry_phase(self) -> None:
        design = (
            ExperimentDesign.create("phases", "Run two explicit phases.")
            .dense_signal(
                "eeg",
                modality="eeg",
                channels=("Cz",),
                unit="uV",
                rate_hz=100.0,
            )
            .phase("baseline", active=("signal.eeg",), next_phases=("control",))
            .phase("control", active=("signal.eeg",))
        )

        with self.assertRaises(DraftLoweringError):
            design.build()

        authored = design.with_initial_phase("baseline").build()
        self.assertEqual(authored.suite.initial_phase, "phase.baseline")

    def test_dense_and_event_stream_names_cannot_collide(self) -> None:
        design = ExperimentDesign.create("collision", "Reject ambiguous streams.").dense_signal(
            "shared",
            modality="eeg",
            channels=("Cz",),
            unit="uV",
            rate_hz=100.0,
        )

        with self.assertRaisesRegex(ValueError, "cannot share generated stream IDs"):
            design.event_stream("shared", kinds=("marker",))

    def test_two_distinct_manifests_compile_on_equivalent_input(self) -> None:
        design, primary, shadow = _comparison_design()
        authored = design.build()
        registry = PluginRegistry()
        registry.register_builtins()
        register_phase5_plugins(registry)

        compiled = authored.compile(
            _deployment(
                authored.suite,
                "phase5_event_window_actions/deployment.json",
            ),
            registry,
            model_manifests={
                primary.manifest_digest: primary,
                shadow.manifest_digest: shadow,
            },
        )

        self.assertEqual(len(compiled.plan.model_bindings), 2)
        self.assertEqual(
            {value.manifest_digest for value in compiled.plan.model_bindings},
            {primary.manifest_digest, shadow.manifest_digest},
        )
        self.assertEqual(
            {value.comparison_group for value in compiled.plan.model_bindings},
            {"decoders"},
        )

    def test_structured_policy_maps_vector_values_but_grants_no_authority(self) -> None:
        policy = StructuredActionPolicy(
            capability="simulated.robot.velocity_3d",
            output_parameters={
                "vx": "velocity.x",
                "vy": "velocity.y",
                "vz": "velocity.z",
            },
            expires_after_seconds=0.2,
        )
        prediction = SimpleNamespace(
            abstained=False,
            outputs={"velocity": {"x": 0.1, "y": -0.2, "z": 0.3}},
            available_time=TimePoint(1.0, "boundary.clock"),
            prediction_id="prediction.vector",
        )
        context = SimpleNamespace(
            current_time=TimePoint(1.1, "boundary.clock"),
            component_id="policy.robot",
            next_id=lambda prefix: f"{prefix}.1",
        )

        request = policy.decide(prediction, {}, context)

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.parameters, {"vx": 0.1, "vy": -0.2, "vz": 0.3})
        self.assertEqual(request.capability, "simulated.robot.velocity_3d")
        self.assertFalse(hasattr(request, "authorization"))

    def test_structured_closed_loop_compiles_observe_only_without_provider(self) -> None:
        manifest = phase5_model_manifest()
        capability = "simulated.robot.velocity_3d"
        design = (
            ExperimentDesign.create("vector.design", "Request a bounded vector action.")
            .dense_signal(
                "neural",
                modality="eeg",
                channels=("C3", "C4"),
                unit="uV",
                rate_hz=250.0,
            )
            .event_stream("markers", kinds=("stimulus",))
            .continuous_window(
                "rolling",
                input="signal.neural",
                duration_seconds=0.2,
                step_seconds=0.1,
            )
            .model(
                "primary",
                plugin_id=manifest.implementations[0].plugin_id,
                manifest_digest=manifest.manifest_digest,
                role="primary",
                inputs={"window": "window.rolling"},
            )
            .policy(
                "robot",
                plugin_id="eegle.actions.structured_action",
                inputs={"prediction": "model.primary.prediction"},
                config={
                    "capability": capability,
                    "output_parameters": {
                        "vx": "score",
                        "vy": "score",
                        "vz": "score",
                    },
                },
            )
            .action(
                "robot",
                capability=capability,
                policy="policy.robot",
                plugin_id="eegle.actions.simulated_actuator",
            )
            .phase(
                "run",
                active=(
                    "signal.neural",
                    "event.markers",
                    "window.rolling",
                    "model.primary",
                    "policy.robot",
                    "action.robot",
                ),
            )
        )
        authored = design.build()
        deployment_payload = _deployment(
            authored.suite,
            "phase5_event_window_actions/deployment.json",
        ).to_payload()
        deployment_payload["permissions"] = []
        deployment_payload["authorization_providers"] = []
        registry = PluginRegistry()
        registry.register_builtins()
        register_phase5_plugins(registry)

        compiled = authored.compile(
            DeploymentSpec.from_payload(deployment_payload),
            registry,
            model_manifests={manifest.manifest_digest: manifest},
        )

        self.assertEqual(compiled.plan.authorization_providers, ())
        self.assertEqual(compiled.plan.action_grants, ())

    def test_restricted_yaml_matches_python_when_optional_parser_is_present(self) -> None:
        design = (
            ExperimentDesign.create("yaml.design", "Record named EEG.")
            .dense_signal(
                "eeg",
                modality="eeg",
                channels=("Fz", "Cz", "Pz"),
                unit="uV",
                rate_hz=500.0,
            )
            .record("signal.eeg")
        )
        try:
            import ruamel.yaml  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("optional ruamel.yaml parser is not installed")

        from_yaml = load_yaml_design(
            json.dumps(design.to_payload()),
            locator="experiment.design.yaml",
        )
        from_python = design.build()

        self.assertEqual(from_yaml.protocol.spec_hash, from_python.protocol.spec_hash)
        self.assertEqual(from_yaml.suite.spec_hash, from_python.suite.spec_hash)
        yaml_sources = {
            value.source.kind for value in from_yaml.provenance.entries
        }
        self.assertIn(SourceKind.YAML, yaml_sources)


if __name__ == "__main__":
    unittest.main()
