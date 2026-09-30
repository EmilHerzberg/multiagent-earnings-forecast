"""POST-HOC CHECK - compare the dataset's window-start membership with an
independent historical constituent list.

    python scripts/32_membership_historical_list_check.py             # cached row
    python scripts/32_membership_historical_list_check.py --download  # fetch from GitHub

Not part of the pre-registered analysis. It touches no database and, unless
``--download`` is given, makes no network call.

The independent list
--------------------
fja05680, "S&P 500 Historical Components & Changes (Updated)", a public
dataset on GitHub (MIT licence) with one row per date on which the index
composition changed and the full ticker list on that date. The row used is
2024-12-23, the last composition before the study window opens on
2025-01-01. It is pinned to commit a2430f2af0c79ddf0748e91de11bdeb1616ab5a7
and a copy of that single row is kept in
``sp500_fja05680_2024-12-23.csv`` so the check runs offline.

What the script does
--------------------
1. Read the 503 positions of the 2024-12-23 row.
2. Normalise share-class notation to the dataset's form (``BRK.B`` -> ``BRK-B``)
   and map the three tickers renamed inside the window to the symbol the
   dataset carries (MMC -> MRSH, BK -> BNY, FI -> FISV).
3. Compare with the dataset's window-start membership: every symbol in
   ``sp500_constituents.txt`` without an addition row in
   ``sp500_index_changes.csv``.

Result on the published files
-----------------------------
    all 502 window-start members are among the 503 positions of the list;
    the one position the dataset lacks is APO (Apollo Global Management,
    added to the index on 2024-12-23 and missing from the working list).

This check cannot see the Eastman Chemical gap (a removal missing from the
change log), because Eastman was still a member on 2024-12-23; that gap is
found by the roll-back in ``31_membership_anchor_rollback.py``.
"""
from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from pathlib import Path

DATASET = Path(__file__).resolve().parents[1]
CACHED_ROW = DATASET / "sp500_fja05680_2024-12-23.csv"
WORKING_LIST = DATASET / "sp500_constituents.txt"
IN_WINDOW = DATASET / "sp500_index_changes.csv"

COMMIT = "a2430f2af0c79ddf0748e91de11bdeb1616ab5a7"
REMOTE = ("https://raw.githubusercontent.com/fja05680/sp500/" + COMMIT +
          "/S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv")
AS_OF = "2024-12-23"
#: Tickers renamed inside the study window; the dataset carries the new symbol.
RENAMES = {"MMC": "MRSH", "BK": "BNY", "FI": "FISV"}


def read_rows(path: Path) -> list[dict]:
    return list(csv.DictReader(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")))


def read_list(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def row_from_download() -> tuple[str, list[str]]:
    with urllib.request.urlopen(REMOTE, timeout=120) as resp:
        text = resp.read().decode("utf-8")
    rows = list(csv.reader(text.splitlines()))
    date, tickers = max((r for r in rows[1:] if r[0] <= "2024-12-31"), key=lambda r: r[0])
    return date, [t.strip() for t in tickers.split(",") if t.strip()]


def row_from_cache() -> tuple[str, list[str]]:
    rows = read_rows(CACHED_ROW)
    if len(rows) != 1:
        raise SystemExit(f"{CACHED_ROW.name} should hold exactly one data row")
    return rows[0]["date"], [t.strip() for t in rows[0]["tickers"].split(",") if t.strip()]


def normalise(ticker: str) -> str:
    return RENAMES.get(ticker.upper().replace(".", "-"), ticker.upper().replace(".", "-"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--download", action="store_true",
                    help=f"Fetch the list from GitHub (commit {COMMIT[:7]}) instead of the cached row.")
    args = ap.parse_args()

    date, raw = row_from_download() if args.download else row_from_cache()
    if date != AS_OF:
        print(f"warning: list row is dated {date}, expected {AS_OF}", file=sys.stderr)
    listed = {normalise(t) for t in raw}
    working = read_list(WORKING_LIST)
    added = {r["symbol"] for r in read_rows(IN_WINDOW) if r["action"] == "add"}
    start = set(working) - added

    src = "download" if args.download else CACHED_ROW.name
    print(f"independent list: fja05680, row {date}, {len(raw)} positions ({src})")
    print(f"dataset window-start membership: {len(start)} symbols")
    missing = sorted(start - listed)
    extra = sorted(listed - start)
    print(f"  window-start members not in the list: {len(missing)}" + (f"  {', '.join(missing)}" if missing else ""))
    print(f"  list positions not in the dataset   : {len(extra)}" + (f"  {', '.join(extra)}" if extra else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
