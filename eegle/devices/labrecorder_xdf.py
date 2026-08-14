"""Managed LabRecorder/XDF acquisition with the existing CSV safety mirror."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from time import monotonic, sleep
from typing import Any, BinaryIO

from eegle.devices.lsl_eeg import LslEegRecorder
from eegle.session import SessionPaths


class LabRecorderXdfRecorder:
    """Control LabRecorder over its loopback socket and retain a CSV mirror."""

    def __init__(
        self,
        config: dict[str, Any],
        paths: SessionPaths,
        *,
        startup_timeout_seconds: float = 20.0,
    ) -> None:
        self.config = config
        self.paths = paths
        self.eeg_config = dict(config.get("hardware", {}).get("eeg", {}) or {})
        self.marker_config = dict(config.get("hardware", {}).get("markers", {}) or {})
        self.recorder_config = dict(config.get("processes", {}).get("recorder", {}) or {})
        if not bool(self.recorder_config.get("csv_mirror", False)):
            raise ValueError("labrecorder_xdf currently requires processes.recorder.csv_mirror=true")
        self.startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self.shutdown_timeout_seconds = max(
            2.0,
            float(self.recorder_config.get("shutdown_timeout_seconds", 15.0)),
        )
        self.xdf_growth_warning_seconds = max(
            2.0,
            float(
                self.recorder_config.get(
                    "xdf_growth_warning_seconds",
                    self.recorder_config.get("xdf_stall_timeout_seconds", 15.0),
                )
            ),
        )
        self.tail_guard_seconds = max(
            0.0,
            float(self.recorder_config.get("tail_guard_seconds", 1.0)),
        )
        self.rcs_port = int(self.recorder_config.get("rcs_port", 22345))
        self.labrecorder_config = paths.process_logs / "labrecorder.cfg"
        self.labrecorder_stdout = paths.process_logs / "labrecorder.stdout.log"
        self.labrecorder_stderr = paths.process_logs / "labrecorder.stderr.log"
        self._mirror = LslEegRecorder(
            self.eeg_config,
            paths.eeg_csv,
            paths.eeg_metadata,
            stream_timeout_seconds=float(self.eeg_config.get("stream_timeout_seconds", 5.0)),
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._rcs_socket: socket.socket | None = None
        self._stdout_handle: BinaryIO | None = None
        self._stderr_handle: BinaryIO | None = None
        self._executable: Path | None = None
        self._executable_sha256: str | None = None
        self._commands: list[str] = []
        self._launch_command: list[str] = []
        self._generated_config: str | None = None
        self._required_streams: list[str] = []
        self._selected_eeg_stream: dict[str, Any] = {}
        self._selected_marker_stream: dict[str, Any] = {}
        self._status = "initialized"
        self._error: str | None = None
        self._notes: list[str] = []
        self._last_xdf_size = 0
        self._last_xdf_growth_at = monotonic()
        self._xdf_growth_warning_active = False
        self._xdf_growth_warning_count = 0
        self._started_at = monotonic()
        self._stop_reason: str | None = None

    def start(self) -> dict[str, Any]:
        """Start the CSV mirror, launch LabRecorder, and prove XDF growth."""
        if self.paths.eeg_xdf.exists():
            raise FileExistsError(f"refusing to overwrite existing XDF recording: {self.paths.eeg_xdf}")
        self._executable = resolve_labrecorder_executable(
            str(self.recorder_config.get("executable") or "LabRecorder.exe")
        )
        self._executable_sha256 = sha256_file(self._executable)
        require_loopback_port_available(self.rcs_port)
        self._status = "starting"
        try:
            self._start_mirror()
            mirror = self._mirror.snapshot()
            self._selected_eeg_stream = dict(mirror.get("stream") or {})
            self._selected_marker_stream = resolve_labrecorder_marker_stream(
                self.marker_config,
                timeout_seconds=float(self.eeg_config.get("stream_timeout_seconds", 5.0)),
            )
            self._required_streams = required_labrecorder_streams(
                self._selected_eeg_stream,
                self._selected_marker_stream,
            )
            config_text = build_labrecorder_config(
                self.paths.raw,
                self._required_streams,
                rcs_port=self.rcs_port,
            )
            self._generated_config = config_text
            self.labrecorder_config.parent.mkdir(parents=True, exist_ok=True)
            self.labrecorder_config.write_text(config_text, encoding="utf-8")
            self._stdout_handle = self.labrecorder_stdout.open("ab")
            self._stderr_handle = self.labrecorder_stderr.open("ab")
            self._launch_command = [
                str(self._executable),
                "--config",
                str(self.labrecorder_config),
            ]
            self._process = subprocess.Popen(
                self._launch_command,
                cwd=str(self._executable.parent),
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                shell=False,
            )
            self._rcs_socket = self._connect_rcs()
            self._send("update")
            self._send(
                "filename "
                f"{{root:{_rcs_value(self.paths.raw)}}} "
                "{template:recording.xdf}"
            )
            self._send("start")
            self._wait_for_xdf_growth()
            self._status = "recording"
            self._write_metadata()
            return self.snapshot()
        except BaseException as exc:
            self._status = "failed"
            self._error = f"{type(exc).__name__}: {exc}"
            self._cleanup_failed_start()
            self._write_metadata()
            raise

    def snapshot(self) -> dict[str, Any]:
        mirror = self._mirror.snapshot()
        xdf_size = _file_size(self.paths.eeg_xdf)
        now = monotonic()
        if xdf_size > self._last_xdf_size:
            self._last_xdf_size = xdf_size
            self._last_xdf_growth_at = now
            if self._xdf_growth_warning_active:
                self._notes.append("LabRecorder XDF file growth resumed after a buffering interval")
            self._xdf_growth_warning_active = False
        if self._status == "recording":
            if self._process is None or self._process.poll() is not None:
                returncode = None if self._process is None else self._process.poll()
                self._fail(f"LabRecorder exited during acquisition with return code {returncode}")
            elif mirror.get("status") != "recording":
                self._fail(str(mirror.get("error") or f"CSV mirror status changed to {mirror.get('status')}"))
            elif now - self._last_xdf_growth_at > self.xdf_growth_warning_seconds:
                if not self._xdf_growth_warning_active:
                    self._xdf_growth_warning_active = True
                    self._xdf_growth_warning_count += 1
                    self._notes.append(
                        "LabRecorder XDF file size did not advance for "
                        f"{now - self._last_xdf_growth_at:.1f} seconds while the process and "
                        "source-preserving CSV/LSL mirror remained healthy; treating this as "
                        "buffering until final XDF validation"
                    )
        xdf_growth_status = (
            "buffering_warning"
            if self._xdf_growth_warning_active
            else ("finalized" if self._status == "stopped" else "advancing")
        )
        return {
            "status": self._status,
            "primary_format": "xdf",
            "raw_file": str(self.paths.eeg_xdf),
            "metadata_file": str(self.paths.xdf_metadata),
            "xdf_file": str(self.paths.eeg_xdf),
            "xdf_size_bytes": xdf_size,
            "xdf_seconds_since_growth": max(0.0, now - self._last_xdf_growth_at),
            "xdf_growth_status": xdf_growth_status,
            "xdf_growth_warning_count": self._xdf_growth_warning_count,
            "xdf_growth_warning_seconds": self.xdf_growth_warning_seconds,
            "tail_guard_seconds": self.tail_guard_seconds,
            "sample_count": int(mirror.get("sample_count") or 0),
            "first_lsl_timestamp": mirror.get("first_lsl_timestamp"),
            "last_lsl_timestamp": mirror.get("last_lsl_timestamp"),
            "stream": mirror.get("stream"),
            "csv_mirror": mirror,
            "labrecorder_pid": None if self._process is None else self._process.pid,
            "labrecorder_returncode": None if self._process is None else self._process.poll(),
            "labrecorder_executable": None if self._executable is None else str(self._executable),
            "labrecorder_executable_sha256": self._executable_sha256,
            "labrecorder_config": str(self.labrecorder_config),
            "labrecorder_config_contents": self._generated_config,
            "labrecorder_launch_command": list(self._launch_command),
            "required_streams": list(self._required_streams),
            "commands": list(self._commands),
            "stop_reason": self._stop_reason,
            "error": self._error,
            "notes": list(self._notes),
        }

    def is_alive(self) -> bool:
        return self.snapshot().get("status") == "recording"

    def stop(self, *, reason: str = "stop_requested") -> dict[str, Any]:
        self._stop_reason = reason
        failures: list[str] = []
        if self._process is not None and self._process.poll() is None:
            if self.tail_guard_seconds > 0:
                sleep(self.tail_guard_seconds)
                self._notes.append(
                    f"kept LabRecorder open for a {self.tail_guard_seconds:.3f}-second marker/EEG tail guard"
                )
            try:
                self._send("stop")
            except Exception as exc:
                failures.append(f"LabRecorder stop command failed: {type(exc).__name__}: {exc}")
            try:
                self._wait_for_xdf_settle()
            except Exception as exc:
                failures.append(f"XDF finalization failed: {type(exc).__name__}: {exc}")
        else:
            failures.append("LabRecorder was not running during recorder shutdown")
        try:
            mirror = self._mirror.stop()
            if mirror.get("status") != "stopped":
                failures.append(str(mirror.get("error") or f"CSV mirror stopped with {mirror.get('status')}"))
        except Exception as exc:
            failures.append(f"CSV mirror shutdown failed: {type(exc).__name__}: {exc}")
        self._close_rcs()
        self._close_process(failures)
        if failures or self._status == "failed":
            self._status = "failed"
            if failures:
                failure_text = "; ".join(failures)
                self._error = f"{self._error}; {failure_text}" if self._error else failure_text
        else:
            self._status = "stopped"
        summary = self.snapshot()
        self._write_metadata(summary)
        return summary

    def _start_mirror(self) -> None:
        self._mirror.start()
        timeout = float(self.eeg_config.get("stream_timeout_seconds", 5.0)) + 2.0
        if not self._mirror.wait_until_ready(timeout=timeout):
            self._mirror.stop()
            raise TimeoutError("CSV mirror did not become ready before the startup deadline")
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            snapshot = self._mirror.snapshot()
            if snapshot.get("status") != "recording":
                self._mirror.stop()
                raise RuntimeError(str(snapshot.get("error") or "CSV mirror did not begin recording"))
            if int(snapshot.get("sample_count") or 0) > 0:
                return
            sleep(0.05)
        self._mirror.stop()
        raise TimeoutError("CSV mirror connected but did not receive any EEG samples")

    def _connect_rcs(self) -> socket.socket:
        deadline = monotonic() + self.startup_timeout_seconds
        last_error: OSError | None = None
        while monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError(f"LabRecorder exited during startup with return code {self._process.poll()}")
            try:
                return socket.create_connection(("127.0.0.1", self.rcs_port), timeout=0.5)
            except OSError as exc:
                last_error = exc
                sleep(0.1)
        raise TimeoutError(
            f"LabRecorder remote control did not listen on 127.0.0.1:{self.rcs_port}: {last_error}"
        )

    def _send(self, command: str) -> None:
        if self._rcs_socket is None:
            raise RuntimeError("LabRecorder remote-control socket is unavailable")
        self._rcs_socket.sendall((command + "\n").encode("utf-8"))
        self._commands.append(command)

    def _wait_for_xdf_growth(self) -> None:
        deadline = monotonic() + self.startup_timeout_seconds
        initial_size = _file_size(self.paths.eeg_xdf)
        while monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError(f"LabRecorder exited during startup with return code {self._process.poll()}")
            size = _file_size(self.paths.eeg_xdf)
            if size > max(4, initial_size) and has_xdf_signature(self.paths.eeg_xdf):
                self._last_xdf_size = size
                self._last_xdf_growth_at = monotonic()
                return
            sleep(0.1)
        raise TimeoutError(f"LabRecorder did not create a growing XDF file at {self.paths.eeg_xdf}")

    def _wait_for_xdf_settle(self) -> None:
        deadline = monotonic() + self.shutdown_timeout_seconds
        previous: int | None = None
        stable_polls = 0
        structure_error: str | None = None
        while monotonic() < deadline:
            size = _file_size(self.paths.eeg_xdf)
            if size > 4 and size == previous:
                stable_polls += 1
                if stable_polls >= 4:
                    if not has_xdf_signature(self.paths.eeg_xdf):
                        raise OSError(f"invalid XDF signature: {self.paths.eeg_xdf}")
                    readable, structure_error = _xdf_is_structurally_readable(self.paths.eeg_xdf)
                    if readable:
                        self._last_xdf_size = size
                        self._last_xdf_growth_at = monotonic()
                        if self._xdf_growth_warning_active:
                            self._notes.append(
                                "LabRecorder finalized a structurally readable XDF after a buffered-write warning"
                            )
                        self._xdf_growth_warning_active = False
                        return
                    stable_polls = 0
            else:
                stable_polls = 0
            previous = size
            sleep(0.25)
        detail = "" if not structure_error else f"; last structural check: {structure_error}"
        raise TimeoutError(
            f"XDF file did not settle into a structurally readable recording within "
            f"{self.shutdown_timeout_seconds:.1f} seconds{detail}"
        )

    def _fail(self, error: str) -> None:
        self._status = "failed"
        self._error = error

    def _cleanup_failed_start(self) -> None:
        if self._rcs_socket is not None and self._process is not None and self._process.poll() is None:
            try:
                self._send("stop")
                if self.paths.eeg_xdf.exists():
                    self._wait_for_xdf_settle()
            except Exception as exc:
                self._notes.append(f"LabRecorder failed-start finalization failed: {type(exc).__name__}: {exc}")
        try:
            self._mirror.stop()
        except Exception as exc:
            self._notes.append(f"CSV mirror cleanup failed: {type(exc).__name__}: {exc}")
        self._close_rcs()
        failures: list[str] = []
        self._close_process(failures)
        self._notes.extend(failures)

    def _close_rcs(self) -> None:
        if self._rcs_socket is not None:
            try:
                self._rcs_socket.close()
            except OSError as exc:
                self._notes.append(f"RCS socket cleanup failed: {type(exc).__name__}: {exc}")
            finally:
                self._rcs_socket = None

    def _close_process(self, failures: list[str]) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=5.0)
                except Exception as exc:
                    failures.append(f"LabRecorder forced shutdown failed: {type(exc).__name__}: {exc}")
            except Exception as exc:
                failures.append(f"LabRecorder shutdown failed: {type(exc).__name__}: {exc}")
        for attribute in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, attribute)
            if handle is not None:
                try:
                    handle.close()
                except OSError as exc:
                    failures.append(f"LabRecorder log cleanup failed: {type(exc).__name__}: {exc}")
                finally:
                    setattr(self, attribute, None)

    def _write_metadata(self, summary: dict[str, Any] | None = None) -> None:
        payload = dict(summary or self.snapshot())
        payload.update(
            {
                "schema": "eegle.labrecorder_xdf.v1",
                "updated_elapsed_seconds": monotonic() - self._started_at,
                "selected_eeg_stream": dict(self._selected_eeg_stream),
                "marker_stream": dict(self._selected_marker_stream) or {
                    "name": self.marker_config.get("lsl_stream_name", "EEGleMarkers"),
                    "type": self.marker_config.get("lsl_stream_type", "Markers"),
                    "source_id": self.marker_config.get("source_id"),
                },
            }
        )
        _write_json_atomic(self.paths.xdf_metadata, payload)


def resolve_labrecorder_executable(configured: str) -> Path:
    value = os.path.expandvars(str(configured)).strip()
    if not value:
        raise FileNotFoundError("processes.recorder.executable is empty")
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"LabRecorder executable does not exist: {resolved}")
        return resolved
    discovered = shutil.which(value)
    if discovered is None:
        raise FileNotFoundError(
            f"LabRecorder executable '{value}' was not found; configure an absolute "
            "processes.recorder.executable path or add it to PATH"
        )
    return Path(discovered).resolve()


def labrecorder_environment(config: dict[str, Any]) -> dict[str, Any]:
    """Validate managed-XDF dependencies without launching LabRecorder."""
    recorder = dict(config.get("processes", {}).get("recorder", {}) or {})
    executable = resolve_labrecorder_executable(str(recorder.get("executable") or "LabRecorder.exe"))
    try:
        version = importlib.metadata.version("pyxdf")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("pyxdf is not installed; install the runtime dependencies") from exc
    if not _version_at_least(version, (1, 17, 5)):
        raise RuntimeError(f"pyxdf {version} is installed; Study 1 requires pyxdf>=1.17.5,<2")
    if _version_at_least(version, (2, 0, 0)):
        raise RuntimeError(f"pyxdf {version} is outside the supported range pyxdf>=1.17.5,<2")
    port = int(recorder.get("rcs_port", 22345))
    require_loopback_port_available(port)
    return {
        "executable": str(executable),
        "executable_sha256": sha256_file(executable),
        "pyxdf_version": version,
        "rcs_host": "127.0.0.1",
        "rcs_port": port,
    }


def require_loopback_port_available(port: int) -> None:
    if not 1 <= int(port) <= 65535:
        raise ValueError("processes.recorder.rcs_port must be between 1 and 65535")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", int(port)))
    except OSError as exc:
        raise OSError(
            f"LabRecorder remote-control port 127.0.0.1:{port} is already in use. "
            "Close every manually started LabRecorder instance (and any stale EEGle LabRecorder process), "
            "then rerun the preflight."
        ) from exc
    finally:
        probe.close()


def required_labrecorder_streams(
    eeg_stream: dict[str, Any],
    marker_stream: dict[str, Any],
) -> list[str]:
    eeg_name = _required_stream_value(str(eeg_stream.get("name") or ""), "EEG stream name")
    eeg_required = _required_stream_label(eeg_name, eeg_stream.get("hostname"), "EEG")
    marker_name = _required_stream_value(
        str(marker_stream.get("name") or ""),
        "marker stream name",
    )
    marker_required = _required_stream_label(
        marker_name,
        marker_stream.get("hostname"),
        "marker",
    )
    return [eeg_required, marker_required]


def resolve_labrecorder_marker_stream(
    marker_config: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Resolve the already-running marker outlet to an exact LabRecorder row."""
    source_id = str(marker_config.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("managed XDF recording requires hardware.markers.source_id")
    expected_name = str(marker_config.get("lsl_stream_name", "EEGleMarkers"))
    expected_type = str(marker_config.get("lsl_stream_type", "Markers"))
    try:
        import pylsl
    except Exception as exc:
        raise RuntimeError(f"pylsl is required to resolve the marker stream: {exc}") from exc
    infos = pylsl.resolve_byprop(
        "source_id",
        source_id,
        minimum=1,
        timeout=max(0.1, float(timeout_seconds)),
    )
    matches = [
        info
        for info in infos
        if str(info.name()) == expected_name and str(info.type()) == expected_type
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one pre-started marker stream for source_id={source_id}, "
            f"name={expected_name!r}, type={expected_type!r}; found {len(matches)}"
        )
    info = matches[0]
    return {
        "name": str(info.name()),
        "type": str(info.type()),
        "source_id": str(info.source_id()),
        "hostname": str(info.hostname()),
        "uid": str(info.uid()),
    }


def build_labrecorder_config(raw_dir: Path, required_streams: list[str], *, rcs_port: int) -> str:
    root = _config_value(raw_dir)
    required = ",".join(f'"{_config_string(value)}"' for value in required_streams)
    return "\n".join(
        (
            f"StudyRoot={root}",
            "PathTemplate=recording.xdf",
            f"RequiredStreams={required}",
            "RCSEnabled=1",
            f"RCSPort={int(rcs_port)}",
            "",
        )
    )


def has_xdf_signature(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"XDF:"
    except OSError:
        return False


def _xdf_is_structurally_readable(path: Path) -> tuple[bool, str | None]:
    """Scan finalized chunks with bounded retained samples before killing LabRecorder."""
    try:
        import pyxdf
    except Exception as exc:
        return False, f"PyXDF import failed: {type(exc).__name__}: {exc}"

    messages: list[str] = []

    class _ErrorCollector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR:
                messages.append(record.getMessage())

    def retain_one_sample(
        values: Any,
        stamps: Any,
        info: dict[str, Any],
        stream_id: int,
    ) -> tuple[Any, Any, dict[str, Any]]:
        return values[:1], stamps[:1], info

    logger = logging.getLogger("pyxdf.pyxdf")
    handler = _ErrorCollector()
    logger.addHandler(handler)
    try:
        streams, _ = pyxdf.load_xdf(
            str(path),
            on_chunk=retain_one_sample,
            synchronize_clocks=False,
            dejitter_timestamps=False,
            verbose=False,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        logger.removeHandler(handler)
    if messages:
        return False, "; ".join(messages[:3])
    if not streams:
        return False, "PyXDF found no streams"
    return True, None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _required_stream_value(value: str, label: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError(f"{label} is empty")
    if any(character in cleaned for character in ('"', "\n", "\r")):
        raise ValueError(f"{label} contains unsupported configuration characters")
    return cleaned


def _required_stream_label(name: str, hostname: Any, kind: str) -> str:
    host = str(hostname or "").strip()
    if not host:
        return name
    return f"{name} ({_required_stream_value(host, f'{kind} hostname')})"


def _config_string(value: str) -> str:
    return _required_stream_value(value, "required stream")


def _config_value(path: Path) -> str:
    value = path.expanduser().resolve().as_posix()
    if any(character in value for character in ("\n", "\r")):
        raise ValueError("LabRecorder storage path contains a newline")
    return value


def _rcs_value(path: Path) -> str:
    value = _config_value(path)
    if "}" in value:
        raise ValueError("LabRecorder storage path contains an unsupported '}' character")
    return value


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _version_at_least(value: str, minimum: tuple[int, int, int]) -> bool:
    parts = [int(token) for token in re.findall(r"\d+", str(value))[:3]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3]) >= minimum


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        last_error: OSError | None = None
        for delay in (0.0, 0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8):
            if delay:
                sleep(delay)
            try:
                temporary.replace(path)
                return
            except OSError as exc:
                if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in {5, 32, 33}:
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
    finally:
        temporary.unlink(missing_ok=True)
