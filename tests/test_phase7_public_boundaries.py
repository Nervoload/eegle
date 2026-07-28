from __future__ import annotations

import ast
import json
from pathlib import Path
import tomllib
import unittest

from eegle.authoring import (
    AUTHORING_EXPORT_SCHEMA_ID,
    AUTHORING_PROJECT_SCHEMA_ID,
    AUTHORING_PROVENANCE_SCHEMA_ID,
    DEPLOYMENT_REQUIREMENTS_SCHEMA_ID,
    EXPERIMENT_DRAFT_SCHEMA_ID,
    TEMPLATE_AUTHORING_SCHEMA_ID,
    TEMPLATE_DEFINITION_SCHEMA_ID,
    TEMPLATE_EXPANSION_LOCK_SCHEMA_ID,
    TemplateProfile,
    SourceKind,
    SourceLocation,
    validate_authoring_provenance_payload,
    validate_experiment_draft_payload,
)
from eegle.operations import (
    DETECTION_REPORT_SCHEMA_ID,
    DEPLOYMENT_PROPOSAL_SCHEMA_ID,
    EXPERIMENT_DIFF_SCHEMA_ID,
    EXPERIMENT_EXPLANATION_SCHEMA_ID,
    OPERATION_ERROR_SCHEMA_ID,
    OPERATION_RESULT_SCHEMA_ID,
    MODEL_REPLACEMENT_COMPARISON_SCHEMA_ID,
    PREFLIGHT_REPORT_SCHEMA_ID,
    PROJECT_MANIFEST_SCHEMA_ID,
    PROJECT_GRAPH_SCHEMA_ID,
    PROJECT_RESULT_SCHEMA_ID,
    REPLAY_INSPECTION_SCHEMA_ID,
    REHEARSAL_REPORT_SCHEMA_ID,
    SESSION_EXPORT_SCHEMA_ID,
    SESSION_INSPECTION_SCHEMA_ID,
    DifferenceImpact,
    ExitCode,
    OperationCategory,
    OperationDiagnostic,
    OperationError,
    RepairKind,
    RepairOption,
    ExplanationViewKind,
    validate_operation_error_payload,
    validate_operation_result_payload,
)
from eegle.models import (
    MODEL_PACKAGE_CHECK_SCHEMA_ID,
    MODEL_PACKAGE_MEDIA_TYPE,
    MODEL_PACKAGE_SCHEMA_ID,
    PACKED_MODEL_SCHEMA_ID,
    SYNTHETIC_TEST_VECTOR_SCHEMA_ID,
    SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID,
)
from eegle.specs import SchemaValidationError


ROOT = Path(__file__).resolve().parents[1]
SURFACE_PATH = ROOT / "docs" / "migration" / "phase7_public_surface.json"
TARGET_PACKAGES = {"authoring", "operations"}
FORBIDDEN_IMPORTS = (
    "eegle.analysis",
    "eegle.calibration",
    "eegle.cli",
    "eegle.config",
    "eegle.devices",
    "eegle.eeg_csv",
    "eegle.experiment",
    "eegle.factory",
    "eegle.feedback_manager",
    "eegle.hardware",
    "eegle.integrations.legacy_sessions",
    "eegle.integrations.task_environment",
    "eegle.lsl",
    "eegle.ml",
    "eegle.preflight",
    "eegle.psychopy_input",
    "eegle.realtime",
    "eegle.recording_health",
    "eegle.session",
    "eegle.tasks",
    "eegle.telemetry",
    "eegle.workers",
    "yaml",
    "ruamel",
)


class Phase7PublicBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.surface = json.loads(SURFACE_PATH.read_text(encoding="utf-8"))

    def test_public_surface_inventory_matches_package_exports(self) -> None:
        self.assertEqual(
            self.surface["schema"],
            "eegle.migration.phase7_public_surface.v1",
        )
        stable = {value["module"] for value in self.surface["stable_alpha"]}
        provisional = {
            value["module"] for value in self.surface["provisional_public"]
        }
        self.assertFalse(stable & provisional)
        self.assertEqual(
            stable,
            {
                "eegle",
                "eegle.actions",
                "eegle.compiler",
                "eegle.models",
                "eegle.plugins",
                "eegle.processing",
                "eegle.recording",
                "eegle.replay",
                "eegle.runtime",
                "eegle.specs",
                "eegle.streams",
            },
        )
        self.assertEqual(
            provisional,
            {"eegle.authoring", "eegle.integrations.lsl", "eegle.operations"},
        )
        for module in sorted(stable | provisional):
            with self.subTest(module=module):
                path = _package_init_path(module)
                exports = _literal_all(path)
                self.assertTrue(exports, f"{module} must declare a non-empty __all__")
                self.assertEqual(len(exports), len(set(exports)))
                if module == "eegle":
                    self.assertEqual(exports, {"ExecutionMode", "__version__"})

    def test_new_packages_are_selected_for_the_wheel_and_are_boundary_clean(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        included = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
        for package in TARGET_PACKAGES:
            self.assertIn(f"eegle.{package}*", included)

        violations: list[str] = []
        for package in TARGET_PACKAGES:
            for path in sorted((ROOT / "eegle" / package).glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    modules = _imported_modules(node)
                    for module in modules:
                        if (
                            path.name == "yaml.py"
                            and (
                                module == "ruamel"
                                or module.startswith("ruamel.")
                            )
                        ):
                            continue
                        if any(
                            module == prefix or module.startswith(prefix + ".")
                            for prefix in FORBIDDEN_IMPORTS
                        ):
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno}: {module}"
                            )
        self.assertEqual(violations, [], "boundary imports:\n" + "\n".join(violations))

    def test_draft_and_provenance_envelopes_are_versioned_and_separate(self) -> None:
        self.assertEqual(
            set(self.surface["authoring_schemas"]),
            {
                EXPERIMENT_DRAFT_SCHEMA_ID,
                AUTHORING_PROVENANCE_SCHEMA_ID,
                DEPLOYMENT_REQUIREMENTS_SCHEMA_ID,
                TEMPLATE_EXPANSION_LOCK_SCHEMA_ID,
                TEMPLATE_DEFINITION_SCHEMA_ID,
                TEMPLATE_AUTHORING_SCHEMA_ID,
                AUTHORING_EXPORT_SCHEMA_ID,
                AUTHORING_PROJECT_SCHEMA_ID,
            },
        )
        self.assertFalse(self.surface["authoring_lowering"]["runtime_input"])
        self.assertFalse(self.surface["authoring_surfaces"]["runtime_input"])
        self.assertEqual(
            self.surface["authoring_surfaces"]["shared_source_schema"],
            TEMPLATE_AUTHORING_SCHEMA_ID,
        )
        self.assertFalse(self.surface["templates"]["runtime_input"])
        self.assertTrue(self.surface["templates"]["exact_version_required"])
        self.assertFalse(self.surface["templates"]["implicit_latest"])
        self.assertEqual(
            set(self.surface["templates"]["profiles"]),
            {value.value for value in TemplateProfile},
        )
        draft = {
            "schema": EXPERIMENT_DRAFT_SCHEMA_ID,
            "draft_id": "attention-state",
            "revision": 1,
            "intent": {
                "study": {"execution_mode": "causal"},
                "signals": [{"signal_id": "eeg"}],
                "deployment_requirements": {"live_stream": "unresolved"},
            },
            "unresolved": [
                {
                    "path": "/intent/deployment_requirements/live_stream",
                    "kind": "detection",
                    "prompt": "Select an EEG stream",
                }
            ],
        }
        validate_experiment_draft_payload(draft)

        digest = "sha256:" + "a" * 64
        sidecar = {
            "schema": AUTHORING_PROVENANCE_SCHEMA_ID,
            "draft_id": "attention-state",
            "draft_revision": 1,
            "draft_digest": digest,
            "canonical_targets": {
                "protocol": {"schema": "eegle.protocol_spec.v1", "digest": digest},
                "suite": {"schema": "eegle.suite_spec.v1", "digest": digest},
            },
            "entries": [
                {
                    "target_artifact": "protocol",
                    "target_path": "/execution_mode",
                    "origin": "user_explicit",
                    "source": {
                        "kind": "yaml",
                        "locator": "experiment.yaml",
                        "line": 2,
                        "column": 7,
                    },
                    "materiality": "scientific",
                    "confirmation": "not_required",
                }
            ],
        }
        validate_authoring_provenance_payload(sidecar)

        leaked = dict(draft)
        leaked["provenance"] = sidecar
        with self.assertRaises(SchemaValidationError):
            validate_experiment_draft_payload(leaked)

        malformed = dict(sidecar)
        malformed["entries"] = [
            {**sidecar["entries"][0], "origin": "compiler_derived"}
        ]
        with self.assertRaises(SchemaValidationError):
            validate_authoring_provenance_payload(malformed)

    def test_operation_error_round_trips_through_the_public_contract(self) -> None:
        source = SourceLocation(
            kind=SourceKind.YAML,
            locator="experiment.yaml",
            line=12,
            column=5,
        )
        diagnostic = OperationDiagnostic(
            code="authoring.unresolved_stream",
            category=OperationCategory.AUTHORING,
            title="EEG stream is unresolved",
            message="Select a detected stream before compilation.",
            path="/intent/deployment_requirements/live_stream",
            source=source,
            repairs=(
                RepairOption(
                    repair_id="select.detected_stream",
                    title="Choose one detected stream",
                    kind=RepairKind.AUTOMATIC_PROPOSAL,
                    changes_scientific_semantics=False,
                ),
            ),
            documentation="docs/deployment.md#stream-selection",
            details={"candidate_count": 2},
        )
        error = OperationError(
            operation="compile",
            exit_code=ExitCode.INVALID_INPUT,
            diagnostics=(diagnostic,),
        )
        payload = error.to_payload()
        self.assertEqual(payload["schema"], OPERATION_ERROR_SCHEMA_ID)
        validate_operation_error_payload(payload)
        restored = OperationError.from_payload(payload)
        self.assertEqual(restored.to_payload(), payload)

        result = {
            "schema": OPERATION_RESULT_SCHEMA_ID,
            "ok": True,
            "operation": "new",
            "exit_code": 0,
            "result": {"schema": "eegle.project_result.v1"},
        }
        validate_operation_result_payload(result)
        self.assertEqual(str(restored), str(error))

    def test_explanation_inventory_declares_non_executable_complete_views(self) -> None:
        self.assertEqual(
            set(self.surface["operation_schemas"]),
            {
                OPERATION_ERROR_SCHEMA_ID,
                OPERATION_RESULT_SCHEMA_ID,
                EXPERIMENT_EXPLANATION_SCHEMA_ID,
                EXPERIMENT_DIFF_SCHEMA_ID,
                PROJECT_MANIFEST_SCHEMA_ID,
                PROJECT_GRAPH_SCHEMA_ID,
                PROJECT_RESULT_SCHEMA_ID,
                SESSION_INSPECTION_SCHEMA_ID,
                REPLAY_INSPECTION_SCHEMA_ID,
                MODEL_REPLACEMENT_COMPARISON_SCHEMA_ID,
                SESSION_EXPORT_SCHEMA_ID,
                DETECTION_REPORT_SCHEMA_ID,
                DEPLOYMENT_PROPOSAL_SCHEMA_ID,
                PREFLIGHT_REPORT_SCHEMA_ID,
                REHEARSAL_REPORT_SCHEMA_ID,
            },
        )
        explanation = self.surface["explanations"]
        self.assertEqual(
            set(explanation["views"]),
            {value.value for value in ExplanationViewKind},
        )
        self.assertEqual(
            set(explanation["difference_impacts"]),
            {value.value for value in DifferenceImpact},
        )
        self.assertFalse(explanation["repair_proposals_mutate_input"])
        self.assertFalse(explanation["runtime_input"])

    def test_exit_code_numbers_are_stable(self) -> None:
        expected = {
            "SUCCESS": 0,
            "USAGE_ERROR": 2,
            "INVALID_INPUT": 3,
            "REJECTED": 4,
            "UNAVAILABLE": 5,
            "EXECUTION_FAILED": 6,
            "INTEGRITY_FAILED": 7,
            "INSUFFICIENT_EVIDENCE": 8,
            "INTERNAL_ERROR": 70,
        }
        self.assertEqual({value.name: int(value) for value in ExitCode}, expected)
        self.assertEqual(self.surface["exit_codes"], expected)
        with self.assertRaisesRegex(ValueError, "success exit code"):
            OperationError(
                operation="compile",
                exit_code=ExitCode.SUCCESS,
                diagnostics=(
                    OperationDiagnostic(
                        code="internal.invalid",
                        category=OperationCategory.INTERNAL,
                        title="Invalid test",
                        message="Success cannot represent an error.",
                    ),
                ),
            )

    def test_cli_inventory_matches_the_console_entry_and_base_commands(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        cli = self.surface["cli"]
        self.assertTrue(cli["installed"])
        self.assertEqual(pyproject["project"]["scripts"]["eegle"], "eegle.__main__:main")
        self.assertEqual(cli["entry_point"], "eegle=eegle.__main__:main")
        self.assertEqual(
            set(cli["base_commands"]),
            {
                "new",
                "detect",
                "compile",
                "explain",
                "diff",
                "graph",
                "preflight",
                "rehearse",
                "run",
                "inspect",
                "replay",
                "compare",
                "export",
                "model",
            },
        )
        self.assertEqual(cli["machine_envelope"], OPERATION_RESULT_SCHEMA_ID)
        self.assertEqual(cli["runtime_input"], "verified_execution_plan_and_lock")

        sessions = self.surface["session_operations"]
        self.assertEqual(
            sessions["default_failure_mode"],
            "structured_partial_or_unavailable_result",
        )
        self.assertEqual(sessions["strict_cli_mode"], "explicit_opt_in")
        self.assertFalse(sessions["unfinished_writers_mutated"])
        self.assertFalse(sessions["source_data_deleted"])
        self.assertFalse(sessions["external_process_signalled"])
        self.assertEqual(sessions["safe_export_allowed_sensitivities"], ["public"])
        self.assertFalse(sessions["safe_export_overwrites_destination"])
        self.assertFalse(sessions["raw_participant_values_exported_by_default"])
        self.assertFalse(
            sessions["protected_deployment_details_exported_by_default"]
        )

    def test_model_packaging_inventory_has_no_framework_runtime_authority(self) -> None:
        packaging = self.surface["model_packaging"]
        self.assertEqual(
            set(packaging["schemas"]),
            {
                MODEL_PACKAGE_SCHEMA_ID,
                MODEL_PACKAGE_CHECK_SCHEMA_ID,
                PACKED_MODEL_SCHEMA_ID,
                SYNTHETIC_TEST_VECTOR_SCHEMA_ID,
                SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID,
            },
        )
        self.assertEqual(packaging["media_type"], MODEL_PACKAGE_MEDIA_TYPE)
        self.assertEqual(packaging["adapter_boundary"], "independent_plugin_wheel")
        self.assertFalse(packaging["framework_runtime"])
        self.assertFalse(packaging["training"])
        self.assertFalse(packaging["implicit_download"])

    def test_yaml_decision_keeps_the_base_dependency_free(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        base = tuple(pyproject["project"]["dependencies"])
        self.assertFalse(
            any(
                "yaml" in dependency.lower() or "ruamel" in dependency.lower()
                for dependency in base
            )
        )
        decision = self.surface["yaml"]
        self.assertEqual(decision["distribution"], "optional_extra")
        self.assertEqual(decision["extra"], "yaml")
        self.assertEqual(decision["dependency"], "ruamel.yaml>=0.18.10,<0.19")
        self.assertFalse(decision["base_dependency"])
        self.assertEqual(
            pyproject["project"]["optional-dependencies"]["yaml"],
            ["ruamel.yaml>=0.18.10,<0.19"],
        )


def _package_init_path(module: str) -> Path:
    parts = module.split(".")
    if len(parts) == 1:
        return ROOT / parts[0] / "__init__.py"
    return ROOT.joinpath(*parts, "__init__.py")


def _literal_all(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{path} does not declare a literal __all__")


def _imported_modules(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.module:
        return (node.module,)
    return ()


if __name__ == "__main__":
    unittest.main()
