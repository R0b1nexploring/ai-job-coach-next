FROM ghcr.io/astral-sh/uv:0.11.32 AS uv
FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    COACH_DATA_DIR=/data \
    COACH_JOURNAL_DIR=/journal \
    COACH_FONT_PATH=/usr/share/fonts/truetype/wqy/wqy-microhei.ttc

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei fontconfig poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 coach \
    && useradd --uid 10001 --gid coach --create-home coach \
    && mkdir -p /app /data /journal \
    && chown -R coach:coach /app /data /journal

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

ENV PATH=/app/.venv/bin:$PATH
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "scripts/container_entrypoint.py"]
CMD ["api"]
