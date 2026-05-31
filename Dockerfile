FROM python:3.14-slim AS builder
RUN pip install uv
WORKDIR /app
COPY pyproject.toml ./
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app/ ./app/
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["python", "-m", "app.main"]
