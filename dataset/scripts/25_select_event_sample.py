"""Draw the event sample for the paid run and write it down so it cannot drift.

The exposé commits to "at least 40 and up to 200 stocks, four quarterly reports
each over about one year", i.e. roughly 160 to 800 events, "selected so that a
broad range of industries and company sizes is covered". This script turns that
sentence into one fixed list of (symbol, report_date) pairs.

Everything that could vary is nailed down HERE, in the file, rather than passed
on the command line: the seed, the pool rule, the number of symbols, the number
of quarters. A seed typed at a shell prompt is not part of the committed record -
six months later `event_sample.txt` and the code that supposedly produced it can
disagree and nothing in the repository notices. With the seed in the source, one
`python scripts/25_select_event_sample.py` rewrites the artefact byte for byte,
and a diff is the proof.

    python scripts/25_select_event_sample.py             # draw, write, report
    python scripts/25_select_event_sample.py --dry-run   # report only, write nothing
    python scripts/25_select_event_sample.py --self-test

The database is opened READ-ONLY. This script only ever selects.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import random
import statistics
import subprocess
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE.parent / "thesis.db"
OUT = HERE.parent / "event_sample.txt"

# --------------------------------------------------------------------------- #
# The draw, fixed in advance and never adjusted afterwards.
# --------------------------------------------------------------------------- #

# The seed lives in the source, not in a command-line flag, so the committed
# artefact and the code that produces it cannot disagree. 20260903 is the date
# the draw was made, which is the only property a seed needs: it was chosen
# before any coverage number was computed, so it cannot have been picked to make
# the sample look good. Changing it invalidates event_sample.txt.
SEED = 20260903

# The date the draw was made, written into the header. It is a CONSTANT and not
# `date.today()` on purpose: with today's date in the header the file changes
# every day the script is run, and "two runs produce a byte-identical file" -
# the whole guarantee this script exists to give - would be false. The date the
# sample was fixed is a fact about the study, not about when someone last
# happened to re-run a script.
DRAW_DATE = "2026-09-03"

# 210, not the 200 the exposé names as its upper bound. Decision D17 scores an
# event only if it produced a usable forecast in BOTH evidence levels, so a
# format failure in either tier costs the event on both sides. A run that starts
# at exactly 800 events finishes below 800 and the thesis then reports a smaller
# sample than the exposé states. 210 x 4 = 840 leaves 40 events (4.8%) of slack;
# the pilot's worst observed structure-level drop rate was ~1.7%.
N_SYMBOLS = 210

# Four quarters, because that is what the exposé commits to ("four quarterly
# reports each over about one year") and it makes the panel balanced: every
# symbol contributes the same number of events, so no company can dominate the
# score by reporting more often.
QUARTERS_PER_SYMBOL = 4

# D9 leakage rule: t0 must fall after the release date of the newest model in
# the comparison, so the answer cannot already be in its weights. The second
# condition, in_index_at_t0 = 1, is point-in-time membership. Both are exactly
# the filter 20_build_evidence_packs.py and 21_metrics.py apply, and the sample
# has to be drawn from the same population the study scores or the three parts
# of the pipeline disagree about which events exist.
ELIGIBLE_AFTER = "2025-04-28"

# Resamples used to put the drawn base rate in context (see `base_rate_context`).
# Seeded off SEED so the printed report is reproducible too.
N_RESAMPLES = 10_000


# --------------------------------------------------------------------------- #
# 1. The population
# --------------------------------------------------------------------------- #
def connect_readonly(db: Path = DB) -> sqlite3.Connection:
    """Open thesis.db read-only. A selection script must not be able to write."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def eligible_events(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every event the study is allowed to use, in a fixed order.

    ORDER BY is not decoration. Rows come back in whatever order SQLite finds
    convenient unless asked otherwise, and every count below - the pool, the
    first four quarters per symbol - is built by walking this list. An unordered
    walk would give a different sample from the same seed.

    Measured on thesis.db (2026-09-03): 2,172 events, 516 distinct symbols.
    """
    return con.execute(
        """SELECT symbol, report_date, t0, sector, market_cap_at_t0, label
             FROM v_level1
            WHERE t0 > ? AND in_index_at_t0 = 1
            ORDER BY symbol, t0, report_date""",
        (ELIGIBLE_AFTER,),
    ).fetchall()


def build_pool(events) -> list[str]:
    """The symbols that can supply four quarters, SORTED.

    The sort is the single most important line in this file and it looks like
    tidiness. `random.sample` walks the sequence it is given, so the result is a
    function of (seed, ORDER). Feed it a set - or anything derived from one -
    and the order depends on Python's per-process string hash randomisation:
    the same seed gives a different sample on every run, and the "reproducible"
    claim in the methods chapter is simply false. `--self-test` demonstrates
    exactly that by running the unsorted draw twice under different
    PYTHONHASHSEED values.

    Belt and braces, deliberately: `eligible_events` already returns rows
    ordered by symbol, so the Counter below is in symbol order and dropping this
    `sorted()` happens not to change today's draw. That is a coincidence of two
    unrelated lines, not a guarantee - change the query's ORDER BY (which no one
    would think of as touching the sample) and the draw moves. Verified by
    mutation: removing this sort alone leaves the artefact byte-identical,
    removing it together with the query's `ORDER BY symbol` does not, and
    changing the query's ORDER BY while this sort stands leaves it identical
    again. Each line is load-bearing on its own.

    Because of that coincidence the sort cannot be pinned by looking at today's
    pool - it is already sorted either way. `--self-test` therefore builds a
    second pool from a SHUFFLED copy of the same rows and requires that one to
    be sorted too, which is the only form of the check that goes red when this
    line is deleted.

    Measured: 445 of the 516 eligible symbols have four or more quarters
    (216 have exactly four, 229 have five). The 71 excluded symbols have one to
    three - companies that joined the index part-way through the window, or left
    it, and so cannot supply a full year.
    """
    per_symbol = Counter(e["symbol"] for e in events)
    return sorted(s for s, n in per_symbol.items() if n >= QUARTERS_PER_SYMBOL)


def draw_symbols(pool: list[str], seed: int = SEED, n: int = N_SYMBOLS) -> list[str]:
    """Draw `n` symbols without replacement, deterministically.

    `random.Random(seed)` rather than the module-level `random.sample`: the
    module-level generator is global state that any import can have already
    drawn from, so the result would depend on what else ran first.
    """
    if len(pool) < n:
        raise SystemExit(f"pool has only {len(pool)} symbols, cannot draw {n}")
    return sorted(random.Random(seed).sample(pool, n))


def first_quarters(events, symbols: list[str]) -> list[sqlite3.Row]:
    """The FIRST `QUARTERS_PER_SYMBOL` eligible events of each drawn symbol.

    First four in date order, not four drawn at random, for two reasons.

    One: it is what the exposé describes. "Four quarterly reports each over
    about one year" is a contiguous run of quarters - a company followed
    through a year - not four quarters picked out of a hat with a gap in the
    middle.

    Two: a random four would be a SECOND random choice on top of the symbol
    draw, and a hidden one. The symbol draw is written down in
    event_sample.txt and can be re-run; a per-symbol quarter draw would have to
    be pinned as well or the sample could not be reproduced, and it would give
    one more knob that someone could suspect had been turned.

    What this costs, stated plainly because it is the one thing a viva can push
    on: 229 of the 445 pool symbols have a fifth eligible quarter, and taking
    the first four drops it. Those fifth quarters fall between 2026-04-22 and
    2026-06-03, a stretch in which only 29.7% of events had a positive five-day
    abnormal return against 46.5% over the whole window, so the rule lifts the
    sample's base rate by about 2.3 percentage points (49.4% over the first four
    quarters of all pool symbols, against 47.2% over all their quarters). The
    rule is not outcome-selected - it was fixed before the labels were looked at
    and applies identically to every symbol - but the shift is real and belongs
    in the methods chapter rather than in a footnote nobody writes.

    Events are ordered by t0 (the reaction day, unique per symbol here), with
    report_date as a tie-break so the order is total and cannot depend on the
    order rows came back in.
    """
    wanted = set(symbols)
    by_symbol: dict[str, list] = defaultdict(list)
    for e in events:
        if e["symbol"] in wanted:
            by_symbol[e["symbol"]].append(e)

    selected = []
    for s in symbols:
        rows = sorted(by_symbol[s], key=lambda r: (r["t0"], r["report_date"]))
        if len(rows) < QUARTERS_PER_SYMBOL:
            raise SystemExit(f"{s} has only {len(rows)} eligible events")
        selected.extend(rows[:QUARTERS_PER_SYMBOL])
    return selected


# --------------------------------------------------------------------------- #
# 2. Coverage arithmetic
# --------------------------------------------------------------------------- #
def _iso_weeks(events) -> set:
    return {date.fromisoformat(e["report_date"]).isocalendar()[:2] for e in events}


def _sector_shares(events) -> dict[str, float]:
    """Share of events per sector, in percent. Two events carry a NULL sector
    (WBA 2025-06-26, SATS 2026-05-11); they are shown as their own bucket rather
    than dropped, because a missing classification is not the absence of a
    company."""
    c = Counter(e["sector"] or "(not classified)" for e in events)
    n = len(events)
    return {k: 100.0 * v / n for k, v in c.items()}


def _caps_bn(events) -> list[float]:
    """Market caps in billions, skipping the events that have none.

    16 of the 2,172 eligible events have no share-count row and so no market
    cap. They are skipped rather than counted as zero - see `_usd_billions` in
    20_build_evidence_packs.py for what treating a missing figure as 0 did to
    the evidence packs.
    """
    return [e["market_cap_at_t0"] / 1e9 for e in events if e["market_cap_at_t0"] is not None]


def base_rate_context(events, pool: list[str], selected) -> dict:
    """Decompose the gap between the drawn base rate and the pool base rate.

    The drawn 840 events are 48.69% positive against 46.50% over all 2,172
    eligible events, and the question a viva will ask is whether that +2.2 point
    gap means the draw is unrepresentative. It does not, and the way to show it
    is to split the gap into the three steps that produced it instead of
    quoting one standard error at it:

        A  all eligible events                        2,172   46.50%
        B  events of the 445 pool symbols             2,009   47.19%   +0.69
        C  first four quarters of those symbols       1,780   49.44%   +2.25
        D  the drawn sample                             840   48.69%   -0.75

    A->B and B->C are STRUCTURAL: they are the pool rule and the first-four rule,
    they are the same for every seed, and B->C is the fifth-quarter effect
    documented in `first_quarters`. Only C->D is the random draw, and C is the
    population the draw is actually from - so C, not A, is what the drawn base
    rate has to be compared against.

    C->D is -0.75 points. The standard deviation of that step is measured here
    by redrawing under the same design, which is the honest way to get it: the
    textbook binomial standard error is wrong twice over, once because 210 of
    445 symbols is a large fraction of a finite pool (which makes it too big)
    and once because events cluster inside a symbol (which would make it too
    small). Both are reported so the size of each correction is visible.
    """
    pool_set = set(pool)
    layer_a = list(events)
    layer_b = [e for e in events if e["symbol"] in pool_set]
    layer_c = first_quarters(events, pool)

    def rate(rows):
        return sum(r["label"] for r in rows) / len(rows)

    rate_c, rate_d = rate(layer_c), rate(selected)

    # Redraw the whole design N_RESAMPLES times. Each symbol contributes a fixed
    # number of positives (its first four labels), so the resample is a sum over
    # symbols - which is precisely what makes it a cluster sample.
    positives = {}
    by_symbol: dict[str, list] = defaultdict(list)
    for e in layer_c:
        by_symbol[e["symbol"]].append(e)
    for s, rows in by_symbol.items():
        positives[s] = sum(r["label"] for r in rows)

    n_events = N_SYMBOLS * QUARTERS_PER_SYMBOL
    rng = random.Random(SEED + 1)          # not SEED: this must not reproduce the draw
    sims = [sum(positives[s] for s in rng.sample(pool, N_SYMBOLS)) / n_events
            for _ in range(N_RESAMPLES)]
    sd = statistics.pstdev(sims)
    naive = (rate_c * (1 - rate_c) / n_events) ** 0.5
    further = sum(1 for x in sims if abs(x - rate_c) >= abs(rate_d - rate_c)) / len(sims)

    return {
        "layers": [("A  all eligible events", layer_a), ("B  events of pool symbols", layer_b),
                   ("C  first four of pool symbols", layer_c), ("D  the drawn sample", selected)],
        "rate_c": rate_c, "rate_d": rate_d, "sd": sd, "naive": naive,
        "z": (rate_d - rate_c) / sd, "further": further,
        "fpc": (1 - N_SYMBOLS / len(pool)) ** 0.5,
    }


# --------------------------------------------------------------------------- #
# 3. The artefact
# --------------------------------------------------------------------------- #
def render_sample(events, pool: list[str], selected) -> str:
    """The text of event_sample.txt.

    Header style follows sp500_index_changes.csv, the project's other
    hand-maintained input: a '#' comment block that says where the rows came
    from and what is known about them, then a column header, then the rows. The
    difference is that this file is GENERATED, so the header says so and gives
    the one command that reproduces it.

    Every number in the header is computed from the data being written, never
    typed in. A hand-typed count is a comment that goes stale silently, and this
    file exists precisely to stop things going stale silently.
    """
    drawn_syms = sorted({e["symbol"] for e in selected})
    pool_shares, drawn_shares = _sector_shares(events), _sector_shares(selected)
    # The unclassified bucket is excluded from "every sector is present" because
    # it is not a sector: it is two events (WBA 2025-06-26, SATS 2026-05-11)
    # whose company record has no sector. Counting it would let the header claim
    # a coverage failure - or, worse, a coverage success - about nothing.
    named = sorted(k for k in pool_shares if k != "(not classified)")
    missing = [k for k in named if k not in drawn_shares]
    worst = max(pool_shares.keys() | drawn_shares.keys(),
                key=lambda k: abs(drawn_shares.get(k, 0.0) - pool_shares.get(k, 0.0)))
    worst_gap = drawn_shares.get(worst, 0.0) - pool_shares.get(worst, 0.0)
    caps_d, caps_p = _caps_bn(selected), _caps_bn(events)
    base_d = sum(e["label"] for e in selected) / len(selected)
    base_p = sum(e["label"] for e in events) / len(events)
    dates = sorted(e["report_date"] for e in selected)

    L = [
        f"# Event sample for the paid run: {len(drawn_syms)} symbols x "
        f"{QUARTERS_PER_SYMBOL} quarters = {len(selected)} events.",
        "#",
        "# GENERATED FILE - do not edit by hand. Reproduce it with",
        "#     python scripts/25_select_event_sample.py",
        "# which rewrites this file byte for byte from thesis.db. If a hand edit and a",
        "# re-run ever disagree, the re-run is right and the edit is lost, which is the",
        "# point: the sample is fixed by the seed below and by nothing else.",
        "#",
        f"# draw date       {DRAW_DATE} (a constant in the script, not the day it last ran)",
        f"# seed            {SEED}",
        f"# eligibility     t0 > {ELIGIBLE_AFTER} AND in_index_at_t0 = 1",
        f"# population      {len(events):,} eligible events, "
        f"{len({e['symbol'] for e in events})} distinct symbols",
        f"# pool            {len(pool)} symbols with {QUARTERS_PER_SYMBOL} or more eligible quarters",
        f"# drawn           {len(drawn_syms)} symbols, sampled without replacement over the SORTED pool",
        f"# events          {len(selected)} - the first {QUARTERS_PER_SYMBOL} eligible quarters of each,"
        " in date order",
        f"# report dates    {dates[0]} .. {dates[-1]}, {len(_iso_weeks(selected))} distinct calendar weeks"
        f" of the population's {len(_iso_weeks(events))}",
        f"# base rate       {100 * base_d:.2f}% positive, against {100 * base_p:.2f}% over all"
        " eligible events",
        "#",
        "# Why 210 symbols and not the 200 the expose names as its upper bound: decision",
        "# D17 counts an event only if it completed in BOTH evidence levels, so a format",
        "# failure costs the event twice. 840 leaves slack to still finish above 800.",
        "#",
        "# Why the draw is random and not stratified by sector or size: it does not need",
        f"# to be. Measured on this draw, all {len(named)} sectors of the population are present"
        + (f" EXCEPT {', '.join(missing)}." if missing else ","),
        f"# the largest deviation from the population is {worst_gap:+.1f} points ({worst}),",
        f"# the market-cap median is {statistics.median(caps_d):,.1f}bn against"
        f" {statistics.median(caps_p):,.1f}bn and the range is",
        f"# {min(caps_d):,.1f}bn-{max(caps_d):,.0f}bn against"
        f" {min(caps_p):,.1f}bn-{max(caps_p):,.0f}bn. So the expose's 'broad range of",
        "# industries and company sizes' is already satisfied, and a random draw leaves no",
        "# selection discretion for anyone to question - there is no cut point, no",
        "# stratum boundary and no tie-break rule that could have been chosen differently.",
        "#",
        "# Sorted by symbol, then report_date, so a diff between two versions of this file",
        "# is readable. The pack builder consumes it with",
        "#     python scripts/20_build_evidence_packs.py --sample event_sample.txt",
        "# which ABORTS if any pair below is not eligible under its own filter.",
        "#",
        "symbol,report_date",
    ]
    L += [f"{e['symbol']},{e['report_date']}"
          for e in sorted(selected, key=lambda r: (r["symbol"], r["report_date"]))]
    return "\n".join(L) + "\n"


def write_sample(text: str, path: Path = OUT) -> None:
    """Write the artefact with explicit LF endings.

    newline="" stops Python translating "\\n" into the platform ending, so the
    bytes on disk are the bytes rendered here on every machine. Without it the
    same draw produces a different FILE on Windows and on Linux, and "two runs
    are byte-identical" would hold only within one operating system.
    sp500_index_changes.csv, the file whose header style this one follows, is
    LF as well.
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


# --------------------------------------------------------------------------- #
# 4. The coverage report - these are the numbers for the methods chapter
# --------------------------------------------------------------------------- #
def coverage_report(events, pool: list[str], selected) -> None:
    drawn_syms = sorted({e["symbol"] for e in selected})
    per = Counter(e["symbol"] for e in selected)
    dates = sorted(e["report_date"] for e in selected)

    print("=" * 78)
    print("EVENT SAMPLE - COVERAGE")
    print("=" * 78)
    print(f"  seed                    {SEED} (fixed in 25_select_event_sample.py)")
    print(f"  eligibility             t0 > {ELIGIBLE_AFTER} AND in_index_at_t0 = 1")
    print(f"  eligible events         {len(events):,}")
    print(f"  distinct symbols        {len({e['symbol'] for e in events})}")
    print(f"  pool (>= {QUARTERS_PER_SYMBOL} quarters)     {len(pool)}")
    print(f"  drawn symbols           {len(drawn_syms)}")
    print(f"  events                  {len(selected)} "
          f"({min(per.values())}-{max(per.values())} per symbol)")
    print(f"  report dates            {dates[0]} .. {dates[-1]}")
    print(f"  calendar weeks          {len(_iso_weeks(selected))} of the population's "
          f"{len(_iso_weeks(events))}")

    ctx = base_rate_context(events, pool, selected)
    print("\n  BASE RATE (share of events with a positive five-day abnormal return)")
    print(f"    {'step':32} {'n':>7}  {'base':>7}   step")
    prev = None
    for label, rows in ctx["layers"]:
        r = sum(x["label"] for x in rows) / len(rows)
        step = "" if prev is None else f"{100 * (r - prev):+6.2f} pts"
        print(f"    {label:32} {len(rows):>7,}  {100 * r:6.2f}%   {step}")
        prev = r
    print("    A->B and B->C are structural (the pool rule and the first-four rule) and are")
    print(f"    the same for every seed. Only C->D is the draw: {100 * (ctx['rate_d'] - ctx['rate_c']):+.2f} points.")
    print(f"    redraw SD of step C->D  {100 * ctx['sd']:.2f} pts  "
          f"({N_RESAMPLES:,} redraws of the same design)")
    print(f"    C->D in SDs             {ctx['z']:+.2f}")
    print(f"    redraws at least this far from C: {100 * ctx['further']:.1f}%")
    print(f"    for comparison, the textbook binomial SE would be {100 * ctx['naive']:.2f} pts;"
          f" it is too")
    print(f"    large here because 210 of {len(pool)} symbols is a big slice of a finite pool"
          f" (fpc {ctx['fpc']:.3f}).")

    print("\n  SECTOR (share of events, drawn against all eligible events)")
    ps, ds = _sector_shares(events), _sector_shares(selected)
    print(f"    {'sector':28} {'population':>10} {'drawn':>8} {'delta':>8}")
    for k in sorted(ps.keys() | ds.keys(), key=lambda k: -ps.get(k, 0.0)):
        print(f"    {k:28} {ps.get(k, 0.0):9.1f}% {ds.get(k, 0.0):7.1f}% "
              f"{ds.get(k, 0.0) - ps.get(k, 0.0):+7.1f}")

    print("\n  MARKET CAP AT t0, billion USD (drawn against all eligible events)")
    cd, cp = _caps_bn(selected), _caps_bn(events)
    print(f"    {'':10} {'median':>10} {'min':>10} {'max':>12} {'n':>7}")
    for name, c in (("population", cp), ("drawn", cd)):
        print(f"    {name:10} {statistics.median(c):10,.1f} {min(c):10,.1f} "
              f"{max(c):12,.0f} {len(c):7,}")
    print(f"    ({len(selected) - len(cd)} drawn and {len(events) - len(cp)} population events"
          " have no share count and are skipped, never counted as zero)")
    print("=" * 78)


# --------------------------------------------------------------------------- #
# 5. Self-test
# --------------------------------------------------------------------------- #
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"\n          {detail}" if detail else ""))
    return condition


def _unsorted_draw_probe() -> None:
    """Print a draw taken over an UNSORTED collection. Called in a subprocess by
    the self-test with two different PYTHONHASHSEED values; see check 3."""
    events = eligible_events(connect_readonly())
    per_symbol = Counter(e["symbol"] for e in events)
    # The one thing that differs from build_pool: no sorted().
    pool_unsorted = list({s for s, n in per_symbol.items() if n >= QUARTERS_PER_SYMBOL})
    print(",".join(random.Random(SEED).sample(pool_unsorted, N_SYMBOLS)[:8]))


def self_test() -> None:
    """Every check states what must be true and then measures it.

    A self-test that only proves the happy path is worthless. Each check here
    corresponds to a rule that can be broken, and each was watched going red
    with the rule broken before it was allowed to go green.
    """
    con = connect_readonly()
    events = eligible_events(con)
    pool = build_pool(events)
    drawn = draw_symbols(pool)
    selected = first_quarters(events, drawn)

    print("\n=== 1. Determinism: the same seed writes the same file ===")
    a = render_sample(events, pool, selected)
    b = render_sample(events, build_pool(eligible_events(con)),
                      first_quarters(events, draw_symbols(build_pool(events))))
    check("two independent runs render byte-identical text",
          a.encode() == b.encode(),
          f"len {len(a)} vs {len(b)}")
    # The header must carry the FIXED draw date. Comparing the header against
    # today's date cannot test this - today happens to be the draw date, so such
    # a test would pass whatever the code did. Read the compiled function
    # instead: if anyone writes date.today() into the renderer, "today" appears
    # in its names and the file starts changing on every run.
    check("the header date is a constant, not date.today()",
          f"draw date       {DRAW_DATE}" in a and "today" not in render_sample.__code__.co_names,
          f"render_sample references {sorted(render_sample.__code__.co_names)}")

    print("\n=== 2. The seed actually drives the draw ===")
    other = draw_symbols(pool, seed=SEED + 1)
    overlap = len(set(drawn) & set(other))
    check("a different seed gives a different set of symbols", set(drawn) != set(other),
          "the 'reproducible because seeded' claim would be empty otherwise")
    # 210 of 445 drawn twice independently overlap by ~210*210/445 = 99 symbols.
    expected = N_SYMBOLS * N_SYMBOLS / len(pool)
    check(f"the overlap between two seeds is about chance ({overlap} vs ~{expected:.0f})",
          abs(overlap - expected) < 4 * (expected ** 0.5),
          f"overlap {overlap}, chance expectation {expected:.1f}")

    print("\n=== 3. Sorting the pool before sampling matters ===")
    # Note what this does and does not catch: because eligible_events already
    # orders by symbol, deleting `sorted()` from build_pool alone leaves the
    # pool sorted and this check green. It goes red the moment the pool is built
    # from a set, or the sort AND the query's ORDER BY both go. See build_pool.
    check("the pool handed to the draw is sorted", pool == sorted(pool),
          "build_pool must not hand random.sample a set-ordered sequence")
    # So take the accident away and test build_pool on its own. Shuffling a copy
    # of the event rows is exactly what a future edit to the query's ORDER BY
    # would do to this function - a change nobody would think of as touching the
    # sample - and the pool must come out sorted anyway, because build_pool
    # sorts it and not because the query happened to. Measured by mutation:
    # dropping the `sorted()` in build_pool leaves the other 22 checks green,
    # including the one directly above, and turns this one red.
    # The shuffle is seeded so that a red here is reproducible rather than a
    # different pool on every run; SEED is reused because the shuffle order has
    # nothing to do with the draw and needs no second constant.
    shuffled = list(events)
    random.Random(SEED).shuffle(shuffled)
    pool_shuffled = build_pool(shuffled)
    check("the pool is sorted even when the event rows arrive unordered",
          pool_shuffled == sorted(pool_shuffled) and pool_shuffled == pool,
          f"first five from shuffled rows {pool_shuffled[:5]}, "
          f"from ordered rows {pool[:5]}")
    reversed_pool = list(reversed(pool))
    check("the same seed over a differently ORDERED pool gives a different sample",
          set(draw_symbols(reversed_pool)) != set(drawn),
          "random.sample is a function of (seed, order), not of the set alone")
    # And the reason the order must not come from a set: string hashing is
    # randomised per process, so a set-ordered pool is a different pool on every
    # run. Two subprocesses, two PYTHONHASHSEEDs, same seed, same data.
    outs = []
    for hashseed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=hashseed)
        outs.append(subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--unsorted-draw-probe"],
            capture_output=True, text=True, env=env, check=True).stdout.strip())
    check("an UNSORTED pool gives a different draw on every process (PYTHONHASHSEED 1 vs 2)",
          outs[0] != outs[1],
          f"PYTHONHASHSEED=1 -> {outs[0][:40]}...\n          PYTHONHASHSEED=2 -> {outs[1][:40]}...")
    print(f"          (sorted pool always starts {','.join(drawn[:8])})")

    print("\n=== 4. Shape of the sample ===")
    # The literals 210, 840 and 4 are written out rather than referred to as
    # N_SYMBOLS etc. A check phrased as `len(drawn) == N_SYMBOLS` cannot fail:
    # it re-states the constant the draw already used. These are the sizes the
    # exposé and the run budget were fixed against, so they are pinned
    # independently and an edit to a constant has to come past them.
    check("exactly 210 symbols", len(set(drawn)) == 210, f"got {len(set(drawn))}")
    check("exactly 840 events", len(selected) == 840, f"got {len(selected)}")
    per = Counter(e["symbol"] for e in selected)
    check("every symbol contributes exactly 4 events", set(per.values()) == {4},
          f"counts seen: {sorted(set(per.values()))}")
    pairs = [(e["symbol"], e["report_date"]) for e in selected]
    check("no (symbol, report_date) pair appears twice", len(set(pairs)) == len(pairs))
    check("every drawn symbol is in the pool", set(drawn) <= set(pool))

    print("\n=== 5. Every drawn event is eligible ===")
    # Re-asked of the database WITHOUT the eligibility filter, so this checks the
    # rows themselves rather than re-running the query that produced them.
    # '2025-04-28' is written out rather than read from ELIGIBLE_AFTER for the
    # same reason as the literals above: the cutoff is the D9 leakage rule, and
    # a check that reads the constant would follow the constant anywhere it was
    # moved to, including somewhere that leaks.
    bad = []
    for sym, rep in pairs:
        r = con.execute("SELECT t0, in_index_at_t0 FROM v_level1 "
                        "WHERE symbol = ? AND report_date = ?", (sym, rep)).fetchone()
        if r is None or r["in_index_at_t0"] != 1 or not r["t0"] > "2025-04-28":
            bad.append((sym, rep))
    check(f"all {len(pairs)} drawn events have in_index_at_t0 = 1 and t0 > 2025-04-28",
          not bad, f"offending pairs: {bad[:5]}")
    check("every drawn event carries a label (nothing unscoreable was drawn)",
          all(e["label"] in (0, 1) for e in selected))
    # The first four, not any four: for every drawn symbol the selected t0s must
    # be the four SMALLEST it has. 104 of the 210 drawn symbols have a fifth
    # eligible quarter, so this check has something to bite on.
    all_t0: dict[str, list[str]] = defaultdict(list)
    for e in events:
        all_t0[e["symbol"]].append(e["t0"])
    got_t0: dict[str, list[str]] = defaultdict(list)
    for e in selected:
        got_t0[e["symbol"]].append(e["t0"])
    wrong = [s for s in drawn
             if sorted(got_t0[s]) != sorted(all_t0[s])[:QUARTERS_PER_SYMBOL]]
    with_five = sum(1 for s in drawn if len(all_t0[s]) > QUARTERS_PER_SYMBOL)
    check(f"each symbol contributes its EARLIEST four quarters "
          f"({with_five} drawn symbols have a fifth that must be excluded)",
          not wrong, f"symbols taking a later quarter: {wrong[:5]}")

    print("\n=== 6. The artefact round-trips through the pack builder's reader ===")
    # 20_build_evidence_packs.py parses this file. Loading its reader here means
    # the two halves cannot drift apart in format without this test failing.
    spec = importlib.util.spec_from_file_location("packs", HERE / "20_build_evidence_packs.py")
    packs = importlib.util.module_from_spec(spec)
    sys.modules["packs"] = packs
    spec.loader.exec_module(packs)
    tmp = HERE.parent / ".event_sample.selftest.txt"
    try:
        write_sample(a, tmp)
        parsed = packs.read_sample(tmp)
        check("the builder reads back exactly the pairs that were written",
              parsed == sorted(pairs), f"{len(parsed)} pairs read, {len(pairs)} written")
        check("the file on disk is byte-identical to a second render",
              tmp.read_bytes() == a.encode("utf-8"))
        check("the file is written with LF endings on every platform",
              b"\r\n" not in tmp.read_bytes())
    finally:
        tmp.unlink(missing_ok=True)

    print("\n=== 7. The builder aborts on an ineligible pair ===")
    rows = [{"symbol": "AAPL", "report_date": "2025-10-30", "t0": "2025-10-31"},
            {"symbol": "MSFT", "report_date": "2025-10-29", "t0": "2025-10-30"}]
    kept = packs.filter_to_sample(rows, [("MSFT", "2025-10-29")])
    check("a listed, eligible pair is kept, an unlisted one dropped",
          [dict(r) for r in kept] == [rows[1]])
    # The rows above hold one quarter per symbol, so they cannot tell a filter
    # that matches on (symbol, report_date) from one that matches on the symbol
    # alone. The real sample has FOUR quarters of every symbol, and a symbol-only
    # match would hand back whichever row it saw last for that symbol: the same
    # 840 events, the same 210 symbols, the wrong quarter in every one of them,
    # and nothing anywhere that says so. Two quarters of MSFT with only the
    # EARLIER one listed, and the later one placed second so that a symbol-keyed
    # lookup would return it. Measured by mutation: keying `available` on
    # r["symbol"] alone leaves every other check in this file and in
    # 20_build_evidence_packs.py's own self-test green, and turns this one red.
    two_quarters = [{"symbol": "MSFT", "report_date": "2025-10-29", "t0": "2025-10-30"},
                    {"symbol": "MSFT", "report_date": "2026-01-27", "t0": "2026-01-28"}]
    picked = packs.filter_to_sample(two_quarters, [("MSFT", "2025-10-29")])
    check("with two quarters of one symbol, the LISTED quarter is built",
          [(r["symbol"], r["report_date"]) for r in picked] == [("MSFT", "2025-10-29")],
          f"got {[(r['symbol'], r['report_date']) for r in picked]}")
    # Observe the abort instead of being killed by it. filter_to_sample carries a
    # tripwire `assert` after its raise, so a filter that stopped aborting would
    # fail this section with an AssertionError escaping into the runner - a
    # traceback and no result line, rather than one clean FAIL - and under
    # `python -O`, which deletes asserts, it would fail differently again.
    # Recording the outcome and letting the check judge it makes the designed
    # path the one that runs, whatever the function does.
    outcome = "returned without aborting"
    try:
        packs.filter_to_sample(rows, [("MSFT", "2025-10-29"), ("HOOD", "2025-04-30")])
    except SystemExit as e:
        outcome = str(e)
    except AssertionError as e:
        outcome = f"AssertionError - the tripwire fired, not the guard: {e}"
    check("an ineligible pair aborts the build", "HOOD,2025-04-30" in outcome,
          f"outcome was: {outcome[:120]}")

    print("\n=== 8. The artefact was drawn by the seed it declares ===")
    # Every check above draws by calling draw_symbols() itself, so none of them
    # ever runs main(). That leaves the one gap that matters most: if main()
    # drew with a seed other than SEED, the header would still print SEED and
    # the committed file would declare a seed that did not produce it - the
    # reproducibility claim false, all other checks green. Measured by mutation:
    # `draw_symbols(pool, seed=20260904)` in main() leaves the other 22 checks
    # passing and writes a sample sharing only 102 of its 210 symbols with the
    # committed one, under a header that still says 20260903.
    #
    # So run the REAL command line, read the seed out of the header IT wrote,
    # and redraw from that declared seed alone. Nothing here is taken from the
    # constants: the seed comes from the file, and only the file's own claim is
    # tested against the file's own contents.
    # --out, not the real path: this writes a throwaway file and never touches
    # event_sample.txt. check=True is deliberately NOT used - a crashing CLI has
    # to arrive here as one red line like any other broken rule, not as a
    # CalledProcessError thrown through the runner.
    name = "the symbols the CLI wrote are the draw of the seed its header declares"
    cli_out = HERE.parent / ".event_sample.cli.selftest.txt"
    try:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                               "--out", str(cli_out)], capture_output=True, text=True)
        if proc.returncode != 0:
            check(name, False, f"the CLI exited {proc.returncode}: "
                               f"{proc.stderr.strip()[-200:]}")
        else:
            lines = cli_out.read_text(encoding="utf-8").splitlines()
            declared = int(next(l.split()[-1] for l in lines if l.startswith("# seed")))
            written = sorted({l.split(",")[0] for l in lines
                              if l and not l.startswith("#") and l != "symbol,report_date"})
            redrawn = draw_symbols(pool, seed=declared)
            check(name, written == redrawn,
                  f"header seed {declared}, {len(written)} symbols written, "
                  f"{len(set(written) & set(redrawn))} of them in that seed's draw")
    finally:
        cli_out.unlink(missing_ok=True)

    print("\n" + "=" * 62)
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"    - {f}")
    print("SELF-TEST", "PASSED" if not FAIL else "FAILED")
    print("=" * 62)
    if FAIL:
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the coverage report but do not write event_sample.txt")
    ap.add_argument("--out", default=str(OUT))
    # Internal, used by the self-test in a subprocess. Not part of the interface.
    ap.add_argument("--unsorted-draw-probe", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.self_test:
        self_test()
        return
    if a.unsorted_draw_probe:
        _unsorted_draw_probe()
        return

    con = connect_readonly()
    events = eligible_events(con)
    pool = build_pool(events)
    drawn = draw_symbols(pool)
    selected = first_quarters(events, drawn)

    coverage_report(events, pool, selected)

    text = render_sample(events, pool, selected)
    if a.dry_run:
        print(f"\n--dry-run: {len(selected)} events NOT written")
        return
    out = Path(a.out)
    write_sample(text, out)
    print(f"\nwrote {len(selected)} events ({len(drawn)} symbols) -> {out}")
    print("build the packs for exactly these events with")
    print(f"  python scripts/20_build_evidence_packs.py --sample {out.name}")


if __name__ == "__main__":
    main()
