#!/usr/bin/env bash
# Generate Pydantic v2 models from the GTFS Diff JSON Schema.
#
# Usage:
#   ./scripts/generate_models.sh                     # use version from src/gtfs_diff/schema.conf
#   ./scripts/generate_models.sh v2-rc1              # fetch a specific version from GitHub
#   ./scripts/generate_models.sh /path/to/local.json # use a local file
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT="$REPO_ROOT/src/gtfs_diff/models.py"
SCHEMA_REPO="MobilityData/gtfs-diff"
SCHEMA_BRANCH="main"

# --- Resolve input: argument > schema.conf -----------------------------------
SCHEMA_CONF="$REPO_ROOT/src/gtfs_diff/schema.conf"
if [ $# -ge 1 ]; then
  INPUT="$1"
elif [ -f "$SCHEMA_CONF" ]; then
  # shellcheck source=../src/gtfs_diff/schema.conf
  source "$SCHEMA_CONF"
  INPUT="${SCHEMA_VERSION:?SCHEMA_VERSION not set in schema.conf}"
  echo "Using version from schema.conf: $INPUT"
else
  echo "Usage: $0 [<version | /path/to/schema.json>]" >&2
  echo "  e.g. $0 v2-rc1" >&2
  echo "  or set SCHEMA_VERSION in src/gtfs_diff/schema.conf" >&2
  exit 1
fi

# --- Resolve schema source ---------------------------------------------------
if [ -f "$INPUT" ]; then
  SCHEMA_FILE="$INPUT"
  echo "Using local schema: $SCHEMA_FILE"
else
  VERSION="$INPUT"
  SCHEMA_URL="https://raw.githubusercontent.com/$SCHEMA_REPO/$SCHEMA_BRANCH/spec/v2/json_schema/${VERSION}.json"
  SCHEMA_FILE="$(mktemp "${TMPDIR:-/tmp}/gtfs_diff_schema_XXXXXX.json")"
  trap 'rm -f "$SCHEMA_FILE"' EXIT

  echo "Fetching schema from $SCHEMA_URL ..."
  curl -fsSL "$SCHEMA_URL" -o "$SCHEMA_FILE"
fi

# --- Ensure datamodel-code-generator is available ----------------------------
if ! command -v datamodel-codegen &>/dev/null; then
  echo "Installing datamodel-code-generator ..."
  pip install 'datamodel-code-generator[ruff]>=0.59'
fi

# --- Generate ----------------------------------------------------------------
echo "Generating models → $OUTPUT"
datamodel-codegen \
  --formatters ruff-format ruff-check \
  --input "$SCHEMA_FILE" \
  --input-file-type jsonschema \
  --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.10 \
  --use-standard-collections \
  --use-union-operator \
  --snake-case-field \
  --collapse-root-models \
  --enum-field-as-literal all \
  --field-constraints \
  --use-schema-description \
  --extra-fields allow \
  --class-name GtfsDiff \
  --output "$OUTPUT"

# --- Post-process: replace header, append __all__ ----------------------------
# Replace the codegen header with a clear auto-generated notice.
{
  echo "# AUTO-GENERATED — DO NOT EDIT"
  echo "# This file is generated from the GTFS Diff JSON Schema."
  echo "# To regenerate: ./scripts/generate_models.sh"
  echo "# Schema source: https://github.com/$SCHEMA_REPO"
  echo ""
  # Strip the original codegen comment block (everything before the first blank line).
  sed -n '/^$/,$p' "$OUTPUT"
} > "$OUTPUT.tmp" && mv "$OUTPUT.tmp" "$OUTPUT"

# Collect class names and append __all__.
CLASSES=$(grep -oE '^class ([A-Za-z_][A-Za-z0-9_]*)' "$OUTPUT" | awk '{print $2}')
{
  echo ""
  echo ""
  echo "__all__ = ["
  for cls in $CLASSES; do
    echo "    \"$cls\","
  done
  echo "]"
} >> "$OUTPUT"

COUNT=$(echo "$CLASSES" | wc -w | tr -d ' ')
echo "Done — $COUNT model(s) generated."
