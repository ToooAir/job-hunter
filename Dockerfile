FROM python:3.12-slim

WORKDIR /app

# All parsers used are html.parser (stdlib) — no system packages needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source code — copied last so code-only changes don't invalidate the pip layer.
# Data directories (data/, qdrant_data/, candidate_kb/, config/) are excluded
# via .dockerignore and mounted as volumes at runtime.
COPY . .

EXPOSE 8501
