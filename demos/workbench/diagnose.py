"""Headless Workbench self-test for a host that has not run the demo before.

Every check runs the same code the GUI runs, in the same order, without Qt.
A host problem therefore surfaces here with a full stack instead of as a
one-line banner inside the application. Checks are independent: a missing
optional dependency does not stop the deterministic simulation path from
being verified.

    python -m demos.workbench.diagnose
    python -m demos.workbench.diagnose --skip-simulation
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from demos.workbench.platform_support import (
    DATA_ROOT_ENV_VAR,
    child_environment,
    child_interpreter,
    default_data_root,
    discovery_remediation,
    project_root_length_risk,
)

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    summary: str
    details: tuple[str, ...] = ()
    stack: str | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def record(
        self,
        name: str,
        operation: Callable[[], tuple[str, str, tuple[str, ...]]],
    ) -> Check:
        try:
            status, summary, details = operation()
            check = Check(name, status, summary, details)
        except BaseException as exc:  # noqa: BLE001 - a diagnostic reports everything
            check = Check(
                name,
                FAIL,
                f"{type(exc).__name__}: {exc}",
                stack="".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            )
        self.checks.append(check)
        _print(check)
        return check

    @property
    def failed(self) -> bool:
        return any(check.status == FAIL for check in self.checks)


_MARK = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}


def _print(check: Check) -> None:
    print(f"[{_MARK[check.status]}] {check.name}: {check.summary}")
    for line in check.details:
        print(f"       - {line}")
    if check.stack:
        print()
        for line in check.stack.rstrip().splitlines():
            print(f"       {line}")
        print()
    sys.stdout.flush()


def check_host() -> tuple[str, str, tuple[str, ...]]:
    details = [
        f"python {sys.version.split()[0]} ({platform.python_implementation()})",
        f"platform {platform.platform()}",
        f"executable {sys.executable}",
        f"child interpreter {child_interpreter()}",
        f"stdout encoding {sys.stdout.encoding}",
        f"filesystem encoding {sys.getfilesystemencoding()}",
    ]
    override = os.environ.get(DATA_ROOT_ENV_VAR, "").strip()
    details.append(
        f"{DATA_ROOT_ENV_VAR}={override}" if override else f"{DATA_ROOT_ENV_VAR} unset"
    )
    return PASS, "host interpreter and encodings reported", tuple(details)


def check_qt() -> tuple[str, str, tuple[str, ...]]:
    try:
        import PySide6
        from PySide6 import QtWidgets
    except Exception as exc:  # noqa: BLE001 - optional GUI dependency
        return (
            WARN,
            f"PySide6 is not importable: {type(exc).__name__}: {exc}",
            (
                (
                    "The GUI cannot start without it; the simulation path "
                    "below does not need it."
                ),
                (
                    "Install it with: python -m pip install -r "
                    "demos/workbench/requirements.txt"
                ),
            ),
        )
    details = [f"PySide6 {PySide6.__version__}", f"QtWidgets {QtWidgets.__name__}"]
    try:
        import pyqtgraph

        details.append(f"pyqtgraph {pyqtgraph.__version__}")
    except Exception as exc:  # noqa: BLE001 - optional preview dependency
        return (
            WARN,
            f"PySide6 {PySide6.__version__} present, pyqtgraph is not",
            (f"{type(exc).__name__}: {exc}", "The live waveform preview stays empty."),
        )
    return PASS, "GUI dependencies are importable", tuple(details)


def check_lsl(wait_seconds: float | None) -> tuple[str, str, tuple[str, ...]]:
    from demos.workbench.operations import lsl_scan_wait_seconds
    from eegle.integrations.lsl import detect_lsl, probe_lsl_dependency

    probe = probe_lsl_dependency()
    if not probe.dependency_available:
        return (
            WARN,
            f"the native LSL library did not load: {probe.unavailable_reason}",
            (
                *probe.remediation,
                (
                    "Live LSL is unavailable until this loads. Simulation "
                    "does not use it."
                ),
            ),
        )
    wait = lsl_scan_wait_seconds() if wait_seconds is None else float(wait_seconds)
    detection = detect_lsl(wait_time=wait)
    if not detection.streams:
        return (
            WARN,
            (
                f"pylsl {probe.library_version} loaded; no streams resolved in "
                f"{wait:g} s"
            ),
            discovery_remediation(),
        )
    details = [
        (
            f"{stream.name} · type={stream.stream_type} · "
            f"{stream.channel_count} values · "
            f"{stream.nominal_rate_hz:g} Hz · {stream.channel_format} · "
            f"host={stream.hostname}"
        )
        for stream in detection.streams
    ]
    return (
        PASS,
        f"pylsl {probe.library_version} resolved {len(detection.streams)} stream(s)",
        tuple(details),
    )


def check_profile() -> tuple[str, str, tuple[str, ...]]:
    from demos.workbench.profile import load_study1_profile

    profile = load_study1_profile()
    return (
        PASS,
        f"prepared profile {profile.project_id} validated",
        (
            f"digest {profile.digest}",
            f"default variant {profile.default_variant}",
            f"{len(profile.logical_channels)} logical channels",
        ),
    )


def check_paths(project_root: Path) -> tuple[str, str, tuple[str, ...]]:
    risk = project_root_length_risk(project_root)
    if risk is None:
        return (
            PASS,
            "evidence paths stay inside every supported host limit",
            (f"project root {project_root}",),
        )
    return WARN, risk, (f"project root {project_root}",)


def check_simulation(project_root: Path) -> tuple[str, str, tuple[str, ...]]:
    """Run the exact deterministic path the Run page drives."""

    from demos.workbench.operations import WorkbenchOperations
    from demos.workbench.profile import load_study1_profile
    from demos.workbench.project import bootstrap_study1_project
    from eegle.operations import rehearse_project

    profile = load_study1_profile()
    bootstrap = bootstrap_study1_project(profile, project_root=project_root)
    operations = WorkbenchOperations()
    build = operations.compile_and_preflight(project_root, None, simulation=True)
    if not build.preflight.ready:
        blocking = [
            f"{check.check_id}: {check.summary}"
            for check in build.checks
            if check.status == "fail"
        ]
        return FAIL, "simulation preflight is not ready", tuple(blocking)

    run = rehearse_project(project_root, session_id="study1.simulation.diagnose")
    payload = run.to_payload()
    if not run.successful:
        return (
            FAIL,
            f"rehearsal ended {payload['status']}",
            (f"terminal reason {payload.get('terminal_reason')}",),
        )

    sessions = operations.inspect_project_sessions(project_root)
    if not sessions:
        return FAIL, "the finished rehearsal registered no session", ()
    session = sessions[0]
    details = [
        f"project {bootstrap.snapshot.project_id}",
        f"plan {build.compilation.plan.plan_hash}",
        f"bundle {payload['bundle_id']}",
        f"{payload['evidence_record_count']} evidence records",
        f"session valid={session.valid} status={session.session_status}",
    ]
    if not session.valid:
        return (
            FAIL,
            "the recorded session did not inspect as valid",
            (*details, *(str(issue) for issue in session.issues)),
        )
    if session.replay_ready:
        replay = operations.replay_session(session)
        details.append(
            f"replay equivalent={replay.equivalent} "
            f"level={replay.evaluated_level} "
            f"records={replay.compared_record_count}"
        )
        if not replay.equivalent:
            return (
                FAIL,
                "replay did not reproduce equivalent evidence",
                (*details, f"first divergence {replay.first_divergence}"),
            )
    return PASS, "compile, preflight, rehearse, inspect, replay all passed", tuple(details)


def check_child_protocol(project_root: Path) -> tuple[str, str, tuple[str, ...]]:
    """Verify the supervised runner speaks NDJSON over real pipes on this host.

    The GUI drives this boundary through QProcess. Running it with the same
    interpreter and environment catches the host-specific failures (no console
    streams, code page mangling, buffered pipes) without needing a display.
    """

    import json
    import subprocess

    process = subprocess.Popen(
        [
            child_interpreter(),
            "-X",
            "utf8",
            "-m",
            "demos.workbench.runner",
            "--project-root",
            str(project_root),
            "--session-id",
            "study1.simulation.protocol",
            "--mode",
            "simulation",
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    messages: list[dict[str, object]] = []
    bad_line: str | None = None
    try:
        # stdin stays open for the whole run, as it does under QProcess.
        # Closing it early makes the runner cancel, which would report a
        # cancelled session instead of exercising normal completion.
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            json.dumps(
                {"schema": "eegle.workbench.runner_protocol.v1", "command": "start"}
            )
            + "\n"
        )
        process.stdin.flush()
        for line in process.stdout:
            if not line.strip():
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                bad_line = line
                break
            if messages[-1].get("type") in {"completed", "failed"}:
                break
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            stderr = process.communicate(timeout=60)[1] or ""
        except subprocess.TimeoutExpired:
            process.kill()
            stderr = process.communicate()[1] or ""
    if bad_line is not None:
        return (
            FAIL,
            "the runner emitted a line that is not valid JSON",
            (f"line {bad_line[:200]!r}", f"stderr {stderr[-600:]}"),
        )
    kinds = [str(value.get("type")) for value in messages]
    if not messages:
        return (
            FAIL,
            "the runner produced no protocol messages",
            (
                f"exit code {process.returncode}",
                f"stderr {stderr[-600:] or '<empty>'}",
            ),
        )
    terminal = messages[-1]
    if terminal.get("type") != "completed":
        error = dict(terminal.get("error") or {})
        return (
            FAIL,
            f"the runner ended with {terminal.get('type')}: {error.get('message')}",
            (
                f"messages {kinds}",
                *((error["traceback"],) if error.get("traceback") else ()),
                f"stderr {stderr[-600:] or '<empty>'}",
            ),
        )
    run = dict(terminal.get("project_run") or {})
    return (
        PASS,
        "the supervised runner completed over real pipes",
        (
            f"messages {kinds}",
            f"status {run.get('status')}",
            f"{run.get('evidence_record_count')} evidence records",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that this host can run the EEGle Workbench demo",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project to use for the simulation checks. Defaults to a "
        "temporary directory that is removed afterwards.",
    )
    parser.add_argument(
        "--skip-simulation",
        action="store_true",
        help="Report the host and LSL checks only.",
    )
    parser.add_argument(
        "--lsl-wait-seconds",
        type=float,
        default=None,
        help="Override the LSL discovery wait for this run.",
    )
    args = parser.parse_args(argv)

    print("EEGle Workbench host diagnosis")
    print("=" * 62)
    report = Report()
    report.record("host", check_host)
    report.record("gui dependencies", check_qt)
    report.record("lsl", lambda: check_lsl(args.lsl_wait_seconds))
    report.record("prepared profile", check_profile)

    temporary: str | None = None
    if args.skip_simulation:
        print("\nSimulation checks skipped by request.")
    else:
        if args.project_root is None:
            temporary = tempfile.mkdtemp(prefix="eegle-workbench-diagnose-")
            project_root = Path(temporary) / "study1-neuracle64-demo"
        else:
            project_root = args.project_root.expanduser().resolve()
        try:
            # The scratch project is temporary; the operator cares about the
            # location Workbench will actually record into.
            report.record(
                "evidence path length",
                lambda: check_paths(default_data_root() / "study1-neuracle64-demo"),
            )
            report.record("simulation path", lambda: check_simulation(project_root))
            report.record(
                "supervised runner protocol",
                lambda: check_child_protocol(project_root),
            )
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)

    print("=" * 62)
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for check in report.checks:
        counts[check.status] += 1
    print(
        f"{counts[PASS]} passed, {counts[WARN]} warning(s), {counts[FAIL]} failure(s)"
    )
    if report.failed:
        print(
            "\nA failure above is a real host problem. The stack shows the "
            "layer that failed."
        )
        return 1
    if counts[WARN]:
        print(
            "\nWarnings do not block the deterministic simulation demo. Live "
            "LSL recording needs the warned items resolved."
        )
    default_root = default_data_root()
    print(f"\nWorkbench will store projects under: {default_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
