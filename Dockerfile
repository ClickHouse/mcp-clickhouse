FROM debian:bookworm-slim

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    clang \
    g++ \
    gcc \
    libbz2-dev \
    libffi-dev \
    liblzma-dev \
    libssl-dev \
    make \
    wget \
    ca-certificates \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Download and install Python from source
ENV PYTHON_VERSION=3.13.2
RUN wget https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz \
    && tar -xf Python-${PYTHON_VERSION}.tgz \
    && cd Python-${PYTHON_VERSION} \
    && ./configure --enable-optimizations \
    && make -j$(nproc) \
    && make install \
    && cd .. \
    && rm -rf Python-${PYTHON_VERSION} \
    && rm Python-${PYTHON_VERSION}.tgz

RUN wget https://bootstrap.pypa.io/get-pip.py -O get-pip.py \
    && python3 get-pip.py \
    && rm get-pip.py

WORKDIR /app

# Install UV package manager
RUN pip install --no-cache-dir uv

# Copy application files
COPY . .

# Install dependencies
RUN uv sync

# Expose port
EXPOSE 8213

# Environment variables for server configuration
ENV MCP_SERVER_MODE=http
ENV MCP_SERVER_HOST=0.0.0.0
ENV MCP_SERVER_PORT=8213

# Run the application
CMD ["uv", "run", "server.py"]
