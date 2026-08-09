#!/usr/bin/env bash
# Phase 5 model bake-off (PLAN.md §6 Phase 5).
#
# Runs both halves of the comparison for each model given, writing raw logs to
# a directory so a number in the writeup can always be traced back to the run
# that produced it:
#
#   tool calling   -> web/scripts/measure-tool-selection.ts, standard + hard
#   categorization -> bookkeeper eval --llm
#
# Both harnesses already take the model from the environment, so nothing here
# patches code to swap models -- `OLLAMA_MODEL` for the TypeScript side,
# `BOOKKEEPER_OLLAMA_MODEL` for the sidecar. That matters for the result: the
# bake-off exercises the same code paths the app ships, not a copy of them.
#
# Models are NOT pulled here. `ollama pull` is multi-GB and the Makefile takes
# the same position for the same reason -- that is the user's call, not a
# script's.
#
#   usage: scripts/model_bakeoff.sh qwen3:8b phi4:14b gemma3:12b
#
# One model at a time, deliberately. Two models resident at once on a 16GB
# machine (§3.2) would make every latency figure a measurement of memory
# pressure rather than of the model.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${BAKEOFF_OUT:-$REPO/.omc/bakeoff}"
mkdir -p "$OUT"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <model> [model...]" >&2
  exit 2
fi

for model in "$@"; do
  slug="${model//[:\/]/-}"
  echo "=== $model ==="

  if ! ollama show "$model" >/dev/null 2>&1; then
    echo "  SKIPPED: not installed. Run: ollama pull $model"
    continue
  fi

  # Unload anything resident first, so the model under test is the only one in
  # memory and the first call's cold start belongs to it.
  ollama stop --all >/dev/null 2>&1 || true

  echo "  tool selection (standard)..."
  ( cd "$REPO/web" && OLLAMA_MODEL="$model" npx tsx scripts/measure-tool-selection.ts ) \
    > "$OUT/$slug.selection-standard.log" 2>&1
  echo "    $(grep -oE '[0-9]+/[0-9]+ correct \([0-9.]+%\)' "$OUT/$slug.selection-standard.log" | tail -1)"

  echo "  tool selection (hard)..."
  ( cd "$REPO/web" && OLLAMA_MODEL="$model" npx tsx scripts/measure-tool-selection.ts --hard ) \
    > "$OUT/$slug.selection-hard.log" 2>&1
  echo "    $(grep -oE '[0-9]+/[0-9]+ defensible[^)]*\)' "$OUT/$slug.selection-hard.log" | tail -1)"

  echo "  categorization (eval --llm)..."
  ( cd "$REPO/sidecar" && BOOKKEEPER_OLLAMA_MODEL="$model" uv run bookkeeper eval --llm ) \
    > "$OUT/$slug.eval.log" 2>&1
  echo "    $(grep -E '^llm ' "$OUT/$slug.eval.log" | tail -1)"

  echo "  resident size while loaded:"
  ollama ps 2>/dev/null | tail -n +2 | sed 's/^/    /'
done

echo
echo "raw logs: $OUT"
echo "latency:  scripts/bakeoff_table.py $OUT"
