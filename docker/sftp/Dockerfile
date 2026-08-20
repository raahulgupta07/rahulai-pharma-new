# Vendored from atmoz/sftp @ 46a8b3db48a99257249a79fcbdf1cd9f666e161b (MIT).
# See README.md in this directory for why this is built rather than pulled.
FROM alpine:3.24
LABEL org.opencontainers.image.source="https://github.com/atmoz/sftp"
LABEL org.opencontainers.image.licenses="MIT"

# Steps done in one RUN layer:
# - Install upgrades and new packages
# - OpenSSH needs /var/run/sshd to run
# - Remove generic host keys, entrypoint generates unique keys
# Two deliberate departures from upstream, both found by smoke-testing a real
# password login against the built image rather than trusting it started:
#
# 1. `openssh-server`, NOT `openssh-server-pam`. Upstream symlinks sshd to the
#    PAM build, but Alpine 3.24 ships no /etc/pam.d/sshd for it, so PAM auth
#    fails and EVERY password is refused — `Permission denied (publickey,
#    password,keyboard-interactive)` — while sshd, the user and the upload dir
#    all look perfectly healthy. Nothing here needs PAM; the plain build reads
#    /etc/shadow, which is what create-sftp-user writes.
# 2. alpine is PINNED. Upstream tracks `latest`, which is how a 2024 image
#    silently stopped authenticating in 2026. A rebuild should produce the
#    image we tested, not whatever shipped that morning.
RUN apk update && apk upgrade && \
    apk add bash shadow openssh-server openssh-sftp-server && \
    rm -rf /var/cache/apk/* && \
    mkdir -p /var/run/sshd && \
    rm -f /etc/ssh/ssh_host_*key*

COPY files/sshd_config /etc/ssh/sshd_config
COPY files/create-sftp-user /usr/local/bin/
COPY files/entrypoint /

EXPOSE 22

ENTRYPOINT ["/entrypoint"]
