#!/usr/bin/env bash
# Start the LangGraph server + Studio for this repo without adding the CLI to the project's lock file.
# `uv run --with` layers the CLI onto the project environment for this invocation only, so the graph's
# own imports resolve. On Intel Macs current `cryptography` has no wheel; the pin keeps a wheel-able version.
set -euo pipefail
cd "$(dirname "$0")/.."
EXTRA=()
if [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "x86_64" ]]; then EXTRA=(--with "cryptography<46"); fi
exec uv run --with "langgraph-cli[inmem]" "${EXTRA[@]}" langgraph dev --config langgraph.json "$@"
