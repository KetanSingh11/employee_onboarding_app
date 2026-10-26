from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import employee_onboarding_app.api.employees as emp_mod
import employee_onboarding_app.api.workflows as wf_mod
import employee_onboarding_app.database as db_mod
import employee_onboarding_app.main as main_mod
import employee_onboarding_app.middleware as mid_mod
from employee_onboarding_app.api.deps import get_db
from employee_onboarding_app.config import settings
from employee_onboarding_app.database import Base
from employee_onboarding_app.main import app

# Target the test database URL
settings.DATABASE_URL = settings.TEST_DATABASE_URL

# NullPool ensures database connections are created on-demand and closed immediately,
# preventing connections from persisting across closed asyncio event loops.
test_engine = create_async_engine(
    settings.TEST_DATABASE_URL,
    poolclass=NullPool,
    echo=False,
)

TestingSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# Patch all modules that reference AsyncSessionLocal directly
db_mod.AsyncSessionLocal = TestingSessionLocal
main_mod.AsyncSessionLocal = TestingSessionLocal
emp_mod.AsyncSessionLocal = TestingSessionLocal
wf_mod.AsyncSessionLocal = TestingSessionLocal
mid_mod.AsyncSessionLocal = TestingSessionLocal


@pytest_asyncio.fixture(scope="session", autouse=True)
async def init_test_schema():
    """Initializes tables once per test session and leaves them intact for inspection."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await test_engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    """Cleans all tables via TRUNCATE CASCADE before and after each test.

    Guarantees 100% test isolation and ensures no test leftovers remain even on failure.
    """
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE workflow_audit_logs, workflow_executions, "
                "employee_skill_map, employees, skills, batch_runs, idempotency_keys CASCADE;"
            )
        )
    yield
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE workflow_audit_logs, workflow_executions, "
                "employee_skill_map, employees, skills, batch_runs, idempotency_keys CASCADE;"
            )
        )


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Provides a clean database session for each test."""
    async with TestingSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Asynchronous HTTP test client with overridden database dependency.

    Does not inject default auth headers, allowing tests to verify 401/403 behavior explicitly.
    """

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
