# syntax=docker/dockerfile:1
# AA-Booking API image. Runs as a Fargate task behind the shared ALB (target group port 8000,
# health check /booking/health — AA-CIS-Infra accounts/aa365/booking.tf).
FROM python:3.12-slim

ARG GIT_SHA=unknown
# Release version (AA-891): the deploy and CI read it from .release-please-manifest.json (outside this build context).
ARG APP_VERSION=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_SHA=${GIT_SHA} \
    APP_VERSION=${APP_VERSION}

# Security fixes the base image does not carry yet (Trivy fails the build on a fixable HIGH/CRITICAL finding):
# upgrade the Debian packages already installed, install nothing new. The deploy and the CI build pull the base tag
# (`docker build --pull`) on a fresh runner with no layer cache, so every new commit's image re-runs this layer against
# that day's Debian archive (S294: a Debian fix published after the last deploy is picked up by the next build; the
# nightly Image Scan of the running images reports the ones still on an older build).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

EXPOSE 8000

# Liveness only (no DB). python:3.12-slim has no curl, so the probe uses the stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/booking/health', timeout=4).status == 200 else 1)"]

# Keep-alive must outlive the ALB idle timeout (100 s, AA-CIS-Infra modules/alb); with uvicorn's
# 5 s default the ALB reuses a closed connection and answers 502 (CIS lesson S231, AA-741).
# AA-993 G4: on SIGTERM wait at most 5 s for open keep-alive connections, then run the lifespan shutdown at once
# (the job worker releases its running job back to the queue there), well inside the ECS stopTimeout.
# AA-1111 (S284): --no-server-header — no "server: uvicorn" for API Gateway to pass on (ZAP baseline, version leak).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "105", "--timeout-graceful-shutdown", "5", "--no-server-header"]
