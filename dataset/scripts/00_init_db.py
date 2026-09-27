"""Create the thesis database schema.

    python scripts/00_init_db.py [--reset]

Run first. Scripts 01-06 assume these tables exist. No API calls.

Tables
------
companies          one row per constituent (master data, script 01)
earnings           one row per company-quarter (scripts 02-04)
prices_daily       one row per company-day (script 05)
income_statement   one row per company-quarter (script 06)
provenance         which endpoint populated which table
data_exclusions    every record deliberately dropped, with the reason
study_meta         window, universe size, conventions
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    symbol          TEXT PRIMARY KEY,
    name            TEXT,
    sector          TEXT,
    industry        TEXT,
    country_iso     TEXT,
    fiscal_year_end TEXT,
    isin            TEXT,
    cik             TEXT,
    ipo_date        TEXT,
    data_source     TEXT   -- which provider supplied the profile (script 01)
);

CREATE TABLE IF NOT EXISTS earnings (
    symbol              TEXT NOT NULL,
    fiscal_date         TEXT NOT NULL,   -- end of the reported fiscal quarter
    report_date         TEXT,            -- announcement date (event-study anchor)
    eps_actual          REAL,
    eps_estimate        REAL,
    eps_surprise_pct    REAL,
    before_after_market TEXT,            -- BeforeMarket / AfterMarket / DuringMarket
    PRIMARY KEY (symbol, fiscal_date),
    FOREIGN KEY (symbol) REFERENCES companies(symbol)
);
CREATE INDEX IF NOT EXISTS ix_earnings_report_date ON earnings(report_date);

CREATE TABLE IF NOT EXISTS prices_daily (
    symbol    TEXT NOT NULL,
    date      TEXT NOT NULL,
    open      REAL,   -- scaled onto the adjusted basis (adj_close/close)
    high      REAL,   -- adjusted
    low       REAL,   -- adjusted
    close     REAL,   -- RAW close, as delivered by the provider
    adj_close REAL,   -- split + dividend adjusted -> use this for returns
    volume    INTEGER,
    PRIMARY KEY (symbol, date),
    FOREIGN KEY (symbol) REFERENCES companies(symbol)
);
CREATE INDEX IF NOT EXISTS ix_prices_date ON prices_daily(date);

CREATE TABLE IF NOT EXISTS income_statement (
    symbol            TEXT NOT NULL,
    fiscal_date       TEXT NOT NULL,
    period            TEXT NOT NULL DEFAULT 'quarterly',
    reported_currency TEXT,
    revenue           REAL,
    net_income        REAL,
    operating_income  REAL,
    ebit              REAL,
    ebitda            REAL,
    gross_profit      REAL,
    PRIMARY KEY (symbol, fiscal_date, period),
    FOREIGN KEY (symbol) REFERENCES companies(symbol)
);

CREATE TABLE IF NOT EXISTS market_benchmark (
    symbol            TEXT NOT NULL,   -- SPY
    date              TEXT NOT NULL,
    open              REAL,
    high              REAL,
    low               REAL,
    close             REAL,            -- RAW close
    adj_close         REAL,            -- total-return basis; use for market return
    volume            INTEGER,
    dividend_amount   REAL,
    split_coefficient REAL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS shares_outstanding (
    symbol            TEXT NOT NULL,
    fiscal_date       TEXT NOT NULL,   -- quarter the count refers to
    shares            REAL,            -- current split basis (see script 08)
    reported_currency TEXT,
    PRIMARY KEY (symbol, fiscal_date),
    FOREIGN KEY (symbol) REFERENCES companies(symbol)
);

-- Point-in-time S&P 500 membership. Derived by script 09 from
-- sp500_index_changes.csv, with removal dates refined from the price series.
CREATE TABLE IF NOT EXISTS index_membership (
    symbol        TEXT PRIMARY KEY,
    in_index_from TEXT,          -- NULL = member before the window started
    in_index_to   TEXT,          -- NULL = still a member at the window end
    from_source   TEXT,          -- verified / aggregator / assumed_at_window_start
    to_source     TEXT,          -- verified / aggregator / price_series / NULL
    note          TEXT,
    FOREIGN KEY (symbol) REFERENCES companies(symbol)
);

-- One row per forecastable event. Built by script 09 from the tables above.
CREATE TABLE IF NOT EXISTS events (
    symbol            TEXT NOT NULL,
    fiscal_date       TEXT NOT NULL,
    report_date       TEXT NOT NULL,
    before_after_market TEXT NOT NULL,
    -- t0 = first trading day that prices the announcement.
    -- BeforeMarket/DuringMarket -> report_date; AfterMarket -> next trading day.
    -- The forecast is made after t0 closes; t0 itself is NOT in the window.
    t0                TEXT NOT NULL,
    window_start      TEXT NOT NULL,   -- t0+1 (first trading day of the window)
    window_end        TEXT NOT NULL,   -- t0+horizon
    horizon_days      INTEGER NOT NULL,
    stock_return      REAL,            -- total return over the window
    market_return     REAL,            -- SPY total return over the same days
    abnormal_return   REAL,            -- stock - market
    label             INTEGER,         -- 1 if abnormal_return > 0 else 0
    -- Values as known at t0, safe to show the model:
    close_at_t0       REAL,            -- RAW close (see README 6.1 / 7.10)
    market_cap_at_t0  REAL,            -- adj_close(t0) x lagged share count
    shares_fiscal_date TEXT,           -- which quarter the share count came from
    eps_actual        REAL,
    eps_estimate      REAL,
    eps_surprise_pct  REAL,
    -- Prompt-construction guards:
    split_after_t0    INTEGER NOT NULL DEFAULT 0,  -- a split occurs later: must stay hidden
    split_in_lookback INTEGER NOT NULL DEFAULT 0,  -- raw series jumps before t0
    has_fundamentals  INTEGER NOT NULL DEFAULT 0,  -- income_statement row exists
    -- 1 when the company was an S&P 500 member on t0. The point-in-time sample
    -- is the subset with in_index_at_t0 = 1; see README 7.1.
    in_index_at_t0    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (symbol, fiscal_date),
    FOREIGN KEY (symbol) REFERENCES companies(symbol)
);
CREATE INDEX IF NOT EXISTS ix_events_t0 ON events(t0);

CREATE TABLE IF NOT EXISTS provenance (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name   TEXT NOT NULL,
    source_api   TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    rows_written INTEGER,
    run_utc      TEXT NOT NULL,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS data_exclusions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    symbol     TEXT,
    ref        TEXT,      -- the excluded key (date or fiscal_date)
    reason     TEXT NOT NULL,
    detail     TEXT,
    run_utc    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

TABLES = ["companies", "earnings", "prices_daily", "income_statement",
          "market_benchmark", "shares_outstanding", "index_membership", "events",
          "provenance", "data_exclusions", "study_meta"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="Drop existing tables first.")
    args = ap.parse_args()

    con = config.connect()
    if args.reset:
        for t in reversed(TABLES):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        print("dropped existing tables")

    con.executescript(SCHEMA)

    symbols = config.constituents()
    con.executemany("INSERT OR IGNORE INTO companies (symbol) VALUES (?)",
                    [(s,) for s in symbols])

    for k, v in {
        "study_window_start": config.START_DATE,
        "study_window_end": config.END_DATE,
        "n_constituents": str(len(symbols)),
        "constituent_basis": (
            "Union of every company that was an S&P 500 member at any point in the "
            "window, including index additions and removals. Point-in-time membership "
            "is reconstructed in index_membership from sp500_index_changes.csv; "
            "events.in_index_at_t0 marks whether the company was a member on the "
            "reaction day. See README 7.1."
        ),
        "price_basis": (
            "adj_close is split- and dividend-adjusted and is the correct series for "
            "return calculations. close is the raw close. open/high/low are scaled "
            "onto the adjusted basis by adj_close/close."
        ),
        "eps_basis": (
            "eps_actual and eps_estimate are the consensus-comparable (street) EPS "
            "figures, generally diluted and adjusted. They are NOT GAAP basic/diluted "
            "EPS and cannot be reconciled with net_income / share count."
        ),
        "initialised_utc": config.utcnow(),
    }.items():
        con.execute("INSERT OR REPLACE INTO study_meta (key, value) VALUES (?,?)", (k, v))

    con.commit()
    print(f"schema created at {config.DB_PATH}")
    print(f"companies seeded: {con.execute('SELECT COUNT(*) FROM companies').fetchone()[0]}")
    print(f"study window    : {config.START_DATE} .. {config.END_DATE}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
