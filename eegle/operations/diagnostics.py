"""Join canonical diagnostics to non-authoritative authoring source maps."""

from __future__ import annotations

import re
from typing import Iterable

from eegle.authoring import (
    AuthoringProvenance,
    CanonicalArtifact,
    ScientificMateriality,
)
from eegle.compiler import CompilationDiagnostic
from eegle.operations.contracts import (
    OperationCategory,
    OperationDiagnostic,
    OperationIssueSeverity,
)
from eegle.specs import SchemaValidationError


_PATH_PART = re.compile(r"\.([^\.\[]+)|\[([0-9]+)\]")


def map_compilation_diagnostics(
    diagnostics: Iterable[CompilationDiagnostic],
    provenance: AuthoringProvenance,
) -> tuple[OperationDiagnostic, ...]:
    """Add source locations without changing compiler codes or paths."""

    if not isinstance(provenance, AuthoringProvenance):
        raise TypeError("diagnostic mapping requires AuthoringProvenance")
    mapped = tuple(
        _map_compilation_diagnostic(value, provenance) for value in diagnostics
    )
    return tuple(
        sorted(mapped, key=lambda value: (value.path or "$", value.code, value.message))
    )


def map_schema_validation_error(
    error: SchemaValidationError,
    artifact: CanonicalArtifact,
    provenance: AuthoringProvenance,
) -> OperationDiagnostic:
    """Map one canonical schema failure to its closest authoring source."""

    if not isinstance(error, SchemaValidationError):
        raise TypeError("schema diagnostic mapping requires SchemaValidationError")
    artifact = CanonicalArtifact(artifact)
    pointer = canonical_path_pointer(error.path, artifact=artifact)[1]
    entry = provenance.entry_for(artifact, pointer)
    return OperationDiagnostic(
        code="schema.invalid",
        category=OperationCategory.SCHEMA,
        title=f"Invalid {artifact.value} specification",
        message=_schema_message(error),
        severity=OperationIssueSeverity.ERROR,
        path=error.path,
        source=None if entry is None else entry.source,
        scientific_impact=(
            ScientificMateriality.UNKNOWN if entry is None else entry.materiality
        ),
        details={
            "canonical_artifact": artifact.value,
            "canonical_path": pointer,
        },
    )


def canonical_path_pointer(
    path: str,
    *,
    artifact: CanonicalArtifact | None = None,
) -> tuple[CanonicalArtifact, str]:
    """Convert compiler/schema ``$`` paths to an artifact and JSON pointer."""

    if not path.startswith("$"):
        raise ValueError("canonical diagnostic path must start at '$'")
    values: list[str] = []
    position = 1
    for match in _PATH_PART.finditer(path, position):
        if match.start() != position:
            raise ValueError(f"unsupported canonical diagnostic path: {path}")
        values.append(match.group(1) if match.group(1) is not None else match.group(2))
        position = match.end()
    if position != len(path):
        raise ValueError(f"unsupported canonical diagnostic path: {path}")
    detected = artifact
    if values and values[0] in {value.value for value in CanonicalArtifact}:
        path_artifact = CanonicalArtifact(values.pop(0))
        if detected is not None and detected != path_artifact:
            raise ValueError("diagnostic path and explicit artifact disagree")
        detected = path_artifact
    if detected is None:
        raise ValueError("canonical diagnostic path does not identify an artifact")
    pointer = "".join(f"/{_escape_pointer(value)}" for value in values)
    return detected, pointer


def _map_compilation_diagnostic(
    diagnostic: CompilationDiagnostic,
    provenance: AuthoringProvenance,
) -> OperationDiagnostic:
    try:
        artifact, pointer = canonical_path_pointer(diagnostic.path)
    except ValueError:
        artifact = None
        pointer = ""
    entry = None if artifact is None else provenance.entry_for(artifact, pointer)
    return OperationDiagnostic(
        code=diagnostic.code,
        category=OperationCategory.COMPILATION,
        title=diagnostic.code.replace("_", " ").replace(".", " ").title(),
        message=diagnostic.message,
        severity=OperationIssueSeverity(diagnostic.severity.value),
        path=diagnostic.path,
        source=None if entry is None else entry.source,
        scientific_impact=(
            ScientificMateriality.UNKNOWN if entry is None else entry.materiality
        ),
        details={
            **dict(diagnostic.details),
            "canonical_artifact": None if artifact is None else artifact.value,
            "canonical_path": pointer,
        },
    )


def _schema_message(error: SchemaValidationError) -> str:
    prefix = f"{error.path}: "
    message = str(error)
    return message[len(prefix) :] if message.startswith(prefix) else message


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
