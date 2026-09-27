"""Fetch daily OHLCV price data from EODHD into ``prices_daily``.

    python scripts/05_eodhd_daily_prices.py [--sleep 0.3]

Source
------
EODHD ``GET /api/eod/{SYMBOL}.US?period=d&from=&to=``

Per-symbol endpoint: one call per constituent.  Response fields:
``date, open, high, low, close, adjusted_close, volume``.  A 404 means the
provider has no series for that symbol; it is logged, not fatal.

History is fetched from ``config.PRICE_HISTORY_FROM`` (five years before the
event window), not from the window start. The endpoint charges one call per
symbol whatever range is asked for, so the extra history is free, and it is
what makes 12-month momentum and 252-day volatility defined for events in
January 2025. Without it roughly 70 % of events have no 12-month lookback.

Adjustment convention
---------------------
The provider adjusts only the close.  ``open``/``high``/``low`` arrive on the
raw basis, so a series that mixes them is internally inconsistent across any
split.  This script stores::

    close          the RAW close, exactly as delivered
    adj_close      split- and dividend-adjusted close  <- use this for returns
    open/high/low  scaled onto the adjusted basis by adj_close/close

Booking.com (BKNG) illustrates why this matters: its raw close drops from
~4,194 to ~176 on 2026-04-06 (a split), while the adjusted close runs through
continuously.  A return series computed from ``close`` would record a -96 % day
that never happened.

Quality filter
--------------
A bar is rejected when its adjusted close deviates from the median adjusted
close of the surrounding 21 bars by more than MAX_LEVEL_RATIO (default 3x).

Rationale: no S&P 500 constituent moves 3x in a day, but corrupted or
mis-mapped vendor bars do.  The filter operates on the *adjusted* close, so
genuine splits — continuous in adj_close — are unaffected.  Every rejected bar
is written to ``data_exclusions`` with its measured ratio, so the filtering is
auditable and nothing is dropped silently.

Observed effect on this dataset: 3 rejected bars out of 178,487, all KLAC
(2026-06-08/09/10), where the adjusted close collapses to ~210 and returns to
~2,411 on 06-11 — a vendor error, not a corporate action.  Use ``--no-filter``
to store everything unfiltered; exclusions are still logged for inspection.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "prices_daily"
ENDPOINT = "/api/eod/{SYMBOL}.US"

#: Half-width of the centred window used for the local median.
MEDIAN_HALF_WINDOW = 10
#: Maximum tolerated ratio between a bar's adjusted close and its local median.
MAX_LEVEL_RATIO = 3.0


def fetch(symbol: str, start: str, end: str) -> list[tuple]:
    """Retrieve and normalise the daily bars for one symbol."""
    out = []
    for b in config.eodhd_eod_prices(symbol, start, end):
        date = config.iso_date(b.get("date"))
        if not date:
            continue
        close = config.to_float(b.get("close"))
        adj = config.to_float(b.get("adjusted_close"))
        o, h, l = (config.to_float(b.get(k)) for k in ("open", "high", "low"))
        o, h, l = config.adjust_ohlc(o, h, l, close, adj)
        volume = config.to_float(b.get("volume"))
        out.append((symbol, date, o, h, l, close, adj, int(volume or 0)))
    return sorted(out, key=lambda r: r[1])


def reject_level_outliers(bars: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    """Split *bars* (date-sorted) into (kept, rejected). Index 6 is adj_close."""
    adj = [b[6] for b in bars]
    kept, rejected = [], []
    for i, bar in enumerate(bars):
        value = adj[i]
        if value is None or value <= 0:
            rejected.append((bar, "non_positive_adjusted_close", str(value)))
            continue
        lo, hi = max(0, i - MEDIAN_HALF_WINDOW), min(len(adj), i + MEDIAN_HALF_WINDOW + 1)
        neighbours = [a for j, a in enumerate(adj[lo:hi], start=lo) if j != i and a and a > 0]
        if len(neighbours) < 5:
            kept.append(bar)
            continue
        median = statistics.median(neighbours)
        ratio = value / median
        if ratio > MAX_LEVEL_RATIO or ratio < 1.0 / MAX_LEVEL_RATIO:
            rejected.append((bar, "level_outlier_vs_local_median",
                             f"adj_close={value:.4f} local_median={median:.4f} ratio={ratio:.3f}"))
        else:
            kept.append(bar)
    return kept, rejected


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--no-filter", action="store_true",
                    help="Store every bar; still logs what would have been dropped.")
    ap.add_argument("--sleep", type=float, default=0.0, help="Seconds between calls.")
    ap.add_argument("--resume", action="store_true",
                    help="Skip symbols already present in prices_daily.")
    ap.add_argument("--history-from", default=config.PRICE_HISTORY_FROM,
                    help="Earliest bar to fetch. Defaults to five years before the event "
                         "window so trailing features are defined at the window start; "
                         "costs no extra calls.")
    args = ap.parse_args()
    args.start = args.history_from

    symbols = config.constituents()
    if args.limit:
        symbols = symbols[: args.limit]

    con = config.connect()
    if args.resume:
        done = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM prices_daily")}
        symbols = [s for s in symbols if s not in done]
        print(f"  resume: {len(done)} symbols present, {len(symbols)} remaining")

    print(f"[05] daily prices | {len(symbols)} symbols | {args.start}..{args.end}")

    total_kept = total_dropped = 0
    no_data: list[str] = []
    for i, symbol in enumerate(symbols, 1):
        try:
            bars = fetch(symbol, args.start, args.end)
        except config.NotFound:
            bars = []
        except config.ProviderError as exc:
            print(f"  {symbol}: {exc}")
            config.record_exclusion(con, TABLE, symbol, f"{args.start}..{args.end}",
                                    "provider_error", str(exc)[:200])
            continue

        if not bars:
            no_data.append(symbol)
            config.record_exclusion(con, TABLE, symbol, f"{args.start}..{args.end}",
                                    "no_price_data", "Provider returned no bars for this symbol.")
            continue

        kept, rejected = (bars, []) if args.no_filter else reject_level_outliers(bars)
        for bar, reason, detail in rejected:
            config.record_exclusion(con, TABLE, symbol, bar[1], reason, detail)
        con.executemany(
            """INSERT INTO prices_daily (symbol, date, open, high, low, close, adj_close, volume)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, date) DO UPDATE SET
                   open=excluded.open, high=excluded.high, low=excluded.low,
                   close=excluded.close, adj_close=excluded.adj_close, volume=excluded.volume""",
            kept,
        )
        total_kept += len(kept)
        total_dropped += len(rejected)

        if i % 50 == 0:
            con.commit()
            print(f"  {i}/{len(symbols)} | bars={total_kept}")
        if args.sleep and i < len(symbols):
            time.sleep(args.sleep)

    config.record_provenance(
        con, TABLE, "EODHD", ENDPOINT, total_kept,
        f"Daily OHLCV on the adjusted basis. Bar filter: adj_close/local_median(21) outside "
        f"[1/{MAX_LEVEL_RATIO:g}, {MAX_LEVEL_RATIO:g}] rejected; {total_dropped} bars dropped."
        + (f" No price data for: {', '.join(no_data)}." if no_data else ""),
    )
    con.commit()

    n, s, d0, d1 = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM prices_daily"
    ).fetchone()
    print(f"  rows: {n} | symbols: {s} | range: {d0} .. {d1}")
    print(f"  bars dropped by quality filter: {total_dropped}")
    if no_data:
        print(f"  no price data: {', '.join(no_data)}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
