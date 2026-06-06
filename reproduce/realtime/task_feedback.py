"""Nonblocking task-side feedback client for deterministic action boundaries."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from reproduce.realtime.policy import TaskAction


@dataclass
class FeedbackAuditRecord:
    status: str
    reason: str
    boundary: str
    trial_index: int
    action: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = 1
        payload["created_at_monotonic"] = monotonic()
        return payload


class TaskFeedbackClient:
    """Poll LSL or JSONL feedback without blocking PsychoPy rendering."""

    def __init__(self, config: dict[str, Any] | None = None, default_jsonl_path: str | Path | None = None) -> None:
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", False))
        self.backend = str(self.config.get("backend", "disabled")).lower()
        self.lsl_stream_name = str(self.config.get("lsl_stream_name", "EEGleFeedback"))
        self.lsl_stream_type = str(self.config.get("lsl_stream_type", "Feedback"))
        self.jsonl_path = Path(self.config.get("jsonl_path") or default_jsonl_path or "")
        self.max_future_trials = int(self.config.get("max_future_trials", 5))
        self._pending: list[TaskAction] = []
        self._seen: set[str] = set()
        self._jsonl_offset = 0
        self._lsl_inlet: Any | None = None
        self._next_lsl_resolve_at = 0.0
        self.audit_records: list[FeedbackAuditRecord] = []

    def poll(self, boundary: str, trial_index: int, block_index: int | None = None) -> list[TaskAction]:
        self.audit_records = []
        if not self.enabled or self.backend in {"disabled", "none"}:
            return []
        self._pull_new_actions(boundary, trial_index)
        accepted: list[TaskAction] = []
        remaining: list[TaskAction] = []
        for action in self._pending:
            status, reason = self._action_status(action, boundary, trial_index, block_index)
            if status == "accepted":
                accepted.append(action)
                self._seen.add(action.action_id)
            elif status == "pending":
                remaining.append(action)
            else:
                self._seen.add(action.action_id)
            self.audit_records.append(
                FeedbackAuditRecord(
                    status=status,
                    reason=reason,
                    boundary=boundary,
                    trial_index=trial_index,
                    action=action.to_payload(),
                )
            )
        self._pending = remaining
        return accepted

    def _pull_new_actions(self, boundary: str, trial_index: int) -> None:
        if self.backend in {"lsl", "auto"}:
            self._pull_lsl(boundary, trial_index)
        if self.backend in {"jsonl", "auto"}:
            self._pull_jsonl(boundary, trial_index)

    def _pull_jsonl(self, boundary: str, trial_index: int) -> None:
        if not self.jsonl_path or not self.jsonl_path.exists():
            return
        with self.jsonl_path.open("r", encoding="utf-8") as handle:
            handle.seek(self._jsonl_offset)
            for line in handle:
                if not line.strip():
                    continue
                self._ingest_payload_line(line, boundary, trial_index)
            self._jsonl_offset = handle.tell()

    def _pull_lsl(self, boundary: str, trial_index: int) -> None:
        try:
            import pylsl
        except Exception:
            return
        now = monotonic()
        if self._lsl_inlet is None and now >= self._next_lsl_resolve_at:
            self._next_lsl_resolve_at = now + 1.0
            try:
                infos = pylsl.resolve_streams(wait_time=0.0)
            except Exception:
                return
            for info in infos:
                if info.name() == self.lsl_stream_name or info.type() == self.lsl_stream_type:
                    try:
                        self._lsl_inlet = pylsl.StreamInlet(info, max_buflen=60, max_chunklen=16, recover=True)
                        self._lsl_inlet.open_stream(timeout=0.0)
                    except Exception:
                        self._lsl_inlet = None
                    break
        if self._lsl_inlet is None:
            return
        samples, _timestamps = self._lsl_inlet.pull_chunk(timeout=0.0, max_samples=32)
        for sample in samples:
            line = sample[0] if isinstance(sample, list) else sample
            self._ingest_payload_line(str(line), boundary, trial_index)

    def _ingest_payload_line(self, line: str, boundary: str, trial_index: int) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self.audit_records.append(FeedbackAuditRecord("rejected", "malformed json", boundary, trial_index, payload={"line": line}))
            return
        actions = payload.get("actions")
        if not isinstance(actions, list):
            actions = [payload] if "action" in payload else []
        if not actions:
            self.audit_records.append(FeedbackAuditRecord("rejected", "payload has no actions", boundary, trial_index, payload=payload))
            return
        for action_payload in actions:
            try:
                action = TaskAction.from_payload(action_payload)
            except (KeyError, TypeError, ValueError) as exc:
                self.audit_records.append(
                    FeedbackAuditRecord("rejected", f"invalid action: {type(exc).__name__}", boundary, trial_index, payload=action_payload)
                )
                continue
            if action.action_id in self._seen:
                continue
            self._pending.append(action)
            self.audit_records.append(
                FeedbackAuditRecord("received", "queued action", boundary, trial_index, action=action.to_payload())
            )

    def _action_status(
        self,
        action: TaskAction,
        boundary: str,
        trial_index: int,
        block_index: int | None,
    ) -> tuple[str, str]:
        if action.action_id in self._seen:
            return "rejected", "duplicate action"
        if action.boundary != boundary:
            return "pending", f"waiting for boundary {action.boundary}"
        if action.block_index is not None and block_index is not None and action.block_index != block_index:
            return "pending", f"waiting for block {action.block_index}"
        if action.target_trial_index is not None:
            if action.target_trial_index < trial_index:
                return "rejected", "stale target trial"
            if action.target_trial_index > trial_index + self.max_future_trials:
                return "rejected", "target trial too far in future"
            if action.target_trial_index > trial_index:
                return "pending", f"waiting for trial {action.target_trial_index}"
        if action.action not in {
            "increase_no_go_probability",
            "adjust_isi",
            "change_stimulus_set",
            "repeat_condition",
            "show_reward",
            "set_visual_alpha",
            "observe_only",
        }:
            return "rejected", f"unsupported action {action.action}"
        return "accepted", "boundary and target matched"
