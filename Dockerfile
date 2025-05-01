# Use a Python image with uv pre-installed
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS uv

# Install the project into `/app`
WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential liblz4-dev libzstd-dev
# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --no-editable

# Then, add the rest of the project source code and install it
# Installing separately from its dependencies allows optimal layer caching
ADD . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm

WORKDIR /app
# Copy the virtual environment
COPY --from=uv --chown=app:app /app/.venv /app/.venv
# Copy the uv executable
COPY --from=uv /usr/local/bin/uv /usr/local/bin/
# Copy the project files
COPY --from=uv /app /app

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:/usr/local/bin:$PATH"

# Add ClickHouse environment variables
ENV CLICKHOUSE_HOST=""
ENV CLICKHOUSE_PORT="8443"
ENV CLICKHOUSE_USER=""
ENV CLICKHOUSE_PASSWORD=""
ENV CLICKHOUSE_SECURE="true"
ENV CLICKHOUSE_VERIFY="true"
ENV CLICKHOUSE_CONNECT_TIMEOUT="30"
ENV CLICKHOUSE_SEND_RECEIVE_TIMEOUT="300"
ENV CLICKHOUSE_DATABASE=""

EXPOSE 28123
# when running the container, add --db-path and a bind mount to the host's db file
ENTRYPOINT ["uv", "run", "mcp-clickhouse-sse", "--directory", "/app"]