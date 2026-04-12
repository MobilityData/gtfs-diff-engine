#!/usr/bin/env bash
# compare_feeds.sh — Download two GTFS feeds by URL and diff them.
#
# Usage:
#   ./scripts/compare_feeds.sh <BASE_URL> <NEW_URL> [OPTIONS]
#
# Arguments:
#   BASE_URL   URL of the base (old) GTFS zip feed
#   NEW_URL    URL of the new GTFS zip feed
#
# Options:
#   -o, --output FILE    Write JSON diff to FILE (default: stdout)
#   -c, --cap    INT     Max row changes per file (default: no cap; 0 = counts only)
#   --no-pretty          Compact JSON output (default: pretty-printed)
#   -h, --help           Show this help message
#
# Examples:
#   ./scripts/compare_feeds.sh \
#       https://example.com/gtfs-jan.zip \
#       https://example.com/gtfs-feb.zip
#
#   ./scripts/compare_feeds.sh \
#       https://example.com/gtfs-jan.zip \
#       https://example.com/gtfs-feb.zip \
#       --output diff.json --cap 1000

set -euo pipefail

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

die() { echo "ERROR: $*" >&2; exit 1; }

require_cmd() {
    command -v "$1" &>/dev/null || die "'$1' is required but not installed."
}

# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

BASE_URL=""
NEW_URL=""
OUTPUT_FILE=""
CAP=""
PRETTY="--pretty"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage ;;
        -o|--output) OUTPUT_FILE="$2"; shift 2 ;;
        -c|--cap)    CAP="$2";         shift 2 ;;
        --no-pretty) PRETTY="--no-pretty"; shift ;;
        -*)          die "Unknown option: $1" ;;
        *)
            if   [[ -z "$BASE_URL" ]]; then BASE_URL="$1"
            elif [[ -z "$NEW_URL"  ]]; then NEW_URL="$1"
            else die "Unexpected argument: $1"
            fi
            shift ;;
    esac
done

[[ -n "$BASE_URL" ]] || die "BASE_URL is required.\nRun with --help for usage."
[[ -n "$NEW_URL"  ]] || die "NEW_URL is required.\nRun with --help for usage."

# --------------------------------------------------------------------------- #
# Dependency checks
# --------------------------------------------------------------------------- #

require_cmd curl
require_cmd gtfs-diff

# --------------------------------------------------------------------------- #
# Temp directory (cleaned up on exit)
# --------------------------------------------------------------------------- #

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

BASE_ZIP="$WORK_DIR/base.zip"
NEW_ZIP="$WORK_DIR/new.zip"

# --------------------------------------------------------------------------- #
# Download feeds
# --------------------------------------------------------------------------- #

echo "⬇  Downloading base feed..." >&2
curl -fsSL --retry 3 --retry-delay 2 -o "$BASE_ZIP" "$BASE_URL" \
    || die "Failed to download base feed: $BASE_URL"

echo "⬇  Downloading new feed..." >&2
curl -fsSL --retry 3 --retry-delay 2 -o "$NEW_ZIP" "$NEW_URL" \
    || die "Failed to download new feed: $NEW_URL"

# Basic sanity check — both must look like zip files
file "$BASE_ZIP" | grep -qi "zip" || die "Base feed does not appear to be a valid zip: $BASE_URL"
file "$NEW_ZIP"  | grep -qi "zip" || die "New feed does not appear to be a valid zip: $NEW_URL"

echo "✔  Both feeds downloaded." >&2

# --------------------------------------------------------------------------- #
# Build gtfs-diff command
# --------------------------------------------------------------------------- #

CMD=(gtfs-diff "$BASE_ZIP" "$NEW_ZIP" "$PRETTY")

[[ -n "$CAP"         ]] && CMD+=(--cap "$CAP")
[[ -n "$OUTPUT_FILE" ]] && CMD+=(--output "$OUTPUT_FILE")

# --------------------------------------------------------------------------- #
# Run diff
# --------------------------------------------------------------------------- #

echo "🔍 Running diff..." >&2
"${CMD[@]}"

if [[ -n "$OUTPUT_FILE" ]]; then
    echo "✔  Diff written to: $OUTPUT_FILE" >&2
fi
