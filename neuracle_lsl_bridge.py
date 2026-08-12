from __future__ import annotations

import argparse
import socket
import time
from dataclasses import dataclass
from typing import Iterator

import numpy as np
from pylsl import StreamInfo, StreamOutlet, cf_float32, cf_string, local_clock


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8712
    eeg_channels: int = 64
    sample_rate: float = 1000.0
    stream_name: str = "Neuracle-64"
    unit: str = "unknown"  # Replace after verifying the Recorder/API scale.
    latency_offset_s: float = 0.0


class NeuracleTCPSource:
    """Read Neuracle's TCP stream: N little-endian float32 EEG values + uint32 trigger."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._record_dtype = np.dtype(
            [
                ("eeg", "<f4", (config.eeg_channels,)),
                ("trigger", "<u4"),
            ],
            align=False,
        )

    def connect(self) -> None:
        sock = socket.create_connection(
            (self.config.host, self.config.port), timeout=5.0
        )
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(2.0)
        self._socket = sock

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
            self._socket = None

    def chunks(self) -> Iterator[tuple[np.ndarray, np.ndarray, float]]:
        if self._socket is None:
            raise RuntimeError("Source is not connected")

        record_bytes = self._record_dtype.itemsize
        while True:
            try:
                packet = self._socket.recv(1 << 20)
            except socket.timeout:
                continue

            if not packet:
                raise ConnectionError("Neuracle data server closed the connection")

            self._buffer.extend(packet)
            n_records = len(self._buffer) // record_bytes
            if n_records == 0:
                continue

            n_bytes = n_records * record_bytes
            complete = bytes(self._buffer[:n_bytes])
            del self._buffer[:n_bytes]

            records = np.frombuffer(complete, dtype=self._record_dtype)
            eeg = np.ascontiguousarray(records["eeg"], dtype=np.float32)
            trigger = np.asarray(records["trigger"], dtype=np.uint32).copy()

            # LSL interprets a scalar chunk timestamp as the capture time of
            # the most recent sample, then reconstructs earlier timestamps.
            t_last = local_clock() - self.config.latency_offset_s
            yield eeg, trigger, t_last


def make_outlets(config: Config) -> tuple[StreamOutlet, StreamOutlet]:
    source_id = f"neuracle-{config.host}-{config.port}-{config.eeg_channels}ch"

    eeg_info = StreamInfo(
        name=config.stream_name,
        type="EEG",
        channel_count=config.eeg_channels,
        nominal_srate=config.sample_rate,
        channel_format=cf_float32,
        source_id=source_id,
    )

    channels = eeg_info.desc().append_child("channels")
    for index in range(config.eeg_channels):
        channel = channels.append_child("channel")
        channel.append_child_value("label", f"Ch{index + 1:02d}")
        channel.append_child_value("type", "EEG")
        channel.append_child_value("unit", config.unit)

    acquisition = eeg_info.desc().append_child("acquisition")
    acquisition.append_child_value("manufacturer", "Neuracle")
    acquisition.append_child_value("transport", "Neuracle TCP data server")
    acquisition.append_child_value("trigger_encoding", "uint32 after EEG samples")

    marker_info = StreamInfo(
        name=f"{config.stream_name}-Markers",
        type="Markers",
        channel_count=1,
        nominal_srate=0.0,
        channel_format=cf_string,
        source_id=f"{source_id}-markers",
    )

    return (
        StreamOutlet(eeg_info, chunk_size=0, max_buffered=60),
        StreamOutlet(marker_info, chunk_size=1, max_buffered=360),
    )


def run(config: Config) -> None:
    eeg_outlet, marker_outlet = make_outlets(config)
    source = NeuracleTCPSource(config)
    last_trigger = 0
    total_samples = 0
    started = time.monotonic()

    try:
        source.connect()
        print(f"Connected to {config.host}:{config.port}")
        print(f"Publishing LSL streams: {config.stream_name} and {config.stream_name}-Markers")

        for eeg, triggers, t_last in source.chunks():
            eeg_outlet.push_chunk(eeg, timestamp=t_last)

            n_samples = eeg.shape[0]
            t_first = t_last - (n_samples - 1) / config.sample_rate
            for index, raw_code in enumerate(triggers):
                code = int(raw_code)
                # Emit a marker only on a non-zero transition to avoid
                # duplicating a trigger that remains high for several samples.
                if code != 0 and code != last_trigger:
                    timestamp = t_first + index / config.sample_rate
                    marker_outlet.push_sample([str(code)], timestamp=timestamp)
                last_trigger = code

            total_samples += n_samples
            if total_samples and total_samples % int(config.sample_rate * 5) < n_samples:
                elapsed = time.monotonic() - started
                observed_rate = total_samples / elapsed
                print(f"Samples: {total_samples:,}; observed rate: {observed_rate:.1f} Hz")

    except KeyboardInterrupt:
        print("Stopping")
    finally:
        source.close()


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="Bridge Neuracle TCP EEG to LSL")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8712)
    parser.add_argument("--eeg-channels", type=int, default=64)
    parser.add_argument("--sample-rate", type=float, default=1000.0)
    parser.add_argument("--stream-name", default="Neuracle-64")
    parser.add_argument("--unit", default="unknown")
    parser.add_argument("--latency-offset-ms", type=float, default=0.0)
    args = parser.parse_args()
    return Config(
        host=args.host,
        port=args.port,
        eeg_channels=args.eeg_channels,
        sample_rate=args.sample_rate,
        stream_name=args.stream_name,
        unit=args.unit,
        latency_offset_s=args.latency_offset_ms / 1000.0,
    )


if __name__ == "__main__":
    run(parse_args())


