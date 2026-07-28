from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from eegle.operations import (
    FaultOutcome,
    PreflightReport,
    RehearsalReport,
    compile_project,
    create_project,
    inspect_session,
    open_project,
    preflight,
    preflight_project,
    rehearse_locked_plan,
    rehearse_project,
)
from eegle.authoring import expand_template
from eegle.compiler import compile_suite, write_lock, write_plan
from eegle.plugins import PluginRegistry
from eegle.specs import DeploymentSpec
from eegle.recording import EvidenceReader, Session
from tests.fixtures.phase5_model_components import (
    phase5_model_manifest,
    register_phase5_plugins,
)
from tests.test_phase7_templates import _deployment, _fixture_inventory


class Phase7PreflightRehearsalTests(unittest.TestCase):
    def test_project_preflight_and_rehearsal_are_hash_verified_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            create_project(root, project_id="phase7-preflight")
            compiled = compile_project(root)

            preflight = preflight_project(root)
            self.assertTrue(preflight.ready)
            self.assertEqual(
                PreflightReport.from_payload(preflight.to_payload()), preflight
            )
            capabilities = {value.capability for value in preflight.checks}
            self.assertTrue(
                {
                    "locked_plan",
                    "locked_plugin",
                    "locked_artifact",
                    "stream",
                    "channels",
                    "units",
                    "measured_rate",
                    "clock_mapping",
                    "storage",
                    "endpoint",
                    "authorization",
                    "model_compatibility",
                    "safe_state",
                    "operator_gate",
                }.issubset(capabilities)
            )

            run = rehearse_project(root, session_id="session.phase7.preflight")
            self.assertTrue(run.successful)
            project = open_project(root)
            report_payload = json.loads(
                project.path_for("rehearsal_report").read_text(encoding="utf-8")
            )
            report = RehearsalReport.from_payload(report_payload)
            self.assertEqual(report.simulation_plan_hash, compiled.plan.plan_hash)
            self.assertFalse(report.physical_action_authorized)
            self.assertEqual(len(report.scenarios), 8)
            self.assertEqual(
                {value.scenario_id for value in report.scenarios},
                {
                    "missing_markers",
                    "late_packets",
                    "clock_drift",
                    "low_quality",
                    "model_latency",
                    "queue_overflow",
                    "authorization_denial",
                    "actuator_rejection",
                },
            )

            session = Session.open(run.session_root, read_only=True)
            reader = EvidenceReader.open(session, session.bundle_paths[0])
            records = reader.records()
            fault_records = [
                value for value in records if value.record_type == "rehearsal_fault_outcome"
            ]
            self.assertEqual(len(fault_records), 8)
            self.assertTrue(reader.verify().valid)
            self.assertTrue(inspect_session(run.session_root).valid)

    def test_operator_gates_fail_closed_and_absent_authority_is_observe_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            create_project(root, project_id="phase7-gates")
            compiled = compile_project(root)
            gated_plan = replace(
                compiled.plan,
                phases=(replace(compiled.plan.phases[0], operator_confirmation=True),),
            )
            gated_lock = replace(compiled.lock, plan_hash=gated_plan.plan_hash)
            deployment = DeploymentSpec.load(root / "deployments" / "simulation.json")
            registry = PluginRegistry()
            registry.register_builtins()
            rejected = preflight(
                gated_plan,
                gated_lock,
                deployment,
                registry,
            )
            operator = next(
                value for value in rejected.checks if value.check_id == "operator.gates"
            )
            self.assertEqual(operator.status.value, "fail")
            accepted = preflight(
                gated_plan,
                gated_lock,
                deployment,
                registry,
                operator_confirmations=(
                    f"phase.{gated_plan.phases[0].phase_id}",
                ),
            )
            self.assertTrue(accepted.ready)
            # The report contract itself cannot be used to claim physical action or
            # reuse a live plan identity.
            report = RehearsalReport(
                "rehearsal.observe-only",
                compiled.plan.spec_hashes["protocol"],
                compiled.plan.spec_hashes["suite"],
                compiled.plan.plan_hash,
                compiled.lock.lock_hash,
                "sha256:" + "1" * 64,
                "session.observe-only",
                "bundle.observe-only",
                (),
            )
            self.assertFalse(report.physical_action_authorized)
            with self.assertRaisesRegex(ValueError, "physical action"):
                replace(report, physical_action_authorized=True)
            with self.assertRaisesRegex(ValueError, "impersonate"):
                replace(report, live_plan_hash=compiled.plan.plan_hash)

            # A plan without an authorization provider records observe-only as a
            # passing preflight fact, never as an inferred permission.
            original = preflight_project(root)
            authorization = next(
                value for value in original.checks if value.check_id == "authorization.absent"
            )
            self.assertEqual(authorization.status.value, "pass")
            self.assertIn("observe-only", authorization.summary)

    def test_closed_loop_reference_rehearses_through_the_normal_engine(self) -> None:
        inventory = _fixture_inventory()
        fixture = next(
            value
            for value in inventory["fixtures"]
            if value["fixture_id"] == "phase7.fixture.simulated_closed_loop.v1"
        )
        expansion = expand_template(
            fixture["template_id"],
            fixture["version"],
            draft_id=fixture["draft_id"],
            parameters=fixture["parameters"],
        )
        registry = PluginRegistry()
        registry.register_builtins()
        register_phase5_plugins(registry)
        manifest = phase5_model_manifest()
        deployment = _deployment(
            expansion.lowered.suite,
            inventory["deployment_fixture"],
        )
        compiled = compile_suite(
            expansion.lowered.protocol,
            expansion.lowered.suite,
            deployment,
            registry,
            model_manifests={manifest.manifest_digest: manifest},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = write_plan(root / "plan.json", compiled.plan)
            lock_path = write_lock(root / "lock.json", compiled.lock)
            rehearsal = rehearse_locked_plan(
                plan_path,
                lock_path,
                deployment,
                root / "session",
                session_id="session.closed-loop-reference",
                registry=registry,
            )

            self.assertTrue(rehearsal.run.successful)
            self.assertTrue(rehearsal.preflight.ready)
            outcomes = {
                value.scenario_id: value.outcome for value in rehearsal.report.scenarios
            }
            self.assertEqual(outcomes["authorization_denial"], FaultOutcome.CONTAINED)
            self.assertEqual(outcomes["actuator_rejection"], FaultOutcome.CONTAINED)
            session = Session.open(rehearsal.run.session_root, read_only=True)
            reader = EvidenceReader.open(session, session.bundle_paths[0])
            self.assertEqual(
                sum(
                    value.record_type == "rehearsal_fault_outcome"
                    for value in reader.records()
                ),
                8,
            )


if __name__ == "__main__":
    unittest.main()
