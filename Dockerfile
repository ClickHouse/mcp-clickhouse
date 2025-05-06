FROM python:3.13-slim AS base
WORKDIR /app

ENV CLICKHOUSE_HOST=localhost \
    CLICKHOUSE_PORT=8123 \
    CLICKHOUSE_USER=default \
    CLICKHOUSE_PASSWORD= \
    CLICKHOUSE_DATABASE=default \
    CLICKHOUSE_SECURE=false \
    CLICKHOUSE_VERIFY=true \
    TRANSPORT=sse \
    PORT=8000

FROM base AS builder
WORKDIR /app
# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY . /app
RUN pip install -e .


FROM base AS production
COPY --from=builder /app /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
ENTRYPOINT ["python", "-m", "mcp_clickhouse.main"]
CMD ["--transport", "sse", "--port", "8000"]