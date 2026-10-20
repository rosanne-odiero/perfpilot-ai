# syntax=docker/dockerfile:1
#
# PerfPilot application image: API (uvicorn), job worker and Streamlit UI.
#
#   docker compose build                      # api + worker + ui (see docker-compose.yml)
#   docker build --target app -t perfpilot .  # API/UI image
#   docker build --target worker -t perfpilot-worker .   # adds the docker CLI for the sandbox
#
# Dependencies come from the committed uv.lock (`uv export --locked`, installed with
# --require-hashes). Networks that cannot reach PyPI can pass a mirror at build time:
#   docker compose build --build-arg UV_INDEX_URL=https://mirror.example/simple
# The mirror is a build-time setting only and is never baked into the image.

ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.3
ARG DOCKER_CLI_IMAGE=docker:28-cli

FROM ${UV_IMAGE} AS uv
FROM ${DOCKER_CLI_IMAGE} AS docker-cli

# --------------------------------------------------------------------------- dependencies
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ARG UV_INDEX_URL=
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
# --locked fails the build if uv.lock is out of date with pyproject.toml. Build args are visible
# as environment variables, so the mirror is hidden from the lock check (it would otherwise look
# like an index change) and only used for downloads.
RUN env -u UV_INDEX_URL uv export --locked --no-dev --no-emit-project --format requirements-txt \
        --output-file requirements.txt \
    && uv venv --python /usr/local/bin/python3 /opt/venv \
    && uv pip install --python /opt/venv/bin/python --require-hashes --no-deps \
        ${UV_INDEX_URL:+--index-url "$UV_INDEX_URL"} -r requirements.txt

# --------------------------------------------------------------------------- runtime (API / UI)
FROM ${PYTHON_IMAGE} AS app
ARG APP_UID=10001
ARG APP_GID=10001
# pango/harfbuzz + a font: WeasyPrint renders PDF reports in the container (xhtml2pdf is the
# fallback elsewhere). git: GitHub imports clone with a hardened, fixed argv.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 fonts-dejavu-core git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" perfpilot \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /home/perfpilot \
        --create-home --shell /usr/sbin/nologin perfpilot

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PERFPILOT_DATA_DIR=/data \
    PERFPILOT_API_BASE_URL=http://api:8000 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
COPY backend ./backend
COPY frontend ./frontend
COPY examples ./examples
COPY .streamlit ./.streamlit
COPY docker/healthcheck.py /usr/local/bin/perfpilot-healthcheck
RUN printf '#!/opt/venv/bin/python\nimport sys\nfrom backend.cli import main\nsys.exit(main())\n' \
        > /opt/venv/bin/perfpilot \
    && chmod 0755 /opt/venv/bin/perfpilot /usr/local/bin/perfpilot-healthcheck \
    && mkdir -p /data /var/lib/perfpilot/db \
    && chown "${APP_UID}:${APP_GID}" /data /var/lib/perfpilot/db \
    && python -m compileall -q /app/backend /app/frontend

USER ${APP_UID}:${APP_GID}
EXPOSE 8000 8501
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["perfpilot-healthcheck", "api"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --------------------------------------------------------------------------- worker
# Same image plus the docker CLI, which the worker uses to start sandbox containers through the
# host daemon. Only this target is ever given the Docker socket (ADR-005).
FROM app AS worker
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
ENV PERFPILOT_SANDBOX_DOCKER_PATH=/usr/local/bin/docker
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["perfpilot-healthcheck", "worker"]
CMD ["python", "-m", "backend.jobs.worker"]
