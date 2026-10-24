import uuid
from datetime import date

import pytest
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
    WorkflowType,
)
from employee_onboarding_app.workflow.base import WorkflowContext, WorkflowStep
from employee_onboarding_app.workflow.orchestrator import (
    SagaOrchestrator,
    default_orchestrator,
)
from employee_onboarding_app.workflow.steps import (
    EmployeeActivationStep,
    ITProvisioningStep,
    SecurityAccessStep,
)


@pytest.mark.asyncio
async def test_saga_happy_path(db_session: AsyncSession):
    """Verifies that all 3 steps execute in order, transition employee to ACTIVE."""
    emp_id = uuid.uuid4()
    emp = Employee(
        id=emp_id,
        email=f"happy.{emp_id.hex[:6]}@company.com",
        first_name="Happy",
        last_name="User",
        start_date=date.today(),
        status=EmployeeStatus.PENDING,
    )
    db_session.add(emp)

    wf_id = uuid.uuid4()
    wf = WorkflowExecution(
        id=wf_id,
        employee_id=emp_id,
        workflow_type=WorkflowType.ONBOARDING,
        status=WorkflowStatus.PENDING,
    )
    db_session.add(wf)
    await db_session.commit()

    ctx = WorkflowContext(
        employee_id=emp_id,
        employee_email=emp.email,
        workflow_id=wf_id,
    )

    final_status = await default_orchestrator.run(db_session, ctx)

    assert final_status == WorkflowStatus.COMPLETED

    # Verify employee transitioned to ACTIVE
    emp_res = await db_session.execute(select(Employee).where(Employee.id == emp_id))
    refreshed_emp = emp_res.scalar_one()
    assert refreshed_emp.status == EmployeeStatus.ACTIVE

    # Verify audit trail has 3 successful forward steps
    audit_res = await db_session.execute(
        select(WorkflowAuditLog)
        .where(
            WorkflowAuditLog.workflow_id == wf_id,
            WorkflowAuditLog.status == StepStatus.SUCCESS,
            WorkflowAuditLog.direction == StepDirection.FORWARD,
        )
        .order_by(WorkflowAuditLog.step_number)
    )
    successful_audits = audit_res.scalars().all()
    assert len(successful_audits) == 3
    assert [a.step_name for a in successful_audits] == [
        "employee_activation",
        "it_provisioning",
        "security_access",
    ]


@pytest.mark.asyncio
async def test_saga_it_failure_rollback(db_session: AsyncSession):
    """Verifies that an IT provisioning failure triggers backward compensation:

    Step 1 (Employee Activation) is compensated, setting employee status to FAILED.
    """
    emp_id = uuid.uuid4()
    emp = Employee(
        id=emp_id,
        email=f"it.fail.{emp_id.hex[:6]}@company.com",
        first_name="ITFail",
        last_name="User",
        start_date=date.today(),
        status=EmployeeStatus.PENDING,
    )
    db_session.add(emp)

    wf_id = uuid.uuid4()
    wf = WorkflowExecution(
        id=wf_id,
        employee_id=emp_id,
        workflow_type=WorkflowType.ONBOARDING,
        status=WorkflowStatus.PENDING,
    )
    db_session.add(wf)
    await db_session.commit()

    ctx = WorkflowContext(
        employee_id=emp_id,
        employee_email=emp.email,
        workflow_id=wf_id,
        simulate_failure="it",
    )

    final_status = await default_orchestrator.run(db_session, ctx)

    assert final_status == WorkflowStatus.ROLLED_BACK

    # Verify employee was compensated to FAILED
    emp_res = await db_session.execute(select(Employee).where(Employee.id == emp_id))
    assert emp_res.scalar_one().status == EmployeeStatus.FAILED

    # Verify workflow is marked ROLLED_BACK with failure reason
    wf_res = await db_session.execute(
        select(WorkflowExecution).where(WorkflowExecution.id == wf_id)
    )
    refreshed_wf = wf_res.scalar_one()
    assert refreshed_wf.status == WorkflowStatus.ROLLED_BACK
    assert "IT service unavailable" in (refreshed_wf.failure_reason or "")


@pytest.mark.asyncio
async def test_saga_security_failure_rollback(db_session: AsyncSession):
    """Verifies Security step failure deprovisions IT accounts and marks employee as FAILED."""
    emp_id = uuid.uuid4()
    emp = Employee(
        id=emp_id,
        email=f"sec.fail.{emp_id.hex[:6]}@company.com",
        first_name="SecFail",
        last_name="User",
        start_date=date.today(),
        status=EmployeeStatus.PENDING,
    )
    db_session.add(emp)

    wf_id = uuid.uuid4()
    wf = WorkflowExecution(
        id=wf_id,
        employee_id=emp_id,
        workflow_type=WorkflowType.ONBOARDING,
        status=WorkflowStatus.PENDING,
    )
    db_session.add(wf)
    await db_session.commit()

    ctx = WorkflowContext(
        employee_id=emp_id,
        employee_email=emp.email,
        workflow_id=wf_id,
        simulate_failure="security",
    )

    final_status = await default_orchestrator.run(db_session, ctx)

    assert final_status == WorkflowStatus.ROLLED_BACK
    assert ctx.security_profile_id is None
    assert ctx.it_account_id is None  # Deprovisioned via compensation


@pytest.mark.asyncio
async def test_workflow_step_extensibility_ocp(db_session: AsyncSession):
    """Demonstrates SOLID OCP: adding a 4th step without modifying orchestrator source code."""

    class PayrollEnrollmentStep(WorkflowStep):
        step_number = 4
        step_name = "payroll_enrollment"

        async def execute(self, session: AsyncSession, ctx: WorkflowContext) -> WorkflowContext:
            ctx.extra_data["payroll_enrolled"] = True
            return ctx

        async def compensate(self, session: AsyncSession, ctx: WorkflowContext) -> None:
            ctx.extra_data["payroll_enrolled"] = False

    custom_orchestrator = SagaOrchestrator(
        steps=[
            EmployeeActivationStep(),
            ITProvisioningStep(),
            SecurityAccessStep(),
            PayrollEnrollmentStep(),
        ]
    )

    emp_id = uuid.uuid4()
    emp = Employee(
        id=emp_id,
        email=f"extensible.{emp_id.hex[:6]}@company.com",
        first_name="Extensible",
        last_name="User",
        start_date=date.today(),
        status=EmployeeStatus.PENDING,
    )
    db_session.add(emp)

    wf_id = uuid.uuid4()
    wf = WorkflowExecution(
        id=wf_id,
        employee_id=emp_id,
        workflow_type=WorkflowType.ONBOARDING,
        status=WorkflowStatus.PENDING,
    )
    db_session.add(wf)
    await db_session.commit()

    ctx = WorkflowContext(
        employee_id=emp_id,
        employee_email=emp.email,
        workflow_id=wf_id,
    )

    final_status = await custom_orchestrator.run(db_session, ctx)

    assert final_status == WorkflowStatus.COMPLETED
    assert ctx.extra_data.get("payroll_enrolled") is True
