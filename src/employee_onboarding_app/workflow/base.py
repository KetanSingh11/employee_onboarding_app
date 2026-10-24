import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class WorkflowStepError(Exception):
    """Base exception for workflow step execution errors."""

    def __init__(self, step_name: str, message: str) -> None:
        super().__init__(f"Step '{step_name}' failed: {message}")
        self.step_name = step_name
        self.message = message


class ExternalServiceError(WorkflowStepError):
    """Raised when an external simulated dependency (IT / Security) encounters a failure."""

    pass


@dataclass
class WorkflowContext:
    """Typed execution context passed sequentially through each workflow step and compensator."""

    employee_id: uuid.UUID
    employee_email: str
    workflow_id: uuid.UUID
    batch_id: uuid.UUID | None = None
    simulate_failure: str | None = None  # e.g. 'it', 'security'

    # Output attributes populated progressively by forward steps:
    it_account_id: str | None = None
    security_profile_id: str | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)


class WorkflowStep(ABC):
    """Abstract Base Class for an atomic, idempotent saga workflow step (SOLID OCP)."""

    step_number: int
    step_name: str

    @abstractmethod
    async def execute(self, session: AsyncSession, ctx: WorkflowContext) -> WorkflowContext:
        """Executes the forward operational step. Must be idempotent."""
        pass

    @abstractmethod
    async def compensate(self, session: AsyncSession, ctx: WorkflowContext) -> None:
        """Executes the compensating (rollback) action for this step. Must be idempotent."""
        pass


class AlertService(ABC):
    """Abstract Base Class for workflow event notification and alerting."""

    @abstractmethod
    def info(self, message: str, context: dict[str, Any] | None = None) -> None:
        pass

    @abstractmethod
    def error(self, message: str, context: dict[str, Any] | None = None) -> None:
        pass
