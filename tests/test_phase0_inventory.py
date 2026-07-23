from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "docs" / "migration" / "phase0_baseline.json"
INVENTORY_PATH = ROOT / "docs" / "PHASE0_INVENTORY.md"
MIGRATION_PATH = ROOT / "docs" / "MIGRATION.md"


class Phase0InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        cls.inventory = INVENTORY_PATH.read_text(encoding="utf-8")

    def test_baseline_identity_and_compatibility_policy_are_explicit(self) -> None:
        self.assertEqual(
            self.baseline["schema"], "eegle.migration.phase0_baseline.v1"
        )
        self.assertEqual(self.baseline["branch"], "sep")
        self.assertEqual(len(self.baseline["baseline_commit"]), 40)
        policy = self.baseline["historical_artifact_policy"]
        self.assertEqual(policy["decision"], "scoped_one_time_importer_later")
        self.assertFalse(policy["runtime_compatibility_layer"])
        self.assertTrue(policy["preserve_originals"])

    def test_recorded_repository_paths_exist(self) -> None:
        paths = [item["path"] for item in self.baseline["largest_modules"]]
        paths.extend(self.baseline["configs"])
        paths.extend(self.baseline["legacy_documents"])
        for relative_path in paths:
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_every_current_top_level_package_has_a_disposition(self) -> None:
        for package in self.baseline["package_file_counts"]:
            with self.subTest(package=package):
                self.assertIn(f"`eegle.{package}`", self.inventory)

    def test_selected_reference_recipes_are_named(self) -> None:
        recipes = self.baseline["reference_recipes"]
        for required in ("classify8", "attention8", "dsart8"):
            self.assertIn(required, recipes)
            self.assertIn(f"`{required}`", self.inventory)

    def test_legacy_documents_have_authority_notices(self) -> None:
        for relative_path in self.baseline["legacy_documents"]:
            with self.subTest(path=relative_path):
                prefix = (ROOT / relative_path).read_text(encoding="utf-8")[:800]
                self.assertIn("Legacy", prefix)
                self.assertIn("EEGLE.md", prefix)

    def test_baseline_console_scripts_are_not_v1_packaging_authority(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for name, target in self.baseline["console_scripts"].items():
            with self.subTest(command=name):
                self.assertNotIn(f'{name} = "{target}"', pyproject)

    def test_user_notes_are_normalized_into_normative_migration_text(self) -> None:
        migration = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertNotIn("-> **", migration)
        self.assertIn("scoped, one-time historical artifact importer", migration)
        self.assertIn("`classify8`, `attention8`, and `dsart8`", migration)
        self.assertIn("Defer destructive source cleanup", migration)


if __name__ == "__main__":
    unittest.main()
