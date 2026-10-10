# syntax=docker/dockerfile:1

# ---- base: Python + app dependencies --------------------------------------
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The geo wheels (pyogrio, pyproj, shapely) bundle GDAL/PROJ/GEOS,
# so no apt packages are needed.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Writable by the app user: uploaded files and the SQLite database. Named
# volumes mounted here start out with this ownership.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/uploads /app/data \
    && chown appuser:appuser /app/uploads /app/data

COPY app ./app

# ---- dev: adds test tooling; used by `docker compose run --rm tests` -------
FROM base AS dev
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
USER appuser
# The app dir is read-only for appuser, so skip pytest's cache folder.
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
CMD ["pytest"]

# ---- runtime: the API image (default target) ------------------------------
FROM base AS runtime
USER appuser
# Hosts like Render pass the port to listen on in $PORT; 8000 otherwise.
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
# Shell form so $PORT is expanded; exec keeps uvicorn as PID 1 to receive stop signals.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"$PORT\""]
