"""Dependency-light actuator substitution for simulation and replay evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eegle.actions.commands import ActionCommand
from eegle.actions.receipts import ActionReceipt, ReceiptStatus
from eegle.streams.clocks import TimePoint

if TYPE_CHECKING:
    from eegle.plugins.contracts import ExecutionContext


class SimulatedActuator:
    """Return an observed receipt without contacting external hardware."""

    actuator_id = "eegle.simulated_actuator"

    def submit(
        self, command: ActionCommand, context: "ExecutionContext"
    ) -> ActionReceipt:
        if command.available_time.clock_id != context.current_time.clock_id:
            raise ValueError("actuator context and command availability must share a clock")
        observed = TimePoint(
            max(command.available_time.seconds, context.current_time.seconds),
            context.current_time.clock_id,
        )
        expired = bool(
            command.expires_at is not None
            and command.expires_at.clock_id == observed.clock_id
            and observed.seconds > command.expires_at.seconds
        )
        return ActionReceipt(
            receipt_id=context.next_id("receipt"),
            command_id=command.command_id,
            actuator_id=self.actuator_id,
            status=ReceiptStatus.EXPIRED if expired else ReceiptStatus.DELIVERED,
            observed_time=observed,
            delivered_time=None if expired else observed,
            details={"simulated": True},
        )
