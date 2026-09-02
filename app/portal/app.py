"""SEDITracker portal — FastAPI on :8002.

Reads from /opt/sedi/app/portal/sedi.db. Templates inherit MNT's base.html
(red ribbon, same look-and-feel) so MNT and SEDITracker share visual identity.

Routes:
  /                  Recent insider activity firehose (last 60 days, all tickers)
  /ticker/{ticker}   Per-ticker insider history
  /insider/{slug}    All trades by one insider across all tickers
  /search            Free-text search across insider names + tickers
"""
from __future__ import annotations
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

DB_PATH = "/opt/sedi/app/portal/sedi.db"
TICKERS_PATH = "/opt/sedi/app/tickers.json"
TEMPLATES_DIR = "/opt/sedi/app/portal/templates"
STATIC_DIR = "/opt/sedi/app/portal/static"

app = FastAPI(title="SEDITracker")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ---------- DB helpers ----------

def get_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def _ticker_to_name(ticker: str) -> str | None:
    try:
        for r in json.load(open(TICKERS_PATH)):
            if isinstance(r, dict) and r.get("ticker") == ticker:
                return r.get("name")
    except Exception:
        pass
    return None


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "x"


def _insider_slug(name: str) -> str:
    return _slugify(name)[:80]


# ST_NAMES_V1: display-quality helpers
_GARBAGE_NAME_RE = re.compile(r'^[A-Z][a-z]+$')   # e.g. 'Pml', 'Ele', 'Ngex'

def _is_garbage_name(name: str, ticker: str) -> bool:
    """A name is garbage if it's just the title-cased bare ticker (e.g. PML.V -> 'Pml')."""
    if not name or not ticker:
        return False
    bare = ticker.split('.')[0]
    if not _GARBAGE_NAME_RE.match(name):
        return False
    # Allow names that legitimately look like 'Aris' even if Aris is a ticker — only
    # mark garbage when the name is exactly the bare ticker title-cased / capitalized.
    return name.lower() == bare.lower() or name == bare.title() or name == bare.capitalize()


def _smart_case(s: str) -> str:
    """Aggressive title-case for ALL-CAPS tokens of 2+ chars when ANY 4+-char
    all-caps token exists. Used for SEDI insider names like 'MASSON, Richard Henry'
    where the surname is filed in caps."""
    if not s:
        return s
    tokens = re.split(r'([\s,./\-&])', s)
    has_long_caps = any(re.fullmatch(r'[A-Z]{4,}', t) for t in tokens)
    if not has_long_caps:
        return s
    out = []
    for tok in tokens:
        if re.fullmatch(r'[A-Z]{2,}', tok):
            out.append(tok[0] + tok[1:].lower())
        else:
            out.append(tok)
    return ''.join(out)


def _smart_case_company(s: str) -> str:
    """ST_NAMES_V2: conservative title-case for company names. Only fires when
    the entire string is ALL-CAPS (zero lowercase letters) — preserves stylized
    brand names like 'STLLR Gold', 'POWR Lithium', 'AKITA Drilling', 'KORE Mining'.

    'HEADWATER EXPLORATION INC' -> 'Headwater Exploration Inc'  (entirely caps)
    'STLLR Gold Inc'            -> 'STLLR Gold Inc'              (has lowercase)
    'POWR Lithium Corp.'        -> 'POWR Lithium Corp.'          (has lowercase)
    'ABOUND Energy Inc'         -> 'ABOUND Energy Inc'           (has lowercase)
    """
    if not s:
        return s
    # If string contains any lowercase letter, treat it as already mixed-case
    if re.search(r'[a-z]', s):
        return s
    # All caps: title-case each 2+-char ALL-CAPS token
    return _smart_case(s)


def _display_company_name(raw_name: str | None, ticker: str) -> str:
    """Final name shown to users. Strips press-release date prefixes, hides
    garbage names like 'Pml', and smart-cases ALL-CAPS company names."""
    nm = (raw_name or '').strip()
    # ST_NAMES_V2: Strip leading "Month dd, yyyy / " or similar press-release prefixes
    nm = re.sub(r'^(?:[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s*/\s*)', '', nm)
    if not nm or _is_garbage_name(nm, ticker):
        return '—'   # ST_NAMES_V2: em-dash when we have no real name (avoid Ticker|Ticker repetition)
    return _smart_case_company(nm)



def _ctx(request: Request, page: str, **kwargs):
    base = {
        "request": request,
        "page": page,
        "now_str": datetime.utcnow().strftime("%A, %B %d, %Y").upper(),
        "is_admin": False,
    }
    base.update(kwargs)
    return base


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, days: int = 60):
    con = get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT t.ticker, t.txn_date, i.name, t.txn_type, t.shares, t.price, "
        "       t.total_value, t.notes "
        "FROM transactions t LEFT JOIN insiders i ON i.insider_id = t.insider_id "
        "WHERE t.txn_date >= ? "
        "ORDER BY t.txn_date DESC, t.txn_id DESC LIMIT 200",
        (cutoff,),
    ).fetchall()

    decorated = []
    for r in rows:
        d = dict(r)
        d["company_name"] = _display_company_name(_ticker_to_name(d["ticker"]), d["ticker"])
        d["insider_slug"] = _insider_slug(d["name"]) if d["name"] else ""
        decorated.append(d)

    total_txns = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    total_tickers = con.execute(
        "SELECT COUNT(DISTINCT ticker) FROM transactions"
    ).fetchone()[0]

    return templates.TemplateResponse(
        request, "home.html",
        _ctx(request, "home",
             rows=decorated, days=days,
             total_txns=total_txns, total_tickers=total_tickers),
    )


@app.get("/ticker/{ticker}", response_class=HTMLResponse)
def ticker_page(request: Request, ticker: str):
    ticker = ticker.upper()
    con = get_conn()
    rows = con.execute(
        "SELECT t.txn_date, i.name, t.txn_type, t.shares, t.price, "
        "       t.total_value, t.post_balance, t.notes "
        "FROM transactions t LEFT JOIN insiders i ON i.insider_id = t.insider_id "
        "WHERE t.ticker = ? "
        "ORDER BY t.txn_date DESC, t.txn_id DESC LIMIT 500",
        (ticker,),
    ).fetchall()
    rows = [dict(r) for r in rows]
    for r in rows:
        r["insider_slug"] = _insider_slug(r["name"]) if r["name"] else ""

    coverage = con.execute(
        "SELECT first_txn_date, last_txn_date, last_scraped_at, n_transactions "
        "FROM ticker_coverage WHERE ticker = ?",
        (ticker,),
    ).fetchone()

    return templates.TemplateResponse(
        request, "ticker.html",
        _ctx(request, "ticker",
             ticker=ticker,
             company_name=_display_company_name(_ticker_to_name(ticker), ticker),
             rows=rows,
             coverage=dict(coverage) if coverage else None),
    )


@app.get("/insider/{slug}", response_class=HTMLResponse)
def insider_page(request: Request, slug: str):
    con = get_conn()
    matches = con.execute(
        "SELECT insider_id, name FROM insiders "
        "WHERE name_norm LIKE ? OR name LIKE ? "
        "LIMIT 5",
        (f"%{slug.replace('-', '')}%", f"%{slug}%"),
    ).fetchall()
    if not matches:
        return templates.TemplateResponse(
            request, "insider.html",
            _ctx(request, "insider", insider_name=None, rows=[]),
        )
    insider = dict(matches[0])
    rows = con.execute(
        "SELECT t.ticker, t.txn_date, t.txn_type, t.shares, t.price, "
        "       t.total_value, t.post_balance, t.notes "
        "FROM transactions t WHERE t.insider_id = ? "
        "ORDER BY t.txn_date DESC, t.txn_id DESC LIMIT 500",
        (insider["insider_id"],),
    ).fetchall()
    rows = [dict(r) for r in rows]
    for r in rows:
        r["company_name"] = _display_company_name(_ticker_to_name(r["ticker"]), r["ticker"])
        r["name"] = _smart_case(r["name"]) if r.get("name") else r.get("name")
    return templates.TemplateResponse(
        request, "insider.html",
        _ctx(request, "insider",
             insider_name=insider["name"], rows=rows),
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    q = (q or "").strip()
    con = get_conn()
    insider_hits = []
    ticker_hits = []
    if q and len(q) >= 2:
        insider_hits = [dict(r) for r in con.execute(
            "SELECT i.insider_id, i.name, COUNT(*) n "
            "FROM insiders i JOIN transactions t ON t.insider_id = i.insider_id "
            "WHERE i.name LIKE ? "
            "GROUP BY i.insider_id ORDER BY n DESC LIMIT 25",
            (f"%{q}%",),
        ).fetchall()]
        for r in insider_hits:
            r["slug"] = _insider_slug(r["name"])

        ticker_hits = [dict(r) for r in con.execute(
            "SELECT ticker, COUNT(*) n FROM transactions "
            "WHERE ticker LIKE ? GROUP BY ticker ORDER BY n DESC LIMIT 10",
            (f"%{q.upper()}%",),
        ).fetchall()]
        for r in ticker_hits:
            r["company_name"] = _display_company_name(_ticker_to_name(r["ticker"]), r["ticker"])
        r["name"] = _smart_case(r["name"]) if r.get("name") else r.get("name")

    return templates.TemplateResponse(
        request, "search.html",
        _ctx(request, "search",
             q=q, insider_hits=insider_hits, ticker_hits=ticker_hits),
    )


@app.get("/tickers", response_class=HTMLResponse)
def tickers_page(request: Request):
    con = get_conn()
    rows = con.execute(
        "SELECT ticker, COUNT(*) as n, MIN(txn_date) as first_d, MAX(txn_date) as last_d "
        "FROM transactions GROUP BY ticker ORDER BY MAX(txn_date) DESC"
    ).fetchall()
    rows = [dict(r) for r in rows]
    for r in rows:
        r["company_name"] = _display_company_name(_ticker_to_name(r["ticker"]), r["ticker"])
        r["name"] = _smart_case(r["name"]) if r.get("name") else r.get("name")
    return templates.TemplateResponse(
        request, "tickers.html",
        _ctx(request, "tickers", rows=rows),
    )


@app.get("/insiders", response_class=HTMLResponse)
def insiders_page(request: Request):
    con = get_conn()
    rows = con.execute(
        "SELECT i.insider_id, i.name, COUNT(t.txn_id) as n, "
        "       COUNT(DISTINCT t.ticker) as n_tickers, "
        "       MAX(t.txn_date) as last_d "
        "FROM insiders i JOIN transactions t ON t.insider_id = i.insider_id "
        "GROUP BY i.insider_id ORDER BY MAX(t.txn_date) DESC LIMIT 500"
    ).fetchall()
    rows = [dict(r) for r in rows]
    for r in rows:
        r["slug"] = _insider_slug(r["name"])
    return templates.TemplateResponse(
        request, "insiders.html",
        _ctx(request, "insiders", rows=rows),
    )


@app.get("/healthz")
def healthz():
    con = get_conn()
    n_txn = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    n_tkr = con.execute("SELECT COUNT(DISTINCT ticker) FROM transactions").fetchone()[0]
    return {"ok": True, "transactions": n_txn, "tickers_covered": n_tkr}


# ===================== JSON API for MTP integration ============================
# /* ST_JSON_API_V1 */
from fastapi.responses import JSONResponse  # noqa: E402

def _row_to_mtp(r):
    """Map ST transactions row -> MTP D.insider_filings schema."""
    d = dict(r)
    tx_type_raw = (d.get("txn_type") or "")
    tt = tx_type_raw.lower()
    return {
        "filing_id":        d.get("txn_id"),
        "ticker":           d.get("ticker"),
        "filing_date":      d.get("txn_date"),
        "transaction_date": d.get("txn_date"),
        "insider_name":     d.get("name") or "",
        "insider_slug":     _insider_slug(d.get("name") or ""),
        "security_type":    "Common Shares",
        "nature_code":      None,
        "nature_desc":      tx_type_raw,
        "volume":           d.get("shares"),
        "price":            d.get("price"),
        "value":            d.get("total_value"),
        "tx_type":          ("buy"  if ("buy" in tt or "purchase" in tt or "acquisition" in tt)
                       else  "sell" if ("sell" in tt or "disposition" in tt)
                       else  tx_type_raw),
        "notes":            d.get("notes"),
    }


_BASE_SQL = (
    "SELECT t.txn_id, t.ticker, t.txn_date, t.txn_type, t.shares, t.price, "
    "       t.total_value, t.notes, i.name "
    "FROM transactions t LEFT JOIN insiders i ON i.insider_id = t.insider_id "
)


@app.get("/api/v1/insiders/recent")
def api_insiders_recent(days: int = 60, limit: int = 200, offset: int = 0):
    days   = max(1, min(days, 365 * 5))
    limit  = max(1, min(limit, 50000))
    offset = max(0, offset)
    con = get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = con.execute(
        _BASE_SQL + "WHERE t.txn_date >= ? ORDER BY t.txn_date DESC, t.txn_id DESC LIMIT ? OFFSET ?",
        (cutoff, limit, offset),
    ).fetchall()
    return {
        "filings":    [_row_to_mtp(r) for r in rows],
        "days":       days,
        "limit":      limit,
        "offset":     offset,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/api/v1/insiders/by-ticker/{ticker}")
def api_insiders_by_ticker(ticker: str, limit: int = 200, offset: int = 0, days: int = 0):
    ticker = (ticker or "").upper().strip()
    limit  = max(1, min(limit, 50000))
    offset = max(0, offset)
    con = get_conn()
    if days > 0:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = con.execute(
            _BASE_SQL + "WHERE t.ticker = ? AND t.txn_date >= ? "
                        "ORDER BY t.txn_date DESC, t.txn_id DESC LIMIT ? OFFSET ?",
            (ticker, cutoff, limit, offset),
        ).fetchall()
    else:
        rows = con.execute(
            _BASE_SQL + "WHERE t.ticker = ? ORDER BY t.txn_date DESC, t.txn_id DESC LIMIT ? OFFSET ?",
            (ticker, limit, offset),
        ).fetchall()
    return {
        "ticker":     ticker,
        "filings":    [_row_to_mtp(r) for r in rows],
        "limit":      limit,
        "offset":     offset,
        "days":       days,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/api/v1/insiders/by-name/{slug}")
def api_insiders_by_name(slug: str, limit: int = 200, offset: int = 0):
    slug   = (slug or "").lower().strip()
    limit  = max(1, min(limit, 50000))
    offset = max(0, offset)
    con = get_conn()
    rows = con.execute(
        _BASE_SQL + "WHERE i.insider_id IN ("
                    "  SELECT insider_id FROM insiders WHERE name_norm = ? OR name = ?"
                    ") ORDER BY t.txn_date DESC, t.txn_id DESC LIMIT ? OFFSET ?",
        (slug.replace("-", ""), slug, limit, offset),
    ).fetchall()
    return {
        "slug":       slug,
        "filings":    [_row_to_mtp(r) for r in rows],
        "limit":      limit,
        "offset":     offset,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/api/v1/insiders/stats")
def api_insiders_stats():
    con = get_conn()
    total  = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    unique = con.execute("SELECT COUNT(DISTINCT insider_id) FROM transactions").fetchone()[0]
    by_type_rows = con.execute(
        "SELECT LOWER(txn_type), COUNT(*) FROM transactions "
        "GROUP BY LOWER(txn_type) ORDER BY 2 DESC"
    ).fetchall()
    by_type = {k: v for k, v in by_type_rows}
    market_buys  = sum(v for k, v in by_type.items() if k and ("buy" in k or "purchase" in k or "acquisition" in k))
    market_sells = sum(v for k, v in by_type.items() if k and ("sell" in k or "disposition" in k))
    top_tickers_rows = con.execute(
        "SELECT ticker, COUNT(*) AS n FROM transactions GROUP BY ticker "
        "ORDER BY n DESC LIMIT 25"
    ).fetchall()
    top_tickers = [{"ticker": r[0], "filings": r[1]} for r in top_tickers_rows]
    newest_date = con.execute("SELECT MAX(txn_date) FROM transactions").fetchone()[0]
    return {
        "total_filings":   total,
        "unique_insiders": unique,
        "market_buys":     market_buys,
        "market_sells":    market_sells,
        "tx_types":        by_type,
        "top_tickers":     top_tickers,
        "newest_date":     newest_date,
        "fetched_at":      datetime.utcnow().isoformat() + "Z",
    }


@app.get("/api/v1/healthz")
def api_healthz():
    con = get_conn()
    n = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    newest = con.execute("SELECT MAX(txn_date) FROM transactions").fetchone()[0]
    return {
        "status":       "ok",
        "transactions": n,
        "newest_date":  newest,
        "fetched_at":   datetime.utcnow().isoformat() + "Z",
    }
# ============================================================

