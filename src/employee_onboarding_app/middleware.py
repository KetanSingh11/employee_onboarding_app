import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from employee_onboarding_app.config import settings
from employee_onboarding_app.database import AsyncSessionLocal
from employee_onboarding_app.models import IdempotencyKey

logger = logging.getLogger(__name__)


async def correlation_and_idempotency_middleware(request: Request, call_next) -> Response:
    """HTTP middleware handling X-Request-ID correlation and scoped Idempotency-Key caching."""
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

        await _cache_idempotency()

    return response

