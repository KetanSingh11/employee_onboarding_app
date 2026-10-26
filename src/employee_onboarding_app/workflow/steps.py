import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from employee_onboarding_app.models import (
    Employee,
    EmployeeStatus,
    validate_employee_transition,
)
from employee_onboarding_app.workflow.base import (
    ExternalServiceError,
    WorkflowContext,
    WorkflowStep,
    WorkflowStepError,
)

logger = logging.getLogger("employee_onboarding_app.workflow.steps")


# ─────────────────────────────────────────────────────────────
# Step 1: Employee Registration & Profile Activation
# ─────────────────────────────────────────────────────────────
class EmployeeActivationStep(WorkflowStep):
    """Activates the employee record in the primary database.

    Acquires an exclusive row-level lock (FOR UPDATE) to prevent concurrent activation races,
    verifies allowed state transitions, and moves employee status to ONBOARDING.
    """

    step_number = 1
    step_name = "employee_activation"

    async def execute(self, session: AsyncSession, ctx: WorkflowContext) -> WorkflowContext:
        stmt = select(Employee).where(Employee.id == ctx.employee_id).with_for_update()
        result = await session.execute(stmt)
        employee = result.scalar_one_or_none()

        if not employee:
            raise WorkflowStepError(
                self.step_name,
                f"Employee ID {ctx.employee_id} not found in database",
            )

        # Idempotency check: if already ONBOARDING, nothing to transition
        if employee.status == EmployeeStatus.ONBOARDING:
            return ctx

        # Validate allowed transition from current state (e.g. PENDING or FAILED) to ONBOARDING
        validate_employee_transition(employee.status, EmployeeStatus.ONBOARDING)
        employee.status = EmployeeStatus.ONBOARDING
        await session.flush()

        logger.info("Employee %s status transitioned to ONBOARDING", ctx.employee_email)
        return ctx

    async def compensate(self, session: AsyncSession, ctx: WorkflowContext) -> None:
        """Rollback: Marks employee record as FAILED. Never deletes data or skills."""
        stmt = select(Employee).where(Employee.id == ctx.employee_id).with_for_update()
        result = await session.execute(stmt)
        employee = result.scalar_one_or_none()

        if employee and employee.status != EmployeeStatus.FAILED:
            employee.status = EmployeeStatus.FAILED
            await session.flush()
            logger.info(
                "Employee %s marked as FAILED in compensation",
                ctx.employee_email,
            )


# ─────────────────────────────────────────────────────────────
# Step 2: IT Provisioning (Simulated External System)
# ─────────────────────────────────────────────────────────────
class ITProvisioningStep(WorkflowStep):
    """Simulated IT provisioning adapter (Email, Slack, Laptop assignment).

    In real environments, this interacts with external IT identity providers.
    """

    step_number = 2
    step_name = "it_provisioning"

    async def execute(self, session: AsyncSession, ctx: WorkflowContext) -> WorkflowContext:
        # Check for simulated failure injection
        if ctx.simulate_failure == "it":
            logger.warning(
                "Simulated failure triggered at IT Provisioning for %s",
                ctx.employee_email,
            )
            raise ExternalServiceError(
                self.step_name,
                "IT service unavailable: timeout communicating with Identity Provider",
            )

        # Simulate provisioning and store external account ID in context
        account_id = f"IT-ACC-{ctx.employee_id.hex[:8].upper()}"
        ctx.it_account_id = account_id
        ctx.extra_data["it_email_created"] = ctx.employee_email
        ctx.extra_data["it_slack_user_id"] = f"U{ctx.employee_id.hex[:8].upper()}"

        logger.info(
            "IT Provisioning completed for %s [Account: %s]",
            ctx.employee_email,
            account_id,
        )
        return ctx

    async def compensate(self, session: AsyncSession, ctx: WorkflowContext) -> None:
        """Rollback: Deprovisions any created IT accounts."""
        if ctx.it_account_id:
            logger.info(
                "Compensating IT Provisioning: Revoking account %s for %s",
                ctx.it_account_id,
                ctx.employee_email,
            )
            ctx.extra_data["it_deprovisioned"] = True
            ctx.it_account_id = None


# ─────────────────────────────────────────────────────────────
# Step 3: Security & Access Assignment (Simulated External System)
# ─────────────────────────────────────────────────────────────
class SecurityAccessStep(WorkflowStep):
    """Simulated Security & Access adapter (Physical Badge, VPN, IAM roles).

    In real environments, this interacts with Badge Systems (e.g. Lenel) and Cloud IAM.
    """

    step_number = 3
    step_name = "security_access"

    async def execute(self, session: AsyncSession, ctx: WorkflowContext) -> WorkflowContext:
        # Check for simulated failure injection
        if ctx.simulate_failure == "security":
            logger.warning(
                "Simulated failure triggered at Security Access for %s",
                ctx.employee_email,
            )
            raise ExternalServiceError(
                self.step_name,
                "Security clearance check failed: automated badge authorization rejected",
            )

        # Simulate security profile creation
        security_id = f"SEC-PRF-{ctx.employee_id.hex[:8].upper()}"
        ctx.security_profile_id = security_id
        ctx.extra_data["badge_id"] = f"BDG-{ctx.employee_id.hex[:6].upper()}"
        ctx.extra_data["vpn_access"] = "granted"

        logger.info(
            "Security & Access assignment completed for %s [Profile: %s]",
            ctx.employee_email,
            security_id,
        )
        return ctx

    async def compensate(self, session: AsyncSession, ctx: WorkflowContext) -> None:
        """Rollback: Revokes issued badges, VPN profiles, and IAM privileges."""
        if ctx.security_profile_id:
            logger.info(
                "Compensating Security Access: Revoking profile %s for %s",
                ctx.security_profile_id,
                ctx.employee_email,
            )
            ctx.extra_data["security_revoked"] = True
            ctx.security_profile_id = None
