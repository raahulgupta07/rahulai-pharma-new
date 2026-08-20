# Pharmacy Agent API image — multi-stage (builds + serves the admin SPA too).
# Build context is assumed to be the project root (pharmacy-agent/).

# ---- stage 1: build the SvelteKit admin to static files ----
FROM node:22-slim AS admin
WORKDIR /admin
COPY admin/package.json admin/package-lock.json* ./
RUN npm install
COPY admin/ ./
RUN npm run build

# ---- stage 2: python API, serving the admin build at /admin ----
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY data/ ./data/
# The release notes are read at runtime by app/release_notes.py, which looks
# one directory ABOVE app/ — so this must land at /app/CHANGELOG.md. Without it
# the Version page renders empty in the container and only in the container,
# which is the kind of difference nobody notices until a customer asks.
COPY CHANGELOG.md ./CHANGELOG.md
# admin static build → /app/admin_build (api._mount_admin serves it at /admin)
COPY --from=admin /admin/build ./admin_build

# Build stamp. Passed by the deploy command, e.g.
#   docker compose build --build-arg GIT_SHA=$(git rev-parse HEAD) \
#                        --build-arg BUILT_AT=$(date -u +%FT%TZ) api
# Unset, the app reports "dev" and the console badges it as an unreleased
# build — deliberately, because a wrong build stamp is worse than none.
ARG GIT_SHA=dev
ARG BUILT_AT=""
ENV GIT_SHA=$GIT_SHA
ENV BUILT_AT=$BUILT_AT

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/health').status==200 else sys.exit(1)"

# `--proxy-headers` is load-bearing behind TLS, and its absence is invisible
# until a browser refuses to run the widget.
#
# Starlette builds `request.base_url` from the scheme uvicorn sees on the wire.
# Without this flag that is always `http`, even when the proxy terminated TLS —
# so `GET /embed/preview` (app/api.py) emitted
# `<script src="http://<host>/api/embed/widget.js">` onto a page the browser had
# loaded over https, and Chrome blocked it as mixed content. The page rendered,
# the API answered 200, and the chat widget simply never appeared.
#
# `--forwarded-allow-ips` is the trust boundary: uvicorn honours the forwarded
# headers ONLY from these peers. It is deliberately NOT `*`. The container port
# is published on the host, so anything that can reach it could otherwise assert
# its own scheme and client IP. `FORWARDED_ALLOW_IPS` is overridable per
# deployment because the trusted peer differs by topology; the default covers a
# proxy on the same host. Set it to the address the proxy actually connects
# FROM — when the proxy dials the host's public address, that is the public IP,
# not a private one.
ENV FORWARDED_ALLOW_IPS=127.0.0.1
CMD ["sh", "-c", "exec uvicorn app.api:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\""]
