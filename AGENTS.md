# employee-onboarding-app

Python REST API for multi-step employee onboarding with saga-based workflow orchestration, skill management, and CSV batch ingestion. Designed to senior production-grade standards.

## Stack

Python 3.14 · FastAPI · SQLAlchemy 2.0 async · PostgreSQL 16 · Alembic · Pydantic v2 · uv · Podman

## Commands

```bash
# Install dependencies (local .venv)
uv sync --all-extras

# Run app locally (single worker)
uv run uvicorn employee_onboarding_app.main:app --reload

# Run with containers (app + postgres, 4 workers)
podman-compose up

# Apply DB migrations
uv run alembic upgrade head

# Lint
uv run ruff check src/ tests/

# Tests (unit only — requires DB containers running)
uv run pytest tests/ -v
```

## Directory Map

```
src/employee_onboarding_app/
  main.py          — App factory, middleware stack (X-Request-ID, auth, idempotency), lifespan, health
  config.py        — All settings via pydantic-settings + DB pool config
  database.py      — Async engine (pool_size=10, max_overflow=20, pool_pre_ping=True), session factory
  models.py        — ORM models + enums + VALID_TRANSITIONS dict
  schemas.py       — All Pydantic request/response schemas
  api/deps.py      — Auth (X-API-Key → role) + async DB session dependency
  api/employees.py — /employees routes + CSV import
  api/skills.py    — /skills routes
  api/workflows.py — /workflows + /batches routes
  workflow/base.py       — WorkflowStep ABC + WorkflowContext dataclass + AlertService ABC
  workflow/orchestrator.py — Saga runner: forward execute + reverse compensate
  workflow/steps.py      — EmployeeActivationStep, ITProvisioningStep, SecurityAccessStep
  workflow/alert.py      — StructuredLogAdapter (AlertService implementation)
  services/ingestion.py  — CSV parse, validate, normalize, INSERT before saga, batch lifecycle
tests/             — Unit tests only (pytest + pytest-asyncio)
samples/           — Sample CSV test datasets (valid_employees.csv, failure_simulation.csv)
docs/              — Full design reference (read ARCHITECTURE.md before making changes)
alembic/           — Migration versions (auto-generated; do not edit manually)
```

## Critical Design Points

- **Ingestion/Saga separation**: Employee row + skills are committed to DB by ingestion service BEFORE the saga runs. The saga only handles status transitions + external service calls.
- **Saga steps are idempotent**: Step 1 (Employee Activation) uses SELECT FOR UPDATE + status guard, not INSERT.
- **Retry is always from Step 1**: Compensation cleans external state; retry finds employee in PENDING status, activates cleanly. No data duplication.
- **Race condition guard**: Partial unique index `UNIQUE(employee_id) WHERE status NOT IN (COMPLETED, ROLLED_BACK, FAILED)` prevents two active workflows per employee at DB level.
- **WorkflowStep is an ABC**: Add new steps by implementing the ABC. Zero orchestrator changes needed (Open/Closed Principle).
- **AlertService is an ABC**: StructuredLogAdapter is the impl. Swap for Slack/SNS without changing call sites.

## Authentication

`X-API-Key` header required on all endpoints except `/health` and `/ready`.

| Key Env Var | Role | Access |
|---|---|---|
| `API_KEY_ADMIN` | admin | All endpoints |
| `API_KEY_HR` | hr_manager | Import, assign skills, retry workflows |
| `API_KEY_VIEWER` | viewer | GET + health only |

## Failure Simulation (Dev/Test)

Pass header `X-Simulate-Failure: it` or `X-Simulate-Failure: security` on any onboarding trigger.

## Hard Rules

- **Never** commit or push to git — user does this manually.
- **Never** write code on `main`/`master` — always on `feature/core-implementation` branch.
- **Never** use `from app.X import` — package is `employee_onboarding_app`.
- **Never** edit files in `alembic/versions/` manually — use `alembic revision --autogenerate`.
- **Never** hardcode secrets — all config via `.env` + pydantic-settings.
- **Never** delete the employee row in saga compensation — set status=FAILED, preserve data.
- **Never** bypass VALID_TRANSITIONS — all status changes go through the transition validator.

## Full Reference

Complete architecture, ADRs, data model, saga design, retry pattern: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
