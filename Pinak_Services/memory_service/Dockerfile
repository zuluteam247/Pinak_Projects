# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /code

# Install uv for dependency management
RUN pip install --no-cache-dir uv

# Copy project metadata for dependency installation first (leverages Docker layer caching)
COPY pyproject.toml uv.lock README.md ./
COPY Pinak_Services/memory_service/pyproject.toml Pinak_Services/memory_service/pyproject.toml

# Install runtime dependencies into a managed virtual environment
RUN uv sync --frozen --project Pinak_Services/memory_service --no-dev --no-install-workspace

# Copy the application source code
COPY Pinak_Services/memory_service/app Pinak_Services/memory_service/app
COPY Pinak_Services/memory_service/schemas Pinak_Services/memory_service/schemas
# Install the service package only after its source exists. Keep root package
# out of this image; the API imports only app.*.
RUN uv sync --frozen --project Pinak_Services/memory_service --no-dev --no-install-package pinak-project
WORKDIR /code/Pinak_Services/memory_service

# Expose the port the app runs on
EXPOSE 8000

# Run the FastAPI application via uv to ensure the managed environment is used
# This image has no optional embeddings extra. Keyword-only retrieval is honest;
# a separate image must install the extra before enabling real vectors.
ENV PINAK_EMBEDDING_BACKEND=none
ENV PINAK_DATA_ROOT=/code/Pinak_Services/memory_service/data
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
