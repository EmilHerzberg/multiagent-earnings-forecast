"""Load daily policy-uncertainty indices: TPU and US EPU.

    python scripts/16_uncertainty_indices.py [--refresh]

Two indices, both newspaper-based, both daily, both free.

Trade Policy Uncertainty (TPU)
------------------------------
Caldara, Iacoviello, Molligo, Prestipino and Raffo (2020), "The Economic
Effects of Trade Policy Uncertainty", *Journal of Monetary Economics* 109.

    https://www.matteoiacoviello.com/tpu.htm
    https://www.matteoiacoviello.com/tpu_files/tpu_web_latest.xlsx  (TPU_DAILY sheet)

Counts articles combining trade-policy terms (tariff, import duty, trade act,
dumping, …) with uncertainty terms. The publisher ships `TPUD_index` plus
7- and 30-day moving averages, which are stored as delivered rather than
recomputed.

Relevant here because tariff and trade-policy shocks hit equities very unevenly:
importers, exporters and domestic-facing firms respond in opposite directions to
the same headline. A single market-wide return cannot capture that, so the level
of trade-policy uncertainty is genuine context for a company-level forecast.

US Economic Policy Uncertainty (EPU)
------------------------------------
Baker, Bloom and Davis (2016), "Measuring Economic Policy Uncertainty",
*Quarterly Journal of Economics* 131(4).

    https://www.policyuncertainty.com/
    https://www.policyuncertainty.com/media/All_Daily_Policy_Data.csv

The standard measure of policy-related economic uncertainty, from newspaper
coverage of policy, uncertainty and the economy jointly. Broader than TPU: it
captures fiscal, regulatory and monetary policy uncertainty as one number.

Both are newspaper-derived, like AI-GPR, so they share its timing property: the
value dated t0 is built from that day's papers and was knowable at t0's close.

Format note
-----------
The EPU file is CSV. The TPU file is XLSX, so parsing it needs
``pandas`` + ``openpyxl`` — the pipeline's only third-party requirement. The
parsed result is cached to ``tpu_daily.csv``, so the dependency is needed on
``--refresh`` and never on a normal rebuild.
"""
from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "uncertainty_indices"

TPU_URL = "https://www.matteoiacoviello.com/tpu_files/tpu_web_latest.xlsx"
EPU_URL = "https://www.policyuncertainty.com/media/All_Daily_Policy_Data.csv"
TPU_CACHE = config.THESIS_DIR / "tpu_daily.csv"
EPU_CACHE = config.THESIS_DIR / "epu_daily.csv"

SCHEMA = """
DROP TABLE IF EXISTS uncertainty_indices;
CREATE TABLE uncertainty_indices (
    date      TEXT PRIMARY KEY,
    tpu       REAL,   -- trade policy uncertainty, daily
    tpu_ma7   REAL,   -- as published
    tpu_ma30  REAL,   -- as published
    epu_us    REAL    -- US economic policy uncertainty, daily (Baker/Bloom/Davis)
);
"""


def fetch(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "thesis-data-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def refresh_tpu() -> None:
    """Download the TPU workbook and cache its daily sheet as CSV."""
    try:
        import pandas as pd
    except ImportError:
        raise SystemExit(
            "Parsing the TPU workbook needs pandas and openpyxl:\n"
            "    pip install pandas openpyxl\n"
            f"Without them, keep using the cached {TPU_CACHE.name}.")

    print(f"  downloading {TPU_URL}")
    tmp = TPU_CACHE.with_suffix(".xlsx")
    tmp.write_bytes(fetch(TPU_URL))
    df = pd.read_excel(tmp, sheet_name="TPU_DAILY")
    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
    df = df.dropna(subset=["DATE"])
    with open(TPU_CACHE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "tpu", "tpu_ma7", "tpu_ma30"])
        for _, r in df.iterrows():
            w.writerow([r["DATE"].date().isoformat(),
                        r.get("TPUD_index"), r.get("TPUD_index_MA7"),
                        r.get("TPUD_index_MA30")])
    tmp.unlink(missing_ok=True)
    print(f"  cached {len(df)} rows -> {TPU_CACHE.name}")


def refresh_epu() -> None:
    print(f"  downloading {EPU_URL}")
    EPU_CACHE.write_bytes(fetch(EPU_URL))
    print(f"  cached -> {EPU_CACHE.name}")


def read_tpu() -> dict[str, tuple]:
    out = {}
    with open(TPU_CACHE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["date"]] = (config.to_float(r["tpu"]), config.to_float(r["tpu_ma7"]),
                              config.to_float(r["tpu_ma30"]))
    return out


def read_epu() -> dict[str, float]:
    """The published file gives day/month/year columns rather than a date."""
    out = {}
    with open(EPU_CACHE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                d = (f"{int(float(r['year'])):04d}-{int(float(r['month'])):02d}"
                     f"-{int(float(r['day'])):02d}")
            except (ValueError, TypeError, KeyError):
                continue
            v = config.to_float(r.get("daily_policy_index"))
            if v is not None:
                out[d] = v
    return out


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--refresh", action="store_true", help="Re-download both sources.")
    args = ap.parse_args()

    print("[16] policy uncertainty indices")
    if args.refresh or not TPU_CACHE.exists():
        refresh_tpu()
    if args.refresh or not EPU_CACHE.exists():
        refresh_epu()

    tpu, epu = read_tpu(), read_epu()
    dates = sorted((set(tpu) | set(epu)))
    rows = [(d, *(tpu.get(d) or (None, None, None)), epu.get(d))
            for d in dates if config.PRICE_HISTORY_FROM <= d <= config.END_DATE]

    con = config.connect()
    con.executescript(SCHEMA)
    con.execute("DELETE FROM provenance WHERE table_name = ?", (TABLE,))
    con.executemany("INSERT INTO uncertainty_indices VALUES (?,?,?,?,?)", rows)
    config.record_provenance(
        con, TABLE,
        "Caldara et al. (2020) JME 109 [TPU]; Baker, Bloom & Davis (2016) QJE 131(4) [EPU]",
        f"{TPU_URL} ; {EPU_URL}", len(rows),
        "Daily trade-policy uncertainty (index plus published 7- and 30-day moving "
        "averages) and US economic policy uncertainty. Newspaper-derived, so the value "
        "dated t0 was knowable at t0.")
    con.commit()

    n = con.execute("SELECT COUNT(*) FROM uncertainty_indices").fetchone()[0]
    d0, d1 = con.execute("SELECT MIN(date), MAX(date) FROM uncertainty_indices").fetchone()
    print(f"  rows: {n:,} ({d0} .. {d1})")
    for col in ("tpu", "tpu_ma7", "tpu_ma30", "epu_us"):
        c, lo, avg, hi = con.execute(
            f"""SELECT COUNT({col}), ROUND(MIN({col}),1), ROUND(AVG({col}),1),
                       ROUND(MAX({col}),1)
                  FROM uncertainty_indices WHERE date BETWEEN ? AND ?""",
            (config.START_DATE, config.END_DATE)).fetchone()
        print(f"    {col:9s} {c:4d} days in window | min {lo} mean {avg} max {hi}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
