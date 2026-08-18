from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QAbstractAnimation, QSettings, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel, QLineEdit

    from demos.workbench.app import create_application
    from demos.workbench.controller import WorkbenchController
    from demos.workbench.main_window import WorkbenchWindow
    from demos.workbench.settings import WorkbenchSettings
    from demos.workbench.state import (
        BuildSnapshot,
        EnvironmentMode,
        Page,
        RunnerSnapshot,
        RunnerStatus,
    )
except ModuleNotFoundError:
    QSettings = None


@unittest.skipUnless(QSettings is not None, "PySide6 is a Workbench-only dependency")
class WorkbenchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_application(["eegle-workbench-test"])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = WorkbenchSettings(
            QSettings(str(root / "workbench.ini"), QSettings.Format.IniFormat)
        )
        self.controller = WorkbenchController(
            project_root=root / "study1-neuracle64-demo",
            settings=settings,
        )
        self.window = WorkbenchWindow(self.controller)
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.temp.cleanup()

    def test_all_pages_are_routed_and_render_at_supported_sizes(self) -> None:
        self.assertEqual(set(self.window.pages), set(Page))
        for page in Page:
            self.controller.navigate(page)
            self.app.processEvents()
            self.assertIs(self.window.stack.currentWidget(), self.window.pages[page])
        for width, height in ((1440, 900), (1120, 720)):
            self.window.resize(width, height)
            self.app.processEvents()
            image = self.window.grab().toImage()
            self.assertGreater(image.width(), 1000)
            self.assertGreater(image.height(), 650)

    def test_design_primary_advances_but_edit_focus_blocks_enter(self) -> None:
        self.controller.navigate(Page.DESIGN)
        self.app.processEvents()
        self.window.setFocus()
        self.app.processEvents()
        self.assertTrue(self.window.pages[Page.DESIGN].primary_button.isEnabled())
        self.window.pages[Page.DESIGN].primary_button.click()
        self.assertEqual(self.controller.state.page, Page.APPARATUS)
        self.controller.navigate(Page.DESIGN)
        editor = QLineEdit(self.window)
        editor.show()
        editor.setFocus()
        self.app.processEvents()
        self.assertFalse(self.window._primary_is_safe())
        editor.close()

    def test_live_actions_are_truthfully_gated_and_psychopy_is_required(self) -> None:
        for page in (Page.APPARATUS, Page.BUILD, Page.SESSIONS, Page.REPLAY):
            widget = self.window.pages[page]
            self.assertFalse(widget.primary_button.isEnabled())
            self.assertTrue(widget.action_hint.text())
        run = self.window.pages[Page.RUN]
        self.assertTrue(run.primary_button.isEnabled())
        self.assertIn("Arm PsychoPy", run.primary_button.text())
        self.assertIn("marker outlet", run.action_hint.text())
        self.assertFalse(run.stop_button.isVisible())

    def test_simulation_runs_inspects_and_replays_through_the_controller(self) -> None:
        self.controller.set_environment(EnvironmentMode.SIMULATION)
        self.controller.compile_and_preflight()
        self._wait_until(lambda: not self.controller.state.busy, timeout_ms=12000)
        self.assertTrue(self.controller.state.build.preflight_ready)
        self.assertEqual(
            self.controller.state.build.deployment_role,
            "simulation_deployment",
        )

        self.controller.start_run("")
        self._wait_until(
            lambda: bool(self.controller.state.sessions),
            timeout_ms=20000,
        )
        self.assertEqual(self.controller.state.runner.status, RunnerStatus.COMPLETED)
        session = self.controller.state.sessions[0]
        self.assertTrue(session.valid)
        self.assertTrue(session.replay_ready)
        self.assertIn(".simulation.", session.session_id)
        marker_counts = {
            key: count
            for stream in session.source_health["streams"]
            for key, count in dict(stream["event_kind_counts"]).items()
        }
        self.assertEqual(marker_counts["dynamic_sart_task_start"], 1)

        self.controller.replay_selected_session(session.session_id)
        self._wait_until(lambda: not self.controller.state.busy, timeout_ms=12000)
        self.assertTrue(self.controller.state.replay.equivalent)
        self.assertGreater(self.controller.state.replay.compared_record_count, 0)

    def _wait_until(self, predicate, *, timeout_ms: int) -> None:
        elapsed = 0
        while not predicate() and elapsed < timeout_ms:
            self.app.processEvents()
            time.sleep(0.05)
            elapsed += 50
        self.assertTrue(predicate(), f"condition was not met within {timeout_ms} ms")

    def test_sidebar_uses_real_assets_without_badges_and_animates_groups(self) -> None:
        sidebar = self.window.sidebar
        self.assertEqual(sidebar.width(), 304)
        self.assertEqual(sidebar.new_button.text(), "New Experiment")
        self.assertFalse(sidebar.new_button.icon().isNull())
        self.assertFalse(sidebar._experiments_header._icon.pixmap().isNull())
        self.assertFalse(sidebar._replay_header._icon.pixmap().isNull())
        self.assertFalse(
            any(label.property("pill") for label in self.window.findChildren(QLabel))
        )

        self.assertFalse(sidebar._replay_panel.isVisible())
        sidebar._replay_header.click()
        animation = sidebar._panel_animations["replay"]
        self.assertEqual(animation.state(), QAbstractAnimation.State.Running)
        QTest.qWait(animation.duration() + 40)
        self.app.processEvents()
        self.assertTrue(sidebar._replay_panel.isVisible())

    def test_task_completion_uses_bounded_trailing_marker_drain(self) -> None:
        calls: list[str] = []
        with patch.object(
            self.controller.runner_process,
            "complete",
            side_effect=calls.append,
        ):
            self.controller._handle_task_message(
                {
                    "type": "completed",
                    "behavior_summary": {"completed_trials": 70},
                }
            )
            self.assertEqual(calls, [])
            QTest.qWait(550)
            self.app.processEvents()
        self.assertEqual(calls, ["task_complete"])

    def test_environment_switch_invalidates_build_and_preflight(self) -> None:
        self.controller.state = replace(
            self.controller.state,
            build=BuildSnapshot(
                plan_hash="sha256:" + "1" * 64,
                lock_hash="sha256:" + "2" * 64,
                preflight_ready=True,
                deployment_role="deployment_proposal",
            ),
        )
        self.controller.set_environment(EnvironmentMode.SIMULATION)
        self.assertIsNone(self.controller.state.build.plan_hash)
        self.assertFalse(self.controller.state.build.preflight_ready)
        self.assertEqual(
            self.controller.state.environment_mode,
            EnvironmentMode.SIMULATION,
        )

    def test_enter_never_triggers_stop(self) -> None:
        self.controller.state = replace(
            self.controller.state,
            page=Page.RUN,
            runner=RunnerSnapshot(
                status=RunnerStatus.RUNNING,
                message="Recording",
            ),
        )
        self.window.render_state(self.controller.state)
        run = self.window.pages[Page.RUN]
        stops: list[bool] = []
        run.stop_requested.connect(lambda: stops.append(True))
        self.window.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        self.assertEqual(stops, [])
        self.assertTrue(run.stop_button.isVisible())
        self.assertFalse(run.primary_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
