"""Small structural protocols for independently replaceable components."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from eegle._domain import ExecutionMode
from eegle.actions.authorization import (
    AuthorizationDecision,
    AuthorizationRequest,
    AuthorizationResult,
)
from eegle.actions.commands import ActionRequest, AuthorizedCommand
from eegle.actions.receipts import ActionReceipt
from eegle.models.predictions import Prediction
from eegle.models.results import ModelResult
from eegle.recording.publications import ArtifactPublication
from eegle.processing.quality import QualityDecision
from eegle.processing.windows import Window
from eegle.runtime.outcomes import Outcome
from eegle.runtime.scheduling import ScheduledTrigger, TriggerResult
from eegle.runtime.state import AdaptationResult, StateTransition
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import Packet
from eegle.streams.sources import Source


class ExecutionContext(Protocol):
    execution_id: str
    component_id: str
    component_version: str
    execution_mode: ExecutionMode
    current_time: TimePoint
    clock_mapping_revisions: Mapping[str, int]

    def next_id(self, namespace: str) -> str:
        ...


@runtime_checkable
class GraphNode(Protocol):
    """Explicit multi-port escape hatch for domain components.

    Specialized one-input protocols remain the simplest component API. Nodes
    with joins or multiple outputs return a mapping from declared output port
    names to one value or an iterable of values.
    """

    def process(
        self,
        input_port: str,
        value: Any,
        context: ExecutionContext,
    ) -> Mapping[str, Any]:
        ...


@runtime_checkable
class Transform(Protocol):
    def update(self, packet: Packet, context: ExecutionContext) -> Packet | None:
        ...


@runtime_checkable
class WindowBuilder(Protocol):
    def update(self, packet: Packet, context: ExecutionContext) -> Iterable[Window]:
        ...


@runtime_checkable
class QualityGate(Protocol):
    def evaluate(self, item: Any, context: ExecutionContext) -> QualityDecision:
        ...


@runtime_checkable
class Model(Protocol):
    def predict(self, item: Any, context: ExecutionContext) -> ModelResult:
        ...


@runtime_checkable
class AdaptiveModel(Model, Protocol):
    """State-owning model whose updates are coordinated and evidenced by EEGle."""

    def adapt(
        self,
        prediction: Prediction,
        outcome: Outcome,
        context: ExecutionContext,
    ) -> AdaptationResult:
        ...


@runtime_checkable
class OutcomeResolver(Protocol):
    def update(self, packet: Packet, context: ExecutionContext) -> Iterable[Outcome]:
        ...


@runtime_checkable
class Adapter(Protocol):
    def update(
        self,
        outcome: Outcome,
        state: Mapping[str, Any],
        context: ExecutionContext,
    ) -> StateTransition | None:
        ...


@runtime_checkable
class Policy(Protocol):
    def decide(
        self,
        prediction: Prediction,
        state: Mapping[str, Any],
        context: ExecutionContext,
    ) -> ActionRequest | None:
        ...


@runtime_checkable
class AuthorizationProvider(Protocol):
    def authorize(
        self,
        request: AuthorizationRequest,
        context: ExecutionContext,
    ) -> AuthorizationResult:
        ...

    def resolve(
        self,
        request: AuthorizationRequest,
        pending: AuthorizationDecision,
        context: ExecutionContext,
    ) -> AuthorizationResult:
        ...


@runtime_checkable
class Actuator(Protocol):
    def submit(self, command: AuthorizedCommand, context: ExecutionContext) -> ActionReceipt:
        ...


@runtime_checkable
class ArtifactProducer(Protocol):
    """Turn a graph value into a versioned, content-addressed artifact."""

    def produce(self, value: Any, context: ExecutionContext) -> ArtifactPublication:
        ...


@runtime_checkable
class TriggerHandler(Protocol):
    def handle_trigger(
        self,
        trigger: ScheduledTrigger,
        context: ExecutionContext,
    ) -> TriggerResult:
        ...


@runtime_checkable
class Sink(Protocol):
    def append(self, record: Any) -> None:
        ...


@runtime_checkable
class StatefulComponent(Protocol):
    def snapshot_state(self) -> Mapping[str, Any]:
        ...

    def restore_state(self, state: Mapping[str, Any]) -> None:
        ...


@runtime_checkable
class LifecycleComponent(Protocol):
    def start(self, context: ExecutionContext) -> None:
        ...

    def stop(self, context: ExecutionContext) -> None:
        ...
