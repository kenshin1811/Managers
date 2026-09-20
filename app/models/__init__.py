"""SQLAlchemy models. Importing this module registers every mapper."""

from app.models.audit import DecisionRecord
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.destination import Destination
from app.models.employee import AvailabilityWindow, Employee, EmployeeSkill, Skill
from app.models.enums import (
    Channel,
    CoverageStatus,
    DecisionAction,
    LeaveStatus,
    LeaveType,
    OfferStatus,
    OrderStatus,
    ProductKind,
    ShiftAssignmentStatus,
    Stage,
    TaskPriority,
    TaskStatus,
)
from app.models.leave import LeaveRequest
from app.models.order import Order, OrderLine
from app.models.product import Product
from app.models.shift import Shift, ShiftAssignment
from app.models.state import SystemState
from app.models.task import Task
from app.models.timeclock import TimeEntry

__all__ = [
    "AvailabilityWindow",
    "Channel",
    "CoverageOffer",
    "CoverageRequest",
    "CoverageStatus",
    "DecisionAction",
    "DecisionRecord",
    "Destination",
    "Employee",
    "EmployeeSkill",
    "LeaveRequest",
    "LeaveStatus",
    "LeaveType",
    "OfferStatus",
    "Order",
    "OrderLine",
    "OrderStatus",
    "Product",
    "ProductKind",
    "Shift",
    "ShiftAssignment",
    "ShiftAssignmentStatus",
    "Skill",
    "Stage",
    "SystemState",
    "Task",
    "TaskPriority",
    "TaskStatus",
    "TimeEntry",
]
