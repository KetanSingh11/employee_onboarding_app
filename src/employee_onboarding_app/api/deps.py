from collections.abc import Callable
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from employee_onboarding_app.config import settings
from employee_onboarding_app.database import get_db

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

ROLE_ADMIN = "admin"
ROLE_HR = "hr_manager"
ROLE_VIEWER = "viewer"


async def get_current_role(
    api_key: str | None = Security(api_key_header),
) -> str:
    """Resolves and validates the API key provided in the X-API-Key header to its corresponding role context."""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required authentication header: 'X-API-Key'",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if api_key == settings.API_KEY_ADMIN:
        return ROLE_ADMIN
    elif api_key == settings.API_KEY_HR:
        return ROLE_HR
    elif api_key == settings.API_KEY_VIEWER:
        return ROLE_VIEWER
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid 'X-API-Key' provided",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def require_role(allowed_roles: list[str]) -> Callable[..., str]:
    """Factory dependency enforcing Role-Based Access Control (RBAC)."""

    async def _role_checker(role: str = Depends(get_current_role)) -> str:
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: role '{role}' does not have sufficient permissions for this operation",
            )
        return role

    return _role_checker


# Role dependencies
require_viewer = require_role([ROLE_VIEWER, ROLE_HR, ROLE_ADMIN])
require_hr = require_role([ROLE_HR, ROLE_ADMIN])
require_admin = require_role([ROLE_ADMIN])
