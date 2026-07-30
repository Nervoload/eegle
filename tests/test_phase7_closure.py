from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLOSURE_PATH = ROOT / "docs" / "migration" / "phase7_closure_evidence.json"


class Phase7ClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.evidence = json.loads(CLOSURE_PATH.read_text(encoding="utf-8"))

    def test_all_exit_gates_and_closure_requirements_are_mapped(self) -> None:
        self.assertEqual(
            self.evidence["schema"],
            "eegle.migration.phase7_closure_evidence.v1",
        )
        gates = {value["gate_id"]: value for value in self.evidence["exit_gates"]}
        self.assertEqual(
            set(gates),
            {f"P7-EG-{index:02d}" for index in range(1, 16)},
        )
        requirements = {
            value["requirement_id"]: value
            for value in self.evidence["closure_requirements"]
        }
        self.assertEqual(
            set(requirements),
            {f"P7-C-{index:02d}" for index in range(1, 12)},
        )
        for requirement in requirements.values():
            self.assertTrue(requirement["gate_ids"])
            self.assertTrue(set(requirement["gate_ids"]) <= set(gates))

    def test_only_truthful_external_gates_block_phase_closure(self) -> None:
        gates = {value["gate_id"]: value for value in self.evidence["exit_gates"]}
        pending = {
            gate_id
            for gate_id, value in gates.items()
            if value["status"] == "pending_external"
        }
        self.assertEqual(pending, {"P7-EG-06", "P7-EG-15"})
        self.assertEqual(
            {value["gate_id"] for value in self.evidence["blocking_gates"]},
            pending,
        )
        self.assertEqual(self.evidence["phase_status"], "blocked_external")
        self.assertEqual(
            self.evidence["support_boundaries"]["lsl"],
            "simulated_validated",
        )
        self.assertEqual(
            self.evidence["support_boundaries"]["public_alpha"],
            "phase_8",
        )

    def test_every_recorded_repository_evidence_path_exists(self) -> None:
        for gate in self.evidence["exit_gates"]:
            for field in ("tests", "docs", "workflows"):
                for relative in gate.get(field, ()):
                    with self.subTest(gate=gate["gate_id"], path=relative):
                        self.assertTrue((ROOT / relative).is_file(), relative)

    def test_remote_baseline_is_exact_and_requires_candidate_refresh(self) -> None:
        remote = self.evidence["remote_matrix"]
        self.assertEqual(remote["run_id"], 30491548423)
        self.assertEqual(
            remote["head_sha"],
            "417d8a44ed2e2bc39bdef990b8cfc16ce12c2740",
        )
        self.assertEqual(remote["conclusion"], "success")
        self.assertTrue(remote["current_candidate_refresh_required"])
        self.assertEqual(len(remote["jobs"]), 5)
        self.assertTrue(all(value["conclusion"] == "success" for value in remote["jobs"]))
        self.assertEqual(
            {value["name"] for value in remote["jobs"]},
            {
                "Python 3.11 on ubuntu-latest",
                "Python 3.12 on ubuntu-latest",
                "Python 3.13 on ubuntu-latest",
                "Python 3.12 on macos-latest",
                "Python 3.12 on windows-latest",
            },
        )

    def test_native_lsl_evidence_is_simulated_and_artifact_based(self) -> None:
        native = self.evidence["local_native_lsl"]
        self.assertEqual(native["status"], "pass")
        self.assertEqual(native["support_level"], "simulated_validated")
        self.assertEqual(native["installed_artifact"], "wheel")
        self.assertFalse(native["real_hardware_claim"])
        self.assertTrue((ROOT / native["client"]).is_file())
        self.assertEqual(
            set(native["observations"]),
            {
                "full_channel_labels:C3,C4",
                "channel_units:uV,uV",
                "dense_packet_samples:2",
            },
        )

    def test_human_closure_record_preserves_narrow_support_claims(self) -> None:
        closure = (ROOT / "docs" / "PHASE7_CLOSURE.md").read_text(encoding="utf-8")
        self.assertIn("Phase 7 remains blocked", closure)
        self.assertIn("current closure candidate", closure)
        self.assertIn("dense capture to\n`RawArray`", closure)
        self.assertIn("pre-alpha, simulation-first candidate", closure)


if __name__ == "__main__":
    unittest.main()
