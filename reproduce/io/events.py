"""Behavior and trigger event logging."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any


@dataclass
class EventRecord:
    label: str
    timestamp: float
    event_type: str = "EVENT"
    trial: int | None = None
    value: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class EventLogger:
    """Writes behavior CSV, JSONL events, and BciPy-style triggers.txt."""

    def __init__(
        self,
        behavior_csv: Path,
        events_jsonl: Path,
        triggers_txt: Path,
        telemetry: Any | None = None,
        component: str = "task",
    ) -> None:
        self.behavior_csv = Path(behavior_csv)
        self.events_jsonl = Path(events_jsonl)
        self.triggers_txt = Path(triggers_txt)
        self.telemetry = telemetry
        self.component = component
        self.behavior_csv.parent.mkdir(parents=True, exist_ok=True)
        self.events_jsonl.parent.mkdir(parents=True, exist_ok=True)
        self.triggers_txt.parent.mkdir(parents=True, exist_ok=True)
        self._csv = self.behavior_csv.open("w", encoding="utf-8", newline="")
        self._jsonl = self.events_jsonl.open("w", encoding="utf-8")
        self._triggers = self.triggers_txt.open("w", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._csv,
            fieldnames=["trial", "label", "event_type", "timestamp", "value", "metadata"],
        )
        self._writer.writeheader()

    def mark(
        self,
        label: str,
        event_type: str = "EVENT",
        timestamp: float | None = None,
        trial: int | None = None,
        value: str | None = None,
        **metadata: Any,
    ) -> EventRecord:
        record = EventRecord(
            label=label,
            event_type=event_type,
            timestamp=monotonic() if timestamp is None else timestamp,
            trial=trial,
            value=value,
            metadata=metadata,
        )
        self._write(record)
        return record

    def _write(self, record: EventRecord) -> None:
        row = asdict(record)
        row["metadata"] = json.dumps(record.metadata, sort_keys=True)
        self._writer.writerow(row)
        self._csv.flush()
        self._jsonl.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        self._jsonl.flush()
        self._triggers.write(f"{record.label} {record.event_type} {record.timestamp:.9f}\n")
        self._triggers.flush()
        self._emit_telemetry(record)

    def _emit_telemetry(self, record: EventRecord) -> None:
        if self.telemetry is None:
            return
        level = _telemetry_level(record)
        event = _telemetry_event(record)
        message = _telemetry_message(record)
        payload = asdict(record)
        self.telemetry.emit(
            event,
            component=self.component,
            level=level,
            message=message,
            metadata=payload,
        )

    def close(self) -> None:
        self._csv.close()
        self._jsonl.close()
        self._triggers.close()

    def __enter__(self) -> "EventLogger":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _telemetry_level(record: EventRecord) -> str:
    label = record.label
    if record.event_type == "SYSTEM":
        return "default"
    if "stimulus_onset" in label or "stimulus_offset" in label or label in {"target_onset", "fixation_onset"}:
        return "realtime"
    if label in {"button_press", "response", "miss", "premature_response", "too_fast_response"}:
        return "realtime"
    return "debug"


def _telemetry_event(record: EventRecord) -> str:
    label = record.label
    if "stimulus_onset" in label or label == "target_onset":
        return "task.stimulus_onset"
    if "stimulus_offset" in label:
        return "task.stimulus_offset"
    if label in {"button_press", "response", "premature_response", "too_fast_response"}:
        return "task.response"
    if label == "miss":
        return "task.miss"
    if label == "task_start":
        return "task.start"
    if label == "task_end":
        return "task.end"
    if "abort" in label:
        return "task.abort"
    return f"task.{label}"


def _telemetry_message(record: EventRecord) -> str:
    if "stimulus_onset" in record.label or record.label == "target_onset":
        trial = "" if record.trial is None else f" trial {record.trial}"
        stimulus = _stimulus_description(record.metadata)
        return f"Stimulus onset{trial}{stimulus}"
    if "stimulus_offset" in record.label:
        trial = "" if record.trial is None else f" trial {record.trial}"
        return f"Stimulus offset{trial}"
    if record.label in {"button_press", "response"}:
        key = "" if record.value is None else f" {record.value}"
        trial = "" if record.trial is None else f" trial {record.trial}"
        return f"Response{key}{trial}"
    if record.label == "task_start":
        return "Task started"
    if record.label == "task_end":
        return "Task ended"
    if "abort" in record.label:
        return f"Task abort: {record.label}"
    return record.label


def _stimulus_description(metadata: dict[str, Any]) -> str:
    shape = metadata.get("shape")
    color = metadata.get("color")
    condition = "no-go" if metadata.get("is_no_go") else "go" if "is_no_go" in metadata else None
    parts = [str(part) for part in (condition, color, shape) if part]
    return "" if not parts else f" ({', '.join(parts)})"
