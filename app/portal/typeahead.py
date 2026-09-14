"""ST_TYPEAHEAD_V1 (2026-09-14) — /api/search, the SediTracker type-ahead.

The SediTracker half of giving all three sites MineTerminalPro's search
behaviour: a dropdown of matches under the box as you type. It answers with
MTP's own /api/search payload, item for item —

    {"results": [{"kind": ..., "label": ..., "sub": ..., "url": ...}, ...]}

— so the browser side is the same file on all three sites. SediTracker's
static directory is a symlink to MNT's, so it already loads MNT's
/static/typeahead.js and /static/typeahead.css; only this endpoint is its own.

Two kinds, not one, on Justin's call (2026-09-14): the box promises "insider
name or ticker", so the dropdown offers both. Companies first, then insiders.

Scope: companies this site actually holds filings for AND that are in the
universe — the same rule the ticker list on /tickers follows, so nothing is
suggested that /tickers would not show. Insiders are NOT universe-filtered, for
the reason app.py already gives: an insider's record is about the person.

Why a module of its own rather than another block at the end of app.py: see the
matching note in MNT's portal/typeahead.py. portal/serve.py wires it on.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from fastapi.responses import JSONResponse

# Two group-by scans of an 80,000-row table, so the index is rebuilt on a timer
# rather than per keystroke. Five minutes against a table that gains rows in a
# slow nightly drip is generous.
_TTL_S = 300

_lock = threading.Lock()
_state: dict[str, Any] = {"companies": [], "insiders": [], "built_at": 0.0}


def _words(s: str) -> list[str]:
    return [w for w in s.lower().replace(",", " ").replace("-", " ").replace(".", " ").split() if w]


def _build() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Imported at call time: portal.app imports nothing from here, and the entry
    # point loads it first. Keeping the arrow one-way means this module can
    # never be what breaks the site's import.
    from portal.app import (
        get_conn,
        _in_universe,
        _display_company_name,
        _ticker_to_name,
        _insider_slug,
        _smart_case,
    )

    con = get_conn()

    companies: list[dict[str, Any]] = []
    for ticker, n in con.execute(
        "SELECT ticker, COUNT(*) FROM transactions GROUP BY ticker"
    ):
        t = (ticker or "").strip().upper()
        if not t or not _in_universe(t):
            continue
        name = _display_company_name(_ticker_to_name(t), t)
        bare = t.split(".")[0]
        companies.append({
            "ticker": t,
            "bare": bare,
            "name": name,
            "n": int(n or 0),
            "_bare_l": bare.lower(),
            "_full_l": t.lower(),
            "_name_l": (name or "").lower(),
            "_words": _words(name or ""),
        })

    insiders: list[dict[str, Any]] = []
    for row in con.execute(
        "SELECT i.name, COUNT(t.txn_id) AS n "
        "FROM insiders i JOIN transactions t ON t.insider_id = i.insider_id "
        "GROUP BY i.insider_id"
    ):
        raw = (row[0] or "").strip()
        if not raw:
            continue
        shown = _smart_case(raw)
        insiders.append({
            "name": shown,
            "slug": _insider_slug(raw),
            "n": int(row[1] or 0),
            "_name_l": raw.lower(),
            "_words": _words(raw),
        })

    return companies, insiders


def _index() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = time.monotonic()
    with _lock:
        cos, ins = _state["companies"], _state["insiders"]
        fresh = (cos or ins) and (now - _state["built_at"]) < _TTL_S
    if fresh:
        return cos, ins

    try:
        built_cos, built_ins = _build()
    except Exception:
        # Stale answers beat empty ones: an out-of-date index still finds every
        # company that existed five minutes ago, a blank one looks broken.
        return cos, ins

    with _lock:
        _state["companies"] = built_cos
        _state["insiders"] = built_ins
        _state["built_at"] = now
    return built_cos, built_ins


def _score_company(row: dict[str, Any], q: str) -> Optional[int]:
    if row["_bare_l"] == q or row["_full_l"] == q:
        return 0
    if row["_bare_l"].startswith(q) or row["_full_l"].startswith(q):
        return 1
    if row["_name_l"].startswith(q):
        return 2
    for w in row["_words"]:
        if w.startswith(q):
            return 3
    if q in row["_bare_l"]:
        return 4
    if q in row["_name_l"]:
        return 5
    return None


def _score_insider(row: dict[str, Any], q: str) -> Optional[int]:
    """Surname first, then any name part, then anywhere.

    SEDI files people as "MASSON, Richard Henry", so matching whole words as
    well as the front of the string is what makes a first name findable at all.
    """
    if row["_name_l"].startswith(q):
        return 0
    for w in row["_words"]:
        if w.startswith(q):
            return 1
    if q in row["_name_l"]:
        return 2
    return None


def search(q: str, limit: int = 14) -> list[dict[str, str]]:
    q = (q or "").strip().lower()
    if not q:
        return []
    companies, insiders = _index()

    co_hits = []
    for row in companies:
        s = _score_company(row, q)
        if s is not None:
            co_hits.append((s, -row["n"], row["name"] or row["bare"], row))
    co_hits.sort(key=lambda h: (h[0], h[1], h[2]))

    in_hits = []
    for row in insiders:
        s = _score_insider(row, q)
        if s is not None:
            in_hits.append((s, -row["n"], row["name"], row))
    in_hits.sort(key=lambda h: (h[0], h[1], h[2]))

    # Companies are what most of this traffic is looking for, but a box that
    # says "insider name" must not spend every slot on tickers. So: companies
    # get first call on the rows, up to six of what is left goes to people.
    #
    # Reserving the people's rows FIRST is what the first cut did, and on a
    # short list it gave people every row — /api/search?q=gold&limit=5 came back
    # with five Goldsteins and no gold companies, because six reserved rows out
    # of five left companies nothing. Companies are guaranteed four rows before
    # anyone is held back.
    co_reserved = min(len(co_hits), max(4, limit - 6))
    in_take = in_hits[: min(6, max(0, limit - co_reserved))]
    co_take = co_hits[: max(0, limit - len(in_take))]

    out: list[dict[str, str]] = []
    for _, _, _, row in co_take:
        out.append({
            "kind": "company",
            "label": row["name"] or row["bare"],
            "sub": row["ticker"],
            "url": "/ticker/" + row["ticker"],
        })
    for _, _, _, row in in_take:
        out.append({
            "kind": "insider",
            "label": row["name"],
            "sub": "{} filing{}".format(row["n"], "" if row["n"] == 1 else "s"),
            "url": "/insider/" + row["slug"],
        })
    return out


def register(app) -> None:
    @app.get("/api/search")
    def api_search(q: str = "", limit: int = 14):
        """Type-ahead for the nav search box. Same payload shape as MTP's."""
        try:
            limit = max(1, min(int(limit or 14), 25))
        except (TypeError, ValueError):
            limit = 14
        results = search(q, limit)
        return JSONResponse(
            {
                "ok": True,
                "q": (q or "").strip(),
                "count": len(results),
                "indexed_companies": len(_state["companies"]),
                "indexed_insiders": len(_state["insiders"]),
                "results": results,
            },
            headers={"Cache-Control": "public, max-age=60"},
        )
