#!/usr/bin/env bash
#
# City Pharma Agent — backup.
#
#   ./deploy/aws/backup.sh [dest-dir]        (default: ./backups)
#
# Takes a timestamped copy of everything that is STATE rather than code:
#
#   postgres.sql.gz   catalog, inventory, users, chat logs, drug_edges, agno
#                     sessions. pg_dump, not a volume copy — restorable into any
#                     Postgres 16, and consistent without stopping the stack.
#   redis.rdb         THE EMBED CREDENTIALS. This is not a cache-only backup:
#                     with EMBED_DEV_CREDENTIAL=false nothing re-seeds them, so
#                     losing redis means every embedded widget stops
#                     authenticating until someone re-mints by hand.
#   sftp_ssh.tar.gz   partner public keys + authorized_keys.
#   env.prod          every secret on the box. THIS FILE IS THE CROWN JEWELS —
#                     see the warning printed at the end.
#
# NOT backed up: the docker images (rebuild them), the uploaded xlsx in
# sftp_data (they are already loaded into Postgres), and Caddy's certificates
# (Let's Encrypt re-issues them for free — but see the README on rate limits).

set -euo pipefail

PROJECT=pharmacy
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

DEST="${1:-$REPO_ROOT/backups}"
STAMP="$(date -u '+%Y%m%d-%H%M%S')"
OUT="$DEST/$STAMP"

ENV_FILE="$REPO_ROOT/.env.prod"
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENV_FILE"
         -f docker-compose.yml -f docker-compose.prod.yml)

say()  { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %-16s %s\n' "$1" "${2:-}"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[1;31mFAILED:\033[0m %s\n\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || die "no .env.prod — nothing installed here to back up."

# Every artifact lands under a 0700 dir: this whole tree is secret.
umask 077
mkdir -p "$OUT"
chmod 700 "$DEST" "$OUT"

say "Backing up to $OUT"

# --- postgres --------------------------------------------------------------
# Runs INSIDE the container, so it needs no client on the host and no published
# port. -Fp (plain SQL) + gzip: restorable with psql alone, no pg_restore.
"${COMPOSE[@]}" exec -T postgres \
    pg_dump -U pharmacy -d pharmacy --clean --if-exists \
    | gzip > "$OUT/postgres.sql.gz" \
    || die "pg_dump failed. Is the stack up? (docker compose -p $PROJECT ps)"
ok postgres.sql.gz "$(du -h "$OUT/postgres.sql.gz" | cut -f1)"

# --- redis (embed credentials) --------------------------------------------
# SAVE is synchronous: when it returns, dump.rdb on disk is current. BGSAVE
# would return immediately and we would copy a file still being written.
"${COMPOSE[@]}" exec -T redis redis-cli SAVE >/dev/null \
    || warn "redis SAVE failed — the .rdb below may be stale"
REDIS_CID="$("${COMPOSE[@]}" ps -q redis)"
if [ -n "$REDIS_CID" ]; then
    docker cp "$REDIS_CID:/data/dump.rdb" "$OUT/redis.rdb" 2>/dev/null \
        || warn "no /data/dump.rdb in the redis container — skipping"
    [ -f "$OUT/redis.rdb" ] && ok redis.rdb "$(du -h "$OUT/redis.rdb" | cut -f1)"
else
    warn "redis container not running — embed credentials NOT backed up"
fi

# --- sftp keys -------------------------------------------------------------
docker run --rm -v "${PROJECT}_sftp_ssh:/s:ro" -v "$OUT:/out" alpine:3 \
    tar czf /out/sftp_ssh.tar.gz -C /s . \
    || warn "could not archive the sftp_ssh volume"
[ -f "$OUT/sftp_ssh.tar.gz" ] && ok sftp_ssh.tar.gz "$(du -h "$OUT/sftp_ssh.tar.gz" | cut -f1)"

# --- secrets ---------------------------------------------------------------
cp "$ENV_FILE" "$OUT/env.prod"
chmod 600 "$OUT/env.prod"
ok env.prod "0600 — contains every secret"

# --- restore instructions, stored WITH the backup --------------------------
cat > "$OUT/RESTORE.md" <<EOF
# Restore — City Pharma Agent, backup ${STAMP}

Onto a fresh box. Assumes Docker and a checkout of the repo.

## 1. Secrets first

    cp env.prod  <repo>/.env.prod
    chmod 600    <repo>/.env.prod

Restore this BEFORE starting anything. POSTGRES_PASSWORD in it is the password
the dumped database expects, and SECRET_KEY is what signs the embed tokens and
the admin JWTs — boot with a different SECRET_KEY and every existing session and
signed embed user is invalidated.

Do NOT run install.sh on the restore box: it would generate fresh secrets and
overwrite exactly the file you just restored.

## 2. Bring up the data services and load the dump

    cd <repo>
    docker compose -p ${PROJECT} --env-file .env.prod \\
      -f docker-compose.yml -f docker-compose.prod.yml up -d postgres redis

    gunzip -c postgres.sql.gz | docker compose -p ${PROJECT} \\
      --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml \\
      exec -T postgres psql -U pharmacy -d pharmacy

The dump is --clean --if-exists, so it drops and recreates its own objects. It
is safe to load over a freshly-initialised empty database.

## 3. Redis — the embed credentials

Stop redis, drop the .rdb in, start it. Redis only reads dump.rdb at STARTUP; a
copy into a running container is ignored, and will be overwritten by the next
save.

    docker compose -p ${PROJECT} --env-file .env.prod \\
      -f docker-compose.yml -f docker-compose.prod.yml stop redis
    docker run --rm -v ${PROJECT}_redis_data:/d -v "\$PWD:/b" alpine:3 \\
      sh -c 'cp /b/redis.rdb /d/dump.rdb && rm -f /d/appendonly.aof /d/appendonlydir/* 2>/dev/null; true'
    docker compose -p ${PROJECT} --env-file .env.prod \\
      -f docker-compose.yml -f docker-compose.prod.yml start redis

Note the AOF removal. Prod runs redis with appendonly=yes, and when an AOF
exists redis loads THAT and ignores dump.rdb entirely — your restore would
silently do nothing. Clearing it forces the .rdb to be read.

Verify the credentials came back:

    docker compose -p ${PROJECT} --env-file .env.prod \\
      -f docker-compose.yml -f docker-compose.prod.yml \\
      exec -T redis redis-cli HGETALL pharmacy:credentials

If it is empty, re-mint one: POST /admin/credentials (see deploy/aws/README.md).

## 4. SFTP partner keys

    docker volume create ${PROJECT}_sftp_ssh
    docker run --rm -v ${PROJECT}_sftp_ssh:/s -v "\$PWD:/b" alpine:3 \\
      sh -c 'tar xzf /b/sftp_ssh.tar.gz -C /s && chown -R 1001:1001 /s && chmod 700 /s /s/keys && chmod 600 /s/authorized_keys'

## 5. Everything up

    docker compose -p ${PROJECT} --env-file .env.prod \\
      -f docker-compose.yml -f docker-compose.prod.yml up -d --build

Then point the DNS A record at the new box. Caddy issues a fresh certificate on
the first request.

## What this backup does NOT contain

- The SFTP host key. Partners' clients will warn that the host identity changed
  (that is honest — it did). Tell them beforehand, or restore the
  ${PROJECT}_sftp_hostkeys volume too.
- Uploaded xlsx files. They are already loaded into Postgres, which IS here.
- Docker images. Rebuilt from source by step 5.
EOF
ok RESTORE.md "read it before you need it"

say "Done"
printf '    %s\n' "$OUT"
printf '\n'
printf '    \033[1;33mThis directory contains .env.prod — the OpenRouter key, the admin\n'
printf '    password and the database password, in clear.\033[0m Move it OFF this box (S3\n'
printf '    with SSE, or a password manager). A backup left next to the thing it\n'
printf '    backs up protects you from nothing.\n\n'
