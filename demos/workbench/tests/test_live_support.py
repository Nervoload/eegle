from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

from demos.workbench.operations import SiteOverlay, WorkbenchOperations
from demos.workbench.profile import load_study1_profile
from demos.workbench.project import bootstrap_study1_project
from demos.workbench.tasks.study1_dsart import (
    _practice_metrics,
    _write_behavior,
    build_task_plan,
    marker_label,
)
from eegle.integrations.lsl import LSL_SPARSE_SOURCE_PLUGIN_ID, LslSource
from eegle.streams import SparseEventBatch
from tests.test_phase7_lsl_integration import _Info, _Pylsl


def _study1_network() -> tuple[_Pylsl, tuple[str, ...]]:
    labels = tuple(f"N{index:02d}" for index in range(1, 65)) + ("TRIGGER_STATUS",)
    channels = tuple({"label": value, "unit": "uV"} for value in labels)
    eeg = _Info(
        "NeuracleEEG",
        "EEG",
        65,
        1000.0,
        "float32",
        "neuracle-01",
        "uid-neuracle",
        chunks=(),
        channels=channels,
    )
    markers = _Info(
        "EEGleMarkers",
        "Markers",
        1,
        0.0,
        "string",
        "eegle-task-01",
        "uid-task",
        chunks=(([["dynamic_sart_stimulus_onset__trial=1"]], [2.0]),),
    )
    return _Pylsl((eeg, markers)), labels


class Study1TaskPlanTests(unittest.TestCase):
    def test_demo_plan_is_deterministic_short_and_spaced(self) -> None:
        profile = load_study1_profile()
        first = build_task_plan(profile, participant_pseudonym="DEMO-001")
        second = build_task_plan(profile, participant_pseudonym="DEMO-001")

        self.assertEqual(first.plan_hash, second.plan_hash)
        self.assertEqual(first.scientific_status, "demonstration_only")
        self.assertEqual((len(first.practice_trials), first.total_trials), (10, 60))
        self.assertEqual(
            (
                first.stimulus_seconds,
                first.onset_interval_seconds,
                first.minimum_valid_rt_seconds,
                first.practice_feedback_seconds,
            ),
            (0.25, 1.6, 0.1, 0.35),
        )
        self.assertEqual(
            (
                first.practice_go_accuracy,
                first.practice_no_go_accuracy,
                first.practice_max_anticipatory_response_rate,
            ),
            (0.8, 0.6666666667, 0.15),
        )
        for block_index in (1, 2, 3):
            rows = [value for value in first.trials if value.block_index == block_index]
            positions = [value.block_trial for value in rows if value.is_no_go]
            self.assertEqual(len(positions), 3)
            self.assertGreater(positions[0], 4)
            self.assertTrue(
                all(right - left - 1 >= 2 for left, right in pairwise(positions))
            )
        self.assertNotIn("digit", marker_label("dynamic_sart_stimulus_onset", trial=1))
        self.assertNotIn("condition", marker_label("dynamic_sart_stimulus_onset", trial=1))

    def test_behavior_summary_is_hashed_linked_and_separates_practice(self) -> None:
        profile = load_study1_profile()
        plan = build_task_plan(profile, participant_pseudonym="PRIVATE-DEMO")
        rows = (*plan.practice_trials, *plan.trials)
        records = [
            {
                "record": True,
                "trial": value.trial,
                "block_index": value.block_index,
                "block_name": value.block_name,
                "phase": value.phase,
                "block_trial": value.block_trial,
                "digit": value.digit,
                "is_no_go": value.is_no_go,
                "practice": value.practice,
                "practice_round": value.practice_round,
                "reaction_time_seconds": None,
                "valid_response": False,
                "correct": value.is_no_go,
            }
            for value in rows
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session.study1.task"
            summary = _write_behavior(root, plan, records, aborted=False)
            persisted = (root / "study1_dsart_summary.json").read_text(encoding="utf-8")

        self.assertEqual(summary["session_id"], "session.study1.task")
        self.assertEqual(summary["completed_trials"], 70)
        self.assertEqual(summary["completed_practice_trials"], 10)
        self.assertEqual(summary["completed_main_trials"], 60)
        self.assertEqual(summary["plan_hash"], plan.plan_hash)
        self.assertNotIn("PRIVATE-DEMO", persisted)
        self.assertTrue(summary["summary_hash"].startswith("sha256:"))

    def test_practice_metrics_apply_minimum_reaction_time(self) -> None:
        records = [
            {"is_no_go": False, "correct": True, "reaction_time_seconds": 0.3},
            {"is_no_go": False, "correct": False, "reaction_time_seconds": None},
            {"is_no_go": True, "correct": True, "reaction_time_seconds": None},
            {"is_no_go": True, "correct": False, "reaction_time_seconds": 0.05},
        ]
        metrics = _practice_metrics(records, 0.1)
        self.assertEqual(metrics["go_accuracy"], 0.5)
        self.assertEqual(metrics["no_go_accuracy"], 0.5)
        self.assertEqual(metrics["anticipatory_response_rate"], 0.25)

    def test_full_plan_preserves_600_trials_and_90_no_go(self) -> None:
        profile = load_study1_profile()
        plan = build_task_plan(
            profile,
            participant_pseudonym="PSEUDONYM-17",
            variant="full_study1",
        )
        self.assertEqual(plan.total_trials, 600)
        self.assertEqual(sum(int(value.is_no_go) for value in plan.trials), 90)
        self.assertEqual((plan.baseline_open_seconds, plan.baseline_closed_seconds), (120.0, 120.0))


class WorkbenchLiveOperationsTests(unittest.TestCase):
    def test_detect_review_compile_and_preflight_real_composed_project(self) -> None:
        pylsl, labels = _study1_network()
        with tempfile.TemporaryDirectory() as directory:
            profile = load_study1_profile()
            project = bootstrap_study1_project(
                profile,
                project_root=Path(directory) / "study1",
            ).project
            operations = WorkbenchOperations(pylsl_module=pylsl)
            detection = operations.scan_lsl(project.root, wait_time=0)
            self.assertEqual(
                tuple(value.name for value in detection.streams),
                ("NeuracleEEG", "EEGleMarkers"),
            )
            accepted = operations.accept_live_deployment(
                project.root,
                profile,
                detection,
                eeg_capability_id=detection.streams[0].capability_id,
                marker_capability_id=detection.streams[1].capability_id,
                overlay=SiteOverlay(labels, "uV", "REF", "GND", "none"),
            )
            self.assertTrue(
                all(
                    value.explicit_selection
                    for value in accepted.result.proposal.decisions
                    if value.requirement_id is not None
                )
            )
            reviewed_eeg = next(
                value
                for value in accepted.report.sources
                if value.capability_id == accepted.eeg_capability_id
            )
            site = reviewed_eeg.stream.metadata["site_overlay"]
            self.assertEqual(tuple(site["physical_channel_order"]), labels)
            self.assertEqual(tuple(site["logical_channel_order"]), profile.logical_channels)
            self.assertEqual(dict(site["selector"]), {"uid": "uid-neuracle"})
            self.assertEqual(site["trigger_status_position"], 65)
            build = operations.compile_and_preflight(project.root, accepted.report)
            self.assertEqual(build.compilation.deployment_role, "deployment_proposal")
            self.assertTrue(build.preflight.ready)
            marker = next(
                value
                for value in accepted.report.sources
                if value.plugin_id == LSL_SPARSE_SOURCE_PLUGIN_ID
            )
            packet = LslSource(marker.config, pylsl_module=pylsl).read()
            self.assertIsInstance(packet, SparseEventBatch)
            self.assertEqual(packet.events[0].kind, "dynamic_sart_stimulus_onset")

    def test_site_overlay_rejects_unreviewed_or_misplaced_status(self) -> None:
        labels = tuple(f"N{index:02d}" for index in range(1, 66))
        with self.assertRaisesRegex(ValueError, "TRIGGER_STATUS"):
            SiteOverlay(labels, "uV", "REF", "GND", "none", trigger_status_position=64)
        with self.assertRaisesRegex(ValueError, "reference"):
            SiteOverlay(labels, "uV", "", "GND", "none")

    def test_review_rejects_an_ambiguous_exact_selector(self) -> None:
        pylsl, labels = _study1_network()
        with tempfile.TemporaryDirectory() as directory:
            profile = load_study1_profile()
            project = bootstrap_study1_project(
                profile,
                project_root=Path(directory) / "study1",
            ).project
            operations = WorkbenchOperations(pylsl_module=pylsl)
            detection = operations.scan_lsl(project.root, wait_time=0)
            ambiguous = replace(
                detection,
                lsl=replace(
                    detection.lsl,
                    sources=(
                        *detection.lsl.sources,
                        detection.lsl.sources[0],
                    ),
                ),
            )

            with self.assertRaisesRegex(ValueError, "ambiguous"):
                operations.accept_live_deployment(
                    project.root,
                    profile,
                    ambiguous,
                    eeg_capability_id=detection.streams[0].capability_id,
                    marker_capability_id=detection.streams[1].capability_id,
                    overlay=SiteOverlay(labels, "uV", "REF", "GND", "none"),
                )

    def test_live_recheck_rejects_stream_disappearance(self) -> None:
        pylsl, labels = _study1_network()
        with tempfile.TemporaryDirectory() as directory:
            profile = load_study1_profile()
            project = bootstrap_study1_project(
                profile,
                project_root=Path(directory) / "study1",
            ).project
            operations = WorkbenchOperations(pylsl_module=pylsl)
            detection = operations.scan_lsl(project.root, wait_time=0)
            accepted = operations.accept_live_deployment(
                project.root,
                profile,
                detection,
                eeg_capability_id=detection.streams[0].capability_id,
                marker_capability_id=detection.streams[1].capability_id,
                overlay=SiteOverlay(labels, "uV", "REF", "GND", "none"),
            )
            operations.compile_and_preflight(project.root, accepted.report)
            pylsl.infos = ()

            with self.assertRaisesRegex(ValueError, "no longer"):
                operations.recheck_live_deployment(project.root, profile, accepted)


if __name__ == "__main__":
    unittest.main()
