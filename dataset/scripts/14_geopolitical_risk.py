"""Load the daily AI-GPR geopolitical risk index.

    python scripts/14_geopolitical_risk.py [--refresh]

Source
------
Caldara, Iacoviello et al. (2026), "The AI-GPR Index: Measuring Geopolitical
Risk using Artificial Intelligence", Board of Governors of the Federal Reserve
System.

    https://www.matteoiacoviello.com/ai_gpr.html
    https://www.matteoiacoviello.com/ai_gpr_files/ai_gpr_data_daily.csv

Why AI-GPR rather than the original GPR
---------------------------------------
The 2022 GPR index counts newspaper articles matching a keyword dictionary.
AI-GPR replaces that with semantic reading: GPT-4o-mini scores roughly five
million articles from the New York Times, Washington Post and Chicago Tribune
(1960-2026) for geopolitical risk intensity. That removes false positives from
articles using words like "war" or "attack" in non-geopolitical senses, and
catches relevant articles that use none of the dictionary terms.

The published `GPR_AER` column reproduces the original keyword index on the same
data, so both are stored and the choice is auditable rather than asserted.

Columns kept
------------
    gpr_ai         the AI index; 100 = 1985-2019 average
    gpr_aer        the original keyword-based index, for comparison
    gpr_threats    risk from threatened events
    gpr_acts       risk from realised events
    gpr_oil        sub-index for oil and energy supply disruption
    gpr_nonoil     the remainder
    gpr_ai_ma7     7-day moving average, computed here
    gpr_ai_ma30    30-day moving average, computed here

Threats and acts are separated because markets price an anticipated conflict
differently from one that has already happened. The oil sub-index matters for a
US equity study because energy-supply shocks transmit to earnings through input
costs rather than through risk appetite.

Moving averages are computed over the **preceding** window including the current
day, so no future observation enters. The daily index is noisy — a single
newspaper day can swing it — and the 30-day average is the level a forecaster
would actually read as "the current geopolitical climate".

No look-ahead: the index for a day is built from that day's newspapers, so the
value dated t0 was knowable at t0's close.

Format
------
The source is CSV, so this script needs no third-party library. It is cached to
``ai_gpr_daily.csv`` so a rebuild works offline; ``--refresh`` re-downloads.
"""
from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "geopolitical_risk"
SOURCE_URL = "https://www.matteoiacoviello.com/ai_gpr_files/ai_gpr_data_daily.csv"
CACHE_CSV = config.THESIS_DIR / "ai_gpr_daily.csv"

#: source column -> our column
FIELDS = {
    "GPR_AI": "gpr_ai",
    "GPR_AER": "gpr_aer",
    "THREATS_GPR_AI": "gpr_threats",
    "ACTS_GPR_AI": "gpr_acts",
    "GPR_OIL": "gpr_oil",
    "GPR_NONOIL": "gpr_nonoil",
}

SCHEMA = """
DROP TABLE IF EXISTS geopolitical_risk;
CREATE TABLE geopolitical_risk (
    date        TEXT PRIMARY KEY,
    gpr_ai      REAL,   -- AI-scored index, 100 = 1985-2019 average
    gpr_aer     REAL,   -- original keyword index (Caldara & Iacoviello 2022)
    gpr_threats REAL,   -- threatened events
    gpr_acts    REAL,   -- realised events
    gpr_oil     REAL,   -- oil / energy supply disruption sub-index
    gpr_nonoil  REAL,
    gpr_ai_ma7  REAL,   -- trailing 7-day mean, current day included
    gpr_ai_ma30 REAL    -- trailing 30-day mean, current day included
);
"""


def download() -> list[dict]:
    print(f"  downloading {SOURCE_URL}")
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "thesis-data-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        blob = r.read()
    CACHE_CSV.write_bytes(blob)
    print(f"  {len(blob):,} bytes cached -> {CACHE_CSV.name}")
    return read_cache()


def read_cache() -> list[dict]:
    with open(CACHE_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def trailing_mean(values: list[float | None], window: int) -> list[float | None]:
    """Mean over the preceding *window* observations, current one included."""
    out: list[float | None] = []
    for i in range(len(values)):
        chunk = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--refresh", action="store_true", help="Re-download the source file.")
    args = ap.parse_args()

    print("[14] AI-GPR geopolitical risk index (Caldara & Iacoviello et al. 2026)")
    rows = download() if (args.refresh or not CACHE_CSV.exists()) else read_cache()
    if not args.refresh:
        print(f"  using cached {CACHE_CSV.name} ({len(rows)} rows); --refresh to update")

    earliest = config.PRICE_HISTORY_FROM
    # Moving averages need history before the window, so compute on everything
    # from a year earlier and only keep the window afterwards.
    lead = f"{int(earliest[:4]) - 1}{earliest[4:]}"
    series = sorted(
        ((config.iso_date(r["Date"]), r) for r in rows if r.get("Date")),
        key=lambda x: x[0])
    series = [(d, r) for d, r in series if lead <= d <= config.END_DATE]

    ai = [config.to_float(r["GPR_AI"]) for _, r in series]
    ma7 = trailing_mean(ai, 7)
    ma30 = trailing_mean(ai, 30)

    out = []
    for i, (d, r) in enumerate(series):
        if d < earliest:
            continue
        out.append((d, *[config.to_float(r.get(src)) for src in FIELDS], ma7[i], ma30[i]))

    con = config.connect()
    con.executescript(SCHEMA)
    con.execute("DELETE FROM provenance WHERE table_name = ?", (TABLE,))
    con.executemany(
        f"INSERT INTO geopolitical_risk VALUES ({','.join('?' * 9)})", out)
    config.record_provenance(
        con, TABLE, "Caldara, Iacoviello et al. (2026), Federal Reserve Board", SOURCE_URL,
        len(out),
        "Daily AI-GPR index (GPT-4o-mini scoring of ~5m newspaper articles), with the "
        "original keyword index retained as gpr_aer for comparison. Threats/acts and "
        "oil/non-oil sub-indices included. 7- and 30-day trailing means computed here "
        "over preceding observations only.")
    con.commit()

    n, d0, d1 = con.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM geopolitical_risk").fetchone()
    print(f"  rows: {n:,} ({d0} .. {d1})")
    w = con.execute(
        """SELECT COUNT(*), ROUND(MIN(gpr_ai),1), ROUND(AVG(gpr_ai),1), ROUND(MAX(gpr_ai),1),
                  ROUND(AVG(gpr_aer),1), ROUND(AVG(gpr_oil),1)
             FROM geopolitical_risk WHERE date BETWEEN ? AND ?""",
        (config.START_DATE, config.END_DATE)).fetchone()
    print(f"  in study window: {w[0]} days")
    print(f"    GPR_AI  min {w[1]}  mean {w[2]}  max {w[3]}")
    print(f"    GPR_AER mean {w[4]}   (original keyword index, same days)")
    print(f"    GPR_OIL mean {w[5]}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
