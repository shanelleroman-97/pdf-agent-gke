FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

ENV UV_SYSTEM_PYTHON=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --no-cache -r pyproject.toml

COPY main.py ./

USER 1000
# Cloud Run injects PORT.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers"]
