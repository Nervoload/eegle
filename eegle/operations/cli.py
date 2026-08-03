"""Internal command rendering over the public operations service layer."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from eegle import __version__
from eegle._validation import thaw_json
from eegle.compiler import read_plan
from eegle.models import (
    ModelArtifactSource,
    ModelManifest,
    ModelPackageIntegrityError,
    check_model_package,
    load_synthetic_test_vectors,
    pack_model,
)
from eegle.operations.contracts import (
    OPERATION_RESULT_SCHEMA_ID,
    ExitCode,
    OperationCategory,
    OperationDiagnostic,
    OperationError,
    validate_operation_result_payload,
)
from eegle.operations.discovery import (
    DeploymentSelection,
    detect_capabilities,
    read_capability_observations,
    read_model_observations,
)
from eegle.operations.plugin_tools import check_plugin, inspect_plugins
from eegle.operations.projects import (
    PROJECT_MANIFEST_NAME,
    compile_project,
    create_project,
    diff_projects,
    explain_project,
    graph_project,
    open_project,
    preflight_project,
    propose_project_deployment,
    record_detection_report,
    rehearse_project,
    run_project,
)
from eegle.operations.sessions import (
    OperationOutcome,
    compare_session_models,
    export_session,
    inspect_session,
    replay_session,
)
from eegle.operations.validation import validate_target
from eegle.plugins import PluginRegistry
from eegle.validation import ValidationStatus

COMMAND_RESULT_SCHEMA_ID = OPERATION_RESULT_SCHEMA_ID


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


class _UsageError(ValueError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="eegle",
        description="Artifact-oriented EEGle experiment operations.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="machine",
        help="emit one machine-readable JSON result",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="create a non-overwriting simulation project")
    new.add_argument("project", help="new project directory")
    new.add_argument("--id", dest="project_id", help="portable project/draft identity")
    authoring_source = new.add_mutually_exclusive_group()
    authoring_source.add_argument(
        "--template",
        help="exact template identity",
    )
    new.add_argument("--template-version", default="1.0.0")
    authoring_source.add_argument(
        "--preset",
        help="exact compositional preset identity",
    )
    new.add_argument("--preset-version", default="2.0.0")
    authoring_source.add_argument(
        "--design",
        help="normalized eegle.experiment_design.v1 JSON source",
    )
    new.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="set one explicit finite JSON template parameter",
    )
    new.add_argument(
        "--grant-simulated-adaptation",
        action="store_true",
        help="explicitly authorize model adaptation in the generated simulation deployment",
    )

    compile_command = commands.add_parser(
        "compile", help="compile current canonical specs against the reviewed deployment"
    )
    compile_command.add_argument("project")
    compile_command.add_argument(
        "--deployment",
        default="simulation_deployment",
        help="exact indexed deployment artifact role",
    )

    detect = commands.add_parser(
        "detect",
        help="inspect installed capabilities and optionally propose a deployment",
    )
    detect.add_argument("project", nargs="?")
    detect.add_argument("--observations", help="typed integration detector output JSON")
    detect.add_argument(
        "--model-manifest",
        action="append",
        default=[],
        metavar="PATH",
        help="exact model manifest file or flat manifest directory",
    )
    detect.add_argument("--id", dest="detection_id")
    detect.add_argument("--observed-at", help="explicit ISO-8601 UTC capture time")
    detect.add_argument(
        "--lsl",
        action="store_true",
        help="run the optional first-party LSL detector",
    )
    detect.add_argument(
        "--lsl-wait",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="bounded LSL discovery wait (default: 1.0)",
    )
    detect.add_argument(
        "--no-entry-points",
        action="store_true",
        help="inspect only base plugins (useful for hermetic automation)",
    )
    detect.add_argument(
        "--propose",
        action="store_true",
        help="write a review-only deployment proposal for PROJECT",
    )
    for option, destination, help_text in (
        (
            "--select-source",
            "source_selections",
            "select COMPONENT_OR_REQUIREMENT=CAPABILITY",
        ),
        (
            "--select-storage",
            "storage_selections",
            "select REQUIREMENT=CAPABILITY",
        ),
        (
            "--select-clock",
            "clock_selections",
            "select REQUIREMENT=CAPABILITY",
        ),
        (
            "--select-placement",
            "placement_selections",
            "select COMPONENT=CAPABILITY",
        ),
        (
            "--select-plugin",
            "plugin_selections",
            "select COMPONENT=PLUGIN",
        ),
    ):
        detect.add_argument(
            option,
            action="append",
            default=[],
            dest=destination,
            metavar="NAME=ID",
            help=help_text,
        )

    explain = commands.add_parser(
        "explain", help="explain authored intent and its matching plan when available"
    )
    explain.add_argument("project")

    diff = commands.add_parser("diff", help="compare authored and optional locked project values")
    diff.add_argument("before")
    diff.add_argument("after")

    graph = commands.add_parser("graph", help="show the verified compiled execution graph")
    graph.add_argument("project")

    rehearse = commands.add_parser(
        "rehearse", help="execute the simulation lock through the normal evidence path"
    )
    rehearse.add_argument("project")
    rehearse.add_argument("--session-id")
    rehearse.add_argument(
        "--confirm-gate",
        action="append",
        default=[],
        metavar="GATE_ID",
        help="confirm one exact phase.* or permission.* operator gate",
    )
    rehearse.add_argument(
        "--secret-provider",
        action="append",
        default=[],
        metavar="PROVIDER_ID",
        help="attest one referenced secret provider is available",
    )

    preflight = commands.add_parser(
        "preflight", help="verify the current lock against declared capabilities"
    )
    preflight.add_argument("project")
    preflight.add_argument(
        "--confirm-gate",
        action="append",
        default=[],
        metavar="GATE_ID",
        help="confirm one exact phase.* or permission.* operator gate",
    )
    preflight.add_argument(
        "--safe-state",
        action="append",
        default=[],
        metavar="COMPONENT_ID",
        help="attest one physical actuator safe-state report",
    )
    preflight.add_argument(
        "--secret-provider",
        action="append",
        default=[],
        metavar="PROVIDER_ID",
        help="attest one referenced secret provider is available",
    )

    run = commands.add_parser("run", help="execute a project's verified plan/lock pair")
    run.add_argument("project")
    run.add_argument("--session-id")

    inspect = commands.add_parser("inspect", help="verify and summarize a session")
    inspect.add_argument("target", help="session directory, or a project with sessions")
    inspect.add_argument(
        "--strict",
        action="store_true",
        help="return a non-zero status when evidence needs attention",
    )

    validate = commands.add_parser(
        "validate",
        help="read-only validation of a project or immutable session bundle",
    )
    validate.add_argument("target", help="project or session directory")
    validate.add_argument("--bundle-id")
    validate.add_argument(
        "--no-replay",
        action="store_true",
        help="skip replay and report replay equivalence as insufficient evidence",
    )
    validate.add_argument(
        "--strict",
        action="store_true",
        help="return a non-zero status for failure or insufficient evidence",
    )

    replay = commands.add_parser("replay", help="replay one immutable evidence bundle")
    replay.add_argument("target", help="session directory, or a project with sessions")
    replay.add_argument("--bundle-id")
    replay.add_argument(
        "--strict",
        action="store_true",
        help="return a non-zero status when replay is unavailable or diverges",
    )

    compare = commands.add_parser(
        "compare", help="replay recorded inputs through a replacement model plan"
    )
    compare.add_argument("target", help="session directory, or a project with sessions")
    compare.add_argument("replacement_plan", help="separately compiled candidate plan JSON")
    compare.add_argument("--bundle-id")
    compare.add_argument(
        "--replace-model",
        action="append",
        required=True,
        metavar="COMPONENT_ID",
        help="name one model component intentionally replaced by the candidate",
    )
    compare.add_argument(
        "--strict",
        action="store_true",
        help="return a non-zero status only when comparison cannot complete",
    )

    export = commands.add_parser(
        "export", help="publish a non-overwriting privacy-safe session export"
    )
    export.add_argument("target", help="session directory, or a project with sessions")
    export.add_argument("destination", help="new portable export directory")
    export.add_argument("--bundle-id")
    export.add_argument(
        "--strict",
        action="store_true",
        help="return a non-zero status when no export is published",
    )

    model = commands.add_parser(
        "model", help="pack and verify framework-neutral model packages"
    )
    model_actions = model.add_subparsers(dest="model_action", required=True)
    model_pack = model_actions.add_parser(
        "pack", help="package a canonical manifest and its local artifact bytes"
    )
    model_pack.add_argument("manifest", help="canonical model manifest JSON")
    model_pack.add_argument("output", help="new .eegle-model package path")
    model_pack.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="ARTIFACT_ID=PATH",
        help="bind one exact manifest artifact to a local file",
    )
    model_pack.add_argument(
        "--vectors",
        help="optional eegle.synthetic_model_test_vectors.v1 JSON file",
    )

    model_check = model_actions.add_parser(
        "check", help="verify package bytes, vectors, and installed adapters"
    )
    model_check.add_argument("package", help="model package to verify")
    model_check.add_argument(
        "--no-entry-points",
        action="store_true",
        help="check only base plugins (useful for hermetic automation)",
    )
    model_check.add_argument(
        "--require-compatible-plugin",
        action="store_true",
        help="reject the package unless a compatible model plugin is installed",
    )

    plugin = commands.add_parser(
        "plugin", help="inspect descriptors and explicitly check installed plugins"
    )
    plugin_actions = plugin.add_subparsers(dest="plugin_action", required=True)
    plugin_inspect = plugin_actions.add_parser(
        "inspect", help="inspect descriptors without constructing components"
    )
    plugin_inspect.add_argument("plugin_id", nargs="?")
    plugin_inspect.add_argument("--version-spec")
    plugin_inspect.add_argument(
        "--no-entry-points",
        action="store_true",
        help="inspect only first-party base descriptors",
    )
    plugin_check = plugin_actions.add_parser(
        "check", help="check a descriptor and optionally construct its component"
    )
    plugin_check.add_argument("plugin_id")
    plugin_check.add_argument("--version-spec")
    plugin_check.add_argument(
        "--config",
        help="JSON object used only when explicit construction is requested",
    )
    plugin_check.add_argument(
        "--construct",
        action="store_true",
        help="explicitly invoke the factory and structural component checks",
    )
    plugin_check.add_argument(
        "--no-entry-points",
        action="store_true",
        help="check only first-party base descriptors",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    machine = "--json" in arguments
    parser = build_parser()
    operation = _operation_hint(arguments)
    try:
        parsed = parser.parse_args(arguments)
        machine = bool(parsed.machine)
        payload = _dispatch(parsed)
        envelope = {
            "schema": COMMAND_RESULT_SCHEMA_ID,
            "ok": True,
            "operation": parsed.command,
            "exit_code": int(ExitCode.SUCCESS),
            "result": thaw_json(payload),
        }
        validate_operation_result_payload(envelope)
        _render_success(parsed.command, envelope, machine=machine)
        return int(ExitCode.SUCCESS)
    except _UsageError as exc:
        error = _error(
            operation,
            ExitCode.USAGE_ERROR,
            "cli.usage",
            OperationCategory.USAGE,
            "Invalid command usage",
            str(exc),
        )
    except OperationError as exc:
        error = exc
    except ModelPackageIntegrityError as exc:
        error = _error(
            operation,
            ExitCode.INTEGRITY_FAILED,
            "model.package_integrity",
            OperationCategory.INTEGRITY,
            "Model package failed integrity verification",
            str(exc),
        )
    except FileExistsError as exc:
        error = _error(
            operation,
            ExitCode.REJECTED,
            "model.package_exists"
            if operation == "model"
            else "project.already_exists",
            OperationCategory.AUTHORING,
            "Refusing to overwrite an existing model package"
            if operation == "model"
            else "Refusing to overwrite existing artifacts",
            str(exc),
        )
    except FileNotFoundError as exc:
        error = _error(
            operation,
            ExitCode.INVALID_INPUT,
            "artifact.not_found",
            OperationCategory.SCHEMA,
            "Required artifact was not found",
            str(exc),
        )
    except (KeyError, TypeError, ValueError) as exc:
        error = _error(
            operation,
            ExitCode.INVALID_INPUT,
            "artifact.invalid",
            OperationCategory.SCHEMA,
            "Artifact or parameter is invalid",
            str(exc),
        )
    except Exception as exc:  # pragma: no cover - defensive process boundary
        error = _error(
            operation,
            ExitCode.INTERNAL_ERROR,
            "internal.unexpected",
            OperationCategory.INTERNAL,
            "Unexpected internal failure",
            str(exc),
        )
    _render_error(error, machine=machine)
    return int(error.exit_code)


def _dispatch(args: argparse.Namespace) -> Mapping[str, Any]:
    if args.command == "new":
        project_path = Path(args.project).expanduser()
        project_id = args.project_id or project_path.name
        parameters = _parse_parameters(args.set)
        return create_project(
            project_path,
            project_id=project_id,
            template_id=args.template or "eegle.template.continuous_recording",
            template_version=args.template_version,
            parameters=parameters,
            design=args.design,
            preset_id=args.preset,
            preset_version=args.preset_version,
            grant_simulated_adaptation=args.grant_simulated_adaptation,
        ).to_payload()
    if args.command == "compile":
        return compile_project(
            args.project,
            deployment_role=args.deployment,
        ).to_payload()
    if args.command == "detect":
        observations: Mapping[str, tuple[Any, ...]] = {
            "sources": (),
            "storage": (),
            "clocks": (),
            "placements": (),
            "authorization_providers": (),
        }
        if args.observations is not None:
            observations = read_capability_observations(args.observations)
        lsl_detection = None
        detector_registry = None
        include_entry_points = not args.no_entry_points
        if args.lsl:
            from eegle.integrations.lsl import detect_lsl, lsl_plugin_descriptors

            lsl_detection = detect_lsl(wait_time=args.lsl_wait)
            observations = {
                **observations,
                "sources": (*observations["sources"], *lsl_detection.sources),
                "clocks": (*observations["clocks"], *lsl_detection.clocks),
            }
            detector_registry = PluginRegistry()
            detector_registry.register_builtins()
            if include_entry_points:
                detector_registry.load_entry_points()
            installed = {
                (value.plugin_id, value.version)
                for value in detector_registry.descriptors()
            }
            for descriptor in lsl_plugin_descriptors():
                if (descriptor.plugin_id, descriptor.version) not in installed:
                    detector_registry.register(descriptor)
            include_entry_points = False
        report = detect_capabilities(
            registry=detector_registry,
            include_entry_points=include_entry_points,
            models=read_model_observations(args.model_manifest),
            sources=observations["sources"],
            storage=observations["storage"],
            clocks=observations["clocks"],
            placements=observations["placements"],
            authorization_providers=observations["authorization_providers"],
            detection_id=args.detection_id,
            observed_at=args.observed_at,
        )
        if args.propose:
            if args.project is None:
                raise _UsageError("detect --propose requires PROJECT")
            selection = DeploymentSelection(
                source_capabilities=_parse_identity_assignments(
                    args.source_selections, "--select-source"
                ),
                storage_capabilities=_parse_identity_assignments(
                    args.storage_selections, "--select-storage"
                ),
                clock_capabilities=_parse_identity_assignments(
                    args.clock_selections, "--select-clock"
                ),
                placement_capabilities=_parse_identity_assignments(
                    args.placement_selections, "--select-placement"
                ),
                component_plugins=_parse_identity_assignments(
                    args.plugin_selections, "--select-plugin"
                ),
            )
            result = propose_project_deployment(
                args.project,
                report,
                selection=selection,
            ).to_payload()
            if lsl_detection is not None:
                result["lsl_support"] = lsl_detection.support.to_payload()
            return result
        if args.project is not None:
            project = record_detection_report(args.project, report)
            result = {
                "schema": "eegle.project_detection.v1",
                "project_root": str(project.root),
                "report_hash": report.report_hash,
                "report_uri": project.manifest.artifact("detection_report").uri,
                "report": report.to_payload(),
            }
            if lsl_detection is not None:
                result["lsl_support"] = lsl_detection.support.to_payload()
            return result
        result = report.to_payload()
        if lsl_detection is not None:
            result["lsl_support"] = lsl_detection.support.to_payload()
        return result
    if args.command == "explain":
        return explain_project(args.project)
    if args.command == "diff":
        return diff_projects(args.before, args.after)
    if args.command == "graph":
        return graph_project(args.project)
    if args.command == "preflight":
        report = preflight_project(
            args.project,
            operator_confirmations=tuple(args.confirm_gate),
            safe_state_reports=tuple(args.safe_state),
            available_secret_providers=tuple(args.secret_provider),
        )
        if not report.ready:
            first = next(value for value in report.checks if value.status.value == "fail")
            raise _error(
                "preflight",
                ExitCode.REJECTED,
                "preflight.failed",
                OperationCategory.PREFLIGHT,
                "Preflight rejected the locked deployment",
                first.summary,
            )
        return report.to_payload()
    if args.command == "rehearse":
        result = rehearse_project(
            args.project,
            session_id=args.session_id,
            operator_confirmations=tuple(args.confirm_gate),
            available_secret_providers=tuple(args.secret_provider),
        )
        _require_successful_run(result)
        return result.to_payload()
    if args.command == "run":
        result = run_project(args.project, session_id=args.session_id)
        _require_successful_run(result)
        return result.to_payload()
    if args.command == "inspect":
        target = _session_target(args.target, require_session=False)
        if target is None:
            return {
                "schema": "eegle.project_inspection.v1",
                **open_project(args.target).to_payload(),
                "latest_session": None,
            }
        inspection = inspect_session(target)
        if args.strict and inspection.outcome != OperationOutcome.COMPLETE:
            _raise_strict_result("inspect", inspection.to_payload())
        return inspection.to_payload()
    if args.command == "validate":
        report = validate_target(
            args.target,
            bundle_id=args.bundle_id,
            replay=not args.no_replay,
        )
        if args.strict and report.status in {
            ValidationStatus.FAIL,
            ValidationStatus.INSUFFICIENT_EVIDENCE,
        }:
            _raise_strict_validation(report.to_payload())
        return report.to_payload()
    if args.command == "replay":
        target = _session_target(args.target, require_session=True)
        assert target is not None
        result = replay_session(target, bundle_id=args.bundle_id)
        if args.strict and result.outcome != OperationOutcome.COMPLETE:
            _raise_strict_result("replay", result.to_payload())
        return result.to_payload()
    if args.command == "compare":
        target = _session_target(args.target, require_session=True)
        assert target is not None
        result = compare_session_models(
            target,
            read_plan(args.replacement_plan),
            bundle_id=args.bundle_id,
            replaced_components=tuple(args.replace_model),
        )
        if args.strict and result.outcome not in {
            OperationOutcome.COMPLETE,
            OperationOutcome.DIVERGED,
        }:
            _raise_strict_result("compare", result.to_payload())
        return result.to_payload()
    if args.command == "export":
        target = _session_target(args.target, require_session=True)
        assert target is not None
        result = export_session(
            target,
            args.destination,
            bundle_id=args.bundle_id,
        )
        if args.strict and not result.published:
            _raise_strict_result("export", result.to_payload())
        return result.to_payload()
    if args.command == "model":
        if args.model_action == "pack":
            manifest = ModelManifest.from_payload(_read_json_object(args.manifest))
            assignments = _parse_identity_assignments(
                args.artifact, "--artifact"
            )
            references = {
                value.artifact_id: value for value in manifest.artifacts
            }
            if set(assignments) != set(references):
                raise _UsageError(
                    "--artifact bindings must exactly match manifest artifact identities"
                )
            sources = tuple(
                ModelArtifactSource.from_file(
                    artifact_id,
                    references[artifact_id].role,
                    references[artifact_id].media_type,
                    path,
                    sensitivity=references[artifact_id].sensitivity,
                )
                for artifact_id, path in sorted(assignments.items())
            )
            vectors = (
                ()
                if args.vectors is None
                else load_synthetic_test_vectors(args.vectors)
            )
            return pack_model(
                manifest,
                sources,
                args.output,
                test_vectors=vectors,
            ).to_payload()
        if args.model_action == "check":
            registry = PluginRegistry()
            registry.register_builtins()
            if not args.no_entry_points:
                registry.load_entry_points()
            report = check_model_package(
                args.package,
                registry=registry,
                require_implementation=args.require_compatible_plugin,
            )
            if not report.ready:
                first = next(value for value in report.issues if value.severity == "error")
                raise OperationError(
                    "model",
                    ExitCode.REJECTED,
                    (
                        OperationDiagnostic(
                            first.code,
                            OperationCategory.AVAILABILITY,
                            "Model package is not executable in this environment",
                            first.message,
                            path=first.path,
                            details={"check": report.to_payload()},
                        ),
                    ),
                )
            return report.to_payload()
        raise _UsageError(f"unsupported model action: {args.model_action}")
    if args.command == "plugin":
        if args.plugin_action == "inspect":
            return inspect_plugins(
                args.plugin_id,
                version_spec=args.version_spec,
                include_entry_points=not args.no_entry_points,
            ).to_payload()
        if args.plugin_action == "check":
            if args.config is not None and not args.construct:
                raise _UsageError("plugin check --config requires --construct")
            report = check_plugin(
                args.plugin_id,
                version_spec=args.version_spec,
                include_entry_points=not args.no_entry_points,
                config={}
                if args.config is None
                else _read_json_object(args.config),
                construct=args.construct,
            )
            if not report.ready:
                first = next(
                    value for value in report.checks if value.status.value == "fail"
                )
                raise OperationError(
                    "plugin",
                    ExitCode.REJECTED,
                    (
                        OperationDiagnostic(
                            "plugin.conformance_failed",
                            OperationCategory.AVAILABILITY,
                            "Plugin conformance check failed",
                            first.summary,
                            details={"check": report.to_payload()},
                        ),
                    ),
                )
            return report.to_payload()
        raise _UsageError(f"unsupported plugin action: {args.plugin_action}")
    raise _UsageError(f"unsupported command: {args.command}")


def _parse_parameters(values: Sequence[str]) -> Mapping[str, Any]:
    parsed: dict[str, Any] = {}
    for assignment in values:
        if "=" not in assignment:
            raise _UsageError("--set requires NAME=JSON")
        name, encoded = assignment.split("=", 1)
        if not name.strip() or name in parsed:
            raise _UsageError(f"invalid or duplicate --set parameter: {name!r}")
        try:
            parsed[name] = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise _UsageError(f"--set {name} is not valid JSON: {exc.msg}") from exc
    return parsed


def _parse_identity_assignments(
    values: Sequence[str], option: str
) -> Mapping[str, str]:
    parsed: dict[str, str] = {}
    for assignment in values:
        if "=" not in assignment:
            raise _UsageError(f"{option} requires NAME=ID")
        name, identity = assignment.split("=", 1)
        if not name.strip() or not identity.strip() or name in parsed:
            raise _UsageError(f"invalid or duplicate {option} selection: {assignment!r}")
        parsed[name] = identity
    return parsed


def _read_json_object(path: str | Path) -> Mapping[str, Any]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON artifact must contain an object: {path}")
    return payload


def _session_target(value: str, *, require_session: bool) -> Path | None:
    target = Path(value).expanduser().resolve()
    if (target / PROJECT_MANIFEST_NAME).is_file():
        project = open_project(target)
        if not project.manifest.session_uris:
            if require_session:
                # Read-only services turn this absent path into structured
                # unavailable evidence without mutating or stopping anything.
                return project.root / "sessions"
            return None
        return project.root / project.manifest.session_uris[-1]
    return target


def _raise_strict_result(operation: str, result: Mapping[str, Any]) -> None:
    issues = result.get("issues") or ()
    first = issues[0] if issues and isinstance(issues[0], Mapping) else {}
    divergence = result.get("first_divergence")
    divergence = divergence if isinstance(divergence, Mapping) else {}
    status = str(result.get("status") or "unavailable")
    if status in {
        OperationOutcome.UNAVAILABLE.value,
        OperationOutcome.NOT_EXPORTED.value,
    }:
        exit_code = ExitCode.INSUFFICIENT_EVIDENCE
        category = OperationCategory.INSUFFICIENT_EVIDENCE
    else:
        exit_code = ExitCode.INTEGRITY_FAILED
        category = OperationCategory.INTEGRITY
    raise OperationError(
        operation,
        exit_code,
        (
            OperationDiagnostic(
                f"{operation}.strict_attention_required",
                category,
                f"{operation.capitalize()} result needs attention",
                str(first.get("message") or f"Operation status is {status}."),
                path=str(divergence.get("path") or "$"),
                details={"result": result},
            ),
        ),
    )


def _raise_strict_validation(result: Mapping[str, Any]) -> None:
    status = str(result.get("status"))
    failed = status == ValidationStatus.FAIL.value
    results = result.get("results") or ()
    first = next(
        (
            value
            for value in results
            if isinstance(value, Mapping) and value.get("status") == status
        ),
        {},
    )
    raise OperationError(
        "validate",
        ExitCode.INTEGRITY_FAILED if failed else ExitCode.INSUFFICIENT_EVIDENCE,
        (
            OperationDiagnostic(
                "validation.failed" if failed else "validation.insufficient_evidence",
                OperationCategory.INTEGRITY
                if failed
                else OperationCategory.INSUFFICIENT_EVIDENCE,
                "Validation failed" if failed else "Validation is inconclusive",
                str(first.get("summary") or f"Validation status is {status}."),
                details={"result": result},
            ),
        ),
    )


def _require_successful_run(result: Any) -> None:
    if result.successful:
        return
    raise OperationError(
        result.kind,
        ExitCode.EXECUTION_FAILED,
        (
            OperationDiagnostic(
                "execution.not_complete",
                OperationCategory.EXECUTION,
                "Execution did not complete",
                f"Engine status is {result.status.value}.",
                details={"run": result.to_payload()},
            ),
        ),
    )


def _render_success(operation: str, envelope: Mapping[str, Any], *, machine: bool) -> None:
    if machine:
        print(json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return
    result = envelope["result"]
    if operation == "new":
        print(f"Created EEGle project at {result['project_root']}")
    elif operation == "compile":
        print(f"Compiled plan {result['plan_hash']}")
        print(f"Lock: {result['lock_uri']}")
    elif operation == "detect":
        if result.get("schema") == "eegle.project_deployment_proposal.v1":
            print(f"Deployment proposal {result['proposal_hash']}")
            print(f"Review: {result['proposal_uri']}")
            print(f"Selectable deployment: {result['deployment_uri']}")
        elif result.get("schema") == "eegle.project_detection.v1":
            print(f"Detection report {result['report_hash']}")
            print(f"Report: {result['report_uri']}")
        else:
            print(f"Detection report {result['report_hash']}")
            print(
                "Observed: "
                f"{len(result.get('plugins', ()))} plugins, "
                f"{len(result.get('models', ()))} models, "
                f"{len(result.get('sources', ()))} sources"
            )
    elif operation in {"run", "rehearse"}:
        print(f"{operation.capitalize()} complete: {result['session_id']}")
        print(f"Session: {result['session_root']}")
        print(f"Bundle: {result['bundle_id']}")
    elif operation == "preflight":
        print(f"Preflight ready: {result['report_hash']}")
        print(f"Checks: {len(result['checks'])}")
    elif operation == "inspect":
        if result.get("latest_session") is None and result.get("schema") == "eegle.project_inspection.v1":
            print(f"Project: {result['project_root']} (no sessions)")
        else:
            print(f"Session inspection: {result['status']}")
            print(f"Integrity: {'valid' if result['valid'] else 'needs attention'}")
            print(f"Bundles: {len(result['bundles'])}")
    elif operation == "validate":
        print(f"Validation: {result['status']}")
        print(f"Checks: {len(result['results'])}")
        print(f"Report: {result['report_hash']}")
    elif operation == "replay":
        if result["status"] == OperationOutcome.UNAVAILABLE.value:
            print("Replay unavailable; recorded evidence was left unchanged")
        else:
            print(
                f"Replay {'equivalent' if result['equivalent'] else 'diverged'} "
                f"at {result['evaluated_level']} level"
            )
        print(f"Compared records: {result['compared_record_count']}")
    elif operation == "compare":
        print(f"Replacement comparison: {result['status']}")
        print(f"Compared records: {result['compared_record_count']}")
        first = result.get("first_divergence")
        if isinstance(first, Mapping):
            print(f"First divergence: {first['record_type']} {first['path']}")
    elif operation == "export":
        print(f"Session export: {result['status']}")
        print(f"Destination: {result['destination']}")
        if not result["published"]:
            print("No export was published; source and destination data were preserved")
    elif operation == "model":
        if result.get("schema") == "eegle.packed_model.v1":
            print(f"Packed model {result['manifest_digest']}")
            print(f"Package: {result['package_path']}")
        else:
            print(f"Model package ready: {result['ready']}")
            print(f"Package digest: {result['package_digest']}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def _render_error(error: OperationError, *, machine: bool) -> None:
    if machine:
        print(
            json.dumps(
                error.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    print(f"eegle {error.operation}: failed (exit {int(error.exit_code)})", file=sys.stderr)
    for diagnostic in error.diagnostics:
        print(f"  {diagnostic.code}: {diagnostic.title}", file=sys.stderr)
        print(f"    {diagnostic.message}", file=sys.stderr)


def _operation_hint(arguments: Sequence[str]) -> str:
    commands = {
        "new",
        "detect",
        "compile",
        "explain",
        "diff",
        "graph",
        "preflight",
        "rehearse",
        "run",
        "inspect",
        "validate",
        "replay",
        "compare",
        "export",
        "model",
    }
    return next((value for value in arguments if value in commands), "cli")


def _error(
    operation: str,
    exit_code: ExitCode,
    code: str,
    category: OperationCategory,
    title: str,
    message: str,
) -> OperationError:
    return OperationError(
        operation,
        exit_code,
        (OperationDiagnostic(code, category, title, message),),
    )
