import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from employee_onboarding_app.models import (
    Employee,
    EmployeeStatus,
    StepDirection,
    StepStatus,
    WorkflowAuditLog,
    WorkflowExecution,
    WorkflowStatus,
    validate_employee_transition,
)
from employee_onboarding_app.workflow.alert import (
    default_alert_service,
)
from employee_onboarding_app.workflow.base import (
    AlertService,
    WorkflowContext,
    WorkflowStep,
)
from employee_onboarding_app.workflow.steps import (
    EmployeeActivationStep,
    ITProvisioningStep,
    SecurityAccessStep,
)

logger = logging.getLogger("employee_onboarding_app.workflow.orchestrator")


class SagaOrchestrator:
    """In-process Saga Orchestrator executing ordered forward steps and reverse compensations.

    Guarantees that no data is left in an inconsistent or partially applied state.
    Records comprehensive audit logs for all transitions within the transactional boundary.
    """

    def __init__(
        self,
        steps: list[WorkflowStep] | None = None,
        alert_service: AlertService | None = None,
    ) -> None:
        self.steps = steps or [
            EmployeeActivationStep(),
            ITProvisioningStep(),
            SecurityAccessStep(),
        ]
        self.alert_service = alert_service or default_alert_service

    async def _log_audit(
        self,
        session: AsyncSession,
        workflow_id: uuid.UUID,
        step_number: int,
        step_name: str,
        direction: StepDirection,
        status: StepStatus,
        payload: dict[str, Any] | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Appends an immutable audit log entry within the active database session."""
        audit = WorkflowAuditLog(
            workflow_id=workflow_id,
            step_number=step_number,
            step_name=step_name,
            direction=direction,
            status=status,
            payload=payload,
            error_detail=error_detail,
        )
        session.add(audit)
        await session.flush()

    async def run(
        self,
        session: AsyncSession,
        ctx: WorkflowContext,
    ) -> WorkflowStatus:
        """Executes the full onboarding saga for the provided context."""
        stmt = select(WorkflowExecution).where(WorkflowExecution.id == ctx.workflow_id)
        result = await session.execute(stmt)
        workflow = result.scalar_one_or_none()

        if not workflow:
            raise ValueError(f"WorkflowExecution {ctx.workflow_id} does not exist")

        workflow.status = WorkflowStatus.IN_PROGRESS
        await session.flush()

        completed_steps: list[WorkflowStep] = []

        # ─────────────────────────────────────────────────────────
        # Forward Step Execution Phase
        # ─────────────────────────────────────────────────────────
        for step in self.steps:
            workflow.current_step = step.step_number

            # 1. Audit log: Step Started
            await self._log_audit(
                session=session,
                workflow_id=ctx.workflow_id,
                step_number=step.step_number,
                step_name=step.step_name,
                direction=StepDirection.FORWARD,
                status=StepStatus.STARTED,
                payload={"email": ctx.employee_email},
            )

            try:
                # 2. Execute forward step
                ctx = await step.execute(session, ctx)

                # 3. Audit log: Step Success
                await self._log_audit(
                    session=session,
                    workflow_id=ctx.workflow_id,
                    step_number=step.step_number,
                    step_name=step.step_name,
                    direction=StepDirection.FORWARD,
                    status=StepStatus.SUCCESS,
                    payload={
                        "email": ctx.employee_email,
                        "it_account_id": ctx.it_account_id,
                        "security_profile_id": ctx.security_profile_id,
                    },
                )
                completed_steps.append(step)
                await session.commit()

            except Exception as exc:
                logger.error(
                    "Saga failure in step '%s' for %s: %s",
                    step.step_name,
                    ctx.employee_email,
                    exc,
                )
                await session.rollback()

                # Re-fetch workflow instance in fresh transaction
                refetched = await session.execute(
                    select(WorkflowExecution).where(WorkflowExecution.id == ctx.workflow_id)
                )
                workflow = refetched.scalar_one()
                workflow.status = WorkflowStatus.ROLLING_BACK
                workflow.failure_reason = str(exc)

                # Log failure in audit
                await self._log_audit(
                    session=session,
                    workflow_id=ctx.workflow_id,
                    step_number=step.step_number,
                    step_name=step.step_name,
                    direction=StepDirection.FORWARD,
                    status=StepStatus.FAILED,
                    error_detail=str(exc),
                )
                await session.flush()

                # Emit Alert
                self.alert_service.error(
                    f"Workflow failed at step '{step.step_name}' for employee {ctx.employee_email}",
                    context={
                        "workflow_id": str(ctx.workflow_id),
                        "employee_id": str(ctx.employee_id),
                        "failed_step": step.step_name,
                        "error": str(exc),
                    },
                )

                # ─────────────────────────────────────────────────────
                # Backward Compensation Phase (Reverse Order)
                # ─────────────────────────────────────────────────────
                await self._compensate(session, ctx, completed_steps)

                workflow.status = WorkflowStatus.ROLLED_BACK
                await session.commit()
                return WorkflowStatus.ROLLED_BACK

        # ─────────────────────────────────────────────────────────
        # Finalization Phase: Successful Onboarding
        # ─────────────────────────────────────────────────────────
        emp_stmt = select(Employee).where(Employee.id == ctx.employee_id).with_for_update()
        emp_result = await session.execute(emp_stmt)
        employee = emp_result.scalar_one()

        validate_employee_transition(employee.status, EmployeeStatus.ACTIVE)
        employee.status = EmployeeStatus.ACTIVE
        workflow.status = WorkflowStatus.COMPLETED
        await session.commit()

        self.alert_service.info(
            f"Onboarding workflow completed successfully for {ctx.employee_email}",
            context={
                "workflow_id": str(ctx.workflow_id),
                "employee_id": str(ctx.employee_id),
                "it_account_id": ctx.it_account_id,
                "security_profile_id": ctx.security_profile_id,
            },
        )
        return WorkflowStatus.COMPLETED

    async def _compensate(
        self,
        session: AsyncSession,
        ctx: WorkflowContext,
        completed_steps: list[WorkflowStep],
    ) -> None:
        """Executes compensation in reverse order for all completed steps."""
        for step in reversed(completed_steps):
            logger.info(
                "Triggering compensation for step '%s' for %s",
                step.step_name,
                ctx.employee_email,
            )
            await self._log_audit(
                session=session,
                workflow_id=ctx.workflow_id,
                step_number=step.step_number,
                step_name=step.step_name,
                direction=StepDirection.COMPENSATE,
                status=StepStatus.STARTED,
            )

            try:
                await step.compensate(session, ctx)
                await self._log_audit(
                    session=session,
                    workflow_id=ctx.workflow_id,
                    step_number=step.step_number,
                    step_name=step.step_name,
                    direction=StepDirection.COMPENSATE,
                    status=StepStatus.SUCCESS,
                )
            except Exception as comp_exc:
                logger.critical(
                    "Compensation error on step '%s' for %s: %s",
                    step.step_name,
                    ctx.employee_email,
                    comp_exc,
                )
                await self._log_audit(
                    session=session,
                    workflow_id=ctx.workflow_id,
                    step_number=step.step_number,
                    step_name=step.step_name,
                    direction=StepDirection.COMPENSATE,
                    status=StepStatus.FAILED,
                    error_detail=str(comp_exc),
                )


# Default shared orchestrator
default_orchestrator = SagaOrchestrator()
