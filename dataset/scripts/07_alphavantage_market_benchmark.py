"""Fetch the market benchmark (SPY) from Alpha Vantage.

    python scripts/07_alphavantage_market_benchmark.py

Source
------
Alpha Vantage ``GET /query?function=TIME_SERIES_DAILY_ADJUSTED&symbol=SPY``

One call. Returns ``open, high, low, close, adjusted close, volume,
dividend amount, split coefficient`` per trading day, back to 1999.

Why SPY and why this provider
-----------------------------
The abnormal return is defined as the stock's return minus the market's return
over the same window, so a market series is required.  SPY is the standard
S&P 500 tracker and the same benchmark the factor literature uses.

The EODHD price universe covers **common stock only** (NYSE/NASDAQ/AMEX), so it
contains no index or ETF series — SPY, IVV, VOO and QQQ are all absent. Alpha
Vantage supplies it in a single call.

Benchmark and stock returns must be computed on the same basis.  ``adj_close``
is used on both sides, so both are total returns (price plus reinvested
dividends).  Mixing an adjusted stock return with a price-only index return
would introduce a systematic dividend-sized bias.

Note on the stock side
----------------------
Only the benchmark is taken from Alpha Vantage. Stock prices stay with EODHD
because Alpha Vantage's coverage of delisted symbols is unreliable — verified
on this universe: BK returns 5 days instead of its full history, and PARA
returns data through 2026-08 although the company merged in 2025-08 (a recycled
ticker carrying a different company's prices). Using it for the stock side
would silently reintroduce survivorship bias and ticker-collision errors.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "market_benchmark"
ENDPOINT = "/query?function=TIME_SERIES_DAILY_ADJUSTED"
SYMBOL = "SPY"


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--symbol", default=SYMBOL, help="Benchmark ticker (default SPY).")
    ap.add_argument("--history-from", default=config.PRICE_HISTORY_FROM,
                    help="Earliest bar to keep. Matches the stock-side history so "
                         "regime features are defined at the window start.")
    args = ap.parse_args()
    pad_start = args.history_from

    print(f"[07] market benchmark {args.symbol} | {args.start}..{args.end}")
    series = config.alphavantage_daily_adjusted(args.symbol)
    print(f"  provider returned {len(series)} trading days")

    rows = []
    for date, bar in series.items():
        date = config.iso_date(date)
        if not (pad_start <= date <= args.end):
            continue
        close = config.to_float(bar.get("4. close"))
        adj = config.to_float(bar.get("5. adjusted close"))
        o, h, l = (config.to_float(bar.get(k)) for k in ("1. open", "2. high", "3. low"))
        o, h, l = config.adjust_ohlc(o, h, l, close, adj)
        rows.append((args.symbol, date, o, h, l, close, adj,
                     int(config.to_float(bar.get("6. volume")) or 0),
                     config.to_float(bar.get("7. dividend amount")),
                     config.to_float(bar.get("8. split coefficient"))))
    rows.sort(key=lambda r: r[1])

    con = config.connect()
    con.executemany(
        """INSERT INTO market_benchmark
               (symbol, date, open, high, low, close, adj_close, volume,
                dividend_amount, split_coefficient)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol, date) DO UPDATE SET
               open=excluded.open, high=excluded.high, low=excluded.low,
               close=excluded.close, adj_close=excluded.adj_close,
               volume=excluded.volume, dividend_amount=excluded.dividend_amount,
               split_coefficient=excluded.split_coefficient""",
        rows,
    )
    config.record_provenance(
        con, TABLE, "Alpha Vantage", f"{ENDPOINT}&symbol={args.symbol}", len(rows),
        f"Market benchmark for abnormal-return computation. Total-return basis "
        f"(adj_close), matching the stock side. History from {pad_start} so trailing "
        f"regime features are defined for events at the window start.",
    )
    con.commit()

    n, d0, d1 = con.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM market_benchmark WHERE symbol=?",
        (args.symbol,)).fetchone()
    in_window = con.execute(
        "SELECT COUNT(*) FROM market_benchmark WHERE symbol=? AND date BETWEEN ? AND ?",
        (args.symbol, args.start, args.end)).fetchone()[0]
    print(f"  rows: {n} ({d0} .. {d1}) | inside study window: {in_window}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
