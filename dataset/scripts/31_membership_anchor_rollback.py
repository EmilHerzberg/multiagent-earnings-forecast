"""POST-HOC CHECK - roll the current S&P 500 constituents back to the window start
and compare with the membership the dataset used.

    python scripts/31_membership_anchor_rollback.py

Not part of the pre-registered analysis. It reads only the curated files in
the dataset folder, touches no database and calls no API.

Why an anchor is needed, and what this repeats
----------------------------------------------
A change log alone (who joined, who left, on which day) does not say who was a
member on day one. That needs one complete list at one date - the anchor -
from which every other date follows by reversing the changes in between. The
working list ``sp500_constituents.txt`` was built before the experiment this
way, from SPY holdings whose retrieval day was not recorded (README section
7.1). This script repeats that backward calculation after the experiment with
holdings retrieved again, this time with a date: the equity holdings of the
SPDR S&P 500 ETF Trust (SPY) as published by the fund on ssga.com, dated
2026-09-21, kept as ``sp500_constituents_current_2026-09-21.csv`` with its
source in the header. SPY replicates the index in full, so its equity holdings
are the constituents on that date. The companion check
``32_membership_historical_list_check.py`` compares the same window-start
membership with an independent historical list.

What the script does
--------------------
1. Start from the anchor (503 tickers; a placeholder line with a zero weight
   is dropped).
2. Reverse the changes between the window end and the anchor date
   (``sp500_index_changes_after_window.csv``, ten rows from three S&P DJI press
   releases): an addition is removed, a removal is put back. Two tickers that
   changed after the window are mapped back to the symbol the dataset uses
   (ECHO -> SATS, VMRK -> EQR). Result: membership on 2026-06-30.
3. Reverse the 59 in-window changes (``sp500_index_changes.csv``) the same way.
   PSKY is mapped back to PARA, the symbol the dataset carries for Paramount.
   Result: membership on 2025-01-01.
4. Compare with the dataset's window-start membership: every symbol in
   ``sp500_constituents.txt`` that has no addition row, i.e. the companies the
   builder treats as members from the first day (``assumed_at_window_start``).
   Also compare the union over the window with the 530-symbol working list.

Result on the published files
-----------------------------
    anchor 503 -> 2026-06-30: 503 -> 2025-01-01: 502 tickers
    dataset window-start membership: 502 tickers
    in the roll-back but not in the dataset : APO   (Apollo Global Management,
        added 2024-12-23; missing from the working list)
    in the dataset but not in the roll-back : EMN   (Eastman Chemical, removed
        2025-11-04; the change log has no row for it, so reversing the log from
        an anchor that no longer holds EMN cannot put it back)
    union over the window vs working list   : +APO, +FDXF, +HONA (the two
        June-2026 spin-offs have no in-window prices and were left out of the
        working list on purpose); -EMN as above

Reading the result: the working list agrees with the roll-back on 501 of 502
window-start members; the one company the roll-back adds is Apollo, the one
it cannot restore is Eastman Chemical, and both are recorded as known errata.
The check is only as good as the change log between the anchor and the window
start; a change missing from the log propagates into the roll-back, which is
exactly what the Eastman case shows.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

DATASET = Path(__file__).resolve().parents[1]
ANCHOR = DATASET / "sp500_constituents_current_2026-09-21.csv"
AFTER_WINDOW = DATASET / "sp500_index_changes_after_window.csv"
IN_WINDOW = DATASET / "sp500_index_changes.csv"
WORKING_LIST = DATASET / "sp500_constituents.txt"

#: Ticker changes between the window end and the anchor date, mapped back to
#: the symbol the dataset uses. EchoStar changed SATS -> ECHO on 2026-06-24;
#: Equity Residential became Vivmark Residential (VMRK) on 2026-08-18.
POST_WINDOW_RENAMES = {"ECHO": "SATS", "VMRK": "EQR"}
#: Paramount Global (PARA) merged into Skydance on 2025-08-07 and trades as
#: PSKY since; the dataset carries the pre-merger symbol.
IN_WINDOW_RENAMES = {"PSKY": "PARA"}
#: Lines in the holdings file that are not constituents (zero-weight placeholders).
NOT_CONSTITUENTS = {"2602335D"}


def read_rows(path: Path) -> list[dict]:
    return list(csv.DictReader(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")))


def read_list(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def reverse(changes: list[dict], members: set[str]) -> set[str]:
    """Undo the changes, newest first: additions come out, removals go back in."""
    out = set(members)
    for r in sorted(changes, key=lambda r: r["effective_date"], reverse=True):
        if r["action"] == "add":
            out.discard(r["symbol"])
        else:
            out.add(r["symbol"])
    return out


def rename(members: set[str], mapping: dict[str, str]) -> set[str]:
    return {mapping.get(s, s) for s in members}


def show(label: str, symbols) -> None:
    symbols = sorted(symbols)
    print(f"  {label}: {len(symbols)}" + (f"  {', '.join(symbols)}" if symbols else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()

    anchor = {r["symbol"] for r in read_rows(ANCHOR)} - NOT_CONSTITUENTS
    after = read_rows(AFTER_WINDOW)
    within = read_rows(IN_WINDOW)
    working = read_list(WORKING_LIST)
    added_in_window = {r["symbol"] for r in within if r["action"] == "add"}
    dataset_start = set(working) - added_in_window

    end_of_window = rename(reverse(after, anchor), POST_WINDOW_RENAMES)
    start_of_window = rename(reverse(within, end_of_window), IN_WINDOW_RENAMES)
    rolled_union = start_of_window | added_in_window

    print(f"anchor: SPY holdings {ANCHOR.name} -> {len(anchor)} tickers")
    print(f"reversed {len(after)} post-window changes -> membership on 2026-06-30: {len(end_of_window)}")
    print(f"reversed {len(within)} in-window changes  -> membership on 2025-01-01: {len(start_of_window)}")
    print(f"dataset window-start membership (working list minus in-window additions): {len(dataset_start)}")
    print()
    print("Window start, 2025-01-01:")
    show("in the roll-back, not in the dataset", start_of_window - dataset_start)
    show("in the dataset, not in the roll-back", dataset_start - start_of_window)
    print("Union over the window:")
    show("in the roll-back union, not in the working list", rolled_union - set(working))
    show("in the working list, not in the roll-back union", set(working) - rolled_union)
    return 0


if __name__ == "__main__":
    sys.exit(main())
