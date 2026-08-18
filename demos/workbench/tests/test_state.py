from __future__ import annotations

import unittest

from demos.workbench.state import (
    EnvironmentMode,
    Lifecycle,
    Page,
    WorkbenchState,
)


class WorkbenchStateTests(unittest.TestCase):
    def test_workflow_progression_is_canonical(self) -> None:
        state = WorkbenchState(
            page=Page.DESIGN,
            lifecycle=Lifecycle.APPARATUS_UNBOUND,
            environment_mode=EnvironmentMode.LIVE_LSL,
        )
        expected = (Page.APPARATUS, Page.BUILD, Page.RUN, Page.SESSIONS, None)
        for target in expected:
            self.assertEqual(state.next_workflow_page(), target)
            if target is not None:
                state = state.navigate(target)

    def test_general_pages_do_not_advance_scientific_workflow(self) -> None:
        self.assertIsNone(WorkbenchState(page=Page.EXPERIMENTS).next_workflow_page())
        self.assertIsNone(WorkbenchState(page=Page.REPLAY).next_workflow_page())

    def test_unbound_project_is_setup_required(self) -> None:
        state = WorkbenchState(lifecycle=Lifecycle.APPARATUS_UNBOUND)
        self.assertEqual(state.highest_status_label, "SETUP REQUIRED")
        self.assertTrue(state.page_needs_attention(Page.APPARATUS))
        self.assertFalse(state.page_completed(Page.APPARATUS))


if __name__ == "__main__":
    unittest.main()
