# City Pharma Agent — Installation Guide

Two ways to install:

- **[A. Production on AWS](#a-production-on-aws)** — one command, TLS, every secret generated. What you deploy for real.
- **[B. Local development](#b-local-development)** — two Docker Compose stacks on your machine for testing.

The app is a FastAPI + Agno agent over Postgres/pgvector + Redis, with an SFTP
drop-folder ingest worker and a SvelteKit admin console baked into the image.

---

## A. Production on AWS

### 1. What you need first

| Thing | Detail |
|-------|--------|
| An EC2 box | Ubuntu 22.04+, 2 vCPU / 4 GB RAM minimum, Docker installed |
| A domain | e.g. `pharma.yourco.com`, with a DNS **A record** pointing at the box's public IP |
| An OpenRouter API key | From <https://openrouter.ai/keys>. The one secret the installer cannot generate. |
| Security-group rules | See below |

**Security group (inbound):**

| Port | Why |
|------|-----|
| 443 | HTTPS (the app) |
| 80 | HTTP → Let's Encrypt uses it to issue the TLS cert. **Required**, even though the app is HTTPS-only. |
| 2222 (or your `--sftp-port`) | Partner SFTP uploads |
| 22 | Your SSH admin access |

### 2. Install Docker (if not already)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
```

### 3. Clone and run the installer

```bash
git clone git@github.com:raahulgupta07/rahulai-pharma-new.git
cd rahulai-pharma-new
export OPENROUTER_API_KEY=sk-or-...        # or the installer will prompt
./deploy/aws/install.sh pharma.yourco.com admin@yourco.com
```

`install.sh <domain> <email> [--sftp-port N] [--force]`

That one command:

1. **Preflight** — checks Docker, Compose v2, openssl, curl, and that ports 80/443/SFTP are free. Fails here rather than half-way through a boot.
2. **Generates every secret** with `openssl rand` — `SECRET_KEY`, `ADMIN_PASSWORD`, `ADMIN_TOKEN`, `POSTGRES_PASSWORD`, `SFTP_PASSWORD`. Writes `.env.prod` at `0600`.
3. **Locks config to your domain** — `ALLOWED_ORIGINS=https://<domain>` (not `*`), `EMBED_DEV_CREDENTIAL=false`, `SFTP_PUBLIC_HOST=<domain>`.
4. **Brings up the stack behind Caddy** with automatic Let's Encrypt TLS (SSE streaming kept unbuffered).
5. **Waits for health**, then **mints a real embed credential** and prints it.
6. **Prints a summary**: admin URL, admin email + generated password, embed credential, SFTP details, and how to register a partner key.

> The installer refuses to overwrite an existing `.env.prod` unless you pass
> `--force` (which **rotates every secret** and breaks the existing DB password).

### 4. After it finishes

- Open `https://pharma.yourco.com/admin` and log in with the printed admin email + password.
- **Change the admin password** on first login.
- The catalog is **empty** until your first data upload — see [Loading data](#loading-data).

### Updating

```bash
./deploy/aws/update.sh
```

Pulls (if a remote is set), rebuilds the api + worker images (the admin SPA is
baked in), restarts, health-checks, and **rolls back** to the previous image if
health fails.

### Backups

```bash
./deploy/aws/backup.sh
```

`pg_dump` + the Redis dump (embed credentials) + the SFTP keys + `.env.prod`,
timestamped into `./backups/<ts>/` with a per-snapshot `RESTORE.md`.

---

## B. Local development

Two side-by-side stacks for benchmarking:

| Stack | Compose | Admin | Postgres | Redis | SFTP |
|-------|---------|-------|----------|-------|------|
| baseline | `docker-compose.yml` | :8088 | :5433 | :6380 | :2222 |
| optimize | `+ docker-compose.optimized.yml` | :8091 | :5434 | :6381 | :2223 |

### 1. Config

```bash
cp .env.example .env
# edit .env: set OPENROUTER_API_KEY=sk-or-... (the rest have working dev defaults)
```

### 2. Bring up the baseline stack

```bash
docker compose up -d --build
# admin at http://localhost:8088/admin
```

### Optimize stack (fast-path enabled)

```bash
docker compose -p pharmacy-opt \
  -f docker-compose.yml -f docker-compose.optimized.yml up -d --build
# admin at http://localhost:8091/admin
```

Default dev login: `admin@citcare.local` / `changeme` (set in `.env`).

### Deploy a code change (dev)

The backend **and** the built admin SPA are baked into the image — there is no
source volume mount. After editing:

```bash
cd admin && ./node_modules/.bin/vite build && cd ..
docker compose -p pharmacy-opt -f docker-compose.yml -f docker-compose.optimized.yml build api
docker compose -p pharmacy-opt -f docker-compose.yml -f docker-compose.optimized.yml up -d api
```

### Run the tests

```bash
./venv/bin/python -m pytest -q          # 211 passed, 4 skipped (skips need a live key)
./venv/bin/python -m evals.run_eval     # accuracy: 15/15
EVAL_SET=eval_set_v2.json ./venv/bin/python -m evals.run_eval   # 34/34
```

---

## Loading data

The agent answers from two source files, matched **by filename**:

| Filename contains | Loads as | Behavior |
|-------------------|----------|----------|
| `article` | catalog | **Full sync** by default — the file is authoritative; drops any drug it omits (empty/partial file deletes nothing). Switch to *Merge* on the SFTP page to keep discontinued drugs. |
| `balance` / `stock` / `inventory` | inventory | **Full replace** — each file is a complete stock snapshot. |

`.csv` or `.xlsx` only. Three ways in:

1. **SFTP** (partners): push into `upload/`. The worker ingests within the poll
   interval, embeds new rows, rebuilds the drug graph, busts the cache.
2. **Admin UI**: SFTP page → *Upload xlsx*.
3. **Manual trigger**: SFTP page → *Ingest now*.

### Partner onboarding (production, key auth)

On the **SFTP uploads** page:

1. Set the **public backend host** (auto-detected; confirm it's what a partner dials).
2. Add the partner's **SSH public key** in the key manager. It works on their
   next connection — no restart. In production, password auth is off, so the key
   *is* the access.
3. Send them the copy-paste snippet (sftp / scp / cron / paramiko / WinSCP) and
   the filename rules, both on the page.

---

## Configuration reference

Set in `.env` (dev) or generated into `.env.prod` (production).

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | LLM + embeddings. The only secret you supply. |
| `OPENROUTER_MODEL` | Chat model (default `google/gemini-3.5-flash`). |
| `SECRET_KEY` | Signs sessions + embed `store_id`. **Must equal** the Laravel `CITYAGENT_SECRET_KEY` if you sign embed users. |
| `ALLOWED_ORIGINS` | CORS. Defaults to a safe localhost list; production sets your domain. `*` only if explicitly written. |
| `EMBED_DEV_CREDENTIAL` | Dev only. `true` seeds a `web`/`web` embed credential when the store is empty. **Production sets `false`** — then an unregistered credential is a 403. |
| `SFTP_PUBLIC_HOST` / `SFTP_PUBLIC_PORT` | What partners dial. |
| `POSTGRES_URL` / `REDIS_URL` | Datastores. |
| `OIDC_ENABLED` / `LDAP_ENABLED` | SSO — off by default; see `docs/SSO.md`. |

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| TLS never issues | Port 80 not open, or DNS A record not pointing at the box. Let's Encrypt needs both. |
| Widget returns 403 | In production `web`/`web` doesn't exist — use a real credential from the Embed page. |
| Partner file lands in `failed/` | Filename doesn't contain `article`/`balance`/`stock`/`inventory`, or isn't `.csv`/`.xlsx`. |
| Admin console is blank | An unimported icon (a past regression class). Run `python3 scripts/check_svelte_icons.py`. |
| CORS blocked after deploy | `ALLOWED_ORIGINS` no longer defaults to `*`; list your real domain(s). |

---

## What is NOT automated

- Rotating the OpenRouter key (do it if it leaks).
- DNS A record and the security-group rules — you create those in AWS.
- Off-box backups / retention / monitoring / WAF.
- `POSTGRES_PASSWORD` cannot be rotated via env once the DB is initialised.
- Single box, no high availability.
