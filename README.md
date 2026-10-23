# Staff & Skill Planning Tool — Employee Onboarding Service

Production-grade, containerized backend service coordinating a multi-step employee onboarding workflow with saga-based consistency guarantees, automatic compensation/rollback logic, CSV batch ingestion, and skill matrix filtering.


---

## 🏛️ System Architecture & Workflow

The service coordinates multi-domain onboarding touches across the internal database and simulated external enterprise services (IT Provisioning and Security & Access).

```
   ┌────────────────────────────────────────────────────────┐
   │                  FastAPI Application                   │
   │  (Middleware: X-Request-ID, Auth, Idempotency Guard)   │
   └───────────────────────────┬────────────────────────────┘
                               │
               ┌───────────────┴───────────────┐
               ▼                               ▼
     [ Ingestion Service ]           [ Saga Orchestrator ]
  • Validates CSV headers & rows   • Coordinates multi-step flow
  • Normalizes email & skills      • Atomic audit logging per step
  • Pre-persists DB record (PENDING)• Reverse compensation on error
  • Returns 202 Accepted           • Emits structured JSON alerts
               │                               │
               └───────────────┬───────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
  [ Step 1: Employee ]  [ Step 2: IT ]        [ Step 3: Security ]
  Row-lock & Activate   Mailbox, Slack,       Physical Badge, VPN,
  (PENDING -> ONBOARD)  Laptop Provisioning   IAM Permissions
        │                      │                      │
        └──────────────┬───────┴──────────────────────┘
                       │ (On failure: rollback in reverse)
                       ▼
       [( PostgreSQL 16 Storage Layer )]
  • employees (status state-machine: PENDING, ONBOARDING, ACTIVE, FAILED)
  • skills & employee_skill_map (one level per skill per employee)
  • workflow_executions & workflow_audit_logs (immutable audit trail)
  • batch_runs & idempotency_keys
```

### Key Design Highlights (ADR Summary)
* **Saga Orchestrator Pattern (ADR-02, ADR-18):** Implemented in-process without heavy external orchestrators. Steps implement the `WorkflowStep` Abstract Base Class (Open/Closed Principle), enabling extension with future steps (e.g., payroll, equipment shipping) without altering the runner.
* **Separation of Ingestion vs Saga (ADR-26):** Employee records and skills are committed before the saga runs. This prevents data duplication on retries and guarantees that skills are never lost if a downstream step fails.
* **Race Condition Guards (ADR-19, ADR-21):**
  * PostgreSQL **Partial Unique Index** (`UNIQUE(employee_id) WHERE status NOT IN ('COMPLETED', 'ROLLED_BACK', 'FAILED')`) guarantees at most one active workflow per employee at the DB level.
  * `SELECT FOR UPDATE` in Step 1 serializes concurrent activations.
  * Formal `VALID_TRANSITIONS` state machine prevents illegal status jumps.
* **Durability & Crash Recovery (ADR-16):** Application lifespan hook detects and recovers orphaned batch runs (PostgreSQL-as-Queue pattern).
* **Idempotency & Correlation (ADR-09, ADR-20):** `X-Request-ID` correlation middleware and 24h `Idempotency-Key` caching on mutating endpoints.

---

## 🚀 Quickstart

### Prerequisites
* [uv](https://docs.astral.sh/uv/) (v0.5+)
* [Podman](https://podman.io/) or Docker with Podman/Docker Compose

### 1. Environment Setup
```bash
cp .env.example .env
uv sync --all-extras
```

### 2. Start PostgreSQL Container
```bash
# Using podman-compose or docker-compose
podman-compose up postgres -d
# (or: docker compose up postgres -d)
```

### 3. Run Database Migrations
```bash
uv run alembic upgrade head
```

### 4. Start the Application Locally
```bash
uv run uvicorn employee_onboarding_app.main:app --reload --port 8000
```
Interactive API documentation is now live at: **http://localhost:8000/docs**

---

## 🔐 Authentication & RBAC

All endpoints (except `/health` and `/ready`) require the `X-API-Key` HTTP header:

| Role | Configured Secret | Permitted Actions |
|---|---|---|
| **Admin** | `admin-secret-key` | Full access, offboarding triggers |
| **HR Manager** | `hr-secret-key` | CSV import, skill assignment, workflow retry, queries |
| **Viewer** | `viewer-secret-key` | Read-only inspection (employees, skills, batch status) |

---

## 📡 API Reference & cURL Examples

### 1. Health & Readiness Probes
```bash
# Liveness probe
curl -i http://localhost:8000/health

# Readiness probe (verifies database connectivity)
curl -i http://localhost:8000/ready
```

### 2. Import Employees via CSV (202 Accepted)
```bash
cat << 'EOF' > sample_employees.csv
email,first_name,last_name,department,job_title,start_date,skills
alex.morgan@company.com,Alex,Morgan,Engineering,Cloud Architect,2026-11-01,"Kubernetes:expert;Python:advanced;Terraform:intermediate"
sarah.connor@company.com,Sarah,Connor,Security,SecOps Lead,2026-11-15,"AWS:expert;Docker:advanced"
EOF

curl -i -X POST "http://localhost:8000/api/v1/employees/import" \
  -H "X-API-Key: hr-secret-key" \
  -H "Idempotency-Key: batch-001" \
  -F "file=@sample_employees.csv"
```

### 3. Poll Batch Ingestion Progress
```bash
curl -i "http://localhost:8000/api/v1/batches/<BATCH_ID>" \
  -H "X-API-Key: viewer-secret-key"
```

### 4. Search & Filter Employees by Skill and Minimum Level
```bash
# Find everyone who knows Kubernetes at an advanced level or higher
curl -i "http://localhost:8000/api/v1/employees?skill=kubernetes&min_proficiency=advanced" \
  -H "X-API-Key: viewer-secret-key"

# Filter by department and status
curl -i "http://localhost:8000/api/v1/employees?department=Engineering&status=ACTIVE" \
  -H "X-API-Key: viewer-secret-key"
```

### 5. Assign or Update Skills
```bash
# Assign a new skill
curl -i -X POST "http://localhost:8000/api/v1/employees/<EMPLOYEE_ID>/skills" \
  -H "X-API-Key: hr-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"skill_name": "FastAPI", "proficiency_level": "advanced"}'

# Update proficiency level
curl -i -X PUT "http://localhost:8000/api/v1/employees/<EMPLOYEE_ID>/skills/<SKILL_ID>" \
  -H "X-API-Key: hr-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"proficiency_level": "expert"}'
```

### 6. Inspect Workflow Audit Trail
```bash
curl -i "http://localhost:8000/api/v1/workflows/<WORKFLOW_ID>" \
  -H "X-API-Key: viewer-secret-key"
```

---

## 🧪 Failure Simulation & Automatic Rollback Demo

To demonstrate automatic compensation and rollback in action, pass the `X-Simulate-Failure` header:

```bash
# Simulates a failure during IT Provisioning (Step 2)
# IT provisioning fails -> Employee Activation is compensated (status -> FAILED)
curl -i -X POST "http://localhost:8000/api/v1/employees/import" \
  -H "X-API-Key: hr-secret-key" \
  -H "X-Simulate-Failure: it" \
  -F "file=@sample_employees.csv"

# Simulates a failure during Security Access (Step 3)
# Security fails -> IT accounts deprovisioned -> DB record set to FAILED
curl -i -X POST "http://localhost:8000/api/v1/employees/import" \
  -H "X-API-Key: hr-secret-key" \
  -H "X-Simulate-Failure: security" \
  -F "file=@sample_employees.csv"
```

### Retrying a Failed Workflow (Fire-and-forget, 202 Accepted)
```bash
curl -i -X POST "http://localhost:8000/api/v1/workflows/<WORKFLOW_ID>/retry" \
  -H "X-API-Key: hr-secret-key"
```

---

## 🧪 Running Automated Tests

```bash
# Execute unit and workflow saga test suite
uv run pytest tests/ -v
```

