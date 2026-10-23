FROM python:3.14-slim

# Prevents Python from writing .pyc files and enables stdout/stderr flushing
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (layer cache optimisation)
COPY pyproject.toml .python-version ./

# Install production dependencies only (no dev extras), no venv — raw system Python
RUN uv pip install --system --no-cache -e "."

# Copy application source
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

EXPOSE 8000

CMD ["uvicorn", "employee_onboarding_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

