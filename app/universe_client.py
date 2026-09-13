#!/usr/bin/env python3
"""UNIVERSE_CLIENT_V1 — read the shared company universe from MinePortal.

The same file ships in MNT and SediTracker. Both run on the same box as
MinePortal, so this talks to 127.0.0.1:8090 directly; nothing here is reachable
from, or reaches, the internet.

Why a cache at all. MinePortal is now the gate on who exists across three
public sites, which means its being briefly unreachable — a restart, a slow
query — must not empty a site. So:

    live fetch  ->  on-disk cache (served at ANY age)  ->  empty + loud log

The middle step is the important one. A stale universe is a company list a few
hours out of date; an empty universe is a site with no companies on it. The
cache is therefore never allowed to expire in the failure path, only in the
refresh path. Callers that cannot tolerate an empty list should check
`is_stale()` and act on it rather than rendering nothing.

Deliberately stdlib-only (urllib, json) so it imports identically under MNT's
venv, SediTracker's symlinked venv and a bare system python in a cron job.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import urllib.request

log = logging.getLogger("universe")

UNIVERSE_URL = os.environ.get(
    "UNIVERSE_URL", "http://127.0.0.1:8090/api/v1/universe"
)
CACHE_PATH = os.environ.get(
    "UNIVERSE_CACHE", "/var/lib/mnt-portal/universe.json"
)
CANDIDATE_URL = os.environ.get(
    "UNIVERSE_CANDIDATE_URL",
    "http://127.0.0.1:8090/api/v1/universe/candidates",
)
TTL_S = int(os.environ.get("UNIVERSE_TTL_S", 6 * 3600))
FETCH_TIMEOUT_S = 20

_lock = threading.Lock()
_mem: list[dict] | None = None
_mem_at: float = 0.0
_mem_stale: bool = False


# --------------------------------------------------------------------------
# fetch / cache
# --------------------------------------------------------------------------

def _fetch() -> list[dict]:
    with urllib.request.urlopen(UNIVERSE_URL, timeout=FETCH_TIMEOUT_S) as r:
        payload = json.loads(r.read().decode("utf-8"))
    companies = payload.get("companies")
    if not isinstance(companies, list) or not companies:
        # An empty 200 is worse than an error: it would look like a valid
        # answer and quietly wipe the list. Treat it as a failure.
        raise ValueError(f"universe payload had no companies (count={payload.get('count')})")
    return companies


def _write_cache(companies: list[dict]) -> None:
    d = os.path.dirname(CACHE_PATH)
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".universe.", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump({"fetched_at": time.time(), "companies": companies}, fh)
        os.replace(tmp, CACHE_PATH)     # atomic; a reader never sees a half file
    except OSError as e:
        log.warning("universe: could not write cache %s: %s", CACHE_PATH, e)


def _read_cache() -> tuple[list[dict] | None, float]:
    try:
        with open(CACHE_PATH) as fh:
            blob = json.load(fh)
        companies = blob.get("companies")
        if isinstance(companies, list) and companies:
            return companies, float(blob.get("fetched_at") or 0)
    except (OSError, ValueError) as e:
        log.warning("universe: cache unreadable (%s)", e)
    return None, 0.0


def load(force: bool = False) -> list[dict]:
    """The universe. Never raises; worst case returns []."""
    global _mem, _mem_at, _mem_stale
    with _lock:
        if _mem is not None and not force and (time.time() - _mem_at) < TTL_S:
            return _mem

        cached, cached_at = _read_cache()
        if cached is not None and not force and (time.time() - cached_at) < TTL_S:
            _mem, _mem_at, _mem_stale = cached, cached_at, False
            return _mem

        try:
            companies = _fetch()
            _write_cache(companies)
            _mem, _mem_at, _mem_stale = companies, time.time(), False
            log.info("universe: loaded %d companies from MinePortal", len(companies))
            return _mem
        except Exception as e:
            if cached is not None:
                age_h = (time.time() - cached_at) / 3600
                log.error(
                    "universe: MinePortal unreachable (%s) — serving cache "
                    "%.1f h old, %d companies", e, age_h, len(cached)
                )
                _mem, _mem_at, _mem_stale = cached, cached_at, True
                return _mem
            log.error(
                "universe: MinePortal unreachable (%s) and NO cache at %s — "
                "returning an empty universe", e, CACHE_PATH
            )
            _mem, _mem_at, _mem_stale = [], time.time(), True
            return _mem


def is_stale() -> bool:
    """True when the last load fell back to cache, or found nothing at all."""
    return _mem_stale


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------

def _bare(sym: str) -> str:
    return (sym or "").split(".")[0].upper()


def by_bare() -> dict[str, dict]:
    return {_bare(c["ticker"]): c for c in load() if c.get("ticker")}


def by_symbol() -> dict[str, dict]:
    return {(c.get("symbol") or "").upper(): c for c in load() if c.get("symbol")}


def symbols() -> list[str]:
    """Exchange-suffixed symbols — what the scrapers and SEDI look up."""
    return [c["symbol"] for c in load() if c.get("symbol")]


def name_for(ticker: str) -> str | None:
    """Company name for a bare or suffixed ticker. None when not in the
    universe — callers render that as an em-dash, they do not invent one."""
    if not ticker:
        return None
    hit = by_symbol().get(ticker.upper()) or by_bare().get(_bare(ticker))
    return (hit or {}).get("name") or None


def scrape_configs() -> list[dict]:
    """The universe in the shape MNT's pipeline wants: one config per company
    that has a news source. A company with no source is still a company — it
    simply has nowhere to collect from yet, and is skipped here rather than
    being dropped from the universe."""
    out = []
    for c in load():
        if not c.get("news_source") or not c.get("symbol"):
            continue
        out.append({
            "ticker": c["symbol"],
            "company_id": c.get("ticker"),
            "name": c.get("name"),
            "exchange": c.get("exchange"),
            "source": c["news_source"],
            "source_params": c.get("news_source_params") or {},
        })
    return out


# --------------------------------------------------------------------------
# propose
# --------------------------------------------------------------------------

def propose(ticker: str, name: str | None = None, exchange: str | None = None,
            source: str | None = None, source_params: dict | None = None,
            discovered_by: str = "unknown", evidence_url: str | None = None) -> str:
    """Report a ticker a collector has seen that is not in the universe.

    This replaces appending to tickers.json. A collector can no longer publish
    a company; it can only say it saw one. The name it passes is whatever it
    scraped from a headline, which is exactly why a person reviews it before it
    reaches a public site.
    """
    body = json.dumps({
        "ticker": ticker, "symbol": ticker, "name": name,
        "exchange": exchange, "news_source": source,
        "news_source_params": source_params,
        "discovered_by": discovered_by, "evidence_url": evidence_url,
    }).encode("utf-8")
    req = urllib.request.Request(
        CANDIDATE_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("status", "?")
    except Exception as e:
        log.warning("universe: could not propose %s: %s", ticker, e)
        return "error"
