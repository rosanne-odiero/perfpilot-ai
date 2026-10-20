# PerfPilot runtime sandbox image (ADR-004).
#
# Runs untrusted repository code for Runtime Audits. The worker starts it with
# --network none --read-only --cap-drop ALL --security-opt no-new-privileges
# --user 65534:65534 and CPU/memory/pids limits (see backend/sandbox/docker_backend.py).
# The image only provides the interpreter and measurement tooling; repository code is
# bind-mounted read-only at /workspace, dependencies (if approved) at /deps, and the
# trusted profiling harness under /perfpilot.
#
# Build: make sandbox-image
FROM python:3.15-slim

ARG PYTEST_VERSION=9.1.1
ARG PYTEST_BENCHMARK_VERSION=5.3.0
ARG PSUTIL_VERSION=7.2.2
# Optional package mirror for networks that cannot reach PyPI directly (build-time only).
ARG PIP_INDEX_URL=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

# Wheels only: no compilers are installed or needed. Setuid/setgid bits are stripped so
# no binary in the image can regain privileges (no-new-privileges is also set at run time).
RUN python -m pip install --no-cache-dir --only-binary=:all: \
        ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
        "pytest==${PYTEST_VERSION}" \
        "pytest-benchmark==${PYTEST_BENCHMARK_VERSION}" \
        "psutil==${PSUTIL_VERSION}" \
    && mkdir -p /workspace /out /deps /perfpilot \
    && chmod 0755 /workspace /deps /perfpilot \
    && chmod 0777 /out \
    && { find / -xdev -type f -perm /6000 -exec chmod a-s {} + 2>/dev/null || true; }

LABEL org.opencontainers.image.title="perfpilot-sandbox" \
      org.opencontainers.image.description="Isolated runtime for PerfPilot Runtime Audits"

USER 65534:65534
WORKDIR /workspace
CMD ["python", "--version"]
