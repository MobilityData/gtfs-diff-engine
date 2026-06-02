#!/usr/bin/env bash
# Check lint and formatting (exits non-zero on violations).
# Usage: ./scripts/lint.sh
set -euo pipefail

echo "Checking lint rules ..."
ruff check src/ tests/

echo "Checking formatting ..."
ruff format --check src/ tests/

echo "All clean."
