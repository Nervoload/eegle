from __future__ import annotations

import json
from pathlib import Path
import unittest

from eegle.authoring import (
    AuthoringOrigin,
    AuthoringProvenance,
    CanonicalArtifact,
    DeploymentRequirementKind,
    DeploymentRequirements,
    DraftLoweringError,
    DraftSourceMap,
    ExperimentDraft,
    SourceKind,
    SourceLocation,
    UnresolvedChoice,
    UnresolvedKind,
    lower_experiment_draft,
)
from eegle.compiler import CompilationDiagnostic, compile_suite
from eegle.operations import (
    canonical_path_pointer,
    map_compilation_diagnostics,
    map_schema_validation_error,
)
from eegle.plugins import PluginRegistry
from eegle.runtime import ExecutionEngine
from eegle.specs import DeploymentSpec, SchemaValidationError


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "migration"
    / "phase5_event_window_actions"
    / "deployment.json"
)


def _intent(*, reverse: bool = False, explicit_recording: bool = False) -> dict:
    claims = [
        {
            "claim_id": "claim.capture",
            "statement": "Capture declared neurophysiological streams.",
        },
        {
            "claim_id": "claim.timing",
            "statement": "Preserve causal clock and packet timing evidence.",
        },
    ]
    signals = [
        {
            "signal_id": "eeg",
            "modality": "eeg",
            "content_kind": "dense_samples",
            "unit": "uV",
            "channel_count": 2,
            "channel_ids": ["channel.c3", "channel.c4"],
            "nominal_rate_hz": 100.0,
            "clock_id": "device.clock",
            "required_capabilities": ["deterministic"],
        },
        {
            "signal_id": "markers",
            "modality": "markers",
            "content_kind": "sparse_events",
            "clock_id": "marker.clock",
            "event_kinds": ["stimulus.target"],
        },
    ]
    if reverse:
        claims.reverse()
        signals.reverse()
    value = {
        "study": {
            "execution_mode": "causal",
            "execution_clock_id": "device.clock",
            "claims": claims,
        },
        "signals": signals,
    }
    if explicit_recording:
        value["recording"] = {"raw_recording": "reference"}
    return value


def _deployment() -> DeploymentSpec:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["deployment_id"] = "deployment.recording"
    payload["suite_id"] = "suite.recording"
    bindings = []
    for binding in payload["component_bindings"]:
        if binding["component_id"] == "source.neural":
            binding["component_id"] = "source.eeg"
            binding["resource_ids"] = ["resource.eeg"]
            binding["config"]["stream_spec"]["stream_id"] = "stream.eeg"
            bindings.append(binding)
        elif binding["component_id"] == "source.markers":
            binding["config"]["stream_spec"]["clock_id"] = "marker.clock"
            bindings.append(binding)
    payload["component_bindings"] = bindings
    resources = []
    for resource in payload["resources"]:
        if resource["resource_id"] == "resource.neural":
            resource["resource_id"] = "resource.eeg"
            resource["contract"].update(
                content_kind="dense_samples",
                rate_model="regular",
                channel_ids=["channel.c3", "channel.c4"],
            )
            resources.append(resource)
        elif resource["resource_id"] == "resource.markers":
            resource["contract"].update(
                content_kind="sparse_events",
                rate_model="event",
                event_kinds=["stimulus.target"],
            )
            resources.append(resource)
    payload["resources"] = resources
    payload["stream_bindings"] = [
        {
            "stream_id": "stream.eeg",
            "resource_id": "resource.eeg",
            "selector": {},
        },
        {
            "stream_id": "stream.markers",
            "resource_id": "resource.markers",
            "selector": {},
        },
    ]
    payload["storage"] = [
        {
            "storage_id": "storage.evidence",
            "kind": "evidence",
            "uri": "memory://phase7/recording",
        }
    ]
    payload["permissions"] = []
    payload["authorization_providers"] = []
    payload["secrets"] = []
    payload["clock_mappings"] = [
        {
            "source_clock": "marker.clock",
            "target_clock": "device.clock",
            "strategy": "declared_affine",
            "maximum_uncertainty_seconds": 0.001,
        }
    ]
    return DeploymentSpec.from_payload(payload)


class Phase7DraftLoweringTests(unittest.TestCase):
    def test_incomplete_draft_round_trips_but_unresolved_intent_cannot_lower(self) -> None:
        draft = ExperimentDraft(
            draft_id="recording",
            revision=3,
            intent={"study": {"execution_mode": "causal"}},
            unresolved=(
                UnresolvedChoice(
                    path="/intent/signals",
                    kind=UnresolvedKind.DETECTION,
                    prompt="Select at least one detected signal stream.",
                ),
            ),
        )

        self.assertEqual(ExperimentDraft.from_payload(draft.to_payload()), draft)
        self.assertFalse(draft.complete)
        with self.assertRaises(DraftLoweringError) as raised:
            lower_experiment_draft(draft)
        self.assertEqual(raised.exception.issues[0].path, "/intent/signals")
        self.assertEqual(
            raised.exception.issues[0].code,
            "authoring.unresolved_detection",
        )

    def test_equivalent_orderings_lower_to_identical_canonical_values(self) -> None:
        first = lower_experiment_draft(
            ExperimentDraft("recording", 1, _intent())
        )
        second = lower_experiment_draft(
            ExperimentDraft("recording", 1, _intent(reverse=True))
        )

        self.assertEqual(first.protocol.to_payload(), second.protocol.to_payload())
        self.assertEqual(first.suite.to_payload(), second.suite.to_payload())
        self.assertEqual(
            first.deployment_requirements.to_payload(),
            second.deployment_requirements.to_payload(),
        )
        self.assertEqual(first.protocol.spec_hash, second.protocol.spec_hash)
        self.assertEqual(first.suite.spec_hash, second.suite.spec_hash)

    def test_recording_lowering_is_non_model_and_emits_site_requirements(self) -> None:
        lowered = lower_experiment_draft(
            ExperimentDraft("recording", 1, _intent())
        )

        self.assertEqual(
            {value.kind.value for value in lowered.suite.components},
            {"source", "sink"},
        )
        self.assertEqual(len(lowered.suite.routes), 2)
        kinds = [
            value.kind for value in lowered.deployment_requirements.requirements
        ]
        self.assertEqual(kinds.count(DeploymentRequirementKind.SOURCE_BINDING), 2)
        self.assertEqual(kinds.count(DeploymentRequirementKind.CLOCK_MAPPING), 1)
        self.assertEqual(kinds.count(DeploymentRequirementKind.STORAGE), 1)
        self.assertEqual(
            DeploymentRequirements.from_payload(
                lowered.deployment_requirements.to_payload()
            ),
            lowered.deployment_requirements,
        )

    def test_resolved_requirements_leave_specs_for_the_existing_compiler(self) -> None:
        lowered = lower_experiment_draft(
            ExperimentDraft("recording", 1, _intent())
        )
        registry = PluginRegistry()
        registry.register_builtins()

        result = compile_suite(
            lowered.protocol,
            lowered.suite,
            _deployment(),
            registry,
        )

        self.assertTrue(result.valid)
        self.assertEqual(
            result.plan.spec_hashes["protocol"],
            lowered.protocol.spec_hash,
        )
        self.assertEqual(
            result.plan.spec_hashes["suite"],
            lowered.suite.spec_hash,
        )

    def test_provenance_distinguishes_explicit_values_from_authoring_defaults(self) -> None:
        execution_source = SourceLocation(
            SourceKind.YAML,
            "experiment.yaml",
            line=4,
            column=5,
        )
        recording_source = SourceLocation(
            SourceKind.YAML,
            "experiment.yaml",
            line=20,
            column=7,
        )
        lowered = lower_experiment_draft(
            ExperimentDraft("recording", 1, _intent(explicit_recording=True)),
            source_map=DraftSourceMap(
                {
                    "/intent/study/execution_mode": execution_source,
                    "/intent/recording/raw_recording": recording_source,
                }
            ),
        )

        explicit = lowered.provenance.entry_for(
            CanonicalArtifact.PROTOCOL,
            "/execution_mode",
        )
        self.assertIsNotNone(explicit)
        self.assertEqual(explicit.origin, AuthoringOrigin.USER_EXPLICIT)
        self.assertEqual(explicit.source, execution_source)
        raw = lowered.provenance.entry_for(
            CanonicalArtifact.SUITE,
            "/recording/raw_recording",
        )
        capture = lowered.provenance.entry_for(
            CanonicalArtifact.SUITE,
            "/recording/execution_capture",
        )
        self.assertEqual(raw.origin, AuthoringOrigin.USER_EXPLICIT)
        self.assertEqual(raw.source, recording_source)
        self.assertEqual(capture.origin, AuthoringOrigin.AUTHORING_DEFAULT)
        self.assertEqual(capture.source.kind, SourceKind.GENERATED)
        self.assertEqual(
            AuthoringProvenance.from_payload(lowered.provenance.to_payload()),
            lowered.provenance,
        )

    def test_source_sidecar_does_not_change_canonical_hashes(self) -> None:
        draft = ExperimentDraft("recording", 1, _intent())
        python = lower_experiment_draft(
            draft,
            source_map=DraftSourceMap(
                {
                    "/intent/signals/0/unit": SourceLocation(
                        SourceKind.PYTHON,
                        symbol="experiment.signals[0].unit",
                    )
                }
            ),
        )
        yaml = lower_experiment_draft(
            draft,
            source_map=DraftSourceMap(
                {
                    "/intent/signals/0/unit": SourceLocation(
                        SourceKind.YAML,
                        "experiment.yaml",
                        line=12,
                        column=11,
                    )
                }
            ),
        )

        self.assertEqual(python.protocol.spec_hash, yaml.protocol.spec_hash)
        self.assertEqual(python.suite.spec_hash, yaml.suite.spec_hash)
        self.assertNotEqual(
            python.provenance.to_payload(),
            yaml.provenance.to_payload(),
        )

    def test_compiler_and_schema_diagnostics_map_to_the_closest_source(self) -> None:
        source = SourceLocation(
            SourceKind.YAML,
            "experiment.yaml",
            line=12,
            column=11,
        )
        lowered = lower_experiment_draft(
            ExperimentDraft("recording", 1, _intent()),
            source_map=DraftSourceMap({"/intent/signals/0/unit": source}),
        )
        diagnostic = CompilationDiagnostic(
            code="contract.unit",
            path="$.suite.streams[0].contract.unit",
            message="source and stream units differ",
        )

        mapped = map_compilation_diagnostics((diagnostic,), lowered.provenance)[0]
        self.assertEqual(mapped.code, diagnostic.code)
        self.assertEqual(mapped.path, diagnostic.path)
        self.assertEqual(mapped.source, source)
        self.assertEqual(
            canonical_path_pointer(diagnostic.path),
            (CanonicalArtifact.SUITE, "/streams/0/contract/unit"),
        )

        schema_error = SchemaValidationError(
            "'uV' was expected",
            path="$.streams[0].contract.unit",
        )
        schema_mapped = map_schema_validation_error(
            schema_error,
            CanonicalArtifact.SUITE,
            lowered.provenance,
        )
        self.assertEqual(schema_mapped.source, source)
        self.assertEqual(
            schema_mapped.details["canonical_path"],
            "/streams/0/contract/unit",
        )

    def test_runtime_boundary_rejects_a_draft_instead_of_interpreting_it(self) -> None:
        draft = ExperimentDraft("recording", 1, _intent())

        with self.assertRaisesRegex(TypeError, "ExecutionPlan"):
            ExecutionEngine.from_plan(draft, PluginRegistry())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
