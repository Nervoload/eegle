from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

from eegle.actions import StructuredActionPolicy
from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle.authoring import (
    AuthoringOrigin,
    ConfirmationState,
    ContractUpdate,
    DraftLoweringError,
    ExperimentDesign,
    ProcessingStep,
    SourceKind,
    load_yaml_design,
    lower_experiment_draft,
)
from eegle.compiler import CompilationError
from eegle.plugins import (
    ContractTransformSpec,
    PluginCapabilities,
    PluginDescriptor,
    PluginRegistry,
    PortSpec,
    StateBehavior,
)
from eegle.recording import EvidenceReader, Session, persist_engine_run
from eegle.replay import BundleReplayRunner
from eegle.operations import ExplanationViewKind, explain_composed_experiment
from eegle.runtime import EngineStatus, ExecutionEngine
from eegle.specs import DeploymentSpec
from eegle.streams import DenseSampleBatch, TimePoint
from tests.fixtures.phase5_model_components import (
    phase5_model_manifest,
    phase5_peak_model_manifest,
    register_phase5_plugins,
)
from tests.test_phase7_templates import _deployment


def _resample_descriptor(contract_transform: ContractTransformSpec) -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="fixture.processing.resample",
        version="0.1.0",
        kind=ComponentKind.TRANSFORM,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "target_rate_hz": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["target_rate_hz"],
            "additionalProperties": False,
        },
        input_ports=(PortSpec("samples", "eegle.dense_sample_batch.v1"),),
        output_ports=(PortSpec("samples", "eegle.dense_sample_batch.v1"),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset(ExecutionMode),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.NUMERIC,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=lambda config: object(),
        implementation="tests.test_phase7_compositional_authoring:resample",
        contract_transform=contract_transform,
    )


def _comparison_design():
    primary = phase5_model_manifest()
    shadow = phase5_peak_model_manifest()
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
                    output_contract=ContractUpdate(),
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
    def test_recording_is_active_only_in_compatible_requested_phases(self) -> None:
        design = (
            ExperimentDesign.create("recording.phases", "Record only the control phase.")
            .dense_signal(
                "eeg",
                modality="eeg",
                channels=("Cz",),
                unit="uV",
                rate_hz=100.0,
            )
            .phase(
                "baseline",
                goals=("signal.eeg",),
                next_phases=("control",),
                initial=True,
            )
            .phase("control", goals=("signal.eeg",))
            .record("signal.eeg", phases=("control",))
        )

        authored = design.build()
        phases = {value.phase_id: set(value.components) for value in authored.suite.phases}

        self.assertNotIn("sink.signal.eeg", phases["phase.baseline"])
        self.assertIn("sink.signal.eeg", phases["phase.control"])

    def test_phase_scoped_recording_executes_and_persists_requested_data(self) -> None:
        design = (
            ExperimentDesign.create("recording.execution", "Record control data.")
            .dense_signal(
                "baseline",
                modality="eeg",
                channels=("C3", "C4"),
                unit="uV",
                rate_hz=100.0,
            )
            .dense_signal(
                "control",
                modality="eeg",
                channels=("C3", "C4"),
                unit="uV",
                rate_hz=100.0,
            )
            .phase(
                "baseline",
                goals=("signal.baseline",),
                next_phases=("control",),
                initial=True,
            )
            .phase("control", goals=("signal.control",))
            .record("signal.control", phases=("control",))
        )
        authored = design.build()
        deployment = _deployment(
            authored.suite,
            "phase5_event_window_actions/deployment.json",
        ).to_payload()
        packet = DenseSampleBatch(
            batch_id="batch.control",
            stream_id="stream.control",
            stream_revision=1,
            sequence_start=0,
            channel_ids=("channel.c3", "channel.c4"),
            values=np.ones((4, 2), dtype=np.float64),
            received_time=TimePoint(0.039, "boundary.clock"),
            available_time=TimePoint(0.04, "boundary.clock"),
            first_sample_time=TimePoint(0.0, "device.clock"),
            sample_period_seconds=0.01,
        )
        for binding in deployment["component_bindings"]:
            binding["config"]["packets"] = (
                [packet.to_payload()]
                if binding["component_id"] == "source.control"
                else []
            )
        registry = PluginRegistry()
        registry.register_builtins()
        compiled = authored.compile(DeploymentSpec.from_payload(deployment), registry)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertEqual(
            tuple(value.phase_id for value in run.phase_results),
            ("phase.baseline", "phase.control"),
        )
        sink = engine.runtime.node("sink.signal.control").component
        self.assertEqual(tuple(value.batch_id for value in sink.records), ("batch.control",))

    def test_phase_goals_expand_the_upstream_dependency_closure(self) -> None:
        design, _, _ = _comparison_design()
        payload = design.to_payload()
        payload["phases"][0]["active"] = []
        payload["phases"][0]["goals"] = ["model.primary", "model.shadow"]
        payload["phases"][0]["include_dependencies"] = True

        authored = ExperimentDesign.from_payload(payload).build()
        components = set(authored.suite.phases[0].components)

        self.assertTrue(
            {
                "source.neural",
                "source.markers",
                "processing.clean.identity",
                "window.prestimulus",
                "model.primary",
                "model.shadow",
            }.issubset(components)
        )
        self.assertNotIn("quality.input-quality", components)
        self.assertIn("sink.signal.neural", components)
        self.assertIn("sink.event.markers", components)

    def test_processing_contract_update_drives_windows_and_is_compiler_verified(self) -> None:
        design = (
            ExperimentDesign.create("contract.update", "Downsample before windowing.")
            .dense_signal(
                "eeg",
                modality="eeg",
                channels=("Cz",),
                unit="uV",
                rate_hz=500.0,
            )
            .processing_chain(
                "downsampled",
                input="signal.eeg",
                steps=(
                    ProcessingStep(
                        "half",
                        "fixture.processing.resample",
                        config={"target_rate_hz": 250.0},
                        output_contract=ContractUpdate(nominal_rate_hz=250.0),
                    ),
                ),
            )
            .continuous_window(
                "one-second",
                input="processing.downsampled",
                duration_seconds=1.0,
                step_seconds=0.5,
            )
        )
        authored = design.build()
        processing = next(
            value
            for value in authored.suite.components
            if value.component_id == "processing.downsampled.half"
        )
        window = next(
            value
            for value in authored.suite.components
            if value.component_id == "window.one-second"
        )

        self.assertEqual(processing.input_contracts["samples"].nominal_rate_hz, 500.0)
        self.assertEqual(processing.output_contracts["samples"].nominal_rate_hz, 250.0)
        self.assertEqual(window.config["window_samples"], 250)
        self.assertEqual(window.config["step_samples"], 125)

        registry = PluginRegistry()
        registry.register_builtins()
        registry.register(
            _resample_descriptor(
                ContractTransformSpec(
                    input_port="samples",
                    output_port="samples",
                    config_projection={"nominal_rate_hz": "/target_rate_hz"},
                )
            )
        )
        authored.compile(
            _deployment(authored.suite, "phase5_event_window_actions/deployment.json"),
            registry,
        )

        unverified_registry = PluginRegistry()
        unverified_registry.register_builtins()
        unverified_registry.register(
            _resample_descriptor(ContractTransformSpec("samples", "samples"))
        )
        unverified = replace(
            unverified_registry.resolve("fixture.processing.resample", "~=0.1.0"),
            contract_transform=None,
        )
        unverified_registry.unregister("fixture.processing.resample")
        unverified_registry.register(unverified)
        with self.assertRaises(CompilationError) as unverified_error:
            authored.compile(
                _deployment(
                    authored.suite,
                    "phase5_event_window_actions/deployment.json",
                ),
                unverified_registry,
            )
        self.assertIn(
            "processing.contract_unverified",
            {value.code for value in unverified_error.exception.diagnostics},
        )

        mismatched_payload = design.to_payload()
        mismatched_payload["processing"][0]["steps"][0]["output_contract"][
            "nominal_rate_hz"
        ] = 200.0
        mismatched = ExperimentDesign.from_payload(mismatched_payload).build()
        with self.assertRaises(CompilationError) as raised:
            mismatched.compile(
                _deployment(
                    mismatched.suite,
                    "phase5_event_window_actions/deployment.json",
                ),
                registry,
            )
        self.assertIn(
            "processing.output_contract",
            {value.code for value in raised.exception.diagnostics},
        )

    def test_field_provenance_distinguishes_omitted_explicit_and_derived_values(self) -> None:
        omitted = (
            ExperimentDesign.create("origin.omitted", "Use bounded defaults.")
            .dense_signal(
                "eeg",
                modality="eeg",
                channels=("Cz",),
                unit="uV",
                rate_hz=100.0,
            )
            .build()
        )
        explicit = (
            ExperimentDesign.create("origin.explicit", "Use bounded defaults.")
            .dense_signal(
                "eeg",
                modality="eeg",
                channels=("Cz",),
                unit="uV",
                rate_hz=100.0,
                clock_id="device.clock",
                rate_model="regular",
                missing_data_policy="explicit_validity",
                layout="samples_by_channels",
            )
            .build()
        )

        omitted_entries = {
            value.target_path: value for value in omitted.provenance.entries
        }
        explicit_entries = {
            value.target_path: value for value in explicit.provenance.entries
        }
        for path in (
            "/streams/0/clock_id",
            "/streams/0/contract/rate_model",
            "/streams/0/contract/missing_data_policy",
            "/streams/0/contract/layout",
        ):
            self.assertEqual(
                omitted_entries[path].origin,
                AuthoringOrigin.AUTHORING_DEFAULT,
            )
            self.assertEqual(
                omitted_entries[path].confirmation,
                ConfirmationState.PENDING,
            )
            self.assertEqual(
                explicit_entries[path].origin,
                AuthoringOrigin.USER_EXPLICIT,
            )
            self.assertEqual(
                explicit_entries[path].confirmation,
                ConfirmationState.NOT_REQUIRED,
            )
        self.assertEqual(
            omitted_entries["/streams/0/contract/channel_count"].origin,
            AuthoringOrigin.AUTHORING_DERIVED,
        )

        comparison = _comparison_design()[0].build()
        component_entries = {
            value.target_path: value for value in comparison.provenance.entries
        }
        primary_index = next(
            index
            for index, component in enumerate(comparison.suite.components)
            if component.component_id == "model.primary"
        )
        prefix = f"/components/{primary_index}"
        self.assertEqual(
            component_entries[prefix + "/component_id"].origin,
            AuthoringOrigin.AUTHORING_DERIVED,
        )
        self.assertEqual(
            component_entries[prefix + "/plugin_id"].origin,
            AuthoringOrigin.USER_EXPLICIT,
        )
        self.assertEqual(
            component_entries[prefix + "/version_spec"].origin,
            AuthoringOrigin.AUTHORING_DEFAULT,
        )
        self.assertEqual(
            component_entries[prefix + "/version_spec"].confirmation,
            ConfirmationState.PENDING,
        )

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
        self.assertNotEqual(
            primary.implementations[0].plugin_id,
            shadow.implementations[0].plugin_id,
        )
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

    def test_two_different_model_implementations_execute_on_the_same_window(self) -> None:
        primary = phase5_model_manifest()
        shadow = phase5_peak_model_manifest()
        design = (
            ExperimentDesign.create("models.execution", "Compare mean and peak models.")
            .dense_signal(
                "neural",
                modality="eeg",
                channels=("C3", "C4"),
                unit="uV",
                rate_hz=100.0,
            )
            .continuous_window(
                "rolling",
                input="signal.neural",
                duration_seconds=0.04,
                step_seconds=0.04,
            )
            .model(
                "mean",
                plugin_id=primary.implementations[0].plugin_id,
                manifest_digest=primary.manifest_digest,
                role="primary",
                inputs={"window": "window.rolling"},
            )
            .model(
                "peak",
                plugin_id=shadow.implementations[0].plugin_id,
                manifest_digest=shadow.manifest_digest,
                role="shadow",
                inputs={"window": "window.rolling"},
            )
            .comparison_group("decoders", members=("model.mean", "model.peak"))
            .phase("compare", goals=("model.mean", "model.peak"), initial=True)
        )
        authored = design.build()
        deployment = _deployment(
            authored.suite,
            "phase5_event_window_actions/deployment.json",
        ).to_payload()
        packet = DenseSampleBatch(
            batch_id="batch.models",
            stream_id="stream.neural",
            stream_revision=1,
            sequence_start=0,
            channel_ids=("channel.c3", "channel.c4"),
            values=np.asarray(
                [[-0.8, 0.1], [0.2, 0.3], [0.4, 0.5], [0.6, 0.7]],
                dtype=np.float64,
            ),
            received_time=TimePoint(0.039, "boundary.clock"),
            available_time=TimePoint(0.04, "boundary.clock"),
            first_sample_time=TimePoint(0.0, "device.clock"),
            sample_period_seconds=0.01,
        )
        deployment["component_bindings"][0]["config"]["packets"] = [
            packet.to_payload()
        ]
        registry = PluginRegistry()
        registry.register_builtins()
        register_phase5_plugins(registry)
        compiled = authored.compile(
            DeploymentSpec.from_payload(deployment),
            registry,
            model_manifests={
                primary.manifest_digest: primary,
                shadow.manifest_digest: shadow,
            },
        )
        engine = ExecutionEngine.from_plan(compiled.plan, registry)

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        result = run.phase_results[0]
        mean = result.emissions_from("model.mean", "prediction")[0]
        peak = result.emissions_from("model.peak", "prediction")[0]
        self.assertNotEqual(mean.outputs["score"], peak.outputs["score"])
        locked = {
            value.component_id: value.plugin_id for value in compiled.plan.components
        }
        self.assertEqual(
            {locked["model.mean"], locked["model.peak"]},
            {
                primary.implementations[0].plugin_id,
                shadow.implementations[0].plugin_id,
            },
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

    def test_composed_structured_action_executes_records_and_replays_in_simulation(self) -> None:
        manifest = phase5_model_manifest()
        capability = "simulated.robot.velocity_3d"
        design = (
            ExperimentDesign.create("vector.execution", "Execute a simulated vector action.")
            .dense_signal(
                "neural",
                modality="eeg",
                channels=("C3", "C4"),
                unit="uV",
                rate_hz=100.0,
            )
            .continuous_window(
                "rolling",
                input="signal.neural",
                duration_seconds=0.04,
                step_seconds=0.04,
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
            .phase("run", goals=("action.robot",), initial=True)
        )
        authored = design.build()
        deployment = _deployment(
            authored.suite,
            "phase5_event_window_actions/deployment.json",
        ).to_payload()
        deployment["permissions"][0].update(
            capability=capability,
            parameter_constraints={
                axis: {"minimum": -2.0, "maximum": 2.0, "allowed_values": []}
                for axis in ("vx", "vy", "vz")
            },
            allow_unlisted_parameters=False,
        )
        packet = DenseSampleBatch(
            batch_id="batch.vector",
            stream_id="stream.neural",
            stream_revision=1,
            sequence_start=0,
            channel_ids=("channel.c3", "channel.c4"),
            values=np.asarray(
                [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6], [0.7, 0.8]],
                dtype=np.float64,
            ),
            received_time=TimePoint(0.039, "boundary.clock"),
            available_time=TimePoint(0.04, "boundary.clock"),
            first_sample_time=TimePoint(0.0, "device.clock"),
            sample_period_seconds=0.01,
        )
        deployment["component_bindings"][0]["config"]["packets"] = [
            packet.to_payload()
        ]
        registry = PluginRegistry()
        registry.register_builtins()
        register_phase5_plugins(registry)
        compiled = authored.compile(
            DeploymentSpec.from_payload(deployment),
            registry,
            model_manifests={manifest.manifest_digest: manifest},
        )
        engine = ExecutionEngine.from_plan(compiled.plan, registry)

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        result = run.phase_results[0]
        requests = result.emissions_from("policy.robot", "request")
        receipts = result.emissions_from("actuator.robot", "receipt")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].parameters, {"vx": 0.45, "vy": 0.45, "vz": 0.45})
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].status.value, "delivered")
        record_types = {value.record_type for value in run.evidence}
        self.assertTrue(
            {
                "authorization_decision",
                "authorized_command",
                "action_receipt",
                "action_disposition",
            }.issubset(record_types)
        )

        streams = (engine.runtime.node("source.neural").component.stream_spec,)
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(
                Path(directory) / "session",
                session_id="session.vector.execution",
            )
            bundle = persist_engine_run(
                session,
                run,
                plan=compiled.plan,
                streams=streams,
            )
            replay = BundleReplayRunner(registry).run(
                EvidenceReader.open(session, bundle.bundle_id)
            )

        self.assertEqual(replay.result.status, EngineStatus.COMPLETE)
        self.assertTrue(replay.equivalence.equivalent, replay.equivalence.divergences)
        replay_receipts = replay.result.phase_results[0].emissions_from(
            "actuator.robot",
            "receipt",
        )
        self.assertEqual(
            tuple(value.to_payload() for value in replay_receipts),
            tuple(value.to_payload() for value in receipts),
        )

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

    def test_yaml_provenance_keeps_field_locations_and_marks_omissions_as_defaults(self) -> None:
        payload = (
            ExperimentDesign.create("yaml.fields", "Track exact YAML fields.")
            .dense_signal(
                "eeg",
                modality="eeg",
                channels=("Cz",),
                unit="uV",
                rate_hz=500.0,
            )
            .to_payload()
        )
        for field in (
            "clock_id",
            "rate_model",
            "channel_units",
            "missing_data_policy",
            "layout",
        ):
            payload["signals"][0].pop(field)
        try:
            import ruamel.yaml  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("optional ruamel.yaml parser is not installed")
        text = json.dumps(payload, indent=2)
        rate_line = next(
            index
            for index, line in enumerate(text.splitlines(), start=1)
            if '"nominal_rate_hz"' in line
        )

        authored = load_yaml_design(text, locator="field-design.yaml")
        entries = {value.target_path: value for value in authored.provenance.entries}

        self.assertEqual(entries["/streams/0/contract/nominal_rate_hz"].source.line, rate_line)
        self.assertEqual(
            entries["/streams/0/clock_id"].origin,
            AuthoringOrigin.AUTHORING_DEFAULT,
        )
        self.assertEqual(entries["/streams/0/clock_id"].source.kind, SourceKind.GENERATED)


if __name__ == "__main__":
    unittest.main()
