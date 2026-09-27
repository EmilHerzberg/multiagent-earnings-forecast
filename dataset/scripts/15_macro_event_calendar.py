"""Flag scheduled macro releases inside each event's forecast window. No API calls.

    python scripts/15_macro_event_calendar.py

Run after script 09.

Why this exists
---------------
The outcome being forecast is a five-trading-day abnormal return. A CPI print or
an FOMC decision landing inside that window moves the whole market, and while
the abnormal return already nets out the market move, a macro shock still
changes cross-sectional dispersion, factor rotation and the amount of attention
left over for a single company's earnings. Knowing that a Fed decision sits two
days after t0 is genuine context for a forecaster.

It also matters for interpretation: if the topologies differ mainly on events
with a macro release in the window, that is a finding about macro-conditioned
reasoning rather than about earnings drift.

Source
------
``macro_event_calendar.csv``. Four release types that reliably move US equities:

    FOMC_DECISION   Federal Open Market Committee rate decision
    CPI             Consumer Price Index
    NFP             Employment Situation (non-farm payrolls)
    PCE             Personal Income and Outlays (the Fed's preferred inflation gauge)

2025 rows come from the project's economic-calendar ingest. That ingest stopped
at the end of 2025 — for 2026 it holds only IPO debuts and JOLTS — so 2026 rows
are taken from the publishing agencies' own advance schedules. The Fed, BLS and
BEA all publish release dates months ahead, which makes the gap fillable from
primary sources rather than a limitation to be documented.

No look-ahead: release dates are announced in advance and were public long
before t0. What is *in* the release is not used, only that it is scheduled.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "macro_event_calendar"
CALENDAR_CSV = config.THESIS_DIR / "macro_event_calendar.csv"

SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_event_calendar (
    date        TEXT NOT NULL,
    event_type  TEXT NOT NULL,   -- FOMC_DECISION / CPI / NFP / PCE
    description TEXT,
    source      TEXT,
    confidence  TEXT,            -- ingested / official_schedule
    PRIMARY KEY (date, event_type)
);

-- One row per (event, macro release falling inside its forecast window).
CREATE TABLE IF NOT EXISTS event_macro_overlap (
    symbol        TEXT NOT NULL,
    fiscal_date   TEXT NOT NULL,
    macro_date    TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    days_after_t0 INTEGER,
    PRIMARY KEY (symbol, fiscal_date, macro_date, event_type)
);
CREATE INDEX IF NOT EXISTS ix_overlap_event ON event_macro_overlap(symbol, fiscal_date);
"""


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    args = ap.parse_args()

    if not CALENDAR_CSV.exists():
        raise SystemExit(f"missing {CALENDAR_CSV}")

    rows = list(csv.DictReader(
        line for line in CALENDAR_CSV.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")))

    con = config.connect()
    con.executescript(SCHEMA)
    con.execute("DELETE FROM macro_event_calendar")
    con.execute("DELETE FROM event_macro_overlap")
    con.execute("DELETE FROM provenance WHERE table_name = ?", (TABLE,))
    con.executemany(
        "INSERT INTO macro_event_calendar VALUES (?,?,?,?,?)",
        [(r["date"], r["event_type"], r["description"], r["source"], r["confidence"])
         for r in rows])

    # Overlap: a release inside (window_start .. window_end), inclusive.
    con.execute("""
        INSERT INTO event_macro_overlap
        SELECT e.symbol, e.fiscal_date, m.date, m.event_type,
               CAST(JULIANDAY(m.date) - JULIANDAY(e.t0) AS INTEGER)
          FROM events e
          JOIN macro_event_calendar m
            ON m.date BETWEEN e.window_start AND e.window_end
    """)

    config.record_provenance(
        con, TABLE, "Federal Reserve / BLS / BEA advance schedules + project calendar ingest",
        CALENDAR_CSV.name, len(rows),
        "Scheduled FOMC, CPI, NFP and PCE release dates. 2025 from the project ingest, "
        "2026 from the publishing agencies. Release dates are public in advance, so "
        "flagging them involves no look-ahead.")
    con.commit()

    n_cal = con.execute("SELECT COUNT(*) FROM macro_event_calendar").fetchone()[0]
    n_ev = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_ovl = con.execute("SELECT COUNT(*) FROM event_macro_overlap").fetchone()[0]
    n_hit = con.execute(
        "SELECT COUNT(DISTINCT symbol || fiscal_date) FROM event_macro_overlap").fetchone()[0]

    print(f"[15] macro event calendar | {n_cal} scheduled releases")
    for y, c in con.execute(
            "SELECT SUBSTR(date,1,4), COUNT(*) FROM macro_event_calendar GROUP BY 1"):
        print(f"    {y}: {c}")
    print(f"\n  events with >=1 macro release in the forecast window: "
          f"{n_hit}/{n_ev} ({100 * n_hit / n_ev:.1f}%)")
    print(f"  total overlaps: {n_ovl}")
    print("  by release type:")
    for t, c in con.execute(
            "SELECT event_type, COUNT(*) FROM event_macro_overlap GROUP BY 1 ORDER BY 2 DESC"):
        print(f"    {t:15s} {c}")
    print("  releases per event:")
    for k, v in con.execute("""SELECT n, COUNT(*) FROM
        (SELECT symbol, fiscal_date, COUNT(*) n FROM event_macro_overlap
          GROUP BY symbol, fiscal_date) GROUP BY n ORDER BY n"""):
        print(f"    {k} release(s): {v} events")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
