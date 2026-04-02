#!/usr/bin/env bash
# run.sh – Run the NBA Scraper Agent (run in WSL)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtual environment
if [ ! -d "venv" ]; then
    echo "[ERR] Virtual environment not found. Run ./setup.sh first."
    exit 1
fi
source venv/bin/activate

# Usage: ./run.sh [once|loop] [--dry-run]
MODE="${1:-loop}"
shift 2>/dev/null || true

echo "=== NBA Scraper Agent – mode: $MODE $* ==="
python3 scraper.py "$MODE" "$@"
