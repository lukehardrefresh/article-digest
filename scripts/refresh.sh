#!/usr/bin/env bash
# Refresh wrapper — runs the digest pipeline.
#
# Usage:
#   ./scripts/refresh.sh                 # default output dir (./output)
#   DIGEST_OUTPUT_DIR=/var/digest ./scripts/refresh.sh
#
# Schedule (Mon & Thu, 09:00 Sydney): see cron line in README.
set -euo pipefail

# Resolve script dir regardless of where it's invoked from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# Load .env if present (sets LLM_PROVIDER, OPENAI_API_KEY, PAGE_READER_CMD, etc.)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi

# Prefer python3, fall back to python
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

exec "$PY" scripts/digest.py
