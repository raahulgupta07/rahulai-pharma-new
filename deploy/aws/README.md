# Deploying City Pharma Agent on AWS

One command on a fresh Ubuntu EC2 box:

```bash
./deploy/aws/install.sh pharma.yourco.com admin@yourco.com
```

You end up with the API behind TLS on your domain, an admin console, an embed
credential that actually works, and an SFTP drop point that partners can only
reach with an SSH key. Every secret is generated on the box; nothing is left at
a dev default.

---

## 1. Before you run it

### The box

`t3.small` is the realistic floor (2 GB RAM). The image build compiles the admin
SPA with node, and Postgres + Redis + the agent share the box. `t3.micro` (1 GB)
will OOM during the build.

Ubuntu 22.04 or 24.04, 20 GB disk.

### Security group

Inbound:

| Port | Source | Why |
|------|--------|-----|
| 22 | **your IP only** | your SSH. Never `0.0.0.0/0`. |
| 80 | `0.0.0.0/0` | **Let's Encrypt validates over HTTP-01 on port 80.** Close it and you never get a certificate — not at install, not at renewal 60 days later. It also redirects human traffic to https. |
| 443 | `0.0.0.0/0` | the app. |
| 2222 | **partner IPs if you know them**, else `0.0.0.0/0` | SFTP uploads. |

Outbound: all (the agent calls OpenRouter; Caddy calls Let's Encrypt).

Port 80 is the one people close "to be safe" and then spend an afternoon
wondering why TLS never came up.

### DNS

An **A record** for `pharma.yourco.com` → the box's public IP (use an Elastic IP,
or the record breaks on the next stop/start). Let it propagate *before* you
install — Caddy asks Let's Encrypt for a certificate the moment it starts, and a
failed attempt counts against a rate limit.

Check it:

```bash
dig +short pharma.yourco.com     # must print the box's IP
```

### Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
```

The installer checks for `docker compose` **v2**. The old `docker-compose`
(hyphen) binary from apt will not work — this repo uses `!override`, which v1
does not understand.

### An OpenRouter key

The one thing the installer cannot invent. <https://openrouter.ai/keys>. It will
prompt, or take `$OPENROUTER_API_KEY` from the environment.

---

## 2. Install

```bash
git clone <this repo> pharmacy-agent && cd pharmacy-agent
./deploy/aws/install.sh pharma.yourco.com admin@yourco.com
```

It will:

1. **Preflight** — Docker, compose v2, openssl, curl; ports 80/443/2222 free;
   refuse to overwrite an existing `.env.prod` (see `--force` below).
2. **Prompt for the OpenRouter key.**
3. **Generate every secret** with `openssl rand` and write `.env.prod`, mode
   `0600`.
4. **Seed the SFTP key volume** (see "the empty-directory landmine" below).
5. **Build** the api image — this compiles the admin SPA *into* it — and start
   everything.
6. **Wait for `/health`.**
7. **Mint a real embed credential** over the admin API and print it.
8. **Print a summary** with the admin password, the embed credential and the SFTP
   details.

Takes about 5 minutes, most of it the image build.

**Save the summary.** The admin password is printed once. It is in `.env.prod`
and nowhere else.

If TLS is not up when the installer finishes, that is normal — it says so, and
it is not a failed install. Caddy issues the certificate on the first real
request. Watch it:

```bash
docker compose -p pharmacy logs -f caddy
```

---

## 3. What the installer generated, and what it derived

**Generated** (`openssl rand`, unique to your box, no default anywhere):

| Variable | What it is |
|----------|------------|
| `SECRET_KEY` | 32 bytes hex. Signs admin JWTs and embed session tokens. **If you sign embed users from a Laravel app, this must equal that app's `CITYAGENT_SECRET_KEY`** — set both to the same value or the HMAC check fails. |
| `ADMIN_PASSWORD` | the seed super-admin. Printed once. |
| `ADMIN_TOKEN` | legacy `/admin` gate, superseded by JWT auth. Generated anyway so it is never the empty default. |
| `POSTGRES_PASSWORD` | the database. |
| the embed `public_key` | minted post-boot, not stored in `.env.prod`. |

**Derived from your domain:**

| Variable | Value |
|----------|-------|
| `ALLOWED_ORIGINS` | `https://<domain>` — **not `*`.** A wildcard lets any site on the internet drive the embed API from a visitor's browser. |
| `EMBED_PUBLIC_BASE` | `https://<domain>` |
| `SFTP_PUBLIC_HOST` / `SFTP_PUBLIC_PORT` | `<domain>` / `2222`. The api container cannot discover its own public address, so this is told to it, not inferred. |
| `EMBED_DEV_CREDENTIAL` | `false` — see below. |
| `COOKIE_SECURE` | `true` (we are behind TLS now). |

### `EMBED_DEV_CREDENTIAL=false` is why the installer mints a credential

In dev, the app seeds a `web`/`web` credential when the credential store is
empty. In prod that flag is off, and `is_valid_credential()` is **fail-closed**:
it rejects every `(embed_id, public_key)` that is not registered, including when
*none* are. So without step 7 the widget could not authenticate at all — the
deploy would look perfectly healthy and every embed would be dead.

The credential lives in **Redis**. Prod runs Redis with `appendonly yes` on a
named volume for exactly this reason. Wipe that volume and you lose the
credential with it; nothing re-seeds it. Re-mint:

```bash
TOKEN=$(curl -fsS -X POST https://pharma.yourco.com/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@pharma.yourco.com","password":"<from .env.prod>"}' | jq -r .token)

curl -fsS -X POST https://pharma.yourco.com/admin/credentials \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"embed_id":"pharma-abcd1234","public_key":"<openssl rand -base64 30>"}'
```

### Embedding on a customer's site

The snippet is in the installer's summary. Any customer domain you paste it into
must **also** be added to `ALLOWED_ORIGINS` in `.env.prod`, comma-separated, or
the browser blocks the call:

```bash
ALLOWED_ORIGINS=https://pharma.yourco.com,https://customer-one.com
docker compose -p pharmacy --env-file .env.prod \
  -f docker-compose.yml -f docker-compose.prod.yml up -d api
```

---

## 4. SFTP: registering a partner's key

**Prod is key-auth only. There is no SFTP password.** The user is created with an
empty password field, which makes atmoz run `usermod -p "*"` — the account has no
usable password, so password authentication cannot succeed. `sftp/sshd_harden.sh`
additionally sets `PasswordAuthentication no` in `sshd_config`, so the guarantee
does not rest on one field in a compose command line.

*(The **dev** compose keeps password auth so local testing stays easy. That is the
only difference in this area between the two.)*

The partner sends you their **public** key — `id_ed25519.pub`, one line starting
`ssh-ed25519 AAAA...`. Never their private key; if they offer one, they have
misunderstood, and the key should be regenerated.

Register it in the admin console: **Data → SFTP → Add key**, paste, save.

It is live **immediately** — sshd re-reads `authorized_keys` on every incoming
connection, so nothing needs restarting. The partner tests with:

```bash
sftp -P 2222 pharma@pharma.yourco.com
sftp> cd upload
sftp> put balance_2026_07.xlsx
```

The ingest worker picks up `article*.xlsx` / `balance*.xlsx` from `upload/`
within ~15 seconds, loads them, and busts the answer cache.

### How the keys survive a rebuild

The api and the sftp container share a Docker volume (`sftp_ssh`): it is
`/sftp_ssh` to the api and `/home/pharma/.ssh` to sshd. The api appends each key
to `authorized_keys` **and** mirrors the `.pub` into `keys/`. The mirror is not
redundant — when the sftp container is *recreated* (not merely restarted), atmoz
regenerates `authorized_keys` from `keys/*`, and any key that only existed in
`authorized_keys` would vanish.

`sftp/chown.sh` re-applies ownership and permissions on every boot (`.ssh` `0700`,
`authorized_keys` `0600`, owned `1001:1001`). Docker creates volumes root-owned
`0755`, and sshd's StrictModes **silently ignores** an `authorized_keys` it does
not like — key auth just fails, with nothing useful in the log.

### The empty-directory landmine

atmoz rebuilds `authorized_keys` with `cat keys/*` under `set -e` and no
`nullglob`. If `keys/` **exists but is empty**, the glob stays literal, `cat`
fails, and **the container dies on boot**. Reachable in normal use: delete the
last partner key and the next recreate refuses to start.

So `keys/` always holds a file called `00-placeholder`, created by the installer
and re-created by `chown.sh` on every boot. It must not be a dotfile — `*` does
not match dotfiles, and a `.placeholder` does not save it. Its content is a `#`
comment line, which sshd ignores in `authorized_keys`.

Verified against the image, not assumed. Do not "tidy" the placeholder away.

---

## 5. Update

```bash
./deploy/aws/update.sh
```

`git pull` (if a remote exists — this repo may have none, and that is fine),
rebuild, restart, health-check, and **roll back automatically** if the new build
is unhealthy. Data volumes are never touched.

**Both the backend and the admin SPA are baked into the api image.** There is no
source mount anywhere in the compose files. Editing a `.py` or a `.svelte` on the
box changes nothing until you rebuild — `update.sh` is how you deploy, not
`docker restart`.

The previous image stays tagged `pharmacy-agent:rollback`. To go back by hand:

```bash
docker image tag pharmacy-agent:rollback pharmacy-agent:latest
docker compose -p pharmacy --env-file .env.prod \
  -f docker-compose.yml -f docker-compose.prod.yml up -d api ingest-worker
```

---

## 6. Backup

```bash
./deploy/aws/backup.sh              # -> ./backups/<timestamp>/
```

Postgres (`pg_dump`, consistent, no downtime), the Redis `.rdb` (**the embed
credentials**), the SFTP keys, and `.env.prod`. Each backup directory contains a
`RESTORE.md` written for that snapshot.

The backup contains every secret on the box in clear. **Move it off the box** —
S3 with SSE, or a password manager. A backup sitting next to the thing it backs
up protects you from nothing.

Nightly, at 03:00:

```bash
crontab -e
0 3 * * * cd /home/ubuntu/pharmacy-agent && ./deploy/aws/backup.sh >> /var/log/pharma-backup.log 2>&1
```

Nothing prunes old backups. Add `find backups/ -maxdepth 1 -type d -mtime +14
-exec rm -rf {} +` if the disk matters to you.

---

## 7. Rotating the OpenRouter key

Do this if the key has ever been shared, pasted into a chat, or committed.

1. Mint a new key at <https://openrouter.ai/keys>.
2. Edit `.env.prod`, replace `OPENROUTER_API_KEY=`.
3. Recreate the containers that read it:

   ```bash
   docker compose -p pharmacy --env-file .env.prod \
     -f docker-compose.yml -f docker-compose.prod.yml up -d api ingest-worker
   ```

   `up -d` after an env change **recreates** the container, which is what you
   want. `docker restart` does **not** re-read the env file and will keep using
   the old key.
4. Confirm from the admin chat, then **revoke the old key** at OpenRouter. Not
   revoking it is the whole point of the rotation left undone.

Same procedure for any other value in `.env.prod` — **except `POSTGRES_PASSWORD`,
which cannot be rotated this way.** `initdb` reads it exactly once, when the data
directory is created; changing it afterwards makes the app unable to log in to
its own database. To actually rotate it: `ALTER USER pharmacy PASSWORD '...'`
inside Postgres, *then* update `.env.prod` to match.

---

## 8. What is NOT automated

Be honest with yourself about this list before you call the deploy done.

- **Accuracy is unmeasured.** `evals/run_eval.py` has never been run in this
  repo. There is no per-question pass/fail record for this agent, anywhere. The
  installer deploys a system whose *correctness* nobody has graded. Do not quote
  a number to a customer; there isn't one.
- **Nobody has opened the authenticated admin UI in a browser.** Health checks
  prove the HTML shell is served, not that the SPA's JavaScript runs. An
  unimported icon in a `.svelte` file blanks the *entire* console at runtime, and
  neither the build nor `svelte-check` catches it. **Open `/admin`, log in, and
  click through the pages** after every deploy. This has bitten before.
- **No data is loaded.** A fresh install has an empty catalog and empty
  inventory. Upload `article*.xlsx` / `balance*.xlsx` via SFTP or the admin Data
  page. Until then the agent answers every stock question with nothing.
- **Embeddings are not built** for a fresh catalog, so semantic search
  (`search_by_meaning`) returns nothing until the first ingest embeds it.
- **`ALLOWED_ORIGINS` covers only your own domain.** Every customer site that
  embeds the widget must be added by hand.
- **SSO/LDAP are off.** Configure them in the admin console (Configuration →
  Authentication) or in `.env.prod`; see `docs/SSO.md`. Point them at real
  servers and *test a real login* — both have shipped with auth bypasses before.
- **No off-box backups, no retention, no monitoring, no alerting, no log
  shipping.** `backup.sh` writes to local disk and nothing rotates it. If the
  EBS volume dies, everything dies with it.
- **No WAF, no rate limiting beyond the app's own `RATE_LIMIT_PER_MIN=30`**, and
  that is per-client-key, not per-IP. The SFTP port is exposed to whatever CIDR
  you allowed.
- **The SFTP host key lives in a Docker volume** (`sftp_hostkeys`). It survives
  container recreation, which is the point — but `docker compose down -v` destroys
  it, and every partner then gets a "REMOTE HOST IDENTIFICATION HAS CHANGED"
  warning on their next connection. Back that volume up too if partners are
  fussy, and warn them if you ever rebuild from scratch.
- **`--force` re-runs are dangerous.** It rotates every secret, including
  `POSTGRES_PASSWORD` — which will then no longer match the already-initialised
  database, and the api will fail to connect. Use it only on a box you are
  genuinely wiping (`docker compose ... down -v` first).
- **Nothing in the stack is highly available.** One box, one Postgres, no
  replica. It reboots, it is down for the reboot.

---

## 9. Troubleshooting

```bash
# is anything actually running?
docker compose -p pharmacy ps

# api logs
docker compose -p pharmacy logs -f api

# TLS / certificate problems live here
docker compose -p pharmacy logs -f caddy

# the app, bypassing Caddy entirely (proves whether TLS is the problem)
curl -fsS http://127.0.0.1:8088/health
curl -fsS http://127.0.0.1:8088/ready     # {catalog_rows, inventory_rows, sites}
```

**No certificate.** Port 80 blocked in the security group, or DNS not pointing
here. Both are outside Docker; the `caddy` logs will name which.

**The chat answers all at once instead of streaming.** Something is buffering the
SSE response. The `Caddyfile` handles `/api/embed/chat/stream` in its own block
with `flush_interval -1` and upstream compression off, precisely to stop this. If
you put another proxy (an ALB, CloudFront, nginx) in front of Caddy, you must
disable response buffering there too — this is the failure that has bitten a
sibling project.

**SFTP: "Permission denied (publickey)".** The key is not in `authorized_keys`,
or the permissions are wrong and sshd is ignoring the file:

```bash
docker compose -p pharmacy exec sftp ls -la /home/pharma/.ssh
# expect: .ssh drwx------ (0700), authorized_keys -rw------- (0600), owner 1001
docker compose -p pharmacy exec sftp cat /home/pharma/.ssh/authorized_keys
```

`chown.sh` fixes those permissions on every boot, so a restart of the `sftp`
service is a legitimate first move.

**Answers are stale after loading new stock.** Anything that writes stock must
bump `data_version` or cached answers survive for up to `CACHE_TTL_SECONDS`
(600). The SFTP watcher and the admin upload both bump it. **A direct SQL write to
Postgres does not** — the app cannot see it. After one, call:

```bash
curl -X POST https://pharma.yourco.com/api/embed/reload
```
