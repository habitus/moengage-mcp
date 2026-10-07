#!/usr/bin/env bash
# MoEngage Docs MCP: installer
# Ensures the search index exists and registers the server with Claude Code.
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="${MOENGAGE_DOCS_CACHE:-$HOME/.cache/moengage_docs}"
INDEX="$CACHE_DIR/fulltext.jsonl"
NAME="moengage-docs"

echo "MoEngage Docs MCP: installer"
echo "Folder: $DIR"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3 from https://www.python.org and re-run."
  exit 1
fi

# 1. Ensure the index exists
mkdir -p "$CACHE_DIR"
if [ -f "$INDEX" ]; then
  echo "[1/2] Index already present at $INDEX, skipping build."
elif [ -f "$DIR/fulltext.jsonl" ]; then
  echo "[1/2] Found bundled index, copying to $INDEX ..."
  cp "$DIR/fulltext.jsonl" "$INDEX"
else
  echo "[1/2] No index found, building it now (one-time crawl of moengage.com/docs)..."
  python3 "$DIR/build_index.py"
fi

# 2. Register with Claude Code
echo "[2/2] Registering with Claude Code (user scope)..."
if command -v claude >/dev/null 2>&1; then
  claude mcp remove "$NAME" --scope user >/dev/null 2>&1 || true
  claude mcp add "$NAME" --scope user -- python3 "$DIR/server.py"
  echo
  echo "Done. Verify with:  claude mcp list"
  echo "Then start a new Claude session and try: \"search the moengage docs for android push token\""
else
  echo "NOTE: 'claude' CLI not found. Register manually:"
  echo "  claude mcp add $NAME --scope user -- python3 \"$DIR/server.py\""
  echo "Or add it to Claude Desktop config (see README.md)."
fi
