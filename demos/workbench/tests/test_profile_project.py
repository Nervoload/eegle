from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from demos.workbench.profile import build_study1_design, load_study1_profile
from demos.workbench.project import bootstrap_study1_project
from eegle.operations import explain_project, open_project


class Study1ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_study1_profile()

    def test_full_protocol_and_rehearsal_are_frozen(self) -> None:
        full = self.profile.variant("full_study1")
        rehearsal = self.profile.variant("demo_rehearsal")
        self.assertEqual(full["baseline"], {"eyes_open_seconds": 120.0, "eyes_closed_seconds": 120.0})
        self.assertEqual([block["trials"] for block in full["blocks"]], [200, 200, 200])
        self.assertEqual([block["planned_no_go_count"] for block in full["blocks"]], [30, 30, 30])
        self.assertEqual(full["practice"]["trials_per_round"], 30)
        self.assertEqual(full["practice"]["max_rounds"], 3)
        self.assertEqual(rehearsal["scientific_status"], "demonstration_only")
        self.assertEqual(rehearsal["session_kind"], "demo_rehearsal")
        self.assertEqual(rehearsal["baseline"], {"eyes_open_seconds": 15.0, "eyes_closed_seconds": 15.0})
        self.assertEqual(rehearsal["practice"]["trials_per_round"], 10)
        self.assertEqual([block["trials"] for block in rehearsal["blocks"]], [20, 20, 20])
        self.assertEqual([block["planned_no_go_count"] for block in rehearsal["blocks"]], [3, 3, 3])
        self.assertEqual(rehearsal["blocks"][1]["break_after_seconds"], 5.0)

    def test_profile_preserves_source_provenance(self) -> None:
        self.assertEqual(
            self.profile.provenance["source_commit"],
            "29d9b88f2a6653291e7cf6d9677286a8f9e4b59b",
        )
        blobs = {
            value["path"]: value["git_blob_hash"]
            for value in self.profile.provenance["source_files"]
        }
        self.assertEqual(
            blobs["eegle/pipelines/study1.py"],
            "b233bd32e4233360e19a2f563edbd969abac6695",
        )
        self.assertEqual(
            blobs["configs/study1_neuracle64.json"],
            "397a35120a7ab6bc3779959df30708eef02483c0",
        )

    def test_profile_lowers_to_real_label_blind_design(self) -> None:
        design = build_study1_design(self.profile)
        self.assertEqual(len(design.signals), 1)
        self.assertEqual(len(design.signals[0].channels), 65)
        self.assertEqual(design.signals[0].channels[0], "PHYSIO_01")
        self.assertEqual(design.signals[0].channels[-2:], ("PHYSIO_64", "TRIGGER_STATUS"))
        self.assertEqual(design.signals[0].nominal_rate_hz, 1000.0)
        self.assertIn("dynamic_sart_stimulus_onset", design.events[0].event_kinds)
        self.assertEqual(design.windows[0].start_offset_seconds, -0.75)
        self.assertEqual(design.windows[0].end_offset_seconds, -0.10)
        self.assertEqual(design.study.annotations["policy"], "observe_only")
        self.assertNotIn("no_go_digit", design.study.annotations)
        self.assertEqual(len(design.phases), 1)
        built = design.build()
        self.assertEqual(built.protocol.protocol_id, "protocol.study1-neuracle64-demo")


class ProjectBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_study1_profile()

    def test_bootstrap_creates_then_idempotently_reopens_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / self.profile.project_id
            first = bootstrap_study1_project(self.profile, project_root=root)
            before = (root / "eegle-project.json").read_bytes()
            second = bootstrap_study1_project(self.profile, project_root=root)
            self.assertEqual(before, (root / "eegle-project.json").read_bytes())
            self.assertEqual(first.snapshot.design_digest, second.snapshot.design_digest)
            self.assertEqual(first.snapshot.profile_digest, self.profile.digest)
            self.assertFalse(second.snapshot.compiled)
            self.assertEqual(second.snapshot.session_uris, ())
            self.assertEqual(open_project(root).manifest.project_id, self.profile.project_id)
            self.assertIn("canonical_hashes", explain_project(root))

    def test_existing_non_project_directory_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / self.profile.project_id
            root.mkdir()
            sentinel = root / "do-not-overwrite.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                bootstrap_study1_project(self.profile, project_root=root)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_profile_digest_mismatch_is_warning_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / self.profile.project_id
            bootstrap_study1_project(self.profile, project_root=root)
            changed = replace(self.profile, digest="sha256:" + "0" * 64)
            reopened = bootstrap_study1_project(changed, project_root=root)
            self.assertEqual(len(reopened.issues), 1)
            self.assertEqual(reopened.issues[0].code, "workbench.profile_stale")
            source = json.loads((root / "authoring" / "experiment.json").read_text())
            self.assertEqual(
                source["study"]["annotations"]["workbench_profile_digest"],
                self.profile.digest,
            )


if __name__ == "__main__":
    unittest.main()
