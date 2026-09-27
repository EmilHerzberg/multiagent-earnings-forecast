"""Fetch the announcement timing (before/after market) from EODHD.

    python scripts/04_eodhd_earnings_timing.py

Run after script 02.

Source
------
EODHD ``GET /api/calendar/earnings?from=&to=``, field ``before_after_market``.
Values: ``BeforeMarket``, ``AfterMarket``, ``DuringMarket``, or NULL where the
provider does not know.

Why this field matters
----------------------
It determines which trading day first prices the announcement, and therefore
which day is t=0 in an event study::

    BeforeMarket   announced before the open  -> reaction on report_date
    DuringMarket   announced intraday         -> reaction on report_date
    AfterMarket    announced after the close  -> reaction on the NEXT trading day
    NULL           unknown                    -> t=0 cannot be assigned safely

Aligning every announcement to ``report_date`` regardless of timing mixes two
different event windows and attenuates measured abnormal returns.  Roughly 40 %
of the announcements in this window are AfterMarket, so the effect is not
marginal.

``event_day_sql()`` below is the mapping; import it in the analysis so the same
rule is applied there.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
quarterly_chunks = __import__("02_eodhd_earnings_eps").quarterly_chunks

TABLE = "earnings"
ENDPOINT = "/api/calendar/earnings"

VALID = {"BeforeMarket", "AfterMarket", "DuringMarket"}


def event_day_sql() -> str:
    """SQL expression mapping an announcement to its first-reaction calendar day.

    ``AfterMarket`` shifts to the next calendar day; the analysis must then snap
    that to the next available trading day in ``prices_daily``.
    """
    return """
        CASE before_after_market
            WHEN 'AfterMarket' THEN DATE(report_date, '+1 day')
            ELSE report_date
        END
    """


def _norm(value):
    if not value:
        return None
    v = str(value).strip()
    return v if v in VALID else None


def fetch(symbols: set[str], start: str, end: str) -> list[tuple]:
    out = []
    for a, b in quarterly_chunks(start, end):
        for r in config.eodhd_earnings_calendar(a, b):
            code = str(r.get("code") or "")
            if not code.endswith(".US") or code[:-3] not in symbols:
                continue
            fiscal = config.iso_date(r.get("date")) or config.iso_date(r.get("report_date"))
            if fiscal:
                out.append((_norm(r.get("before_after_market")), code[:-3], fiscal))
    return out


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    args = ap.parse_args()

    symbols = config.constituents()
    if args.limit:
        symbols = symbols[: args.limit]

    print(f"[04] announcement timing | {args.start}..{args.end}")
    con = config.connect()
    if not con.execute("SELECT COUNT(*) FROM earnings").fetchone()[0]:
        raise SystemExit("earnings is empty — run 02_eodhd_earnings_eps.py first")

    rows = fetch(set(symbols), args.start, args.end)
    con.executemany(
        "UPDATE earnings SET before_after_market = ? WHERE symbol = ? AND fiscal_date = ?", rows
    )
    con.commit()

    dist = con.execute(
        """SELECT COALESCE(before_after_market, '(unknown)'), COUNT(*)
             FROM earnings GROUP BY 1 ORDER BY 2 DESC"""
    ).fetchall()
    total = sum(c for _, c in dist)
    filled = sum(c for v, c in dist if v != "(unknown)")

    config.record_provenance(
        con, TABLE, "EODHD", f"{ENDPOINT} (field: before_after_market)", filled,
        "Announcement timing. AfterMarket announcements first price on the following "
        "trading day; see event_day_sql().",
    )
    con.commit()

    print(f"  timing filled: {filled} / {total}")
    for value, count in dist:
        print(f"    {value:12s} {count:5d}  ({100 * count / total:.1f}%)")
    if total - filled:
        print(f"  note: {total - filled} announcements have no timing -> t=0 ambiguous.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
