# Vendored SFTP image

This is [atmoz/sftp](https://github.com/atmoz/sftp), MIT-licensed, vendored at
upstream commit `46a8b3db48a99257249a79fcbdf1cd9f666e161b` (2024-09-14).
Upstream's licence is kept verbatim in `LICENSE.upstream.txt`.

## Why it is here rather than pulled

`atmoz/sftp` is published for **amd64 only** — no tag has an arm64 variant. On
the ARM host this runs on, the container could not start at all:

```
exec /entrypoint: exec format error
```

It restarted **8,798 times** over six days, so the SFTP drop was dead and the
catalog went seventeen days without an update. `docker pull`, `restart` and
`up --force-recreate` all fail identically, because the image simply does not
exist for this architecture.

Nothing in the Dockerfile is architecture-specific — `alpine:latest` is
multi-arch and `apk` resolves per-architecture. It was never *published* for
arm64; it *builds* for arm64 fine. So the repo builds it, and one
`docker compose up -d --build` now produces a working stack on either
architecture with no manual step.

## Why vendored rather than swapped for another image

The app depends on this image's *behaviour*, not just on "an SFTP server":

* `command: "pharma:<pass>:1001:1001:upload"` — atmoz's user syntax.
* The entrypoint runs `/etc/sftp.d/*` as root before dropping privileges, which
  is how `sftp/chown.sh` fixes volume ownership and keeps `keys/*` non-empty.
* `create-sftp-user` rebuilds `authorized_keys` from `.ssh/keys/*` at boot —
  that is where the admin UI's key registration writes.
* Production passes an empty password field so atmoz runs `usermod -p "*"`,
  which is what disables password auth.

Swapping to `linuxserver/openssh-server`, `emberstack/sftp` or `sftpgo` — all of
which do publish arm64 — means re-establishing every one of those. The one that
bites silently is key registration: a different image reads keys from a
different path, so the admin UI keeps accepting keys and no partner can ever
connect, with nothing in any log to say why.

## Updating it

Re-copy `Dockerfile-alpine` and `files/` from upstream, update the commit above,
and rebuild. Check `files/create-sftp-user` still rebuilds `authorized_keys`
from `.ssh/keys/*` before trusting a new revision.
