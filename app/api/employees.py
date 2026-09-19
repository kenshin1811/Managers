"""Employees, what they are trained in, and when they can be called in."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    AvailabilityIn,
    EmployeeDetail,
    EmployeeIn,
    EmployeeOut,
    EmployeeSkillOut,
    SkillGrant,
    SkillIn,
    SkillOut,
)
from app.db import get_session
from app.models.employee import AvailabilityWindow, Employee, EmployeeSkill, Skill

router = APIRouter(tags=["employees"])


def _detail(employee: Employee) -> EmployeeDetail:
    return EmployeeDetail(
        **EmployeeOut.model_validate(employee).model_dump(),
        skills=[
            EmployeeSkillOut(skill_code=link.skill.code, proficiency=link.proficiency)
            for link in employee.skills
        ],
        availability=[
            AvailabilityIn(
                weekday=window.weekday,
                start_minute=window.start_minute,
                end_minute=window.end_minute,
            )
            for window in employee.availability
        ],
    )


@router.post("/skills", response_model=SkillOut, status_code=201)
def create_skill(payload: SkillIn, session: Session = Depends(get_session)) -> Skill:
    existing = session.scalar(select(Skill).where(Skill.code == payload.code))
    if existing is not None:
        return existing
    skill = Skill(code=payload.code, name=payload.name)
    session.add(skill)
    session.commit()
    return skill


@router.get("/skills", response_model=list[SkillOut])
def list_skills(session: Session = Depends(get_session)) -> list[Skill]:
    return list(session.scalars(select(Skill).order_by(Skill.code)))


@router.post("/employees", response_model=EmployeeOut, status_code=201)
def create_employee(payload: EmployeeIn, session: Session = Depends(get_session)) -> Employee:
    employee = Employee(**payload.model_dump())
    session.add(employee)
    session.commit()
    return employee


@router.get("/employees", response_model=list[EmployeeOut])
def list_employees(
    active_only: bool = True, session: Session = Depends(get_session)
) -> list[Employee]:
    query = select(Employee).order_by(Employee.id)
    if active_only:
        query = query.where(Employee.active.is_(True))
    return list(session.scalars(query))


@router.get("/employees/{employee_id}", response_model=EmployeeDetail)
def get_employee(employee_id: int, session: Session = Depends(get_session)) -> EmployeeDetail:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee")
    return _detail(employee)


@router.post("/employees/{employee_id}/skills", response_model=EmployeeDetail)
def grant_skill(
    employee_id: int, payload: SkillGrant, session: Session = Depends(get_session)
) -> EmployeeDetail:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee")
    skill = session.scalar(select(Skill).where(Skill.code == payload.skill_code))
    if skill is None:
        raise HTTPException(404, f"No such skill: {payload.skill_code}")

    for link in employee.skills:
        if link.skill_id == skill.id:
            link.proficiency = payload.proficiency
            break
    else:
        session.add(
            EmployeeSkill(
                employee_id=employee.id, skill_id=skill.id, proficiency=payload.proficiency
            )
        )
    session.commit()
    session.refresh(employee)
    return _detail(employee)


@router.post("/employees/{employee_id}/availability", response_model=EmployeeDetail)
def add_availability(
    employee_id: int, payload: AvailabilityIn, session: Session = Depends(get_session)
) -> EmployeeDetail:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee")
    session.add(AvailabilityWindow(employee_id=employee.id, **payload.model_dump()))
    session.commit()
    session.refresh(employee)
    return _detail(employee)
