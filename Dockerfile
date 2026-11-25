FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

# /data/attachments is the mount point of the attachments volume (BE-33). It is created here,
# owned by appuser, because a new named volume copies the owner of the folder it covers.
# events/ holds the event cover images (R-12); on an existing volume the API creates it.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/attachments/events \
    && chown -R appuser /app /data
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
