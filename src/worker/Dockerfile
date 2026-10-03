# Sandbox image for one PDF-analysis session. Runs under gVisor with no GCP
# identity and egress limited to api.anthropic.com (see deploy/base).
FROM python:3.12-slim-bookworm

# The SDK worker requires /bin/bash at that exact path.
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      poppler-utils \
      qpdf \
      tesseract-ocr \
      tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd --create-home --uid 1000 worker \
    && mkdir -p /workspace \
    && chown -R worker:worker /workspace

COPY --chown=root:root entrypoint.py /usr/local/bin/entrypoint.py

# Root filesystem is read-only at runtime; point caches at the /tmp emptyDir.
ENV HOME=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER worker
WORKDIR /workspace
ENTRYPOINT ["python3", "/usr/local/bin/entrypoint.py"]
