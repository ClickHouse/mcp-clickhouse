# Build stage
FROM debian:bookworm-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    wget \
    gcc \
    g++ \
    make \
    zlib1g-dev \
    libffi-dev \
    libssl-dev \
    libbz2-dev \
    liblzma-dev \
    clang \
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

# Create and activate virtual environment
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install UV package manager
RUN pip install uv

# Set working directory
WORKDIR /app

# Copy application files
COPY . .

# Install dependencies
RUN uv sync

# Runtime stage
FROM debian:bookworm-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    zlib1g \
    libffi8 \
    libssl3 \
    libbz2-1.0 \
    liblzma5 \
    && rm -rf /var/lib/apt/lists/*

# Install Python from builder
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /usr/local/lib /usr/local/lib
COPY --from=builder /usr/local/include /usr/local/include

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy application files
COPY . .

# Expose port
EXPOSE 18123

# Run the application
CMD ["python3", "server.py"]