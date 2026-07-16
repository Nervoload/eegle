"""Shared raw EEG CSV column contracts."""

from __future__ import annotations

from collections.abc import Iterable


EEG_TIMESTAMP_COLUMNS = frozenset(
    {
        "lsl_timestamp",
        "local_received_time",
        "source_lsl_timestamp",
        "lsl_time_correction_seconds",
    }
)


def eeg_channel_columns(columns: Iterable[str]) -> list[str]:
    """Return sample-value columns without treating timestamp provenance as EEG."""
    return [str(column) for column in columns if str(column) not in EEG_TIMESTAMP_COLUMNS]


def eeg_channel_indices(columns: Iterable[str]) -> list[int]:
    return [index for index, column in enumerate(columns) if str(column) not in EEG_TIMESTAMP_COLUMNS]
