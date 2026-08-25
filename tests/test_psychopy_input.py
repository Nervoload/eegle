from __future__ import annotations

import unittest

from eegle.psychopy_input import (
    create_hardware_keyboard,
    poll_hardware_keyboard,
    poll_psychopy_keys,
    stop_hardware_keyboard,
)
from eegle.pipelines.dsart_recording import _baseline_instruction
from eegle.tasks.dynamic_sart import PersistentKeyboardCollector


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def reset(self) -> None:
        self.value = 0.0

    def getTime(self) -> float:
        return self.value


class _EventModule:
    def __init__(self, events: list) -> None:
        self.events = list(events)
        self.polls = 0
        self.cleared = 0

    def getKeys(self, *, timeStamped: object = False) -> list:
        self.polls += 1
        events = self.events
        self.events = []
        return events

    def clearEvents(self, *, eventType: str) -> None:
        self.cleared += 1
        self.events = []


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.checkpoints = 0

    def append_key_event(self, row: dict) -> None:
        self.rows.append(row)

    def checkpoint(self) -> None:
        self.checkpoints += 1


class _MalformedKeyPress:
    name = ["SPACE", 0.5]
    rt = None


class _HardwareKeyboard:
    def __init__(self, *, backend: str) -> None:
        self.backend = backend
        self.clock = _Clock()
        self.events: list[object] = []
        self.cleared = 0
        self.stopped = 0

    def clearEvents(self) -> None:
        self.cleared += 1

    def getKeys(self, *, keyList: object, waitRelease: bool, clear: bool) -> list[object]:
        self.asserted_call = (keyList, waitRelease, clear)
        values = self.events
        self.events = []
        return values

    def stop(self) -> None:
        self.stopped += 1


class _Window:
    def __init__(self) -> None:
        self.flips = 0

    def flip(self) -> None:
        self.flips += 1


class _Visual:
    class TextStim:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.drawn = False

        def draw(self) -> None:
            self.drawn = True


class PsychoPyInputTests(unittest.TestCase):
    def test_event_reader_reads_every_key_and_preserves_timestamps(self) -> None:
        event = _EventModule([["space", 0.125], ["escape", 0.25]])

        keys = poll_psychopy_keys(event, clock=_Clock())

        self.assertEqual([(key.name, key.rt) for key in keys], [("space", 0.125), ("escape", 0.25)])
        self.assertEqual(event.polls, 1)

    def test_nested_event_names_are_normalized(self) -> None:
        event = _EventModule([_MalformedKeyPress()])
        keys = poll_psychopy_keys(event)
        self.assertEqual([(key.name, key.rt) for key in keys], [("space", 0.5)])

    def test_dsart_collector_recognizes_space_and_escape(self) -> None:
        event = _EventModule([])
        store = _Store()
        collector = PersistentKeyboardCollector(
            event,
            _Clock(),
            store,
            response_keys=["space"],
            escape_keys=["escape", "q"],
        )
        event.events = [["SPACE", 0.1], ["Escape", 0.2]]

        rows = collector.poll(task_state="STIMULUS_VISIBLE", assigned_trial=1)

        self.assertEqual([row["key"] for row in rows], ["space", "escape"])
        self.assertTrue(rows[0]["is_response_key"])
        self.assertTrue(rows[1]["is_escape_key"])
        self.assertEqual([row["keyboard_time"] for row in rows], [0.1, 0.2])
        self.assertEqual(store.rows, rows)
        self.assertEqual(store.checkpoints, 0)

    def test_dsart_collector_checkpoints_keys_on_static_instruction_screen(self) -> None:
        event = _EventModule([])
        store = _Store()
        collector = PersistentKeyboardCollector(
            event,
            _Clock(),
            store,
            response_keys=["space"],
            escape_keys=["escape", "q"],
        )
        event.events = [["SPACE", 0.1]]

        rows = collector.poll(task_state="INSTRUCTIONS")

        self.assertEqual([row["key"] for row in rows], ["space"])
        self.assertEqual(store.checkpoints, 1)

    def test_hardware_keyboard_uses_ptb_queue_and_keydown_timestamps(self) -> None:
        module = type("KeyboardModule", (), {"Keyboard": _HardwareKeyboard})
        keyboard = create_hardware_keyboard(module, backend="ptb")
        keyboard.events = [type("KeyPress", (), {"name": "SPACE", "rt": 0.125})()]

        keys = poll_hardware_keyboard(keyboard)

        self.assertEqual(keyboard.backend, "ptb")
        self.assertEqual(keyboard.cleared, 1)
        self.assertEqual(keyboard.asserted_call, (None, False, True))
        self.assertEqual([(key.name, key.rt) for key in keys], [("space", 0.125)])

        stop_hardware_keyboard(keyboard)
        self.assertEqual(keyboard.stopped, 1)

    def test_hardware_keyboard_cleanup_requires_public_stop_contract(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "did not expose stop"):
            stop_hardware_keyboard(object())

    def test_baseline_instruction_accepts_space_and_aborts_on_escape(self) -> None:
        self.assertTrue(_baseline_instruction(_Window(), _Visual, _EventModule(["space"]), "Continue"))
        self.assertFalse(_baseline_instruction(_Window(), _Visual, _EventModule(["escape"]), "Abort"))


if __name__ == "__main__":
    unittest.main()
