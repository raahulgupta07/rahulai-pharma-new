"""Application configuration.

Loads settings from environment variables (and an optional ``.env`` file)
using ``pydantic-settings``. This module is REAL and working: it is the
foundation other modules depend on for credentials and tunables.

Import is always safe — settings are constructed lazily via
:func:`get_settings` (cached with ``lru_cache``), so importing this module
never crashes even when no ``.env`` file is present. Validation errors only
surface the first time settings are actually accessed.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Environment variables are matched to fields case-insensitively by name,
    e.g. ``OPENROUTER_API_KEY`` -> ``openrouter_api_key``.
    """

    openrouter_api_key: str
    openrouter_model: str = "google/gemini-3.5-flash"  # chat default; selectable per-request in chat UI
    embedding_model: str = "google/gemini-embedding-2"  # OpenRouter embeddings (3072-dim)
    postgres_url: str
    redis_url: str
    secret_key: str = "dev-secret-change-me"  # HMAC + session-token signing
    admin_token: str = ""   # legacy /admin gate; superseded by user auth
    session_ttl_seconds: int = 900

    # ---- user auth: seed super admin (no signup; admins create users) ----
    admin_email: str = "admin@citcare.local"
    admin_password: str = "changeme"   # CHANGE in prod; seeds the first super_admin
    auth_token_ttl_hours: int = 12

    # ---- LDAP (optional; merge-by-email) ----
    ldap_enabled: bool = False
    ldap_host: str = ""
    ldap_port: int = 389
    ldap_use_ssl: bool = False      # LDAPS on connect (port 636). Prefer this or start_tls.
    ldap_start_tls: bool = False    # StartTLS on a plaintext port (389). Ignored if use_ssl.
    ldap_validate_cert: bool = True  # verify the directory's TLS cert. OFF = MITM-able.
    ldap_ca_cert_file: str = ""     # PEM bundle for a private CA; "" = system trust store
    ldap_timeout_seconds: int = 8   # connect + receive; ldap3 is sync, so this bounds the thread
    ldap_bind_dn: str = ""          # service account to search with
    ldap_bind_password: str = ""
    ldap_base_dn: str = ""          # e.g. ou=users,dc=corp,dc=com
    ldap_user_filter: str = "(uid={username})"
    ldap_email_attr: str = "mail"
    ldap_name_attr: str = "cn"

    # ---- Keycloak / OIDC SSO (optional; merge-by-email) ----
    oidc_enabled: bool = False
    oidc_provider_name: str = "Keycloak"
    oidc_discovery_url: str = ""    # .../realms/<realm>/.well-known/openid-configuration
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""     # .../auth/sso/callback
    oidc_scopes: str = "openid email profile"
    oidc_discovery_ttl_seconds: int = 300   # cache the well-known doc; 0 disables caching
    oidc_state_ttl_seconds: int = 600       # how long a login may sit at the Keycloak form

    # Set true once the app is behind TLS: marks the SSO state cookie Secure.
    # Left false by default because the dev stack is plain http on :8088/:8091.
    cookie_secure: bool = False
    data_dir: str = "data"          # where reload looks for article*/balance* xlsx
    incoming_dir: str = "data/incoming"   # SFTP drop dir the watcher ingests from
    watch_interval_seconds: int = 15      # poll cadence for the ingest watcher

    # ---- SFTP handoff (what a PARTNER connects to) --------------------------
    # The address a partner dials is operator knowledge. Empty means "not
    # configured": `GET /sftp/connection` then falls back to the hostname the
    # request itself arrived on (X-Forwarded-Host / Host) and labels it
    # host_source="detected" — a suggestion the operator confirms, never a fact.
    # Behind a proxy that rewrites Host, or when the admin console and the sftp
    # port live on different names, the detected value is simply wrong; setting
    # this env var is the only authoritative answer.
    #
    # sftp_password must MATCH docker-compose's `pharma:${SFTP_PASSWORD:-pharma}`
    # — the compose default is what the container is actually created with, so
    # the default here mirrors it rather than inventing a second one. Until now
    # SFTP_PASSWORD was swallowed by `extra="ignore"` below (it was read only by
    # compose); declaring it lets the admin API hand the real credential over.
    sftp_public_host: str = ""
    sftp_public_port: int = 2222
    sftp_username: str = "pharma"
    sftp_password: str = "pharma"

    # Where the api container sees the sftp account's .ssh directory. A shared
    # docker volume (`sftp_ssh`) is mounted here read-write and into the sftp
    # container at /home/pharma/.ssh, which is what makes partner-key
    # registration take effect with NO restart: sshd re-reads authorized_keys on
    # every connection.
    #
    # Both writes matter. `authorized_keys` is what sshd reads NOW;
    # `keys/<label>.pub` is what atmoz/sftp rebuilds authorized_keys FROM at
    # boot. A key written only to authorized_keys works until the next restart
    # and then silently vanishes.
    sftp_keys_dir: str = "/sftp_ssh"
    cache_ttl_seconds: int = 600
    rate_limit_per_min: int = 30
    log_level: str = "INFO"
    # CORS. Comma-separated origins. The default is the local dev origins, NOT
    # "*": a wildcard lets any site on the internet call the embed API from a
    # victim's browser. "*" is still reachable, but only by setting it here
    # explicitly (ALLOWED_ORIGINS=*), and app.api logs a warning when it is in
    # effect. A real embed deployment lists the customer domains instead.
    allowed_origins: str = "http://localhost:5173,http://localhost:8088,http://localhost:8091"
    chat_log_retention_days: int = 30
    embed_in_background: bool = True   # don't block reload on embedding

    # ---- embed credentials (fail-closed) ----
    # cache.is_valid_credential rejects every (embed_id, public_key) that is not
    # registered — including when NO credential is registered at all. It used to
    # do the opposite (empty store => allow everything), so a prod deploy that
    # never seeded a credential accepted any embed on earth.
    #
    # Fail-closed would brick the documented web/web snippet, so the app seeds
    # exactly ONE credential on startup — and only when the credential store is
    # completely empty AND this flag is on. It logs a loud warning when it fires.
    # In prod: set EMBED_DEV_CREDENTIAL=false and register real credentials via
    # POST /admin/credentials.
    embed_dev_credential: bool = True
    embed_dev_credential_id: str = "web"
    embed_dev_credential_key: str = "web"

    # ---- Agno self-learning (preferences-only memory; see agent.py) ----
    # Off by default: enabling replays 3 prior runs into every prompt and runs a
    # second extraction model per turn, so it adds latency + cost to the hot path
    # (including trivial lookups). Flip to True via env to trade speed for memory.
    learning_enabled: bool = False
    learning_model: str = "google/gemini-2.5-flash-lite"  # cheap extraction model

    # ---- conversation history (multi-turn follow-ups) ----
    # ON by default. Without it every turn is answered in isolation, so "which
    # other shop has it?" cannot know what "it" is. Unlike learning_enabled this
    # adds NO extra LLM call — only prompt tokens for the replayed turns.
    # Applies only when the client sent a real per-conversation session_id.
    history_enabled: bool = True
    history_turns: int = 3

    # ---- latency: delete LLM round trips (measured baseline p50 ~9.8s) ----
    # Fast path: resolve the drug deterministically (trigram + drug_alias), run
    # one SQL query, and spend a single LLM call phrasing the answer — instead of
    # the model picking a tool, waiting, then writing. Cuts 3 legs to 1.
    fast_path_enabled: bool = False
    # Semantic cache: near-match the question embedding instead of hashing it.
    # KEEP OFF. Measured on gemini-embedding-2, whole-question cosine cannot
    # separate "same question" from "different strength": "do I have Panadol"
    # scores 0.947 against "...Panadol 1g" but only 0.927 against "Do we have
    # Panadol?" — the dangerous pair is CLOSER than the benign one. No threshold
    # is safe. To use this, pin the resolved article_code into the scope key
    # (see app/resolver.py) so variants land in different buckets.
    semantic_cache_enabled: bool = False
    semantic_cache_threshold: float = 0.94
    # Tool selection is an easy task; answer phrasing is not. Agno's output_model
    # runs the tool loop on router_model, then regenerates the final answer with
    # the strong model. Saves cost, not latency — the round trips remain.
    router_split_enabled: bool = False
    router_model: str = "google/gemini-2.5-flash-lite"

    # ---- MySQL source sync (pull the client's app DB -> our Postgres) ----
    # We never write to their DB. A read-only account runs two SELECTs whose
    # column ALIASES map their schema onto ours (article_code, brand_name, ...).
    mysql_sync_enabled: bool = False
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_db: str = ""
    # Operator overrides these SELECTs to match the client's real table/columns.
    # Aliases MUST be our column names. Unmapped catalog columns are left as-is.
    mysql_catalog_sql: str = (
        "SELECT code AS article_code, name AS brand_name, generic AS generic_name, "
        "composition AS composition, category AS category, indication AS indication, "
        "dosage AS dosage, side_effect AS side_effect, status AS status FROM products"
    )
    mysql_inventory_sql: str = (
        "SELECT product_code AS article_code, branch_code AS site_code, "
        "branch_name AS site_name, qty AS stock_qty, price AS price, uom AS uom FROM stock"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",   # ignore env vars meant for other services (e.g. POSTGRES_PASSWORD)
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` instance.

    Built lazily on first call so that merely importing this module never
    triggers environment validation. Subsequent calls return the same
    cached instance.
    """

    return Settings()


class _LazySettings:
    """Lazy proxy that resolves attribute access to :func:`get_settings`.

    Allows ``from app.config import settings`` followed by
    ``settings.openrouter_api_key`` without constructing the real settings
    object at import time.
    """

    def __getattr__(self, name: str):
        return getattr(get_settings(), name)


# Lazy module-level accessor. Use ``get_settings()`` directly where possible;
# ``settings`` exists for ergonomic attribute access without import-time cost.
settings = _LazySettings()

__all__ = ["Settings", "get_settings", "settings"]
