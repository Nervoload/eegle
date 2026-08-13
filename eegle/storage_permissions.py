"""Recording-storage permission probe for constrained Windows endpoints."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from time import monotonic_ns, sleep
from typing import Any


STORAGE_PROBE_SCHEMA = "eegle.recording_storage_probe.v1"
_CLEANUP_RETRY_SECONDS = (0.0, 0.05, 0.15, 0.4, 0.8)


def probe_recording_storage(
    output_root: str | Path,
    *,
    include_child_process: bool = True,
    child_timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Exercise the operations used across baseline-to-task transitions.

    A top-level write is insufficient on application-controlled endpoints.  The
    real experiment creates nested session trees, appends ledgers, atomically
    replaces manifests, and launches a fresh Python process for the visual task.
    This probe performs all of those operations in a disposable directory.
    """

    root = Path(output_root).expanduser().resolve()
    token = f"{os.getpid()}-{monotonic_ns()}"
    probe_parent = root / ".eegle_recording_storage_probe"
    probe_root = probe_parent / token
    failures: list[str] = []
    parent_operations: dict[str, Any] | None = None
    child: dict[str, Any] = {
        "status": "skipped",
        "reason": "child-process probe disabled",
    }
    cleanup_error: str | None = None
    try:
        parent_operations = _exercise_storage_operations(probe_root, actor="parent")
        if include_child_process:
            request_path = probe_root / "phase_workers" / "task.request.json"
            _atomic_json_write(
                request_path,
                {
                    "schema": STORAGE_PROBE_SCHEMA,
                    "probe_root": str(probe_root),
                    "parent_pid": os.getpid(),
                },
            )
            child = _run_child_probe(
                probe_root,
                request_path,
                timeout_seconds=max(1.0, float(child_timeout_seconds)),
            )
            if child.get("status") != "ok":
                failures.append(str(child.get("error") or "fresh child Python process could not update the data root"))
    except BaseException as exc:
        failures.append(_exception_detail("parent storage operations", exc, probe_root))
    finally:
        cleanup_error = _remove_probe_tree(probe_root)
        if cleanup_error:
            failures.append(cleanup_error)
        try:
            probe_parent.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Another concurrent probe or a policy-created remnant may keep the
            # shared parent directory.  Only failure to remove our unique tree
            # is significant.
            pass
    return {
        "schema": STORAGE_PROBE_SCHEMA,
        "status": "fail" if failures else "ok",
        "output_root": str(root),
        "probe_root": str(probe_root),
        "user": getpass.getuser(),
        "platform": sys.platform,
        "python_executable": sys.executable,
        "parent_pid": os.getpid(),
        "windows_is_admin": _windows_is_admin(),
        "parent_operations": parent_operations,
        "child_process": child,
        "cleanup_error": cleanup_error,
        "failures": failures,
    }


def _exercise_storage_operations(probe_root: Path, *, actor: str) -> dict[str, Any]:
    session = probe_root / "participants" / "permission-probe" / "sessions" / "date" / "experiment" / "task" / "run"
    raw = session / "raw"
    events = session / "events"
    process_logs = session / "logs" / "processes"
    phase_workers = probe_root / "phase_workers"
    visit = probe_root / "study1" / "permission-probe" / "visits" / "visit-1" / "probe"
    for folder in (raw, events, process_logs, phase_workers, visit):
        folder.mkdir(parents=True, exist_ok=True)

    xdf = raw / f"{actor}-recording.xdf"
    with xdf.open("wb") as handle:
        handle.write(b"EEGle permission probe\n")
        handle.flush()
        os.fsync(handle.fileno())

    ledger = events / f"{actor}-events.jsonl"
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"actor": actor, "event": 1}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"actor": actor, "event": 2}) + "\n")

    manifest = visit / f"{actor}-visit-manifest.json"
    _atomic_json_write(manifest, {"actor": actor, "revision": 1})
    _atomic_json_write(manifest, {"actor": actor, "revision": 2})
    if json.loads(manifest.read_text(encoding="utf-8")).get("revision") != 2:
        raise OSError(f"atomic manifest replacement could not be read back: {manifest}")

    rename_source = process_logs / f"{actor}.partial"
    rename_target = process_logs / f"{actor}.complete"
    rename_source.write_text("complete\n", encoding="utf-8")
    rename_source.replace(rename_target)
    rename_target.unlink()

    result = phase_workers / f"{actor}.result.json"
    _atomic_json_write(result, {"status": "ok", "actor": actor, "pid": os.getpid()})
    return {
        "status": "ok",
        "actor": actor,
        "pid": os.getpid(),
        "session_tree": str(session),
        "append_file": str(ledger),
        "atomic_replace_file": str(manifest),
        "result_file": str(result),
    }


def _run_child_probe(probe_root: Path, request_path: Path, *, timeout_seconds: float) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "eegle.storage_permissions",
        "--child-probe-root",
        str(probe_root),
        "--request",
        str(request_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except BaseException as exc:
        return {
            "status": "fail",
            "command": command,
            "error": _exception_detail("launching fresh child Python process", exc, probe_root),
        }
    stdout = completed.stdout.strip()
    payload: dict[str, Any] = {}
    if stdout:
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
    expected_result = probe_root / "phase_workers" / "child.result.json"
    ok = completed.returncode == 0 and payload.get("status") == "ok" and expected_result.is_file()
    error = None
    if not ok:
        child_detail = payload.get("error") or completed.stderr.strip() or stdout or "no child result was produced"
        error = (
            "fresh child Python process failed the recording-root transition probe: "
            f"return_code={completed.returncode}; executable={sys.executable}; root={probe_root}; {child_detail}"
        )
    return {
        **payload,
        "status": "ok" if ok else "fail",
        "command": command,
        "return_code": completed.returncode,
        "stdout": "" if ok else stdout[-4000:],
        "stderr": "" if ok else completed.stderr.strip()[-4000:],
        "expected_result_file": str(expected_result),
        "error": error,
    }


def _run_child_operations(probe_root: Path, request_path: Path) -> dict[str, Any]:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("schema") != STORAGE_PROBE_SCHEMA:
        raise ValueError(f"unexpected storage-probe request schema: {request.get('schema')}")
    if Path(str(request.get("probe_root"))).resolve() != probe_root.resolve():
        raise ValueError("storage-probe request root does not match the child command")
    return _exercise_storage_operations(probe_root, actor="child")


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{monotonic_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_probe_tree(path: Path) -> str | None:
    if not path.exists():
        return None
    last_error: OSError | None = None
    for delay in _CLEANUP_RETRY_SECONDS:
        if delay:
            sleep(delay)
        try:
            shutil.rmtree(path)
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            last_error = exc
    if last_error is None:
        return None
    return _exception_detail("removing the disposable storage probe", last_error, path)


def _exception_detail(operation: str, exc: BaseException, path: Path) -> str:
    winerror = getattr(exc, "winerror", None)
    suffix = "" if winerror is None else f"; winerror={winerror}"
    return f"{operation} failed at {path}: {type(exc).__name__}: {exc}{suffix}"


def _windows_is_admin() -> bool | None:
    if os.name != "nt":
        return None
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Test EEGle recording storage, including a fresh child Python process",
    )
    parser.add_argument("--root", default=None)
    parser.add_argument("--child-probe-root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--request", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child_probe_root:
        try:
            result = _run_child_operations(
                Path(args.child_probe_root).expanduser().resolve(),
                Path(str(args.request)).expanduser().resolve(),
            )
        except BaseException as exc:
            result = {
                "schema": STORAGE_PROBE_SCHEMA,
                "status": "fail",
                "error": _exception_detail(
                    "child storage operations",
                    exc,
                    Path(args.child_probe_root).expanduser().resolve(),
                ),
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") == "ok" else 1
    if not args.root:
        parser.error("--root is required")
    result = probe_recording_storage(args.root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
