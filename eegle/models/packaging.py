"""Deterministic, framework-neutral model package construction and checking.

The package is a transport envelope around the existing canonical
``ModelManifest``.  It never becomes a factory, environment installer, model
registry, trainer, or prediction authority.  Executable construction remains
owned by an installed ``PluginDescriptor`` and prediction identity remains
owned by the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

from jsonschema import Draft202012Validator
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from eegle._domain import ComponentKind
from eegle._validation import (
    freeze_json,
    require_digest,
    require_identifier,
    thaw_json,
)
from eegle.compiler.lock import canonical_hash, canonical_json_bytes
from eegle.models.contracts import ModelContract, ModelStateBehavior
from eegle.models.manifests import (
    ModelImplementationRequirement,
    ModelManifest,
)
from eegle.models.results import ModelResult
from eegle.models.state_artifacts import ModelStateArtifact
from eegle.plugins import (
    ConstructionAPI,
    PluginDescriptor,
    PluginRegistry,
    StateBehavior,
)
from eegle.recording import ArtifactReference, Sensitivity
from eegle.specs import ModelArtifactBindingSpec


MODEL_PACKAGE_SCHEMA_ID = "eegle.model_package.v1"
MODEL_PACKAGE_CHECK_SCHEMA_ID = "eegle.model_package_check.v1"
PACKED_MODEL_SCHEMA_ID = "eegle.packed_model.v1"
SYNTHETIC_TEST_VECTOR_SCHEMA_ID = "eegle.synthetic_model_test_vector.v1"
SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID = "eegle.synthetic_model_test_vectors.v1"
MODEL_PACKAGE_MEDIA_TYPE = "application/vnd.eegle.model-package+zip"
MODEL_PACKAGE_INDEX_MEMBER = "package.json"
_MAX_INDEX_BYTES = 16 * 1024 * 1024
_PYTHON_SOURCE_SUFFIXES = frozenset({".py", ".pyc", ".pyo", ".pyw"})
_PYTHON_SOURCE_MEDIA_TYPES = frozenset(
    {"text/x-python", "application/x-python-code", "application/x-python-bytecode"}
)


class ModelPackageIntegrityError(ValueError):
    """Raised before package content can be materialized or constructed."""


@dataclass(frozen=True, slots=True)
class ModelArtifactSource:
    """Local packaging input that is never serialized into the package index."""

    artifact_id: str
    role: str
    media_type: str
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    path: Path | None = None
    data: bytes | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "artifact_id", require_identifier(self.artifact_id, "artifact_id")
        )
        object.__setattr__(self, "role", require_identifier(self.role, "artifact role"))
        media_type = self.media_type.strip()
        if not media_type:
            raise ValueError("model artifact media_type cannot be empty")
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))
        if (self.path is None) == (self.data is None):
            raise ValueError("model artifact source requires exactly one of path or data")
        if self.path is not None:
            path = Path(self.path).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(path)
            if path.suffix.lower() in _PYTHON_SOURCE_SUFFIXES:
                raise ValueError("model packages cannot serialize Python source or bytecode")
            object.__setattr__(self, "path", path)
        if media_type.lower().split(";", 1)[0].strip() in _PYTHON_SOURCE_MEDIA_TYPES:
            raise ValueError("model packages cannot serialize Python source or bytecode")
        if self.data is not None:
            object.__setattr__(self, "data", bytes(self.data))

    @classmethod
    def from_file(
        cls,
        artifact_id: str,
        role: str,
        media_type: str,
        path: str | Path,
        *,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
    ) -> "ModelArtifactSource":
        return cls(
            artifact_id,
            role,
            media_type,
            sensitivity=sensitivity,
            path=Path(path),
        )

    @classmethod
    def initial_state(
        cls,
        artifact: ModelStateArtifact,
        *,
        artifact_id: str = "artifact.initial-state",
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
    ) -> "ModelArtifactSource":
        return cls(
            artifact_id,
            "model_initial_state",
            "application/vnd.eegle.model-state+json",
            sensitivity=sensitivity,
            data=canonical_json_bytes(artifact.to_payload()),
        )

    def reference(self) -> ArtifactReference:
        digest, size = _source_digest(self)
        return ArtifactReference(
            self.artifact_id,
            self.role,
            f"artifact://{self.artifact_id}",
            digest,
            self.media_type,
            size,
            self.sensitivity,
        )


@dataclass(frozen=True, slots=True)
class SyntheticModelTestVector:
    """Portable JSON input and expected framework-neutral model results."""

    vector_id: str
    inputs: Mapping[str, Any]
    expected_outputs: Mapping[str, ModelResult]
    description: str | None = None
    schema: str = SYNTHETIC_TEST_VECTOR_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != SYNTHETIC_TEST_VECTOR_SCHEMA_ID:
            raise ValueError(f"unsupported synthetic model vector schema: {self.schema}")
        object.__setattr__(
            self, "vector_id", require_identifier(self.vector_id, "vector_id")
        )
        object.__setattr__(self, "inputs", freeze_json(self.inputs))
        outputs = {
            require_identifier(str(key), "output port"): value
            for key, value in self.expected_outputs.items()
        }
        if not outputs or not all(
            isinstance(value, ModelResult) for value in outputs.values()
        ):
            raise TypeError("synthetic expected outputs must contain ModelResult values")
        object.__setattr__(self, "expected_outputs", MappingProxyType(outputs))
        if self.description is not None and not self.description.strip():
            raise ValueError("synthetic vector description cannot be empty")

    @property
    def vector_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "vector_id": self.vector_id,
            "inputs": thaw_json(self.inputs),
            "expected_outputs": {
                key: value.to_payload()
                for key, value in sorted(self.expected_outputs.items())
            },
            "description": self.description,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["vector_hash"] = self.vector_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SyntheticModelTestVector":
        value = cls(
            vector_id=str(payload["vector_id"]),
            inputs=dict(payload.get("inputs") or {}),
            expected_outputs={
                str(key): ModelResult.from_payload(item)
                for key, item in dict(payload.get("expected_outputs") or {}).items()
            },
            description=None
            if payload.get("description") is None
            else str(payload["description"]),
            schema=str(payload.get("schema", SYNTHETIC_TEST_VECTOR_SCHEMA_ID)),
        )
        if payload.get("vector_hash") != value.vector_hash:
            raise ValueError("synthetic model test-vector hash mismatch")
        return value


@dataclass(frozen=True, slots=True)
class ModelPackageIndex:
    manifest: ModelManifest
    artifact_members: Mapping[str, str]
    test_vectors: tuple[SyntheticModelTestVector, ...] = ()
    schema: str = MODEL_PACKAGE_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != MODEL_PACKAGE_SCHEMA_ID:
            raise ValueError(f"unsupported model package schema: {self.schema}")
        if not isinstance(self.manifest, ModelManifest):
            raise TypeError("model package requires a ModelManifest")
        members = {
            require_identifier(str(key), "artifact_id"): _safe_member(str(value))
            for key, value in self.artifact_members.items()
        }
        expected = {value.artifact_id for value in self.manifest.artifacts}
        if set(members) != expected:
            raise ValueError("model package members must exactly match manifest artifacts")
        if any(not value.startswith("artifacts/") for value in members.values()):
            raise ValueError("model package artifact members must live under artifacts/")
        object.__setattr__(self, "artifact_members", freeze_json(members))
        vectors = tuple(sorted(self.test_vectors, key=lambda value: value.vector_id))
        if len(vectors) != len({value.vector_id for value in vectors}):
            raise ValueError("synthetic model test-vector identities must be unique")
        for vector in vectors:
            _validate_vector(vector, self.manifest.contract)
        object.__setattr__(self, "test_vectors", vectors)

    @property
    def index_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "manifest": self.manifest.to_payload(),
            "artifact_members": thaw_json(self.artifact_members),
            "test_vectors": [value.to_payload() for value in self.test_vectors],
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["index_hash"] = self.index_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelPackageIndex":
        value = cls(
            manifest=ModelManifest.from_payload(payload["manifest"]),
            artifact_members={
                str(key): str(item)
                for key, item in dict(payload.get("artifact_members") or {}).items()
            },
            test_vectors=tuple(
                SyntheticModelTestVector.from_payload(item)
                for item in payload.get("test_vectors", ())
            ),
            schema=str(payload.get("schema", MODEL_PACKAGE_SCHEMA_ID)),
        )
        if payload.get("index_hash") != value.index_hash:
            raise ValueError("model package index hash mismatch")
        return value


@dataclass(frozen=True, slots=True)
class PackedModel:
    package_path: Path
    package_digest: str
    index: ModelPackageIndex

    def __post_init__(self) -> None:
        object.__setattr__(self, "package_path", Path(self.package_path).resolve())
        object.__setattr__(self, "package_digest", require_digest(self.package_digest))
        if not isinstance(self.index, ModelPackageIndex):
            raise TypeError("packed model requires a ModelPackageIndex")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": PACKED_MODEL_SCHEMA_ID,
            "package_path": str(self.package_path),
            "package_digest": self.package_digest,
            "manifest_digest": self.index.manifest.manifest_digest,
            "contract_digest": self.index.manifest.contract.contract_digest,
            "artifact_count": len(self.index.manifest.artifacts),
            "test_vector_count": len(self.index.test_vectors),
        }


@dataclass(frozen=True, slots=True)
class VerifiedModelPackage:
    package_path: Path
    package_digest: str
    index: ModelPackageIndex

    def __post_init__(self) -> None:
        object.__setattr__(self, "package_path", Path(self.package_path).resolve())
        object.__setattr__(self, "package_digest", require_digest(self.package_digest))
        if not isinstance(self.index, ModelPackageIndex):
            raise TypeError("verified model package requires a ModelPackageIndex")


@dataclass(frozen=True, slots=True)
class MaterializedModelPackage:
    root: Path
    package_digest: str
    index: ModelPackageIndex
    artifact_paths: Mapping[str, Path]

    def __post_init__(self) -> None:
        root = Path(self.root).resolve()
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "package_digest", require_digest(self.package_digest))
        if not isinstance(self.index, ModelPackageIndex):
            raise TypeError("materialized model package requires a ModelPackageIndex")
        references = {
            value.artifact_id: value for value in self.index.manifest.artifacts
        }
        paths = {
            require_identifier(str(key), "artifact_id"): Path(value).resolve()
            for key, value in self.artifact_paths.items()
        }
        if set(paths) != set(references):
            raise ValueError(
                "materialized model artifact paths must exactly match the manifest"
            )
        for artifact_id, path in paths.items():
            expected = (
                root
                / "artifacts"
                / references[artifact_id].digest.removeprefix("sha256:")
            )
            if path != expected:
                raise ValueError(
                    f"materialized model artifact path is not content-addressed: {artifact_id}"
                )
        object.__setattr__(
            self,
            "artifact_paths",
            MappingProxyType(paths),
        )

    def deployment_bindings(self) -> tuple[ModelArtifactBindingSpec, ...]:
        references = {value.artifact_id: value for value in self.index.manifest.artifacts}
        return tuple(
            ModelArtifactBindingSpec(
                self.index.manifest.manifest_digest,
                artifact_id,
                path.as_uri(),
                references[artifact_id].digest,
            )
            for artifact_id, path in sorted(self.artifact_paths.items())
        )


@dataclass(frozen=True, slots=True)
class ModelPackageIssue:
    code: str
    severity: str
    message: str
    path: str = "$"

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", require_identifier(self.code, "issue code"))
        if self.severity not in {"warning", "error"}:
            raise ValueError("model package issue severity must be warning or error")
        if not self.message.strip() or not self.path.strip():
            raise ValueError("model package issue message and path cannot be empty")

    def to_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelPackageIssue":
        return cls(
            code=str(payload["code"]),
            severity=str(payload["severity"]),
            message=str(payload["message"]),
            path=str(payload.get("path", "$")),
        )


@dataclass(frozen=True, slots=True)
class ModelPackageCheckReport:
    package_digest: str
    manifest_digest: str
    contract_digest: str
    artifact_count: int
    test_vector_count: int
    implementation_checked: bool
    compatible_plugins: tuple[str, ...]
    issues: tuple[ModelPackageIssue, ...] = ()
    schema: str = MODEL_PACKAGE_CHECK_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != MODEL_PACKAGE_CHECK_SCHEMA_ID:
            raise ValueError(f"unsupported model package check schema: {self.schema}")
        for field_name in ("package_digest", "manifest_digest", "contract_digest"):
            object.__setattr__(
                self, field_name, require_digest(getattr(self, field_name), field_name)
            )
        for field_name in ("artifact_count", "test_vector_count"):
            value = int(getattr(self, field_name))
            if value < 0:
                raise ValueError(f"{field_name} cannot be negative")
            object.__setattr__(self, field_name, value)
        if not isinstance(self.implementation_checked, bool):
            raise TypeError("implementation_checked must be boolean")
        plugins = tuple(
            sorted(
                require_identifier(value, "plugin_id")
                for value in self.compatible_plugins
            )
        )
        if len(plugins) != len(set(plugins)):
            raise ValueError("compatible plugin identities must be unique")
        object.__setattr__(self, "compatible_plugins", plugins)
        issues = tuple(self.issues)
        if not all(isinstance(value, ModelPackageIssue) for value in issues):
            raise TypeError("model package check issues must be ModelPackageIssue values")
        object.__setattr__(self, "issues", issues)

    @property
    def ready(self) -> bool:
        return not any(value.severity == "error" for value in self.issues)

    @property
    def report_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "package_digest": self.package_digest,
            "manifest_digest": self.manifest_digest,
            "contract_digest": self.contract_digest,
            "artifact_count": self.artifact_count,
            "test_vector_count": self.test_vector_count,
            "implementation_checked": self.implementation_checked,
            "compatible_plugins": list(self.compatible_plugins),
            "issues": [value.to_payload() for value in self.issues],
            "ready": self.ready,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["report_hash"] = self.report_hash
        return payload

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> "ModelPackageCheckReport":
        value = cls(
            package_digest=str(payload["package_digest"]),
            manifest_digest=str(payload["manifest_digest"]),
            contract_digest=str(payload["contract_digest"]),
            artifact_count=int(payload["artifact_count"]),
            test_vector_count=int(payload["test_vector_count"]),
            implementation_checked=payload["implementation_checked"],
            compatible_plugins=tuple(
                str(plugin_id)
                for plugin_id in payload.get("compatible_plugins", ())
            ),
            issues=tuple(
                ModelPackageIssue.from_payload(issue)
                for issue in payload.get("issues", ())
            ),
            schema=str(payload.get("schema", MODEL_PACKAGE_CHECK_SCHEMA_ID)),
        )
        if payload.get("ready") is not value.ready:
            raise ValueError("model package check readiness mismatch")
        if payload.get("report_hash") != value.report_hash:
            raise ValueError("model package check report hash mismatch")
        return value


def build_model_manifest(
    *,
    model_id: str,
    model_version: str,
    contract: ModelContract,
    implementations: Iterable[ModelImplementationRequirement],
    artifacts: Iterable[ModelArtifactSource] = (),
    initial_state_artifact_id: str | None = None,
    training_provenance: Mapping[str, Any] | None = None,
    evaluation_provenance: Mapping[str, Any] | None = None,
    license: str | None = None,
    annotations: Mapping[str, Any] | None = None,
) -> ModelManifest:
    """Hash local packaging inputs into one path-free canonical manifest."""

    sources = tuple(artifacts)
    if len(sources) != len({value.artifact_id for value in sources}):
        raise ValueError("model artifact source identities must be unique")
    return ModelManifest(
        model_id=model_id,
        model_version=model_version,
        contract=contract,
        artifacts=tuple(value.reference() for value in sources),
        implementations=tuple(implementations),
        initial_state_artifact_id=initial_state_artifact_id,
        training_provenance=training_provenance or {},
        evaluation_provenance=evaluation_provenance or {},
        license=license,
        annotations=annotations or {},
    )


def build_model_package(
    output: str | Path,
    *,
    model_id: str,
    model_version: str,
    contract: ModelContract,
    implementations: Iterable[ModelImplementationRequirement],
    artifacts: Iterable[ModelArtifactSource] = (),
    initial_state: Mapping[str, Any] | None = None,
    initial_state_artifact_id: str = "artifact.initial-state",
    test_vectors: Iterable[SyntheticModelTestVector] = (),
    training_provenance: Mapping[str, Any] | None = None,
    evaluation_provenance: Mapping[str, Any] | None = None,
    license: str | None = None,
    annotations: Mapping[str, Any] | None = None,
) -> PackedModel:
    """Build a manifest and deterministic package without importing a framework."""

    sources = list(artifacts)
    selected_initial_id: str | None = None
    if initial_state is not None:
        state = contract.state
        if state.behavior == ModelStateBehavior.STATELESS or state.state_schema_id is None:
            raise ValueError("initial state requires a stateful model contract")
        envelope = ModelStateArtifact.create(
            model_id=model_id,
            model_version=model_version,
            contract_digest=contract.contract_digest,
            state_schema_id=state.state_schema_id,
            state=initial_state,
        )
        sources.append(
            ModelArtifactSource.initial_state(
                envelope,
                artifact_id=initial_state_artifact_id,
            )
        )
        selected_initial_id = initial_state_artifact_id
    elif contract.state.initial_state_required:
        raise ValueError("model contract requires initial_state packaging input")
    manifest = build_model_manifest(
        model_id=model_id,
        model_version=model_version,
        contract=contract,
        implementations=implementations,
        artifacts=sources,
        initial_state_artifact_id=selected_initial_id,
        training_provenance=training_provenance,
        evaluation_provenance=evaluation_provenance,
        license=license,
        annotations=annotations,
    )
    return pack_model(manifest, sources, output, test_vectors=test_vectors)


def pack_model(
    manifest: ModelManifest,
    artifacts: Iterable[ModelArtifactSource],
    output: str | Path,
    *,
    test_vectors: Iterable[SyntheticModelTestVector] = (),
) -> PackedModel:
    """Write a deterministic, non-overwriting package for an existing manifest."""

    target = Path(output).expanduser().resolve()
    if target.exists():
        raise FileExistsError(target)
    materialized_sources = tuple(artifacts)
    sources = {value.artifact_id: value for value in materialized_sources}
    if len(sources) != len(materialized_sources):
        raise ValueError("model artifact source identities must be unique")
    expected = {value.artifact_id: value for value in manifest.artifacts}
    if set(sources) != set(expected):
        raise ValueError("packaging inputs must exactly match manifest artifacts")
    for artifact_id, source in sources.items():
        if source.reference() != expected[artifact_id]:
            raise ValueError(f"packaging input differs from manifest artifact {artifact_id}")
    members = {
        artifact_id: f"artifacts/{reference.digest.removeprefix('sha256:')}"
        for artifact_id, reference in expected.items()
    }
    index = ModelPackageIndex(manifest, members, tuple(test_vectors))
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_STORED, allowZip64=True) as archive:
            _write_member(
                archive,
                MODEL_PACKAGE_INDEX_MEMBER,
                canonical_json_bytes(index.to_payload()),
            )
            written: set[str] = set()
            for artifact_id, member in sorted(members.items()):
                if member in written:
                    continue
                _write_source_member(archive, member, sources[artifact_id])
                written.add(member)
        verified = open_model_package(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return PackedModel(target, verified.package_digest, index)


def open_model_package(path: str | Path) -> VerifiedModelPackage:
    """Verify every index and artifact byte before returning package metadata."""

    package_path = Path(path).expanduser().resolve()
    if not package_path.is_file():
        raise FileNotFoundError(package_path)
    try:
        with ZipFile(package_path, "r") as archive:
            infos = archive.infolist()
            names = [value.filename for value in infos]
            if len(names) != len(set(names)):
                raise ModelPackageIntegrityError("model package contains duplicate members")
            for info in infos:
                try:
                    _safe_member(info.filename)
                except ValueError as exc:
                    raise ModelPackageIntegrityError(str(exc)) from exc
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ModelPackageIntegrityError("model package cannot contain symlinks")
            if MODEL_PACKAGE_INDEX_MEMBER not in names:
                raise ModelPackageIntegrityError("model package has no package.json index")
            info = archive.getinfo(MODEL_PACKAGE_INDEX_MEMBER)
            if info.file_size > _MAX_INDEX_BYTES:
                raise ModelPackageIntegrityError("model package index exceeds its size limit")
            try:
                payload = json.loads(archive.read(info).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ModelPackageIntegrityError("model package index is not valid JSON") from exc
            if not isinstance(payload, Mapping):
                raise ModelPackageIntegrityError("model package index must be an object")
            try:
                index = ModelPackageIndex.from_payload(payload)
            except (KeyError, TypeError, ValueError) as exc:
                raise ModelPackageIntegrityError(str(exc)) from exc
            expected_names = {
                MODEL_PACKAGE_INDEX_MEMBER,
                *index.artifact_members.values(),
            }
            if set(names) != expected_names:
                raise ModelPackageIntegrityError(
                    "model package members differ from the canonical index"
                )
            references = {
                value.artifact_id: value for value in index.manifest.artifacts
            }
            verified_members: dict[str, tuple[str, int]] = {}
            for artifact_id, member in index.artifact_members.items():
                observed = verified_members.get(member)
                if observed is None:
                    observed = _hash_archive_member(archive, member)
                    verified_members[member] = observed
                reference = references[artifact_id]
                if observed != (reference.digest, reference.size_bytes):
                    raise ModelPackageIntegrityError(
                        f"model artifact {artifact_id} digest or size mismatch"
                    )
            _verify_initial_state(archive, index)
    except BadZipFile as exc:
        raise ModelPackageIntegrityError("model package is not a valid ZIP archive") from exc
    digest, _ = _hash_path(package_path)
    return VerifiedModelPackage(package_path, digest, index)


def materialize_model_package(
    path: str | Path,
    destination: str | Path,
) -> MaterializedModelPackage:
    """Extract verified artifacts into one immutable content-addressed directory."""

    verified = open_model_package(path)
    destination_root = Path(destination).expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    target = destination_root / verified.package_digest.removeprefix("sha256:")
    references = {
        value.artifact_id: value for value in verified.index.manifest.artifacts
    }
    artifact_paths = {
        artifact_id: target
        / "artifacts"
        / reference.digest.removeprefix("sha256:")
        for artifact_id, reference in references.items()
    }
    if target.is_symlink():
        raise ModelPackageIntegrityError(
            "model package materialization cannot be a symbolic link"
        )
    if target.exists():
        for artifact_id, artifact_path in artifact_paths.items():
            observed = _hash_path(artifact_path)
            reference = references[artifact_id]
            if observed != (reference.digest, reference.size_bytes):
                raise ModelPackageIntegrityError(
                    f"existing materialization differs for {artifact_id}"
                )
        return MaterializedModelPackage(
            target,
            verified.package_digest,
            verified.index,
            artifact_paths,
        )
    temporary = Path(tempfile.mkdtemp(prefix=".eegle-model-", dir=destination_root))
    try:
        (temporary / "artifacts").mkdir()
        with ZipFile(verified.package_path, "r") as archive:
            written: set[Path] = set()
            for artifact_id, member in sorted(verified.index.artifact_members.items()):
                output = (
                    temporary
                    / "artifacts"
                    / references[artifact_id].digest.removeprefix("sha256:")
                )
                if output in written:
                    continue
                with archive.open(member, "r") as source, output.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                written.add(output)
        for artifact_id, reference in references.items():
            output = (
                temporary
                / "artifacts"
                / reference.digest.removeprefix("sha256:")
            )
            if _hash_path(output) != (reference.digest, reference.size_bytes):
                raise ModelPackageIntegrityError(
                    f"materialized model artifact differs for {artifact_id}"
                )
        (temporary / "manifest.json").write_bytes(
            canonical_json_bytes(verified.index.manifest.to_payload())
        )
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return MaterializedModelPackage(
        target,
        verified.package_digest,
        verified.index,
        artifact_paths,
    )


def check_model_package(
    path: str | Path,
    *,
    registry: PluginRegistry | None = None,
    require_implementation: bool = False,
) -> ModelPackageCheckReport:
    """Verify bytes, vectors, and optional installed-plugin conformance."""

    verified = open_model_package(path)
    issues: list[ModelPackageIssue] = []
    compatible: list[str] = []
    if registry is not None:
        for descriptor in registry.descriptors():
            descriptor_issues = _descriptor_issues(verified.index.manifest, descriptor)
            if descriptor_issues is None:
                continue
            if not descriptor_issues:
                compatible.append(descriptor.plugin_id)
            else:
                issues.extend(descriptor_issues)
        if not compatible:
            issues.append(
                ModelPackageIssue(
                    "model.implementation_unavailable",
                    "error" if require_implementation else "warning",
                    "No installed model plugin conforms to a manifest implementation requirement.",
                    "$.manifest.implementations",
                )
            )
    elif require_implementation:
        issues.append(
            ModelPackageIssue(
                "model.implementation_not_checked",
                "error",
                "Implementation conformance was required without a plugin registry.",
                "$.manifest.implementations",
            )
        )
    return ModelPackageCheckReport(
        verified.package_digest,
        verified.index.manifest.manifest_digest,
        verified.index.manifest.contract.contract_digest,
        len(verified.index.manifest.artifacts),
        len(verified.index.test_vectors),
        registry is not None,
        tuple(sorted(set(compatible))),
        tuple(issues),
    )


def load_synthetic_test_vectors(path: str | Path) -> tuple[SyntheticModelTestVector, ...]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("synthetic model vector set must be an object")
    if payload.get("schema") != SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID:
        raise ValueError("unsupported synthetic model vector-set schema")
    return tuple(
        SyntheticModelTestVector.from_payload(value)
        for value in payload.get("vectors", ())
    )


def _descriptor_issues(
    manifest: ModelManifest,
    descriptor: PluginDescriptor,
) -> tuple[ModelPackageIssue, ...] | None:
    requirements = tuple(
        value
        for value in manifest.implementations
        if value.plugin_id == descriptor.plugin_id
        and Version(descriptor.version) in SpecifierSet(value.version_spec)
    )
    if not requirements:
        return None
    issues: list[ModelPackageIssue] = []
    if descriptor.kind != ComponentKind.MODEL:
        issues.append(_implementation_issue(descriptor, "is not a model plugin"))
    declared_inputs = {value.name: value.type_id for value in descriptor.input_ports}
    contract_inputs = {
        value.port_name: value.type_id for value in manifest.contract.inputs
    }
    if declared_inputs != contract_inputs:
        issues.append(
            _implementation_issue(
                descriptor, "input ports differ from the model contract"
            )
        )
    declared_outputs = {value.name: value.type_id for value in descriptor.output_ports}
    contract_outputs = {
        value.port_name: value.type_id for value in manifest.contract.outputs
    }
    if declared_outputs != contract_outputs:
        issues.append(
            _implementation_issue(
                descriptor, "output ports differ from the model contract"
            )
        )
    if not manifest.contract.supported_modes.issubset(
        descriptor.capabilities.supported_modes
    ):
        issues.append(
            _implementation_issue(
                descriptor, "execution modes do not cover the model contract"
            )
        )
    expected_state = {
        ModelStateBehavior.STATELESS: StateBehavior.STATELESS,
        ModelStateBehavior.SNAPSHOT_RESTORE: StateBehavior.SNAPSHOT_RESTORE,
        ModelStateBehavior.EXTERNAL: StateBehavior.EXTERNAL,
    }[manifest.contract.state.behavior]
    if descriptor.capabilities.state_behavior != expected_state:
        issues.append(
            _implementation_issue(
                descriptor, "state behavior differs from the model contract"
            )
        )
    if (
        manifest.artifacts
        and descriptor.construction_api != ConstructionAPI.MODEL_CONTEXT_V1
    ):
        issues.append(
            _implementation_issue(
                descriptor,
                "artifact-backed models require model-context construction",
            )
        )
    return tuple(issues)


def _implementation_issue(descriptor: PluginDescriptor, message: str) -> ModelPackageIssue:
    return ModelPackageIssue(
        "model.implementation_incompatible",
        "warning",
        f"Plugin {descriptor.plugin_id} {descriptor.version} {message}.",
        "$.manifest.implementations",
    )


def _validate_vector(vector: SyntheticModelTestVector, contract: ModelContract) -> None:
    inputs = {value.port_name for value in contract.inputs}
    outputs = {value.port_name: value for value in contract.outputs}
    if set(vector.inputs) != inputs:
        raise ValueError(
            f"synthetic vector {vector.vector_id} inputs differ from the model contract"
        )
    if set(vector.expected_outputs) != set(outputs):
        raise ValueError(
            f"synthetic vector {vector.vector_id} outputs differ from the model contract"
        )
    for output_port, result in vector.expected_outputs.items():
        output = outputs[output_port]
        _require_schema(
            result.value,
            output.value_schema,
            vector.vector_id,
            output_port,
            "value",
        )
        if result.uncertainty is not None:
            if output.uncertainty_schema is None:
                raise ValueError(
                    f"synthetic vector {vector.vector_id} has undeclared uncertainty"
                )
            _require_schema(
                result.uncertainty,
                output.uncertainty_schema,
                vector.vector_id,
                output_port,
                "uncertainty",
            )
        if result.validity is not None:
            if output.validity_schema is None:
                raise ValueError(
                    f"synthetic vector {vector.vector_id} has undeclared validity"
                )
            _require_schema(
                result.validity,
                output.validity_schema,
                vector.vector_id,
                output_port,
                "validity",
            )
        if result.abstained and not output.abstention_supported:
            raise ValueError(
                f"synthetic vector {vector.vector_id} uses undeclared abstention"
            )


def _require_schema(
    value: Any,
    schema: Mapping[str, Any],
    vector_id: str,
    output_port: str,
    field_name: str,
) -> None:
    errors = sorted(
        Draft202012Validator(thaw_json(schema)).iter_errors(thaw_json(value)),
        key=lambda value: tuple(str(part) for part in value.absolute_path),
    )
    if errors:
        raise ValueError(
            f"synthetic vector {vector_id} {output_port}.{field_name} violates its schema: "
            f"{errors[0].message}"
        )


def _verify_initial_state(archive: ZipFile, index: ModelPackageIndex) -> None:
    artifact_id = index.manifest.initial_state_artifact_id
    if artifact_id is None:
        return
    member = index.artifact_members[artifact_id]
    try:
        payload = json.loads(archive.read(member).decode("utf-8"))
        envelope = ModelStateArtifact.from_payload(payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ModelPackageIntegrityError("initial model state is invalid") from exc
    expected = (
        index.manifest.model_id,
        index.manifest.model_version,
        index.manifest.contract.contract_digest,
        index.manifest.contract.state.state_schema_id,
    )
    observed = (
        envelope.model_id,
        envelope.model_version,
        envelope.contract_digest,
        envelope.state_schema_id,
    )
    if observed != expected:
        raise ModelPackageIntegrityError(
            "initial model state identity differs from the manifest"
        )


def _source_digest(source: ModelArtifactSource) -> tuple[str, int]:
    if source.data is not None:
        return f"sha256:{hashlib.sha256(source.data).hexdigest()}", len(source.data)
    assert source.path is not None
    return _hash_path(source.path)


def _hash_path(path: Path) -> tuple[str, int]:
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _hash_archive_member(archive: ZipFile, member: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(member, "r") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _safe_member(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith(("/", "\\"))
        or path.is_absolute()
        or ".." in path.parts
        or any("\\" in part for part in path.parts)
    ):
        raise ValueError(f"unsafe model package member: {value!r}")
    return path.as_posix()


def _write_member(archive: ZipFile, name: str, content: bytes) -> None:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content)


def _write_source_member(
    archive: ZipFile,
    name: str,
    source: ModelArtifactSource,
) -> None:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    with archive.open(info, "w", force_zip64=True) as output:
        if source.data is not None:
            output.write(source.data)
            return
        assert source.path is not None
        with source.path.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)


__all__ = [
    "MODEL_PACKAGE_CHECK_SCHEMA_ID",
    "MODEL_PACKAGE_MEDIA_TYPE",
    "MODEL_PACKAGE_SCHEMA_ID",
    "PACKED_MODEL_SCHEMA_ID",
    "SYNTHETIC_TEST_VECTOR_SCHEMA_ID",
    "SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID",
    "MaterializedModelPackage",
    "ModelArtifactSource",
    "ModelPackageCheckReport",
    "ModelPackageIndex",
    "ModelPackageIntegrityError",
    "ModelPackageIssue",
    "PackedModel",
    "SyntheticModelTestVector",
    "VerifiedModelPackage",
    "build_model_manifest",
    "build_model_package",
    "check_model_package",
    "load_synthetic_test_vectors",
    "materialize_model_package",
    "open_model_package",
    "pack_model",
]
