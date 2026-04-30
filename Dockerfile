FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Auto-discover instrumentations for every library in requirements.txt
# (per SigNoz's Bedrock guide).
RUN opentelemetry-bootstrap --action=install

COPY app/ .

# Create non-root user, uploads directory, and assign ownership in one layer
RUN useradd -m appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app/uploads

USER appuser

EXPOSE 8000

CMD ["opentelemetry-instrument", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
