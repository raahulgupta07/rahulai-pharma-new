#!/bin/sh
# Runs as ROOT from /etc/sftp.d/ on every boot of the atmoz/sftp container,
# before privileges are dropped and before sshd starts.
#
# Two jobs, both of which must happen on EVERY start, not just the first:
#
# 1. Ownership/permissions. /home/pharma/upload and /home/pharma/.ssh are both
#    Docker VOLUMES, which are created root-owned 0755. The sftp user cannot
#    write into a root-owned upload dir; and sshd applies StrictModes, so a
#    group-/world-writable ~/.ssh or an untrusted authorized_keys is IGNORED
#    SILENTLY — key auth fails with nothing useful in the log.
#
# 2. Keep .ssh/keys non-empty. atmoz's create-sftp-user rebuilds authorized_keys
#    from `.ssh/keys/*` — under `set -e`, with no nullglob. If that directory
#    EXISTS but is EMPTY the glob stays literal, `cat` fails on it, and the
#    CONTAINER DIES ON BOOT. That state is reachable in normal use: the admin API
#    creates keys/ up front, and an operator can delete the last partner key. The
#    placeholder below keeps the glob matching (sshd ignores '#' lines in
#    authorized_keys, so a comment file is inert).
set -eu

SFTP_UID=1001
SFTP_GID=1001

for home in /home/*/; do
    user="$(basename "$home")"

    if [ -d "${home}upload" ]; then
        chown -R "${SFTP_UID}:${SFTP_GID}" "${home}upload"
    fi

    ssh_dir="${home}.ssh"
    [ -d "$ssh_dir" ] || continue

    mkdir -p "${ssh_dir}/keys"
    # NOT a dotfile, and created unconditionally. Both details are load-bearing:
    # the glob atmoz uses is `keys/*`, which does NOT match dotfiles — a
    # `.placeholder` leaves the directory still "empty" as far as the glob is
    # concerned and the container dies exactly as before. Verified, the hard way.
    # No .pub suffix either, so it never shows up as a partner key in the admin UI.
    if [ ! -f "${ssh_dir}/keys/00-placeholder" ]; then
        printf '# placeholder - keeps keys/* matching; see sftp/chown.sh\n' \
            > "${ssh_dir}/keys/00-placeholder"
    fi

    [ -f "${ssh_dir}/authorized_keys" ] || : > "${ssh_dir}/authorized_keys"

    chown -R "${SFTP_UID}:${SFTP_GID}" "$ssh_dir"
    chmod 0700 "$ssh_dir" "${ssh_dir}/keys"
    chmod 0600 "${ssh_dir}/authorized_keys"

    echo "[chown.sh] ${user}: .ssh 0700, authorized_keys 0600, owned ${SFTP_UID}:${SFTP_GID}"
done
