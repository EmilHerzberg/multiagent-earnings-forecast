"""Find the announcement-timing conflicts of working document §8.1.

The provider's BeforeMarket/AfterMarket flag decides t0. For AfterMarket
events the report date should be quiet and the move should land on t0. For a
small set it does not: the report-date move is larger. WD §8.1 documents the
rule and the population share (~3.2 % of AfterMarket events); the analysis
plan §8.2 pre-registers recomputing the primary endpoints without these
events. This script derives the list DETERMINISTICALLY from thesis.db so the
exclusion file handed to `24_score_experiment.py --exclude-events` is
reproducible rather than hand-maintained.

The rule, verbatim from WD §8.1 (parameters below, fixed):
    an AfterMarket event is a conflict iff
        |return(report_date)|  >  RATIO x |return(t0)|
    and |return(report_date)|  >  FLOOR
Returns are close-to-close on the symbol's own adjusted series (adj_close),
each day against the previous trading day in that series.

Usage:
    python scripts/26_find_timing_conflicts.py                  # population
    python scripts/26_find_timing_conflicts.py --sample event_sample.txt
    python scripts/26_find_timing_conflicts.py --self-test

With --sample the output file contains only conflicts among the drawn events,
in exactly the SYMBOL,DATE format --exclude-events expects.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_DEFAULT = HERE.parent / "thesis.db"
OUT_DEFAULT = HERE.parent / "timing_conflicts.csv"

# The WD §8.1 rule's parameters. Fixed; changing them is an analysis-plan
# amendment, not a tweak.
RATIO = 2.0
FLOOR = 0.03


def day_return(series: dict[str, float], dates: list[str], day: str) -> float | None:
    """Close-to-close return on `day` vs the previous trading day in the series.

    `dates` is the symbol's sorted trading calendar. None when the day is not a
    bar of this series or has no predecessor - such events cannot be assessed
    and are reported as unassessable rather than silently passed.
    """
    if day not in series:
        return None
    import bisect
    i = bisect.bisect_left(dates, day)
    if i == 0 or dates[i] != day:
        return None
    prev = dates[i - 1]
    if series[prev] == 0:
        return None
    return series[day] / series[prev] - 1.0


def is_conflict(ret_report: float, ret_t0: float,
                ratio: float = RATIO, floor: float = FLOOR) -> bool:
    """The WD §8.1 rule on two returns. Pure, so the self-test can pin it."""
    return abs(ret_report) > ratio * abs(ret_t0) and abs(ret_report) > floor


def find_conflicts(db_path: Path) -> tuple[list[dict], int, int]:
    """All AfterMarket conflicts in the events table, plus counts."""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    events = con.execute(
        "SELECT symbol, report_date, t0 FROM events "
        "WHERE before_after_market = 'AfterMarket' ORDER BY symbol, report_date"
    ).fetchall()

    conflicts, unassessable = [], 0
    by_symbol: dict[str, tuple[dict, list]] = {}
    for sym, report_date, t0 in events:
        if sym not in by_symbol:
            rows = con.execute(
                "SELECT date, adj_close FROM prices_daily WHERE symbol = ? "
                "ORDER BY date", (sym,)).fetchall()
            series = {d: c for d, c in rows if c is not None}
            by_symbol[sym] = (series, sorted(series))
        series, dates = by_symbol[sym]
        r_rep = day_return(series, dates, report_date)
        r_t0 = day_return(series, dates, t0)
        if r_rep is None or r_t0 is None:
            unassessable += 1
            continue
        if is_conflict(r_rep, r_t0):
            conflicts.append({"symbol": sym, "report_date": report_date,
                              "ret_report": r_rep, "ret_t0": r_t0})
    con.close()
    return conflicts, len(events), unassessable


def self_test() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond

    print("=== the rule itself ===")
    check("big report move, quiet t0 -> conflict", is_conflict(0.05, 0.01))
    check("below the 3% floor is never a conflict, however lopsided",
          not is_conflict(0.029, 0.001))
    check("exactly 2x is NOT a conflict (strictly greater required)",
          not is_conflict(0.04, 0.02))
    check("big t0 move dominates -> no conflict", not is_conflict(0.05, 0.04))
    check("signs are irrelevant, magnitudes decide", is_conflict(-0.05, 0.01))

    print("=== the return arithmetic ===")
    series = {"2025-01-02": 100.0, "2025-01-03": 103.0, "2025-01-06": 100.94}
    dates = sorted(series)
    r = day_return(series, dates, "2025-01-03")
    check("close-to-close return 100 -> 103 is +3%", r is not None and abs(r - 0.03) < 1e-12)
    check("a day that is not a bar returns None (unassessable, not zero)",
          day_return(series, dates, "2025-01-04") is None)
    check("the first bar has no predecessor -> None",
          day_return(series, dates, "2025-01-02") is None)
    print("SELF-TEST:", "all passed" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--sample", default=None,
                    help="event_sample.txt; restrict the OUTPUT to drawn events")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    conflicts, n_am, unassessable = find_conflicts(Path(args.db))
    print(f"AfterMarket events assessed: {n_am - unassessable} of {n_am} "
          f"({unassessable} unassessable: day not a bar of the symbol's series)")
    print(f"conflicts in the population: {len(conflicts)} "
          f"({len(conflicts) / max(n_am, 1) * 100:.1f}% of AfterMarket)")

    keep = conflicts
    if args.sample:
        drawn = set()
        for line in Path(args.sample).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line == "symbol,report_date":
                continue
            sym, date = line.split(",")
            drawn.add((sym, date))
        keep = [c for c in conflicts if (c["symbol"], c["report_date"]) in drawn]
        print(f"conflicts among the {len(drawn)} drawn events: {len(keep)}")

    out = Path(args.out)
    lines = ["# Timing conflicts per WD §8.1 rule: AfterMarket, "
             f"|ret(report)| > {RATIO} x |ret(t0)| and > {FLOOR:.0%}.",
             "# Generated by scripts/26_find_timing_conflicts.py - do not edit.",
             "# Format matches 24_score_experiment.py --exclude-events.",
             "symbol,report_date"]
    for c in keep:
        lines.append(f"{c['symbol']},{c['report_date']}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(keep)} events)")
    for c in keep:
        print(f"  {c['symbol']:6} {c['report_date']}  ret(report) {c['ret_report']:+.2%}"
              f"  ret(t0) {c['ret_t0']:+.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
