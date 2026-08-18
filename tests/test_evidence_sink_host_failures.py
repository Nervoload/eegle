"""A failing host artifact write must not corrupt evidence sequencing.

The incremental engine sink both materializes a component-state artifact and
commits the evidence record describing it. On a host where the artifact write
fails (a path-length limit, a permission or quota error), the two must not
disagree: if the ledger commits a record whose artifact never landed, the
emitting engine and the ledger fall onto different sequence numbers and every
later record is rejected with a sequence gap that names neither the failing
component nor the real host error.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eegle.recording import (
    EvidenceRecord,
    EvidenceWriter,
    IncrementalEngineEvidenceSink,
    Session,
)
from eegle.streams import TimePoint

PLAN_HASH = "sha256:" + "e" * 64


def _record(sequence: int, record_type: str, payload: dict) -> EvidenceRecord:
    return EvidenceRecord(
        f"evidence.{sequence:08d}",
        record_type,
        sequence,
        TimePoint(float(sequence), "boundary.clock"),
        payload,
    )


def _component_state(sequence: int, component_id: str) -> EvidenceRecord:
    return _record(
        sequence,
        "component_state",
        {
            "phase_id": "phase.observe",
            "component_id": component_id,
            "component_kind": "source",
            "state": {"index": sequence},
            "state_hash": "sha256:" + "f" * 64,
        },
    )


class _FailingArtifactWriter(EvidenceWriter):
    """An EvidenceWriter whose component-state artifact write always fails."""

    def snapshot_component(self, **kwargs: object):  # type: ignore[override]
        raise OSError(22, "host rejected the artifact path")


class EvidenceSinkHostFailureTests(unittest.TestCase):
    def _writer(self, directory: str, cls=EvidenceWriter) -> EvidenceWriter:
        session = Session.create(
            Path(directory) / "session",
            session_id="session.sink.host_failure",
        )
        return cls(
            session,
            bundle_id="bundle.sink.host_failure",
            plan_hash=PLAN_HASH,
            created_time=TimePoint(0.0, "boundary.clock"),
            durable=True,
        )

    def test_a_failed_artifact_write_leaves_the_ledger_on_the_same_sequence(
        self,
    ) -> None:
        """The engine can then record the failure at the sequence it still holds."""

        with tempfile.TemporaryDirectory() as directory:
            writer = self._writer(directory, _FailingArtifactWriter)
            sink = IncrementalEngineEvidenceSink(writer)
            sink(_record(0, "phase_started", {"phase_id": "phase.observe"}))

            with self.assertRaises(OSError):
                sink(_component_state(1, "source.eeg"))

            # The rejected record was not committed, so sequence 1 is still the
            # next expected sequence rather than a permanent gap.
            self.assertEqual(writer.record_count, 1)
            self.assertEqual(writer.last_sequence, 0)

            sink(
                _record(
                    1,
                    "component_state_unavailable",
                    {
                        "phase_id": "phase.observe",
                        "component_id": "source.eeg",
                        "failure": "OSError: host rejected the artifact path",
                    },
                )
            )
            self.assertEqual(writer.last_sequence, 1)

            # The run continues and stays contiguous afterwards.
            sink(
                _record(
                    2,
                    "phase_finished",
                    {"phase_id": "phase.observe", "status": "complete"},
                )
            )
            self.assertEqual(writer.last_sequence, 2)

    def test_a_successful_state_record_still_registers_its_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            writer = self._writer(directory)
            sink = IncrementalEngineEvidenceSink(writer)
            sink(
                _record(
                    0,
                    "component_started",
                    {
                        "phase_id": "phase.observe",
                        "component_id": "source.eeg",
                        "component_version": "0.1.0",
                    },
                )
            )
            sink(_component_state(1, "source.eeg"))
            self.assertEqual(writer.last_sequence, 1)
            roles = {entry.reference.role for entry in writer.session.artifacts.entries}
            self.assertIn("component_state", roles)
            versions = {
                entry.lineage.component_version
                for entry in writer.session.artifacts.entries
                if entry.reference.role == "component_state"
            }
            # The version recorded for the state comes from component_started.
            self.assertEqual(versions, {"0.1.0"})


if __name__ == "__main__":
    unittest.main()
