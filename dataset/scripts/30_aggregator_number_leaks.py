"""POST-HOC ANALYSIS - how often a worker's reasoning stated its own probability.

    python scripts/30_aggregator_number_leaks.py                  # reads scaffold/pilot5/L1_* and L2_*
    python scripts/30_aggregator_number_leaks.py --dir <folder>   # another outcomes folder

Not part of the pre-registered analysis. Written after the run to quantify a
design residual (D27) for the results discussion; it answers no research
question and feeds no score.

What is being counted
---------------------
The aggregator rung reads only the workers' written reasoning, never their
probabilities (D1, D27): the prompt asks each worker to keep the number out of
the reasoning text so that the aggregator combines arguments, not numbers.
Compliance is not perfect. At run time the harness therefore checked every
worker answer that was handed to the aggregator and counted how many of the
three stated their own probability inside the reasoning text:

    harness/pilot/prompts.ts       reasonStatesOwnNumber(reason, probability)
    harness/pilot/structures5.ts   aggregatorNumberLeaks = workers.filter(...).length

The count is stored per ladder (event x model x level) in the field
``aggregatorNumberLeaks`` of ``L1_outcomes.jsonl`` / ``L2_outcomes.jsonl``.
This script only aggregates that field: leaks summed over all ladders of a
model, divided by 3 x ladders = the share of worker answers the aggregator
read that contained the worker's own number.

What the detector recognises, and what it misses
-----------------------------------------------
``reasonStatesOwnNumber`` matches the worker's own probability written as a
percentage ("65 %", "65 percent") or as a decimal ("0.65", ".65"), with
boundaries so that unrelated figures such as "9.65 %" or "0.653" do not
count. It does not recognise verbal statements ("roughly two in three",
"slightly below even odds"), ranges ("60-70 %") or a number given in a
different rounding, so the shares below are a lower bound on how often the
aggregator could infer a worker's number.

Result on the run (both levels, 840 ladders per model and level)
----------------------------------------------------------------
    L1  deepseek-chat-v3-0324     41 of 2,520 worker answers   1.6 %
    L1  qwen3-32b@on           1,915 of 2,520                 76.0 %
    L2  deepseek-chat-v3-0324     54 of 2,520                  2.1 %
    L2  qwen3-32b@on           1,680 of 2,520                 66.7 %
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

WORKERS_PER_LADDER = 3
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "scaffold" / "pilot5"


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def summarise(records: list[dict]) -> dict[str, tuple[int, int, int]]:
    """model -> (ladders, leaks, ladders whose workers were all well-formed)."""
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in records:
        m = out[r["modelKey"]]
        m[0] += 1
        m[1] += int(r.get("aggregatorNumberLeaks", 0))
        if not r["outcomes"]["repetition"]["dropped"]:
            m[2] += 1
    return {k: (v[0], v[1], v[2]) for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                    help="Folder holding L1_outcomes.jsonl and L2_outcomes.jsonl.")
    args = ap.parse_args()

    print("Worker answers read by the aggregator that stated the worker's own probability")
    print(f"(field aggregatorNumberLeaks, {WORKERS_PER_LADDER} workers per ladder; source: {args.dir})\n")
    print(f"{'level':5s}  {'model':26s}  {'ladders':>7s}  {'leaks':>6s}  {'answers':>8s}  {'share':>6s}")
    found = False
    for level in ("L1", "L2"):
        path = args.dir / f"{level}_outcomes.jsonl"
        if not path.exists():
            print(f"{level:5s}  (no {path.name})")
            continue
        found = True
        for model, (ladders, leaks, complete) in sorted(summarise(read_jsonl(path)).items()):
            answers = WORKERS_PER_LADDER * ladders
            note = "" if complete == ladders else f"   ({ladders - complete} ladders without a full worker set)"
            print(f"{level:5s}  {model:26s}  {ladders:>7,}  {leaks:>6,}  {answers:>8,}  {100 * leaks / answers:>5.1f}%{note}")
    if not found:
        print("no outcomes files found", file=sys.stderr)
        return 1
    print("\nA ladder's count is the number of its three workers whose reasoning text contained\n"
          "the worker's own probability (percentage or decimal form). Verbal statements and\n"
          "ranges are not detected, so the shares are lower bounds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
