FROM python:3.11-slim

WORKDIR /app

# System dependencies for asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (cached layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY app/ app/

# Copy Alembic config and migrations for release_command
COPY alembic.ini .
COPY migrations/ migrations/

# Non-root user for production
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
