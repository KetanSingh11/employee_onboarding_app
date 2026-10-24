import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from employee_onboarding_app.models import (
    BatchStatus,
    EmployeeStatus,
    ProficiencyLevel,
    StepDirection,
    StepStatus,
    WorkflowStatus,
    WorkflowType,
)


# ─────────────────────────────────────────────────────────────
# Skill Schemas
# ─────────────────────────────────────────────────────────────
class SkillBase(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        clean = v.strip().lower()
        if not clean:
            raise ValueError("Skill name cannot be empty")
        return clean


class SkillCreate(SkillBase):
    pass


class SkillRead(SkillBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EmployeeSkillAssignment(BaseModel):
    skill_name: str
    proficiency_level: ProficiencyLevel

    @field_validator("skill_name")
    @classmethod
    def normalize_skill_name(cls, v: str) -> str:
        clean = v.strip().lower()
        if not clean:
            raise ValueError("Skill name cannot be empty")
        return clean


class EmployeeSkillUpdate(BaseModel):
    proficiency_level: ProficiencyLevel


class EmployeeSkillRead(BaseModel):
    skill_id: uuid.UUID
    skill_name: str
    proficiency_level: ProficiencyLevel
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ─────────────────────────────────────────────────────────────
# Employee Schemas
# ─────────────────────────────────────────────────────────────
class EmployeeBase(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    department: str | None = None
    job_title: str | None = None
    start_date: date

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: EmailStr) -> str:
        return str(v).strip().lower()

    @field_validator("first_name", "last_name")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        clean = v.strip().title()
        if not clean:
            raise ValueError("Name cannot be empty")
        return clean

    @field_validator("department", "job_title")
    @classmethod
    def clean_optional_strings(cls, v: str | None) -> str | None:
        if v is None:
            return None
        clean = v.strip()
        return clean if clean else None

    @field_validator("start_date")
    @classmethod
    def validate_start_date_not_past(cls, v: date) -> date:
        today = date.today()
        if v < today:
            raise ValueError(f"start_date cannot be in the past ({v} < {today})")
        return v


class EmployeeCreate(EmployeeBase):
    skills: list[EmployeeSkillAssignment] = []


class EmployeeRead(EmployeeBase):
    id: uuid.UUID
    status: EmployeeStatus
    skills: list[EmployeeSkillRead] = []
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ─────────────────────────────────────────────────────────────
# Workflow & Audit Schemas
# ─────────────────────────────────────────────────────────────
class WorkflowAuditLogRead(BaseModel):
    id: uuid.UUID
    step_number: int
    step_name: str
    direction: StepDirection
    status: StepStatus
    payload: dict[str, Any] | None = None
    error_detail: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkflowExecutionRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    batch_id: uuid.UUID | None = None
    workflow_type: WorkflowType
    status: WorkflowStatus
    current_step: int
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    audit_logs: list[WorkflowAuditLogRead] = []

    model_config = ConfigDict(from_attributes=True)


class WorkflowRetryResponse(BaseModel):
    message: str
    new_workflow_id: uuid.UUID
    employee_id: uuid.UUID
    status: WorkflowStatus


# ─────────────────────────────────────────────────────────────
# Batch Ingestion Schemas
# ─────────────────────────────────────────────────────────────
class BatchIngestAcceptedResponse(BaseModel):
    batch_id: uuid.UUID
    filename: str
    status: BatchStatus
    message: str


class BatchRunRead(BaseModel):
    id: uuid.UUID
    filename: str
    status: BatchStatus
    total_rows: int
    valid_rows: int
    skipped_invalid: int
    skipped_duplicate: int
    triggered_onboarding: int
    report: list[dict[str, Any]] | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ─────────────────────────────────────────────────────────────
# Health & Status Schemas
# ─────────────────────────────────────────────────────────────
class HealthResponse(BaseModel):
    status: str
    timestamp: datetime


class ReadinessResponse(BaseModel):
    status: str
    database: str
    timestamp: datetime
