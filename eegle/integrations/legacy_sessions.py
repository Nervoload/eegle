"""Historical recipe profiles for the generic one-time session importer.

Recipe filenames belong at this integration boundary, not in the recording
kernel. The resulting artifacts still use generic evidence roles.
"""

from __future__ import annotations

from eegle.recording.artifacts import Sensitivity
from eegle.recording.importers import LegacyImportRule, LegacySessionImporter


_RECIPE_RULES = {
    "events/go_nogo_trials.csv": LegacyImportRule(
        "events",
        "behavior_events",
        "text/csv",
        Sensitivity.PSEUDONYMIZED,
    ),
    "events/go_nogo_results.json": LegacyImportRule(
        "events",
        "behavior_result",
        "application/json",
        Sensitivity.PSEUDONYMIZED,
    ),
    "events/dynamic_sart_trials.jsonl": LegacyImportRule(
        "events",
        "behavior_events",
        "application/x-ndjson",
        Sensitivity.PSEUDONYMIZED,
    ),
    "events/dynamic_sart_trials.csv": LegacyImportRule(
        "events",
        "behavior_events",
        "text/csv",
        Sensitivity.PSEUDONYMIZED,
    ),
    "events/dynamic_sart_key_events.jsonl": LegacyImportRule(
        "events",
        "key_event_ledger",
        "application/x-ndjson",
        Sensitivity.PSEUDONYMIZED,
    ),
    "events/dynamic_sart_blocks.csv": LegacyImportRule(
        "events",
        "phase_ledger",
        "text/csv",
        Sensitivity.PSEUDONYMIZED,
    ),
    "events/dynamic_sart_results.json": LegacyImportRule(
        "events",
        "behavior_result",
        "application/json",
        Sensitivity.PSEUDONYMIZED,
    ),
    "events/dynamic_sart_support_reference.json": LegacyImportRule(
        "events",
        "support_reference",
        "application/json",
        Sensitivity.PSEUDONYMIZED,
    ),
    "events/dynamic_sart_probes.jsonl": LegacyImportRule(
        "events",
        "probe_ledger",
        "application/x-ndjson",
        Sensitivity.PSEUDONYMIZED,
    ),
    "events/dsart_baseline_results.json": LegacyImportRule(
        "events",
        "baseline_result",
        "application/json",
        Sensitivity.PSEUDONYMIZED,
    ),
}


def legacy_bcipy_recipe_importer() -> LegacySessionImporter:
    """Return the importer with selected historical recipe artifacts enabled."""

    return LegacySessionImporter(additional_rules=_RECIPE_RULES)
