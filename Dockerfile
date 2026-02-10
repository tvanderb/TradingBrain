FROM python:3.12-slim

WORKDIR /app

# System deps (gcc needed for some Python C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY src/ src/
COPY strategy/ strategy/
COPY statistics/ statistics/
COPY config/ config/

# Create runtime directories and non-root user
RUN groupadd -r brain && useradd -r -g brain -d /app brain && \
    mkdir -p data reports && \
    chown -R brain:brain /app

# Drop to non-root user
USER brain

# Environment
ENV PYTHONUNBUFFERED=1
ENV JSON_LOGS=1

ENTRYPOINT ["python", "-m", "src.main"]
