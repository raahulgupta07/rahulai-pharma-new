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
# admin static build → /app/admin_build (api._mount_admin serves it at /admin)
COPY --from=admin /admin/build ./admin_build

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/health').status==200 else sys.exit(1)"

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
