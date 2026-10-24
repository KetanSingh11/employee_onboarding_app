import contextlib
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from employee_onboarding_app.api.deps import (
    get_db,
    require_hr,
    require_viewer,
)
from employee_onboarding_app.database import AsyncSessionLocal
from employee_onboarding_app.models import (
    BatchRun,
    Employee,
    EmployeeStatus,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowType,
    validate_employee_transition,
)
from employee_onboarding_app.schemas import (
    BatchRunRead,
    WorkflowAuditLogRead,
    WorkflowExecutionRead,
    WorkflowRetryResponse,
)
from employee_onboarding_app.workflow.base import WorkflowContext
from employee_onboarding_app.workflow.orchestrator import default_orchestrator

router = APIRouter(tags=["Workflows & Batches"])


async def _run_retry_saga_background(
    employee_id: uuid.UUID,
    employee_email: str,
    new_workflow_id: uuid.UUID,
) -> None:
    """Executes retried saga in an isolated async session."""
    async with AsyncSessionLocal() as session:
        ctx = WorkflowContext(
            employee_id=employee_id,
            employee_email=employee_email,
            workflow_id=new_workflow_id,
        )
        with contextlib.suppress(Exception):
            await default_orchestrator.run(session, ctx)


# ─────────────────────────────────────────────────────────────
# 1. Workflow Status & Audit History
# ─────────────────────────────────────────────────────────────
@router.get(
    "/workflows/{workflow_id}",
    response_model=WorkflowExecutionRead,
    summary="Get workflow execution status and step-level audit trail",
)
async def get_workflow_status(
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_viewer),
) -> WorkflowExecutionRead:
    stmt = (
        select(WorkflowExecution)
        .where(WorkflowExecution.id == workflow_id)
        .options(selectinload(WorkflowExecution.audit_logs))
    )
    result = await session.execute(stmt)
    wf = result.scalar_one_or_none()
    if not wf:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow execution '{workflow_id}' not found",
        )

    audit_reads = [WorkflowAuditLogRead.model_validate(log) for log in wf.audit_logs]

    return WorkflowExecutionRead(
        id=wf.id,
        employee_id=wf.employee_id,
        batch_id=wf.batch_id,
        workflow_type=wf.workflow_type,
        status=wf.status,
        current_step=wf.current_step,
        failure_reason=wf.failure_reason,
        created_at=wf.created_at,
        updated_at=wf.updated_at,
        audit_logs=audit_reads,
    )


# ─────────────────────────────────────────────────────────────
# 2. Workflow Retry Endpoint (ADR-17)
# ─────────────────────────────────────────────────────────────
@router.post(
    "/workflows/{workflow_id}/retry",
    response_model=WorkflowRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry a failed or rolled-back workflow (Fire-and-forget, 202 Accepted)",
)
async def retry_failed_workflow(
    workflow_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_hr),
) -> WorkflowRetryResponse:
    # 1. Fetch failed workflow
    stmt = select(WorkflowExecution).where(WorkflowExecution.id == workflow_id)
    res = await session.execute(stmt)
    old_wf = res.scalar_one_or_none()
    if not old_wf:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow '{workflow_id}' not found",
        )

    # 2. Verify eligible status for retry
    if old_wf.status not in {WorkflowStatus.FAILED, WorkflowStatus.ROLLED_BACK}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot retry workflow with status '{old_wf.status.value}'. "
                "Must be FAILED or ROLLED_BACK."
            ),
        )

    # 3. Check for any currently active workflow for the same employee
    active_stmt = select(WorkflowExecution).where(
        WorkflowExecution.employee_id == old_wf.employee_id,
        WorkflowExecution.status.notin_(
            [
                WorkflowStatus.COMPLETED,
                WorkflowStatus.ROLLED_BACK,
                WorkflowStatus.FAILED,
            ]
        ),
    )
    active_res = await session.execute(active_stmt)
    if active_res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An active workflow execution is already in-progress for this employee.",
        )

    # 4. Fetch employee and reset status to PENDING
    emp_stmt = select(Employee).where(Employee.id == old_wf.employee_id).with_for_update()
    emp_res = await session.execute(emp_stmt)
    emp = emp_res.scalar_one_or_none()
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target employee not found for workflow retry",
        )

    validate_employee_transition(emp.status, EmployeeStatus.PENDING)
    emp.status = EmployeeStatus.PENDING

    # 5. Create new WorkflowExecution record (preserves full historical audit)
    new_wf = WorkflowExecution(
        employee_id=emp.id,
        batch_id=old_wf.batch_id,
        workflow_type=WorkflowType.ONBOARDING,
        status=WorkflowStatus.PENDING,
    )
    session.add(new_wf)
    await session.commit()
    await session.refresh(new_wf)

    # 6. Dispatch retry saga in background
    background_tasks.add_task(
        _run_retry_saga_background,
        emp.id,
        emp.email,
        new_wf.id,
    )

    return WorkflowRetryResponse(
        message=f"Workflow retry accepted. Tracking under new execution ID '{new_wf.id}'",
        new_workflow_id=new_wf.id,
        employee_id=emp.id,
        status=WorkflowStatus.PENDING,
    )


# ─────────────────────────────────────────────────────────────
# 3. Poll Batch Run Status & Row-Level Ingestion Report
# ─────────────────────────────────────────────────────────────
@router.get(
    "/batches/{batch_id}",
    response_model=BatchRunRead,
    summary="Poll CSV ingestion batch status and row-level validation report",
)
async def get_batch_run_status(
    batch_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    _role: str = Depends(require_viewer),
) -> BatchRunRead:
    stmt = select(BatchRun).where(BatchRun.id == batch_id)
    res = await session.execute(stmt)
    batch = res.scalar_one_or_none()
    if not batch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch run '{batch_id}' not found",
        )
    return BatchRunRead.model_validate(batch)
