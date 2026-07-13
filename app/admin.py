"""Admin API — backs the SvelteKit management panel.

Read-mostly endpoints over the existing data + a little CRUD for credentials
and config. Mounted under /admin in app.api.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel

from app import cache
from app.config import get_settings
from app.db import counts, execute, q
from app.tools import _site_clause

router = APIRouter(prefix="/admin", tags=["admin"])


def _int_or_none(v) -> Optional[int]:
    """int(v), preserving NULL. NULL stock is UNKNOWN — never coerce it to 0."""

    return None if v is None else int(v)


# ---- schema owned by the admin surface -------------------------------------


async def ensure_admin_schema() -> None:
    """Create/extend the tables this router owns. Idempotent; run at startup.

    * ``users.store_id`` — pins an admin account to ONE branch. NULL (the state
      every existing row is in) means the global view, so adding the column
      changes nobody's access.
    * ``drug_alias`` — the fast path's learned-alias table. It has existed as
      ``migrations/0002_drug_alias.sql`` since the fast path landed, but nothing
      applied it on boot and nothing wrote to it, so ``resolver._alias_lookup``
      was a permanent miss. Created here so the write path below has a table.
    """

    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS store_id TEXT")
    # last_seen: the run timestamp of the last article file to carry a catalog
    # row. Written by ingest_catalog, read by full_sync (delete rows not in the
    # latest file) and the manual stale purge. NULL on every existing row until
    # the first ingest after this deploy — treated as "unknown age".
    await execute("ALTER TABLE catalog ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ")
    await execute(
        """
        CREATE TABLE IF NOT EXISTS drug_alias (
            alias        TEXT PRIMARY KEY,
            article_code TEXT NOT NULL
                         REFERENCES catalog(article_code) ON DELETE CASCADE,
            source       TEXT,
            created_at   TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    await execute(
        "CREATE INDEX IF NOT EXISTS idx_drug_alias_article ON drug_alias (article_code)"
    )


# ---- caller store scope ----------------------------------------------------


async def caller_store_scope(authorization: str = Header(default="")) -> Optional[str]:
    """The branch this admin caller is pinned to, or ``None`` for the global view.

    The chat layer scopes by taking ``store_id`` off a signed token and forcing
    every tool through ``tools._site_clause``. The admin layer had no equivalent
    at all, so ``GET /admin/catalog/{code}`` handed any caller every branch's
    stock. This is the admin-side half of the same mechanism: the scope comes
    from the server (the caller's ``users`` row), never from the request, and it
    is matched with the very same ``_site_clause``.

    * ``super_admin`` — always global. A pinned super_admin would be a way to
      lock the top account out of its own data.
    * anyone else — scoped iff their row carries a ``store_id``.

    Every existing account has ``store_id`` NULL, so today this is a no-op and
    the admin console is unchanged. Assigning a store to an ``admin`` row (see
    ``PATCH /admin/users/{id}``) turns them into a branch manager.

    ``require_admin`` has already authenticated this exact token at the router
    level, so a failure here means the token vanished between two dependencies —
    reject rather than fall through to the global view.
    """

    from app import auth as authmod

    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = authmod.decode_token(authorization.split(" ", 1)[1])
    except Exception:  # noqa: BLE001 — any decode failure is a rejected caller
        raise HTTPException(status_code=401, detail="invalid or expired session")

    user = await authmod.get_by_email(claims.get("email", ""))
    if not user:
        raise HTTPException(status_code=401, detail="account not found")
    if user["role"] == "super_admin":
        return None
    return (user.get("store_id") or "").strip() or None


async def require_super_admin(authorization: str = Header(default="")) -> Dict:
    """Narrow an /admin/* endpoint from "any admin" to super_admin only.

    The router-level ``api.require_admin`` already proved this token belongs to
    an active, approved ``admin`` **or** ``super_admin``. Endpoints that hand
    back a shared secret (see ``GET /sftp/connection``, which returns the SFTP
    password) need the stricter half, so this re-reads the caller's row exactly
    as ``caller_store_scope`` does — role from the ``users`` table, never from
    the token — and rejects a plain ``admin`` with 403.

    Re-reading the DB rather than trusting the JWT's claims is deliberate and
    matches ``require_admin``: a demoted account loses the password at its next
    request, not at token expiry.
    """

    from app import auth as authmod

    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = authmod.decode_token(authorization.split(" ", 1)[1])
    except Exception:  # noqa: BLE001 — any decode failure is a rejected caller
        raise HTTPException(status_code=401, detail="invalid or expired session")

    user = await authmod.get_by_email(claims.get("email", ""))
    if not user or not user["active"] or not user.get("approved"):
        raise HTTPException(status_code=401, detail="account not found")
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="super_admin access required")
    return user


# ---- catalog ---------------------------------------------------------------


@router.get("/catalog")
async def catalog(
    search: str = "", category: str = "", limit: int = 50, offset: int = 0
) -> List[Dict]:
    """List/search catalog (brand, generic, or exact code), paginated.

    Optional `category` filters by category substring (e.g. "PRESCRIPTION").
    """

    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    conds, params = [], []
    if search:
        params.append(search)
        n = len(params)
        conds.append(
            f"(brand_name ILIKE '%'||${n}||'%' OR generic_name ILIKE '%'||${n}||'%' OR article_code = ${n})"
        )
    else:
        # Browse view hides stub rows (brand_name == article_code).
        conds.append("brand_name <> article_code")
    if category:
        params.append(category)
        conds.append(f"category ILIKE '%'||${len(params)}||'%'")
    where = "WHERE " + " AND ".join(conds)
    params.append(limit)
    params.append(offset)
    return await q(
        f"""SELECT article_code, brand_name, generic_name, category
              FROM catalog {where}
             ORDER BY brand_name LIMIT ${len(params)-1} OFFSET ${len(params)}""",
        *params,
    )


@router.get("/categories")
async def categories() -> List[Dict]:
    """Distinct catalog categories with counts (for the data-page filter)."""

    return await q(
        """SELECT category, count(*) AS n FROM catalog
            WHERE category IS NOT NULL AND brand_name <> article_code
            GROUP BY category ORDER BY n DESC"""
    )


@router.get("/catalog/{code}")
async def catalog_one(
    code: str, scope: Optional[str] = Depends(caller_store_scope)
) -> Dict:
    """Full article detail + per-site stock + summary, scoped to the caller's branch.

    Two bugs lived here, both of them a pharmacy reading a number that is not
    true:

    **Every branch's stock, to everyone.** The site query had no scope clause at
    all, so a branch-scoped account saw its siblings' inventory — the same leak
    class already fixed in ``search_by_meaning`` / ``related_drugs``. It now
    filters through ``tools._site_clause``, the one correct site matcher (a bare
    ``=`` or an ``ILIKE '%x%'`` here would reintroduce the two scoping bugs
    documented in CLAUDE.md). ``scope`` is ``None`` for admin/super_admin, and the
    ``$2 IS NULL`` guard keeps that the full view.

    **NULL stock counted as zero.** ``sum(s["stock_qty"] or 0)`` coerced UNKNOWN
    to 0, contradicting the repo-wide invariant that a NULL ``stock_qty`` means
    *we do not know*, never *none on hand*. A pharmacist reading "0" does not
    dispense. Unknown now stays unknown: ``total_stock`` sums only the branches
    we actually have a figure for, and is ``None`` when we have a figure for none
    of them. ``unknown_site_count`` says how many branches were left out, so a
    partial total is never mistaken for a complete one.
    """

    rows = await q("SELECT * FROM catalog WHERE article_code=$1", code)
    if not rows:
        raise HTTPException(status_code=404, detail="article not found")
    article = {k: v for k, v in rows[0].items() if k != "embedding"}
    sites = await q(
        """SELECT site_code, stock_qty, price FROM inventory
            WHERE article_code=$1
              AND ($2::text IS NULL OR """ + _site_clause("site_code", "$2") + """)
            ORDER BY stock_qty DESC NULLS LAST""",
        code,
        scope,
    )
    for s in sites:
        s["price"] = float(s["price"]) if s["price"] is not None else None

    known = [s["stock_qty"] for s in sites if s["stock_qty"] is not None]
    return {
        "article": article,
        "sites": sites,
        # None (not 0) when no branch in scope reports a quantity: UNKNOWN.
        "total_stock": sum(known) if known else None,
        "site_count": len(sites),
        "known_site_count": len(known),
        "unknown_site_count": len(sites) - len(known),
        "store_scope": scope,
    }


# ---- inventory -------------------------------------------------------------


@router.get("/inventory")
async def inventory(
    site: str = "", search: str = "", status: str = "", limit: int = 100, offset: int = 0
) -> List[Dict]:
    """Inventory rows, optionally filtered by site, article code and/or stock status.

    `status` is one of: in (>=20), low (1–19), out (0).
    """

    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    conds, params = [], []
    if site:
        params.append(site)
        conds.append(f"i.site_code ILIKE '%'||${len(params)}||'%'")
    if search:
        params.append(search)
        conds.append(
            f"(i.article_code = ${len(params)} OR c.brand_name ILIKE '%'||${len(params)}||'%')"
        )
    if status == "out":
        conds.append("i.stock_qty = 0")
    elif status == "low":
        conds.append("i.stock_qty BETWEEN 1 AND 19")
    elif status == "in":
        conds.append("i.stock_qty >= 20")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    params.append(offset)
    rows = await q(
        f"""SELECT i.article_code, COALESCE(c.brand_name, i.article_code) AS brand_name,
                   i.site_code, i.stock_qty, i.price
              FROM inventory i LEFT JOIN catalog c USING (article_code)
              {where}
             ORDER BY i.stock_qty DESC LIMIT ${len(params)-1} OFFSET ${len(params)}""",
        *params,
    )
    for r in rows:
        r["price"] = float(r["price"]) if r["price"] is not None else None
    return rows


# ---- stores ----------------------------------------------------------------


@router.get("/stores")
async def stores() -> List[Dict]:
    """Per-site summary from the materialized view (fast). Falls back to live."""

    try:
        rows = await q("SELECT site_code, skus, units, value FROM mv_store_summary ORDER BY value DESC")
    except Exception:  # view missing -> live aggregate
        rows = await q(
            """SELECT site_code, COUNT(*) AS skus, SUM(stock_qty) AS units,
                      ROUND(SUM(price * stock_qty)) AS value
                 FROM inventory GROUP BY site_code ORDER BY value DESC"""
        )
    for r in rows:
        r["units"] = int(r["units"] or 0)
        r["value"] = float(r["value"] or 0)
        r["skus"] = int(r["skus"] or 0)
    return rows


@router.get("/overview")
async def overview(limit: int = 10) -> List[Dict]:
    """Top articles by total stock, from the article-summary materialized view."""

    limit = min(max(limit, 1), 50)
    try:
        rows = await q(
            """SELECT article_code, brand_name, total_stock, weighted_avg_price, site_count
                 FROM mv_article_summary ORDER BY total_stock DESC LIMIT $1""",
            limit,
        )
    except Exception:
        return []
    for r in rows:
        # NULL is UNKNOWN, not zero: `or 0` here would report a drug nobody has
        # counted as one nobody has. site_count is a COUNT(), so 0 is a real 0.
        ts, price = r["total_stock"], r["weighted_avg_price"]
        r["total_stock"] = int(ts) if ts is not None else None
        r["weighted_avg_price"] = float(price) if price is not None else None
        r["site_count"] = int(r["site_count"] or 0)
    return rows


@router.get("/views")
async def views_status() -> Dict:
    """Materialized view row counts (admin visibility)."""

    out = {}
    for mv in ("mv_store_summary", "mv_article_summary"):
        try:
            out[mv] = (await q(f"SELECT count(*) AS n FROM {mv}"))[0]["n"]
        except Exception:
            out[mv] = None
    return out


@router.post("/views/refresh")
async def views_refresh() -> Dict:
    """Manually refresh both materialized views."""

    from app.ingest import refresh_views

    await refresh_views()
    return await views_status()


# ---- conversations ---------------------------------------------------------


@router.get("/conversations")
async def conversations(limit: int = 50, lang: str = "", store: str = "", offset: int = 0) -> List[Dict]:
    """Recent chat logs (question, answer, lang, store, cached, latency), paginated."""

    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    conds, params = [], []
    if lang:
        params.append(lang)
        conds.append(f"lang = ${len(params)}")
    if store:
        params.append(store)
        conds.append(f"store_id = ${len(params)}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    params.append(offset)
    try:
        return await q(
            f"""SELECT id, ts, lang, store_id, question, answer, cached, latency_ms
                  FROM chat_logs {where} ORDER BY id DESC
                 LIMIT ${len(params)-1} OFFSET ${len(params)}""",
            *params,
        )
    except Exception:  # table not created yet
        return []


# ---- credentials (tenants) -------------------------------------------------


class Credential(BaseModel):
    embed_id: str
    public_key: str


@router.get("/credentials")
async def list_creds() -> List[Dict]:
    creds = await cache.list_credentials()
    return [{"embed_id": k, "public_key": v} for k, v in creds.items()]


@router.post("/credentials")
async def add_cred(c: Credential) -> Dict:
    await cache.register_credential(c.embed_id, c.public_key)
    return {"status": "ok", "embed_id": c.embed_id}


@router.delete("/credentials/{embed_id}")
async def del_cred(embed_id: str) -> Dict:
    n = await cache.remove_credential(embed_id)
    return {"status": "ok", "removed": n}


# ---- drug aliases (fast-path memory) ---------------------------------------
#
# resolver.py resolves a free-text mention in three layers: exact code -> alias
# -> trigram. The alias layer was dead: the table existed, the read existed, and
# NOTHING wrote to it, so every lookup missed and every mention fell through to
# the trigram scan. This is the missing write path.


def _norm_alias(alias: str) -> str:
    """Normalise a mention to its stored key.

    Must match ``resolver._alias_lookup``, which queries ``WHERE alias = lower($1)``
    against a mention that ``resolve()`` has only ``.strip()``ed. So: strip, then
    lowercase — and nothing else. Collapsing internal whitespace here would store
    a key the resolver can never look up.
    """

    return (alias or "").strip().lower()


class Alias(BaseModel):
    alias: str
    article_code: str
    source: str = "admin"


@router.get("/aliases")
async def list_aliases(
    search: str = "", article_code: str = "", limit: int = 100, offset: int = 0
) -> List[Dict]:
    """Learned aliases, newest first. Filter by alias substring and/or article."""

    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    conds, params = [], []
    if search:
        params.append(search)
        conds.append(f"a.alias ILIKE '%'||${len(params)}||'%'")
    if article_code:
        params.append(article_code)
        conds.append(f"a.article_code = ${len(params)}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    params.append(offset)
    try:
        return await q(
            f"""SELECT a.alias, a.article_code, a.source, a.created_at,
                       c.brand_name, c.generic_name
                  FROM drug_alias a LEFT JOIN catalog c USING (article_code)
                  {where}
                 ORDER BY a.created_at DESC
                 LIMIT ${len(params)-1} OFFSET ${len(params)}""",
            *params,
        )
    except Exception:  # noqa: BLE001 — table not created yet
        return []


@router.post("/aliases")
async def add_alias(a: Alias) -> Dict:
    """Teach the resolver that ``alias`` means ``article_code`` (upsert).

    The article must exist: drug_alias FKs to catalog, so an unknown code would
    surface as a 500 from asyncpg. Check first and answer 400.
    """

    alias = _norm_alias(a.alias)
    code = (a.article_code or "").strip()
    if len(alias) < 2:
        raise HTTPException(status_code=400, detail="alias must be at least 2 characters")
    if not code:
        raise HTTPException(status_code=400, detail="article_code is required")
    if not await q("SELECT 1 FROM catalog WHERE article_code=$1", code):
        raise HTTPException(status_code=404, detail="article not found")

    rows = await q(
        """INSERT INTO drug_alias (alias, article_code, source)
           VALUES ($1,$2,$3)
           ON CONFLICT (alias) DO UPDATE
               SET article_code = EXCLUDED.article_code,
                   source       = EXCLUDED.source,
                   created_at   = now()
           RETURNING alias, article_code, source""",
        alias, code, (a.source or "admin").strip() or "admin",
    )
    # An alias changes what a question RESOLVES to, so answers cached against the
    # old resolution are now wrong for the same words. Same rule as
    # /admin/graph/rebuild: a writer that changes answers bumps the version.
    version = await cache.bump_data_version()
    return {"status": "ok", **(rows[0] if rows else {}), "data_version": version}


@router.delete("/aliases/{alias}")
async def del_alias(alias: str) -> Dict:
    """Forget one learned alias."""

    rows = await q(
        "DELETE FROM drug_alias WHERE alias=$1 RETURNING alias", _norm_alias(alias)
    )
    version = await cache.bump_data_version()
    return {"status": "ok", "removed": len(rows), "data_version": version}


# ---- agent config ----------------------------------------------------------


class ConfigUpdate(BaseModel):
    system_prompt: Optional[str] = None


@router.get("/config")
async def get_config() -> Dict:
    s = get_settings()
    overrides = await cache.get_config_overrides()
    from app.agent import BILINGUAL_SYSTEM_PROMPT

    return {
        "model": s.openrouter_model,
        "embedding_model": s.embedding_model,
        "rate_limit_per_min": s.rate_limit_per_min,
        "cache_ttl_seconds": s.cache_ttl_seconds,
        "session_ttl_seconds": s.session_ttl_seconds,
        "system_prompt": overrides.get("system_prompt", BILINGUAL_SYSTEM_PROMPT),
        "prompt_overridden": "system_prompt" in overrides,
    }


@router.put("/config")
async def put_config(c: ConfigUpdate) -> Dict:
    if c.system_prompt is not None:
        await cache.set_config_override("system_prompt", c.system_prompt)
    return {"status": "ok", "note": "applied on next agent rebuild/restart"}


# ---- authentication config (Keycloak SSO + LDAP) ---------------------------


@router.get("/auth-config")
async def get_auth_config() -> Dict:
    """Effective LDAP/OIDC config for the admin page. Secrets are masked.

    A secret value is never returned; the client only learns whether one is set
    (``ldap_bind_password_set`` / ``oidc_client_secret_set``).
    """

    from app import auth as authmod

    return await authmod.get_auth_config()


@router.put("/auth-config")
async def put_auth_config(updates: Dict = Body(...)) -> Dict:
    """Persist a partial LDAP/OIDC update. Takes effect on the next login.

    An empty secret field is treated as "keep the current value", so the masked
    password box can be saved without wiping the stored secret.
    """

    from app import auth as authmod

    await authmod.set_auth_config(updates)
    return {"status": "ok", "note": "applied on next login; no restart needed"}


# ---- evaluation ------------------------------------------------------------


@router.post("/eval/run")
async def run_eval() -> Dict:
    """Run the eval set live through the agent. Slow (LLM calls)."""

    from pathlib import Path

    from app.agent import build_agent

    cases = json.loads(
        (Path(__file__).parent.parent / "evals" / "eval_set.json").read_text("utf-8")
    )["cases"]
    agent = build_agent()
    results, passed = [], 0
    for c in cases:
        out = await agent.arun(c["question"])
        ans = getattr(out, "content", str(out))
        na = ans.lower().replace(",", "").replace(" ", "")
        ok = all(n.lower().replace(",", "").replace(" ", "") in na
                 for n in c.get("expect_contains", []))
        if c.get("expect_script") == "my":
            ok = ok and any("က" <= ch <= "႟" for ch in ans)
        passed += ok
        results.append({"id": c["id"], "question": c["question"],
                        "pass": ok, "answer": ans[:300]})
    return {"score": f"{passed}/{len(cases)}", "passed": passed,
            "total": len(cases), "results": results}


# ---- knowledge graph (GraphRAG) -------------------------------------------


@router.get("/graph")
async def graph_status() -> Dict:
    """Edge counts per relation in the drug knowledge graph."""

    out = {}
    for rel in ("has_generic", "contains", "in_category", "treats"):
        try:
            out[rel] = (await q("SELECT count(*) n FROM drug_edges WHERE rel=$1", rel))[0]["n"]
        except Exception:
            out[rel] = 0
    return out


@router.post("/graph/rebuild")
async def graph_rebuild() -> Dict:
    """Rebuild structured edges (generic/ingredient/category) from the catalog."""

    from app.cache import bump_data_version
    from app.graph import build_edges

    res = await build_edges()
    # Substitute/related answers are derived from drug_edges, so a rebuild can
    # change them. Without the bump, cached answers outlive the graph they came
    # from for up to CACHE_TTL_SECONDS.
    res["data_version"] = await bump_data_version()
    return res


@router.post("/graph/treats")
async def graph_treats(limit: int = 200, background: bool = True) -> Dict:
    """Stage 2: LLM-extract treats-edges from indication text (bounded by limit).

    Runs in the background by default so it never blocks the worker / UI — poll
    GET /admin/graph to watch the treats count grow. Set background=false to wait.
    """

    from app.graph import build_treats_edges

    if background:
        import asyncio

        asyncio.create_task(build_treats_edges(limit=limit))
        return {"status": "started", "limit": limit, "note": "poll /admin/graph for progress"}
    return await build_treats_edges(limit=limit)


@router.get("/graph/overview")
async def graph_overview(limit: int = 80) -> Dict:
    """Galaxy view — a bounded subgraph of the richest (treats-bearing) drugs plus
    their ingredient/condition hubs and same-generic drug links. Nodes + links for
    a d3 force layout."""

    limit = min(max(limit, 10), 200)
    sel = await q(
        "SELECT DISTINCT src FROM drug_edges WHERE rel='treats' ORDER BY src LIMIT $1", limit
    )
    codes = [r["src"] for r in sel]
    if not codes:
        return {"nodes": [], "links": []}
    attr_edges = await q(
        """SELECT e.src, e.rel, e.dst FROM drug_edges e
            WHERE e.src = ANY($1) AND e.rel IN ('contains','treats')""",
        codes,
    )
    gen_edges = await q(
        """SELECT a.src AS d1, b.src AS d2
             FROM drug_edges a JOIN drug_edges b
               ON a.dst = b.dst AND a.rel='has_generic' AND b.rel='has_generic' AND a.src < b.src
            WHERE a.src = ANY($1) AND b.src = ANY($1)""",
        codes,
    )
    brands = {r["article_code"]: r["brand_name"] for r in await q(
        "SELECT article_code, brand_name FROM catalog WHERE article_code = ANY($1)", codes)}

    nodes, seen = [], set()
    def add(nid, ntype, label):
        if nid not in seen:
            seen.add(nid); nodes.append({"id": nid, "type": ntype, "label": label})
    for c in codes:
        add(c, "drug", brands.get(c, c))
    links = []
    for e in attr_edges:
        ntype = "ing" if e["rel"] == "contains" else "cond"
        add(e["dst"], ntype, e["dst"])
        links.append({"source": e["src"], "target": e["dst"], "rel": e["rel"]})
    for e in gen_edges:
        links.append({"source": e["d1"], "target": e["d2"], "rel": "generic"})
    return {"nodes": nodes, "links": links}


@router.get("/graph/node")
async def graph_node(code: str) -> Dict:
    """Detail-panel data for one article — grouped graph links + live total stock."""

    cat = await q("SELECT article_code, brand_name, generic_name FROM catalog WHERE article_code=$1", code)
    contains = [r["dst"] for r in await q("SELECT dst FROM drug_edges WHERE src=$1 AND rel='contains'", code)]
    treats = [r["dst"] for r in await q("SELECT dst FROM drug_edges WHERE src=$1 AND rel='treats'", code)]
    same_generic = await q(
        """SELECT DISTINCT e2.src AS article_code, c.brand_name
             FROM drug_edges e1 JOIN drug_edges e2
               ON e1.dst = e2.dst AND e1.rel='has_generic' AND e2.rel='has_generic' AND e2.src <> $1
             JOIN catalog c ON c.article_code = e2.src
            WHERE e1.src = $1 ORDER BY c.brand_name LIMIT 20""",
        code,
    )
    stock = await q("SELECT total_stock, site_count FROM mv_article_summary WHERE article_code=$1", code)
    return {
        "article_code": code,
        "brand_name": cat[0]["brand_name"] if cat else code,
        "generic_name": cat[0]["generic_name"] if cat else None,
        # A summary row can exist with a NULL total (every branch blank), so the
        # int() must be guarded on the VALUE, not just on the row: int(None) is a
        # TypeError -> 500, and `or 0` would turn unknown into zero.
        "total_stock": _int_or_none(stock[0]["total_stock"]) if stock else None,
        "site_count": _int_or_none(stock[0]["site_count"]) if stock else None,
        "contains": contains,
        "treats": treats,
        "same_generic": same_generic,
    }


@router.get("/graph/by-attribute")
async def graph_by_attribute(rel: str, value: str, limit: int = 16) -> Dict:
    """Articles linked to an attribute node (ingredient/condition/generic) — lets
    the graph explorer expand a non-article node."""

    limit = min(max(limit, 1), 40)
    rows = await q(
        """SELECT e.src AS article_code, c.brand_name
             FROM drug_edges e JOIN catalog c ON c.article_code = e.src
            WHERE e.rel = $1 AND e.dst = $2
            ORDER BY c.brand_name LIMIT $3""",
        rel, value, limit,
    )
    return {"center": {"value": value, "rel": rel}, "articles": rows}


@router.get("/graph/neighbors")
async def graph_neighbors(code: str, limit: int = 14) -> Dict:
    """Subgraph around an article — its attribute nodes + articles sharing them.
    Powers the admin graph visualization."""

    limit = min(max(limit, 1), 40)
    center = await q("SELECT article_code, brand_name FROM catalog WHERE article_code=$1", code)
    attrs = await q(
        "SELECT rel, dst FROM drug_edges WHERE src=$1 AND rel IN ('has_generic','contains','treats') ORDER BY rel",
        code,
    )
    related = await q(
        """SELECT DISTINCT e2.src AS article_code, c.brand_name, e1.rel, e1.dst AS via
             FROM drug_edges e1
             JOIN drug_edges e2 ON e2.dst = e1.dst AND e2.rel = e1.rel AND e2.src <> e1.src
             JOIN catalog c ON c.article_code = e2.src
            WHERE e1.src = $1 AND e1.rel IN ('has_generic','contains','treats')
            ORDER BY e1.rel, c.brand_name LIMIT $2""",
        code, limit,
    )
    return {
        "center": center[0] if center else {"article_code": code, "brand_name": code},
        "attributes": attrs,
        "related": related,
    }


# ---- manual upload + SFTP status -------------------------------------------


@router.post("/upload")
async def upload(file: UploadFile = File(...)) -> Dict:
    """Manually upload an article/balance xlsx/csv → drop in incoming dir → ingest now."""

    from pathlib import Path

    from app.watcher import scan_once

    name = Path(file.filename or "upload.xlsx").name
    if not name.lower().endswith((".xlsx", ".csv")):
        raise HTTPException(status_code=400, detail="only .xlsx or .csv files accepted")
    incoming = Path(get_settings().incoming_dir)
    incoming.mkdir(parents=True, exist_ok=True)
    dest = incoming / name
    dest.write_bytes(await file.read())
    summary = await scan_once(stable_only=False)
    return {"status": "uploaded", "file": name, **summary}


# ---- ingest config + manual stale purge ------------------------------------
#
# These change ingest behaviour for the whole tenant (both the api and the
# worker container read the Redis config), and the purge is the ONLY delete path
# the operator can trigger from the UI, so every endpoint here is super_admin
# only — the router-level require_admin is not enough.


class IngestConfigUpdate(BaseModel):
    poll_seconds: Optional[int] = None
    catalog_mode: Optional[str] = None


@router.get("/ingest/config", dependencies=[Depends(require_super_admin)])
async def ingest_config_get() -> Dict:
    """The effective ingest config (poll cadence + catalog mode) from Redis."""

    return await cache.get_ingest_config()


@router.post("/ingest/config", dependencies=[Depends(require_super_admin)])
async def ingest_config_set(c: IngestConfigUpdate) -> Dict:
    """Persist a partial ingest-config update. Clamped/validated on write.

    ``poll_seconds`` is clamped to 5..3600; ``catalog_mode`` must be 'merge' or
    'full_sync'. Takes effect on the worker's next loop/scan — no restart.
    """

    try:
        return await cache.set_ingest_config(
            poll_seconds=c.poll_seconds, catalog_mode=c.catalog_mode
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _stale_cutoff(days: int):
    """now() - ``days``, as an aware datetime. Refuses days < 1."""

    from datetime import datetime, timedelta, timezone

    if days < 1:
        raise HTTPException(status_code=400, detail="days must be at least 1")
    return datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/ingest/stale", dependencies=[Depends(require_super_admin)])
async def ingest_stale(days: int = 30) -> Dict:
    """PREVIEW ONLY — how many catalog rows a purge of ``days`` would remove.

    Counts rows whose ``last_seen`` is older than the cutoff OR is NULL (never
    seen since the column was added — unknown age). ``legacy_count`` breaks out
    the NULL rows so the operator knows the first purge may match legacy data.
    Deletes nothing.
    """

    cutoff = _stale_cutoff(days)
    rows = await q(
        """SELECT count(*) AS n,
                  count(*) FILTER (WHERE last_seen IS NULL) AS legacy
             FROM catalog
            WHERE last_seen < $1 OR last_seen IS NULL""",
        cutoff,
    )
    return {
        "count": int(rows[0]["n"]),
        "legacy_count": int(rows[0]["legacy"]),
        "cutoff": cutoff.isoformat(),
        "days": days,
    }


class PurgeStale(BaseModel):
    days: int


@router.post("/ingest/purge-stale", dependencies=[Depends(require_super_admin)])
async def ingest_purge_stale(body: PurgeStale) -> Dict:
    """Delete catalog rows older than ``days`` (or never seen). Busts the cache.

    The destructive twin of GET /ingest/stale — same predicate. Deletions change
    answers, so the data version is bumped afterwards. Refuses days < 1.
    """

    cutoff = _stale_cutoff(body.days)
    rows = await q(
        "DELETE FROM catalog WHERE last_seen < $1 OR last_seen IS NULL "
        "RETURNING article_code",
        cutoff,
    )
    version = await cache.bump_data_version()
    return {"deleted": len(rows), "cutoff": cutoff.isoformat(), "data_version": version}


@router.get("/sftp")
async def sftp_status() -> Dict:
    """SFTP connection info + pending / archived / failed file listings."""

    from pathlib import Path

    s = get_settings()
    base = Path(s.incoming_dir)

    def listing(p: Path):
        if not p.is_dir():
            return []
        out = []
        files = list(p.glob("*.xlsx")) + list(p.glob("*.csv"))
        for f in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime)})
        return out[:50]

    return {
        # The page's connection card reads the richer GET /sftp/connection; this
        # block is legacy. Report the real configured values rather than a
        # "<server>" placeholder so anything still reading it is not misled.
        "connection": {
            "host": (s.sftp_public_host or "").strip(),
            "port": s.sftp_public_port,
            "user": s.sftp_username,
            "path": "upload/",
        },
        "incoming_dir": str(base),
        "pending": listing(base),
        "archived": listing(base / "archive"),
        "failed": listing(base / "failed"),
        # Effective cadence (Redis override, else the settings default) so the
        # page never claims a poll interval the worker isn't actually using.
        "poll_seconds": await cache.get_poll_seconds(),
    }


# ---- SFTP partner handoff --------------------------------------------------

# Extensions the pipeline will pick up. Kept next to the rules it describes and
# asserted against the real ingest surface in tests/test_sftp_page.py.
INGEST_EXTENSIONS = [".csv", ".xlsx"]


def _detect_kind_keywords() -> Dict[str, List[str]]:
    """The substrings ``ingest.detect_kind`` keys on, read out of the function itself.

    The filename contract is the single thing a partner most needs and the one
    thing the UI never told them. Retyping "article -> catalog, balance|stock|
    inventory -> inventory" into this endpoint would create a second copy that
    silently drifts the first time someone edits ``detect_kind`` — and the way it
    fails is a partner's file landing in ``failed/`` while the page insists the
    name was fine.

    So the keywords are read from ``detect_kind``'s AST: each ``"lit" in name``
    test, paired with the kind its branch returns. A rewrite that no longer looks
    like a chain of substring tests yields an EMPTY mapping — loudly wrong on the
    page and caught by ``test_rules_cover_every_detect_kind_branch`` — rather than
    a stale one that looks right.
    """

    import ast
    import inspect
    import textwrap

    from app.ingest import detect_kind

    tree = ast.parse(textwrap.dedent(inspect.getsource(detect_kind)))
    out: Dict[str, List[str]] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # The branch's kind: `return "catalog"`.
        kinds = [
            n.value.value
            for n in node.body
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        ]
        if not kinds:
            continue

        # Its tests: `"article" in name`, or `"a" in name or "b" in name`.
        tests = node.test.values if isinstance(node.test, ast.BoolOp) else [node.test]
        for t in tests:
            if (
                isinstance(t, ast.Compare)
                and len(t.ops) == 1
                and isinstance(t.ops[0], ast.In)
                and isinstance(t.left, ast.Constant)
                and isinstance(t.left.value, str)
            ):
                out.setdefault(kinds[0], []).append(t.left.value)

    return out


def filename_rules() -> Dict:
    """The ingest filename contract, derived — never retyped.

    ``kind`` on every example is computed by calling ``detect_kind`` on the name
    shown, so the page cannot advertise a "good" name the watcher would reject.
    """

    from app.ingest import detect_kind

    keywords = _detect_kind_keywords()
    good = ["articles-export-2026-07-13.csv", "balance_stock_20260713.xlsx"]
    bad = ["data.csv", "export (1).xlsx"]

    return {
        "extensions": INGEST_EXTENSIONS,
        # e.g. [{"kind": "catalog", "keywords": ["article"]}, …]
        "kinds": [{"kind": k, "keywords": v} for k, v in keywords.items()],
        "good": [{"name": n, "kind": detect_kind(n)} for n in good],
        "bad": [{"name": n, "kind": detect_kind(n)} for n in bad],
        "unmatched_dir": "failed/",
        "archive_dir": "archive/",
    }


def _detect_host(request: Request) -> str:
    """The hostname this request arrived on — hostname only, no scheme, no port.

    A *suggestion*, never an answer. Behind a proxy that does not forward the
    original Host, or when the admin console is reached on a name the sftp port
    is not published under, this is confidently wrong — which is why the caller
    tags it ``host_source="detected"`` and the page asks the operator to confirm
    it rather than printing it as fact.
    """

    raw = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not raw:
        raw = (request.headers.get("host") or "").strip()
    if not raw:
        return ""

    # Tolerate a full URL landing in the header (some proxies do this).
    if "//" in raw:
        raw = raw.split("//", 1)[1]
    raw = raw.split("/", 1)[0].strip()

    # IPv6 literal: [::1]:2222 -> ::1. Splitting on ":" would shred it.
    if raw.startswith("["):
        return raw[1:].split("]", 1)[0]
    return raw.split(":", 1)[0]


@router.get("/sftp/connection", dependencies=[Depends(require_super_admin)])
async def sftp_connection(request: Request) -> Dict:
    """Everything a partner needs to connect — including the shared password.

    super_admin ONLY (``require_super_admin``): the response body carries the
    SFTP account's password, and the router-level ``require_admin`` would hand
    that to every branch admin.

    ``host_source`` says how much to trust ``host``:

    * ``env``      — ``SFTP_PUBLIC_HOST`` is set. Authoritative.
    * ``detected`` — inferred from this request's forwarded/Host header. A
      starting point the operator confirms; it can be flatly wrong behind a
      proxy, and the sftp port need not even be published on that name.
    * ``none``     — neither. The page asks for it.
    """

    s = get_settings()
    env_host = (s.sftp_public_host or "").strip()
    host = env_host or _detect_host(request)
    source = "env" if env_host else ("detected" if host else "none")

    root = Path(s.sftp_keys_dir)

    return {
        "host": host,
        "host_source": source,
        # Kept for the page's existing "is this trustworthy" checks. Only an
        # env-configured host counts as configured — a detected one does not.
        "host_configured": bool(env_host),
        "port": s.sftp_public_port,
        "username": s.sftp_username,
        "password": s.sftp_password,
        "upload_path": "upload/",
        "incoming_dir": s.incoming_dir,
        "poll_seconds": await cache.get_poll_seconds(),
        "rules": filename_rules(),
        # Keys are now registered from this console and take effect on the
        # partner's NEXT connection: sshd re-reads authorized_keys every time,
        # and the api container writes into the same volume the sftp container
        # serves. `available` is False when that volume is not mounted (a dev
        # stack), which is the one case the endpoints below refuse.
        "key_auth": {
            "manageable": True,
            "available": root.is_dir(),
            "keys_dir": str(root),
            "needs_service_restart": False,
        },
    }


# ---- SFTP partner keys ------------------------------------------------------
#
# authorized_keys is remote-code-access control for the sftp container, and the
# key material arrives from a partner over email. Everything below exists to
# keep the two apart.
#
# The line format is `<type> <base64> [comment]`, but OpenSSH ALSO accepts a
# leading options field: `command="…",environment="…" ssh-ed25519 AAAA…` runs
# that command on every login. A partner-supplied (or partner-relayed) key
# carrying options is remote code execution, so the parser below accepts a line
# ONLY if it begins with a known key type — anything else, including options, is
# rejected before it reaches disk. An embedded newline is the same attack in a
# second line, so it is rejected too.
#
# We never write the partner's comment field; the canonical line we store is
# `<type> <base64> pharma:<label>`, built from a label that matches
# _SFTP_LABEL_RE. Nothing an attacker controls ends up outside the base64 blob,
# whose bytes we have already decoded and type-checked.

SFTP_KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
)
MAX_SFTP_KEYS = 50
MAX_SFTP_KEY_CHARS = 4096          # a 4096-bit RSA key is ~740 chars
_SFTP_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")

# The uid/gid atmoz/sftp runs the pharma account as. sshd refuses an
# authorized_keys it does not own, or one that is group/world-writable.
_SFTP_UID = 1001
_SFTP_GID = 1001

# Serialises the read-modify-write of authorized_keys. Two concurrent POSTs
# would otherwise both read the old file and the second would erase the first.
_sftp_keys_lock = asyncio.Lock()


class SftpKey(BaseModel):
    label: str
    public_key: str


def _keys_root() -> Path:
    """The mounted .ssh directory, or a 503 that says exactly what is missing."""

    root = Path(get_settings().sftp_keys_dir)
    if not root.is_dir():
        raise HTTPException(
            status_code=503,
            detail=(
                f"SFTP key directory {root} is not mounted, so a key registered here "
                "would never reach sshd. Mount the `sftp_ssh` volume into the api "
                "container at that path (env SFTP_KEYS_DIR) and into the sftp "
                "container at /home/pharma/.ssh."
            ),
        )
    return root


def _clean_label(label: str) -> str:
    """Labels become filenames (``keys/<label>.pub``), so keep them boring."""

    label = (label or "").strip()
    if not _SFTP_LABEL_RE.match(label):
        raise HTTPException(
            status_code=400,
            detail=(
                "label must be 1-48 chars of letters, digits, dot, dash or underscore "
                "and start with a letter or digit (it becomes a filename)"
            ),
        )
    return label


def _parse_public_key(raw: str) -> Dict[str, str]:
    """Validate ONE OpenSSH public key line. Rejects everything else.

    Returns ``{"type", "b64", "fingerprint"}``. Raises 400 with a reason a human
    can act on — an operator pasting a key needs to know *which* thing was wrong.
    """

    raw = raw or ""
    if len(raw) > MAX_SFTP_KEY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"public key is too long (max {MAX_SFTP_KEY_CHARS} characters)",
        )

    # Surrounding whitespace is normal — a .pub file ends in a newline and that
    # is what an operator pastes. An INTERIOR newline is not: it makes this two
    # authorized_keys entries, and the second one is whatever the sender wants,
    # unvalidated. Same for any other control byte.
    key = raw.strip()
    # A real .pub is one line of space-separated ASCII. Reject every control
    # char INCLUDING tab: a tab lets `type\t<blob>\tcommand="…"` split into
    # three clean fields, so the options ride in disguised as a comment. The
    # write path discards the comment anyway, but an honest parser must not
    # return "ok" for a line carrying an injection attempt.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in key):
        raise HTTPException(
            status_code=400,
            detail=(
                "a public key is a single line — this contains a line break, tab, or "
                "control character. Paste exactly one line from the partner's .pub file."
            ),
        )

    parts = key.split()
    if len(parts) < 2:
        raise HTTPException(
            status_code=400,
            detail="expected an OpenSSH public key line: '<type> <base64> [comment]'",
        )

    ktype = parts[0]
    if ktype not in SFTP_KEY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"key must start with one of {', '.join(SFTP_KEY_TYPES)}. A line that "
                "starts with anything else — in particular authorized_keys options "
                'like command="…", environment="…" or permitopen="…" — is refused: '
                "those execute on the SFTP server at login."
            ),
        )

    b64 = parts[1]
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:  # noqa: BLE001 — binascii.Error and friends
        raise HTTPException(status_code=400, detail="key material is not valid base64")

    # An OpenSSH key blob opens with its own type as a length-prefixed string.
    # If that disagrees with the line's prefix, the line is lying about what it
    # is — reject rather than guess which half to believe.
    if len(blob) < 4:
        raise HTTPException(status_code=400, detail="key material is truncated")
    n = int.from_bytes(blob[:4], "big")
    if n <= 0 or n > 64 or len(blob) < 4 + n:
        raise HTTPException(status_code=400, detail="key material is not an OpenSSH key blob")
    embedded = blob[4 : 4 + n].decode("ascii", "replace")
    if embedded != ktype:
        raise HTTPException(
            status_code=400,
            detail=f"key type mismatch: the line says '{ktype}' but the key material is '{embedded}'",
        )

    # Exactly what `ssh-keygen -lf key.pub` prints, so an operator can read it
    # back to the partner over the phone and compare, character for character.
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return {"type": ktype, "b64": b64, "fingerprint": f"SHA256:{digest}"}


def _canonical_line(ktype: str, b64: str, label: str) -> str:
    """The only shape we ever write. The comment is OUR label, not the partner's."""

    return f"{ktype} {b64} pharma:{label}"


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Write + fsync a temp file, then rename over the target.

    authorized_keys is read by sshd on every connection, including while we are
    rewriting it. A rename is atomic, so a partner mid-connect sees the old file
    or the new one — never a half-written one that locks everybody out.
    """

    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    # sshd ignores an authorized_keys owned by the wrong user. The sftp
    # container's boot script fixes ownership too, so a failure here (we are not
    # root) is survivable, not fatal.
    try:
        os.chown(tmp, _SFTP_UID, _SFTP_GID)
    except (PermissionError, OSError):
        pass
    os.replace(tmp, path)


def _read_keys(root: Path) -> List[Dict]:
    """Registered keys, read back from ``keys/*.pub`` — the durable half.

    keys/ is the registry (it survives a container rebuild); authorized_keys is
    the live copy sshd reads. Listing from keys/ means the page shows what will
    still be there after a restart.
    """

    kdir = root / "keys"
    if not kdir.is_dir():
        return []

    out: List[Dict] = []
    for pub in sorted(kdir.glob("*.pub")):
        try:
            parsed = _parse_public_key(pub.read_text(encoding="utf-8", errors="replace").strip())
        except HTTPException:
            continue  # a hand-placed file we did not write; don't crash the page
        except OSError:
            continue
        out.append(
            {
                "label": pub.stem,
                "type": parsed["type"],
                "fingerprint": parsed["fingerprint"],
                "added_at": int(pub.stat().st_mtime),
            }
        )
    return out


@router.get("/sftp/keys", dependencies=[Depends(require_super_admin)])
async def sftp_keys_list() -> List[Dict]:
    """Registered partner keys. Fingerprints only — never the raw key material.

    The fingerprint is what an operator can actually verify (the partner reads
    theirs off `ssh-keygen -lf id_ed25519.pub`); echoing the blob back just
    invites pasting it somewhere else.
    """

    return _read_keys(_keys_root())


@router.post("/sftp/keys", dependencies=[Depends(require_super_admin)])
async def sftp_keys_add(k: SftpKey) -> Dict:
    """Register a partner's public key. Live on their next connection.

    Writes BOTH halves under one lock: ``authorized_keys`` (sshd re-reads it per
    connection → no restart) and ``keys/<label>.pub`` (atmoz/sftp rebuilds
    authorized_keys from this dir at boot → without it the key dies at the next
    restart).
    """

    root = _keys_root()
    label = _clean_label(k.label)
    parsed = _parse_public_key(k.public_key)

    async with _sftp_keys_lock:
        existing = _read_keys(root)
        if len(existing) >= MAX_SFTP_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"key limit reached ({MAX_SFTP_KEYS}); remove an unused key first",
            )
        for e in existing:
            if e["label"] == label:
                raise HTTPException(status_code=409, detail=f"a key labelled '{label}' already exists")
            if e["fingerprint"] == parsed["fingerprint"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"this key is already registered as '{e['label']}'",
                )

        line = _canonical_line(parsed["type"], parsed["b64"], label)

        (root / "keys").mkdir(parents=True, exist_ok=True)
        _atomic_write(root / "keys" / f"{label}.pub", line + "\n", 0o644)

        ak = root / "authorized_keys"
        try:
            current = ak.read_text(encoding="utf-8") if ak.exists() else ""
        except OSError:
            current = ""
        # Keep any line an operator put there by hand; append ours.
        lines = [ln for ln in current.splitlines() if ln.strip()]
        lines.append(line)
        _atomic_write(ak, "\n".join(lines) + "\n", 0o600)

    return {
        "status": "ok",
        "label": label,
        "type": parsed["type"],
        "fingerprint": parsed["fingerprint"],
        "active": "immediately",
    }


@router.delete("/sftp/keys/{label}", dependencies=[Depends(require_super_admin)])
async def sftp_keys_delete(label: str) -> Dict:
    """Revoke a key. Removed from authorized_keys AND keys/ — or it comes back.

    Deleting only the .pub file leaves the partner connecting until the next
    restart; deleting only the authorized_keys line lets the boot rebuild
    resurrect it. Both, always.
    """

    root = _keys_root()
    label = _clean_label(label)

    async with _sftp_keys_lock:
        pub = root / "keys" / f"{label}.pub"
        if not pub.is_file():
            raise HTTPException(status_code=404, detail=f"no key labelled '{label}'")

        try:
            b64 = _parse_public_key(pub.read_text(encoding="utf-8").strip())["b64"]
        except HTTPException:
            b64 = ""

        pub.unlink()

        ak = root / "authorized_keys"
        if ak.exists():
            kept = []
            for ln in ak.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                parts = ln.split()
                # Match on the key material, not the comment: that is the thing
                # sshd actually authenticates against.
                if b64 and len(parts) >= 2 and parts[1] == b64:
                    continue
                if len(parts) >= 3 and parts[2] == f"pharma:{label}":
                    continue
                kept.append(ln)
            _atomic_write(ak, ("\n".join(kept) + "\n") if kept else "", 0o600)

    return {"status": "ok", "removed": label}


# ---- user management -------------------------------------------------------


class NewUser(BaseModel):
    email: str
    name: str = ""
    password: Optional[str] = None
    role: str = "user"


class UserPatch(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    approved: Optional[bool] = None
    password: Optional[str] = None
    # Pin this account to one branch (see caller_store_scope). "" clears it back
    # to the global view. Distinct from None, which means "leave unchanged".
    store_id: Optional[str] = None


@router.get("/users")
async def users_list() -> List[Dict]:
    from app import auth

    users = await auth.list_users()
    # auth._public() does not carry store_id, and auth.py is off-limits here, so
    # the scope is joined on rather than smuggled into the auth module.
    try:
        scopes = {r["email"]: r["store_id"] for r in await q("SELECT email, store_id FROM users")}
    except Exception:  # noqa: BLE001 — column not added yet
        scopes = {}
    for u in users:
        u["store_id"] = scopes.get(u["email"])
    return users


@router.post("/users")
async def users_create(u: NewUser) -> Dict:
    from app import auth

    try:
        return await auth.create_user(u.email, u.name, u.password, u.role)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/users/{user_id}")
async def users_update(user_id: int, p: UserPatch) -> Dict:
    from app import auth

    out: Dict = {}
    if any(v is not None for v in (p.role, p.active, p.approved, p.password)):
        try:
            out = await auth.update_user(user_id, role=p.role, active=p.active,
                                         approved=p.approved, password=p.password)
        except auth.AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if p.store_id is not None:
        # "" clears the pin (back to the global view); any other value scopes the
        # account to that branch. Enforcement is in caller_store_scope, not here.
        store = p.store_id.strip() or None
        rows = await q(
            "UPDATE users SET store_id=$1 WHERE id=$2 RETURNING email, role, store_id",
            store, user_id,
        )
        if not rows:
            raise HTTPException(status_code=400, detail="user not found")
        out = {**out, "store_id": rows[0]["store_id"]}

    if not out:
        raise HTTPException(status_code=400, detail="nothing to update")
    return out


@router.delete("/users/{user_id}")
async def users_delete(user_id: int) -> Dict:
    from app import auth

    n = await auth.delete_user(user_id)
    return {"status": "ok", "removed": n}


# ---- chat logging helper (used by api.py) ---------------------------------


async def ensure_chat_logs() -> None:
    """Create the chat_logs table if missing (non-destructive)."""

    await q(
        """CREATE TABLE IF NOT EXISTS chat_logs (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               lang TEXT, store_id TEXT,
               question TEXT, answer TEXT,
               cached BOOLEAN, latency_ms INT
           )"""
    )


async def prune_chat_logs() -> int:
    """Delete chat logs older than the configured retention. Returns rows removed."""

    days = get_settings().chat_log_retention_days
    try:
        rows = await q(
            "DELETE FROM chat_logs WHERE ts < now() - ($1 || ' days')::interval RETURNING id",
            str(days),
        )
        return len(rows)
    except Exception:
        return 0


async def log_chat(question: str, answer: str, store_id, cached: bool, latency_ms: int) -> None:
    """Persist one Q+A for the conversations view. Best-effort."""

    lang = "MY" if any("က" <= ch <= "႟" for ch in question) else "EN"
    try:
        await q(
            """INSERT INTO chat_logs (lang, store_id, question, answer, cached, latency_ms)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            lang, store_id, question, answer, cached, latency_ms,
        )
    except Exception:
        pass


# ---- self-learning audit + chat feedback -----------------------------------


class FeedbackIn(BaseModel):
    session_id: str = ""
    store_id: Optional[str] = None
    model: str = ""
    question: str = ""
    answer: str = ""
    tools: List[str] = []
    verdict: str = "up"  # 'up' | 'down'
    correction: str = ""


async def ensure_feedback() -> None:
    """Create the chat_feedback table if missing (non-destructive)."""

    await q(
        """CREATE TABLE IF NOT EXISTS chat_feedback (
               id BIGSERIAL PRIMARY KEY,
               ts TIMESTAMPTZ DEFAULT now(),
               session_id TEXT, store_id TEXT, model TEXT,
               question TEXT, answer TEXT, tools JSONB,
               verdict TEXT, correction TEXT
           )"""
    )


@router.post("/feedback")
async def post_feedback(fb: FeedbackIn) -> Dict:
    """Capture a thumbs up/down (and optional correction) on one answer."""

    verdict = fb.verdict if fb.verdict in ("up", "down") else "up"
    try:
        rows = await q(
            """INSERT INTO chat_feedback
                   (session_id, store_id, model, question, answer, tools, verdict, correction)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8) RETURNING id""",
            fb.session_id, fb.store_id, fb.model, fb.question, fb.answer,
            json.dumps(fb.tools or []), verdict, fb.correction,
        )
        return {"ok": True, "id": rows[0]["id"] if rows else None}
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="could not save feedback")


@router.get("/feedback")
async def get_feedback(limit: int = 100, verdict: str = "") -> List[Dict]:
    """Recent feedback rows, newest first; optional verdict filter."""

    limit = min(max(limit, 1), 500)
    conds, params = [], []
    if verdict in ("up", "down"):
        params.append(verdict)
        conds.append(f"verdict = ${len(params)}")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params.append(limit)
    try:
        return await q(
            f"""SELECT id, ts, verdict, model, question, answer, correction, store_id, session_id
                  FROM chat_feedback {where} ORDER BY id DESC LIMIT ${len(params)}""",
            *params,
        )
    except Exception:  # noqa: BLE001 - table not created yet
        return []


@router.get("/feedback/stats")
async def feedback_stats() -> Dict:
    """Up / down / correction counts."""

    try:
        rows = await q("SELECT verdict, count(*) c FROM chat_feedback GROUP BY verdict")
        up = sum(int(r["c"]) for r in rows if r["verdict"] == "up")
        down = sum(int(r["c"]) for r in rows if r["verdict"] == "down")
        corr = await q(
            "SELECT count(*) c FROM chat_feedback WHERE correction IS NOT NULL AND correction <> ''"
        )
        return {"up": up, "down": down, "corrections": int(corr[0]["c"]) if corr else 0, "total": up + down}
    except Exception:  # noqa: BLE001
        return {"up": 0, "down": 0, "corrections": 0, "total": 0}


# Epoch (s / ms / µs) -> timestamp, tolerant of whichever unit Agno wrote.
_AGNO_TS = (
    "to_char(to_timestamp(CASE "
    "WHEN COALESCE(updated_at, created_at) > 1000000000000000 THEN COALESCE(updated_at, created_at)/1000000.0 "
    "WHEN COALESCE(updated_at, created_at) > 1000000000000 THEN COALESCE(updated_at, created_at)/1000.0 "
    "ELSE COALESCE(updated_at, created_at)::float8 END), 'YYYY-MM-DD HH24:MI')"
)


@router.get("/learning")
async def list_learning(limit: int = 200) -> List[Dict]:
    """What the agent has learned (Agno's agno_learnings store), newest first.

    ``summary`` pulls the most human field from the JSON content with fallbacks.
    """

    limit = min(max(limit, 1), 500)
    try:
        return await q(
            f"""SELECT learning_id AS id, learning_type, user_id,
                       COALESCE(content->>'summary', content->>'context',
                                content->'memories'->0->>'content',
                                content->>'memory', content->>'content',
                                content->>'text', left(content::text, 300)) AS summary,
                       {_AGNO_TS} AS updated_at
                  FROM agno_learnings
                 ORDER BY COALESCE(updated_at, created_at) DESC
                 LIMIT $1""",
            limit,
        )
    except Exception:  # noqa: BLE001 - learning disabled / table absent
        return []


@router.get("/learning/stats")
async def learning_stats() -> Dict:
    """Memory counts by learning type + distinct users learned about."""

    try:
        by = await q("SELECT learning_type, count(*) c FROM agno_learnings GROUP BY learning_type")
        by_type = {r["learning_type"]: int(r["c"]) for r in by}
        users = await q("SELECT count(DISTINCT user_id) c FROM agno_learnings WHERE user_id IS NOT NULL")
        return {"total": sum(by_type.values()), "users": int(users[0]["c"]) if users else 0, "by_type": by_type}
    except Exception:  # noqa: BLE001
        return {"total": 0, "users": 0, "by_type": {}}


@router.delete("/learning/{learning_id}")
async def delete_learning(learning_id: str) -> Dict:
    """Forget one learned memory."""

    try:
        await q("DELETE FROM agno_learnings WHERE learning_id = $1", learning_id)
        return {"ok": True}
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="could not delete")


# ---- MySQL source sync (pull client app DB -> our Postgres) ----------------


@router.get("/sync/mysql/config")
async def sync_config() -> Dict:
    """Show the MySQL sync configuration (password redacted)."""

    s = get_settings()
    return {
        "enabled": s.mysql_sync_enabled,
        "host": s.mysql_host,
        "port": s.mysql_port,
        "db": s.mysql_db,
        "user": s.mysql_user,
        "password_set": bool(s.mysql_password),
        "catalog_sql": s.mysql_catalog_sql,
        "inventory_sql": s.mysql_inventory_sql,
    }


@router.post("/sync/mysql")
async def run_sync(pipeline: bool = True) -> Dict:
    """Pull catalog + inventory from the client's MySQL into Postgres.

    Read-only on the client side. ``pipeline=false`` skips the embed/graph
    rebuild (faster; just refresh the rows).
    """

    from app.cache import bump_data_version
    from app.sync_mysql import sync_mysql

    res = await sync_mysql(run_pipeline=pipeline)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "sync failed"))

    # This path rewrites inventory. Every other writer (the SFTP watcher, the
    # reload endpoint) bumps the data version; this one did not, so cached stock
    # answers survived a sync and were served for up to CACHE_TTL_SECONDS.
    res["data_version"] = await bump_data_version()
    return res


__all__ = [
    "router",
    "ensure_chat_logs",
    "ensure_feedback",
    "ensure_admin_schema",
    "caller_store_scope",
    "log_chat",
]
