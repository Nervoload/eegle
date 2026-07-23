"""Execution-capture authority.

New packet captures use the versioned framed ``SampleStore``. The CLRE1 names
are intentionally isolated in ``legacy_capture`` for historical reads.
"""

from eegle.recording.stores import (
    FRAMED_SAMPLE_MEDIA_TYPE,
    FramedSampleStore,
    SampleStore,
    SampleStorePurpose,
    SampleStoreReadResult,
    read_framed_sample_store,
)


__all__ = [
    "FRAMED_SAMPLE_MEDIA_TYPE",
    "FramedSampleStore",
    "SampleStore",
    "SampleStorePurpose",
    "SampleStoreReadResult",
    "read_framed_sample_store",
]
