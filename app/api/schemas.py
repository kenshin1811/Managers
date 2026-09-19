"""Request and response shapes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import LeaveType, TaskPriority


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- employees --------------------------------------------------------------


class SkillIn(BaseModel):
    code: str
    name: str


class SkillOut(ORMModel):
    id: int
    code: str
    name: str


class SkillGrant(BaseModel):
    skill_code: str
    proficiency: int = Field(default=3, ge=1, le=5)


class AvailabilityIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday")
    start_minute: int = Field(default=0, ge=0, le=1440)
    end_minute: int = Field(default=1440, ge=0, le=1440)


class EmployeeIn(BaseModel):
    full_name: str
    email: str | None = None
    slack_user_id: str | None = None
    role: str = "staff"
    is_manager: bool = False


class EmployeeSkillOut(BaseModel):
    skill_code: str
    proficiency: int


class EmployeeOut(ORMModel):
    id: int
    full_name: str
    email: str | None
    slack_user_id: str | None
    role: str
    is_manager: bool
    active: bool


class EmployeeDetail(EmployeeOut):
    skills: list[EmployeeSkillOut] = []
    availability: list[AvailabilityIn] = []


# --- orders and tasks -------------------------------------------------------


class OrderIn(BaseModel):
    code: str
    customer_name: str = ""
    placed_at: datetime | None = None
    pickup_at: datetime
    driver_name: str | None = None
    driver_service: str | None = None


class OrderOut(ORMModel):
    id: int
    code: str
    customer_name: str
    placed_at: datetime
    pickup_at: datetime
    driver_name: str | None
    driver_service: str | None
    status: str


class TaskIn(BaseModel):
    title: str
    station: str = ""
    order_id: int | None = None
    assignee_id: int | None = None
    required_skill_code: str | None = None
    min_proficiency: int = Field(default=1, ge=1, le=5)
    starts_at: datetime
    due_at: datetime
    estimated_minutes: int = Field(default=15, ge=1)
    priority: TaskPriority = TaskPriority.ROUTINE


class TaskOut(ORMModel):
    id: int
    title: str
    station: str
    order_id: int | None
    assignee_id: int | None
    required_skill_id: int | None
    min_proficiency: int
    starts_at: datetime
    due_at: datetime
    estimated_minutes: int
    status: str
    priority: str


class TaskReassign(BaseModel):
    employee_id: int | None
    note: str = ""
    manager_id: int | None = None


# --- shifts -----------------------------------------------------------------


class ShiftIn(BaseModel):
    name: str = "shift"
    location: str = "main"
    starts_at: datetime
    ends_at: datetime
    employee_ids: list[int] = []


class ShiftOut(ORMModel):
    id: int
    name: str
    location: str
    starts_at: datetime
    ends_at: datetime


# --- time tracking ----------------------------------------------------------


class ClockIn(BaseModel):
    employee_id: int
    shift_id: int | None = None
    at: datetime | None = None
    source: str = "api"


class ClockOut(BaseModel):
    employee_id: int
    at: datetime | None = None
    break_minutes: int = Field(default=0, ge=0)


class TimeEntryOut(ORMModel):
    id: int
    employee_id: int
    shift_id: int | None
    clock_in: datetime
    clock_out: datetime | None
    break_minutes: int
    source: str


class HoursSummary(BaseModel):
    employee_id: int
    full_name: str
    hours_today: float
    hours_week: float
    daily_limit: float
    weekly_limit: float
    on_the_clock: bool


# --- leave ------------------------------------------------------------------


class LeaveIn(BaseModel):
    employee_id: int
    starts_at: datetime
    ends_at: datetime
    leave_type: LeaveType = LeaveType.PERSONAL
    reason: str = ""


class LeaveOut(ORMModel):
    id: int
    employee_id: int
    leave_type: str
    reason: str
    starts_at: datetime
    ends_at: datetime
    status: str
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    notes: str


class LeaveDecision(BaseModel):
    manager_id: int
    approve: bool
    note: str = ""


class LeaveResult(BaseModel):
    """What the leave request triggered."""

    leave: LeaveOut
    plan: dict[str, Any]
    decisions: list[int]


# --- coverage ---------------------------------------------------------------


class CoverageOfferOut(ORMModel):
    id: int
    employee_id: int
    status: str
    rank: int
    score: float
    sent_at: datetime
    responded_at: datetime | None


class CoverageOut(ORMModel):
    id: int
    task_id: int
    leave_request_id: int | None
    vacating_employee_id: int | None
    reason: str
    status: str
    wave: int
    created_at: datetime
    expires_at: datetime
    filled_by_id: int | None
    filled_at: datetime | None
    escalated_at: datetime | None
    offers: list[CoverageOfferOut] = []


class CoverageReply(BaseModel):
    employee_id: int
    accept: bool


class CoverageReplyResult(BaseModel):
    ok: bool
    status: str
    message: str
    task_id: int | None = None
    blockers: list[str] = []


# --- audit ------------------------------------------------------------------


class DecisionOut(ORMModel):
    id: int
    created_at: datetime
    actor: str
    trigger: str
    action: str
    subject_type: str
    subject_id: int | None
    summary: str
    details: dict[str, Any]
    reverted_by_id: int | None


class OverrideIn(BaseModel):
    manager_id: int
    employee_id: int | None = None
    """Who the task should go to instead. Defaults to whoever held it before."""

    note: str = ""
