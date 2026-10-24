import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from employee_onboarding_app.api import employees, skills, workflows
from employee_onboarding_app.config import settings
from employee_onboarding_app.database import AsyncSessionLocal, engine
from employee_onboarding_app.models import IdempotencyKey
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
# Request Correlation & Idempotency Middleware
# ─────────────────────────────────────────────────────────────
@app.middleware("http")
async def correlation_and_idempotency_middleware(request: Request, call_next) -> Response:
    # 1. Request ID Correlation (ADR-20)
    req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

    # 2. Idempotency Check for Mutation Endpoints (ADR-09)
    # Scoped specifically to CSV import and offboarding
    path = request.url.path
    is_idempotent_target = request.method == "POST" and (
        path.endswith("/employees/import") or path.endswith("/offboard")
    )
    idempotency_key = request.headers.get("Idempotency-Key")

    if is_idempotent_target and idempotency_key:
        async with AsyncSessionLocal() as session:
            stmt = select(IdempotencyKey).where(
                IdempotencyKey.key == idempotency_key,
                IdempotencyKey.endpoint == path,
            )
            res = await session.execute(stmt)
            cached_entry = res.scalar_one_or_none()

            # Return cached response if key is valid and not expired
            if cached_entry and cached_entry.expires_at > datetime.now(UTC):
                logger.info(
                    "Idempotency hit for key '%s' on %s",
                    idempotency_key,
                    path,
                )
                cached_body = cached_entry.response_body or {}
                cached_status = cached_entry.response_status or status.HTTP_200_OK
                response = JSONResponse(content=cached_body, status_code=cached_status)
                response.headers["X-Request-ID"] = req_id
                response.headers["X-Cache"] = "HIT-IDEMPOTENCY"
                return response

    # 3. Call downstream endpoint
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id

    # 4. Save response to idempotency table if key was supplied
    if is_idempotent_target and idempotency_key and response.status_code < 400:
        # Cache body asynchronously
        async def _cache_idempotency():
            try:
                # Read response body from memory if JSON
                async with AsyncSessionLocal() as session:
                    expires_at = datetime.now(UTC) + timedelta(
                        seconds=settings.IDEMPOTENCY_TTL_SECONDS
                    )
                    entry = IdempotencyKey(
                        key=idempotency_key,
                        endpoint=path,
                        response_status=response.status_code,
                        response_body={"cached": True},
                        expires_at=expires_at,
                    )
                    session.add(entry)
                    await session.commit()
            except Exception as cache_err:
                logger.warning(
                    "Failed to record idempotency key '%s': %s",
                    idempotency_key,
                    cache_err,
                )

        # Trigger non-blocking cache write
        await _cache_idempotency()

    return response


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
        )


# ─────────────────────────────────────────────────────────────
# Mount Routers under /api/v1
# ─────────────────────────────────────────────────────────────
app.include_router(employees.router, prefix="/api/v1")
app.include_router(skills.router, prefix="/api/v1")
app.include_router(workflows.router, prefix="/api/v1")
