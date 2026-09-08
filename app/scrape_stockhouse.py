"""scrape_stockhouse.py — pull insider transactions from Stockhouse for our mining tickers.

Uses Playwright to bootstrap a viz_id (their visualization API requires
a session), then makes plain JSON API calls year-by-year.

Architecture per run:
  1. Playwright loads /companies/insiders?symbol=t.ivn
     → captures viz_id from network response
     → captures session cookies
  2. ctx.request.get()s the data API per ticker
     → paginate while transactions returned
  3. Insert into sedi.db with txn_hash dedup
  4. Update ticker_coverage row — only for tickers we actually read

Driver mode: --batch N picks the N tickers most-overdue for a refresh.
Backfill mode: --all walks every ticker (used for first-time bootstrap).

Stockhouse symbol prefixes:
  TSX        AGI.TO   → T.AGI
  TSXV       NFG.V    → V.NFG
  CSE        AUOZ.CN  → C.AUOZ
  Other      <skip>

--- G4, 2026-09-08 -----------------------------------------------------------
Stockhouse serves exactly **50 requests per session** and then returns
HTTP 403 'Forbidden' for everything after. A freshly bootstrapped session works
again immediately, with no cooldown and no challenge.

This file previously opened one session per run and then asked for ~250 requests
(50 tickers x 5 years each), so the first ~10 tickers were read and the other
~40 were refused — 82% of every run. Two faults made that invisible:

  * it read only the response BODY, never the status, so a 403 was
    indistinguishable from a ticker with no filings; and
  * it called update_coverage() and counted the ticker as succeeded regardless,
    so a refused ticker was stamped freshly-scraped and rotated to the BACK of
    the queue having never been read.

The fix is to ask for less rather than to work around their limit:
years_for() re-reads history only for tickers we have never successfully read.
2023-2025 do not change, and re-fetching them every four hours was ~80% of our
requests. A REQUEST_BUDGET stops the run cleanly before the 50th request; the
deferred tickers keep their old last_scraped_at, so they are first in line next
run instead of being silently skipped.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

DB_PATH      = "/opt/sedi/app/portal/sedi.db"
TICKERS_PATH = "/opt/sedi/app/tickers.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15")

# Years we'll scan per ticker. Stockhouse exposes recent + last 4 calendar years.
YEARS = [None, 2026, 2025, 2024, 2023]

# Pagination cap (safety): typical ticker has <100 txns per year; cap at 20 pages * 25 = 500.
MAX_PAGES_PER_YEAR = 20

# Stockhouse refuses the 51st request on a session (measured 2026-09-08). Stop
# short of it so a run ends by choice rather than by being cut off mid-ticker.
REQUEST_BUDGET = 45

# Returns the status alongside the body. The old version returned r.text() only,
# which is the reason a 403 looked like an empty result.
FETCH_JS = ("url => fetch(url, {credentials: 'include'})"
            ".then(async r => r.status + '|' + (await r.text()))")


# ---------- ticker symbol mapping ----------

_PREFIX_MAP = {"TO": "T", "V": "V", "CN": "C"}


def stockhouse_symbol(ticker: str) -> str | None:
    """AGI.TO → T.AGI, NFG.V → V.NFG, AUOZ.CN → C.AUOZ. None if unsupported exchange."""
    if "." not in ticker:
        return None
    bare, suffix = ticker.rsplit(".", 1)
    suffix = suffix.upper()
    if suffix not in _PREFIX_MAP:
        return None
    return f"{_PREFIX_MAP[suffix]}.{bare.upper()}"


# ---------- request budget ----------

class Budget:
    """Counts requests against Stockhouse's per-session limit."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)


# ---------- DB helpers ----------

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA busy_timeout = 30000")
    return c


def get_or_create_insider(con: sqlite3.Connection, name: str) -> int:
    name = (name or "").strip()
    if not name:
        return 0
    norm = re.sub(r"[^a-z0-9]+", "", name.lower())[:200]
    row = con.execute("SELECT insider_id FROM insiders WHERE name_norm = ?", (norm,)).fetchone()
    if row:
        return row[0]
    cur = con.execute(
        "INSERT INTO insiders (name, name_norm, insider_type) VALUES (?, ?, ?)",
        (name, norm, "individual" if " " in name else "entity"),
    )
    return cur.lastrowid


def txn_hash(ticker: str, txn_date: str, person: str, txn_type: str, shares, price) -> str:
    h = hashlib.sha256()
    h.update("|".join([
        ticker or "",
        (txn_date or "")[:10],
        person or "",
        txn_type or "",
        f"{shares:.4f}" if shares is not None else "",
        f"{price:.4f}"  if price  is not None else "",
    ]).encode("utf-8"))
    return h.hexdigest()


def upsert_transaction(con: sqlite3.Connection, *, ticker: str, raw: dict) -> bool:
    """Insert one transaction. Returns True if new, False if dup."""
    person = (raw.get("PersonName") or "").strip()
    txn_date = (raw.get("TransactionDate") or "")[:10]   # ISO YYYY-MM-DD
    txn_type = raw.get("TransactionType") or raw.get("AcquisitionType")
    shares   = raw.get("SharesTraded")
    price    = raw.get("TransactionPrice")
    h = txn_hash(ticker, txn_date, person, txn_type, shares, price)

    if con.execute("SELECT 1 FROM transactions WHERE txn_hash = ?", (h,)).fetchone():
        return False

    insider_id = get_or_create_insider(con, person)

    total_value = raw.get("TransactionValue")
    if (total_value in (None, 0)) and shares and price:
        total_value = shares * price

    con.execute(
        "INSERT INTO transactions ("
        "txn_hash, insider_id, ticker, txn_date, filing_date, txn_type, txn_code, "
        "security_type, shares, price, total_value, post_balance, "
        "ownership_type, nature_of_ownership, notes, source"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            h, insider_id, ticker, txn_date,
            None,  # Stockhouse doesn't expose filing_date separately
            txn_type, None,
            None,  # security_type not in API
            shares, price, total_value,
            raw.get("SharesHeld"),
            None, None,
            (raw.get("PersonTitle") or "")[:255],
            "stockhouse",
        ),
    )
    return True


def update_coverage(con: sqlite3.Connection, ticker: str, n_total_in_db: int):
    row = con.execute(
        "SELECT MIN(txn_date), MAX(txn_date), COUNT(*) "
        "FROM transactions WHERE ticker = ?",
        (ticker,)
    ).fetchone()
    first_d, last_d, cnt = row
    con.execute(
        "INSERT INTO ticker_coverage (ticker, first_txn_date, last_txn_date, last_scraped_at, source, n_transactions) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "  first_txn_date  = excluded.first_txn_date, "
        "  last_txn_date   = excluded.last_txn_date, "
        "  last_scraped_at = excluded.last_scraped_at, "
        "  source          = excluded.source, "
        "  n_transactions  = excluded.n_transactions",
        (ticker, first_d, last_d, datetime.utcnow().isoformat(timespec="seconds"),
         "stockhouse", cnt),
    )


def years_for(con: sqlite3.Connection, ticker: str) -> list:
    """Which years to request for this ticker.

    Full history only for a ticker we have never successfully read. For one we
    already hold, the recent window (year=None) is enough: 2023-2025 are closed
    and do not change, and re-requesting them every four hours was roughly 80%
    of all our traffic — the reason a run ran out of session budget after ten
    tickers (G4).
    """
    row = con.execute(
        "SELECT n_transactions FROM ticker_coverage WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row and row[0]:
        return [None]
    return YEARS


def pick_batch(con: sqlite3.Connection, tickers: list[str], batch_size: int) -> list[str]:
    """Pick `batch_size` tickers most-overdue for refresh.
    Never-scraped tickers come first; oldest-stale next."""
    coverage = {r[0]: r[1] for r in con.execute(
        "SELECT ticker, last_scraped_at FROM ticker_coverage"
    )}
    untouched = [t for t in tickers if t not in coverage]
    aged = sorted(
        (t for t in tickers if t in coverage),
        key=lambda t: coverage[t] or "0000",
    )
    return (untouched + aged)[:batch_size]


# ---------- Stockhouse API client ----------

def bootstrap_session(playwright_ctx) -> tuple[object, str | None]:
    """Open one Playwright page on Stockhouse insider view and capture a viz_id.
    Keep the page open so subsequent page.evaluate fetch() calls have proper origin.
    Returns (page, viz_id)."""
    page = playwright_ctx.new_page()
    captured = {"viz_id": None}

    def on_response(resp):
        if "getdata" in resp.url and not captured["viz_id"]:
            m = re.search(r"/getdata/([a-zA-Z0-9]+)", resp.url)
            if m:
                captured["viz_id"] = m.group(1)

    page.on("response", on_response)
    page.goto(
        "https://stockhouse.com/companies/insiders?symbol=t.ivn",
        wait_until="domcontentloaded", timeout=25000,
    )
    page.wait_for_timeout(5000)
    return page, captured["viz_id"]


def _api_url(viz: str, sh_symbol: str, year, page_num: int) -> str:
    cfg = json.dumps({
        "ElementName": "insider",
        "Symbol": sh_symbol,
        "Page": page_num,
        "Year": year,
        "LanguageCode": "en",
    }, separators=(",", ":"))
    return (f"https://charting.stockhouse.com/visualization/getdata/{viz}"
            f"?sid=stockhouse&config={urllib.parse.quote(cfg)}")


def fetch_for_ticker(page, viz: str, sh_symbol: str, ticker: str, *,
                     years, budget: Budget, sleep_s: float = 0.6):
    """Pull the requested years for one ticker using in-page fetch().

    Returns (rows, outcome) where outcome is one of:
      'ok'       — every request answered; rows may legitimately be empty
      'refused'  — the source returned a non-200 or unparseable body
      'budget'   — stopped before spending the session's last request

    Only 'ok' means "we know what this ticker holds". The other two mean we do
    not know, and the caller must not record the ticker as read.
    """
    all_rows = []
    for year in years:
        for page_num in range(1, MAX_PAGES_PER_YEAR + 1):
            if not budget.take():
                return all_rows, "budget"
            url = _api_url(viz, sh_symbol, year, page_num)
            try:
                resp = page.evaluate(FETCH_JS, url)
            except Exception as e:
                print(f"  [{ticker}] year={year} pg={page_num} fetch err: {e}")
                return all_rows, "refused"
            status, _, body = (resp or "").partition("|")
            if status != "200":
                print(f"  [{ticker}] HTTP {status} year={year} pg={page_num} "
                      f"body={body[:40]!r} — REFUSED")
                return all_rows, "refused"
            if not body:
                break
            try:
                data = json.loads(body)
            except Exception:
                print(f"  [{ticker}] unparseable body[:80]={body[:80]!r} — REFUSED")
                return all_rows, "refused"
            it = data.get("InsiderTransaction") or {}
            txns = it.get("Transactions") or []
            if not txns:
                break
            all_rows.extend(txns)
            total = it.get("TotalTransactions") or 0
            per   = it.get("NumberPerPage") or 25
            if page_num * per >= total:
                break
            time.sleep(sleep_s)
        time.sleep(sleep_s)
    return all_rows, "ok"


def log_job(con, *, started, finished, status, attempted, succeeded, inserted, skipped, note):
    con.execute(
        "INSERT INTO scrape_jobs (source, started_at, finished_at, status, "
        "tickers_attempted, tickers_succeeded, rows_inserted, rows_skipped, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("stockhouse", started, finished, status,
         attempted, succeeded, inserted, skipped, note),
    )


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=50,
                    help="how many tickers to scrape this run (default 50)")
    ap.add_argument("--all", action="store_true",
                    help="scrape every ticker, ignoring batch size")
    ap.add_argument("--ticker", action="append",
                    help="scrape just these tickers (repeatable, for testing)")
    ap.add_argument("--sleep", type=float, default=0.6,
                    help="seconds between API calls within a ticker")
    ap.add_argument("--budget", type=int, default=REQUEST_BUDGET,
                    help=f"max requests per session (Stockhouse refuses the 51st; "
                         f"default {REQUEST_BUDGET})")
    ap.add_argument("--full-history", action="store_true",
                    help="request every year for every ticker, not just unread ones")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    tickers_data = json.load(open(TICKERS_PATH))
    all_tickers = [t["ticker"] for t in tickers_data if isinstance(t, dict) and t.get("ticker")]
    sh_map = {t: stockhouse_symbol(t) for t in all_tickers}
    eligible = [t for t in all_tickers if sh_map[t]]
    unmappable = len(all_tickers) - len(eligible)
    print(f"[stockhouse] tickers in tickers.json: {len(all_tickers)}, "
          f"stockhouse-mappable: {len(eligible)}, skipped (exchange): {unmappable}")

    con = _conn()

    if args.ticker:
        batch = [t for t in args.ticker if sh_map.get(t)]
    elif args.all:
        batch = eligible
    else:
        batch = pick_batch(con, eligible, args.batch)

    print(f"[stockhouse] batch size: {len(batch)}  request budget: {args.budget}")

    started = datetime.utcnow().isoformat(timespec="seconds")
    budget = Budget(args.budget)
    n_attempted = n_ok = n_inserted = n_skipped = 0
    n_refused = n_deferred = 0
    refused_tickers: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA)

        print("[stockhouse] bootstrapping session...")
        page, viz = bootstrap_session(ctx)
        if not viz:
            print("[stockhouse] FAILED to capture viz_id; abort")
            browser.close()
            finished = datetime.utcnow().isoformat(timespec="seconds")
            log_job(con, started=started, finished=finished, status="failed",
                    attempted=0, succeeded=0, inserted=0, skipped=0,
                    note="bootstrap failed: no viz_id")
            con.commit()
            return 1
        print(f"[stockhouse] session ready, viz_id={viz}")

        for ticker in batch:
            sh = sh_map[ticker]
            yrs = YEARS if args.full_history else years_for(con, ticker)
            # Only start a ticker we can finish. Spending the last few requests
            # part-way through one would discard them for nothing.
            if budget.left < len(yrs):
                n_deferred += 1
                continue
            n_attempted += 1
            print(f"  [{ticker:10s}] -> {sh}  years={len(yrs)}  budget_left={budget.left}")
            try:
                rows, outcome = fetch_for_ticker(page, viz, sh, ticker,
                                                 years=yrs, budget=budget,
                                                 sleep_s=args.sleep)
            except Exception as e:
                print(f"    fetch err: {e}")
                n_refused += 1
                refused_tickers.append(ticker)
                continue

            if outcome != "ok":
                # Deliberately do NOT call update_coverage(). Stamping
                # last_scraped_at here would rotate a ticker we never read to the
                # back of the queue and make it look freshly checked (G4).
                if outcome == "refused":
                    n_refused += 1
                    refused_tickers.append(ticker)
                else:
                    n_deferred += 1
                    n_attempted -= 1
                print(f"    {outcome.upper()} — coverage left unchanged, will retry next run")
                continue

            new_rows = dup_rows = 0
            for raw in rows:
                if upsert_transaction(con, ticker=ticker, raw=raw):
                    new_rows += 1
                else:
                    dup_rows += 1
            update_coverage(con, ticker, n_total_in_db=new_rows + dup_rows)
            con.commit()
            n_ok += 1
            n_inserted += new_rows
            n_skipped  += dup_rows
            print(f"    new={new_rows}  dup={dup_rows}  total_returned={len(rows)}")

        page.close()
        browser.close()

    finished = datetime.utcnow().isoformat(timespec="seconds")
    status = "ok" if n_refused == 0 else "partial"
    note = (f"read={n_ok} refused={n_refused} deferred_budget={n_deferred} "
            f"requests={budget.used}/{budget.limit}")
    if refused_tickers:
        note += " refused_tickers=" + ",".join(refused_tickers[:20])
    log_job(con, started=started, finished=finished, status=status,
            attempted=n_attempted, succeeded=n_ok,
            inserted=n_inserted, skipped=n_skipped, note=note)
    con.commit()
    print(f"[stockhouse] DONE status={status}  read={n_ok}  refused={n_refused}  "
          f"deferred={n_deferred}  inserted={n_inserted}  dup_skipped={n_skipped}  "
          f"requests={budget.used}/{budget.limit}")
    if n_refused:
        print(f"[stockhouse] WARNING {n_refused} ticker(s) were REFUSED by the source "
              f"and hold no fresh data: {', '.join(refused_tickers[:10])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
