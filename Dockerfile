FROM python:3.12-slim

WORKDIR /app

# All parsers used are html.parser (stdlib). libatomic1: playwright's bundled
# node (>=1.65) links against it and python:3.12-slim does not ship it.
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# Source code — copied last so code-only changes don't invalidate the pip layer.
# Data directories (data/, qdrant_data/, candidate_kb/, config/) are excluded
# via .dockerignore and mounted as volumes at runtime.
COPY . .

EXPOSE 8501
