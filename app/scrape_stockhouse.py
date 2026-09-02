"""scrape_stockhouse.py — pull insider transactions from Stockhouse for our 367 mining tickers.

Uses Playwright to bootstrap a per-ticker viz_id (their visualization API requires
a session bound to the symbol), then makes plain JSON API calls year-by-year.

Architecture per ticker:
  1. Playwright loads /companies/insiders?symbol=<prefix>.<ticker>
     → captures viz_id from network response
     → captures session cookies
  2. ctx.request.get()s the data API for year=null + each year (2026..2023)
     → paginate while transactions returned
  3. Insert into sedi.db with txn_hash dedup
  4. Update ticker_coverage row

Driver mode: --batch N picks the N tickers most-overdue for a refresh.
Backfill mode: --all walks every ticker (used for first-time bootstrap).

Stockhouse symbol prefixes:
  TSX        AGI.TO   → T.AGI
  TSXV       NFG.V    → V.NFG
  CSE        AUOZ.CN  → C.AUOZ
  Other      <skip>
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
                     years=YEARS, sleep_s: float = 0.6) -> list[dict]:
    """Pull every year + page for one ticker using in-page fetch().
    Reuses the bootstrapped Playwright page (same origin → cookies + headers
    are propagated automatically, which is what gets us past the 403 wall)."""
    all_rows = []
    for year in years:
        for page_num in range(1, MAX_PAGES_PER_YEAR + 1):
            url = _api_url(viz, sh_symbol, year, page_num)
            try:
                body = page.evaluate(
                    """url => fetch(url, {credentials: 'include'}).then(r => r.text())""",
                    url,
                )
            except Exception as e:
                print(f"  [{ticker}] year={year} pg={page_num} fetch err: {e}")
                break
            if not body:
                break
            try:
                data = json.loads(body)
            except Exception:
                if page_num == 1 and year is None:
                    print(f"  [{ticker}] non-JSON body[:80]={body[:80]!r}")
                break
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
    return all_rows


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
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    tickers_data = json.load(open(TICKERS_PATH))
    all_tickers = [t["ticker"] for t in tickers_data if isinstance(t, dict) and t.get("ticker")]
    sh_map = {t: stockhouse_symbol(t) for t in all_tickers}
    eligible = [t for t in all_tickers if sh_map[t]]
    print(f"[stockhouse] tickers in tickers.json: {len(all_tickers)}, "
          f"stockhouse-mappable: {len(eligible)}")

    con = _conn()

    if args.ticker:
        batch = [t for t in args.ticker if sh_map.get(t)]
    elif args.all:
        batch = eligible
    else:
        batch = pick_batch(con, eligible, args.batch)

    print(f"[stockhouse] batch size: {len(batch)}")

    started = datetime.utcnow().isoformat(timespec="seconds")
    n_attempted = n_succeeded = n_inserted = n_skipped = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA)

        # Bootstrap once — get a viz_id we can reuse for all tickers via in-page fetch
        print("[stockhouse] bootstrapping session...")
        page, viz = bootstrap_session(ctx)
        if not viz:
            print("[stockhouse] FAILED to capture viz_id; abort")
            browser.close()
            return 1
        print(f"[stockhouse] session ready, viz_id={viz}")

        for ticker in batch:
            sh = sh_map[ticker]
            n_attempted += 1
            print(f"  [{ticker:10s}] -> {sh}")
            try:
                rows = fetch_for_ticker(page, viz, sh, ticker, sleep_s=args.sleep)
            except Exception as e:
                print(f"    fetch err: {e}")
                continue
            new_rows = 0
            dup_rows = 0
            for raw in rows:
                if upsert_transaction(con, ticker=ticker, raw=raw):
                    new_rows += 1
                else:
                    dup_rows += 1
            update_coverage(con, ticker, n_total_in_db=new_rows + dup_rows)
            con.commit()
            n_succeeded += 1
            n_inserted  += new_rows
            n_skipped   += dup_rows
            print(f"    new={new_rows}  dup={dup_rows}  total_returned={len(rows)}")
        page.close()
        browser.close()

    finished = datetime.utcnow().isoformat(timespec="seconds")
    log_job(con, started=started, finished=finished, status="ok",
            attempted=n_attempted, succeeded=n_succeeded,
            inserted=n_inserted, skipped=n_skipped, note=None)
    con.commit()
    print(f"[stockhouse] DONE  attempted={n_attempted}  succeeded={n_succeeded}  "
          f"inserted={n_inserted}  dup_skipped={n_skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
