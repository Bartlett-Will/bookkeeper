.DEFAULT_GOAL := dev
.PHONY: dev install check clean

SIDECAR_HOST := 127.0.0.1
SIDECAR_PORT := 8000
OLLAMA_MODEL := qwen3:8b

# --- install ---------------------------------------------------------------
# `just` isn't installed on this machine and there's no docker, so this is a
# plain Makefile (see .omc/handoffs/team-plan.md). `pnpm` isn't on PATH
# either; `corepack enable pnpm` (via `npx corepack`, since Node 26 no longer
# ships corepack as a bare binary) makes `pnpm` resolve globally.

install:
	@echo "==> sidecar: uv sync"
	@cd sidecar && uv sync
	@echo "==> web: pnpm install"
	@cd web && pnpm install

# --- dev ---------------------------------------------------------------
# Starts Ollama (if not already serving), the Python sidecar, and the
# Next.js dev server together, and tears all three down on Ctrl+C.
#
# What this does NOT do: pull the model for you. `ollama pull qwen3:8b` is a
# multi-GB download; run it once yourself and this target just checks it's
# there and warns (not fails) if it isn't, per PLAN.md §5.3's model choice.

dev:
	@bash scripts/dev.sh

# --- check ---------------------------------------------------------------
# Each side's own lint/typecheck/test entry points, run from the root so CI
# and humans have one command. Does not attempt to unify their tooling.

check:
	@echo "==> sidecar: ruff + pytest"
	@cd sidecar && uv run ruff check . && uv run pytest
	@echo "==> web: check"
	@cd web && pnpm run check

clean:
	rm -rf web/.next web/node_modules sidecar/.venv
