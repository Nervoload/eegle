"""Reusable behavioral assertions for executable EEGle plugin contracts."""

from __future__ import annotations

from eegle.plugins.contracts import ExecutionContext
from eegle.streams.packets import DenseSampleBatch


def exercise_dense_transform(
    component: object,
    packet: DenseSampleBatch,
    context: ExecutionContext,
) -> DenseSampleBatch:
    """Exercise the common dense-transform contract or raise ``AssertionError``."""

    update = getattr(component, "update", None)
    if not callable(update):
        raise AssertionError("transform does not expose callable update")
    output = update(packet, context)
    if not isinstance(output, DenseSampleBatch):
        raise AssertionError("transform did not return DenseSampleBatch")
    if output.available_time != context.current_time:
        raise AssertionError("transform output availability does not match execution time")
    if output.lineage is None:
        raise AssertionError("transform output is missing lineage")
    if output.lineage.component_id != context.component_id:
        raise AssertionError("transform lineage has the wrong component identity")
    if output.lineage.input_ids != (packet.batch_id,):
        raise AssertionError("transform lineage does not identify the admitted input")
    if output.lineage.latest_input_available_time != packet.available_time:
        raise AssertionError("transform lineage lost the input availability frontier")
    if output.lineage.stream_revisions.get(packet.stream_id) != packet.stream_revision:
        raise AssertionError("transform lineage lost the input stream revision")
    return output

