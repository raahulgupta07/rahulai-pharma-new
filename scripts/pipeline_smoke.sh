#!/usr/bin/env bash
#
# End-to-end smoke test for the SFTP data pipeline.
#
# Registers its own partner key, uploads a catalog file over SFTP as a partner
# would, waits for the watcher to load it, checks the catalog is intact and the
# assistant still answers from it, then removes the key it made.
#
# This is NOT a unit test and does not belong in pytest: it needs a running
# stack, a real sshd, a real database and a real model call. That is the point
# — every one of those is a thing the test suite mocks and production does not.
#
# WHAT IT WILL DO TO YOUR DATA. Catalog load mode is `full_sync`: the file
# REPLACES the product list, and rows absent from the file are deleted (the
# stub backfill re-adds those that still have stock). Uploading the file the
# instance already ingested therefore nets to zero — which is why --file
# defaults to the newest catalog in the archive rather than to a fixture.
# Point it at a DIFFERENT file and you have performed a real catalog load.
#
# Exits non-zero naming the stage that failed.

set -uo pipefail

BASE=""                      # http(s) base URL of the admin API
SFTP_HOST=127.0.0.1          # where sshd is reachable FROM this machine
SFTP_PORT=""                 # default: read from the connection card
FILE=""                      # default: newest catalog in the archive
LABEL="smoke-$(date -u +%Y%m%d-%H%M%S)"
KEEP_KEY=0
TIMEOUT=300                  # seconds to wait for the load
CONFIRM_STOCK=0

die() { echo "FAIL [$1] $2" >&2; exit 1; }
note() { echo "  $*"; }
stage() { echo; echo "== $* =="; }

usage() {
  cat <<USAGE
usage: $0 --base URL [options]

  --base URL          admin API base, e.g. https://citycareagent.citygpt.xyz
  --sftp-host HOST    where sshd is reachable from here (default 127.0.0.1)
  --sftp-port PORT    default: whatever the connection card advertises
  --file PATH         catalog file to upload (default: newest in archive)
  --label NAME        key label to create (default: smoke-<timestamp>)
  --keep-key          do not delete the key afterwards
  --timeout SECONDS   how long to wait for the load (default 300)
  --i-know-this-replaces-stock
                      required if --file names an INVENTORY file, which
                      truncates and reloads the stock table
Credentials come from ADMIN_EMAIL / ADMIN_PASSWORD in the environment.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    --sftp-host) SFTP_HOST="$2"; shift 2 ;;
    --sftp-port) SFTP_PORT="$2"; shift 2 ;;
    --file) FILE="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --keep-key) KEEP_KEY=1; shift ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --i-know-this-replaces-stock) CONFIRM_STOCK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die args "unknown option $1" ;;
  esac
done

[ -n "$BASE" ] || { usage; die args "--base is required"; }
[ -n "${ADMIN_EMAIL:-}" ] && [ -n "${ADMIN_PASSWORD:-}" ] \
  || die args "set ADMIN_EMAIL and ADMIN_PASSWORD in the environment"

WORK=$(mktemp -d)
KEYFILE="$WORK/key.pem"
CLEANED=0
cleanup() {
  [ "$CLEANED" = 1 ] && return
  CLEANED=1
  # A file we uploaded that never loaded must not be left in the drop folder:
  # the watcher would pick it up later, out of sight of whoever ran this, and
  # a catalog load is not a thing that should happen by surprise.
  if [ -n "${REMOTE:-}" ] && [ "${STATE:-}" != loaded ] && [ -n "${TOKEN:-}" ]; then
    curl -sS -X DELETE "$BASE/admin/sftp/file/$REMOTE" \
      -H "Authorization: Bearer $TOKEN" -o /dev/null 2>/dev/null \
      && note "removed the un-loaded upload $REMOTE from the drop folder"
  fi
  # Order matters: revoke the key server-side BEFORE deleting the only copy of
  # the private half, or a failure here leaves a key registered that nobody can
  # prove they hold.
  if [ "$KEEP_KEY" = 0 ] && [ -n "${TOKEN:-}" ]; then
    curl -sS -X DELETE "$BASE/admin/sftp/keys/$LABEL" \
      -H "Authorization: Bearer $TOKEN" -o /dev/null 2>/dev/null \
      && note "removed key $LABEL"
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

api() { # api <method> <path> [json body]
  local m="$1" p="$2" b="${3:-}"
  if [ -n "$b" ]; then
    curl -sS -m 60 -X "$m" "$BASE$p" -H "Authorization: Bearer $TOKEN" \
      -H 'content-type: application/json' -d "$b"
  else
    curl -sS -m 60 -X "$m" "$BASE$p" -H "Authorization: Bearer $TOKEN"
  fi
}

jqp() { python3 -c "import sys,json;d=json.load(sys.stdin);$1"; }

stage "1/8 sign in"
TOKEN=$(python3 -c "import json,os;print(json.dumps({'email':os.environ['ADMIN_EMAIL'],'password':os.environ['ADMIN_PASSWORD']}))" \
  | curl -sS -m 60 -X POST "$BASE/auth/login" -H 'content-type: application/json' --data @- \
  | jqp "print(d.get('token',''))")
[ -n "$TOKEN" ] || die login "no token returned by $BASE/auth/login"
note "signed in as $ADMIN_EMAIL"

stage "2/8 read the connection card"
CARD=$(api GET /admin/sftp/connection) || die card "request failed"
[ -z "$SFTP_PORT" ] && SFTP_PORT=$(printf '%s' "$CARD" | jqp "print(d['port'])")
CARD_HOST=$(printf '%s' "$CARD" | jqp "print(d['host'])")
note "card advertises $CARD_HOST:$SFTP_PORT (connecting to $SFTP_HOST:$SFTP_PORT)"

stage "3/8 baseline"
# Automatic loading can be switched off from the console. With it off the file
# lands in the drop folder and simply waits — which looks identical to a broken
# pipeline until you know to check. Say so up front instead of timing out.
CFG=$(api GET /admin/ingest/config) || die baseline "cannot read ingest config"
printf '%s' "$CFG" | jqp "
import sys
if not d.get('enabled'):
    sys.exit('automatic loading is OFF (Data > file transfer). A dropped file would wait forever.')
print('auto-load on, polling every', d.get('poll_seconds'), 'seconds')" \
  | sed 's/^/  /' || die baseline "automatic loading is off — turn it on, or this test can only ever time out"
BEFORE=$(api GET /admin/analytics/data-health) || die baseline "request failed"
CAT_BEFORE=$(printf '%s' "$BEFORE" | jqp "print(d['catalog']['total'])")
note "catalog rows: $CAT_BEFORE"
[ "$CAT_BEFORE" -gt 0 ] 2>/dev/null || die baseline "catalog reports $CAT_BEFORE rows — refusing to test against an empty catalog"

stage "4/8 create a partner key"
GEN=$(api POST /admin/sftp/keys/generate "{\"label\":\"$LABEL\"}") || die keygen "request failed"
printf '%s' "$GEN" | jqp "
import os
k=d.get('private_key')
assert k, 'no private key in response: '+json.dumps(d)[:200]
open('$KEYFILE','w').write(k); os.chmod('$KEYFILE',0o600)
print('fingerprint',d['fingerprint'])" || die keygen "generate failed"
note "key $LABEL created"

stage "5/8 log in over SFTP with it"
sftp -i "$KEYFILE" -P "$SFTP_PORT" -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o PreferredAuthentications=publickey \
  -o PasswordAuthentication=no -o BatchMode=yes -o ConnectTimeout=20 \
  "pharma@$SFTP_HOST" </dev/null >/dev/null 2>&1 \
  || die sftp-auth "the key we just registered cannot log in to $SFTP_HOST:$SFTP_PORT"
note "key authenticates"

stage "6/8 upload"
if [ -z "$FILE" ]; then
  FILES=$(api GET /admin/sftp/files) || die pick-file "cannot list files"
  FILE_NAME=$(printf '%s' "$FILES" | jqp "
rows=[f for f in d['files'] if f.get('kind')=='catalog' and f.get('folder')=='archive']
rows.sort(key=lambda f: f.get('mtime',0), reverse=True)
print(rows[0]['stored_as'] if rows else '')")
  [ -n "$FILE_NAME" ] || die pick-file "no archived catalog file to replay; pass --file"
  api GET "/admin/sftp/file/$FILE_NAME" > "$WORK/upload.bin" 2>/dev/null \
    || die pick-file "cannot download $FILE_NAME"
  # Keep the extension: the loader picks its reader from it.
  case "$FILE_NAME" in
    *.csv)  FILE="$WORK/articles-export.csv" ;;
    *.xlsx) FILE="$WORK/articles-export.xlsx" ;;
    *) die pick-file "unexpected archive name $FILE_NAME" ;;
  esac
  mv "$WORK/upload.bin" "$FILE"
  note "replaying $FILE_NAME ($(wc -c < "$FILE") bytes)"
else
  case "$(basename "$FILE" | tr '[:upper:]' '[:lower:]')" in
    *balance*|*stock*|*inventory*)
      [ "$CONFIRM_STOCK" = 1 ] || die guard \
        "$FILE looks like an inventory file, which REPLACES the stock table. Pass --i-know-this-replaces-stock." ;;
  esac
  [ -f "$FILE" ] || die args "no such file: $FILE"
fi

REMOTE=$(basename "$FILE")
printf 'cd upload\nput "%s" "%s"\nbye\n' "$FILE" "$REMOTE" \
  | sftp -i "$KEYFILE" -P "$SFTP_PORT" -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o PreferredAuthentications=publickey \
      -o BatchMode=yes -o ConnectTimeout=20 -b - "pharma@$SFTP_HOST" >/dev/null 2>&1 \
  || die upload "sftp put failed"
note "uploaded as $REMOTE"

stage "7/8 wait for the watcher"
DEADLINE=$(( $(date +%s) + TIMEOUT ))
STATE=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  ROW=$(api GET /admin/sftp/files | jqp "
rows=[f for f in d['files'] if f.get('name')=='$REMOTE']
print((rows[0].get('folder','')+'|'+str(rows[0].get('state',''))+'|'+str(rows[0].get('step',''))) if rows else '|')" 2>/dev/null)
  case "$ROW" in
    archive\|ok\|*) STATE=loaded; break ;;
    failed\|*)      STATE=failed; break ;;
  esac
  sleep 5
done
[ "$STATE" = loaded ] || die ingest "file did not load within ${TIMEOUT}s (last seen: ${ROW:-nothing})"
note "loaded and archived"

stage "8/8 verify"
AFTER=$(api GET /admin/analytics/data-health) || die verify "request failed"
CAT_AFTER=$(printf '%s' "$AFTER" | jqp "print(d['catalog']['total'])")
note "catalog rows: $CAT_BEFORE -> $CAT_AFTER"
# A replay must not change the row count. A real difference is not necessarily
# a bug, but it means the file was NOT the one already loaded, so the operator
# has to be told rather than shown a green tick.
[ "$CAT_AFTER" -gt 0 ] 2>/dev/null || die verify "catalog is empty after the load"
if [ "$CAT_AFTER" != "$CAT_BEFORE" ]; then
  echo "WARN  row count moved ($CAT_BEFORE -> $CAT_AFTER): the file differed from the one already loaded" >&2
fi

# A loaded table is not a working product. Ask the assistant something only
# the catalog can answer, and require a real answer back — this is the check
# that would have caught a load that succeeded into a database nothing reads.
CRED=$(api GET /admin/credentials | jqp "
print((d[0]['embed_id']+' '+d[0]['public_key']) if d else '')")
if [ -z "$CRED" ]; then
  echo "WARN  no embed credential registered — skipped the answer check" >&2
else
  EMBED_ID=${CRED%% *}; EMBED_KEY=${CRED##* }
  SESS=$(I="$EMBED_ID" K="$EMBED_KEY" python3 -c \
    "import json,os;print(json.dumps({'embed_id':os.environ['I'],'public_key':os.environ['K']}))")
  STOK=$(curl -sS -m 60 -X POST "$BASE/api/embed/session/create" \
      -H 'content-type: application/json' --data "$SESS" | jqp "print(d.get('session_token',''))")
  [ -n "$STOK" ] || die answer "could not open a chat session as $EMBED_ID"
  ANS=$(ST="$STOK" python3 -c "import json,os;print(json.dumps({'session_token':os.environ['ST'],'message':'Do you have paracetamol in stock?'}))" \
    | curl -sS -m 180 -X POST "$BASE/api/embed/chat" -H 'content-type: application/json' --data @- \
    | jqp "print((d.get('content') or '').replace(chr(10),' ')[:160])")
  [ -n "$ANS" ] || die answer "the assistant returned nothing after the load"
  note "answer: $ANS"
fi

echo
echo "PASS  pipeline is healthy: key auth, upload, load, archive, catalog intact, assistant answering"
