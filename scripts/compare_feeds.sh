#!/usr/bin/env bash
# compare_feeds.sh — Compare two GTFS feeds and produce a structured JSON diff.
#
# Each feed can be supplied as:
#   • A remote URL   (https://…)  — downloaded to a temp directory
#   • A local zip    (/path/to/feed.zip)
#   • A local folder (/path/to/gtfs-dir/)
#
# Usage:
#   ./scripts/compare_feeds.sh <BASE_FEED> <NEW_FEED> [OPTIONS]
#
# Arguments:
#   BASE_FEED   Base (old) GTFS feed — URL, local zip, or local folder
#   NEW_FEED    New GTFS feed        — URL, local zip, or local folder
#
# Options:
#   -o, --output FILE    Write JSON diff to FILE (default: stdout)
#   -c, --cap    INT     Max row changes per file (default: no cap; 0 = counts only)
#   --no-pretty          Compact JSON output (default: pretty-printed)
#   -h, --help           Show this help message
#
# Examples:
#   # Two remote URLs
#   ./scripts/compare_feeds.sh \
#       https://example.com/gtfs-jan.zip \
#       https://example.com/gtfs-feb.zip
#
#   # Mix of local zip and remote URL
#   ./scripts/compare_feeds.sh \
#       /data/gtfs-jan.zip \
#       https://example.com/gtfs-feb.zip \
#       --output diff.json --cap 1000
#
#   # Two local folders
#   ./scripts/compare_feeds.sh \
#       /data/gtfs-jan/ \
#       /data/gtfs-feb/ \
#       --output diff.json

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

is_url() { [[ "$1" == http://* || "$1" == https://* ]]; }

# resolve_feed <label> <input> <dest_zip>
#   Resolves a feed input to a path gtfs-diff can consume:
#     - URL       → downloaded to <dest_zip>; prints the dest_zip path
#     - local zip → validated and echoed as-is
#     - local dir → validated and echoed as-is
resolve_feed() {
    local label="$1" input="$2" dest_zip="$3"

    if is_url "$input"; then
        require_cmd curl
        echo "⬇  Downloading $label feed..." >&2
        curl -fsSL --retry 3 --retry-delay 2 -o "$dest_zip" "$input" \
            || die "Failed to download $label feed: $input"
        file -b "$dest_zip" | grep -qi "zip" \
            || die "$label feed URL did not return a valid zip: $input"
        echo "✔  $label feed downloaded." >&2
        echo "$dest_zip"
    elif [[ -f "$input" ]]; then
        [[ "$input" == *.zip ]] \
            || die "$label feed file does not have a .zip extension: $input"
        file -b "$input" | grep -qi "zip" \
            || die "$label feed does not appear to be a valid zip: $input"
        echo "$input"
    elif [[ -d "$input" ]]; then
        echo "$input"
    else
        die "$label feed not found (not a URL, zip file, or directory): $input"
    fi
}

# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

BASE_FEED=""
NEW_FEED=""
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
            if   [[ -z "$BASE_FEED" ]]; then BASE_FEED="$1"
            elif [[ -z "$NEW_FEED"  ]]; then NEW_FEED="$1"
            else die "Unexpected argument: $1"
            fi
            shift ;;
    esac
done

[[ -n "$BASE_FEED" ]] || die "BASE_FEED is required.\nRun with --help for usage."
[[ -n "$NEW_FEED"  ]] || die "NEW_FEED is required.\nRun with --help for usage."

# --------------------------------------------------------------------------- #
# Dependency checks
# --------------------------------------------------------------------------- #

require_cmd gtfs-diff

# --------------------------------------------------------------------------- #
# Temp directory (cleaned up on exit)
# --------------------------------------------------------------------------- #

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# --------------------------------------------------------------------------- #
# Resolve feeds
# --------------------------------------------------------------------------- #

BASE_PATH="$(resolve_feed "base" "$BASE_FEED" "$WORK_DIR/base.zip")"
NEW_PATH="$(resolve_feed "new"  "$NEW_FEED"  "$WORK_DIR/new.zip")"

# --------------------------------------------------------------------------- #
# Build gtfs-diff command
# --------------------------------------------------------------------------- #

CMD=(gtfs-diff "$BASE_PATH" "$NEW_PATH" "$PRETTY")

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
