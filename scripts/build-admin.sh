#!/usr/bin/env bash
# Build the admin SPA under an exclusive lock.
#
# Why this exists
# ---------------
# Two processes running `npm run build` in admin/ at the same moment clobber
# `.svelte-kit/output` and the loser dies with:
#
#     ENOENT … .svelte-kit/output/server/manifest-full.js
#
# That reads exactly like a real build failure. It cost us one false red on
# 2026-08-17, and a false red is worse than a slow build: somebody eventually
# "fixes" code that was never broken.
#
# `mkdir` is atomic on POSIX — it succeeds for exactly one caller and fails for
# everyone else. That is the whole mechanism; no flock, no lockfile races.
#
# What does NOT work, measured rather than assumed
# ------------------------------------------------
#   npx vite build --outDir .svelte-kit/verify-mine
#
# The SvelteKit plugin IGNORES --outDir. It writes `.svelte-kit/output/**`
# anyway — the very file that collides — and it also runs the static adapter,
# rewriting `build/`, the deploy artifact. So it is exactly as collision-prone
# as a normal build AND it overwrites the thing you were trying to protect.
# Do not reach for it.
#
#   npx svelte-check
#
# Safe (touches no shared output) and fast (~7s), but blind to the failure this
# lock guards against. Verified by injecting a named import of a symbol the
# module does not export: svelte-check reported `0 errors`, and
# scripts/check_svelte_icons.py missed it too. A missing export is a
# bundler-level error and only a real build surfaces it.
#
# So: svelte-check answers "does my syntax compile". Only this answers "does the
# bundle link".
#
# Usage:  scripts/build-admin.sh [--wait]
#         --wait   block until the lock frees instead of exiting 75
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/admin/.buildlock"
WAIT="${1:-}"

acquire() { mkdir "$LOCK" 2>/dev/null; }

if ! acquire; then
  if [ "$WAIT" = "--wait" ]; then
    echo "admin build is locked by another process; waiting…" >&2
    for _ in $(seq 1 120); do
      sleep 2
      acquire && break
    done
    if [ ! -d "$LOCK" ]; then
      echo "could not acquire the admin build lock after 4 minutes" >&2
      exit 75
    fi
  else
    echo "admin build is already running (lock: $LOCK)." >&2
    echo "This is NOT a build failure. Re-run with --wait, or wait and retry." >&2
    exit 75
  fi
fi

# Release on any exit, including a failed build or a Ctrl-C, or the next build
# blocks forever on a lock nobody holds.
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

cd "$ROOT/admin"
npm run build
