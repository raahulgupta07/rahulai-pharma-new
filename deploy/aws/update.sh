#!/usr/bin/env bash
#
# City Pharma Agent — update in place, with an automatic rollback.
#
#   ./deploy/aws/update.sh
#
# 1. git pull (only if a remote exists — this repo may well have none)
# 2. tag the running image as :rollback, so there is something to go back TO
# 3. rebuild — BOTH the Python backend and the admin SPA are BAKED INTO the api
#    image (docker/Dockerfile stage 1 runs the vite build; there is no source
#    mount anywhere). Editing files on the host changes NOTHING until this runs.
# 4. restart, wait for /health
# 5. if health fails: put :rollback back, restart, and say so loudly.
#
# Data is untouched: postgres, redis and the sftp volumes are never recreated.

set -euo pipefail

PROJECT=pharmacy
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env.prod"
IMAGE=pharmacy-agent
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENV_FILE"
         -f docker-compose.yml -f docker-compose.prod.yml)

say()  { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[1;31mFAILED:\033[0m %s\n\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || die "no .env.prod — this box has never been installed. Run deploy/aws/install.sh <domain> <email>."

# ---------------------------------------------------------------------------
# 1. pull
# ---------------------------------------------------------------------------

say "Fetching code"
if [ -d .git ] && git remote | grep -q .; then
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
    git pull --ff-only origin "$BRANCH" \
        || die "git pull failed (local commits, or a diverged branch). Resolve by hand, then re-run."
    ok "pulled origin/$BRANCH -> $(git rev-parse --short HEAD)"
else
    warn "no git remote configured — building whatever is on disk right now."
    warn "(This repo is local-only by default; that is expected, not an error.)"
fi

# ---------------------------------------------------------------------------
# 2. rollback point
# ---------------------------------------------------------------------------

say "Tagging a rollback point"
if docker image inspect "${IMAGE}:latest" >/dev/null 2>&1; then
    docker image tag "${IMAGE}:latest" "${IMAGE}:rollback"
    ok "${IMAGE}:latest -> ${IMAGE}:rollback ($(docker image inspect -f '{{.Id}}' "${IMAGE}:latest" | cut -c8-19))"
    HAVE_ROLLBACK=1
else
    warn "no ${IMAGE}:latest image yet — nothing to roll back to on this run."
    HAVE_ROLLBACK=0
fi

# ---------------------------------------------------------------------------
# 3. rebuild (backend + admin SPA, in the image)
# ---------------------------------------------------------------------------

say "Rebuilding the api image (compiles the admin SPA — a few minutes)"
"${COMPOSE[@]}" build api ingest-worker \
    || die "the image build failed. Nothing was restarted; the old stack is still serving."
ok "built"

# ---------------------------------------------------------------------------
# 4. restart + health
# ---------------------------------------------------------------------------

say "Restarting api + ingest-worker"
"${COMPOSE[@]}" up -d api ingest-worker
ok "containers replaced"

say "Waiting for /health"
healthy=0
for i in $(seq 1 60); do
    if curl -fsS --max-time 3 "http://127.0.0.1:8088/health" >/dev/null 2>&1; then
        healthy=1
        ok "healthy after ${i}s"
        break
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 5. roll back on failure
# ---------------------------------------------------------------------------

if [ "$healthy" -eq 0 ]; then
    printf '\n--- api logs (last 40) ---\n' >&2
    "${COMPOSE[@]}" logs --tail 40 api >&2 || true

    if [ "$HAVE_ROLLBACK" -eq 1 ]; then
        say "ROLLING BACK to ${IMAGE}:rollback"
        docker image tag "${IMAGE}:rollback" "${IMAGE}:latest"
        "${COMPOSE[@]}" up -d api ingest-worker

        for i in $(seq 1 60); do
            if curl -fsS --max-time 3 "http://127.0.0.1:8088/health" >/dev/null 2>&1; then
                die "the new build was UNHEALTHY and has been rolled back. The previous version is serving again (healthy after ${i}s). Logs from the failed build are above."
            fi
            sleep 1
        done
        die "the new build was unhealthy AND the rollback did not come up either. The site is DOWN. Logs above; check 'docker compose -p $PROJECT logs api'."
    fi

    die "the new build never became healthy, and there was no previous image to roll back to. Logs above."
fi

say "Update complete"
printf '    running: %s\n' "$(docker image inspect -f '{{.Id}}' "${IMAGE}:latest" | cut -c8-19)"
printf '    the previous image is still tagged %s:rollback if you need it.\n\n' "$IMAGE"
