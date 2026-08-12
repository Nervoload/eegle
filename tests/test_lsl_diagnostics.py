from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eegle.lsl_diagnostics import (
    LOOPBACK_TYPE,
    _discover_streams,
    _lsl_config_snapshot,
    diagnostic_verdict,
)


class _Info:
    def __init__(self, *, name: str, stream_type: str, source_id: str) -> None:
        self._name = name
        self._stream_type = stream_type
        self._source_id = source_id

    def name(self) -> str:
        return self._name

    def type(self) -> str:
        return self._stream_type

    def source_id(self) -> str:
        return self._source_id


class _Pylsl:
    def resolve_streams(self, *, wait_time: float) -> list[_Info]:
        return [
            _Info(
                name="EEGle-LSL-Diagnostic",
                stream_type=LOOPBACK_TYPE,
                source_id="eegle-lsl-diagnostic-stale",
            ),
            _Info(name="Collect", stream_type="EEG", source_id="neuracle-1"),
        ]


class LslDiagnosticTests(unittest.TestCase):
    def test_failed_loopback_is_local_stack_failure(self) -> None:
        verdict = diagnostic_verdict({"status": "fail"}, [])
        self.assertEqual(verdict["status"], "fail")
        self.assertEqual(verdict["code"], "local_lsl_interprocess_failure")

    def test_working_loopback_with_no_streams_points_to_missing_external_outlet(self) -> None:
        verdict = diagnostic_verdict({"status": "pass"}, [])
        self.assertEqual(verdict["status"], "warning")
        self.assertEqual(verdict["code"], "local_lsl_ok_no_external_streams")

    def test_visible_eeg_stream_passes(self) -> None:
        verdict = diagnostic_verdict(
            {"status": "pass"},
            [{"name": "Collect", "type": "EEG"}],
        )
        self.assertEqual(verdict["status"], "pass")
        self.assertEqual(verdict["code"], "external_eeg_stream_visible")

    def test_discovery_excludes_the_short_lived_diagnostic_outlet(self) -> None:
        streams = _discover_streams(_Pylsl(), wait_seconds=0.25, rounds=1)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["name"], "Collect")

    def test_config_snapshot_exposes_discovery_isolation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lsl_api.cfg"
            path.write_text(
                "[ports]\nMulticastPort=16571\nBasePort=16572\nPortRange=32\n"
                "[multicast]\nResolveScope=machine\n"
                "[lab]\nSessionID=isolated\nKnownPeers={127.0.0.1}\n",
                encoding="utf-8",
            )
            snapshot = _lsl_config_snapshot(str(path))

        self.assertTrue(snapshot["exists"])
        self.assertEqual(snapshot["ports"]["multicast_port"], "16571")
        self.assertEqual(snapshot["multicast"]["resolve_scope"], "machine")
        self.assertEqual(snapshot["lab"]["session_id"], "isolated")


if __name__ == "__main__":
    unittest.main()
