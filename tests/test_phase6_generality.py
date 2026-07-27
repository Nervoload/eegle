from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any, Mapping
import unittest

import numpy as np

from eegle._domain import (
    ComponentKind,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
)
from eegle.compiler import CompilationError, compile_suite
from eegle.models import (
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
    ModelResult,
    ModelStateContract,
)
from eegle.plugins import (
    PluginCapabilities,
    PluginDescriptor,
    PluginRegistry,
    PortSpec,
    StateBehavior,
)
from eegle.recording import EvidenceReader, Session, persist_engine_run
from eegle.replay import BundleReplayRunner
from eegle.runtime import EngineStatus, ExecutionEngine, GraphInput
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import (
    ChannelSpec,
    ContentKind,
    DenseSampleBatch,
    RateModel,
    SparseEvent,
    SparseEventBatch,
    StreamSpec,
    TimePoint,
)


PREDICTION_SCHEMA = "eegle.prediction.v2"
DENSE_SCHEMA = "eegle.dense_sample_batch.v1"
SPARSE_SCHEMA = "eegle.sparse_event_batch.v1"
BOUNDARY_CLOCK = "boundary.clock"
EMPTY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
}
SUMMARY_VALUE_SCHEMA = {
    "type": "object",
    "required": ["input_port", "record_kind", "observation_count", "summary"],
    "properties": {
        "input_port": {"type": "string"},
        "record_kind": {"type": "string", "enum": ["dense", "sparse"]},
        "observation_count": {"type": "integer", "minimum": 1},
        "summary": {"type": "number"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class GeneralityCase:
    case_id: str
    streams: tuple[StreamSpec, ...]
    packets: Mapping[str, tuple[DenseSampleBatch | SparseEventBatch, ...]]
    contracts: Mapping[str, Mapping[str, Any]]


class PacketSummaryModel:
    """Dependency-light fixture proving packet shape is not an engine branch."""

    def process(self, input_port: str, value: Any, context: Any) -> Mapping[str, Any]:
        if isinstance(value, DenseSampleBatch):
            mask = np.isfinite(value.values)
            if value.validity_mask is not None:
                mask &= value.validity_mask
            if not bool(np.any(mask)):
                raise ValueError("dense fixture requires one valid observation")
            result = {
                "input_port": input_port,
                "record_kind": "dense",
                "observation_count": value.sample_count,
                "summary": float(np.mean(value.values[mask])),
            }
        elif isinstance(value, SparseEventBatch):
            result = {
                "input_port": input_port,
                "record_kind": "sparse",
                "observation_count": len(value.events),
                "summary": float(len({event.kind for event in value.events})),
            }
        else:
            raise TypeError(f"unsupported fixture input {type(value).__name__}")
        return {"prediction": ModelResult(result)}


class EstimatorFixture:
    """Minimal sklearn-shaped test double; no sklearn import belongs in EEGle."""

    def predict_proba(self, rows: np.ndarray) -> np.ndarray:
        scores = 1.0 / (1.0 + np.exp(-np.mean(rows, axis=1)))
        return np.column_stack((1.0 - scores, scores))


class TensorModuleFixture:
    """Minimal Torch-shaped callable; tensor conversion stays adapter-owned."""

    def __call__(self, rows: np.ndarray) -> np.ndarray:
        means = np.mean(rows, axis=1)
        return np.column_stack((-means, means))


class EstimatorAdapterFixture:
    def __init__(self, estimator: EstimatorFixture) -> None:
        self.estimator = estimator

    def predict(self, value: DenseSampleBatch, context: Any) -> ModelResult:
        rows = np.asarray(value.values, dtype=np.float64).reshape(1, -1)
        probability = float(self.estimator.predict_proba(rows)[0, 1])
        return ModelResult({"backend": "estimator", "score": probability})


class TensorAdapterFixture:
    def __init__(self, module: TensorModuleFixture) -> None:
        self.module = module

    def predict(self, value: DenseSampleBatch, context: Any) -> ModelResult:
        rows = np.asarray(value.values, dtype=np.float32).reshape(1, -1)
        logits = np.asarray(self.module(rows), dtype=np.float64)[0]
        shifted = logits - float(np.max(logits))
        probabilities = np.exp(shifted) / float(np.sum(np.exp(shifted)))
        return ModelResult(
            {"backend": "tensor", "score": float(probabilities[1])}
        )


def _dense_stream(
    stream_id: str,
    modality: str,
    clock_id: str,
    channels: tuple[tuple[str, str, str], ...],
    *,
    rate_model: RateModel,
    sample_rate_hz: float | None = None,
) -> StreamSpec:
    return StreamSpec(
        stream_id=stream_id,
        revision=1,
        modality=modality,
        content_kind=ContentKind.DENSE_SAMPLES,
        rate_model=rate_model,
        clock_id=clock_id,
        channels=tuple(
            ChannelSpec(channel_id, kind, unit)
            for channel_id, kind, unit in channels
        ),
        sample_rate_hz=sample_rate_hz,
        sample_dtype="float64",
        metadata={"fixture_only": True},
    )


def _sparse_stream(
    stream_id: str,
    modality: str,
    clock_id: str,
) -> StreamSpec:
    return StreamSpec(
        stream_id=stream_id,
        revision=1,
        modality=modality,
        content_kind=ContentKind.SPARSE_EVENTS,
        rate_model=RateModel.EVENT,
        clock_id=clock_id,
        metadata={"fixture_only": True},
    )


def _regular_packet(
    stream: StreamSpec,
    batch_id: str,
    values: np.ndarray,
    available: float,
) -> DenseSampleBatch:
    assert stream.sample_rate_hz is not None
    return DenseSampleBatch(
        batch_id=batch_id,
        stream_id=stream.stream_id,
        stream_revision=stream.revision,
        sequence_start=0,
        channel_ids=tuple(value.channel_id for value in stream.channels),
        values=np.asarray(values, dtype=np.float64),
        received_time=TimePoint(available - 0.001, BOUNDARY_CLOCK),
        available_time=TimePoint(available, BOUNDARY_CLOCK),
        first_sample_time=TimePoint(0.0, stream.clock_id),
        sample_period_seconds=1.0 / stream.sample_rate_hz,
    )


def _irregular_packet(
    stream: StreamSpec,
    batch_id: str,
    values: np.ndarray,
    sample_seconds: tuple[float, ...],
    available: float,
) -> DenseSampleBatch:
    return DenseSampleBatch(
        batch_id=batch_id,
        stream_id=stream.stream_id,
        stream_revision=stream.revision,
        sequence_start=0,
        channel_ids=tuple(value.channel_id for value in stream.channels),
        values=np.asarray(values, dtype=np.float64),
        received_time=TimePoint(available - 0.01, BOUNDARY_CLOCK),
        available_time=TimePoint(available, BOUNDARY_CLOCK),
        sample_times=tuple(TimePoint(value, stream.clock_id) for value in sample_seconds),
    )


def _event_packet(
    stream: StreamSpec,
    batch_id: str,
    kinds: tuple[str, ...],
    available: float,
) -> SparseEventBatch:
    events = tuple(
        SparseEvent(
            event_id=f"event.{batch_id}.{index}",
            kind=kind,
            event_time=TimePoint(available - 0.02 + index * 0.001, stream.clock_id),
            received_time=TimePoint(available - 0.001, BOUNDARY_CLOCK),
            available_time=TimePoint(available, BOUNDARY_CLOCK),
            value={"unit": index + 1},
        )
        for index, kind in enumerate(kinds)
    )
    return SparseEventBatch(
        batch_id=batch_id,
        stream_id=stream.stream_id,
        stream_revision=stream.revision,
        sequence_start=0,
        events=events,
    )


def _signal_contract(
    stream: StreamSpec,
    *,
    event_kinds: tuple[str, ...] = (),
    minimum_duration_seconds: float | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type_id": DENSE_SCHEMA
        if stream.content_kind == ContentKind.DENSE_SAMPLES
        else SPARSE_SCHEMA,
        "content_kind": stream.content_kind.value,
        "rate_model": stream.rate_model.value,
    }
    if stream.channels:
        payload["channel_ids"] = [value.channel_id for value in stream.channels]
        payload["units"] = {
            value.channel_id: value.unit for value in stream.channels
        }
    if stream.sample_rate_hz is not None:
        payload["nominal_rate_hz"] = stream.sample_rate_hz
    if event_kinds:
        payload["event_kinds"] = list(event_kinds)
    if minimum_duration_seconds is not None:
        payload["minimum_duration_seconds"] = minimum_duration_seconds
        payload["window_duration_seconds"] = minimum_duration_seconds
    return payload


def _generality_cases() -> tuple[GeneralityCase, ...]:
    eeg = _dense_stream(
        "stream.eeg",
        "eeg",
        "device.eeg.clock",
        tuple((f"channel.eeg.{name.lower()}", "eeg", "V") for name in ("Fz", "Cz", "Pz", "Oz")),
        rate_model=RateModel.REGULAR,
        sample_rate_hz=250.0,
    )
    fnirs = _dense_stream(
        "stream.fnirs",
        "fnirs",
        "device.fnirs.clock",
        (
            ("channel.fnirs.hbo", "fnirs_hbo", "mol_per_l"),
            ("channel.fnirs.hbr", "fnirs_hbr", "mol_per_l"),
        ),
        rate_model=RateModel.IRREGULAR,
    )
    lfp = _dense_stream(
        "stream.lfp",
        "neuropixels_lfp",
        "device.probe.clock",
        tuple((f"channel.lfp.{index}", "lfp", "V") for index in range(4)),
        rate_model=RateModel.REGULAR,
        sample_rate_hz=2500.0,
    )
    spikes = _sparse_stream(
        "stream.spikes",
        "neuropixels_spikes",
        "device.probe.clock",
    )
    aux_eeg = _dense_stream(
        "stream.aux_eeg",
        "eeg",
        "device.aux_eeg.clock",
        (("channel.aux_eeg.cz", "eeg", "V"),),
        rate_model=RateModel.REGULAR,
        sample_rate_hz=250.0,
    )
    emg = _dense_stream(
        "stream.emg",
        "emg",
        "device.emg.clock",
        (("channel.emg.flexor", "emg", "V"),),
        rate_model=RateModel.REGULAR,
        sample_rate_hz=1000.0,
    )
    behavior = _sparse_stream(
        "stream.behavior",
        "behavior",
        "device.behavior.clock",
    )
    return (
        GeneralityCase(
            "dense_eeg",
            (eeg,),
            {
                eeg.stream_id: (
                    _regular_packet(
                        eeg,
                        "batch.eeg",
                        np.arange(64, dtype=np.float64).reshape(16, 4) / 1e6,
                        0.08,
                    ),
                )
            },
            {"eeg": _signal_contract(eeg)},
        ),
        GeneralityCase(
            "irregular_fnirs",
            (fnirs,),
            {
                fnirs.stream_id: (
                    _irregular_packet(
                        fnirs,
                        "batch.fnirs",
                        np.asarray(
                            [
                                [1.0e-6, -0.5e-6],
                                [1.2e-6, -0.4e-6],
                                [1.1e-6, -0.6e-6],
                                [1.3e-6, -0.7e-6],
                            ]
                        ),
                        (0.0, 0.7, 1.9, 3.2),
                        3.3,
                    ),
                )
            },
            {
                "fnirs": _signal_contract(
                    fnirs,
                    minimum_duration_seconds=3.0,
                )
            },
        ),
        GeneralityCase(
            "sparse_spikes_dense_lfp",
            (lfp, spikes),
            {
                lfp.stream_id: (
                    _regular_packet(
                        lfp,
                        "batch.lfp",
                        np.arange(80, dtype=np.float64).reshape(20, 4) / 1e6,
                        0.10,
                    ),
                ),
                spikes.stream_id: (
                    _event_packet(
                        spikes,
                        "batch.spikes",
                        ("spike.unit_1", "spike.unit_2", "spike.unit_1"),
                        0.12,
                    ),
                ),
            },
            {
                "lfp": _signal_contract(lfp),
                "spikes": _signal_contract(
                    spikes,
                    event_kinds=("spike.unit_1", "spike.unit_2"),
                ),
            },
        ),
        GeneralityCase(
            "multirate_aux_behavior",
            (aux_eeg, emg, behavior),
            {
                aux_eeg.stream_id: (
                    _regular_packet(
                        aux_eeg,
                        "batch.aux_eeg",
                        np.linspace(-1e-6, 1e-6, 10).reshape(10, 1),
                        0.05,
                    ),
                ),
                emg.stream_id: (
                    _regular_packet(
                        emg,
                        "batch.emg",
                        np.linspace(0.0, 2e-3, 40).reshape(40, 1),
                        0.06,
                    ),
                ),
                behavior.stream_id: (
                    _event_packet(
                        behavior,
                        "batch.behavior",
                        ("response.button",),
                        0.07,
                    ),
                ),
            },
            {
                "aux_eeg": _signal_contract(aux_eeg),
                "emg": _signal_contract(emg),
                "behavior": _signal_contract(
                    behavior,
                    event_kinds=("response.button",),
                ),
            },
        ),
    )


def _summary_descriptor(case: GeneralityCase) -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id=f"fixture.model.packet_summary.{case.case_id}",
        version="1.0.0",
        kind=ComponentKind.MODEL,
        config_schema=EMPTY_SCHEMA,
        input_ports=tuple(
            PortSpec(port_name, str(contract["type_id"]))
            for port_name, contract in case.contracts.items()
        ),
        output_ports=(PortSpec("prediction", PREDICTION_SCHEMA, multiple=True),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset({ExecutionMode.CAUSAL}),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.BITWISE,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=lambda config: PacketSummaryModel(),
        implementation="tests.test_phase6_generality:PacketSummaryModel",
        distribution="phase6-generality-fixture",
    )


def _manifest(case: GeneralityCase, plugin_id: str) -> ModelManifest:
    return ModelManifest(
        model_id=f"model.{case.case_id}",
        model_version="1.0.0",
        contract=ModelContract(
            inputs=tuple(
                ModelInputContract(
                    port_name,
                    str(contract["type_id"]),
                    requirements={
                        key: value
                        for key, value in contract.items()
                        if key != "type_id"
                    },
                )
                for port_name, contract in case.contracts.items()
            ),
            outputs=(
                ModelOutputContract(
                    "prediction",
                    PREDICTION_SCHEMA,
                    value_schema=SUMMARY_VALUE_SCHEMA,
                ),
            ),
            state=ModelStateContract(),
        ),
        artifacts=(),
        implementations=(ModelImplementationRequirement(plugin_id, "~=1.0"),),
        annotations={"representational_fixture": True},
    )


def _protocol(case: GeneralityCase) -> ProtocolSpec:
    return ProtocolSpec.from_payload(
        {
            "schema": "eegle.protocol_spec.v1",
            "protocol_id": f"protocol.{case.case_id}",
            "execution_mode": "causal",
            "claims": [
                {
                    "claim_id": f"claim.{case.case_id}",
                    "statement": "The core contracts can represent this fixture.",
                }
            ],
            "metrics": [],
            "acceptance": [],
            "annotations": {"support_claim": "representational_only"},
        }
    )


def _suite(case: GeneralityCase, manifest: ModelManifest) -> SuiteSpec:
    sources = []
    routes = []
    for port_name, stream in zip(case.contracts, case.streams, strict=True):
        sources.append(
            {
                "component_id": f"source.{port_name}",
                "kind": "source",
                "plugin_id": None,
                "stream_id": stream.stream_id,
                "config": {},
            }
        )
        routes.append(
            {
                "route_id": f"route.{port_name}_model",
                "source": {
                    "component": f"source.{port_name}",
                    "port": "samples"
                    if stream.content_kind == ContentKind.DENSE_SAMPLES
                    else "events",
                },
                "target": {"component": "model.summary", "port": port_name},
            }
        )
    plugin_id = f"fixture.model.packet_summary.{case.case_id}"
    components = [
        *sources,
        {
            "component_id": "model.summary",
            "kind": "model",
            "plugin_id": plugin_id,
            "version_spec": "~=1.0",
            "role": "observer",
            "config": {},
        },
    ]
    return SuiteSpec.from_payload(
        {
            "schema": "eegle.suite_spec.v1",
            "suite_id": f"suite.{case.case_id}",
            "protocol_id": f"protocol.{case.case_id}",
            "streams": [
                {
                    "stream_id": stream.stream_id,
                    "modality": stream.modality,
                    "clock_id": stream.clock_id,
                    "contract": contract,
                }
                for stream, contract in zip(
                    case.streams, case.contracts.values(), strict=True
                )
            ],
            "components": components,
            "routes": routes,
            "phases": [
                {
                    "phase_id": "phase.run",
                    "components": [value["component_id"] for value in components],
                    "transitions": [],
                    "timeout_seconds": 20.0,
                }
            ],
            "initial_phase": "phase.run",
            "artifacts": [],
            "model_roles": [],
            "model_uses": [
                {
                    "component_id": "model.summary",
                    "manifest_digest": manifest.manifest_digest,
                    "role_id": "observer",
                    "comparison_group": None,
                }
            ],
            "scheduling": {},
            "scheduled_triggers": [],
            "state_triggers": [],
            "clock_policy": {
                "execution_clock_id": BOUNDARY_CLOCK,
                "ordering": "availability_watermark",
            },
            "recording": {
                "execution_capture": True,
                "semantic_evidence": True,
            },
            "validation": {"require_replay_equivalence": True},
        }
    )


def _deployment(case: GeneralityCase) -> DeploymentSpec:
    bindings = []
    resources = []
    stream_bindings = []
    mappings: dict[str, dict[str, Any]] = {}
    for port_name, stream in zip(case.contracts, case.streams, strict=True):
        component_id = f"source.{port_name}"
        resource_id = f"resource.{port_name}"
        contract = dict(case.contracts[port_name])
        bindings.append(
            {
                "component_id": component_id,
                "plugin_id": "eegle.sources.packet_sequence_dense"
                if stream.content_kind == ContentKind.DENSE_SAMPLES
                else "eegle.sources.packet_sequence_sparse",
                "version_spec": "~=0.1.0",
                "placement": "in_process",
                "resource_ids": [resource_id],
                "config": {
                    "stream_spec": stream.to_payload(),
                    "packets": [
                        value.to_payload() for value in case.packets[stream.stream_id]
                    ],
                },
            }
        )
        resources.append(
            {
                "resource_id": resource_id,
                "kind": "simulator",
                "selector": {"generator": "packet_sequence"},
                "capabilities": ["deterministic"],
                "contract": contract,
            }
        )
        stream_bindings.append(
            {
                "stream_id": stream.stream_id,
                "resource_id": resource_id,
                "selector": {},
            }
        )
        mappings[stream.clock_id] = {
            "source_clock": stream.clock_id,
            "target_clock": BOUNDARY_CLOCK,
            "strategy": "declared_affine",
            "maximum_uncertainty_seconds": 0.001,
        }
    return DeploymentSpec.from_payload(
        {
            "schema": "eegle.deployment_spec.v1",
            "deployment_id": f"deployment.{case.case_id}",
            "suite_id": f"suite.{case.case_id}",
            "component_bindings": bindings,
            "resources": resources,
            "stream_bindings": stream_bindings,
            "storage": [
                {
                    "storage_id": "storage.evidence",
                    "kind": "evidence",
                    "uri": f"memory://phase6/{case.case_id}",
                }
            ],
            "permissions": [],
            "authorization_providers": [],
            "secrets": [],
            "clock_mappings": list(mappings.values()),
            "model_artifacts": [],
        }
    )


def _compile_case(case: GeneralityCase):
    descriptor = _summary_descriptor(case)
    manifest = _manifest(case, descriptor.plugin_id)
    registry = PluginRegistry()
    registry.register_builtins()
    registry.register(descriptor)
    compiled = compile_suite(
        _protocol(case),
        _suite(case, manifest),
        _deployment(case),
        registry,
        model_manifests={manifest.manifest_digest: manifest},
    )
    return compiled, registry


def _framework_descriptor(plugin_id: str, factory: Any) -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id=plugin_id,
        version="1.0.0",
        kind=ComponentKind.MODEL,
        config_schema=EMPTY_SCHEMA,
        input_ports=(PortSpec("signal", DENSE_SCHEMA, required=False),),
        output_ports=(PortSpec("prediction", PREDICTION_SCHEMA),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset({ExecutionMode.CAUSAL}),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.NUMERIC,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=factory,
        implementation=f"phase6_optional_fixture:{plugin_id}",
        distribution="phase6-optional-framework-fixture",
    )


def _framework_manifest(plugin_id: str) -> ModelManifest:
    return ModelManifest(
        model_id=f"model.{plugin_id.rsplit('.', 1)[-1]}",
        model_version="1.0.0",
        contract=ModelContract(
            inputs=(ModelInputContract("signal", DENSE_SCHEMA),),
            outputs=(
                ModelOutputContract(
                    "prediction",
                    PREDICTION_SCHEMA,
                    value_schema={
                        "type": "object",
                        "required": ["backend", "score"],
                        "properties": {
                            "backend": {"type": "string"},
                            "score": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "additionalProperties": False,
                    },
                ),
            ),
            state=ModelStateContract(replay_equivalence=EquivalenceLevel.NUMERIC),
        ),
        artifacts=(),
        implementations=(ModelImplementationRequirement(plugin_id, "~=1.0"),),
        annotations={"framework_contract_fixture": True},
    )


class Phase6GeneralityTests(unittest.TestCase):
    def test_representational_matrix_compiles_runs_records_replays_and_verifies(self) -> None:
        for case in _generality_cases():
            with self.subTest(case=case.case_id):
                compiled, registry = _compile_case(case)
                engine = ExecutionEngine.from_plan(compiled.plan, registry)
                run = engine.run()
                self.assertEqual(run.status, EngineStatus.COMPLETE, run.failure)
                expected = sum(len(values) for values in case.packets.values())
                predictions = run.phase_results[0].emissions_from(
                    "model.summary", "prediction"
                )
                self.assertEqual(len(predictions), expected)
                self.assertEqual(
                    {value.value["input_port"] for value in predictions},
                    set(case.contracts),
                )
                self.assertTrue(
                    all(value.admitted_input_ids for value in predictions)
                )
                streams = tuple(
                    engine.runtime.node(f"source.{port_name}").component.stream_spec
                    for port_name in case.contracts
                )
                with tempfile.TemporaryDirectory() as directory:
                    session = Session.create(
                        Path(directory) / "session",
                        session_id=f"session.phase6.{case.case_id}",
                    )
                    bundle = persist_engine_run(
                        session,
                        run,
                        plan=compiled.plan,
                        streams=streams,
                    )
                    reader = EvidenceReader.open(session, bundle.bundle_id)
                    self.assertTrue(reader.verify().valid)
                    replay = BundleReplayRunner(registry).run(reader)
                self.assertEqual(replay.result.status, EngineStatus.COMPLETE)
                self.assertTrue(
                    replay.equivalence.equivalent,
                    replay.equivalence.divergences,
                )

    def test_generality_claims_are_checked_contracts_not_modality_names(self) -> None:
        case = next(
            value for value in _generality_cases() if value.case_id == "irregular_fnirs"
        )
        descriptor = _summary_descriptor(case)
        invalid_contracts = {
            "fnirs": {**case.contracts["fnirs"], "rate_model": "regular"}
        }
        invalid = GeneralityCase(
            case.case_id,
            case.streams,
            case.packets,
            invalid_contracts,
        )
        manifest = _manifest(invalid, descriptor.plugin_id)
        registry = PluginRegistry()
        registry.register_builtins()
        registry.register(descriptor)
        with self.assertRaises(CompilationError) as raised:
            compile_suite(
                _protocol(case),
                _suite(case, manifest),
                _deployment(case),
                registry,
                model_manifests={manifest.manifest_digest: manifest},
            )
        self.assertIn(
            "rate model irregular does not match required regular",
            str(raised.exception),
        )

    def test_framework_adapters_need_no_new_core_plugin_abstraction(self) -> None:
        case = _generality_cases()[0]
        packet = case.packets[case.streams[0].stream_id][0]
        assert isinstance(packet, DenseSampleBatch)
        factories = {
            "fixture.framework.estimator": lambda config: EstimatorAdapterFixture(
                EstimatorFixture()
            ),
            "fixture.framework.tensor": lambda config: TensorAdapterFixture(
                TensorModuleFixture()
            ),
        }
        for plugin_id, factory in factories.items():
            with self.subTest(plugin_id=plugin_id):
                descriptor = _framework_descriptor(plugin_id, factory)
                manifest = _framework_manifest(plugin_id)
                registry = PluginRegistry()
                registry.register(descriptor)
                suite_payload = {
                    "schema": "eegle.suite_spec.v1",
                    "suite_id": f"suite.{plugin_id.rsplit('.', 1)[-1]}",
                    "protocol_id": "protocol.framework-boundary",
                    "streams": [],
                    "components": [
                        {
                            "component_id": "model.framework",
                            "kind": "model",
                            "plugin_id": plugin_id,
                            "version_spec": "~=1.0",
                            "role": "observer",
                            "config": {},
                        }
                    ],
                    "routes": [],
                    "phases": [
                        {
                            "phase_id": "phase.run",
                            "components": ["model.framework"],
                            "transitions": [],
                        }
                    ],
                    "initial_phase": "phase.run",
                    "artifacts": [],
                    "model_roles": [],
                    "model_uses": [
                        {
                            "component_id": "model.framework",
                            "manifest_digest": manifest.manifest_digest,
                            "role_id": "observer",
                            "comparison_group": None,
                        }
                    ],
                    "scheduling": {},
                    "scheduled_triggers": [],
                    "state_triggers": [],
                    "clock_policy": {"execution_clock_id": BOUNDARY_CLOCK},
                    "recording": {},
                    "validation": {},
                }
                protocol = ProtocolSpec.from_payload(
                    {
                        "schema": "eegle.protocol_spec.v1",
                        "protocol_id": "protocol.framework-boundary",
                        "execution_mode": "causal",
                        "claims": [
                            {
                                "claim_id": "claim.framework-boundary",
                                "statement": "An external adapter uses the model boundary.",
                            }
                        ],
                        "metrics": [],
                        "acceptance": [],
                        "annotations": {"support_claim": "contract_fixture_only"},
                    }
                )
                deployment = DeploymentSpec.from_payload(
                    {
                        "schema": "eegle.deployment_spec.v1",
                        "deployment_id": f"deployment.{plugin_id.rsplit('.', 1)[-1]}",
                        "suite_id": suite_payload["suite_id"],
                        "component_bindings": [],
                        "resources": [],
                        "stream_bindings": [],
                        "storage": [],
                        "permissions": [],
                        "authorization_providers": [],
                        "secrets": [],
                        "clock_mappings": [],
                        "model_artifacts": [],
                    }
                )
                compiled = compile_suite(
                    protocol,
                    SuiteSpec.from_payload(suite_payload),
                    deployment,
                    registry,
                    model_manifests={manifest.manifest_digest: manifest},
                )
                run = ExecutionEngine.from_plan(compiled.plan, registry).run(
                    inputs_by_phase={
                        "phase.run": (
                            GraphInput("model.framework", "signal", packet),
                        )
                    }
                )
                self.assertEqual(run.status, EngineStatus.COMPLETE, run.failure)
                prediction = run.phase_results[0].emissions_from(
                    "model.framework", "prediction"
                )[0]
                self.assertIn(prediction.value["backend"], {"estimator", "tensor"})
                self.assertGreaterEqual(prediction.value["score"], 0.0)
                self.assertLessEqual(prediction.value["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
