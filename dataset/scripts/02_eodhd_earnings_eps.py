"""Fetch EPS actual and EPS estimate from EODHD into ``earnings``.

    python scripts/02_eodhd_earnings_eps.py

Source
------
EODHD ``GET /api/calendar/earnings?from=&to=``

A market-wide endpoint: one call returns every company reporting in the
requested range, so the full study window costs 6 calls rather than one per
constituent.  Requested in quarterly chunks because the endpoint truncates very
large ranges.

Response fields consumed here::

    code          symbol with exchange suffix, e.g. "AAPL.US"
    report_date   announcement date
    date          end of the reported fiscal quarter
    actual        reported EPS
    estimate      consensus EPS estimate

Definition note
---------------
``actual`` and ``estimate`` are the consensus-comparable ("street") EPS figures,
generally diluted and adjusted for non-recurring items.  They are matched on a
like-for-like basis, which is what makes the surprise in script 03 well
defined.  They are NOT the GAAP basic/diluted EPS from the 10-Q and cannot be
reconciled to net income divided by a share count.

This script creates the ``earnings`` rows; scripts 03 and 04 enrich them.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "earnings"
ENDPOINT = "/api/calendar/earnings"


def quarterly_chunks(start: str, end: str):
    """Split [start, end] into calendar-quarter ranges."""
    y, m = int(start[:4]), int(start[5:7])
    q_start = f"{y}-{((m - 1) // 3) * 3 + 1:02d}-01"
    while q_start <= end:
        y, m = int(q_start[:4]), int(q_start[5:7])
        ny, nm = (y + 1, 1) if m + 3 > 12 else (y, m + 3)
        q_end = min(f"{ny}-{nm:02d}-01", end)
        yield max(q_start, start), q_end
        q_start = f"{ny}-{nm:02d}-01"


def fetch(symbols: set[str], start: str, end: str) -> list[tuple]:
    """Retrieve EPS actual/estimate for the constituent universe."""
    out, calls = [], 0
    for a, b in quarterly_chunks(start, end):
        records = config.eodhd_earnings_calendar(a, b)
        calls += 1
        kept = 0
        for r in records:
            code = str(r.get("code") or "")
            if not code.endswith(".US"):
                continue
            symbol = code[:-3]
            if symbol not in symbols:
                continue
            report = config.iso_date(r.get("report_date"))
            fiscal = config.iso_date(r.get("date")) or report
            if not fiscal:
                continue
            out.append((symbol, fiscal, report,
                        config.to_float(r.get("actual")),
                        config.to_float(r.get("estimate"))))
            kept += 1
        print(f"  {a} .. {b}: {len(records)} records, {kept} in universe")
    print(f"  {calls} API calls")
    return out


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    args = ap.parse_args()

    symbols = config.constituents()
    if args.limit:
        symbols = symbols[: args.limit]

    print(f"[02] EPS actual + estimate | {args.start}..{args.end}")
    rows = fetch(set(symbols), args.start, args.end)

    # Collapse duplicate fiscal periods (a company can appear in two chunks if
    # the provider reports a revision).
    seen, clean, dupes = set(), [], 0
    for r in rows:
        key = (r[0], r[1])
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        clean.append(r)

    con = config.connect()
    con.executemany(
        """INSERT INTO earnings (symbol, fiscal_date, report_date, eps_actual, eps_estimate)
           VALUES (?,?,?,?,?)
           ON CONFLICT(symbol, fiscal_date) DO UPDATE SET
               report_date  = excluded.report_date,
               eps_actual   = excluded.eps_actual,
               eps_estimate = excluded.eps_estimate""",
        clean,
    )
    config.record_provenance(
        con, TABLE, "EODHD", ENDPOINT, len(clean),
        "EPS actual + estimate. Street (consensus-comparable) EPS, not GAAP basic/diluted."
        + (f" {dupes} duplicate fiscal periods collapsed." if dupes else ""),
    )
    con.commit()

    n = con.execute("SELECT COUNT(*) FROM earnings").fetchone()[0]
    a = con.execute("SELECT COUNT(eps_actual) FROM earnings").fetchone()[0]
    e = con.execute("SELECT COUNT(eps_estimate) FROM earnings").fetchone()[0]
    s = con.execute("SELECT COUNT(DISTINCT symbol) FROM earnings").fetchone()[0]
    print(f"  rows: {n} | symbols: {s} | eps_actual: {a} | eps_estimate: {e}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
