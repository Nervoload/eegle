"""Optional task, transport, framework, format, and vendor integrations."""

from eegle.integrations.mne import (
    MneAnnotationsExport,
    MneEpochsExport,
    MneRawExport,
    MneReplayInputs,
    attach_annotations_to_mne_raw,
    dense_batch_to_mne_raw,
    dense_windows_to_mne_epochs,
    mne_raw_to_replay_inputs,
    sparse_events_to_mne_annotations,
)
from eegle.integrations.support import (
    RESEARCH_SUPPORT_MATRIX_JSON_SCHEMA,
    RESEARCH_SUPPORT_MATRIX_SCHEMA_ID,
    IntegrationCapabilitySupport,
    ResearchIntegrationSupportMatrix,
    SupportValidationLevel,
    research_integration_support_matrix,
    validate_research_support_matrix_payload,
)

__all__ = [
    "RESEARCH_SUPPORT_MATRIX_JSON_SCHEMA",
    "RESEARCH_SUPPORT_MATRIX_SCHEMA_ID",
    "IntegrationCapabilitySupport",
    "MneAnnotationsExport",
    "MneEpochsExport",
    "MneRawExport",
    "MneReplayInputs",
    "ResearchIntegrationSupportMatrix",
    "SupportValidationLevel",
    "attach_annotations_to_mne_raw",
    "dense_batch_to_mne_raw",
    "dense_windows_to_mne_epochs",
    "mne_raw_to_replay_inputs",
    "research_integration_support_matrix",
    "sparse_events_to_mne_annotations",
    "validate_research_support_matrix_payload",
]
