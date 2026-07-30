#!/usr/bin/env bash
# Starts Ollama (if not already serving), the Python sidecar, and the
# Next.js dev server together; tears down whatever this script itself
# started when you Ctrl+C. Invoked via `make dev` — see ../Makefile.
set -uo pipefail
# Job control (`set -m`) puts each `&` job in its own process group, so
# `kill -TERM -$pid` below reaches the whole tree (uv -> bookkeeper serve,
# pnpm -> sh -> next dev -> its workers) instead of just the immediate
# child. Without this, Ctrl+C leaves orphaned servers holding :8000/:3000.
set -m

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OLLAMA_URL="${OLLAMA_BASE_URL_PROBE:-http://localhost:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:8b}"
SIDECAR_HOST="${SIDECAR_HOST:-127.0.0.1}"
SIDECAR_PORT="${SIDECAR_PORT:-8000}"

pids=()
started_ollama=0

cleanup() {
	echo ""
	echo "==> shutting down..."
	for pid in "${pids[@]}"; do
		kill -TERM "-$pid" 2>/dev/null
	done
	if [[ "$started_ollama" -eq 1 ]]; then
		echo "==> stopping the Ollama server this script started"
		kill -TERM "-$ollama_pid" 2>/dev/null
	fi
	wait 2>/dev/null
}
trap cleanup EXIT INT TERM

# --- Ollama ------------------------------------------------------------
if curl -fsS -m 2 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
	echo "==> Ollama already serving at $OLLAMA_URL"
else
	echo "==> starting Ollama..."
	ollama serve >/tmp/bookkeeper-ollama.log 2>&1 &
	ollama_pid=$!
	started_ollama=1
	for _ in $(seq 1 30); do
		if curl -fsS -m 1 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
			break
		fi
		sleep 1
	done
	if ! curl -fsS -m 2 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
		echo "!! Ollama did not come up after 30s — see /tmp/bookkeeper-ollama.log"
	fi
fi

if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$OLLAMA_MODEL"; then
	echo "==> model $OLLAMA_MODEL is present"
else
	echo "!! model $OLLAMA_MODEL is not pulled yet — chat will fail until you run:"
	echo "!!   ollama pull $OLLAMA_MODEL"
fi

# --- sidecar -------------------------------------------------------------
echo "==> starting sidecar on $SIDECAR_HOST:$SIDECAR_PORT..."
(cd "$ROOT_DIR/sidecar" && uv run bookkeeper serve --host "$SIDECAR_HOST" --port "$SIDECAR_PORT") &
pids+=("$!")

# --- web -------------------------------------------------------------------
echo "==> starting Next.js dev server..."
(cd "$ROOT_DIR/web" && pnpm run dev) &
pids+=("$!")

wait
