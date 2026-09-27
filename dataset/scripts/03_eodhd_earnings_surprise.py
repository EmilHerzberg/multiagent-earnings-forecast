"""Fetch the EPS surprise percentage from EODHD into ``earnings``.

    python scripts/03_eodhd_earnings_surprise.py [--verify]

Run after script 02: this updates the ``eps_surprise_pct`` column of rows that
02 created.

Source
------
EODHD ``GET /api/calendar/earnings?from=&to=``, field ``percent``.

Definition
----------
The provider reports the surprise as a percentage of the estimate::

    percent = (actual - estimate) / |estimate| * 100

Two properties matter for empirical work:

1. The denominator is the *estimate*, not the share price.  Where the estimate
   is near zero the ratio explodes, which is why raw surprise percentages are
   conventionally winsorised, or replaced by a standardised measure (SUE,
   scaled by forecast dispersion or by price), before entering a regression.
2. It is undefined where the estimate is zero or missing.

The provider value is stored as delivered.  ``--verify`` independently
recomputes it from ``eps_actual``/``eps_estimate`` and reports any disagreement,
which doubles as a check that the two fields refer to the same period.
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


def fetch(symbols: set[str], start: str, end: str) -> list[tuple]:
    out = []
    for a, b in quarterly_chunks(start, end):
        for r in config.eodhd_earnings_calendar(a, b):
            code = str(r.get("code") or "")
            if not code.endswith(".US") or code[:-3] not in symbols:
                continue
            fiscal = config.iso_date(r.get("date")) or config.iso_date(r.get("report_date"))
            if fiscal:
                out.append((config.to_float(r.get("percent")), code[:-3], fiscal))
    return out


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--verify", action="store_true",
                    help="Recompute the surprise from actual/estimate and report deviations.")
    args = ap.parse_args()

    symbols = config.constituents()
    if args.limit:
        symbols = symbols[: args.limit]

    print(f"[03] EPS surprise % | {args.start}..{args.end}")
    con = config.connect()
    if not con.execute("SELECT COUNT(*) FROM earnings").fetchone()[0]:
        raise SystemExit("earnings is empty — run 02_eodhd_earnings_eps.py first")

    rows = fetch(set(symbols), args.start, args.end)
    con.executemany(
        "UPDATE earnings SET eps_surprise_pct = ? WHERE symbol = ? AND fiscal_date = ?", rows
    )
    filled = con.execute("SELECT COUNT(eps_surprise_pct) FROM earnings").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM earnings").fetchone()[0]

    config.record_provenance(
        con, TABLE, "EODHD", f"{ENDPOINT} (field: percent)", filled,
        "EPS surprise %, provider-computed as (actual-estimate)/|estimate|*100.",
    )
    con.commit()
    print(f"  surprise filled: {filled} / {total}")

    if args.verify:
        bad = con.execute(
            """SELECT symbol, fiscal_date, eps_surprise_pct,
                      (eps_actual - eps_estimate) / ABS(eps_estimate) * 100
                 FROM earnings
                WHERE eps_actual IS NOT NULL AND eps_estimate IS NOT NULL
                  AND eps_estimate != 0 AND eps_surprise_pct IS NOT NULL
                  AND ABS(eps_surprise_pct
                          - (eps_actual - eps_estimate) / ABS(eps_estimate) * 100) > 1.0"""
        ).fetchall()
        checked = con.execute(
            """SELECT COUNT(*) FROM earnings
                WHERE eps_actual IS NOT NULL AND eps_estimate IS NOT NULL
                  AND eps_estimate != 0 AND eps_surprise_pct IS NOT NULL"""
        ).fetchone()[0]
        print(f"  verification: {len(bad)} of {checked} rows deviate >1pp from recomputation")
        for r in bad[:10]:
            print(f"    {r[0]:6s} {r[1]}  provider={r[2]:.2f}  recomputed={r[3]:.2f}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
