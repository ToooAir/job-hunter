#!/bin/bash
# Manual full-pipeline run — invoked by the dashboard "Run now" button
# (works from a shell too). Mirrors scheduler.py STAGES; keep the two in sync.

set -euo pipefail

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJ_DIR/logs"

# Use venv python if available (local dev), otherwise fall back to system python (Docker)
if [ -f "$PROJ_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJ_DIR/.venv/bin/python"
else
    PYTHON="$(command -v python3 || command -v python)"
fi

cd "$PROJ_DIR"

echo ""
echo "════════════════════════════════════════"
echo " Job Pipeline  $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════"

# Rotate log if it exceeds 5 MB
LOG_FILE="$LOG_DIR/pipeline.log"
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5242880 ]; then
    mv "$LOG_FILE" "$LOG_DIR/pipeline.$(date '+%Y%m%d').log"
    echo "Log rotated at $(date '+%Y-%m-%d %H:%M:%S')" > "$LOG_FILE"
fi

echo "--- Phase 1: ingest ---"
"$PYTHON" phase1_ingestor.py
echo "Phase 1 finished at $(date '+%H:%M:%S')"

echo ""
echo "--- Geo triage (best-effort) ---"
"$PYTHON" remote_geo_triage.py --write-db || echo "geo triage failed — continuing"

echo ""
echo "--- Phase 2: score ---"
"$PYTHON" phase2_scorer.py
echo "Phase 2 finished at $(date '+%H:%M:%S')"

echo ""
echo "--- ATS scan (best-effort) ---"
"$PYTHON" ats_scan.py --write-db || echo "ats_scan failed — continuing"

echo ""
echo "--- Stage 1 drafts (best-effort) ---"
"$PYTHON" apply_stage1.py || echo "apply_stage1 failed — continuing"

echo ""
echo "Pipeline complete. $(date '+%Y-%m-%d %H:%M:%S')"
