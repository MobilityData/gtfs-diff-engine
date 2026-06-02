#!/usr/bin/env bash
# Generate Pydantic v2 models from the GTFS Diff JSON Schema.
#
# Usage:
#   ./scripts/generate_models.sh                     # use version from schema.conf
#   ./scripts/generate_models.sh v2-rc1              # fetch a specific version from GitHub
#   ./scripts/generate_models.sh /path/to/local.json # use a local file
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT="$REPO_ROOT/src/gtfs_diff/models.py"
SCHEMA_REPO="MobilityData/gtfs-diff"
SCHEMA_BRANCH="main"

# --- Resolve input: argument > schema.conf -----------------------------------
if [ $# -ge 1 ]; then
  INPUT="$1"
elif [ -f "$REPO_ROOT/schema.conf" ]; then
  # shellcheck source=../schema.conf
  source "$REPO_ROOT/schema.conf"
  INPUT="${SCHEMA_VERSION:?SCHEMA_VERSION not set in schema.conf}"
  echo "Using version from schema.conf: $INPUT"
else
  echo "Usage: $0 [<version | /path/to/schema.json>]" >&2
  echo "  e.g. $0 v2-rc1" >&2
  echo "  or set SCHEMA_VERSION in schema.conf" >&2
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
  --class-name GtfsDiff \
  --output "$OUTPUT"

# --- Post-process: clean header, append __all__ ------------------------------
# Remove the timestamp and temp filename so re-generation doesn't create noisy diffs.
sed -i.bak '/^#   timestamp:/d; /^#   filename:/d' "$OUTPUT" && rm -f "$OUTPUT.bak"

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
