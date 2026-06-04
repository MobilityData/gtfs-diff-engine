#!/usr/bin/env bash
# Auto-fix lint violations and reformat code.
# Usage: ./scripts/lint-fix.sh
set -euo pipefail

echo "Fixing lint violations ..."
ruff check --fix src/ tests/

echo "Formatting ..."
ruff format src/ tests/

echo "Done."
