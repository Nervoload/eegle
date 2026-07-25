from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TARGET_PACKAGES = (
    "actions",
    "compiler",
    "models",
    "ml",
    "plugins",
    "processing",
    "recording",
    "replay",
    "runtime",
    "specs",
    "streams",
)
LEGACY_PREFIXES = (
    "eegle.analysis",
    "eegle.calibration",
    "eegle.cli",
    "eegle.config",
    "eegle.experiment",
    "eegle.factory",
    "eegle.feedback_manager",
    "eegle.hardware",
    "eegle.integrations",
    "eegle.lsl",
    "eegle.pipelines",
    "eegle.preflight",
    "eegle.realtime",
    "eegle.session",
    "eegle.tasks",
    "eegle.telemetry",
    "eegle.workers",
)


class SourceBoundaryTests(unittest.TestCase):
    def test_target_packages_do_not_import_legacy_application_code(self) -> None:
        violations: list[str] = []
        for package in TARGET_PACKAGES:
            for path in sorted((ROOT / "eegle" / package).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                package_parts = ["eegle", *path.relative_to(ROOT / "eegle").parent.parts]
                for node in ast.walk(tree):
                    imported: list[str] = []
                    if isinstance(node, ast.Import):
                        imported = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        if node.level:
                            keep = len(package_parts) - (node.level - 1)
                            anchor = package_parts[:keep]
                            if node.module:
                                imported = [".".join([*anchor, node.module])]
                            else:
                                imported = [".".join([*anchor, alias.name]) for alias in node.names]
                        elif node.module:
                            imported = [node.module]
                    for module in imported:
                        if any(module == prefix or module.startswith(prefix + ".") for prefix in LEGACY_PREFIXES):
                            violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")

        self.assertEqual(violations, [], "target-to-legacy imports:\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
