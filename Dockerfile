FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY constitution.py ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED="1"

EXPOSE 8080
CMD ["python", "constitution.py"]
