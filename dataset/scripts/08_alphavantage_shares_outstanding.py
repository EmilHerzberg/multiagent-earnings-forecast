"""Fetch quarterly shares outstanding from Alpha Vantage (market-cap basis).

    python scripts/08_alphavantage_shares_outstanding.py [--resume]

Source
------
Alpha Vantage ``GET /query?function=BALANCE_SHEET&symbol={SYMBOL}``,
field ``commonStockSharesOutstanding`` from ``quarterlyReports``.

One call per constituent (501 calls), ~81 quarters each.

Why not the OVERVIEW endpoint
-----------------------------
``OVERVIEW`` returns ``MarketCapitalization`` and ``SharesOutstanding`` directly,
but only as a **current snapshot**. Attaching today's share count to a 2025
event is a look-ahead error, and it gives no quarterly series to stratify on.
``BALANCE_SHEET`` gives the historical quarterly path instead.

Market capitalisation
---------------------
Cap is derived at event-build time (script 09), not stored here, because it
depends on which trading day is being valued::

    market_cap(t) = adj_close(t) x shares(most recent quarter known at t)

``adj_close`` is used rather than the raw close because the share counts are
expressed on the **current split basis** — retroactively adjusted, like
``adj_close``. Pairing the raw close with a current-basis share count would
double-count every split. Cross-validated against an independent source:
AAPL 2026-03-31 gives 14,768,115,000 shares from both, exactly.

Residual imprecision: ``adj_close`` also carries dividend reinvestment, which
the share count does not, so historical caps are slightly understated relative
to a split-only adjustment. The effect is well below the width of a size
bucket and does not affect stratification.

Availability
------------
Alpha Vantage drops **all** fundamentals for delisted companies — verified on
this universe: the twelve constituents that left during the window return an
empty payload from INCOME_STATEMENT, EARNINGS *and* BALANCE_SHEET alike. Their
share counts are therefore missing here. See README section 7.9.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "shares_outstanding"
ENDPOINT = "/query?function=BALANCE_SHEET"


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--rpm", type=int, default=70, help="Requests per minute.")
    ap.add_argument("--resume", action="store_true",
                    help="Skip symbols already present in shares_outstanding.")
    ap.add_argument("--history-from", default=config.PRICE_HISTORY_FROM,
                    help="Earliest fiscal period to keep. Aligned with the price history "
                         "so a lagged share count is always available; costs no extra calls.")
    args = ap.parse_args()

    symbols = config.constituents()
    if args.limit:
        symbols = symbols[: args.limit]

    con = config.connect()
    if args.resume:
        done = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM shares_outstanding")}
        symbols = [s for s in symbols if s not in done]
        print(f"  resume: {len(done)} symbols present, {len(symbols)} remaining")

    delay = 60.0 / max(args.rpm, 1)
    print(f"[08] shares outstanding | {len(symbols)} symbols | from {args.history_from}")

    written = failed = 0
    no_data: list[str] = []
    for i, symbol in enumerate(symbols, 1):
        try:
            payload = config.alphavantage_balance_sheet(symbol)
        except config.ProviderError as exc:
            message = str(exc)
            if "rate limit" in message.lower() or "premium" in message.lower():
                con.commit()
                print(f"\n  ABORT at {symbol} ({i}/{len(symbols)}): {message}")
                print("  Partial data committed. Re-run with --resume once quota allows.")
                break
            print(f"  {symbol}: {message}")
            failed += 1
            config.record_exclusion(con, TABLE, symbol, "", "provider_error", message[:200])
            continue

        rows = []
        for r in payload.get("quarterlyReports") or []:
            fiscal = config.iso_date(r.get("fiscalDateEnding"))
            shares = config.to_float(r.get("commonStockSharesOutstanding"))
            if fiscal and shares and fiscal >= args.history_from and fiscal <= args.end:
                rows.append((symbol, fiscal, shares, r.get("reportedCurrency")))

        if not rows:
            no_data.append(symbol)
            config.record_exclusion(
                con, TABLE, symbol, f"{args.history_from}..{args.end}", "no_shares_data",
                "Empty payload or no share count in range (typical for delisted symbols).")
        else:
            con.executemany(
                """INSERT INTO shares_outstanding (symbol, fiscal_date, shares, reported_currency)
                   VALUES (?,?,?,?)
                   ON CONFLICT(symbol, fiscal_date) DO UPDATE SET
                       shares=excluded.shares,
                       reported_currency=excluded.reported_currency""",
                rows)
            written += len(rows)

        if i % 25 == 0:
            con.commit()
            print(f"  {i}/{len(symbols)} | rows={written} | failed={failed}")
        if i < len(symbols):
            time.sleep(delay)

    config.record_provenance(
        con, TABLE, "Alpha Vantage", f"{ENDPOINT} (commonStockSharesOutstanding)", written,
        "Quarterly share counts on the current split basis; market cap is derived in "
        "script 09 as adj_close x lagged shares. Delisted symbols return an empty payload.",
    )
    con.commit()

    n, s = con.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM shares_outstanding").fetchone()
    print(f"\n  rows: {n} | symbols: {s} | failed: {failed}")
    if no_data:
        print(f"  no share data ({len(no_data)}): {', '.join(no_data)}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
