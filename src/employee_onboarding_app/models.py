import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from employee_onboarding_app.database import Base


# ─────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────
class EmployeeStatus(enum.StrEnum):
    PENDING = "PENDING"
    ONBOARDING = "ONBOARDING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    OFFBOARDING = "OFFBOARDING"
    INACTIVE = "INACTIVE"
    OFFBOARDING_FAILED = "OFFBOARDING_FAILED"


class WorkflowType(enum.StrEnum):
    ONBOARDING = "ONBOARDING"
    OFFBOARDING = "OFFBOARDING"


class WorkflowStatus(enum.StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


class StepDirection(enum.StrEnum):
    FORWARD = "FORWARD"
    COMPENSATE = "COMPENSATE"


class StepStatus(enum.StrEnum):
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ProficiencyLevel(enum.StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    EXPERT = "expert"


class BatchStatus(enum.StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"


# ─────────────────────────────────────────────────────────────
# State Transition Engine
# ─────────────────────────────────────────────────────────────
class InvalidStateTransitionError(Exception):
    """Raised when an illegal employee state transition is requested."""

    def __init__(self, current: EmployeeStatus, target: EmployeeStatus) -> None:
        super().__init__(
            f"Invalid employee status transition from '{current.value}' to '{target.value}'"
        )
        self.current = current
        self.target = target


VALID_TRANSITIONS: dict[EmployeeStatus, set[EmployeeStatus]] = {
    EmployeeStatus.PENDING: {EmployeeStatus.ONBOARDING},
    EmployeeStatus.ONBOARDING: {EmployeeStatus.ACTIVE, EmployeeStatus.FAILED},
    EmployeeStatus.ACTIVE: {EmployeeStatus.OFFBOARDING},
    EmployeeStatus.FAILED: {EmployeeStatus.PENDING, EmployeeStatus.ONBOARDING},
    EmployeeStatus.OFFBOARDING: {
        EmployeeStatus.INACTIVE,
        EmployeeStatus.OFFBOARDING_FAILED,
    },
    EmployeeStatus.OFFBOARDING_FAILED: {EmployeeStatus.OFFBOARDING},
}


def validate_employee_transition(current: EmployeeStatus, target: EmployeeStatus) -> None:
    if target not in VALID_TRANSITIONS.get(current, set()):
        raise InvalidStateTransitionError(current, target)


# ─────────────────────────────────────────────────────────────
# ORM Models
# ─────────────────────────────────────────────────────────────
class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[EmployeeStatus] = mapped_column(
        Enum(EmployeeStatus, name="employee_status_enum", native_enum=True),
        default=EmployeeStatus.PENDING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    skills: Mapped[list[EmployeeSkillMap]] = relationship(
        "EmployeeSkillMap",
        back_populates="employee",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    workflows: Mapped[list[WorkflowExecution]] = relationship(
        "WorkflowExecution", back_populates="employee", cascade="all, delete-orphan"
    )


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    employee_maps: Mapped[list[EmployeeSkillMap]] = relationship(
        "EmployeeSkillMap", back_populates="skill"
    )


class EmployeeSkillMap(Base):
    __tablename__ = "employee_skill_map"
    __table_args__ = (UniqueConstraint("employee_id", "skill_id", name="uq_employee_skill_unique"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True
    )
    proficiency_level: Mapped[ProficiencyLevel] = mapped_column(
        Enum(ProficiencyLevel, name="proficiency_level_enum", native_enum=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    employee: Mapped[Employee] = relationship("Employee", back_populates="skills")
    skill: Mapped[Skill] = relationship("Skill", back_populates="employee_maps", lazy="selectin")


class BatchRun(Base):
    __tablename__ = "batch_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus, name="batch_status_enum", native_enum=True),
        default=BatchStatus.PROCESSING,
        nullable=False,
    )
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    valid_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_invalid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_duplicate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    triggered_onboarding: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    report: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    workflows: Mapped[list[WorkflowExecution]] = relationship(
        "WorkflowExecution", back_populates="batch_run"
    )


class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        # Partial unique index: at most one active workflow per employee at any time
        Index(
            "uq_one_active_workflow_per_employee",
            "employee_id",
            unique=True,
            postgresql_where=text("status NOT IN ('COMPLETED', 'ROLLED_BACK', 'FAILED')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("batch_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    workflow_type: Mapped[WorkflowType] = mapped_column(
        Enum(WorkflowType, name="workflow_type_enum", native_enum=True),
        default=WorkflowType.ONBOARDING,
        nullable=False,
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        Enum(WorkflowStatus, name="workflow_status_enum", native_enum=True),
        default=WorkflowStatus.PENDING,
        nullable=False,
    )
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    employee: Mapped[Employee] = relationship("Employee", back_populates="workflows")
    batch_run: Mapped[BatchRun | None] = relationship("BatchRun", back_populates="workflows")
    audit_logs: Mapped[list[WorkflowAuditLog]] = relationship(
        "WorkflowAuditLog",
        back_populates="workflow_execution",
        cascade="all, delete-orphan",
        order_by="WorkflowAuditLog.step_number",
        lazy="selectin",
    )


class WorkflowAuditLog(Base):
    __tablename__ = "workflow_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(100), nullable=False)
    direction: Mapped[StepDirection] = mapped_column(
        Enum(StepDirection, name="step_direction_enum", native_enum=True),
        nullable=False,
    )
    status: Mapped[StepStatus] = mapped_column(
        Enum(StepStatus, name="step_status_enum", native_enum=True),
        nullable=False,
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    workflow_execution: Mapped[WorkflowExecution] = relationship(
        "WorkflowExecution", back_populates="audit_logs"
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
