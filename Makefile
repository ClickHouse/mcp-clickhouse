# 帮助信息
.PHONY: help
help:
	@echo "可用的命令："
	@echo "开发命令："
	@echo "  install     - 安装项目依赖"
	@echo "  dev        - 启动本地开发服务器"
	@echo "  test       - 运行测试"
	@echo "  lint       - 运行代码检查"
	@echo "Docker命令："
	@echo "  docker-build  - 构建Docker镜像"
	@echo "  docker-up     - 启动Docker容器"
	@echo "  docker-down   - 停止Docker容器"
	@echo "  test-services - 启动测试服务"
	@echo "清理命令："
	@echo "  clean      - 清理临时文件和缓存"

# 开发相关命令
.PHONY: install dev test lint
install:
	uv sync --all-extras --dev

# 启动本地开发服务器
dev:
	export $(grep -v '^#' .env | xargs)
	python -m mcp_clickhouse.main --transport sse --port 8000

# 运行测试
test: test-services
	uv run pytest tests

# 代码检查
lint:
	uv run ruff check .

# Docker相关命令
.PHONY: docker-build docker-up docker-down test-services
docker-build:
	docker-compose build

docker-up:docker-down
	docker-compose up

docker-down:
	docker-compose down
docker-logs:
	docker-compose logs

# 启动测试服务
test-services:
	docker-compose -f test-services/docker-compose.yaml up -d

# 清理命令
.PHONY: clean
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.pyd" -delete
	find . -type f -name ".coverage" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +