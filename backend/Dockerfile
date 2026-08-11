FROM ghcr.io/astral-sh/uv:0.9.28-python3.13-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY app ./app
COPY scripts ./scripts

RUN addgroup --system --gid 10001 expenseguard \
    && adduser --system --uid 10001 --ingroup expenseguard --home /nonexistent expenseguard \
    && mkdir -p /data/private \
    && chown -R expenseguard:expenseguard /app /data/private

USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"]

CMD ["sh", "-c", "alembic upgrade head && exec python -m app"]
