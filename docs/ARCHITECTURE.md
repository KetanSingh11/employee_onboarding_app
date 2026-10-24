# employee-onboarding-app — Architecture Document

## 1. System Overview

A multi-step employee onboarding workflow engine with REST API, CSV batch ingestion, skill management, and saga-based consistency guarantees. The system is designed as a production-oriented, containerized Python service reflecting senior-level engineering practices: clean separation of concerns, SOLID principles, async-first DB access, race-condition-safe state transitions, and operational observability hooks.

---

## 2. Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.14+ | Required; uv-managed venv locally, raw system Python in container |
| Framework | FastAPI | Required; async-native, OpenAPI/Swagger built-in |
| ORM | SQLAlchemy 2.0 (async) | Required; declarative models, async sessions, migration support |
| DB | PostgreSQL 16 (container) | Required; ENUM types, JSONB, partial indexes, FOR UPDATE locking |
| Migrations | Alembic | Standard SQLAlchemy companion; async env.py |
| Validation | Pydantic v2 | FastAPI-native; strict schema enforcement |
| Settings | pydantic-settings | Typed `.env` config loading |
| Package Manager | uv | Required |
| Containerization | Podman + podman-compose | Required; docker-compose.yml is podman-compose compatible |
| Testing | pytest + pytest-asyncio + httpx | Unit tests only; asyncio_mode=auto |
| CI | GitHub Actions | Lint (ruff) + pytest pipeline |

---

## 3. Architecture Decision Records (ADRs)

| # | Decision | Choice | Reason |
|---|---|---|---|
| ADR-01 | Database | PostgreSQL 16 (separate container) | ENUM/JSONB/partial index/FOR UPDATE support |
| ADR-02 | Workflow execution | In-process Saga via `BackgroundTasks` | No broker needed; DB is source of truth |
| ADR-03 | Authentication | `X-API-Key` header → role | Evaluator-friendly; Swagger UI compatible |
| ADR-04 | CSV ingestion response | Immediate 202 + `batch_id`; poll to check | Non-blocking; realistic async API design |
| ADR-05 | CSV processing | Validate all rows first → execute valid rows sequentially | Selective; no full-reject on single bad row |
| ADR-06 | Skill auto-creation | Auto-create in `skills` table on first encounter | No pre-seeding; normalized to lowercase |
| ADR-07 | Proficiency levels | Fixed Enum: `beginner/intermediate/advanced/expert` | No lookup table; strict validation |
| ADR-08 | Department | Free text, `.strip()` normalized | Lean model; no lookup table |
| ADR-09 | Idempotency | `Idempotency-Key` header; scoped to CSV import + offboard | Prevents duplicate workflow triggers |
| ADR-10 | Deboarding | Skeletal placeholder; same saga infra | Scope-limited for now; fully extensible |
| ADR-11 | Failure simulation | `X-Simulate-Failure: it` or `security` header | Swagger/curl testable; deterministic |
| ADR-12 | Text normalization | Email + skill name: lowercase; names: title case | DB-level uniqueness correctness |
| ADR-13 | `start_date` validation | Must be today or future; past dates rejected | Business rule enforced at ingest layer |
| ADR-14 | Package layout | `src/` layout (`src/employee_onboarding_app/`) | uv default; modern Python packaging |
| ADR-15 | Test DB | Separate named DB in same PostgreSQL container | Realistic; no SQLite override |
| ADR-16 | Background task durability | `BackgroundTasks` + startup recovery for orphaned `PROCESSING` batch_runs | No Redis needed; PostgreSQL-as-queue; upgrade path to `arq` noted |
| ADR-17 | Retry pattern | `POST /workflows/{id}/retry` → 202 + new `workflow_id`; BackgroundTask runs full saga from Step 1 | Fire-and-forget; no duplication (see §8); full audit trail preserved |
| ADR-18 | Workflow step design | `WorkflowStep` ABC + typed `WorkflowContext` dataclass | OCP: add steps without touching orchestrator; typed contracts between steps |
| ADR-19 | Active workflow race condition | Partial unique index: `UNIQUE(employee_id) WHERE status NOT IN (COMPLETED, ROLLED_BACK, FAILED)` | DB-enforced; one active workflow per employee |
| ADR-20 | Request correlation | `X-Request-ID` middleware; generate if absent; echo in response; attach to log context | Traceability; operational standard |
| ADR-21 | Employee status transitions | `VALID_TRANSITIONS` dict enforced in service layer; invalid transitions raise `InvalidStateTransitionError` | Prevents illegal state jumps |
| ADR-22 | Router SRP | `api/workflows.py` = `/workflows` + `/batches` (separate from `skills.py`) | Single Responsibility; clean router boundaries |
| ADR-23 | Health endpoints | `GET /health` (liveness) + `GET /ready` (DB connectivity check) | Production standard; Docker health check target |
| ADR-24 | DB connection pool | `pool_size=10, max_overflow=20, pool_pre_ping=True`; configurable via `.env` | Prevents stale connections; supports 4-worker deployment |
| ADR-25 | Multi-worker | `--workers 4` in compose; concurrency safety is entirely at DB layer | Real concurrency; no shared mutable in-process state |
| ADR-26 | Ingestion/saga separation | Employee row + skills written by ingestion service BEFORE saga starts | Idempotent retry; no data duplication on saga failure |
| ADR-27 | AlertService design | ABC with `StructuredLogAdapter` implementation | Swap to Slack/SNS adapter without changing call sites |
| ADR-28 | External service boundary | IT and Security steps are simulated adapters to hypothetical external systems | Out of scope as real systems; mocked in `steps.py` |
| ADR-29 | No pub-sub | BackgroundTasks is sufficient; direct calls correct for monolith | RabbitMQ/Kafka only needed for multi-service fan-out; not applicable here |
| ADR-30 | Database query pushdown | Skill, level, and pagination evaluated via SQL subquery and LIMIT/OFFSET | Prevents OOM crashes on 1M+ rows; index-backed performance |
| ADR-31 | Concurrent race-safety | Savepoints (`begin_nested`) & `IntegrityError` catches on concurrent CSV rows | Graceful deduplication without crashing background batches |

---

## 4. System Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                REST Clients (Swagger / curl / Postman)           │
└──────────────────────────┬───────────────────────────────────────┘
                           │  HTTP + X-API-Key + X-Request-ID
┌──────────────────────────▼───────────────────────────────────────┐
│                     FastAPI App (4 workers)                      │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ Middleware Stack                                           │  │
│  │  1. X-Request-ID (generate/propagate correlation ID)      │  │
│  │  2. Auth (X-API-Key → role)                               │  │
│  │  3. Idempotency (scoped to 2 endpoints)                   │  │
│  └──────────────────────┬─────────────────────────────────────┘  │
│                         │                                         │
│  ┌──────────────────────▼─────────────────────────────────────┐  │
│  │ Routers                                                    │  │
│  │  /health /ready          ← liveness + readiness           │  │
│  │  /api/v1/employees       ← import (CSV), list, get        │  │
│  │  /api/v1/skills          ← catalog, assign, update level  │  │
│  │  /api/v1/workflows       ← status, retry                  │  │
│  │  /api/v1/batches         ← poll ingestion status          │  │
│  └──────────┬────────────────────┬──────────────────────────┘  │
│             │                    │                               │
│  ┌──────────▼──────────┐  ┌──────▼──────────────────────────┐  │
│  │ Ingestion Service   │  │ Workflow Orchestrator (Saga)     │  │
│  │                     │  │                                  │  │
│  │ 1. Parse + validate │  │ Steps: list[WorkflowStep]        │  │
│  │ 2. Normalize        │  │   Step 1: Employee Activation    │  │
│  │ 3. INSERT employee  │  │   Step 2: IT Provisioning        │  │
│  │    + skills (BEFORE │  │   Step 3: Security & Access      │  │
│  │    saga)            │  │                                  │  │
│  │ 4. Trigger saga via │  │ On failure: compensate reverse   │  │
│  │    BackgroundTask   │  │ AlertService on each transition  │  │
│  └─────────────────────┘  └──────────────────────────────────┘  │
└──────────────────────────────────┬───────────────────────────────┘
                                   │ SQLAlchemy AsyncSession
┌──────────────────────────────────▼───────────────────────────────┐
│                      PostgreSQL 16 (Container)                   │
│  employees · skills · employee_skill_map                        │
│  workflow_executions · workflow_audit_logs                       │
│  batch_runs · idempotency_keys                                   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 5. Layered Request Flow

```
HTTP Request
  │
  ▼ Middleware (Request-ID → Auth → Idempotency check)
  │
  ▼ Router (validate HTTP input, resolve dependencies)
  │
  ├──► [For CSV import]
  │      Ingestion Service:
  │        1. Validate CSV headers → reject entire file if missing required columns
  │        2. Per-row Pydantic validation → collect errors, do not abort
  │        3. Normalize text (email lowercase, skills lowercase, names title case)
  │        4. Check email uniqueness in DB → skip duplicates
  │        5. INSERT employee (status=PENDING) + skill records → committed
  │        6. Return 202 + batch_id immediately
  │        7. BackgroundTask: for each valid employee → trigger Saga
  │
  └──► [For saga trigger / retry]
         Orchestrator:
           FOR each WorkflowStep in steps:
             1. Log STARTED to workflow_audit_logs (same session)
             2. step.execute(context)
             3. Log SUCCESS
             4. Store step output in WorkflowContext
           ON failure at step N:
             FOR each completed step in reverse:
               step.compensate(context)
             employee.status = FAILED
             emit AlertService.error(...)
```

---

## 6. Data Model

### Enums

```
EmployeeStatus:     PENDING | ONBOARDING | ACTIVE | FAILED |
                    OFFBOARDING | INACTIVE | OFFBOARDING_FAILED

WorkflowType:       ONBOARDING | OFFBOARDING

WorkflowStatus:     PENDING | IN_PROGRESS | COMPLETED |
                    FAILED | ROLLING_BACK | ROLLED_BACK

StepDirection:      FORWARD | COMPENSATE

StepStatus:         STARTED | SUCCESS | FAILED

ProficiencyLevel:   BEGINNER | INTERMEDIATE | ADVANCED | EXPERT

BatchStatus:        PROCESSING | COMPLETED | PARTIALLY_COMPLETED | FAILED
```

### Valid Employee Status Transitions (enforced in service layer)

```python
VALID_TRANSITIONS = {
    PENDING:             {ONBOARDING},
    ONBOARDING:          {ACTIVE, FAILED},
    ACTIVE:              {OFFBOARDING},
    FAILED:              {ONBOARDING},        # on retry
    OFFBOARDING:         {INACTIVE, OFFBOARDING_FAILED},
    OFFBOARDING_FAILED:  {OFFBOARDING},       # on retry
}
```

### Table Definitions

```
employees
  id              UUID        PK
  email           VARCHAR     UNIQUE NOT NULL   -- lower + strip at service layer
  first_name      VARCHAR     NOT NULL          -- title case
  last_name       VARCHAR     NOT NULL          -- title case
  department      VARCHAR                       -- stripped
  job_title       VARCHAR                       -- stripped
  start_date      DATE        NOT NULL          -- must be today or future
  status          ENUM(EmployeeStatus)  DEFAULT PENDING
  created_at      TIMESTAMPTZ NOT NULL  DEFAULT now()
  updated_at      TIMESTAMPTZ NOT NULL  DEFAULT now()

skills
  id              UUID        PK
  name            VARCHAR     UNIQUE NOT NULL   -- lower + strip
  created_at      TIMESTAMPTZ NOT NULL  DEFAULT now()
  updated_at      TIMESTAMPTZ NOT NULL  DEFAULT now()

employee_skill_map
  id              UUID        PK
  employee_id     UUID        FK → employees.id  ON DELETE CASCADE
  skill_id        UUID        FK → skills.id
  proficiency_level  ENUM(ProficiencyLevel) NOT NULL
  UNIQUE(employee_id, skill_id)    -- one proficiency level per skill per employee
  created_at      TIMESTAMPTZ NOT NULL  DEFAULT now()
  updated_at      TIMESTAMPTZ NOT NULL  DEFAULT now()

workflow_executions
  id              UUID        PK
  employee_id     UUID        FK → employees.id
  batch_id        UUID        FK → batch_runs.id  NULLABLE
  workflow_type   ENUM(WorkflowType)
  status          ENUM(WorkflowStatus) DEFAULT PENDING
  current_step    INT         DEFAULT 0  -- tracks step at failure for audit
  failure_reason  TEXT        NULLABLE
  created_at      TIMESTAMPTZ NOT NULL  DEFAULT now()
  updated_at      TIMESTAMPTZ NOT NULL  DEFAULT now()

  INDEX: UNIQUE(employee_id) WHERE status NOT IN ('COMPLETED','ROLLED_BACK','FAILED')
  -- Partial unique index: enforces max one active workflow per employee at DB level

workflow_audit_logs
  id              UUID        PK
  workflow_id     UUID        FK → workflow_executions.id
  step_number     INT         NOT NULL
  step_name       VARCHAR     NOT NULL
  direction       ENUM(StepDirection)
  status          ENUM(StepStatus)
  payload         JSONB       -- contextual snapshot at time of step
  error_detail    TEXT        NULLABLE
  created_at      TIMESTAMPTZ NOT NULL  DEFAULT now()
  updated_at      TIMESTAMPTZ NOT NULL  DEFAULT now()

batch_runs
  id              UUID        PK
  filename        VARCHAR
  status          ENUM(BatchStatus) DEFAULT PROCESSING
  total_rows      INT         DEFAULT 0
  valid_rows      INT         DEFAULT 0
  skipped_invalid INT         DEFAULT 0
  skipped_duplicate INT       DEFAULT 0
  triggered_onboarding INT    DEFAULT 0
  report          JSONB       -- [{row, email, reason}] for failed/skipped rows
  created_at      TIMESTAMPTZ NOT NULL  DEFAULT now()
  updated_at      TIMESTAMPTZ NOT NULL  DEFAULT now()

idempotency_keys
  id              UUID        PK
  key             VARCHAR     UNIQUE NOT NULL
  endpoint        VARCHAR     NOT NULL
  response_status INT
  response_body   JSONB
  expires_at      TIMESTAMPTZ NOT NULL  -- 24h TTL
  created_at      TIMESTAMPTZ NOT NULL  DEFAULT now()
  updated_at      TIMESTAMPTZ NOT NULL  DEFAULT now()
```

---

## 7. CSV Format Specification

```
email,first_name,last_name,department,job_title,start_date,skills
jane.doe@corp.com,Jane,Doe,Engineering,Software Engineer,2026-11-01,"Python:expert;FastAPI:intermediate;Docker:beginner"
bob.smith@corp.com,Bob,Smith,HR,HR Manager,2026-11-15,"Excel:advanced"
```

**Required columns:** `email, first_name, last_name, start_date`
**Optional columns:** `department, job_title, skills`

**Ingestion rules (applied in order):**
1. Validate header row → missing required column = reject entire file immediately (HTTP 422)
2. Extra/unknown columns → silently dropped (logged at DEBUG)
3. Per-row Pydantic validation → invalid rows collected, not aborted
4. `start_date` must be ≥ today → row rejected if past date
5. Email uniqueness vs DB → duplicate = `skipped_duplicate`
6. Skills token format: `name:level` pairs separated by `;` → malformed = row rejected
7. Proficiency level must match enum → invalid = row rejected
8. Employee row + skill rows committed to DB before saga starts
9. Saga triggered per valid employee via BackgroundTask

---

## 8. Onboarding Workflow (Saga Pattern)

### Separation of Concerns: Ingestion vs Saga

```
Ingestion Service                    Saga Orchestrator
─────────────────────────────────    ───────────────────────────────────────
INSERT employee (status=PENDING)  →  Step 1: Activate  (PENDING → ONBOARDING)
INSERT employee_skill_map rows       Step 2: IT Provisioning  (external mock)
COMMIT                               Step 3: Security & Access (external mock)
                                     → ACTIVE on full success
```

**Why this separation:** Employee data and skills are durable from the moment of valid ingestion. The saga only manages state transitions and external system calls — all of which are reversible. This makes retries completely safe with zero data duplication.

### Step Definitions

| Step | Name | Forward Action | Compensation |
|---|---|---|---|
| 1 | Employee Activation | `SELECT FOR UPDATE` on employee; validate status in {PENDING, FAILED}; UPDATE → ONBOARDING; create `workflow_executions` record | UPDATE employee → FAILED; mark workflow ROLLED_BACK |
| 2 | IT Provisioning | Simulate HTTP call to IT system; store `it_account_id` in `WorkflowContext` | Simulate deprovision call using `it_account_id` from context |
| 3 | Security & Access | Simulate HTTP call to security system; store `security_profile_id` in context | Simulate revoke call using `security_profile_id` from context |

### Compensation Flow

```
Step N fails
  │
  ├── Log FAILED to workflow_audit_logs
  ├── Set workflow_executions.status = ROLLING_BACK
  │
  └── FOR step in [completed steps in reverse]:
        step.compensate(context)
        log COMPENSATE result
  │
  └── Set employee.status = FAILED
      Set workflow_executions.status = ROLLED_BACK
      AlertService.error("Onboarding failed at step N for {email}: {reason}")
```

### Each Audit Log Write = Same DB Transaction as Its Business Operation

Audit log row and the business state change (employee status update, workflow status update) are written in a **single atomic transaction**. If the DB write fails, both fail together — no partial audit trail, no phantom transitions.

### Failure Simulation

| Header value | Effect |
|---|---|
| `X-Simulate-Failure: it` | IT Provisioning step raises `ExternalServiceError` |
| `X-Simulate-Failure: security` | Security step raises `ExternalServiceError` |
| _(absent)_ | Normal success flow |

---

## 9. Retry / Resume Pattern

```
POST /api/v1/workflows/{id}/retry
  │
  ├── Guard: workflow.status must be FAILED or ROLLED_BACK
  ├── Guard: no other active workflow for employee (partial unique index enforcement)
  ├── Reset: employee.status → PENDING  (valid transition: FAILED → PENDING → ONBOARDING)
  ├── Create: new workflow_executions record (old record preserved as audit history)
  ├── Return: 202 Accepted + {"new_workflow_id": "..."}
  │
  └── BackgroundTask: run full saga from Step 1 using existing employee data
        Step 1: finds employee in PENDING status → activates (no INSERT, no duplication)
        Step 2: provisions fresh IT (compensation cleaned up old accounts)
        Step 3: grants fresh security access
```

**Why always from Step 1:** After full compensation, all external state is clean. The employee record and skills persist safely. Step 1 is an idempotent status update — finding PENDING is the expected state on retry. Resuming from Step 2 would skip re-provisioning IT accounts that were deprovisioned.

**Fire-and-forget semantics:** Client receives 202 immediately, polls `GET /workflows/{new_workflow_id}` for result. Same polling pattern as initial onboarding.

---

## 10. WorkflowStep Design (SOLID — OCP)

```python
# workflow/base.py

@dataclass
class WorkflowContext:
    employee_id: UUID
    employee_email: str
    workflow_id: UUID
    batch_id: UUID | None = None
    simulate_failure: str | None = None
    # Populated progressively as steps succeed:
    it_account_id: str | None = None        # set by Step 2, used by Step 2 compensation
    security_profile_id: str | None = None  # set by Step 3, used by Step 3 compensation

class WorkflowStep(ABC):
    step_number: int
    step_name: str

    @abstractmethod
    async def execute(self, session: AsyncSession, ctx: WorkflowContext) -> WorkflowContext: ...

    @abstractmethod
    async def compensate(self, session: AsyncSession, ctx: WorkflowContext) -> None: ...
```

Adding Step 4 (e.g., `PayrollSetupStep`, `WelcomeEmailStep`) = one new class, zero orchestrator changes.

```python
# workflow/alert.py

class AlertService(ABC):
    @abstractmethod
    def info(self, message: str, context: dict) -> None: ...
    @abstractmethod
    def error(self, message: str, context: dict) -> None: ...

class StructuredLogAdapter(AlertService):
    """Emits structured JSON log lines. Swap for SlackAdapter/SNSAdapter without changing call sites."""
    ...
```

---

## 11. Concurrency & Race Condition Handling

| Scenario | Guard | Mechanism |
|---|---|---|
| Two requests create same employee simultaneously | Unique constraint on `employees.email` | DB rejects second INSERT; service catches `UniqueViolation` → `skipped_duplicate` |
| Two workflows start for same employee simultaneously | Partial unique index on `workflow_executions` | DB rejects second active workflow; service catches → HTTP 409 |
| Employee status updated while saga runs | `SELECT FOR UPDATE` in Step 1 | Row-level lock; second request blocks until first commits or rolls back |
| Two retries for same failed workflow | Partial unique index (same as above) | Only one new active workflow allowed per employee |

---

## 12. API Surface

> Base path: `/api/v1` | Auth: `X-API-Key` header on all endpoints

### Health (no auth)
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness — always returns 200 if app is up |
| `GET` | `/ready` | Readiness — checks DB connectivity; 503 if DB unreachable |

### Employees
| Method | Path | Role | Description |
|---|---|---|---|
| `POST` | `/employees/import` | `hr_manager` | Upload CSV; 202 + `batch_id` |
| `GET` | `/employees` | `viewer` | List; filter: `?skill=&min_proficiency=&department=&status=` |
| `GET` | `/employees/{id}` | `viewer` | Detail with skills |
| `POST` | `/employees/{id}/offboard` | `admin` | **[SKELETAL PLACEHOLDER]** |

### Skills
| Method | Path | Role | Description |
|---|---|---|---|
| `GET` | `/skills` | `viewer` | Master skill catalog |
| `POST` | `/employees/{id}/skills` | `hr_manager` | Assign skill + proficiency |
| `PUT` | `/employees/{id}/skills/{skill_id}` | `hr_manager` | Update proficiency level |

### Workflows & Batches
| Method | Path | Role | Description |
|---|---|---|---|
| `GET` | `/workflows/{id}` | `viewer` | Workflow status + full audit log |
| `POST` | `/workflows/{id}/retry` | `hr_manager` | Fire-and-forget retry; 202 + new workflow_id |
| `GET` | `/batches/{id}` | `viewer` | Poll batch ingestion status + row-level report |

**Total: 12 endpoints** (10 original + `/health` + `/ready`)

---

## 13. Idempotency

Scoped to **2 endpoints**:
- `POST /employees/import`
- `POST /employees/{id}/offboard`

**Flow:**
1. Client sends `Idempotency-Key: <uuid>` header.
2. Middleware checks `idempotency_keys` table (key + endpoint).
3. Found + not expired (24h TTL) → return cached response. No re-processing.
4. Not found → process → store result → respond.
5. Missing key → accepted, not cached. Not rejected (keeps evaluator UX simple).

---

## 14. Role Definitions

| Role | Key Env Var | Access |
|---|---|---|
| `admin` | `API_KEY_ADMIN` | All endpoints |
| `hr_manager` | `API_KEY_HR` | All except admin-only offboard |
| `viewer` | `API_KEY_VIEWER` | GET + health endpoints only |

---

## 15. Startup Recovery (Orphaned Batch Runs)

On application startup (FastAPI `lifespan` event):
1. Query `batch_runs WHERE status = 'PROCESSING' AND created_at < now() - interval '10 minutes'`
2. Found → mark each as `FAILED`, `failure_reason = 'worker_crash_recovery'`
3. Emit `AlertService.error(...)` for each orphaned run
4. Operator re-triggers via retry or re-upload

This handles the BackgroundTasks durability gap: if a uvicorn worker crashes mid-processing, the stuck record is detected and surfaced on next restart.

---

## 16. Project Folder Structure

> `src/` layout. Package root: `src/employee_onboarding_app/`

```
employee_onboarding_app/                ← git repo root
├── AGENTS.md                           # Cross-agent instructions (concise)
├── pyproject.toml                      # All deps + tool config
├── Dockerfile                          # python:3.14-slim; no venv in container
├── docker-compose.yml                  # app (4 workers) + postgres; podman-compose compatible
├── .env.example                        # All required env vars
├── .python-version                     # Pins 3.14 for uv
├── .gitignore / .dockerignore
├── alembic.ini
├── alembic/
│   ├── env.py                          # Async alembic setup (run_async_migrations)
│   └── versions/                       # Auto-generated migration files
├── .github/
│   └── workflows/ci.yml                # Lint + test pipeline
├── docs/
│   └── ARCHITECTURE.md                 # This document
├── scripts/
│   └── init_db.sql                     # Creates staffplanner_test DB on first container start
└── src/
    └── employee_onboarding_app/
        ├── __init__.py                 # __version__ = "0.1.0"
        ├── main.py                     # App factory, middleware stack, lifespan, health routes
        ├── config.py                   # pydantic-settings; DB URL, API keys, pool config
        ├── database.py                 # Async engine (pool_size=10, max_overflow=20, pre_ping), session factory
        ├── models.py                   # ORM models + enums + VALID_TRANSITIONS
        ├── schemas.py                  # All Pydantic schemas (request + response)
        ├── api/
        │   ├── __init__.py
        │   ├── deps.py                 # Auth dependency + async DB session dependency
        │   ├── employees.py            # /employees routes + CSV import
        │   ├── skills.py              # /skills routes
        │   └── workflows.py           # /workflows + /batches routes
        ├── workflow/
        │   ├── __init__.py
        │   ├── base.py                 # WorkflowStep ABC + WorkflowContext dataclass + AlertService ABC
        │   ├── orchestrator.py         # Saga runner: forward execute + reverse compensate
        │   ├── steps.py               # EmployeeActivationStep, ITProvisioningStep, SecurityAccessStep
        │   └── alert.py               # StructuredLogAdapter (AlertService impl)
        └── services/
            ├── __init__.py
            └── ingestion.py           # CSV parse, validate, normalize, INSERT before saga, batch lifecycle
tests/
    ├── conftest.py
    ├── test_workflow.py
    └── test_api.py
samples/
    ├── valid_employees.csv             # 3 valid employees with skills across levels
    └── failure_simulation.csv          # Targeted dataset for failure simulation
```

**Core logic files: 15** | Supporting/config: ~9

---

## 17. Key Implementation Constraints

- All DB operations use `AsyncSession` (SQLAlchemy 2.0 async).
- `updated_at` auto-refreshed via SQLAlchemy `onupdate` event.
- Each saga step's audit log write is in the **same transaction** as its business operation.
- `WorkflowContext` is a typed dataclass; no raw dicts between steps.
- All `WorkflowStep.execute()` implementations must be **idempotent**.
- `AlertService` is an ABC; `StructuredLogAdapter` is the only concrete implementation in this scope.
- `X-Request-ID` is propagated into Python `contextvars` for structured log correlation.
- `ruff` for linting (`line-length=100`, `src` layout aware).
- `pytest-asyncio` with `asyncio_mode = auto`.
- Never commit on `main`/`master`. Never `git push` manually — user controls all commits.
- Never hardcode secrets; all config via `.env` + `pydantic-settings`.
