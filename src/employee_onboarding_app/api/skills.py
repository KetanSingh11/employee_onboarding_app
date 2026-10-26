import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from employee_onboarding_app.api.deps import (
    get_db,
    require_hr,
    require_viewer,
)
from employee_onboarding_app.models import (
    Employee,
    EmployeeSkillMap,
    Skill,
)
from employee_onboarding_app.schemas import (
    EmployeeSkillAssignment,
    EmployeeSkillRead,
    EmployeeSkillUpdate,
    SkillRead,
)

router = APIRouter(tags=["Skills"])


# ─────────────────────────────────────────────────────────────
# 1. Skill Catalog: List All Skills
# ─────────────────────────────────────────────────────────────
@router.get(
    "/skills",
    response_model=list[SkillRead],
    summary="List all skills registered in the master catalog",
)
async def list_skills(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_viewer),
) -> list[SkillRead]:
    stmt = select(Skill).order_by(Skill.name.asc()).offset(offset).limit(limit)
    result = await session.execute(stmt)
    skills = result.scalars().all()
    return [SkillRead.model_validate(s) for s in skills]


# ─────────────────────────────────────────────────────────────
# 2. Assign Skill to Employee
# ─────────────────────────────────────────────────────────────
@router.post(
    "/employees/{employee_id}/skills",
    response_model=EmployeeSkillRead,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a skill and proficiency level to an employee",
)
async def assign_skill_to_employee(
    employee_id: uuid.UUID,
    payload: EmployeeSkillAssignment,
    response: Response,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_hr),
) -> EmployeeSkillRead:
    # 1. Verify employee exists with row-level lock
    emp_stmt = select(Employee).where(Employee.id == employee_id).with_for_update()
    emp_res = await session.execute(emp_stmt)
    employee = emp_res.scalar_one_or_none()
    if not employee:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{employee_id}' not found",
        )

    # 2. Get or create skill in master catalog (race-condition safe with savepoint)
    skill_stmt = select(Skill).where(Skill.name == payload.skill_name)
    skill_res = await session.execute(skill_stmt)
    skill = skill_res.scalar_one_or_none()
    if not skill:
        try:
            async with session.begin_nested():
                skill = Skill(name=payload.skill_name)
                session.add(skill)
                await session.flush()
        except IntegrityError:
            skill_res = await session.execute(skill_stmt)
            skill = skill_res.scalar_one()

    # 3. Check if mapping already exists with row-level lock
    map_stmt = (
        select(EmployeeSkillMap)
        .where(
            EmployeeSkillMap.employee_id == employee_id,
            EmployeeSkillMap.skill_id == skill.id,
        )
        .with_for_update()
    )
    map_res = await session.execute(map_stmt)
    mapping = map_res.scalar_one_or_none()

    if mapping:
        mapping.proficiency_level = payload.proficiency_level
    else:
        try:
            async with session.begin_nested():
                mapping = EmployeeSkillMap(
                    employee_id=employee_id,
                    skill_id=skill.id,
                    proficiency_level=payload.proficiency_level,
                )
                session.add(mapping)
                await session.flush()
        except IntegrityError:
            # Concurrent insert detected; serialize update
            map_res = await session.execute(map_stmt)
            mapping = map_res.scalar_one()
            mapping.proficiency_level = payload.proficiency_level

    await session.commit()
    await session.refresh(mapping)

    response.headers["ETag"] = f'"{mapping.updated_at.isoformat()}"'

    return EmployeeSkillRead(
        skill_id=skill.id,
        skill_name=skill.name,
        proficiency_level=mapping.proficiency_level,
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )


# ─────────────────────────────────────────────────────────────
# 3. Get Specific Employee Skill Assignment (with ETag)
# ─────────────────────────────────────────────────────────────
@router.get(
    "/employees/{employee_id}/skills/{skill_id}",
    response_model=EmployeeSkillRead,
    summary="Get details and ETag of a specific employee skill assignment",
)
async def get_employee_skill(
    employee_id: uuid.UUID,
    skill_id: uuid.UUID,
    response: Response,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_viewer),
) -> EmployeeSkillRead:
    map_stmt = select(EmployeeSkillMap).where(
        EmployeeSkillMap.employee_id == employee_id,
        EmployeeSkillMap.skill_id == skill_id,
    )
    map_res = await session.execute(map_stmt)
    mapping = map_res.scalar_one_or_none()

    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{skill_id}' is not assigned to employee '{employee_id}'",
        )

    skill_stmt = select(Skill).where(Skill.id == skill_id)
    skill_res = await session.execute(skill_stmt)
    skill = skill_res.scalar_one()

    response.headers["ETag"] = f'"{mapping.updated_at.isoformat()}"'

    return EmployeeSkillRead(
        skill_id=skill.id,
        skill_name=skill.name,
        proficiency_level=mapping.proficiency_level,
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )


# ─────────────────────────────────────────────────────────────
# 4. Update Existing Skill Proficiency Level (Two-Level Concurrency Check)
# ─────────────────────────────────────────────────────────────
@router.put(
    "/employees/{employee_id}/skills/{skill_id}",
    response_model=EmployeeSkillRead,
    summary="Update skill proficiency level with optimistic (ETag) and pessimistic row locking",
)
async def update_skill_proficiency(
    employee_id: uuid.UUID,
    skill_id: uuid.UUID,
    payload: EmployeeSkillUpdate,
    response: Response,
    if_match: Annotated[
        str | None,
        Header(
            alias="If-Match",
            description=(
                "ETag from prior GET response for optimistic concurrency control "
                "(e.g. '\"2026-10-26T14:00:00\"')"
            ),
        ),
    ] = None,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_hr),
) -> EmployeeSkillRead:
    # Level 2 check: Database row-level exclusive lock (serializes concurrent writes)
    map_stmt = (
        select(EmployeeSkillMap)
        .where(
            EmployeeSkillMap.employee_id == employee_id,
            EmployeeSkillMap.skill_id == skill_id,
        )
        .with_for_update()
    )
    map_res = await session.execute(map_stmt)
    mapping = map_res.scalar_one_or_none()

    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{skill_id}' is not assigned to employee '{employee_id}'",
        )

    # Level 1 check: Optimistic Concurrency Control (ETag / If-Match verification)
    current_etag = f'"{mapping.updated_at.isoformat()}"'
    if if_match is not None:
        clean_if_match = if_match.strip().strip('"')
        clean_current_etag = mapping.updated_at.isoformat()
        if clean_if_match != clean_current_etag:
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail=(
                    "Precondition Failed: Resource was modified by another request. "
                    f"Current ETag is {current_etag}. Please refresh and retry."
                ),
            )

    mapping.proficiency_level = payload.proficiency_level
    await session.commit()
    await session.refresh(mapping)

    skill_stmt = select(Skill).where(Skill.id == skill_id)
    skill_res = await session.execute(skill_stmt)
    skill = skill_res.scalar_one()

    response.headers["ETag"] = f'"{mapping.updated_at.isoformat()}"'

    return EmployeeSkillRead(
        skill_id=skill.id,
        skill_name=skill.name,
        proficiency_level=mapping.proficiency_level,
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )
