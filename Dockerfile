FROM python:3.12-slim

WORKDIR /app

# matrix-nio[e2e] may need libolm headers when a compatible wheel is not available.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libolm-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY walden ./walden
RUN pip install --no-cache-dir '.[matrix]'

COPY config.yaml ./config.yaml
COPY move ./move

CMD ["walden", "-c", "config.yaml", "matrix"]
