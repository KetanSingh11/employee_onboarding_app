import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from employee_onboarding_app.api import employees, skills, workflows
from employee_onboarding_app.config import settings
from employee_onboarding_app.database import AsyncSessionLocal, engine
from employee_onboarding_app.middleware import correlation_and_idempotency_middleware
from employee_onboarding_app.schemas import HealthResponse, ReadinessResponse
from employee_onboarding_app.services.ingestion import recover_orphaned_batches

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("employee_onboarding_app.main")


# ─────────────────────────────────────────────────────────────
# Application Lifespan: Startup Recovery & Engine Teardown
# ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    logger.info("Initializing Employee Onboarding App...")

    # Startup recovery: detect and recover any orphaned batch runs from prior crashes
    async with AsyncSessionLocal() as session:
        try:
            recovered_count = await recover_orphaned_batches(session)
            if recovered_count > 0:
                logger.warning(
                    "Startup recovery: marked %d stale batch runs as FAILED",
                    recovered_count,
                )
        except Exception as rec_err:
            logger.error("Failed to run startup batch recovery: %s", rec_err)

    yield

    logger.info("Shutting down Employee Onboarding App: disposing engine...")
    await engine.dispose()


app = FastAPI(
    title="Staff & Skill Planning Tool — Employee Onboarding Service",
    description=(
        "Production-grade backend service coordinating multi-step employee onboarding workflows "
        "with saga-based consistency guarantees, rollback logic, and skill management."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Standard CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# Request Correlation & Idempotency Middleware (ADR-09, ADR-20)
# ─────────────────────────────────────────────────────────────
app.middleware("http")(correlation_and_idempotency_middleware)


# ─────────────────────────────────────────────────────────────
# Health & Readiness Endpoints (ADR-23)
# ─────────────────────────────────────────────────────────────
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Service Liveness Probe",
)
async def liveness_probe() -> HealthResponse:
    """Liveness probe returning 200 as long as the application process is running."""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(UTC),
    )


@app.get(
    "/ready",
    response_model=ReadinessResponse,
    tags=["System"],
    summary="Service Readiness Probe (Database Connectivity Check)",
)
async def readiness_probe() -> ReadinessResponse:
    """Readiness probe checking active PostgreSQL database connectivity."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return ReadinessResponse(
            status="ready",
            database="connected",
            timestamp=datetime.now(UTC),
        )
    except Exception as db_err:
        logger.error("Readiness probe DB check failed: %s", db_err)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unready",
                "database": f"disconnected: {db_err}",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )  # type: ignore


# ─────────────────────────────────────────────────────────────
# Mount Routers under /api/v1
# ─────────────────────────────────────────────────────────────
app.include_router(employees.router, prefix="/api/v1")
app.include_router(skills.router, prefix="/api/v1")
app.include_router(workflows.router, prefix="/api/v1")
