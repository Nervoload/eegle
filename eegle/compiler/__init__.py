"""Compiler foundation types; the Suite compiler is implemented in Phase 5."""

from eegle.compiler.lock import (
    CanonicalizationError,
    canonical_hash,
    canonical_json_bytes,
    content_hash,
    verify_hash,
)
from eegle.compiler.plan import ExecutionPlan, LockedPlugin, PlannedComponent


__all__ = [
    "CanonicalizationError",
    "ExecutionPlan",
    "LockedPlugin",
    "PlannedComponent",
    "canonical_hash",
    "canonical_json_bytes",
    "content_hash",
    "verify_hash",
]
