from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from eegle.operations import check_plugin, inspect_plugins
from eegle.operations.cli import main as cli_main
from eegle.plugins import (
    ComponentKind,
    ConformanceStatus,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
    PluginCapabilities,
    PluginDescriptor,
    PluginExercise,
    PortSpec,
    StateBehavior,
    check_plugin_conformance,
)
from eegle.streams import TimePoint

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_MODELS = (
    ROOT
    / "examples"
    / "plugins"
    / "eegle-example-models"
    / "src"
    / "eegle_example_models"
    / "__init__.py"
)


def _load_example_models():
    specification = importlib.util.spec_from_file_location(
        "eegle_phase7_conformance_models",
        EXAMPLE_MODELS,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load example model distribution")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _window(values, validity_mask=None):
    return SimpleNamespace(
        values=np.asarray(values, dtype=float),
        validity_mask=None
        if validity_mask is None
        else np.asarray(validity_mask, dtype=bool),
    )


class _LifecycleContext:
    execution_id = "execution.conformance"
    component_id = "component.lifecycle"
    component_version = "1.0.0"
    execution_mode = ExecutionMode.CAUSAL
    current_time = TimePoint(1.0, "host.monotonic")
    clock_mapping_revisions = {}

    def next_id(self, namespace: str) -> str:
        return f"conformance.{namespace}"


class Phase7PluginConformanceTests(unittest.TestCase):
    def test_descriptor_inspection_and_static_check_never_construct(self) -> None:
        calls: list[str] = []
        descriptor = PluginDescriptor(
            "fixture.inspection.safe",
            "1.0.0",
            ComponentKind.TRANSFORM,
            {"type": "object", "additionalProperties": False},
            (PortSpec("input", "fixture.input.v1"),),
            (PortSpec("output", "fixture.output.v1"),),
            PluginCapabilities(
                frozenset({ExecutionMode.CAUSAL}),
                Determinism.DETERMINISTIC,
                EquivalenceLevel.BITWISE,
                StateBehavior.STATELESS,
            ),
            lambda config: calls.append("constructed"),
            "fixture:never_constructed",
        )

        report = check_plugin_conformance(descriptor)

        self.assertTrue(report.ready)
        self.assertFalse(report.constructed)
        self.assertEqual(calls, [])
        self.assertEqual(
            report.to_payload()["checks"][1]["check_id"],
            "descriptor.factory_not_invoked",
        )

    def test_example_models_cover_valid_partial_and_all_invalid_windows(self) -> None:
        module = _load_example_models()
        for descriptor in module.plugin_descriptors():
            exercises = (
                PluginExercise(
                    "valid",
                    lambda component: component.predict(
                        _window([[-2.0], [1.0]]), SimpleNamespace()
                    ),
                    expected_abstained=False,
                ),
                PluginExercise(
                    "partially_invalid",
                    lambda component: component.predict(
                        _window([[100.0], [2.0]], [[False], [True]]),
                        SimpleNamespace(),
                    ),
                    expected_abstained=False,
                ),
                PluginExercise(
                    "empty",
                    lambda component: component.predict(
                        _window(np.empty((0, 1), dtype=float)),
                        SimpleNamespace(),
                    ),
                    expected_abstained=True,
                ),
                PluginExercise(
                    "all_invalid",
                    lambda component: component.predict(
                        _window([[1.0], [2.0]], [[False], [False]]),
                        SimpleNamespace(),
                    ),
                    expected_abstained=True,
                ),
            )
            with self.subTest(plugin_id=descriptor.plugin_id):
                report = check_plugin_conformance(
                    descriptor,
                    config={},
                    construct=True,
                    exercises=exercises,
                )
                self.assertTrue(report.ready, report.to_payload())
                statuses = {value.check_id: value.status for value in report.checks}
                self.assertEqual(
                    statuses["execution.empty"], ConformanceStatus.PASS
                )
                self.assertEqual(
                    statuses["execution.all_invalid"], ConformanceStatus.PASS
                )
                self.assertEqual(
                    statuses["replay.equivalence"], ConformanceStatus.PASS
                )

                component = descriptor.factory({})
                result = component.predict(
                    _window([[1.0]], [[False]]), SimpleNamespace()
                )
                self.assertTrue(result.abstained)
                self.assertEqual(result.abstention_reason, "all_samples_invalid")
                self.assertTrue(np.isfinite(float(result.value["score"])))

    def test_failure_path_still_runs_lifecycle_cleanup(self) -> None:
        events: list[str] = []

        class Component:
            def start(self, context) -> None:
                events.append("start")

            def stop(self, context) -> None:
                events.append("stop")

            def update(self, value, context):
                return value

            def fail(self):
                raise RuntimeError("expected exercise failure")

        descriptor = PluginDescriptor(
            "fixture.lifecycle.cleanup",
            "1.0.0",
            ComponentKind.TRANSFORM,
            {"type": "object", "additionalProperties": False},
            (PortSpec("input", "fixture.input.v1"),),
            (PortSpec("output", "fixture.output.v1"),),
            PluginCapabilities(
                frozenset({ExecutionMode.CAUSAL}),
                Determinism.DETERMINISTIC,
                EquivalenceLevel.BITWISE,
                StateBehavior.STATELESS,
            ),
            lambda config: Component(),
            "fixture:Component",
        )

        report = check_plugin_conformance(
            descriptor,
            construct=True,
            lifecycle_context=_LifecycleContext(),
            exercises=(PluginExercise("failure", lambda component: component.fail()),),
        )

        self.assertFalse(report.ready)
        self.assertEqual(events, ["start", "stop"])
        self.assertEqual(
            next(value for value in report.checks if value.check_id == "execution.cleanup").status,
            ConformanceStatus.PASS,
        )

    def test_operations_and_cli_expose_safe_plugin_tools(self) -> None:
        inspection = inspect_plugins(
            "eegle.processing.identity",
            include_entry_points=False,
        )
        self.assertFalse(inspection.to_payload()["factory_invoked"])
        self.assertTrue(
            check_plugin(
                "eegle.processing.identity",
                include_entry_points=False,
            ).ready
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(
                [
                    "--json",
                    "plugin",
                    "inspect",
                    "eegle.processing.identity",
                    "--no-entry-points",
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["result"]["factory_invoked"])


if __name__ == "__main__":
    unittest.main()
