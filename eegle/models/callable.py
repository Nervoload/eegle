"""Dependency-light adapter for plain Python model callables."""

from __future__ import annotations

from typing import Any, Callable

from eegle.models.results import ModelResult


class CallableModel:
    """Expose a plain ``callable(item)`` through the model plugin contract.

    Configuration remains the responsibility of the surrounding plugin
    factory.  EEGle specifications never contain arbitrary Python import
    strings or serialized callables.
    """

    def __init__(
        self,
        function: Callable[[Any], Any],
        *,
        completion_delay_seconds: float = 0.0,
    ) -> None:
        if not callable(function):
            raise TypeError("CallableModel requires a callable")
        self._function = function
        self._completion_delay_seconds = float(completion_delay_seconds)
        # Reuse ModelResult's finite/non-negative validation at construction.
        ModelResult(None, completion_delay_seconds=self._completion_delay_seconds)

    def predict(self, item: Any, context: Any) -> ModelResult:
        value = self._function(item)
        if isinstance(value, ModelResult):
            if self._completion_delay_seconds != 0.0:
                raise ValueError(
                    "CallableModel completion delay must be declared either by the "
                    "adapter or the returned ModelResult, not both"
                )
            return value
        return ModelResult(
            value,
            completion_delay_seconds=self._completion_delay_seconds,
        )
