#!/usr/bin/env python3
"""UNIVERSE_CLIENT_V2 — read the shared company universe from MinePortal.

The same file ships in MNT and SediTracker. Both run on the same box as
MinePortal, so this talks to 127.0.0.1:8090 directly; nothing here is reachable
from, or reaches, the internet.

Why a cache at all. MinePortal is the gate on who exists across three public
sites, which means its being briefly unreachable — a restart, a slow query —
must not empty a site. So:

    live fetch  ->  on-disk cache (served at ANY age)  ->  empty + loud log

The middle step is the important one. A stale universe is a company list a few
hours out of date; an empty universe is a site with no companies on it. The
cache is therefore never allowed to expire in the failure path, only in the
refresh path. Callers that cannot tolerate an empty list should check
`is_stale()` and act on it rather than rendering nothing.

--- V2, 2026-09-13 (same day) -------------------------------------------------
Two faults in V1, both found by Justin asking a fair question: "if I update
MineProperty's list, will all three websites update?"

1. **A six-hour hold on a long-running process.** V1 kept the universe in memory
   for TTL_S and never looked again, so `universe_sync.py` could refresh the
   on-disk cache every 30 minutes and the web processes would not notice for up
   to six hours. The TTL was inherited from MNT's old six-hourly MinePortal
   fetch, which was reasonable when the company list changed by hand-import a
   few times a month and is not now. V2 checks the cache file's **mtime** (a
   stat, at most every couple of seconds) and adopts a newer file immediately.
   TTL_S survives only as the backstop for the case where nothing is refreshing
   the file at all.

2. **The derived indexes were rebuilt on every lookup.** `name_for()` called
   `by_symbol()` and `by_bare()`, each of which built a 1,000-entry dict from
   scratch — per row. On SediTracker's firehose that is four dict builds a row
   across thousands of rows. This is PERF_A14's bug reintroduced in a new place:
   the *parse* was cached, the *index* was not. The indexes are now built once,
   when the data changes, and lookups are a dict get.

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
# Backstop only. The mtime check below is what actually keeps a running process
# current; this covers "nothing is writing the cache file at all".
TTL_S = int(os.environ.get("UNIVERSE_TTL_S", 6 * 3600))
# How often we are willing to stat the cache file. A page can call load() once
# per row, and a stat per row would be a syscall per row for nothing.
STAT_INTERVAL_S = float(os.environ.get("UNIVERSE_STAT_INTERVAL_S", 2.0))
FETCH_TIMEOUT_S = 20

# RLock, not Lock: by_symbol()/by_bare() take it and call load(), which takes it
# again.
_lock = threading.RLock()
_mem: list[dict] | None = None
_mem_at: float = 0.0
_mem_mtime: float = -1.0
_mem_stale: bool = False
_last_stat_at: float = 0.0

# Derived once per data change, never per lookup.
_idx_symbol: dict = {}
_idx_bare: dict = {}
_symbols: list = []


def _bare(sym: str) -> str:
    return (sym or "").split(".")[0].upper()


def _adopt(companies: list[dict], loaded_at: float, stale: bool,
           mtime: float) -> None:
    """Install a new universe and rebuild the indexes. Caller holds the lock."""
    global _mem, _mem_at, _mem_stale, _mem_mtime
    global _idx_symbol, _idx_bare, _symbols
    _mem = companies
    _mem_at = loaded_at
    _mem_stale = stale
    _mem_mtime = mtime
    idx_symbol, idx_bare, syms = {}, {}, []
    for c in companies:
        sym = (c.get("symbol") or "").upper()
        tkr = (c.get("ticker") or "").upper()
        if sym:
            idx_symbol[sym] = c
            syms.append(c["symbol"])
        if tkr:
            idx_bare.setdefault(_bare(tkr), c)
    _idx_symbol, _idx_bare, _symbols = idx_symbol, idx_bare, syms


def _cache_mtime() -> float:
    try:
        return os.path.getmtime(CACHE_PATH)
    except OSError:
        return -1.0


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


def _write_cache(companies: list[dict]) -> float:
    """Returns the mtime written, or -1."""
    d = os.path.dirname(CACHE_PATH)
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".universe.", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump({"fetched_at": time.time(), "companies": companies}, fh)
        os.replace(tmp, CACHE_PATH)     # atomic; a reader never sees a half file
        return _cache_mtime()
    except OSError as e:
        log.warning("universe: could not write cache %s: %s", CACHE_PATH, e)
        return -1.0


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
    global _last_stat_at
    with _lock:
        now = time.time()

        if _mem is not None and not force:
            # 1. Has somebody written a newer cache? This is the path that
            #    matters: universe_sync.py rewrites the file every 30 minutes
            #    and a long-running web process must notice.
            if (now - _last_stat_at) >= STAT_INTERVAL_S:
                _last_stat_at = now
                m = _cache_mtime()
                if m > _mem_mtime:
                    cached, cached_at = _read_cache()
                    if cached is not None:
                        _adopt(cached, cached_at, False, m)
                        log.info("universe: picked up a newer cache (%d companies)",
                                 len(cached))
                        return _mem
            # 2. Otherwise serve what we have until the backstop expires.
            if (now - _mem_at) < TTL_S:
                return _mem

        cached, cached_at = _read_cache()
        if cached is not None and not force and (time.time() - cached_at) < TTL_S:
            _adopt(cached, cached_at, False, _cache_mtime())
            return _mem

        try:
            companies = _fetch()
            mtime = _write_cache(companies)
            _adopt(companies, time.time(), False, mtime)
            log.info("universe: loaded %d companies from MinePortal", len(companies))
            return _mem
        except Exception as e:
            if cached is not None:
                age_h = (time.time() - cached_at) / 3600
                log.error(
                    "universe: MinePortal unreachable (%s) — serving cache "
                    "%.1f h old, %d companies", e, age_h, len(cached)
                )
                _adopt(cached, cached_at, True, _cache_mtime())
                return _mem
            log.error(
                "universe: MinePortal unreachable (%s) and NO cache at %s — "
                "returning an empty universe", e, CACHE_PATH
            )
            _adopt([], time.time(), True, -1.0)
            return _mem


def is_stale() -> bool:
    """True when the last load fell back to cache, or found nothing at all."""
    return _mem_stale


def loaded_at() -> float:
    """Unix time of the data currently held. For health endpoints."""
    return _mem_at


# --------------------------------------------------------------------------
# views — all O(1) after load()
# --------------------------------------------------------------------------

def by_bare() -> dict[str, dict]:
    with _lock:
        load()
        return _idx_bare


def by_symbol() -> dict[str, dict]:
    with _lock:
        load()
        return _idx_symbol


def symbols() -> list[str]:
    """Exchange-suffixed symbols — what the scrapers and SEDI look up."""
    with _lock:
        load()
        return list(_symbols)


def name_for(ticker: str) -> str | None:
    """Company name for a bare or suffixed ticker. None when not in the
    universe — callers render that as the ticker, they do not invent a name."""
    if not ticker:
        return None
    with _lock:
        load()
        hit = _idx_symbol.get(ticker.upper()) or _idx_bare.get(_bare(ticker))
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
