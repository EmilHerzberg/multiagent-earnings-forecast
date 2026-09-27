"""Load the VIX volatility index from CBOE.

    python scripts/17_vix.py [--refresh]

Source
------
Cboe Global Markets, the index publisher itself:

    https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv

Daily open/high/low/close from 1990. Free, no key, plain CSV.

Why the publisher rather than a data vendor
-------------------------------------------
Alpha Vantage does not carry index symbols — ``^VIX`` returns nothing and the
only alternatives there are leveraged ETFs (VIXY, UVXY) whose prices decay
against the index through roll costs and are therefore a poor stand-in.
Taking the series from Cboe avoids both the proxy error and a second vendor
dependency.

What it adds beyond realised volatility
---------------------------------------
``event_features`` already carries *realised* volatility — how much the stock
and the market actually moved. VIX is *implied* volatility: what the options
market expects over the coming 30 days. The gap between the two is
informative. Realised volatility says the last month was calm; VIX says whether
the market expects that to continue. For a five-day forecast made at t0, the
forward-looking measure is the more relevant one.

No look-ahead: VIX is a market price, published continuously, so the close on
t0 was known at t0's close.
"""
from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "vix"
SOURCE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
CACHE_CSV = config.THESIS_DIR / "vix_daily.csv"

SCHEMA = """
DROP TABLE IF EXISTS vix;
CREATE TABLE vix (
    date  TEXT PRIMARY KEY,
    open  REAL,
    high  REAL,
    low   REAL,
    close REAL   -- the level to use; VIX has no adjustment concept
);
"""


def download() -> None:
    print(f"  downloading {SOURCE_URL}")
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "thesis-data-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        CACHE_CSV.write_bytes(r.read())
    print(f"  cached -> {CACHE_CSV.name}")


def parse_date(raw: str) -> str | None:
    """Cboe ships MM/DD/YYYY."""
    raw = (raw or "").strip()
    if "/" in raw:
        m, d, y = raw.split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    return config.iso_date(raw)


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--refresh", action="store_true", help="Re-download the source file.")
    args = ap.parse_args()

    print("[17] VIX (Cboe Global Markets)")
    if args.refresh or not CACHE_CSV.exists():
        download()
    else:
        print(f"  using cached {CACHE_CSV.name}; --refresh to update")

    rows = []
    with open(CACHE_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = parse_date(r.get("DATE", ""))
            if d and config.PRICE_HISTORY_FROM <= d <= config.END_DATE:
                rows.append((d, config.to_float(r.get("OPEN")), config.to_float(r.get("HIGH")),
                             config.to_float(r.get("LOW")), config.to_float(r.get("CLOSE"))))

    con = config.connect()
    con.executescript(SCHEMA)
    con.execute("DELETE FROM provenance WHERE table_name = ?", (TABLE,))
    con.executemany("INSERT INTO vix VALUES (?,?,?,?,?)", rows)
    config.record_provenance(
        con, TABLE, "Cboe Global Markets", SOURCE_URL, len(rows),
        "Daily VIX implied-volatility index, taken from the publisher. Complements the "
        "realised volatility in event_features: VIX is forward-looking.")
    con.commit()

    n, d0, d1, lo, avg, hi = con.execute(
        "SELECT COUNT(*), MIN(date), MAX(date), ROUND(MIN(close),1), "
        "ROUND(AVG(close),1), ROUND(MAX(close),1) FROM vix").fetchone()
    print(f"  rows: {n:,} ({d0} .. {d1})")
    w = con.execute(
        "SELECT COUNT(*), ROUND(MIN(close),1), ROUND(AVG(close),1), ROUND(MAX(close),1) "
        "FROM vix WHERE date BETWEEN ? AND ?",
        (config.START_DATE, config.END_DATE)).fetchone()
    print(f"  in study window: {w[0]} days | close min {w[1]} mean {w[2]} max {w[3]}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
