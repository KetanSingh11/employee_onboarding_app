import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from employee_onboarding_app.config import settings
from employee_onboarding_app.models import (
    Employee,
    EmployeeSkillMap,
    EmployeeStatus,
    ProficiencyLevel,
    Skill,
)


@pytest.mark.asyncio
async def test_csv_import_endpoint(client: AsyncClient):
    """Verifies CSV import endpoint accepts file and returns 202 Accepted."""
    csv_payload = (
        "email,first_name,last_name,department,job_title,start_date,skills\n"
        'test.user@company.com,Test,User,Engineering,Dev,2026-11-01,"Python:expert"\n'
    )

    response = await client.post(
        "/api/v1/employees/import",
        files={"file": ("import.csv", csv_payload.encode(), "text/csv")},
        headers={"X-API-Key": settings.API_KEY_HR},
    )

    assert response.status_code == 202
    data = response.json()
    assert "batch_id" in data
    assert data["status"] == "PROCESSING"


@pytest.mark.asyncio
async def test_skill_filtering_query(client: AsyncClient, db_session: AsyncSession):
    """Verifies that filtering by skill and minimum proficiency filters accurately at SQL level.

    Expected: 'kubernetes' with min_proficiency=advanced matches expert and advanced.
    """
    # 1. Seed master skill
    skill_k8s = Skill(name="kubernetes")
    db_session.add(skill_k8s)
    await db_session.flush()

    # 2. Seed 3 test employees
    emp_alex = Employee(
        email="alex.k8s@company.com",
        first_name="Alex",
        last_name="Morgan",
        start_date=date.today(),
        status=EmployeeStatus.ACTIVE,
    )
    emp_sarah = Employee(
        email="sarah.k8s@company.com",
        first_name="Sarah",
        last_name="Connor",
        start_date=date.today(),
        status=EmployeeStatus.ACTIVE,
    )
    emp_jordan = Employee(
        email="jordan.k8s@company.com",
        first_name="Jordan",
        last_name="Lee",
        start_date=date.today(),
        status=EmployeeStatus.ACTIVE,
    )
    db_session.add_all([emp_alex, emp_sarah, emp_jordan])
    await db_session.flush()

    # 3. Seed skill proficiency mappings: Alex=expert, Sarah=intermediate, Jordan=advanced
    db_session.add_all(
        [
            EmployeeSkillMap(
                employee_id=emp_alex.id,
                skill_id=skill_k8s.id,
                proficiency_level=ProficiencyLevel.EXPERT,
            ),
            EmployeeSkillMap(
                employee_id=emp_sarah.id,
                skill_id=skill_k8s.id,
                proficiency_level=ProficiencyLevel.INTERMEDIATE,
            ),
            EmployeeSkillMap(
                employee_id=emp_jordan.id,
                skill_id=skill_k8s.id,
                proficiency_level=ProficiencyLevel.ADVANCED,
            ),
        ]
    )
    await db_session.commit()

    # 4. Query API
    response = await client.get(
        "/api/v1/employees?skill=kubernetes&min_proficiency=advanced",
        headers={"X-API-Key": settings.API_KEY_VIEWER},
    )

    assert response.status_code == 200
    results = response.json()
    result_emails = [r["email"] for r in results]

    assert "alex.k8s@company.com" in result_emails
    assert "jordan.k8s@company.com" in result_emails
    assert "sarah.k8s@company.com" not in result_emails


@pytest.mark.asyncio
async def test_auth_enforcement(client: AsyncClient):
    """Verifies that unauthorized requests receive 401 and role-restricted actions receive 403."""
    # 1. Missing API key -> 401
    res_no_key = await client.get("/api/v1/employees", headers={})
    assert res_no_key.status_code == 401

    # 2. Invalid API key -> 401
    res_bad_key = await client.get("/api/v1/employees", headers={"X-API-Key": "totally-bogus-key"})
    assert res_bad_key.status_code == 401

    # 3. Insufficient permissions (viewer attempting HR write endpoint) -> 403
    dummy_id = uuid.uuid4()
    res_forbidden = await client.post(
        f"/api/v1/employees/{dummy_id}/skills",
        json={"skill_name": "python", "proficiency_level": "expert"},
        headers={"X-API-Key": settings.API_KEY_VIEWER},
    )
    assert res_forbidden.status_code == 403


@pytest.mark.asyncio
async def test_idempotency_header(client: AsyncClient):
    """Verifies identical requests with matching Idempotency-Key return cached response on retry."""
    key = f"test-idemp-{uuid.uuid4().hex[:8]}"
    csv_payload = (
        "email,first_name,last_name,start_date\nidemp.user@company.com,Idemp,User,2026-11-01\n"
    )

    # 1. First invocation
    res1 = await client.post(
        "/api/v1/employees/import",
        files={"file": ("test.csv", csv_payload.encode(), "text/csv")},
        headers={"X-API-Key": settings.API_KEY_HR, "Idempotency-Key": key},
    )
    assert res1.status_code == 202

    # 2. Duplicate invocation with same key
    res2 = await client.post(
        "/api/v1/employees/import",
        files={"file": ("test.csv", csv_payload.encode(), "text/csv")},
        headers={"X-API-Key": settings.API_KEY_HR, "Idempotency-Key": key},
    )
    assert res2.status_code == 202
    assert res2.headers.get("X-Cache") == "HIT-IDEMPOTENCY"
