#!/usr/bin/env bash
#
# City Pharma Agent — one-command production install.
#
#   ./deploy/aws/install.sh pharma.yourco.com admin@yourco.com
#
# Fresh Ubuntu box with Docker. Ends with a TLS-served stack, every secret
# randomly generated, CORS locked to your domain, SFTP on key-auth only, and a
# real embed credential minted and printed.
#
# Prints an OPENROUTER_API_KEY prompt if $OPENROUTER_API_KEY is not already set.
# It is the one thing that cannot be generated.
#
# Idempotent-ish: it refuses to overwrite an existing .env.prod (secrets you may
# already have handed out) unless you pass --force. Everything after that point
# is safe to re-run.

set -euo pipefail

# ---------------------------------------------------------------------------
# args
# ---------------------------------------------------------------------------

PROJECT=pharmacy
SFTP_PORT=2222
FORCE=0
DOMAIN=""
EMAIL=""

usage() {
    cat >&2 <<'USAGE'
usage: deploy/aws/install.sh <domain> <email> [--sftp-port N] [--force]

  <domain>   public hostname, already pointing at this box (A record).
             e.g. pharma.yourco.com
  <email>    contact address for Let's Encrypt expiry notices.
  --sftp-port N   public SFTP port for partner uploads (default 2222).
  --force         overwrite an existing .env.prod. This ROTATES every secret,
                  including the admin password and the Postgres password — and
                  the Postgres one will NOT match the already-initialised
                  database. Read the README before using it.

  OPENROUTER_API_KEY is read from the environment, or prompted for.
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force)     FORCE=1; shift ;;
        --sftp-port) SFTP_PORT="${2:-}"; shift 2 ;;
        -h|--help)   usage ;;
        -*)          echo "unknown option: $1" >&2; usage ;;
        *)
            if   [ -z "$DOMAIN" ]; then DOMAIN="$1"
            elif [ -z "$EMAIL" ];  then EMAIL="$1"
            else echo "unexpected argument: $1" >&2; usage
            fi
            shift ;;
    esac
done

[ -n "$DOMAIN" ] && [ -n "$EMAIL" ] || usage

# Let's Encrypt issues for DNS names only. An IP or 'localhost' would fail ACME
# after a full install — catch it in the first second instead.
case "$DOMAIN" in
    localhost|*[!a-zA-Z0-9.-]*)
        echo "ERROR: '$DOMAIN' is not a valid public hostname." >&2; exit 1 ;;
    *.*) : ;;
    *)  echo "ERROR: '$DOMAIN' has no dot — pass the real public domain, e.g. pharma.yourco.com" >&2; exit 1 ;;
esac
if printf '%s' "$DOMAIN" | grep -Eq '^[0-9.]+$'; then
    echo "ERROR: '$DOMAIN' is an IP address. Let's Encrypt cannot certify an IP — point a DNS A record at this box and use that name." >&2
    exit 1
fi
case "$EMAIL" in
    *@*.*) : ;;
    *)     echo "ERROR: '$EMAIL' is not an email address." >&2; exit 1 ;;
esac
case "$SFTP_PORT" in
    ''|*[!0-9]*) echo "ERROR: --sftp-port must be a number, got '$SFTP_PORT'." >&2; exit 1 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env.prod"
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENV_FILE"
         -f docker-compose.yml -f docker-compose.prod.yml)

say()  { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[1;31mFAILED:\033[0m %s\n\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. preflight — fail here, not half way through a boot
# ---------------------------------------------------------------------------

say "Preflight"

[ "$(id -u)" -ne 0 ] || warn "running as root; the stack does not need it"

command -v docker >/dev/null 2>&1 \
    || die "docker is not installed. See the README (Install Docker)."
docker compose version >/dev/null 2>&1 \
    || die "'docker compose' (v2) is not available. The legacy 'docker-compose' binary will not do."
docker info >/dev/null 2>&1 \
    || die "cannot talk to the Docker daemon. Is it running, and is your user in the 'docker' group? (newgrp docker)"
command -v openssl >/dev/null 2>&1 \
    || die "openssl is not installed — every secret in this deploy comes from 'openssl rand'."
command -v curl >/dev/null 2>&1 \
    || die "curl is not installed; the installer needs it to health-check and to mint the embed credential."
ok "docker, compose v2, openssl, curl"

# Ports. Caddy needs 80 + 443; partners need the SFTP port. Something already
# listening means the bind fails LATER, half way through `up`, with a message
# that does not name the culprit — so check now.
port_busy() {
    local p="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -Hltn "sport = :$p" 2>/dev/null | grep -q . && return 0
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$p" -sTCP:LISTEN -n -P >/dev/null 2>&1 && return 0
    else
        warn "neither ss nor lsof found — cannot check whether port $p is free"
    fi
    return 1
}

# If OUR stack is what is holding the ports, that is fine: this is a re-run.
ours_up=0
if [ -n "$(docker compose -p "$PROJECT" ps -q 2>/dev/null || true)" ]; then
    ours_up=1
    ok "an existing '$PROJECT' stack is running — treating this as a re-run"
fi

if [ "$ours_up" -eq 0 ]; then
    for p in 80 443 "$SFTP_PORT"; do
        if port_busy "$p"; then
            die "port $p is already in use. Free it (a stray nginx/apache is the usual cause: 'sudo systemctl stop nginx'), or pass --sftp-port for a different SFTP port."
        fi
    done
    ok "ports 80, 443, $SFTP_PORT are free"
fi

if [ "$SFTP_PORT" = "22" ]; then
    die "--sftp-port 22 collides with the box's own SSH daemon. You would lock yourself out. Pick another (2222 is the default)."
fi

if [ -f "$ENV_FILE" ] && [ "$FORCE" -eq 0 ]; then
    die "$ENV_FILE already exists.

This box looks installed. Re-running would mint NEW secrets — a new admin
password, and a new Postgres password that will NOT match the database that is
already on disk (initdb reads POSTGRES_PASSWORD exactly once, at creation).

  * to update the code:            ./deploy/aws/update.sh
  * to genuinely start over:       ./deploy/aws/install.sh $DOMAIN $EMAIL --force
                                   (and read the README section on --force first)"
fi

# ---------------------------------------------------------------------------
# 2. the one secret we cannot invent
# ---------------------------------------------------------------------------

say "OpenRouter API key"

OR_KEY="${OPENROUTER_API_KEY:-}"
if [ -z "$OR_KEY" ]; then
    if [ -t 0 ]; then
        printf '    The agent cannot answer a single question without this.\n'
        printf '    Get one at https://openrouter.ai/keys\n\n'
        printf '    OPENROUTER_API_KEY: '
        read -rs OR_KEY
        printf '\n'
    else
        die "OPENROUTER_API_KEY is not set and there is no terminal to prompt on.
Run:  OPENROUTER_API_KEY=sk-or-... ./deploy/aws/install.sh $DOMAIN $EMAIL"
    fi
fi
[ -n "$OR_KEY" ] || die "no OpenRouter key given. The install cannot continue."
case "$OR_KEY" in
    sk-or-*) ok "key accepted (sk-or-…${OR_KEY: -4})" ;;
    *)       warn "that does not look like an OpenRouter key (they start 'sk-or-'). Continuing anyway." ;;
esac

# ---------------------------------------------------------------------------
# 3. generate every secret
# ---------------------------------------------------------------------------

say "Generating secrets"

# Alphanumeric on purpose. A '$' or '#' in an env file is a live grenade:
# docker compose interpolates '$' and truncates at an unquoted '#'. Entropy is
# preserved by length, not by punctuation.
randstr() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-"${1:-32}"; }

SECRET_KEY="$(openssl rand -hex 32)"      # 32 bytes, as the HMAC signer expects
ADMIN_TOKEN="$(randstr 40)"
ADMIN_PASSWORD="$(randstr 24)"
POSTGRES_PASSWORD="$(randstr 32)"
ADMIN_EMAIL="admin@${DOMAIN}"
EMBED_ID="pharma-$(openssl rand -hex 4)"
EMBED_KEY="$(randstr 40)"

ok "SECRET_KEY, ADMIN_TOKEN, ADMIN_PASSWORD, POSTGRES_PASSWORD"

# ---------------------------------------------------------------------------
# 4. write .env.prod (0600 BEFORE the secrets go in)
# ---------------------------------------------------------------------------

say "Writing .env.prod"

umask 077
: > "$ENV_FILE"
chmod 600 "$ENV_FILE"

cat > "$ENV_FILE" <<EOF
# Generated by deploy/aws/install.sh on $(date -u '+%Y-%m-%d %H:%M:%S UTC')
# Domain: ${DOMAIN}
#
# EVERY secret below is unique to this box. There is no default anywhere in this
# file. Mode 0600, git-ignored. If you lose it you lose admin access and the
# database password — back it up somewhere else.

OPENROUTER_API_KEY=${OR_KEY}
OPENROUTER_MODEL=google/gemini-3.5-flash
EMBEDDING_MODEL=google/gemini-embedding-2

PHARMA_DOMAIN=${DOMAIN}
ACME_EMAIL=${EMAIL}

# Locked to this host. NOT "*" — a wildcard would let any site on the internet
# drive the embed API from a visitor's browser. Add customer domains that embed
# the widget, comma-separated, then: docker compose ... up -d api
ALLOWED_ORIGINS=https://${DOMAIN}
EMBED_PUBLIC_BASE=https://${DOMAIN}
COOKIE_SECURE=true

SECRET_KEY=${SECRET_KEY}
ADMIN_TOKEN=${ADMIN_TOKEN}
ADMIN_EMAIL=${ADMIN_EMAIL}
ADMIN_PASSWORD=${ADMIN_PASSWORD}

POSTGRES_PASSWORD=${POSTGRES_PASSWORD}

# No dev 'web'/'web' credential in prod. install.sh mints a real one over the
# admin API after boot; nothing else seeds one. Wipe the redis volume and every
# embed stops authenticating (fail-closed) until you re-mint.
EMBED_DEV_CREDENTIAL=false

# SFTP: KEY AUTH ONLY. The user is created with an empty password field, which
# disables password login — so there is no password to put here.
SFTP_PASSWORD=
SFTP_PUBLIC_HOST=${DOMAIN}
SFTP_PUBLIC_PORT=${SFTP_PORT}
SFTP_USERNAME=pharma
SFTP_KEYS_DIR=/sftp_ssh

CACHE_TTL_SECONDS=600
RATE_LIMIT_PER_MIN=30
SESSION_TTL_SECONDS=900
LOG_LEVEL=INFO

HISTORY_ENABLED=true
HISTORY_TURNS=3

# Hot-intent fast path: measured ~9.8s -> ~5.8s median on "do I have X" /
# "who else has X". Falls through to the full agent when a drug cannot be
# resolved unambiguously.
FAST_PATH_ENABLED=true
# KEEP OFF — see CLAUDE.md. Cosine cannot separate "same question" from
# "different strength", and a false hit serves the wrong drug's stock.
SEMANTIC_CACHE_ENABLED=false
LEARNING_ENABLED=false
ROUTER_SPLIT_ENABLED=false
ROUTER_MODEL=google/gemini-2.5-flash-lite

OIDC_ENABLED=false
LDAP_ENABLED=false
EOF

ok "$ENV_FILE (0600)"

# ---------------------------------------------------------------------------
# 5. seed the SFTP key volume BEFORE the sftp container ever starts
# ---------------------------------------------------------------------------
#
# atmoz/sftp rebuilds authorized_keys from .ssh/keys/* at container creation,
# with `set -e` and no nullglob. If that directory EXISTS but is EMPTY, the glob
# stays literal, `cat` fails on it, and the container dies on boot. The api
# creates keys/ eagerly, so that state is reachable on a first boot where the two
# containers race. Seed the volume now so it never happens.

say "Seeding the SFTP key volume"

VOL="${PROJECT}_sftp_ssh"
docker volume create "$VOL" >/dev/null
docker run --rm -v "$VOL:/s" alpine:3 sh -c '
    set -e
    mkdir -p /s/keys
    # Deliberately not a dotfile: atmoz globs keys/*, which does not match
    # dotfiles, so a .placeholder would not save it. See sftp/chown.sh.
    [ -f /s/keys/00-placeholder ] || \
        printf "# placeholder - keeps keys/* matching; see sftp/chown.sh\n" > /s/keys/00-placeholder
    [ -f /s/authorized_keys ] || : > /s/authorized_keys
    chown -R 1001:1001 /s
    chmod 0700 /s /s/keys
    chmod 0600 /s/authorized_keys
' >/dev/null
ok "$VOL — .ssh 0700, authorized_keys 0600, owned 1001:1001"

# ---------------------------------------------------------------------------
# 6. build + boot
# ---------------------------------------------------------------------------

say "Building images (the admin SPA is compiled INTO the api image — this takes a few minutes)"
"${COMPOSE[@]}" build api ingest-worker
ok "images built"

say "Starting the stack"
"${COMPOSE[@]}" up -d
ok "containers up"

# ---------------------------------------------------------------------------
# 7. wait for health
# ---------------------------------------------------------------------------

say "Waiting for the API to come up"

# Loopback, not the domain: this proves the APP is healthy without also waiting
# on DNS propagation and an ACME round trip. TLS is checked separately below.
HEALTH="http://127.0.0.1:8088/health"
for i in $(seq 1 60); do
    if curl -fsS --max-time 3 "$HEALTH" >/dev/null 2>&1; then
        ok "/health is answering (after ${i}s)"
        break
    fi
    if [ "$i" -eq 60 ]; then
        printf '\n--- api logs (last 40) ---\n' >&2
        "${COMPOSE[@]}" logs --tail 40 api >&2 || true
        die "the API never became healthy. Logs above. Nothing has been rolled back — fix, then re-run with --force or just 'docker compose ... up -d'."
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 8. mint a REAL embed credential
# ---------------------------------------------------------------------------
#
# EMBED_DEV_CREDENTIAL=false means the dev web/web pair does not exist and
# is_valid_credential() rejects everything that is not registered. Without this
# step the widget cannot authenticate at all — the deploy would look fine and the
# embed would be dead.

say "Minting an embed credential"

TOKEN="$(curl -fsS --max-time 10 -X POST "http://127.0.0.1:8088/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\"}" \
    | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"

[ -n "$TOKEN" ] || die "could not log in as ${ADMIN_EMAIL} to mint the embed credential.
The stack is UP but the widget will not authenticate until a credential exists.
Fix the login, then:
  curl -X POST https://${DOMAIN}/admin/credentials -H 'Authorization: Bearer <jwt>' \\
       -H 'Content-Type: application/json' \\
       -d '{\"embed_id\":\"${EMBED_ID}\",\"public_key\":\"${EMBED_KEY}\"}'"

curl -fsS --max-time 10 -X POST "http://127.0.0.1:8088/admin/credentials" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    -d "{\"embed_id\":\"${EMBED_ID}\",\"public_key\":\"${EMBED_KEY}\"}" >/dev/null \
    || die "credential mint failed. The stack is up but no embed can authenticate."

ok "embed credential registered: ${EMBED_ID}"

# ---------------------------------------------------------------------------
# 9. TLS check — advisory, not fatal
# ---------------------------------------------------------------------------

say "Checking TLS"
if curl -fsS --max-time 20 "https://${DOMAIN}/health" >/dev/null 2>&1; then
    ok "https://${DOMAIN}/health is answering — certificate issued"
else
    warn "https://${DOMAIN}/health did not answer yet."
    warn "This is EXPECTED for a minute or two, and is not a failed install:"
    warn "  * the DNS A record for ${DOMAIN} must point at this box's public IP,"
    warn "  * the security group must allow inbound 80 AND 443 (80 is how"
    warn "    Let's Encrypt validates — closing it means no certificate, ever),"
    warn "  * then Caddy issues the cert on the first request."
    warn "Watch it:  docker compose -p ${PROJECT} logs -f caddy"
fi

# ---------------------------------------------------------------------------
# 10. summary
# ---------------------------------------------------------------------------

cat <<EOF

  ────────────────────────────────────────────────────────────────────────
   City Pharma Agent is installed.
  ────────────────────────────────────────────────────────────────────────

   ADMIN CONSOLE   https://${DOMAIN}/admin
     email         ${ADMIN_EMAIL}
     password      ${ADMIN_PASSWORD}

   This password is shown ONCE. It is in .env.prod (0600) and nowhere else.
   Save it now, then change it from the admin console.

   EMBED CREDENTIAL   (the dev web/web pair does NOT exist in prod)
     embed_id      ${EMBED_ID}
     public_key    ${EMBED_KEY}

     <script src="https://${DOMAIN}/api/embed/widget.js"
             data-embed-id="${EMBED_ID}"
             data-public-key="${EMBED_KEY}"></script>

     Any customer site you paste that into must ALSO be added to
     ALLOWED_ORIGINS in .env.prod — it is locked to https://${DOMAIN} today,
     and the browser will block the call from anywhere else.

   SFTP (partner uploads)     KEY AUTH ONLY — passwords are disabled
     host          ${DOMAIN}
     port          ${SFTP_PORT}
     user          pharma
     drop files    article*.xlsx / balance*.xlsx into upload/

   NEXT STEP — register a partner's public key. They send you their PUBLIC
   key (id_ed25519.pub — never the private one). Then, in the admin console:

     Data -> SFTP -> Add key,  paste it, save.

   It is live immediately: sshd re-reads authorized_keys on every connection,
   so nothing needs restarting. Verify from their machine:

     sftp -P ${SFTP_PORT} pharma@${DOMAIN}

   ROUTINE OPS
     update      ./deploy/aws/update.sh
     backup      ./deploy/aws/backup.sh
     logs        docker compose -p ${PROJECT} logs -f api
     runbook     deploy/aws/README.md

  ────────────────────────────────────────────────────────────────────────

EOF
