from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from eegle.psychopy_audio import (
    play_psychopy_end_signal,
    prepare_psychopy_audio_output,
    probe_psychopy_audio_output,
)


def _config() -> dict:
    return {
        "hardware": {
            "audio": {
                "output_enabled": True,
                "preferred_output_name_patterns": ["speakers", "realtek", "built-in"],
                "avoid_output_name_patterns": ["nvidia high definition audio", "hdmi"],
                "latency_class": 0,
                "end_signal_frequency_hz": 880.0,
                "end_signal_seconds": 0.5,
            }
        }
    }


class PsychoPyAudioTests(unittest.TestCase):
    def test_builtin_speakers_are_preferred_over_external_display_audio(self) -> None:
        class Speaker:
            devices = [
                {"deviceName": "C34H89x (NVIDIA High Definition Audio)", "index": 3},
                {"deviceName": "Speakers (Realtek(R) Audio)", "index": 4},
            ]

            @classmethod
            def getAvailableDevices(cls):
                return cls.devices

            def __init__(self, *, index, latencyClass, resample):
                self.index = index
                self.name = next(row["deviceName"] for row in self.devices if row["index"] == index)
                self.channels = 2
                self.close = MagicMock()

        output, report = prepare_psychopy_audio_output(_config(), speaker_class=Speaker)

        self.assertIsNotNone(output)
        assert output is not None
        self.assertEqual(output.device_name, "Speakers (Realtek(R) Audio)")
        self.assertEqual(report["status"], "ok")
        self.assertIn("preferred name pattern", report["selection_reason"])

    def test_failed_preferred_speaker_falls_back_to_next_usable_output(self) -> None:
        class Speaker:
            devices = [
                {"deviceName": "Speakers (Realtek(R) Audio)", "index": 4},
                {"deviceName": "USB Audio Output", "index": 5},
            ]

            @classmethod
            def getAvailableDevices(cls):
                return cls.devices

            def __init__(self, *, index, latencyClass, resample):
                if index == 4:
                    raise OSError("busy")
                self.index = index
                self.name = "USB Audio Output"
                self.channels = 2

            def close(self):
                return None

        output, report = prepare_psychopy_audio_output(_config(), speaker_class=Speaker)

        self.assertIsNotNone(output)
        assert output is not None
        self.assertEqual(output.device_name, "USB Audio Output")
        self.assertEqual([row["status"] for row in report["attempts"]], ["failed", "opened"])

    def test_only_hdmi_output_is_warning_not_failure(self) -> None:
        class Speaker:
            @staticmethod
            def getAvailableDevices():
                return [{"deviceName": "External HDMI (NVIDIA High Definition Audio)", "index": 2}]

            def __init__(self, *, index, latencyClass, resample):
                self.index = index
                self.name = "External HDMI (NVIDIA High Definition Audio)"
                self.channels = 2

            def close(self):
                return None

        output, report = prepare_psychopy_audio_output(_config(), speaker_class=Speaker)

        self.assertIsNotNone(output)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["failure_policy"], "warn")

    def test_every_output_open_failure_is_warning_not_exception(self) -> None:
        class Speaker:
            @staticmethod
            def getAvailableDevices():
                return [
                    {"deviceName": "Speakers (Realtek(R) Audio)", "index": 4},
                    {"deviceName": "USB Audio Output", "index": 5},
                ]

            def __init__(self, *, index, latencyClass, resample):
                raise OSError(f"device {index} is unavailable")

        output, report = prepare_psychopy_audio_output(_config(), speaker_class=Speaker)

        self.assertIsNone(output)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(len(report["attempts"]), 2)
        self.assertIn("no enumerated speaker could be opened", report["detail"])

    def test_missing_speaker_is_warning_not_exception(self) -> None:
        class Speaker:
            @staticmethod
            def getAvailableDevices():
                return []

        output, report = prepare_psychopy_audio_output(_config(), speaker_class=Speaker)

        self.assertIsNone(output)
        self.assertEqual(report["status"], "warn")
        self.assertIn("no audio playback devices", report["detail"])

    def test_probe_closes_opened_speaker(self) -> None:
        speaker = SimpleNamespace(name="Speakers", index=1, close=MagicMock())
        output = SimpleNamespace(close=speaker.close)
        # Exercise the public probe's cleanup contract by swapping its preparer
        # through the module global rather than importing PsychoPy in the test.
        import eegle.psychopy_audio as audio_module

        original = audio_module.prepare_psychopy_audio_output
        try:
            audio_module.prepare_psychopy_audio_output = lambda _config: (
                output,
                {"status": "ok", "detail": "ready"},
            )
            report = probe_psychopy_audio_output(_config())
        finally:
            audio_module.prepare_psychopy_audio_output = original

        self.assertEqual(report["status"], "ok")
        speaker.close.assert_called_once_with()

    def test_playback_failure_is_warning_not_exception(self) -> None:
        speaker = SimpleNamespace(name="Speakers", index=1, channels=2)
        output = SimpleNamespace(speaker=speaker, device_name="Speakers", device_index=1)
        sound_module = SimpleNamespace(Sound=MagicMock(side_effect=OSError("unavailable")))

        report = play_psychopy_end_signal(
            output,
            _config(),
            sound_module=sound_module,
            sleep_fn=lambda _seconds: None,
        )

        self.assertEqual(report["status"], "warn")
        self.assertIn("OSError: unavailable", report["detail"])


if __name__ == "__main__":
    unittest.main()
