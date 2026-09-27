"""Fetch macroeconomic series from Alpha Vantage.

    python scripts/13_alphavantage_macro.py

Eight calls total. Stores the raw series in ``macro_series``; script 15 attaches
the values that were public at each event's t0.

Source
------
Alpha Vantage economic-indicator endpoints, one call each:

    FEDERAL_FUNDS_RATE   daily    policy rate
    TREASURY_YIELD       daily    2-year and 10-year constant maturity
    WTI / BRENT          daily    crude oil spot prices
    CPI                  monthly  headline consumer price index
    UNEMPLOYMENT         monthly  unemployment rate
    RETAIL_SALES         monthly  advance retail sales

The two Treasury maturities are stored separately so the **yield curve slope**
(10-year minus 2-year) can be derived; an inverted curve is the standard
recession signal and behaves differently from either yield on its own.

Oil is included because it is an input cost for a large part of the index and a
revenue driver for the energy sector, so the same move in crude has opposite
sign effects across constituents — exactly the kind of cross-sectional context a
market-wide return cannot express.

Publication lag — a leakage vector
----------------------------------
These endpoints return the **observation date**, not the release date. The CPI
observation dated 2026-06-01 refers to June but is not published until roughly
mid-July. Attaching it to an event in early July would show the model a number
that did not exist yet.

Two classes of series need different treatment:

*Market-priced series* (``FEDERAL_FUNDS_RATE``, ``TREASURY_YIELD``) are quoted
continuously. The observation date *is* the day it was known, so no lag applies.

*Statistical releases* (``CPI``, ``UNEMPLOYMENT``, ``RETAIL_SALES``) are
published weeks after the period they describe. Each carries a conservative
``publication_lag_days`` below, and script 15 only uses an observation once
``observation_date + lag < t0``. The lags are deliberately generous: being a few
days late costs nothing, being early is leakage.

This is a mitigation, not a solution — the true release date varies by a few
days per month. A series with genuine release timestamps would be better; the
project's own ``macro_time_series`` carries ``release_date_utc`` but covers only
initial jobless claims. Documented in README 7.14.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "macro_series"
ENDPOINT = "/query?function={INDICATOR}"

#: series_id -> (AV function, params, publication lag in days, description)
#: A lag of 0 means the value is a market price, known on its observation date.
SERIES = {
    "FED_FUNDS": ("FEDERAL_FUNDS_RATE", {"interval": "daily"}, 0,
                  "Effective federal funds rate, daily"),
    "UST_10Y": ("TREASURY_YIELD", {"interval": "daily", "maturity": "10year"}, 0,
                "10-year Treasury constant maturity yield, daily"),
    "UST_2Y": ("TREASURY_YIELD", {"interval": "daily", "maturity": "2year"}, 0,
               "2-year Treasury constant maturity yield, daily"),
    "CPI": ("CPI", {"interval": "monthly"}, 45,
            "Consumer price index, monthly; released mid-month for the prior month"),
    "UNEMPLOYMENT": ("UNEMPLOYMENT", {}, 30,
                     "Unemployment rate, monthly; released early the following month"),
    "RETAIL_SALES": ("RETAIL_SALES", {}, 45,
                     "Advance retail sales, monthly"),
    "WTI": ("WTI", {"interval": "daily"}, 0,
            "West Texas Intermediate crude oil spot price, daily"),
    "BRENT": ("BRENT", {"interval": "daily"}, 0,
              "Brent crude oil spot price, daily"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_series (
    series_id            TEXT NOT NULL,
    observation_date     TEXT NOT NULL,
    value                REAL,
    -- Earliest date this observation can be assumed public:
    -- observation_date + publication_lag_days. Zero lag for market-priced series.
    public_from          TEXT NOT NULL,
    publication_lag_days INTEGER NOT NULL,
    description          TEXT,
    PRIMARY KEY (series_id, observation_date)
);
CREATE INDEX IF NOT EXISTS ix_macro_public ON macro_series(series_id, public_from);
"""


def plus_days(iso: str, days: int) -> str:
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--from-date", default=None,
                    help="Earliest observation to keep. Defaults to the price history start.")
    args = ap.parse_args()
    earliest = args.from_date or config.PRICE_HISTORY_FROM

    con = config.connect()
    con.executescript(SCHEMA)
    con.execute("DELETE FROM provenance WHERE table_name = ?", (TABLE,))

    print(f"[13] macro series | {len(SERIES)} indicators | observations from {earliest}")
    total = 0
    for series_id, (function, params, lag, description) in SERIES.items():
        try:
            data = config.alphavantage_macro(function, **params)
        except config.ProviderError as exc:
            print(f"  {series_id:14s} FAILED: {exc}")
            config.record_exclusion(con, TABLE, None, series_id, "provider_error", str(exc)[:200])
            continue

        rows = []
        for obs in data:
            date = config.iso_date(obs.get("date"))
            value = config.to_float(obs.get("value"))
            if date and value is not None and earliest <= date <= args.end:
                rows.append((series_id, date, value, plus_days(date, lag), lag, description))
        con.executemany(
            "INSERT OR REPLACE INTO macro_series VALUES (?,?,?,?,?,?)", rows)
        total += len(rows)
        latest = max((r[1] for r in rows), default="-")
        print(f"  {series_id:14s} {len(rows):5d} obs, latest {latest}, lag {lag}d")
        time.sleep(1.0)

    config.record_provenance(
        con, TABLE, "Alpha Vantage", ENDPOINT, total,
        "Macro indicators. Market-priced series (fed funds, Treasury yields) carry no "
        "publication lag; statistical releases carry a conservative lag so a value is "
        "only used once public_from < t0. See README 7.14.")
    con.commit()

    print(f"\n  total observations: {total}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
