from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from eegle._domain import ExecutionMode
from eegle.authoring import SourceKind, SourceLocation
from eegle.models import (
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
)
from eegle.operations import (
    AuthorizationProviderCapability,
    ClockCapability,
    DeploymentProposal,
    DeploymentSelection,
    DetectionReport,
    ExitCode,
    OperationError,
    SourceCapability,
    StorageCapability,
    compile_project,
    create_project,
    create_simulation_deployment,
    detect_capabilities,
    open_project,
    propose_deployment,
    propose_project_deployment,
    read_project_authoring,
    read_model_observations,
    record_detection_report,
    signal_contract_from_stream,
)
from eegle.operations.cli import COMMAND_RESULT_SCHEMA_ID, main as cli_main
from eegle.plugins import PluginRegistry
from eegle.specs import SecretReference, StorageBinding
from eegle.streams import StreamSpec


CAPTURED_AT = "2026-07-28T12:00:00Z"


class Phase7DiscoveryDeploymentTests(unittest.TestCase):
    def test_detection_is_canonical_inspectable_and_hash_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, base_report = _live_report(root)
            manifest_path = root / "model-manifest.json"
            manifest_path.write_text(
                json.dumps(_model_manifest().to_payload()), encoding="utf-8"
            )
            report = detect_capabilities(
                include_entry_points=False,
                models=read_model_observations((manifest_path,)),
                sources=base_report.sources,
                storage=base_report.storage,
                clocks=base_report.clocks,
                authorization_providers=base_report.authorization_providers,
                detection_id=base_report.detection_id,
                observed_at=base_report.observed_at,
            )

            restored = DetectionReport.from_payload(report.to_payload())
            reordered = detect_capabilities(
                include_entry_points=False,
                models=reversed(report.models),
                sources=reversed(report.sources),
                storage=reversed(report.storage),
                clocks=reversed(report.clocks),
                authorization_providers=reversed(report.authorization_providers),
                detection_id=report.detection_id,
                observed_at=report.observed_at,
            )

            self.assertEqual(restored, report)
            self.assertEqual(reordered.report_hash, report.report_hash)
            self.assertEqual(
                tuple(value.plugin_id for value in report.plugins),
                tuple(sorted(value.plugin_id for value in report.plugins)),
            )
            self.assertEqual(report.models[0].manifest.model_id, "model.detected")
            tampered = report.to_payload()
            tampered["sources"][0]["selector"]["serial"] = "changed"
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                DetectionReport.from_payload(tampered)

    def test_same_portable_suite_compiles_against_simulation_and_detected_site(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            authored, simulation, report = _live_report(Path(directory))
            suite_before = authored.suite.to_payload()
            proposal = propose_deployment(authored, report)
            registry = PluginRegistry()
            registry.register_builtins()

            simulated = authored.compile(simulation, registry)
            detected = authored.compile(proposal.deployment, registry)

            self.assertEqual(authored.suite.to_payload(), suite_before)
            self.assertEqual(proposal.suite_hash, simulated.plan.spec_hashes["suite"])
            self.assertEqual(proposal.suite_hash, detected.plan.spec_hashes["suite"])
            self.assertNotEqual(simulated.plan.plan_hash, detected.plan.plan_hash)
            self.assertFalse(proposal.authorization_inferred)
            self.assertEqual(proposal.deployment.permissions, ())
            self.assertEqual(proposal.deployment.authorization_providers, ())
            self.assertEqual(
                DeploymentProposal.from_payload(proposal.to_payload()), proposal
            )

    def test_ambiguous_sources_fail_with_candidates_until_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            authored, _, report = _live_report(Path(directory))
            first = report.sources[0]
            second = SourceCapability(
                capability_id="capability.source.amplifier-b",
                resource_id="resource.amplifier-b",
                resource_kind=first.resource_kind,
                stream=first.stream,
                contract=first.contract,
                plugin_id=first.plugin_id,
                plugin_version=first.plugin_version,
                selector={"serial": "AMP-B"},
                capabilities=first.capabilities,
                config=first.config,
                source=_source("detector.amplifier-b"),
            )
            ambiguous = detect_capabilities(
                include_entry_points=False,
                sources=(first, second),
                storage=report.storage,
                clocks=report.clocks,
                detection_id="detection.ambiguous",
                observed_at=CAPTURED_AT,
            )

            with self.assertRaises(OperationError) as caught:
                propose_deployment(authored, ambiguous)

            error = caught.exception
            self.assertEqual(error.exit_code, ExitCode.REJECTED)
            diagnostic = error.diagnostics[0]
            self.assertEqual(diagnostic.code, "deployment.source_ambiguous")
            self.assertEqual(
                tuple(diagnostic.details["candidates"]),
                (
                    "capability.source.amplifier-a",
                    "capability.source.amplifier-b",
                ),
            )
            component_id = next(
                value.component_id
                for value in authored.suite.components
                if value.kind.value == "source"
            )
            proposal = propose_deployment(
                authored,
                ambiguous,
                selection=DeploymentSelection(
                    source_capabilities={
                        component_id: "capability.source.amplifier-b"
                    }
                ),
            )
            self.assertEqual(
                proposal.deployment.stream_bindings[0].resource_id,
                "resource.amplifier-b",
            )
            source_decision = next(
                value
                for value in proposal.decisions
                if value.target == f"$.component_bindings.{component_id}"
            )
            self.assertTrue(source_decision.explicit_selection)

    def test_incompatible_channel_unit_or_rate_is_not_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            authored, _, report = _live_report(Path(directory))
            source = report.sources[0]
            stream_payload = source.stream.to_payload()
            stream_payload["channels"] = [
                {**value, "unit": "mV"} for value in stream_payload["channels"]
            ]
            incompatible_stream = StreamSpec.from_payload(stream_payload)
            incompatible_contract = signal_contract_from_stream(incompatible_stream)
            incompatible = SourceCapability(
                capability_id=source.capability_id,
                resource_id=source.resource_id,
                resource_kind=source.resource_kind,
                stream=incompatible_stream,
                contract=incompatible_contract,
                plugin_id=source.plugin_id,
                plugin_version=source.plugin_version,
                selector=source.selector,
                capabilities=source.capabilities,
                config=source.config,
                source=source.source,
            )
            changed = detect_capabilities(
                include_entry_points=False,
                sources=(incompatible,),
                storage=report.storage,
                clocks=report.clocks,
                detection_id="detection.incompatible",
                observed_at=CAPTURED_AT,
            )

            with self.assertRaises(OperationError) as caught:
                propose_deployment(authored, changed)

            self.assertEqual(caught.exception.exit_code, ExitCode.UNAVAILABLE)
            self.assertEqual(
                caught.exception.diagnostics[0].code,
                "deployment.source_unavailable",
            )

    def test_secrets_are_references_and_provider_presence_is_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            authored, _, report = _live_report(Path(directory), secret_alias="access")
            component_id = next(
                value.component_id
                for value in authored.suite.components
                if value.kind.value == "source"
            )
            selection = DeploymentSelection(
                secrets=(SecretReference("secret.amplifier", "keychain", "EEGLE_AMP"),),
                component_secret_refs={
                    component_id: {"access": "secret.amplifier"}
                },
            )

            proposal = propose_deployment(authored, report, selection=selection)

            source_binding = next(
                value
                for value in proposal.deployment.component_bindings
                if value.component_id == component_id
            )
            self.assertEqual(source_binding.secret_refs["access"], "secret.amplifier")
            self.assertNotIn("EEGLE_AMP", json.dumps(source_binding.to_payload()))
            self.assertEqual(proposal.deployment.permissions, ())
            self.assertEqual(proposal.deployment.authorization_providers, ())
            with self.assertRaisesRegex(ValueError, "use SecretReference"):
                SourceCapability(
                    capability_id="capability.invalid-secret",
                    resource_id="resource.invalid-secret",
                    resource_kind="eeg_amplifier",
                    stream=report.sources[0].stream,
                    contract=report.sources[0].contract,
                    plugin_id=report.sources[0].plugin_id,
                    plugin_version=report.sources[0].plugin_version,
                    config={"token": "literal-value"},
                    source=_source("detector.invalid"),
                )

    def test_project_keeps_proposal_separate_and_compilation_choice_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            create_project(root, project_id="deployment-project")
            _, _, report = _live_report(
                Path(directory), existing_project=root
            )
            simulation_before = open_project(root).manifest.artifact(
                "simulation_deployment"
            )

            result = propose_project_deployment(root, report)
            newer_report = detect_capabilities(
                include_entry_points=False,
                detection_id="detection.site-b",
                observed_at="2026-07-28T12:01:00Z",
            )
            record_detection_report(root, newer_report)
            proposal_compilation = compile_project(
                root, deployment_role="deployment_proposal"
            )
            simulation_compilation = compile_project(root)
            opened = open_project(root)

            self.assertTrue(
                opened.manifest.artifact("detection_report").immutable
            )
            self.assertEqual(
                DetectionReport.load(opened.path_for("detection_report")).report_hash,
                newer_report.report_hash,
            )
            self.assertTrue(
                opened.manifest.artifact("deployment_proposal").immutable
            )
            self.assertEqual(
                opened.manifest.artifact("simulation_deployment").digest,
                simulation_before.digest,
            )
            self.assertEqual(
                opened.manifest.artifact("deployment_proposal").digest,
                result.proposal.deployment.spec_hash,
            )
            self.assertEqual(
                proposal_compilation.deployment_role, "deployment_proposal"
            )
            self.assertEqual(
                simulation_compilation.deployment_role, "simulation_deployment"
            )
            self.assertNotEqual(
                proposal_compilation.plan.plan_hash,
                simulation_compilation.plan.plan_hash,
            )

    def test_detect_command_runs_without_a_project_or_optional_transport(self) -> None:
        code, output, errors = _invoke_cli(
            [
                "--json",
                "detect",
                "--no-entry-points",
                "--id",
                "detection.cli",
                "--observed-at",
                CAPTURED_AT,
            ]
        )
        payload = json.loads(output)

        self.assertEqual(code, ExitCode.SUCCESS, errors)
        self.assertEqual(payload["schema"], COMMAND_RESULT_SCHEMA_ID)
        self.assertEqual(payload["operation"], "detect")
        self.assertEqual(payload["result"]["schema"], "eegle.detection_report.v1")
        self.assertGreater(len(payload["result"]["plugins"]), 0)
        self.assertEqual(payload["result"]["sources"], [])


def _live_report(
    directory: Path,
    *,
    secret_alias: str | None = None,
    existing_project: Path | None = None,
):
    root = existing_project or directory / "source-project"
    if existing_project is None:
        project = create_project(root, project_id="discovery-fixture")
    else:
        project = open_project(root)
    authored = read_project_authoring(project)
    simulation = create_simulation_deployment(authored)
    source_binding = next(
        value
        for value in simulation.component_bindings
        if value.component_id.startswith("source.")
    )
    stream = StreamSpec.from_payload(source_binding.config["stream_spec"])
    resource = simulation.resources[0]
    source = SourceCapability(
        capability_id="capability.source.amplifier-a",
        resource_id="resource.amplifier-a",
        resource_kind="eeg_amplifier",
        stream=stream,
        contract=resource.contract,
        plugin_id=source_binding.plugin_id,
        plugin_version="0.1.0",
        selector={"vendor": "example", "serial": "AMP-A"},
        capabilities=("channels_known", "rate_known", "units_known"),
        config=source_binding.config,
        required_secret_aliases=() if secret_alias is None else (secret_alias,),
        source=_source("detector.amplifier-a"),
    )
    storage = StorageCapability(
        "capability.storage.local",
        StorageBinding(
            "storage.site-evidence",
            "evidence",
            (directory / "site-evidence").resolve().as_uri(),
        ),
        ("atomic_publish",),
        _source("detector.storage"),
    )
    clocks = tuple(
        ClockCapability(
            f"capability.clock.{index}",
            value,
            _source("detector.clock"),
        )
        for index, value in enumerate(simulation.clock_mappings, 1)
    )
    authorization = AuthorizationProviderCapability(
        capability_id="capability.authorization.simulation",
        provider_id="authorization.site-observer",
        plugin_id="eegle.authorization.simulation",
        plugin_version="0.1.0",
        capabilities=("device.stimulation",),
        source=_source("detector.authorization"),
    )
    report = detect_capabilities(
        include_entry_points=False,
        sources=(source,),
        storage=(storage,),
        clocks=clocks,
        authorization_providers=(authorization,),
        detection_id="detection.site-a",
        observed_at=CAPTURED_AT,
    )
    return authored, simulation, report


def _source(locator: str) -> SourceLocation:
    return SourceLocation(SourceKind.DETECTION, locator=locator)


def _model_manifest() -> ModelManifest:
    return ModelManifest(
        model_id="model.detected",
        model_version="1.0.0",
        contract=ModelContract(
            inputs=(
                ModelInputContract(
                    port_name="input",
                    type_id="eegle.dense_window.v1",
                ),
            ),
            outputs=(
                ModelOutputContract(
                    port_name="result",
                    type_id="eegle.model_result.v1",
                    value_schema={"type": "number"},
                ),
            ),
            supported_modes=frozenset({ExecutionMode.CAUSAL}),
        ),
        artifacts=(),
        implementations=(
            ModelImplementationRequirement(
                plugin_id="example.model.detected",
                version_spec="==1.0.0",
            ),
        ),
    )


def _invoke_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
