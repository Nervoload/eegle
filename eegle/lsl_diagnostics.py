"""Standalone LSL diagnostics for separating local-stack and device-outlet failures."""

from __future__ import annotations

import argparse
import configparser
import importlib.metadata
import json
import os
import platform
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


DIAGNOSTIC_SCHEMA = "eegle.lsl_diagnostic.v1"
LOOPBACK_NAME = "EEGle-LSL-Diagnostic"
LOOPBACK_TYPE = "EEGleDiagnostic"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test pylsl across processes and enumerate every visible LSL stream",
    )
    config = parser.add_mutually_exclusive_group()
    config.add_argument("--lsl-config", default=None, help="Set LSLAPICFG before importing pylsl")
    config.add_argument(
        "--ignore-lsl-config",
        action="store_true",
        help="Remove LSLAPICFG and use normal liblsl configuration discovery",
    )
    parser.add_argument("--wait", type=float, default=5.0)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--output", default=None)
    parser.add_argument("--label", default=None)
    parser.add_argument("--serve-loopback", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--duration", type=float, default=12.0, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _apply_lsl_config(args.lsl_config, bool(args.ignore_lsl_config))
    if args.serve_loopback:
        return _serve_loopback(str(args.source_id or ""), float(args.duration))
    report = run_diagnostics(
        wait_seconds=max(0.5, float(args.wait)),
        rounds=max(1, int(args.rounds)),
        label=args.label,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        _write_json_atomic(Path(args.output), report)
    return 0 if report["loopback"].get("status") == "pass" else 1


def run_diagnostics(
    *,
    wait_seconds: float,
    rounds: int,
    label: str | None,
) -> dict[str, Any]:
    started = time.time()
    environment = _environment_snapshot(label)
    try:
        import pylsl
    except Exception as exc:
        loopback = {
            "status": "fail",
            "error": f"pylsl import failed: {type(exc).__name__}: {exc}",
        }
        streams: list[dict[str, Any]] = []
        pylsl_snapshot: dict[str, Any] = {"importable": False}
    else:
        pylsl_snapshot = _pylsl_snapshot(pylsl)
        loopback = _cross_process_loopback(pylsl, wait_seconds=wait_seconds)
        streams = _discover_streams(pylsl, wait_seconds=wait_seconds, rounds=rounds)
    verdict = diagnostic_verdict(loopback, streams)
    return {
        "schema": DIAGNOSTIC_SCHEMA,
        "label": label,
        "status": verdict["status"],
        "verdict": verdict,
        "environment": environment,
        "pylsl": pylsl_snapshot,
        "loopback": loopback,
        "stream_count": len(streams),
        "streams": streams,
        "elapsed_seconds": time.time() - started,
    }


def diagnostic_verdict(
    loopback: dict[str, Any],
    streams: list[dict[str, Any]],
) -> dict[str, Any]:
    if loopback.get("status") != "pass":
        return {
            "status": "fail",
            "code": "local_lsl_interprocess_failure",
            "detail": (
                "The local pylsl installation could not discover/read an outlet hosted by a second "
                "Python process. Repair the local LSL stack or its Windows firewall/configuration first."
            ),
        }
    eeg_streams = [
        stream for stream in streams if str(stream.get("type", "")).strip().lower() == "eeg"
    ]
    if eeg_streams:
        return {
            "status": "pass",
            "code": "external_eeg_stream_visible",
            "detail": f"The local LSL stack works and {len(eeg_streams)} EEG stream(s) are visible.",
        }
    if streams:
        return {
            "status": "warning",
            "code": "lsl_visible_without_eeg",
            "detail": (
                f"The local LSL stack works and {len(streams)} external stream(s) are visible, "
                "but none has type EEG."
            ),
        }
    return {
        "status": "warning",
        "code": "local_lsl_ok_no_external_streams",
        "detail": (
            "The local LSL stack works across processes, but no external outlet is visible. "
            "Collect is likely not publishing an LSL outlet, or its liblsl configuration/session is isolated."
        ),
    }


def _apply_lsl_config(config_path: str | None, ignore: bool) -> None:
    if ignore:
        os.environ.pop("LSLAPICFG", None)
    elif config_path:
        os.environ["LSLAPICFG"] = str(Path(config_path).expanduser().resolve())


def _environment_snapshot(label: str | None) -> dict[str, Any]:
    config_path = os.environ.get("LSLAPICFG")
    return {
        "label": label,
        "python": sys.executable,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "hostname": socket.gethostname(),
        "host_addresses": _host_addresses(),
        "lsl_api_config_env": config_path,
        "lsl_api_config": _lsl_config_snapshot(config_path),
    }


def _host_addresses() -> list[str]:
    try:
        values = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        return []
    return sorted({str(value[4][0]) for value in values if value[4]})


def _lsl_config_snapshot(config_path: str | None) -> dict[str, Any]:
    if not config_path:
        return {"mode": "normal_liblsl_search", "path": None, "exists": None}
    path = Path(config_path).expanduser()
    result: dict[str, Any] = {
        "mode": "explicit_lslapicfg",
        "path": str(path),
        "exists": path.is_file(),
    }
    if not path.is_file():
        return result
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["ports"] = {
        "multicast_port": parser.get("ports", "MulticastPort", fallback=None),
        "base_port": parser.get("ports", "BasePort", fallback=None),
        "port_range": parser.get("ports", "PortRange", fallback=None),
        "ipv6": parser.get("ports", "IPv6", fallback=None),
    }
    result["multicast"] = {
        "resolve_scope": parser.get("multicast", "ResolveScope", fallback=None),
        "listen_address": parser.get("multicast", "ListenAddress", fallback=None),
    }
    result["lab"] = {
        "session_id": parser.get("lab", "SessionID", fallback="default (implicit)"),
        "known_peers": parser.get("lab", "KnownPeers", fallback="{} (implicit)"),
    }
    return result


def _pylsl_snapshot(pylsl: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"importable": True}
    try:
        result["package_version"] = importlib.metadata.version("pylsl")
    except importlib.metadata.PackageNotFoundError:
        result["package_version"] = getattr(pylsl, "__version__", "unknown")
    for name in ("library_version", "protocol_version"):
        function = getattr(pylsl, name, None)
        if callable(function):
            try:
                result[name] = function()
            except Exception as exc:
                result[f"{name}_error"] = f"{type(exc).__name__}: {exc}"
    try:
        from pylsl import library

        path = getattr(library, "libpath", None)
        if callable(path):
            path = path()
        result["native_library"] = None if path is None else str(path)
    except Exception:
        result["native_library"] = None
    return result


def _cross_process_loopback(pylsl: Any, *, wait_seconds: float) -> dict[str, Any]:
    source_id = f"eegle-lsl-diagnostic-{uuid.uuid4()}"
    command = [
        sys.executable,
        "-m",
        "eegle.lsl_diagnostics",
        "--serve-loopback",
        "--source-id",
        source_id,
        "--duration",
        str(max(8.0, wait_seconds + 3.0)),
    ]
    process = subprocess.Popen(
        command,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
    )
    started = time.monotonic()
    try:
        time.sleep(1.0)
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1.0)
            return {
                "status": "fail",
                "stage": "outlet_process_start",
                "source_id": source_id,
                "child_returncode": process.returncode,
                "child_stdout": stdout[-2000:],
                "child_stderr": stderr[-2000:],
            }
        infos = pylsl.resolve_byprop(
            "source_id",
            source_id,
            minimum=1,
            timeout=wait_seconds,
        )
        if len(infos) != 1:
            return {
                "status": "fail",
                "stage": "cross_process_discovery",
                "source_id": source_id,
                "resolved_count": len(infos),
            }
        inlet = pylsl.StreamInlet(infos[0], max_buflen=1)
        deadline = time.monotonic() + wait_seconds
        sample = None
        timestamp = None
        while time.monotonic() < deadline and timestamp is None:
            sample, timestamp = inlet.pull_sample(timeout=min(0.5, max(0.0, deadline - time.monotonic())))
        if timestamp is None:
            return {
                "status": "fail",
                "stage": "cross_process_sample_read",
                "source_id": source_id,
                "resolved_count": len(infos),
            }
        return {
            "status": "pass",
            "stage": "complete",
            "source_id": source_id,
            "stream": _stream_info(infos[0]),
            "sample": sample,
            "timestamp": timestamp,
            "elapsed_seconds": time.monotonic() - started,
            "child_command": command,
        }
    except Exception as exc:
        return {
            "status": "fail",
            "stage": "exception",
            "source_id": source_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=2.0)


def _serve_loopback(source_id: str, duration: float) -> int:
    if not source_id.strip():
        return 2
    import pylsl

    info = pylsl.StreamInfo(
        LOOPBACK_NAME,
        LOOPBACK_TYPE,
        1,
        20.0,
        "float32",
        source_id,
    )
    outlet = pylsl.StreamOutlet(info)
    deadline = time.monotonic() + max(2.0, duration)
    sample = 0.0
    while time.monotonic() < deadline:
        outlet.push_sample([sample])
        sample += 1.0
        time.sleep(0.05)
    return 0


def _discover_streams(pylsl: Any, *, wait_seconds: float, rounds: int) -> list[dict[str, Any]]:
    discovered: dict[tuple[str, ...], dict[str, Any]] = {}
    per_round_wait = max(0.25, wait_seconds / rounds)
    for _ in range(rounds):
        infos = pylsl.resolve_streams(wait_time=per_round_wait)
        for info in infos:
            row = _stream_info(info)
            if (
                str(row.get("type", "")) == LOOPBACK_TYPE
                or str(row.get("source_id", "")).startswith("eegle-lsl-diagnostic-")
            ):
                # The short-lived loopback outlet can remain in liblsl's resolver cache
                # briefly after its child process exits. It is not an external stream.
                continue
            key = (
                str(row.get("uid") or ""),
                str(row.get("source_id") or ""),
                str(row.get("hostname") or ""),
                str(row.get("name") or ""),
                str(row.get("type") or ""),
            )
            discovered[key] = row
    return sorted(
        discovered.values(),
        key=lambda row: (str(row.get("type", "")), str(row.get("name", ""))),
    )


def _stream_info(info: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in (
        "name",
        "type",
        "channel_count",
        "nominal_srate",
        "channel_format",
        "source_id",
        "hostname",
        "uid",
        "session_id",
        "version",
        "created_at",
    ):
        getter = getattr(info, field, None)
        if not callable(getter):
            continue
        try:
            values[field] = getter()
        except Exception as exc:
            values[f"{field}_error"] = f"{type(exc).__name__}: {exc}"
    return values


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(target)


if __name__ == "__main__":
    raise SystemExit(main())
