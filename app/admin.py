"""Admin API — backs the SvelteKit management panel.

Read-mostly endpoints over the existing data + a little CRUD for credentials
and config. Mounted under /admin in app.api.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app import cache
from app.config import get_settings
from app.db import counts, q

router = APIRouter(prefix="/admin", tags=["admin"])


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
async def catalog_one(code: str) -> Dict:
    """Full article detail + per-site stock + summary."""

    rows = await q("SELECT * FROM catalog WHERE article_code=$1", code)
    if not rows:
        raise HTTPException(status_code=404, detail="article not found")
    article = {k: v for k, v in rows[0].items() if k != "embedding"}
    sites = await q(
        """SELECT site_code, stock_qty, price FROM inventory
            WHERE article_code=$1 ORDER BY stock_qty DESC""",
        code,
    )
    for s in sites:
        s["price"] = float(s["price"]) if s["price"] is not None else None
    total = sum(s["stock_qty"] or 0 for s in sites)
    return {"article": article, "sites": sites, "total_stock": total, "site_count": len(sites)}


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
        r["total_stock"] = int(r["total_stock"] or 0)
        r["weighted_avg_price"] = float(r["weighted_avg_price"] or 0)
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

    from app.graph import build_edges

    return await build_edges()


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
        "total_stock": int(stock[0]["total_stock"]) if stock else None,
        "site_count": int(stock[0]["site_count"]) if stock else None,
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


@router.get("/sftp")
async def sftp_status() -> Dict:
    """SFTP connection info + pending / archived / failed file listings."""

    from pathlib import Path

    base = Path(get_settings().incoming_dir)

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
        "connection": {"host": "<server>", "port": 2222, "user": "pharma", "path": "upload/"},
        "incoming_dir": str(base),
        "pending": listing(base),
        "archived": listing(base / "archive"),
        "failed": listing(base / "failed"),
        "poll_seconds": get_settings().watch_interval_seconds,
    }


# ---- user management -------------------------------------------------------


class NewUser(BaseModel):
    email: str
    name: str = ""
    password: Optional[str] = None
    role: str = "user"


class UserPatch(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


@router.get("/users")
async def users_list() -> List[Dict]:
    from app import auth

    return await auth.list_users()


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

    try:
        return await auth.update_user(user_id, role=p.role, active=p.active, password=p.password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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

    from app.sync_mysql import sync_mysql

    res = await sync_mysql(run_pipeline=pipeline)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "sync failed"))
    return res


__all__ = ["router", "ensure_chat_logs", "ensure_feedback", "log_chat"]
