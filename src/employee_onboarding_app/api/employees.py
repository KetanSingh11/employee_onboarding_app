import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from employee_onboarding_app.api.deps import (
    get_db,
    require_admin,
    require_hr,
    require_viewer,
)
from employee_onboarding_app.config import settings
from employee_onboarding_app.database import AsyncSessionLocal
from employee_onboarding_app.models import (
    BatchRun,
    BatchStatus,
    Employee,
    EmployeeSkillMap,
    EmployeeStatus,
    ProficiencyLevel,
    Skill,
)
from employee_onboarding_app.schemas import (
    BatchIngestAcceptedResponse,
    EmployeeRead,
    EmployeeSkillRead,
)
from employee_onboarding_app.services.ingestion import process_batch_ingestion

router = APIRouter(prefix="/employees", tags=["Employees"])

PROFICIENCY_RANK: dict[ProficiencyLevel, int] = {
    ProficiencyLevel.BEGINNER: 1,
    ProficiencyLevel.INTERMEDIATE: 2,
    ProficiencyLevel.ADVANCED: 3,
    ProficiencyLevel.EXPERT: 4,
}


async def _run_batch_background(
    batch_id: uuid.UUID,
    csv_text: str,
    simulate_failure: str | None,
) -> None:
    """Safely runs batch CSV processing in an isolated async session."""
    async with AsyncSessionLocal() as session:
        await process_batch_ingestion(
            session=session,
            batch_id=batch_id,
            csv_content=csv_text,
            simulate_failure=simulate_failure,
        )


def _to_employee_read(emp: Employee) -> EmployeeRead:
    skill_reads: list[EmployeeSkillRead] = []
    for m in emp.skills:
        skill_name = m.skill.name if m.skill else "unknown"
        skill_reads.append(
            EmployeeSkillRead(
                skill_id=m.skill_id,
                skill_name=skill_name,
                proficiency_level=m.proficiency_level,
                created_at=m.created_at,
                updated_at=m.updated_at,
            )
        )
    return EmployeeRead(
        id=emp.id,
        email=emp.email,
        first_name=emp.first_name,
        last_name=emp.last_name,
        department=emp.department,
        job_title=emp.job_title,
        start_date=emp.start_date,
        status=emp.status,
        skills=skill_reads,
        created_at=emp.created_at,
        updated_at=emp.updated_at,
    )


# ─────────────────────────────────────────────────────────────
# 1. Ingestion: CSV Upload
# ─────────────────────────────────────────────────────────────
@router.post(
    "/import",
    response_model=BatchIngestAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest CSV of employees and trigger multi-step onboarding",
)
async def import_employees_csv(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    simulate_failure: Annotated[str | None, Header(alias="X-Simulate-Failure")] = None,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Optional client-generated UUID for idempotency protection on retries",
        ),
    ] = None,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_hr),
) -> BatchIngestAcceptedResponse:
    if not file.filename or not file.filename.endswith((".csv", ".txt")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must be a valid .csv or .txt file",
        )

    # Validate Content-Type if present
    allowed_content_types = {
        "text/csv",
        "text/plain",
        "application/vnd.ms-excel",
        "application/csv",
        "text/x-csv",
        "application/octet-stream",
    }
    if file.content_type and file.content_type.lower() not in allowed_content_types:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{file.content_type}'. Must be CSV or plain text.",
        )

    # Non-blocking chunked streaming with configurable size cap (prevents OOM on large files)
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    chunks: list[bytes] = []
    total_bytes = 0
    chunk_size = 64 * 1024  # 64 KB per chunk

    while chunk := await file.read(chunk_size):
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds maximum allowed limit of {settings.MAX_UPLOAD_SIZE_MB}MB",
            )
        chunks.append(chunk)

    content_bytes = b"".join(chunks)
    try:
        csv_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV file must be UTF-8 encoded",
        ) from err

    if not csv_text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded CSV file is completely empty",
        )

    # Create tracked BatchRun record
    batch = BatchRun(
        filename=file.filename,
        status=BatchStatus.PROCESSING,
    )
    session.add(batch)
    await session.commit()
    await session.refresh(batch)

    # Dispatch non-blocking background task
    background_tasks.add_task(
        _run_batch_background,
        batch.id,
        csv_text,
        simulate_failure,
    )

    return BatchIngestAcceptedResponse(
        batch_id=batch.id,
        filename=file.filename,
        status=BatchStatus.PROCESSING,
        message=(
            "CSV ingestion accepted for background processing. "
            "Poll /api/v1/batches/{batch_id} for status."
        ),
    )


# ─────────────────────────────────────────────────────────────
# 2. Query & Search: Filter Employees by Skill, Level, Dept
# ─────────────────────────────────────────────────────────────
@router.get(
    "",
    response_model=list[EmployeeRead],
    summary="List and filter employees by skill, minimum proficiency, department, and status",
)
async def list_employees(
    skill: Annotated[str | None, Query(description="Filter by specific skill name")] = None,
    min_proficiency: Annotated[
        ProficiencyLevel | None,
        Query(description="Filter by minimum required proficiency level"),
    ] = None,
    department: Annotated[str | None, Query(description="Filter by department")] = None,
    employee_status: Annotated[
        EmployeeStatus | None,
        Query(alias="status", description="Filter by employee lifecycle status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_viewer),
) -> list[EmployeeRead]:
    stmt = (
        select(Employee)
        .options(selectinload(Employee.skills).selectinload(EmployeeSkillMap.skill))
        .order_by(Employee.created_at.desc())
    )

    if department:
        stmt = stmt.where(func.lower(Employee.department) == department.strip().lower())

    if employee_status:
        stmt = stmt.where(Employee.status == employee_status)

    # Pushdown skill & minimum proficiency filter into PostgreSQL query
    if skill or min_proficiency:
        clean_skill = skill.strip().lower() if skill else None
        min_rank = PROFICIENCY_RANK[min_proficiency] if min_proficiency else 1
        qualifying_levels = [lvl for lvl, rank in PROFICIENCY_RANK.items() if rank >= min_rank]

        skill_subquery = (
            select(EmployeeSkillMap.employee_id)
            .join(Skill, Skill.id == EmployeeSkillMap.skill_id)
            .where(EmployeeSkillMap.proficiency_level.in_(qualifying_levels))
        )
        if clean_skill:
            skill_subquery = skill_subquery.where(Skill.name == clean_skill)

        stmt = stmt.where(Employee.id.in_(skill_subquery))

    # Apply pagination directly at the database engine level (safe for 1M+ rows)
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    employees = result.scalars().all()

    return [_to_employee_read(e) for e in employees]


# ─────────────────────────────────────────────────────────────
# 3. Employee Detail
# ─────────────────────────────────────────────────────────────
@router.get(
    "/{employee_id}",
    response_model=EmployeeRead,
    summary="Get employee details and assigned skills by ID",
)
async def get_employee_by_id(
    employee_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_viewer),
) -> EmployeeRead:
    stmt = (
        select(Employee)
        .where(Employee.id == employee_id)
        .options(selectinload(Employee.skills).selectinload(EmployeeSkillMap.skill))
    )
    result = await session.execute(stmt)
    emp = result.scalar_one_or_none()
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee with ID '{employee_id}' not found",
        )
    return _to_employee_read(emp)


# ─────────────────────────────────────────────────────────────
# 4. Offboarding Skeletal Placeholder (ADR-10)
# ─────────────────────────────────────────────────────────────
@router.post(
    "/{employee_id}/offboard",
    summary="[Placeholder] Trigger employee deboarding/offboarding workflow",
)
async def trigger_offboarding(
    employee_id: uuid.UUID,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description="Optional client-generated UUID for idempotency protection on retries",
        ),
    ] = None,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_admin),
) -> dict[str, str]:
    stmt = select(Employee).where(Employee.id == employee_id)
    result = await session.execute(stmt)
    emp = result.scalar_one_or_none()
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee with ID '{employee_id}' not found",
        )

    # Skeletal placeholder indication per ADR-10
    return {
        "message": (
            f"Offboarding workflow scheduled for employee '{emp.email}' "
            "(Skeletal placeholder per ADR-10)"
        ),
        "employee_id": str(employee_id),
        "status": "OFFBOARDING_SCHEDULED",
    }
