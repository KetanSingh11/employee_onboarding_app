import asyncio
import csv
import io
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from employee_onboarding_app.models import (
    BatchRun,
    BatchStatus,
    Employee,
    EmployeeSkillMap,
    EmployeeStatus,
    ProficiencyLevel,
    Skill,
    WorkflowExecution,
    WorkflowType,
)
from employee_onboarding_app.schemas import EmployeeBase
from employee_onboarding_app.workflow.alert import default_alert_service
from employee_onboarding_app.workflow.base import WorkflowContext
from employee_onboarding_app.workflow.orchestrator import default_orchestrator

logger = logging.getLogger("employee_onboarding_app.services.ingestion")

REQUIRED_CSV_COLUMNS = {"email", "first_name", "last_name", "start_date"}


class ParsedEmployeeRecord:
    def __init__(
        self,
        row_number: int,
        email: str,
        first_name: str,
        last_name: str,
        start_date: date,
        department: str | None,
        job_title: str | None,
        skills: list[tuple[str, ProficiencyLevel]],
    ) -> None:
        self.row_number = row_number
        self.email = email
        self.first_name = first_name
        self.last_name = last_name
        self.start_date = start_date
        self.department = department
        self.job_title = job_title
        self.skills = skills


def parse_and_validate_csv_content(
    content: str,
) -> tuple[list[ParsedEmployeeRecord], list[dict[str, Any]]]:
    """Parses raw CSV content, enforces header requirements, and validates each row individually.

    Does NOT abort the entire file on row-level validation issues.
    """
    f = io.StringIO(content.strip())
    reader = csv.DictReader(f)

    if not reader.fieldnames:
        raise ValueError("Uploaded CSV is empty or has no header row")

    fieldnames = {col.strip().lower() for col in reader.fieldnames if col}
    missing_required = REQUIRED_CSV_COLUMNS - fieldnames
    if missing_required:
        raise ValueError(f"Missing required CSV header columns: {sorted(list(missing_required))}")

    valid_records: list[ParsedEmployeeRecord] = []
    error_reports: list[dict[str, Any]] = []

    for row_idx, raw_row in enumerate(reader, start=2):
        row = {k.strip().lower(): (v.strip() if v else "") for k, v in raw_row.items() if k}

        email = row.get("email", "")
        # Validate base employee data via Pydantic schema
        try:
            start_date_val = row.get("start_date", "")
            parsed_date = date.fromisoformat(start_date_val)
        except Exception:
            error_reports.append(
                {
                    "row": row_idx,
                    "email": email,
                    "reason": (
                        f"Invalid start_date format '{row.get('start_date')}'. Expected YYYY-MM-DD"
                    ),
                }
            )
            continue

        try:
            emp_schema = EmployeeBase(
                email=email,
                first_name=row.get("first_name", ""),
                last_name=row.get("last_name", ""),
                department=row.get("department") or None,
                job_title=row.get("job_title") or None,
                start_date=parsed_date,
            )
        except ValidationError as val_err:
            error_reports.append(
                {
                    "row": row_idx,
                    "email": email,
                    "reason": f"Field validation error: {val_err.errors()}",
                }
            )
            continue

        # Parse skills token if present
        skills_raw = row.get("skills", "").strip()
        parsed_skills: list[tuple[str, ProficiencyLevel]] = []
        skills_valid = True

        if skills_raw:
            # Format: "Python:expert;FastAPI:intermediate"
            skill_tokens = [t.strip() for t in skills_raw.split(";") if t.strip()]
            for token in skill_tokens:
                parts = token.split(":")
                if len(parts) != 2:
                    error_reports.append(
                        {
                            "row": row_idx,
                            "email": email,
                            "reason": (
                                f"Malformed skill token '{token}'. Expected 'skill_name:level'"
                            ),
                        }
                    )
                    skills_valid = False
                    break

                skill_name = parts[0].strip().lower()
                level_str = parts[1].strip().lower()

                try:
                    level_enum = ProficiencyLevel(level_str)
                except ValueError:
                    allowed_levels = [e.value for e in ProficiencyLevel]
                    error_reports.append(
                        {
                            "row": row_idx,
                            "email": email,
                            "reason": (
                                f"Invalid proficiency level '{level_str}'. "
                                f"Allowed: {allowed_levels}"
                            ),
                        }
                    )
                    skills_valid = False
                    break

                parsed_skills.append((skill_name, level_enum))

        if not skills_valid:
            continue

        valid_records.append(
            ParsedEmployeeRecord(
                row_number=row_idx,
                email=emp_schema.email,
                first_name=emp_schema.first_name,
                last_name=emp_schema.last_name,
                start_date=emp_schema.start_date,
                department=emp_schema.department,
                job_title=emp_schema.job_title,
                skills=parsed_skills,
            )
        )

    return valid_records, error_reports


async def process_batch_ingestion(
    session: AsyncSession,
    batch_id: uuid.UUID,
    csv_content: str,
    simulate_failure: str | None = None,
) -> None:
    """Processes CSV ingestion asynchronously in a background task:

    1. Parses & validates CSV.
    2. Persists valid new employees and skill mappings in database (BEFORE saga).
    3. Triggers Saga orchestrator for each activated employee.
    4. Updates batch status and generates row-level execution report.
    """
    stmt = select(BatchRun).where(BatchRun.id == batch_id)
    result = await session.execute(stmt)
    batch = result.scalar_one_or_none()
    if not batch:
        logger.error("BatchRun %s not found for processing", batch_id)
        return

    try:
        valid_records, errors = await asyncio.to_thread(parse_and_validate_csv_content, csv_content)
    except Exception as exc:
        batch.status = BatchStatus.FAILED
        batch.report = [{"error": str(exc)}]
        await session.commit()
        return

    total_rows = len(valid_records) + len(errors)
    batch.total_rows = total_rows
    batch.skipped_invalid = len(errors)

    skipped_duplicate = 0
    triggered_onboarding = 0
    report_items = list(errors)

    for record in valid_records:
        # Check domain-level idempotency via email existence
        emp_stmt = select(Employee).where(Employee.email == record.email)
        emp_res = await session.execute(emp_stmt)
        existing_emp = emp_res.scalar_one_or_none()

        if existing_emp:
            skipped_duplicate += 1
            report_items.append(
                {
                    "row": record.row_number,
                    "email": record.email,
                    "reason": f"Employee with email '{record.email}' already exists in system",
                }
            )
            continue

        try:
            # 1. Insert Employee with status=PENDING
            employee = Employee(
                email=record.email,
                first_name=record.first_name,
                last_name=record.last_name,
                department=record.department,
                job_title=record.job_title,
                start_date=record.start_date,
                status=EmployeeStatus.PENDING,
            )
            session.add(employee)
            await session.flush()

            # 2. Insert Skills (auto-create in master table if absent)
            for skill_name, level in record.skills:
                skill_stmt = select(Skill).where(Skill.name == skill_name)
                skill_res = await session.execute(skill_stmt)
                skill_obj = skill_res.scalar_one_or_none()

                if not skill_obj:
                    skill_obj = Skill(name=skill_name)
                    session.add(skill_obj)
                    await session.flush()

                mapping = EmployeeSkillMap(
                    employee_id=employee.id,
                    skill_id=skill_obj.id,
                    proficiency_level=level,
                )
                session.add(mapping)

            # 3. Create WorkflowExecution instance
            workflow = WorkflowExecution(
                employee_id=employee.id,
                batch_id=batch_id,
                workflow_type=WorkflowType.ONBOARDING,
            )
            session.add(workflow)
            await session.flush()

            # Commit employee + skill persistence BEFORE running saga
            await session.commit()
            triggered_onboarding += 1
        except IntegrityError:
            await session.rollback()
            skipped_duplicate += 1
            report_items.append(
                {
                    "row": record.row_number,
                    "email": record.email,
                    "reason": (
                        f"Concurrent duplicate detected: employee with email '{record.email}' "
                        "was registered simultaneously"
                    ),
                }
            )
            continue

        # 4. Run Saga Workflow Orchestrator
        ctx = WorkflowContext(
            employee_id=employee.id,
            employee_email=employee.email,
            workflow_id=workflow.id,
            batch_id=batch_id,
            simulate_failure=simulate_failure,
        )

        try:
            await default_orchestrator.run(session, ctx)
        except Exception as saga_err:
            logger.error("Unhandled saga exception for %s: %s", record.email, saga_err)

    # 5. Finalize BatchRun record
    batch.valid_rows = len(valid_records)
    batch.skipped_duplicate = skipped_duplicate
    batch.triggered_onboarding = triggered_onboarding
    batch.report = report_items

    if triggered_onboarding == 0 and total_rows > 0:
        batch.status = BatchStatus.FAILED
    elif len(report_items) > 0:
        batch.status = BatchStatus.PARTIALLY_COMPLETED
    else:
        batch.status = BatchStatus.COMPLETED

    await session.commit()
    logger.info(
        "Batch %s completed: %d total, %d onboarded, %d duplicate, %d invalid",
        batch_id,
        total_rows,
        triggered_onboarding,
        skipped_duplicate,
        batch.skipped_invalid,
    )


async def recover_orphaned_batches(session: AsyncSession) -> int:
    """Recovers batches left in PROCESSING state after an unexpected worker crash."""
    stale_threshold = datetime.now(UTC) - timedelta(minutes=10)
    stmt = select(BatchRun).where(
        BatchRun.status == BatchStatus.PROCESSING,
        BatchRun.created_at < stale_threshold,
    )
    result = await session.execute(stmt)
    stale_batches = result.scalars().all()

    for b in stale_batches:
        b.status = BatchStatus.FAILED
        b.report = [{"error": "worker_crash_recovery_detected"}]
        default_alert_service.error(
            f"Recovered orphaned batch {b.id}: marked as FAILED due to timeout",
            context={"batch_id": str(b.id)},
        )

    if stale_batches:
        await session.commit()

    return len(stale_batches)
