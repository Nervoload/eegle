"""Optional restricted YAML adapters for bounded authoring sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eegle._validation import freeze_json, thaw_json
from eegle.authoring.builders import AuthoredExperiment, ExperimentBuilder
from eegle.authoring.contracts import SourceKind, SourceLocation
from eegle.authoring.composed_projects import ComposedExperiment
from eegle.authoring.composition import ExperimentDesign
from eegle.authoring.drafts import DraftLoweringError
from eegle.authoring.provenance import DraftSourceMap
from eegle.authoring.schemas import validate_template_authoring_payload
from eegle.specs import SchemaValidationError


@dataclass(frozen=True, slots=True)
class RestrictedYamlLimits:
    max_bytes: int = 262_144
    max_depth: int = 32
    max_nodes: int = 10_000
    max_scalar_characters: int = 65_536

    def __post_init__(self) -> None:
        for field in (
            "max_bytes",
            "max_depth",
            "max_nodes",
            "max_scalar_characters",
        ):
            if int(getattr(self, field)) <= 0:
                raise ValueError(f"{field} must be positive")


class RestrictedYamlError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        source: SourceLocation,
        path: str = "",
    ) -> None:
        self.code = str(code)
        self.source = source
        self.path = str(path)
        location = source.locator or "<yaml>"
        if source.line is not None:
            location += f":{source.line}"
            if source.column is not None:
                location += f":{source.column}"
        super().__init__(f"{self.code} at {location}: {message}")


def parse_yaml_experiment(
    text: str,
    *,
    locator: str = "<string>",
    limits: RestrictedYamlLimits | None = None,
) -> ExperimentBuilder:
    """Parse, validate, and type-check one restricted YAML authoring document."""

    payload, source_map = _load_explicit_yaml(
        text,
        locator=locator,
        limits=limits or RestrictedYamlLimits(),
    )
    try:
        validate_template_authoring_payload(payload)
    except SchemaValidationError as exc:
        path = _schema_path_to_pointer(exc.path)
        raise RestrictedYamlError(
            "yaml.schema",
            str(exc),
            source=source_map.source_for(path),
            path=path,
        ) from exc
    try:
        builder = ExperimentBuilder.from_source_payload(
            payload,
            source_map=source_map,
        )
    except (KeyError, TypeError, ValueError) as exc:
        path = "/template"
        raise RestrictedYamlError(
            "yaml.authoring",
            str(exc),
            source=source_map.source_for(path),
            path=path,
        ) from exc
    builder.build()
    return builder


def load_yaml_experiment(
    text: str,
    *,
    locator: str = "<string>",
    limits: RestrictedYamlLimits | None = None,
) -> AuthoredExperiment:
    """Return the same authored result produced by the typed Python builder."""

    return parse_yaml_experiment(
        text,
        locator=locator,
        limits=limits,
    ).build()


def read_yaml_experiment(
    path: str | Path,
    *,
    limits: RestrictedYamlLimits | None = None,
) -> AuthoredExperiment:
    source = Path(path)
    effective_limits = limits or RestrictedYamlLimits()
    if source.stat().st_size > effective_limits.max_bytes:
        raise RestrictedYamlError(
            "yaml.limit_bytes",
            f"input exceeds {effective_limits.max_bytes} UTF-8 bytes",
            source=SourceLocation(SourceKind.YAML, str(source), line=1, column=1),
        )
    return load_yaml_experiment(
        source.read_text(encoding="utf-8"),
        locator=str(source),
        limits=effective_limits,
    )


def parse_yaml_design(
    text: str,
    *,
    locator: str = "<string>",
    limits: RestrictedYamlLimits | None = None,
) -> ExperimentDesign:
    """Parse one non-executable compositional design with source locations."""

    payload, source_map = _load_explicit_yaml(
        text,
        locator=locator,
        limits=limits or RestrictedYamlLimits(),
    )
    try:
        design = ExperimentDesign.from_payload(payload, source_map=source_map)
    except SchemaValidationError as exc:
        path = _schema_path_to_pointer(exc.path)
        raise RestrictedYamlError(
            "yaml.design_schema",
            str(exc),
            source=source_map.source_for(path),
            path=path,
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise RestrictedYamlError(
            "yaml.design",
            str(exc),
            source=source_map.fallback,
        ) from exc
    try:
        design.build()
    except DraftLoweringError as exc:
        issue = exc.issues[0]
        raise RestrictedYamlError(
            issue.code,
            issue.message,
            source=issue.source,
            path=issue.path,
        ) from exc
    return design


def load_yaml_design(
    text: str,
    *,
    locator: str = "<string>",
    limits: RestrictedYamlLimits | None = None,
) -> ComposedExperiment:
    """Return the same canonical result as the typed compositional API."""

    return parse_yaml_design(text, locator=locator, limits=limits).build()


def read_yaml_design(
    path: str | Path,
    *,
    limits: RestrictedYamlLimits | None = None,
) -> ComposedExperiment:
    source = Path(path)
    effective_limits = limits or RestrictedYamlLimits()
    if source.stat().st_size > effective_limits.max_bytes:
        raise RestrictedYamlError(
            "yaml.limit_bytes",
            f"input exceeds {effective_limits.max_bytes} UTF-8 bytes",
            source=SourceLocation(SourceKind.YAML, str(source), line=1, column=1),
        )
    return load_yaml_design(
        source.read_text(encoding="utf-8"),
        locator=str(source),
        limits=effective_limits,
    )


def _load_explicit_yaml(
    text: str,
    *,
    locator: str,
    limits: RestrictedYamlLimits,
) -> tuple[dict[str, Any], DraftSourceMap]:
    if not isinstance(text, str):
        raise TypeError("restricted YAML input must be text")
    fallback = SourceLocation(SourceKind.YAML, locator, line=1, column=1)
    if len(text.encode("utf-8")) > limits.max_bytes:
        raise RestrictedYamlError(
            "yaml.limit_bytes",
            f"input exceeds {limits.max_bytes} UTF-8 bytes",
            source=fallback,
        )
    if not text.strip():
        raise RestrictedYamlError(
            "yaml.empty",
            "document cannot be empty",
            source=fallback,
        )

    YAML = _yaml_class(fallback)
    scanner = _yaml_instance(YAML)
    try:
        tokens = tuple(scanner.scan(text))
    except Exception as exc:
        raise _parser_error("yaml.syntax", exc, locator, fallback) from exc
    forbidden_tokens = {
        "AliasToken": "aliases",
        "AnchorToken": "anchors",
        "TagToken": "explicit or custom tags",
        "DirectiveToken": "directives",
    }
    for token in tokens:
        name = type(token).__name__
        if name in forbidden_tokens:
            raise RestrictedYamlError(
                f"yaml.forbidden_{name[:-5].lower()}",
                f"{forbidden_tokens[name]} are not allowed",
                source=_mark_location(getattr(token, "start_mark", None), locator, fallback),
            )
    _inspect_token_depth(tokens, locator, fallback, limits)

    composer = _yaml_instance(YAML)
    try:
        documents = tuple(composer.compose_all(text))
    except Exception as exc:
        raise _parser_error("yaml.syntax", exc, locator, fallback) from exc
    if len(documents) != 1 or documents[0] is None:
        raise RestrictedYamlError(
            "yaml.document_count",
            "exactly one non-empty document is required",
            source=fallback,
        )
    root = documents[0]
    locations: dict[str, SourceLocation] = {}
    _inspect_node(
        root,
        path="",
        depth=1,
        locator=locator,
        fallback=fallback,
        limits=limits,
        state={"nodes": 0},
        locations=locations,
    )

    loader = _yaml_instance(YAML)
    try:
        values = tuple(loader.load_all(text))
    except Exception as exc:
        raise _parser_error("yaml.construct", exc, locator, fallback) from exc
    if len(values) != 1 or values[0] is None:
        raise RestrictedYamlError(
            "yaml.document_count",
            "exactly one non-empty document is required",
            source=fallback,
        )
    try:
        explicit = thaw_json(freeze_json(values[0]))
    except (TypeError, ValueError) as exc:
        raise RestrictedYamlError(
            "yaml.non_json",
            str(exc),
            source=fallback,
        ) from exc
    if not isinstance(explicit, dict):
        raise RestrictedYamlError(
            "yaml.root",
            "document root must be an object",
            source=locations.get("", fallback),
        )
    return explicit, DraftSourceMap(locations, fallback=fallback)


def _yaml_class(fallback: SourceLocation):
    try:
        from ruamel.yaml import YAML
    except (ImportError, ModuleNotFoundError) as exc:
        raise RestrictedYamlError(
            "yaml.unavailable",
            "install the optional 'eegle[yaml]' dependency",
            source=fallback,
        ) from exc
    return YAML


def _yaml_instance(YAML):
    value = YAML(typ="safe", pure=True)
    value.version = (1, 2)
    value.allow_duplicate_keys = False
    return value


def _inspect_token_depth(
    tokens: tuple[Any, ...],
    locator: str,
    fallback: SourceLocation,
    limits: RestrictedYamlLimits,
) -> None:
    """Reject deeply nested collections before constructing a node graph."""

    opening = {
        "BlockMappingStartToken",
        "BlockSequenceStartToken",
        "FlowMappingStartToken",
        "FlowSequenceStartToken",
    }
    closing = {
        "BlockEndToken",
        "FlowMappingEndToken",
        "FlowSequenceEndToken",
    }
    depth = 0
    for token in tokens:
        name = type(token).__name__
        if name in opening:
            depth += 1
            if depth > limits.max_depth:
                raise RestrictedYamlError(
                    "yaml.limit_depth",
                    f"document exceeds depth {limits.max_depth}",
                    source=_mark_location(
                        getattr(token, "start_mark", None),
                        locator,
                        fallback,
                    ),
                )
        elif name in closing:
            depth = max(0, depth - 1)


def _inspect_node(
    node: Any,
    *,
    path: str,
    depth: int,
    locator: str,
    fallback: SourceLocation,
    limits: RestrictedYamlLimits,
    state: dict[str, int],
    locations: dict[str, SourceLocation],
) -> None:
    state["nodes"] += 1
    source = _mark_location(getattr(node, "start_mark", None), locator, fallback)
    locations[path] = source
    if state["nodes"] > limits.max_nodes:
        raise RestrictedYamlError(
            "yaml.limit_nodes",
            f"document exceeds {limits.max_nodes} nodes",
            source=source,
            path=path,
        )
    if depth > limits.max_depth:
        raise RestrictedYamlError(
            "yaml.limit_depth",
            f"document exceeds depth {limits.max_depth}",
            source=source,
            path=path,
        )
    tag = str(getattr(node, "tag", ""))
    allowed_tags = {
        "tag:yaml.org,2002:map",
        "tag:yaml.org,2002:seq",
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:null",
    }
    if tag == "tag:yaml.org,2002:timestamp":
        raise RestrictedYamlError(
            "yaml.implicit_date",
            "implicit date and datetime values are not allowed; quote them",
            source=source,
            path=path,
        )
    if tag not in allowed_tags:
        raise RestrictedYamlError(
            "yaml.tag",
            f"tag is not in the JSON-compatible subset: {tag}",
            source=source,
            path=path,
        )

    name = type(node).__name__
    if name == "ScalarNode":
        if len(str(node.value)) > limits.max_scalar_characters:
            raise RestrictedYamlError(
                "yaml.limit_scalar",
                f"scalar exceeds {limits.max_scalar_characters} characters",
                source=source,
                path=path,
            )
        return
    if name == "SequenceNode":
        for index, child in enumerate(node.value):
            _inspect_node(
                child,
                path=f"{path}/{index}",
                depth=depth + 1,
                locator=locator,
                fallback=fallback,
                limits=limits,
                state=state,
                locations=locations,
            )
        return
    if name != "MappingNode":
        raise RestrictedYamlError(
            "yaml.node",
            f"unsupported YAML node: {name}",
            source=source,
            path=path,
        )
    for key, child in node.value:
        key_source = _mark_location(getattr(key, "start_mark", None), locator, fallback)
        if str(getattr(key, "value", "")) == "<<":
            raise RestrictedYamlError(
                "yaml.merge",
                "merge keys are not allowed",
                source=key_source,
                path=path,
            )
        if type(key).__name__ != "ScalarNode" or str(key.tag) != "tag:yaml.org,2002:str":
            raise RestrictedYamlError(
                "yaml.key",
                "mapping keys must be strings",
                source=key_source,
                path=path,
            )
        child_path = f"{path}/{_escape_pointer(str(key.value))}"
        _inspect_node(
            child,
            path=child_path,
            depth=depth + 1,
            locator=locator,
            fallback=fallback,
            limits=limits,
            state=state,
            locations=locations,
        )


def _parser_error(
    code: str,
    error: Exception,
    locator: str,
    fallback: SourceLocation,
) -> RestrictedYamlError:
    mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
    return RestrictedYamlError(
        code,
        str(error).splitlines()[0],
        source=_mark_location(mark, locator, fallback),
    )


def _mark_location(mark: Any, locator: str, fallback: SourceLocation) -> SourceLocation:
    if mark is None:
        return fallback
    return SourceLocation(
        SourceKind.YAML,
        locator,
        line=int(mark.line) + 1,
        column=int(mark.column) + 1,
    )


def _schema_path_to_pointer(path: str) -> str:
    if path == "$":
        return ""
    value = path[1:]
    parts: list[str] = []
    while value:
        if value.startswith("."):
            value = value[1:]
            end = len(value)
            for marker in (".", "["):
                index = value.find(marker)
                if index >= 0:
                    end = min(end, index)
            parts.append(value[:end])
            value = value[end:]
        elif value.startswith("["):
            end = value.index("]")
            parts.append(value[1:end])
            value = value[end + 1 :]
        else:
            break
    return "".join(f"/{_escape_pointer(part)}" for part in parts)


def _escape_pointer(value: str) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")
