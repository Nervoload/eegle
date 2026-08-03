from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from eegle.authoring import ExperimentBuilder
from eegle.compiler import compile_suite
from eegle.integrations.lsl import (
    LSL_DENSE_SOURCE_PLUGIN_ID,
    LSL_METADATA_SOURCE_PLUGIN_ID,
    LSL_SPARSE_SOURCE_PLUGIN_ID,
    LslOutlet,
    LslSource,
    LslSupportLevel,
    LslSupportReport,
    detect_lsl,
    exact_selector,
    lsl_plugin_descriptors,
    select_exact_stream,
)
from eegle.operations import (
    StorageCapability,
    create_simulation_deployment,
    detect_capabilities,
    preflight,
    propose_deployment,
)
from eegle.plugins import PluginRegistry
from eegle.processing.windows import ContinuousWindowBuilder
from eegle.recording import EvidenceReader, Session, persist_engine_run
from eegle.runtime import EngineStatus, ExecutionEngine
from eegle.specs import DeploymentSpec, ProtocolSpec, StorageBinding, SuiteSpec
from eegle.streams import ContentKind, DenseSampleBatch, MetadataEvent, SparseEventBatch
from eegle.validation import (
    ValidationStatus,
    validate_evidence,
    validate_evidence_payloads,
)
from tests.fixtures.phase5_model_components import compile_phase5_suite
from tests.test_phase5_plan_execution import _payload, _recording_suite, _registry


class _Xml:
    def __init__(self, channels=(), index=0):
        self.channels = tuple(channels)
        self.index = index

    def child(self, name):
        return _Xml(self.channels, 0)

    def child_value(self, name):
        if self.index >= len(self.channels):
            return ""
        return self.channels[self.index].get(name, "")

    def next_sibling(self, name):
        return _Xml(self.channels, self.index + 1)

    def empty(self):
        return self.index >= len(self.channels)


class _Info:
    def __init__(
        self,
        name,
        stream_type,
        count,
        rate,
        channel_format,
        source_id,
        uid,
        chunks=(),
        channels=(),
        hostname="fixture-host",
        failures=0,
        descriptor_channels=None,
    ):
        self.values = (name, stream_type, count, rate, channel_format, source_id, uid, hostname)
        self.chunks = list(chunks)
        self.channels = tuple(channels)
        self.failures = failures
        self.descriptor_channels = (
            self.channels if descriptor_channels is None else tuple(descriptor_channels)
        )

    def name(self): return self.values[0]
    def type(self): return self.values[1]
    def channel_count(self): return self.values[2]
    def nominal_srate(self): return self.values[3]
    def channel_format(self): return self.values[4]
    def source_id(self): return self.values[5]
    def uid(self): return self.values[6]
    def hostname(self): return self.values[7]
    def desc(self): return _Xml(self.descriptor_channels)


class _Inlet:
    def __init__(self, info, recover, processing_flags):
        self.stream_info = info
        self.recover = recover
        self.processing_flags = processing_flags
        self.closed = False

    def pull_chunk(self, timeout, max_samples):
        if self.stream_info.failures:
            self.stream_info.failures -= 1
            raise RuntimeError("simulated disconnect")
        if not self.stream_info.chunks:
            return [], []
        return self.stream_info.chunks.pop(0)

    def time_correction(self, timeout):
        return 0.002

    def info(self, timeout):
        return _Info(
            *self.stream_info.values[:7],
            channels=self.stream_info.channels,
            hostname=self.stream_info.values[7],
        )

    def close_stream(self):
        self.closed = True


class _Outlet:
    def __init__(self, info):
        self.info = info
        self.samples = []
        self.chunks = []

    def push_sample(self, sample, timestamp):
        self.samples.append((sample, timestamp))

    def push_chunk(self, chunk, timestamp):
        self.chunks.append((chunk, timestamp))


class _Pylsl:
    proc_clocksync = 1
    proc_dejitter = 2
    proc_monotonize = 4
    cf_float32 = 1
    cf_string = 3

    def __init__(self, infos):
        self.infos = tuple(infos)
        self.inlets = []
        self.outlets = []
        self.clock = 10.0

    def resolve_streams(self, wait_time):
        return self.infos

    def StreamInlet(self, info, recover, processing_flags):
        value = _Inlet(info, recover, processing_flags)
        self.inlets.append(value)
        return value

    def StreamInfo(self, *args):
        return args

    def StreamOutlet(self, info):
        value = _Outlet(info)
        self.outlets.append(value)
        return value

    def local_clock(self):
        self.clock += 0.01
        return self.clock

    def library_version(self):
        return 118


def _network():
    dense = _Info(
        "Amp-EEG",
        "EEG",
        2,
        100.0,
        "float32",
        "amp-01",
        "uid-eeg",
        chunks=(
            ([[1.0, 2.0], [3.0, 4.0]], [1.00, 1.01]),
            ([[5.0, 6.0]], [1.05]),
        ),
        channels=(
            {"label": "C3", "unit": "uV"},
            {"label": "C4", "unit": "uV"},
        ),
    )
    sparse = _Info(
        "Task-Markers",
        "Markers",
        1,
        0.0,
        "string",
        "task-01",
        "uid-markers",
        chunks=(([["stimulus"]], [2.0]),),
    )
    metadata = _Info(
        "Quality",
        "Metadata",
        1,
        0.0,
        "string",
        "quality-01",
        "uid-metadata",
        chunks=(([[json.dumps({"impedance": "ok"})]], [3.0]),),
    )
    return _Pylsl((dense, sparse, metadata)), dense, sparse, metadata


def _compiled_lsl_recording(*, timeout_seconds: float):
    suite = _recording_suite()
    suite["phases"][0]["timeout_seconds"] = timeout_seconds
    deployment = _payload("deployment.json")
    source_binding = deployment["component_bindings"][0]
    source_binding["plugin_id"] = LSL_DENSE_SOURCE_PLUGIN_ID
    source_binding["version_spec"] = "~=0.1.0"
    source_binding["config"] = {
        "stream_spec": source_binding["config"]["stream_spec"],
        "selector": {"uid": "uid-eeg"},
        "boundary_clock_id": "boundary.clock",
        "timestamp_mode": "raw_with_mapping",
        "resolve_timeout_seconds": 0,
        "pull_timeout_seconds": 0,
    }
    deployment["resources"][0]["kind"] = "lsl_stream"
    deployment["resources"][0]["selector"] = {"uid": "uid-eeg"}
    deployment["resources"][0]["capabilities"] = ["lsl", "clock_mapping"]
    deployment["clock_mappings"][0]["strategy"] = "online_estimated"
    deployment["clock_mappings"][0]["maximum_uncertainty_seconds"] = 0.01
    registry = _registry()
    for descriptor in lsl_plugin_descriptors():
        registry.register(descriptor)
    compiled = compile_phase5_suite(
        ProtocolSpec.from_payload(_payload("protocol.json")),
        SuiteSpec.from_payload(suite),
        DeploymentSpec.from_payload(deployment),
        registry,
    )
    return compiled, registry, source_binding["config"]


class Phase7LslIntegrationTests(unittest.TestCase):
    def test_discovery_is_typed_exact_and_truthfully_simulation_validated(self) -> None:
        pylsl, dense, sparse, metadata = _network()
        detected = detect_lsl(wait_time=0, pylsl_module=pylsl)

        self.assertEqual(detected.support.support_level, LslSupportLevel.SIMULATED_VALIDATED)
        self.assertIsNone(detected.support.real_acceptance_id)
        with self.assertRaisesRegex(ValueError, "acceptance evidence identity"):
            LslSupportReport(
                True,
                "118",
                LslSupportLevel.LIVE_OBSERVE_ONLY_VALIDATED,
            )
        self.assertEqual(len(detected.sources), 3)
        by_plugin = {value.plugin_id: value for value in detected.sources}
        self.assertEqual(by_plugin[LSL_DENSE_SOURCE_PLUGIN_ID].stream.content_kind, ContentKind.DENSE_SAMPLES)
        self.assertEqual(by_plugin[LSL_SPARSE_SOURCE_PLUGIN_ID].stream.content_kind, ContentKind.SPARSE_EVENTS)
        self.assertEqual(by_plugin[LSL_METADATA_SOURCE_PLUGIN_ID].stream.content_kind, ContentKind.METADATA)
        eeg = by_plugin[LSL_DENSE_SOURCE_PLUGIN_ID].stream
        self.assertEqual(tuple(value.name for value in eeg.channels), ("C3", "C4"))
        self.assertEqual(tuple(value.unit for value in eeg.channels), ("uV", "uV"))
        self.assertEqual(eeg.sample_rate_hz, 100.0)
        self.assertIs(select_exact_stream((dense, sparse), {"uid": "uid-eeg"}), dense)
        with self.assertRaisesRegex(ValueError, "requires uid"):
            exact_selector({"name": "Amp-EEG"})
        duplicate = _Info(*dense.values[:7], hostname=dense.values[7])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            select_exact_stream((dense, duplicate), {"source_id": "amp-01"})

    def test_discovery_retrieves_native_full_info_channel_metadata(self) -> None:
        short = _Info(
            "Amp-Short-Info",
            "EEG",
            2,
            100.0,
            "float32",
            "amp-short",
            "uid-short",
            channels=(
                {"label": "Fz", "unit": "uV"},
                {"label": "Cz", "unit": "uV"},
            ),
            descriptor_channels=(),
        )
        pylsl = _Pylsl((short,))

        detected = detect_lsl(wait_time=0, pylsl_module=pylsl)

        stream = detected.sources[0].stream
        self.assertEqual(tuple(channel.name for channel in stream.channels), ("Fz", "Cz"))
        self.assertEqual(tuple(channel.unit for channel in stream.channels), ("uV", "uV"))
        self.assertEqual(len(pylsl.inlets), 1)
        self.assertTrue(pylsl.inlets[0].closed)

    def test_dense_sparse_metadata_clock_reconnect_and_packet_loss(self) -> None:
        pylsl, dense, sparse, metadata = _network()
        detected = detect_lsl(wait_time=0, pylsl_module=pylsl)
        by_plugin = {value.plugin_id: value for value in detected.sources}

        dense.failures = 1
        source = LslSource(by_plugin[LSL_DENSE_SOURCE_PLUGIN_ID].config, pylsl_module=pylsl)
        first = source.read()
        second = source.read()
        self.assertIsInstance(first, DenseSampleBatch)
        self.assertIsInstance(second, DenseSampleBatch)
        self.assertEqual(
            tuple(value.seconds for value in first.sample_times),
            (1.0, 1.01),
        )

        class Context:
            component_id = "window.lsl"
            component_version = "0.1.0"
            clock_mapping_revisions = {}
            current_time = first.available_time

            def next_id(self, namespace):
                return f"{namespace}.lsl"

        windows = tuple(ContinuousWindowBuilder(2, 2).update(first, Context()))
        self.assertEqual(len(windows), 1)
        self.assertEqual(source.reconnect_count, 1)
        self.assertEqual(second.sequence_start, 5)
        self.assertEqual(source.packet_loss_observations[0].estimated_missing_samples, 3)
        self.assertEqual(source.clock_observation().correction_seconds, 0.002)
        self.assertEqual(pylsl.inlets[-1].processing_flags, 0)
        observations = source.drain_evidence_observations()
        self.assertEqual(
            tuple(value[0] for value in observations),
            (
                "source_clock_observation",
                "source_reconnect",
                "source_clock_observation",
                "source_packet_loss",
            ),
        )
        self.assertTrue(
            all(
                value.clock_id == by_plugin[LSL_DENSE_SOURCE_PLUGIN_ID].stream.clock_id
                for value in (*first.sample_times, *second.sample_times)
            )
        )
        self.assertEqual(
            observations[0][1]["timestamp_mode"], "raw_with_mapping"
        )

        marker_source = LslSource(by_plugin[LSL_SPARSE_SOURCE_PLUGIN_ID].config, pylsl_module=pylsl)
        marker = marker_source.read()
        self.assertIsInstance(marker, SparseEventBatch)
        self.assertEqual(marker.events[0].value, "stimulus")

        metadata_source = LslSource(by_plugin[LSL_METADATA_SOURCE_PLUGIN_ID].config, pylsl_module=pylsl)
        event = metadata_source.read()
        self.assertIsInstance(event, MetadataEvent)
        self.assertEqual(event.metadata["impedance"], "ok")

    def test_metadata_chunks_preserve_every_record_and_sequence(self) -> None:
        metadata = _Info(
            "Quality",
            "Metadata",
            1,
            0.0,
            "string",
            "quality-multi",
            "uid-metadata-multi",
            chunks=(
                (
                    [[json.dumps({"n": 1})], [json.dumps({"n": 2})]],
                    [3.0, 3.1],
                ),
            ),
        )
        pylsl = _Pylsl((metadata,))
        detected = detect_lsl(wait_time=0, pylsl_module=pylsl)
        source = LslSource(detected.sources[0].config, pylsl_module=pylsl)

        first = source.read()
        second = source.read()

        self.assertIsInstance(first, MetadataEvent)
        self.assertIsInstance(second, MetadataEvent)
        self.assertEqual((first.metadata["n"], second.metadata["n"]), (1, 2))
        self.assertEqual((first.sequence, second.sequence), (0, 1))
        self.assertEqual(
            (first.event_time.seconds, second.event_time.seconds),
            (3.0, 3.1),
        )

    def test_outlets_and_dependency_lazy_descriptors(self) -> None:
        pylsl, _, _, _ = _network()
        detected = detect_lsl(wait_time=0, pylsl_module=pylsl)
        by_plugin = {value.plugin_id: value for value in detected.sources}
        dense = LslSource(
            by_plugin[LSL_DENSE_SOURCE_PLUGIN_ID].config,
            pylsl_module=pylsl,
        ).read()
        marker = LslSource(
            by_plugin[LSL_SPARSE_SOURCE_PLUGIN_ID].config,
            pylsl_module=pylsl,
        ).read()
        metadata = LslSource(
            by_plugin[LSL_METADATA_SOURCE_PLUGIN_ID].config,
            pylsl_module=pylsl,
        ).read()
        dense_outlet = LslOutlet(
            {
                "name": "EEGle-EEG",
                "type": "EEG",
                "source_id": "eegle-dense-test",
                "channel_count": 2,
                "nominal_rate_hz": 100.0,
                "channel_format": "cf_float32",
            },
            pylsl_module=pylsl,
        )
        marker_outlet = LslOutlet({
            "name": "EEGle-Markers",
            "type": "Markers",
            "source_id": "eegle-test",
            "channel_count": 1,
            "nominal_rate_hz": 0.0,
            "channel_format": "cf_string",
        }, pylsl_module=pylsl)
        metadata_outlet = LslOutlet(
            {
                "name": "EEGle-Metadata",
                "type": "Metadata",
                "source_id": "eegle-metadata-test",
                "channel_count": 1,
                "nominal_rate_hz": 0.0,
                "channel_format": "cf_string",
            },
            pylsl_module=pylsl,
        )
        dense_outlet.append(dense)
        marker_outlet.append(marker)
        metadata_outlet.append(metadata)
        self.assertEqual(len(pylsl.outlets[0].samples), 2)
        self.assertEqual(pylsl.outlets[1].samples[0][0], ["stimulus"])
        self.assertEqual(
            json.loads(pylsl.outlets[2].samples[0][0][0]),
            {"impedance": "ok"},
        )
        self.assertEqual(len(lsl_plugin_descriptors()), 6)

        code = """
import builtins
real = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'pylsl' or name.startswith('pylsl.'):
        raise AssertionError('pylsl import leaked')
    return real(name, *args, **kwargs)
builtins.__import__ = blocked
import eegle
import eegle.operations
from eegle.integrations.lsl import lsl_plugin_descriptors
assert len(lsl_plugin_descriptors()) == 6
"""
        completed = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_same_portable_suite_compiles_for_simulation_and_lsl(self) -> None:
        authored = (
            ExperimentBuilder.continuous_recording("lsl-portability")
            .signal(unit="uV", channel_count=2, sample_rate_hz=100.0)
            .build()
        )
        registry = PluginRegistry()
        registry.register_builtins()
        for descriptor in lsl_plugin_descriptors():
            registry.register(descriptor)
        simulated = compile_suite(
            authored.protocol,
            authored.suite,
            create_simulation_deployment(authored),
            registry,
        )

        pylsl, _, _, _ = _network()
        detection = detect_lsl(wait_time=0, pylsl_module=pylsl)
        capability = next(
            value
            for value in detection.sources
            if value.plugin_id == LSL_DENSE_SOURCE_PLUGIN_ID
        )
        report = detect_capabilities(
            registry=registry,
            include_entry_points=False,
            sources=(capability,),
            storage=(
                StorageCapability(
                    "capability.storage.lsl-test",
                    StorageBinding("storage.evidence", "evidence", "memory://lsl-live"),
                ),
            ),
            clocks=detection.clocks,
            detection_id="detection.lsl.portability",
            observed_at="2026-07-28T12:00:00Z",
        )
        live = propose_deployment(authored, report).deployment
        logical = authored.suite.streams[0]
        source_binding = next(
            value
            for value in live.component_bindings
            if value.plugin_id == LSL_DENSE_SOURCE_PLUGIN_ID
        )
        self.assertEqual(source_binding.config["stream_spec"]["stream_id"], logical.stream_id)
        self.assertEqual(source_binding.config["stream_spec"]["clock_id"], logical.clock_id)
        live_compiled = compile_suite(
            authored.protocol,
            authored.suite,
            live,
            registry,
        )

        self.assertEqual(
            simulated.plan.spec_hashes["suite"], live_compiled.plan.spec_hashes["suite"]
        )
        self.assertNotEqual(simulated.plan.plan_hash, live_compiled.plan.plan_hash)
        self.assertTrue(
            preflight(
                live_compiled.plan,
                live_compiled.lock,
                live,
                registry,
                detection_report=report,
            ).ready
        )
        changed_capability = replace(capability, selector={"uid": "changed-stream"})
        changed_report = replace(report, sources=(changed_capability,))
        changed_preflight = preflight(
            live_compiled.plan,
            live_compiled.lock,
            live,
            registry,
            detection_report=changed_report,
        )
        identity = next(
            value
            for value in changed_preflight.checks
            if value.check_id.endswith(".available")
        )
        self.assertEqual(identity.status.value, "fail")

    def test_compiled_engine_handles_silence_reconnect_gap_timeout_and_persistence(self) -> None:
        compiled, registry, config = _compiled_lsl_recording(timeout_seconds=0.03)
        pylsl, dense, _, _ = _network()
        pylsl.clock = -0.02
        dense.failures = 1
        source = LslSource(config, pylsl_module=pylsl)
        engine = ExecutionEngine.from_plan(
            compiled.plan,
            registry,
            component_overrides={"source.neural": source},
        )

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.TIMED_OUT)
        self.assertEqual(len(run.captured_packets), 2)
        record_types = [value.record_type for value in run.evidence]
        for expected in (
            "source_clock_observation",
            "source_reconnect",
            "source_packet_loss",
            "source_sequence_gap",
            "phase_timeout",
            "phase_finished",
        ):
            self.assertIn(expected, record_types)
        semantic = validate_evidence_payloads(
            run.evidence,
            subject_id="session.lsl.lifecycle",
        )
        self.assertEqual(semantic.status, ValidationStatus.PASS, semantic.details)
        health = next(
            value
            for value in validate_evidence(
                run.evidence,
                subject_id="session.lsl.lifecycle",
                required_source_observations=("source_clock_observation",),
            ).results
            if value.result_id == "source.acquisition_observations"
        )
        self.assertEqual(health.status, ValidationStatus.WARNING)
        observations = {
            value.observation_id: value.value for value in health.observations
        }
        self.assertEqual(observations["source.clock_observation_count"], 2)
        self.assertEqual(observations["source.reconnect_count"], 1)
        self.assertEqual(observations["source.packet_loss_event_count"], 1)

        without_clock = tuple(
            value
            for value in run.evidence
            if value.record_type != "source_clock_observation"
        )
        missing_health = next(
            value
            for value in validate_evidence(
                without_clock,
                subject_id="session.lsl.missing-clock",
                required_source_observations=("source_clock_observation",),
            ).results
            if value.result_id == "source.acquisition_observations"
        )
        self.assertEqual(
            missing_health.status,
            ValidationStatus.INSUFFICIENT_EVIDENCE,
        )

        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(
                Path(directory) / "session",
                session_id="session.lsl.lifecycle",
            )
            bundle = persist_engine_run(
                session,
                run,
                plan=compiled.plan,
                streams=(source.stream_spec,),
                bundle_id="bundle.lsl.lifecycle",
            )
            reader = EvidenceReader.open(session, bundle.bundle_id)
            self.assertTrue(reader.verify().valid)
            self.assertEqual(
                tuple(reader.bundle.metadata["source_observation_requirements"]),
                ("source_clock_observation",),
            )
            persisted_types = {
                value.record_type for value in reader.records()
            }

        self.assertIn("source_clock_observation", persisted_types)
        self.assertIn("source_packet_loss", persisted_types)

    def test_compiled_live_lsl_run_has_a_synchronous_cancellation_path(self) -> None:
        compiled, registry, config = _compiled_lsl_recording(timeout_seconds=1.0)
        pylsl, dense, _, _ = _network()
        pylsl.clock = -0.02
        dense.chunks.clear()
        source = LslSource(config, pylsl_module=pylsl)
        engine = ExecutionEngine.from_plan(
            compiled.plan,
            registry,
            component_overrides={"source.neural": source},
        )
        engine.cancel()

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.CANCELLED)
        self.assertTrue(
            any(value.record_type == "phase_finished" for value in run.evidence)
        )


if __name__ == "__main__":
    unittest.main()
