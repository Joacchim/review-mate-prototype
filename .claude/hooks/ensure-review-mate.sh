#!/usr/bin/env bash
# SessionStart hook: ensure the review-mate bridge-server (UI + /api + /mcp) is up.
#
# Idempotent and non-blocking on failure: if the server already answers, do nothing; otherwise start
# it detached and wait briefly for readiness. Runs *before* the session binds MCP, so the review-mate
# MCP tools connect cleanly. Once started it survives the session, so later sessions find it already
# up. Never fails the session — always exits 0.
set -u

URL="http://127.0.0.1:8765/api/sessions"
up() { curl -sf -m 2 -o /dev/null "$URL"; }

up && exit 0   # already running — nothing to do

DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
( cd "$DIR" && nohup uv run review-mate >/tmp/review-mate-server.log 2>&1 & ) >/dev/null 2>&1

for _ in $(seq 1 20); do up && break; sleep 0.5; done   # wait up to ~10s for readiness
exit 0
