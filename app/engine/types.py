"""Value objects passed between the engine's stages.

These are plain dataclasses rather than ORM rows so that planning can happen
without a transaction open, and so a plan can be inspected, logged or shown to
a manager before anything about it becomes true.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.models.enums import DecisionAction


@dataclass(frozen=True)
class RuleResult:
    """One guardrail, evaluated against one candidate.

    Both outcomes are kept.  A record of *why* someone was ruled out is the
    part a manager actually needs when they ask why the system picked Sam.
    """

    rule: str
    passed: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateScore:
    employee_id: int
    employee_name: str
    on_shift: bool
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    rules: list[RuleResult] = field(default_factory=list)
    displaces_task_id: int | None = None
    """Set when taking this work means dropping lower-priority work they hold."""

    @property
    def eligible(self) -> bool:
        return all(rule.passed for rule in self.rules)

    @property
    def blocking_rules(self) -> list[RuleResult]:
        return [rule for rule in self.rules if not rule.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "employee_name": self.employee_name,
            "on_shift": self.on_shift,
            "eligible": self.eligible,
            "score": round(self.score, 2),
            "components": {k: round(v, 2) for k, v in self.components.items()},
            "displaces_task_id": self.displaces_task_id,
            "rules": [rule.as_dict() for rule in self.rules],
        }


@dataclass
class ImpactedTask:
    """A job that the leave collides with."""

    task_id: int
    title: str
    starts_at: datetime
    due_at: datetime
    deadline: datetime
    priority: str
    critical: bool
    order_code: str | None = None
    pickup_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("starts_at", "due_at", "deadline", "pickup_at"):
            value = data[key]
            data[key] = value.isoformat() if value else None
        return data


@dataclass
class TaskPlan:
    """What the engine intends to do about one impacted task."""

    impact: ImpactedTask
    action: DecisionAction
    reason: str
    chosen: CandidateScore | None = None
    ask_list: list[CandidateScore] = field(default_factory=list)
    candidates: list[CandidateScore] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        """True when this task no longer needs a human to intervene."""
        return self.action in (DecisionAction.AUTO_REASSIGNED, DecisionAction.NO_ACTION_NEEDED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.impact.as_dict(),
            "action": str(self.action),
            "reason": self.reason,
            "chosen": self.chosen.as_dict() if self.chosen else None,
            "asked": [c.as_dict() for c in self.ask_list],
            "candidates": [c.as_dict() for c in self.candidates],
        }


@dataclass
class ReassignmentPlan:
    """The full intent for one leave request. Nothing here has happened yet."""

    leave_request_id: int
    employee_id: int
    employee_name: str
    leave_starts_at: datetime
    leave_ends_at: datetime
    generated_at: datetime
    task_plans: list[TaskPlan] = field(default_factory=list)
    released_shift_assignment_ids: list[int] = field(default_factory=list)

    @property
    def fully_resolved(self) -> bool:
        return all(plan.resolved for plan in self.task_plans)

    @property
    def has_critical_unresolved(self) -> bool:
        return any(plan.impact.critical and not plan.resolved for plan in self.task_plans)

    def as_dict(self) -> dict[str, Any]:
        return {
            "leave_request_id": self.leave_request_id,
            "employee_id": self.employee_id,
            "employee_name": self.employee_name,
            "leave_starts_at": self.leave_starts_at.isoformat(),
            "leave_ends_at": self.leave_ends_at.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "fully_resolved": self.fully_resolved,
            "released_shift_assignment_ids": list(self.released_shift_assignment_ids),
            "task_plans": [plan.as_dict() for plan in self.task_plans],
        }
