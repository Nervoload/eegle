from __future__ import annotations

import unittest

from eegle.integrations import (
    ResearchIntegrationSupportMatrix,
    SupportValidationLevel,
    research_integration_support_matrix,
    validate_research_support_matrix_payload,
)


class Phase8ResearchIntegrationSupportTests(unittest.TestCase):
    def test_packaged_support_matrix_is_typed_hashed_and_schema_valid(self) -> None:
        matrix = research_integration_support_matrix()
        payload = matrix.to_payload()

        validate_research_support_matrix_payload(payload)
        self.assertEqual(
            ResearchIntegrationSupportMatrix.from_payload(payload),
            matrix,
        )
        self.assertEqual(payload["matrix_hash"], matrix.matrix_hash)
        self.assertEqual(
            tuple(value.capability_id for value in matrix.capabilities),
            tuple(sorted(value.capability_id for value in matrix.capabilities)),
        )

    def test_support_dimensions_do_not_overstate_reference_or_live_evidence(self) -> None:
        matrix = research_integration_support_matrix()
        support = {value.capability_id: value for value in matrix.capabilities}

        lsl = support["lsl.dense_source"]
        self.assertTrue(lsl.representable)
        self.assertTrue(lsl.adapter_available)
        self.assertTrue(lsl.validated)
        self.assertTrue(lsl.reference_supported)
        self.assertIn(SupportValidationLevel.SIMULATED_ENGINE, lsl.validation_levels)
        self.assertNotIn(SupportValidationLevel.EXTERNAL_REFERENCE, lsl.validation_levels)
        self.assertTrue(any("Real EEG" in value for value in lsl.limitations))

        for capability_id in (
            "mne.annotations.sparse_markers",
            "mne.epochs.admitted_windows",
            "mne.raw.explicit_lsl",
            "mne.replay_inputs.eeg_raw_annotations",
        ):
            capability = support[capability_id]
            self.assertTrue(capability.representable)
            self.assertTrue(capability.adapter_available)
            self.assertTrue(capability.validated)
            self.assertFalse(capability.reference_supported)
            self.assertEqual(
                capability.validation_levels,
                (SupportValidationLevel.UNIT,),
            )

        for capability_id in (
            "foundation_model.external_plugin",
            "pyriemann.external_plugin",
        ):
            capability = support[capability_id]
            self.assertTrue(capability.representable)
            self.assertFalse(capability.adapter_available)
            self.assertFalse(capability.validated)
            self.assertFalse(capability.reference_supported)
            self.assertEqual(capability.validation_levels, ())


if __name__ == "__main__":
    unittest.main()
