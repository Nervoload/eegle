"""Human- and machine-readable plan explanation and material diffing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.plan import ExecutionPlan


class ChangeMateriality(str, Enum):
    SCIENTIFIC = "scientific"
    OPERATIONAL = "operational"


@dataclass(frozen=True, slots=True)
class PlanChange:
    path: str
    materiality: ChangeMateriality
    before: Any
    after: Any

    def __post_init__(self) -> None:
        if not self.path.startswith("$"):
            raise ValueError("plan change path must start at '$'")
        object.__setattr__(self, "materiality", ChangeMateriality(self.materiality))
        object.__setattr__(self, "before", freeze_json(self.before))
        object.__setattr__(self, "after", freeze_json(self.after))

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "materiality": self.materiality.value,
            "before": thaw_json(self.before),
            "after": thaw_json(self.after),
        }


@dataclass(frozen=True, slots=True)
class PlanDiff:
    before_plan_hash: str
    after_plan_hash: str
    changes: tuple[PlanChange, ...]

    @property
    def equivalent(self) -> bool:
        return not self.changes

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.plan_diff.v1",
            "before_plan_hash": self.before_plan_hash,
            "after_plan_hash": self.after_plan_hash,
            "equivalent": self.equivalent,
            "changes": [value.to_payload() for value in self.changes],
        }


@dataclass(frozen=True, slots=True)
class PlanExplanation:
    plan_id: str
    plan_hash: str
    execution_mode: str
    component_order: tuple[str, ...]
    components: tuple[Mapping[str, Any], ...]
    phases: tuple[Mapping[str, Any], ...]
    placements: tuple[Mapping[str, Any], ...]
    claims: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", require_identifier(self.plan_id, "plan_id"))
        object.__setattr__(self, "components", tuple(freeze_json(value) for value in self.components))
        object.__setattr__(self, "phases", tuple(freeze_json(value) for value in self.phases))
        object.__setattr__(self, "placements", tuple(freeze_json(value) for value in self.placements))
        object.__setattr__(self, "claims", tuple(freeze_json(value) for value in self.claims))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.plan_explanation.v1",
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "execution_mode": self.execution_mode,
            "component_order": list(self.component_order),
            "components": [thaw_json(value) for value in self.components],
            "phases": [thaw_json(value) for value in self.phases],
            "placements": [thaw_json(value) for value in self.placements],
            "claims": [thaw_json(value) for value in self.claims],
        }


def explain_plan(plan: ExecutionPlan) -> PlanExplanation:
    """Project the locked plan without constructing executable components."""

    edges: dict[str, set[str]] = {value.component_id: set() for value in plan.components}
    indegree = {value.component_id: 0 for value in plan.components}
    for component in plan.components:
        for endpoint in component.input_bindings.values():
            values = endpoint if isinstance(endpoint, tuple) else (endpoint,)
            for value in values:
                source = str(value).rsplit(".", 1)[0]
                if component.component_id not in edges.get(source, set()):
                    edges.setdefault(source, set()).add(component.component_id)
                    indegree[component.component_id] += 1
    ready = sorted(key for key, value in indegree.items() if value == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in sorted(edges[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    claims = tuple(plan.validation_rules.get("claims", ()))
    return PlanExplanation(
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        execution_mode=plan.execution_mode.value,
        component_order=tuple(order),
        components=tuple(
            {
                "component_id": value.component_id,
                "plugin": f"{value.plugin_id}=={value.plugin_version}",
                "role": value.role,
                "stream_id": value.stream_id,
                "inputs": thaw_json(value.input_bindings),
                "outputs": thaw_json(value.output_bindings),
                "config_hash": _config_hash(value.config),
            }
            for value in plan.components
        ),
        phases=tuple(value.to_payload() for value in plan.phases),
        placements=tuple(value.to_payload() for value in plan.placements),
        claims=claims,
    )


def diff_plans(before: ExecutionPlan, after: ExecutionPlan) -> PlanDiff:
    """Explain scientifically and operationally material plan changes."""

    changes: list[PlanChange] = []
    if before.execution_mode != after.execution_mode:
        changes.append(
            PlanChange(
                "$.execution_mode",
                ChangeMateriality.SCIENTIFIC,
                before.execution_mode.value,
                after.execution_mode.value,
            )
        )
    for key in sorted(set(before.spec_hashes) | set(after.spec_hashes)):
        old = before.spec_hashes.get(key)
        new = after.spec_hashes.get(key)
        if old != new:
            materiality = (
                ChangeMateriality.OPERATIONAL
                if key == "deployment"
                else ChangeMateriality.SCIENTIFIC
            )
            changes.append(PlanChange(f"$.spec_hashes.{key}", materiality, old, new))
    _diff_identity_records(
        "components",
        {value.component_id: value.to_payload() for value in before.components},
        {value.component_id: value.to_payload() for value in after.components},
        ChangeMateriality.SCIENTIFIC,
        changes,
    )
    _diff_identity_records(
        "plugins",
        {f"{value.plugin_id}@{value.version}": value.to_payload() for value in before.plugins},
        {f"{value.plugin_id}@{value.version}": value.to_payload() for value in after.plugins},
        ChangeMateriality.SCIENTIFIC,
        changes,
    )
    _diff_identity_records(
        "phases",
        {value.phase_id: value.to_payload() for value in before.phases},
        {value.phase_id: value.to_payload() for value in after.phases},
        ChangeMateriality.SCIENTIFIC,
        changes,
    )
    _diff_identity_records(
        "placements",
        {value.component_id: value.to_payload() for value in before.placements},
        {value.component_id: value.to_payload() for value in after.placements},
        ChangeMateriality.OPERATIONAL,
        changes,
    )
    for field, materiality in (
        ("clock_policy", ChangeMateriality.OPERATIONAL),
        ("recording_policy", ChangeMateriality.OPERATIONAL),
        ("validation_rules", ChangeMateriality.SCIENTIFIC),
        ("initial_phase", ChangeMateriality.SCIENTIFIC),
    ):
        old = getattr(before, field)
        new = getattr(after, field)
        if old != new:
            changes.append(PlanChange(f"$.{field}", materiality, old, new))
    return PlanDiff(
        before_plan_hash=before.plan_hash,
        after_plan_hash=after.plan_hash,
        changes=tuple(sorted(changes, key=lambda value: value.path)),
    )


def _diff_identity_records(
    field: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    materiality: ChangeMateriality,
    changes: list[PlanChange],
) -> None:
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            changes.append(PlanChange(f"$.{field}.{key}", materiality, old, new))


def _config_hash(value: Any) -> str:
    from eegle.compiler.lock import canonical_hash

    return canonical_hash(value)
