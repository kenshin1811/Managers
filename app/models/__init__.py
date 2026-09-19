"""SQLAlchemy models. Importing this module registers every mapper."""

from app.models.audit import DecisionRecord
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.employee import AvailabilityWindow, Employee, EmployeeSkill, Skill
from app.models.enums import (
    CoverageStatus,
    DecisionAction,
    LeaveStatus,
    LeaveType,
    OfferStatus,
    OrderStatus,
    ShiftAssignmentStatus,
    TaskPriority,
    TaskStatus,
)
from app.models.leave import LeaveRequest
from app.models.order import Order
from app.models.shift import Shift, ShiftAssignment
from app.models.task import Task
from app.models.timeclock import TimeEntry

__all__ = [
    "AvailabilityWindow",
    "CoverageOffer",
    "CoverageRequest",
    "CoverageStatus",
    "DecisionAction",
    "DecisionRecord",
    "Employee",
    "EmployeeSkill",
    "LeaveRequest",
    "LeaveStatus",
    "LeaveType",
    "OfferStatus",
    "Order",
    "OrderStatus",
    "Shift",
    "ShiftAssignment",
    "ShiftAssignmentStatus",
    "Skill",
    "Task",
    "TaskPriority",
    "TaskStatus",
    "TimeEntry",
]
