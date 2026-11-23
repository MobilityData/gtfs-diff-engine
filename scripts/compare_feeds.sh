#!/usr/bin/env bash
# compare_feeds.sh — Compare two GTFS feeds and produce a structured JSON diff.
#
# Each feed can be supplied as:
#   • A remote URL   (https://…)  — downloaded to a temp directory
#   • A local zip    (/path/to/feed.zip)
#   • A local folder (/path/to/gtfs-dir/)
#
# Usage:
#   ./scripts/compare_feeds.sh <BASE_FEED> <NEW_FEED> [GTFS-DIFF OPTIONS]
#
# Arguments:
#   BASE_FEED   Base (old) GTFS feed — URL, local zip, or local folder
#   NEW_FEED    New GTFS feed        — URL, local zip, or local folder
#
# Options:
#   Everything after the two feeds is passed to gtfs-diff as is.
#   Run `gtfs-diff --help` for the full list.
#   -h, --help           Show this help message
#
# Examples:
#   # Two remote URLs
#   ./scripts/compare_feeds.sh \
#       https://example.com/gtfs-jan.zip \
#       https://example.com/gtfs-feb.zip
#
#   # Mix of local zip and remote URL, with shape maps in the HTML report
#   ./scripts/compare_feeds.sh \
#       /data/gtfs-jan.zip \
#       https://example.com/gtfs-feb.zip \
#       --output diff.json --cap 1000 --shape-geometry
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

[[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]] && usage
[[ $# -ge 2 ]] || die "BASE_FEED and NEW_FEED are required. Run with --help for usage."
[[ "$1" != -* && "$2" != -* ]] || die "Give BASE_FEED and NEW_FEED first, then the gtfs-diff options."

BASE_FEED="$1"
NEW_FEED="$2"
shift 2

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
# Run diff
# --------------------------------------------------------------------------- #

echo "🔍 Running diff..." >&2
gtfs-diff "$BASE_PATH" "$NEW_PATH" "$@"
