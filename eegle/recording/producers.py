"""Dependency-light reference artifact producers."""

from __future__ import annotations

from typing import Any, Mapping

from eegle.compiler.lock import canonical_json_bytes, content_hash
from eegle.models.predictions import Prediction
from eegle.recording.artifacts import ArtifactReference, Sensitivity
from eegle.recording.publications import ArtifactPublication


class PredictionArtifactProducer:
    """Publish one model prediction as a small portable JSON artifact."""

    def __init__(
        self,
        *,
        artifact_id: str,
        role: str,
        sensitivity: str = "internal",
    ) -> None:
        self.artifact_id = artifact_id
        self.role = role
        self.sensitivity = Sensitivity(sensitivity)

    def produce(self, value: Any, context: Any) -> ArtifactPublication:
        if not isinstance(value, Prediction):
            raise TypeError("prediction artifact producer requires a Prediction")
        payload: Mapping[str, Any] = {
            "schema": "eegle.prediction_artifact.v1",
            "prediction": value.to_payload(),
        }
        data = canonical_json_bytes(payload)
        digest = content_hash(data)
        return ArtifactPublication(
            publication_id=context.next_id("artifact_publication"),
            reference=ArtifactReference(
                artifact_id=self.artifact_id,
                role=self.role,
                uri=f"memory://{self.artifact_id}/{digest.removeprefix('sha256:')}",
                digest=digest,
                media_type="application/json",
                size_bytes=len(data),
                sensitivity=self.sensitivity,
                embedded=False,
            ),
            producer_component_id=context.component_id,
            producer_component_version=context.component_version,
            produced_time=context.current_time,
            input_ids=(value.prediction_id,),
            metadata={
                "prediction_id": value.prediction_id,
                "prediction_input_ids": list(value.input_ids),
                "model_id": value.model_id,
            },
            materialized_payload=payload,
        )
