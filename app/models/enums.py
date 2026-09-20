"""Shared enumerations.

Stored as strings so the database stays readable and an audit row still makes
sense years later, after the code has moved on.
"""

from __future__ import annotations

from enum import StrEnum


class ProductKind(StrEnum):
    FRESH = "fresh"
    FROZEN = "frozen"


class Channel(StrEnum):
    RETAIL = "retail"
    WHOLESALE = "wholesale"
    INTERNAL = "internal"
    """The wholesale packer team: a handover, not a delivery."""


class Stage(StrEnum):
    """The pipeline a donut goes through, in order.

    The order is the point: you cannot pack what is still in the freezer, and
    you cannot load a van with a consignment nobody has sorted. The planner
    reads these as hard dependencies rather than as labels.
    """

    RETRIEVE = "retrieve"
    PACK = "pack"
    SORT = "sort"
    DISPATCH = "dispatch"

    @property
    def order(self) -> int:
        return {"retrieve": 0, "pack": 1, "sort": 2, "dispatch": 3}[self.value]

    @property
    def label(self) -> str:
        return {
            "retrieve": "Pull from freezer",
            "pack": "Pack and label",
            "sort": "Sort to orders",
            "dispatch": "Load the van",
        }[self.value]


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    ROUTINE = "routine"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"routine": 1, "high": 2, "critical": 3}[self.value]


class LeaveType(StrEnum):
    SICK = "sick"
    EMERGENCY = "emergency"
    PERSONAL = "personal"
    VACATION = "vacation"


class LeaveStatus(StrEnum):
    PENDING_COVERAGE = "pending_coverage"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"


class CoverageStatus(StrEnum):
    OPEN = "open"
    FILLED = "filled"
    ESCALATED = "escalated"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OfferStatus(StrEnum):
    SENT = "sent"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class ShiftAssignmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    RELEASED = "released"
    COMPLETED = "completed"


class OrderStatus(StrEnum):
    OPEN = "open"
    READY = "ready"
    PICKED_UP = "picked_up"
    CANCELLED = "cancelled"


class DecisionAction(StrEnum):
    NO_ACTION_NEEDED = "no_action_needed"
    AUTO_REASSIGNED = "auto_reassigned"
    COVERAGE_REQUESTED = "coverage_requested"
    COVERAGE_FILLED = "coverage_filled"
    ESCALATED = "escalated"
    LEAVE_APPROVED = "leave_approved"
    LEAVE_PENDING_COVERAGE = "leave_pending_coverage"
    OVERRIDDEN = "overridden"
    BLOCKED_BY_POLICY = "blocked_by_policy"
