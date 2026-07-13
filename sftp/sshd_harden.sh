#!/bin/sh
# PROD ONLY (mounted by docker-compose.prod.yml, not the dev compose).
#
# Second lock on the door. docker-compose.prod.yml already creates the sftp user
# with an EMPTY password field, which makes atmoz run `usermod -p "*"` — the
# account has no usable password and password auth cannot succeed. This script
# says the same thing in sshd's own config, so the guarantee does not depend on
# one field in a compose command line surviving a future edit.
#
# The image ships no PasswordAuthentication directive at all (it relies purely on
# the disabled account password), and its sshd_config has no Include, so we edit
# the file in place. /etc/ssh is a named volume in prod (to keep host keys stable
# across container recreation), so this runs against persisted state on every
# boot — hence: strip the directive, then re-add it. Idempotent by construction,
# never appends a duplicate.
set -eu

CONF=/etc/ssh/sshd_config

harden() {
    key="$1"
    value="$2"
    sed -i "/^[[:space:]]*${key}[[:space:]]/d" "$CONF"
    echo "${key} ${value}" >> "$CONF"
}

harden PasswordAuthentication no
harden PermitEmptyPasswords no
harden ChallengeResponseAuthentication no
harden KbdInteractiveAuthentication no
harden PubkeyAuthentication yes

echo "[sshd_harden.sh] password auth disabled; public-key auth only"
