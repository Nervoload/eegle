"""Information-flow rules for causal model inference inputs."""

from __future__ import annotations

from eegle._domain import ExecutionMode

_CAUSAL_LABEL_BEARING_TYPES = frozenset(
    {
        "eegle.metadata_event.v1",
        "eegle.sparse_event_batch.v1",
    }
)


def causal_model_input_rejection(
    execution_mode: ExecutionMode,
    type_id: str,
    *,
    model_input_safety: str | None = None,
) -> str | None:
    """Return a stable reason when a typed input can expose labels causally."""

    if ExecutionMode(execution_mode) != ExecutionMode.CAUSAL:
        return None
    normalized = str(type_id)
    if model_input_safety == "label_bearing":
        return f"causal model input type {normalized} is declared label-bearing"
    if normalized.startswith("eegle.outcome"):
        return (
            f"causal model input type {normalized} is label-bearing; route it only "
            "through a declared non-causal research protocol"
        )
    if (
        normalized in _CAUSAL_LABEL_BEARING_TYPES
        and model_input_safety != "label_blind"
    ):
        return (
            f"causal model input type {normalized} requires an explicit label_blind "
            "source contract; route label-bearing content only through a declared "
            "non-causal research protocol"
        )
    return None
