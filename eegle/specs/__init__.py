"""Portable scientific, suite, and deployment specifications."""

from eegle.specs.deployment import (
    ClockMappingStrategy,
    ClockMappingBinding,
    ComponentBindingSpec,
    DeploymentSpec,
    PermissionGrant,
    Placement,
    ResourceSpec,
    SecretReference,
    StorageBinding,
    StreamBinding,
)
from eegle.specs.protocol import (
    AcceptanceCriterion,
    ClaimSpec,
    ComparisonOperator,
    MetricSpec,
    ProtocolSpec,
)
from eegle.specs.schemas import SchemaValidationError, validate_schema, validate_payload
from eegle.specs.suite import (
    ComponentSpec,
    LogicalStreamSpec,
    PhaseSpec,
    PhaseTransition,
    ResumePolicy,
    RouteSpec,
    SignalContract,
    SuiteOverlay,
    SuiteSpec,
    TransitionCondition,
    compose_suite,
)


__all__ = [
    "AcceptanceCriterion",
    "ClaimSpec",
    "ClockMappingBinding",
    "ClockMappingStrategy",
    "ComparisonOperator",
    "ComponentBindingSpec",
    "ComponentSpec",
    "DeploymentSpec",
    "LogicalStreamSpec",
    "MetricSpec",
    "PermissionGrant",
    "PhaseSpec",
    "PhaseTransition",
    "Placement",
    "ProtocolSpec",
    "ResourceSpec",
    "ResumePolicy",
    "RouteSpec",
    "SchemaValidationError",
    "SecretReference",
    "SignalContract",
    "StorageBinding",
    "StreamBinding",
    "SuiteOverlay",
    "SuiteSpec",
    "TransitionCondition",
    "compose_suite",
    "validate_payload",
    "validate_schema",
]
