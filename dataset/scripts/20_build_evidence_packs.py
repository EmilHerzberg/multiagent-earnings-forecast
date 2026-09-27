"""Build the level-1 and level-2 evidence packs that the agents read.

The prompt scaffold (TypeScript) needs plain text, so this exports one JSON file
that the runner loads. Keeping the SQL here and the prompts there preserves the
separation the prompt session established: everything a model reads is either
evidence (this file) or instruction (`pilot/prompts.ts`), never a mixture.

Level assignment is NOT decided here. It is read from `level_assignment`, which
is the table the prompt session pinned so a field cannot drift between tiers
without that table changing.

    python scripts/20_build_evidence_packs.py --sample event_sample.txt
    python scripts/20_build_evidence_packs.py --symbol MSFT --report-date 2025-10-29
    python scripts/20_build_evidence_packs.py --eligible-after 2025-04-28 --limit 200
    python scripts/20_build_evidence_packs.py --self-test

`--sample` is how the paid run is built: it restricts the packs to exactly the
(symbol, report_date) pairs that 25_select_event_sample.py drew and pinned in
event_sample.txt. Without it the builder behaves as it always has and exports
every eligible event.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "thesis.db"
OUT = Path(__file__).resolve().parents[1] / "scaffold" / "pilot5" / "evidence_packs.json"


# --------------------------------------------------------------------------- #
# The fixed event sample (see 25_select_event_sample.py)
# --------------------------------------------------------------------------- #
def read_sample(path) -> list[tuple[str, str]]:
    """Parse event_sample.txt into (symbol, report_date) pairs.

    The format is deliberately the dullest thing that works: '#' comments, one
    'symbol,report_date' column header, then one pair per line. The parser is
    here rather than imported from 25_select_event_sample.py so that building
    the packs - the step the paid run depends on - needs nothing but this file
    and the database. The two are kept honest by 25's self-test, which writes a
    sample with 25 and reads it back with THIS function and compares.

    Every rejection below is a refusal to guess. A line this parser cannot
    understand is a line whose event would silently vanish from the sample, and
    a sample that quietly shrinks is exactly the failure mode --sample exists to
    prevent.
    """
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"sample file not found: {path}")
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line == "symbol,report_date":
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise SystemExit(f"{path}:{lineno}: expected 'SYMBOL,report_date', got {raw!r}")
        pair = (parts[0], parts[1])
        if pair in seen:
            # A duplicate would build the same pack twice and be scored twice,
            # which silently weights that event double.
            raise SystemExit(f"{path}:{lineno}: {pair[0]},{pair[1]} listed twice")
        seen.add(pair)
        pairs.append(pair)
    if not pairs:
        raise SystemExit(f"{path}: no (symbol, report_date) rows found")
    return pairs


def filter_to_sample(rows, pairs: list[tuple[str, str]]):
    """Keep exactly `pairs` out of `rows`, or ABORT.

    `rows` is the result of this script's own eligibility query, so a pair that
    is not in it is a pair this script would not build. The alternative -
    skipping it - is what must never happen: the sample is sized at 840 events
    with deliberate slack for the D17 both-levels rule, and a builder that
    quietly drops three of them spends that slack before the run even starts,
    without anybody being told. Whatever is wrong (a stale sample file, a
    changed eligibility cutoff, a re-scanned index membership) has to be fixed
    or re-drawn, not absorbed.

    Order is by t0, the same as the unfiltered build, so --sample changes WHICH
    packs are exported and nothing else. event_sample.txt is sorted by symbol
    for readable diffs; that is a property of the file, not of the packs.
    """
    available = {(r["symbol"], r["report_date"]): r for r in rows}
    missing = [p for p in pairs if p not in available]
    if missing:
        shown = "\n  ".join(f"{s},{d}" for s, d in missing[:20])
        more = f"\n  ... and {len(missing) - 20} more" if len(missing) > 20 else ""
        raise SystemExit(
            f"{len(missing)} of {len(pairs)} sampled events are not eligible under this "
            f"builder's own filter (t0 > cutoff AND in_index_at_t0 = 1):\n  "
            f"{shown}{more}\n"
            "Refusing to build a smaller sample than the one that was drawn. Either the "
            "sample file is stale (re-run scripts/25_select_event_sample.py) or the "
            "eligibility rule changed under it (then the draw has to be redone and "
            "re-pinned, not silently trimmed)."
        )
    kept = [available[p] for p in pairs]
    kept.sort(key=lambda r: (r["t0"], r["symbol"]))
    # Unreachable as the function stands - `missing` is empty by the line above -
    # and kept anyway as a tripwire for the next edit to this function. The count
    # going in and the count coming out is the whole contract, and an `assert`
    # here costs nothing. It is not the guard, though: the guard is the raise
    # above, because `python -O` deletes asserts and would delete this one.
    assert len(kept) == len(pairs), f"{len(pairs)} sampled pairs, {len(kept)} rows kept"
    return kept


def _fmt(v, unit="", nd=2):
    # The None branch is the point of this function, not an afterthought. A
    # missing figure must reach the model as "not available", because "we do not
    # know" and "we measured exactly zero" are different pieces of evidence and
    # the model cannot tell them apart once they render identically. Six callers
    # used to defeat this branch by pre-coercing with `(x or 0)`; that fabricated
    # "net income year on year 0%" in 179 of the 2,267 eligible packs and
    # "market capitalisation 0.00 billion USD" for 19 S&P 500 constituents. Never
    # substitute a number for an unknown before calling this - pass the None on.
    if v is None:
        return "not available"
    if isinstance(v, float):
        return f"{v:,.{nd}f}{unit}"
    return f"{v}{unit}"


def _pct(v, nd=2):
    """Render a FRACTION as a percentage: 0.6423 -> "64.23%".

    Everything ratio-shaped in `event_features` is stored as a fraction, never
    as an already-multiplied percentage, so every one of them needs the same
    x100. `pct_of_52w_range` was the one field in the price-history sentence
    missing it: rendered as `_fmt(0.6423, "%", 0)` it became "64%"'s evil twin
    "1%", and across all 2,267 eligible events the sentence took exactly two
    values, "0%" (922 events) and "1%" (1,345). That is not merely a lost
    decimal - it inverts the meaning. A stock at 91% of its 52-week range, i.e.
    near its one-year high, was described to the model as sitting at "1% of its
    52-week range", i.e. at the low, in the same sentence that told it the stock
    was 3% off that high. Doing the scaling in one helper means a ratio field
    cannot be added later without it.

    None is passed through untouched so `_fmt`'s "not available" branch fires.
    float() is deliberate: `_fmt` prints ints without decimals, so a genuine
    zero would read "0%" while a coerced missing value read "0.00%" (or the
    reverse) - two different renderings for two different meanings, but not
    reliably the right way round. Forcing float makes a real zero always "0.00%"
    and leaves "not available" as the only spelling of unknown.
    """
    return _fmt(None if v is None else float(v) * 100, "%", nd)


def _pctile(v):
    """Render a percentile STORED AS A FRACTION: 0.9803 -> "98th".

    18_derive_event_regime.py computes the four *_pctile_5y fields as
    count/len over the last five years, so they are fractions in [0,1]
    (measured on thesis.db: vix 0.149-1.0, gpr 0.160-0.995, tpu 0.437-1.0,
    epu 0.0005-0.998). They were rendered with `_fmt(v, "th", 0)`, which prints
    the raw fraction to zero decimals and glues "th percentile" on, so every
    value under 0.5 read "0th percentile" and everything else "1th percentile" -
    four fields, two distinct strings, 2,267 events.

    That flips the sign of the signal. Trade-policy uncertainty at the 98th
    percentile - about as tense as the last five years ever got - was handed to
    the model as the "1th percentile", i.e. historically calm. These four fields
    exist only in level 2, so this corrupted precisely the tier whose extra
    value RQ3 exists to measure.

    round() rather than a format spec so the result is an int and no stray
    decimals appear next to the ordinal; the "th" suffix is left as it was
    (100th, 76th - the code has never inflected ordinals and that is cosmetic).
    """
    return _fmt(None if v is None else round(float(v) * 100), "th")


def _usd_billions(v):
    """Market cap in billions, keeping a missing share count missing.

    19 eligible events have no share-count row. `(v or 0) / 1e9` reported those
    as "market capitalisation 0.00 billion USD" - a zero-dollar S&P 500
    constituent, an outlier implausible enough that the model will visibly
    react to it.
    """
    return _fmt(None if v is None else float(v) / 1e9, " billion USD")


def level1_text(r: sqlite3.Row) -> str:
    """The numeric market record. One block per level_assignment 'block'."""
    L = []
    L.append(f"COMPANY: {r['name']} ({r['symbol']}), {r['sector']} / {r['industry']}.")
    L.append(
        f"ANNOUNCEMENT: results for the quarter ending {r['fiscal_date']}, "
        f"released on {r['report_date']} {r['before_after_market']}. "
        f"The prediction is made after the close of {r['t0']}."
    )
    L.append(
        "EARNINGS SURPRISE: reported EPS "
        f"{_fmt(r['eps_actual'])} against a consensus of {_fmt(r['eps_estimate'])}"
        + (f", a surprise of {_fmt(r['eps_surprise_pct'], '%', 1)}." if r["eps_surprise_pct"] is not None else ".")
    )
    L.append(
        "REACTION ON THE FIRST TRADING DAY: the stock returned "
        f"{_pct(r['reaction_return'])} and "
        f"{_pct(r['reaction_abnormal'])} relative to the market; "
        f"volume was {_fmt(r['volume_ratio'], 'x')} its recent average. "
        f"Closing price {_fmt(r['close_at_t0'])}; market capitalisation "
        f"{_usd_billions(r['market_cap_at_t0'])}."
    )
    L.append(
        "PRICE HISTORY: return over the last 21 trading days "
        f"{_pct(r['ret_21d'])}, over 63 days {_pct(r['ret_63d'])}, "
        f"over 252 days {_pct(r['ret_252d'])}. "
        f"Annualised volatility {_pct(r['volatility_252d'])}. "
        # Zero decimals here is intentional - "sits at 64% of its 52-week range"
        # is as precise as that number deserves to be read - but it must be the
        # scaled percentage, not the raw fraction. See _pct.
        f"The stock sits at {_pct(r['pct_of_52w_range'], 0)} of its 52-week range, "
        f"{_pct(r['drawdown_from_252d_high'])} from its 252-day high."
    )
    L.append(
        "VALUATION AND GROWTH: trailing P/E {}, trailing P/S {}, revenue year on year {}, "
        "net income year on year {}.".format(
            _fmt(r["pe_trailing"]), _fmt(r["ps_trailing"]),
            _pct(r["revenue_yoy"]), _pct(r["net_income_yoy"]),
        )
    )
    L.append(
        "MARKET CONTEXT: the S&P 500 returned "
        f"{_pct(r['spy_ret_21d'])} over 21 days and "
        f"{_pct(r['spy_ret_63d'])} over 63 days, with 21-day volatility "
        f"{_pct(r['spy_volatility_21d'])}."
    )
    return "\n\n".join(L)


def level2_text(con: sqlite3.Connection, r: sqlite3.Row) -> str:
    """Level 1 plus what the level split assigns to tier 2: news and world conditions."""
    L = [level1_text(r), ""]

    news = con.execute(
        """SELECT published_et, relative_timing, title, summary, source, sentiment_label
           FROM event_news WHERE symbol=? AND fiscal_date=?
           ORDER BY published_et""",
        (r["symbol"], r["fiscal_date"]),
    ).fetchall()
    if news:
        L.append(f"NEWS ({len(news)} items published before the prediction time):")
        for n in news:
            head = f"  [{n['published_et']}, {n['relative_timing']}, {n['source']}, sentiment {n['sentiment_label']}]"
            body = (n["summary"] or "").strip().replace("\n", " ")
            L.append(f"{head}\n  {n['title'].strip()}\n  {body}")
        L.append("")

    g = con.execute(
        "SELECT * FROM v_level2_regime WHERE symbol=? AND fiscal_date=?",
        (r["symbol"], r["fiscal_date"]),
    ).fetchone()
    if g:
        L.append(
            "MARKET VOLATILITY: VIX {} ({} percentile of the last five years).".format(
                _fmt(g["vix"]), _pctile(g["vix_pctile_5y"])
            )
        )
        L.append(
            "GEOPOLITICAL RISK: AI-GPR index {} (30-day average {}, {} percentile of five years); "
            "threats {}, acts {}, oil-related {}.".format(
                _fmt(g["gpr_ai"]), _fmt(g["gpr_ai_ma30"]), _pctile(g["gpr_pctile_5y"]),
                _fmt(g["gpr_threats"]), _fmt(g["gpr_acts"]), _fmt(g["gpr_oil"]),
            )
        )
        L.append(
            "POLICY UNCERTAINTY: trade-policy uncertainty {} (7-day {}, 30-day {}, {} percentile); "
            "US economic policy uncertainty {} ({} percentile).".format(
                _fmt(g["tpu"]), _fmt(g["tpu_ma7"]), _fmt(g["tpu_ma30"]), _pctile(g["tpu_pctile_5y"]),
                _fmt(g["epu_us"]), _pctile(g["epu_pctile_5y"]),
            )
        )
        L.append(
            "MACRO CONDITIONS: fed funds rate {}%, 2-year Treasury {}%, 10-year Treasury {}%, "
            "yield curve (10y minus 2y) {} percentage points. WTI crude {} USD, Brent {} USD. "
            "CPI {} (as of {}), unemployment {}%, retail sales {}.".format(
                _fmt(g["fed_funds"]), _fmt(g["ust_2y"]), _fmt(g["ust_10y"]), _fmt(g["yield_curve"]),
                _fmt(g["wti"]), _fmt(g["brent"]), _fmt(g["cpi"]), g["cpi_observation"],
                _fmt(g["unemployment"]), _fmt(g["retail_sales"]),
            )
        )

    # The 'calendar' block. 19_define_levels.py pins exactly two fields of
    # event_macro_overlap into level 2 - event_type and macro_date - and
    # study_meta.level2_definition ends "...and scheduled macro releases inside
    # the forecast window". That table exists so a prompt builder cannot quietly
    # change what a tier contains; the drift happened anyway, in the direction
    # nobody watches for: the block was simply never written, so a pinned field
    # was missing from 100% of packs while 1,455 of the 2,267 eligible events
    # (64%) actually have an FOMC / CPI / NFP / PCE release inside their window.
    #
    # Rendered from the two pinned fields ONLY. days_after_t0 sits in the same
    # table and would be easy to add, but it is not in level_assignment, and
    # adding it here is the exact failure this block is being restored to undo.
    # event_type is printed as stored ("FOMC_DECISION"), not prettified, so the
    # pack and the pre-registration table can be diffed literally.
    #
    # This is not lookahead. These dates are published months in advance by the
    # Fed / BLS / BEA (15_macro_event_calendar.py), so a forecaster standing at
    # t0 knows them; what happened AT the release is not in this table.
    cal = con.execute(
        """SELECT event_type, macro_date FROM event_macro_overlap
           WHERE symbol=? AND fiscal_date=? ORDER BY macro_date, event_type""",
        (r["symbol"], r["fiscal_date"]),
    ).fetchall()
    if cal:
        L.append(
            "SCHEDULED MACRO RELEASES INSIDE THE FORECAST WINDOW: "
            + ", ".join(f"{c['event_type']} on {c['macro_date']}" for c in cal)
            + "."
        )
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Regression tests. Every expected string below is derived from the source data
# by hand - a percentile fraction times 100, a NULL that must stay unknown - and
# NOT from what this file happens to print, because all four bugs these guard
# against printed something perfectly plausible.
_L1_COLS = [
    "name", "symbol", "sector", "industry", "fiscal_date", "report_date",
    "before_after_market", "t0", "eps_actual", "eps_estimate", "eps_surprise_pct",
    "reaction_return", "reaction_abnormal", "volume_ratio", "close_at_t0",
    "market_cap_at_t0", "ret_21d", "ret_63d", "ret_252d", "volatility_252d",
    "pct_of_52w_range", "drawdown_from_252d_high", "pe_trailing", "ps_trailing",
    "revenue_yoy", "net_income_yoy", "spy_ret_21d", "spy_ret_63d",
    "spy_volatility_21d", "label", "abnormal_return",
]
_G_COLS = [
    "symbol", "fiscal_date", "vix", "vix_pctile_5y", "gpr_ai", "gpr_ai_ma30",
    "gpr_pctile_5y", "gpr_threats", "gpr_acts", "gpr_oil", "tpu", "tpu_ma7",
    "tpu_ma30", "tpu_pctile_5y", "epu_us", "epu_pctile_5y", "fed_funds", "ust_2y",
    "ust_10y", "yield_curve", "wti", "brent", "cpi", "cpi_observation",
    "unemployment", "retail_sales",
]


def _fixture(level1=None, regime=None, overlaps=()):
    """An in-memory stand-in for the real views, so the tests need no thesis.db."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE ev (%s)" % ",".join(_L1_COLS))
    v = dict.fromkeys(_L1_COLS, 0.0)
    v.update(name="Microsoft Corporation", symbol="MSFT", sector="Technology",
             industry="Software", fiscal_date="2025-03-31", report_date="2025-04-30",
             before_after_market="AfterMarket", t0="2025-05-01",
             label=1, abnormal_return=0.1234567)
    v.update(level1 or {})
    con.execute("INSERT INTO ev VALUES (%s)" % ",".join("?" * len(_L1_COLS)),
                [v[c] for c in _L1_COLS])
    con.execute("CREATE TABLE event_news (symbol, fiscal_date, published_et, "
                "relative_timing, title, summary, source, sentiment_label)")
    con.execute("CREATE TABLE v_level2_regime (%s)" % ",".join(_G_COLS))
    if regime is not None:
        g = dict.fromkeys(_G_COLS, 0.0)
        g.update(symbol="MSFT", fiscal_date="2025-03-31", cpi_observation="2025-03-01")
        g.update(regime)
        con.execute("INSERT INTO v_level2_regime VALUES (%s)" % ",".join("?" * len(_G_COLS)),
                    [g[c] for c in _G_COLS])
    con.execute("CREATE TABLE event_macro_overlap (symbol, fiscal_date, macro_date, "
                "event_type, days_after_t0)")
    for o in overlaps:
        con.execute("INSERT INTO event_macro_overlap VALUES (?,?,?,?,?)", o)
    return con


def self_test() -> None:
    """Check the rendered text against values worked out from the data by hand."""
    ok = True

    def want(label, needle, text, present=True):
        nonlocal ok
        hit = needle in text
        if hit is not present:
            ok = False
            print(f"  FAIL {label}: expected {'' if present else 'NO '}{needle!r}")
        return hit

    # --- the four *_pctile_5y fields are fractions in [0,1] -------------------
    # 18_derive_event_regime.py computes them as count/len over five years.
    # Real MSFT event, fiscal_date 2025-03-31, t0 2025-05-01: the DB holds
    # 0.7608 / 0.8643 / 0.9803 / 0.9984, so times 100 and rounded these are the
    # 76th, 86th, 98th and 100th percentile. Before the fix all four printed
    # "1th percentile", which reads as "calmer than almost any day in five
    # years" - the opposite of what three of them mean.
    con = _fixture(regime=dict(vix=24.6,
                               vix_pctile_5y=0.7607505863956215,
                               gpr_pctile_5y=0.8642583470169677,
                               tpu_pctile_5y=0.9802955665024631,
                               epu_pctile_5y=0.9983579638752053))
    t2 = level2_text(con, con.execute("SELECT * FROM ev").fetchone())
    for field, expect in [("vix", "76th percentile"), ("gpr", "86th percentile"),
                          ("tpu", "98th percentile"), ("epu", "100th percentile")]:
        want(f"{field}_pctile_5y", expect, t2)
    # Also pin the FULL list of percentiles the pack renders, in order. A
    # substring test alone would not have caught the original bug's signature -
    # four fields collapsing onto two strings - so read back every ordinal that
    # precedes the word "percentile" and compare against the whole expected list.
    words = t2.replace("(", " ").replace(")", " ").replace(";", " ").replace(".", " ").split()
    got = [words[i - 1] for i, w in enumerate(words) if w == "percentile" and i]
    if got != ["76th", "86th", "98th", "100th"]:
        ok = False
        print(f"  FAIL percentiles rendered: expected ['76th', '86th', '98th', '100th'], got {got}")
    print("  percentile fractions render as 76th/86th/98th/100th: ok")

    # A low percentile must stay low, not round to "0th". The observed VIX
    # minimum in thesis.db is 0.14910 -> the 15th percentile.
    con = _fixture(regime=dict(vix_pctile_5y=0.14910226385636222))
    want("vix at the observed 5-year minimum", "15th percentile",
         level2_text(con, con.execute("SELECT * FROM ev").fetchone()))
    print("  a low percentile (0.1491) renders 15th, not 0th: ok")

    # --- pct_of_52w_range is a fraction too ----------------------------------
    # 11_derive_context_features.py documents it as "0 = at the 52-week low,
    # 1 = at the high". 0.6423 is 64% of the way up the range; 0.91359 (the real
    # MSFT 2025-10-29 value) is 91%, which has to agree with the drawdown clause
    # in the same sentence saying the stock is 3% off its one-year high.
    for frac, expect in [(0.6422809783574042, "sits at 64% of its 52-week range"),
                         (0.91359, "sits at 91% of its 52-week range")]:
        con = _fixture(level1=dict(pct_of_52w_range=frac))
        want(f"pct_of_52w_range={frac}", expect,
             level1_text(con.execute("SELECT * FROM ev").fetchone()))
    print("  pct_of_52w_range 0.6423 -> 64%, 0.9136 -> 91%: ok")

    # --- unknown must not be rendered as zero --------------------------------
    # A company with no prior-year income statement has UNKNOWN, not flat,
    # earnings growth; a company with no share count has unknown, not zero,
    # market capitalisation. pe_trailing was always rendered honestly and is the
    # control: the fix makes its five neighbours behave the same way.
    con = _fixture(level1=dict(market_cap_at_t0=None, revenue_yoy=None,
                               net_income_yoy=None, ret_252d=None,
                               volatility_252d=None, pe_trailing=None))
    t1 = level1_text(con.execute("SELECT * FROM ev").fetchone())
    for label, expect in [
        ("market_cap_at_t0", "market capitalisation not available"),
        ("net_income_yoy", "net income year on year not available"),
        ("revenue_yoy", "revenue year on year not available"),
        ("ret_252d", "over 252 days not available"),
        ("volatility_252d", "Annualised volatility not available"),
        ("pe_trailing (control)", "trailing P/E not available"),
    ]:
        want(label, expect, t1)
    for fabricated in ("market capitalisation 0.00 billion USD",
                       "net income year on year 0%",
                       "revenue year on year 0%", "over 252 days 0%"):
        want("fabricated zero", fabricated, t1, present=False)
    print("  NULL renders as 'not available', never as 0: ok")

    # A measured zero is still a measurement and must print as a number. This is
    # the other half of the same fix: before it, `(None or 0)` produced an int
    # and printed "0%" while a real 0.0 printed "0.00%", so the two were told
    # apart by a formatting accident rather than by meaning.
    con = _fixture(level1=dict(market_cap_at_t0=0.0, net_income_yoy=0.0, ret_252d=0.0))
    t1 = level1_text(con.execute("SELECT * FROM ev").fetchone())
    for label, expect in [("market cap 0", "market capitalisation 0.00 billion USD"),
                          ("net_income_yoy 0", "net income year on year 0.00%"),
                          ("ret_252d 0", "over 252 days 0.00%")]:
        want(label, expect, t1)
    print("  a genuine zero still renders as a number: ok")

    # --- the pre-registered calendar block -----------------------------------
    # level_assignment pins (2, event_macro_overlap, event_type) and
    # (2, event_macro_overlap, macro_date). Real ACGL 2025-03-31, t0 2025-04-30:
    # NFP on 2025-05-02 and the FOMC decision on 2025-05-07, both inside the
    # five-day window. Sorted by date, so NFP comes first.
    con = _fixture(overlaps=[("MSFT", "2025-03-31", "2025-05-07", "FOMC_DECISION", 7),
                             ("MSFT", "2025-03-31", "2025-05-02", "NFP", 2)])
    t2 = level2_text(con, con.execute("SELECT * FROM ev").fetchone())
    want("calendar block", "SCHEDULED MACRO RELEASES INSIDE THE FORECAST WINDOW: "
         "NFP on 2025-05-02, FOMC_DECISION on 2025-05-07.", t2)
    # days_after_t0 is in the same table but NOT in level_assignment. Rendering
    # it would be exactly the silent tier-widening the level split exists to stop.
    want("unpinned field leaked", "days_after_t0", t2, present=False)
    want("unpinned field leaked", "t0+", t2, present=False)
    print("  calendar block renders both pinned fields, in date order: ok")

    con = _fixture()
    want("no overlaps -> no block", "SCHEDULED MACRO RELEASES",
         level2_text(con, con.execute("SELECT * FROM ev").fetchone()), present=False)
    print("  an event with no release in its window gets no calendar block: ok")

    # --- structural invariants -----------------------------------------------
    # RQ3 compares level 2 against level 1, so level 2 must be level 1 plus
    # additions and never a rewording of it, or the contrast measures two
    # differences at once.
    con = _fixture(regime=dict(vix=24.6, vix_pctile_5y=0.76),
                   overlaps=[("MSFT", "2025-03-31", "2025-05-02", "NFP", 2)])
    r = con.execute("SELECT * FROM ev").fetchone()
    t1, t2 = level1_text(r), level2_text(con, r)
    if t1 not in t2:
        ok = False
        print("  FAIL: level 2 is not a strict superset of level 1")
    if len(t2) <= len(t1):
        ok = False
        print("  FAIL: level 2 adds nothing")
    print("  level 2 contains level 1 verbatim and adds to it: ok")

    # The outcome must never reach the model. The fixture carries label=1 and
    # abnormal_return=0.1234567 (which would render as 12.35%) in the row object
    # precisely so that anyone who later adds a convenient r['...'] to a
    # sentence trips this.
    for leak in ("0.1234567", "12.35%", "abnormal_return", "label"):
        want("outcome leaked into the pack", leak, t2, present=False)
    print("  neither the label nor the realised return appears in the text: ok")

    # --- the --sample filter -------------------------------------------------
    # The rule under test: a listed pair that is not eligible must ABORT the
    # build. Skipping it would silently shrink the 840-event sample that was
    # drawn with only 40 events of slack for the D17 both-levels rule.
    import tempfile

    rows = [{"symbol": "AAPL", "report_date": "2025-10-30", "t0": "2025-10-31"},
            {"symbol": "MSFT", "report_date": "2025-10-29", "t0": "2025-10-30"},
            {"symbol": "KO", "report_date": "2025-10-14", "t0": "2025-10-14"}]

    kept = filter_to_sample(rows, [("MSFT", "2025-10-29"), ("KO", "2025-10-14")])
    if [r["symbol"] for r in kept] != ["KO", "MSFT"]:
        ok = False
        print(f"  FAIL: --sample must keep exactly the listed pairs in t0 order, got "
              f"{[r['symbol'] for r in kept]}")
    print("  a sample of eligible pairs is kept, in t0 order: ok")

    # The rows above hold one quarter per symbol, so they cannot tell a filter
    # keyed on (symbol, report_date) from one keyed on the symbol alone. The real
    # sample is 210 symbols x FOUR quarters, and a symbol-only match would return
    # whichever row of that symbol it saw last: 840 packs with the right ids and
    # the wrong quarter inside every one of them - same count, same symbols,
    # different evidence, and no error anywhere. Two MSFT quarters with only the
    # earlier one listed, the later one placed second so a symbol-keyed lookup
    # would win with it. (Verified by mutation: keying `available` on
    # r["symbol"] alone passes every other check in this file and in
    # 25_select_event_sample.py's self-test, and fails this one.)
    two_quarters = [{"symbol": "MSFT", "report_date": "2025-10-29", "t0": "2025-10-30"},
                    {"symbol": "MSFT", "report_date": "2026-01-27", "t0": "2026-01-28"}]
    picked = filter_to_sample(two_quarters, [("MSFT", "2025-10-29")])
    if [(r["symbol"], r["report_date"]) for r in picked] != [("MSFT", "2025-10-29")]:
        ok = False
        print(f"  FAIL: a symbol with two quarters must build the LISTED quarter, got "
              f"{[(r['symbol'], r['report_date']) for r in picked]}")
    else:
        print("  the quarter listed in the sample is the quarter built, not just the "
              "symbol: ok")

    # The outcome is recorded and judged once, rather than the except clause
    # carrying the verdict. A defect that lets a bad pair through trips the
    # tripwire assert inside filter_to_sample before reaching this code, and an
    # uncaught AssertionError ends the run with a traceback and no summary line
    # - a failure that looks like a crash instead of a named result, and that
    # disappears entirely under `python -O`, which deletes asserts. Catching it
    # here gives one red line in both modes. Same shape as section 7 of
    # 25_select_event_sample.py.
    try:
        # HOOD reported on 2025-04-30 but did not join the index until
        # 2025-09-22, so it is not eligible - a real example of the mistake.
        filter_to_sample(rows, [("MSFT", "2025-10-29"), ("HOOD", "2025-04-30")])
        outcome = "returned without aborting"
    except SystemExit as e:
        outcome = str(e)
    except AssertionError as e:
        outcome = f"AssertionError - the tripwire fired, not the guard: {e}"
    if "HOOD,2025-04-30" not in outcome:
        ok = False
        print(f"  FAIL: an ineligible pair must abort the build and be named; "
              f"outcome was: {outcome[:120]}")
    else:
        print("  an ineligible pair aborts the build and is named: ok")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sample.txt"
        p.write_text("# a header comment\n#\nsymbol,report_date\nMSFT,2025-10-29\n"
                     "\nKO,2025-10-14\n", encoding="utf-8")
        got = read_sample(p)
        if got != [("MSFT", "2025-10-29"), ("KO", "2025-10-14")]:
            ok = False
            print(f"  FAIL: read_sample dropped or mangled rows: {got}")
        print("  comments, the column header and blank lines are skipped: ok")

        # Each malformed file carries a GOOD row as well as the bad one. Without
        # it a parser that merely skipped the bad line would still raise - on
        # "no rows found" - and this test would pass while the file silently
        # lost an event, which is the failure it is here to catch. (Verified by
        # mutation: turning the raise below into `continue` survives the
        # one-row version of this test and is caught by this one.)
        good = "KO,2025-10-14\n"
        for bad, why in [("symbol,report_date\nMSFT\n" + good, "a line with no comma"),
                         ("symbol,report_date\nMSFT,2025-10-29,extra\n" + good, "a third field"),
                         ("symbol,report_date\n,2025-10-29\n" + good, "an empty symbol"),
                         ("symbol,report_date\nMSFT,2025-10-29\nMSFT,2025-10-29\n" + good,
                          "a duplicated pair"),
                         ("# only comments\n", "no rows at all")]:
            p.write_text(bad, encoding="utf-8")
            try:
                read_sample(p)
                ok = False
                print(f"  FAIL: {why} was accepted instead of refused")
            except SystemExit:
                pass
        print("  a malformed, duplicated or empty sample file is refused, not guessed: ok")

    print("\nSELF-TEST", "PASSED" if ok else "FAILED")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--symbol")
    ap.add_argument("--report-date")
    ap.add_argument("--eligible-after", default="2025-04-28",
                    help="D9 eligibility: t0 must be after the binding model release date")
    ap.add_argument("--sample", "--symbols", dest="sample", metavar="PATH",
                    help="restrict the build to the (symbol, report_date) pairs in "
                         "event_sample.txt; aborts if any of them is not eligible")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    if a.self_test:
        self_test()
        return

    # --sample is exhaustive by construction, so anything that could shrink it
    # is refused rather than combined. --limit with --sample would build the
    # first N of the drawn events and print a cheerful count; --symbol would
    # narrow it to one company. Both produce a run that is not the sample that
    # was drawn and pinned, which is the one thing this flag exists to prevent.
    if a.sample and (a.symbol or a.report_date or a.limit):
        raise SystemExit("--sample builds exactly the drawn sample and cannot be combined "
                         "with --symbol / --report-date / --limit")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    # Two conditions, and the second one is easy to forget because it looks like
    # a detail. `t0 > ?` is the leakage rule: only events after the newest model
    # was released, so the answer cannot be sitting in its weights.
    #
    # `in_index_at_t0 = 1` is point-in-time membership, and it is here because
    # `21_metrics.py` scores on it. Until 2026-08-30 this script did NOT apply it
    # and rendered 2,267 packs where the analysis scored 2,209 - the two halves
    # of the study disagreed about which events belong to it.
    #
    # The 58 extra events were companies that joined the index LATER: Robinhood
    # reported on 2025-04-30 and joined on 2025-09-22, Coinbase joined 2025-05-19,
    # Datadog 2025-07-09. Including them means selecting an April 2025 event
    # because of something nobody knew until September - hindsight leaking into
    # the sample rather than into the prompt.
    #
    # Note this is NOT the same as the 530-symbol universe, which deliberately
    # keeps every company that was ever a member (`study_meta.constituent_basis`)
    # so that dropouts are not silently discarded. That guards survivorship bias
    # at the universe level; this guards it per event.
    where = ["t0 > ?", "in_index_at_t0 = 1"]
    args: list = [a.eligible_after]
    if a.symbol:
        where.append("symbol = ?"); args.append(a.symbol)
    if a.report_date:
        where.append("report_date = ?"); args.append(a.report_date)
    sql = f"SELECT * FROM v_level1 WHERE {' AND '.join(where)} ORDER BY t0"
    if a.limit:
        sql += f" LIMIT {a.limit}"
    rows = con.execute(sql, args).fetchall()
    if not rows:
        # Name the filter, because the most likely cause of an empty result on a
        # specific --symbol is that the company was not an index member yet.
        raise SystemExit(
            f"no events matched (t0 > {a.eligible_after}, in_index_at_t0 = 1"
            + (f", symbol = {a.symbol}" if a.symbol else "")
            + (f", report_date = {a.report_date}" if a.report_date else "")
            + ")")

    if a.sample:
        pairs = read_sample(a.sample)
        rows = filter_to_sample(rows, pairs)
        print(f"sample {a.sample}: {len(pairs)} events, "
              f"{len({s for s, _ in pairs})} symbols - all eligible")

    packs = []
    for r in rows:
        packs.append({
            "id": f"{r['symbol']}_{r['report_date']}",
            "symbol": r["symbol"],
            "reportDate": r["report_date"],
            "t0": r["t0"],
            "label": r["label"],                       # ground truth, NEVER shown to a model
            "abnormalReturn": r["abnormal_return"],
            "level1": level1_text(r),
            "level2": level2_text(con, r),
        })

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(packs, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(packs)} evidence packs -> {out}")
    for p in packs[:3]:
        print(f"  {p['id']}: level1 {len(p['level1']):,} chars, level2 {len(p['level2']):,} chars, label={p['label']}")


if __name__ == "__main__":
    main()
