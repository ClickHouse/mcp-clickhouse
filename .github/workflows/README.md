# GitHub Actions CI/CD

This directory contains GitHub Actions workflows for automated testing and linting.

## Workflows

### 🧪 Tests (`test.yml`)

Runs the full test suite against multiple Python versions.

**Trigger:**
- Push to `main`, `master`, or `develop` branches
- Pull requests to these branches
- Manual trigger via workflow_dispatch

**What it does:**
1. Tests against Python 3.10, 3.11, and 3.12
2. Starts required services:
   - MyScaleDB (using ClickHouse image)
   - PostgreSQL with pgvector extension
3. Runs all unit tests in `tests/` directory
4. Uploads test results as artifacts

**Services:**
- **MyScaleDB**: `localhost:8123` (HTTP), `localhost:9000` (Native)
- **PostgreSQL**: `localhost:5432`

### 🔍 Lint (`lint.yml`)

Runs code quality checks using Ruff.

**Trigger:**
- Push to `main`, `master`, or `develop` branches
- Pull requests to these branches
- Manual trigger via workflow_dispatch

**What it does:**
1. Checks code style with `ruff check`
2. Checks code formatting with `ruff format --check`

## Running Tests Locally

To run the same tests locally:

```bash
# Install dependencies
uv sync --all-extras --dev

# Start test services
docker compose -f test-services/docker-compose.yml up -d

# Run all tests
uv run pytest tests/ -v

# Run specific test suites
uv run pytest tests/test_tool.py -v          # MyScaleDB tests
uv run pytest tests/test_chdb_tool.py -v     # chDB tests
uv run pytest tests/test_pgvector_tool.py -v # pgvector tests

# Run linting
uv run ruff check .
uv run ruff format --check .
```

## Environment Variables

Tests use the following environment variables:

### MyScaleDB
- `MYSCALE_HOST=localhost`
- `MYSCALE_PORT=8123`
- `MYSCALE_USER=default`
- `MYSCALE_PASSWORD=clickhouse`
- `MYSCALE_SECURE=false`
- `MYSCALE_VERIFY=false`

### pgvector
- `PGVECTOR_HOST=localhost`
- `PGVECTOR_PORT=5432`
- `PGVECTOR_USER=postgres`
- `PGVECTOR_PASSWORD=postgres`
- `PGVECTOR_DATABASE=vectordb`
- `PGVECTOR_SSLMODE=disable`

### chDB
- `CHDB_ENABLED=true`
- `CHDB_DATA_PATH=:memory:`

## Adding Status Badges

Add these badges to your main README.md:

```markdown
[![Tests](https://github.com/YOUR_USERNAME/YOUR_REPO/actions/workflows/test.yml/badge.svg)](https://github.com/YOUR_USERNAME/YOUR_REPO/actions/workflows/test.yml)
[![Lint](https://github.com/YOUR_USERNAME/YOUR_REPO/actions/workflows/lint.yml/badge.svg)](https://github.com/YOUR_USERNAME/YOUR_REPO/actions/workflows/lint.yml)
```

Replace `YOUR_USERNAME` and `YOUR_REPO` with your actual GitHub username and repository name.

## Troubleshooting

### Tests fail locally but pass in CI
- Make sure your local services are running: `docker compose up -d`
- Check environment variables are set correctly
- Ensure you're using the correct Python version

### Database connection errors
- Wait a few seconds after starting services for health checks to pass
- Check service logs: `docker compose logs myscaledb` or `docker compose logs postgres`

### Dependency issues
- Update uv: `uv self update`
- Sync dependencies: `uv sync --all-extras --dev`
- Clear cache: `rm -rf .venv && uv sync --all-extras --dev`

