"""Best-effort progress reporting for an isolated visual task process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from eegle.workers.common import StatusWriter


DSART_PHASE_STATUS_ENV = "EEGLE_DSART_PHASE_STATUS_FILE"
_writer: StatusWriter | None = None


def configure_dsart_phase_status(path: str | Path) -> None:
    """Select the durable phase-status artifact for this worker process."""

    global _writer
    target = Path(path).expanduser().resolve()
    os.environ[DSART_PHASE_STATUS_ENV] = str(target)
    _writer = StatusWriter(target, "dsart_phase_worker", "fresh_python_process")


def update_dsart_phase_status(stage: str, *, status: str = "running", **fields: Any) -> bool:
    """Publish a task milestone without ever making diagnostics task-critical."""

    global _writer
    try:
        if _writer is None:
            configured = os.environ.get(DSART_PHASE_STATUS_ENV)
            if not configured:
                return False
            _writer = StatusWriter(
                Path(configured).expanduser().resolve(),
                "dsart_phase_worker",
                "fresh_python_process",
            )
        _writer.update(status, stage=str(stage), process_id=os.getpid(), **fields)
        return True
    except Exception:
        # This file is an observer. A locked diagnostic path must never alter
        # stimulus delivery, keyboard handling, recording, or exit status.
        return False
