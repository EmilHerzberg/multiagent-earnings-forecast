"""Pin the level-1 / level-2 information split. No API calls.

    python scripts/19_define_levels.py

Run last. Creates the views a prompt builder reads from, records which field
belongs to which tier in ``level_assignment``, and reports the resulting prompt
size per tier.

Why this is a script and not a convention
-----------------------------------------
RQ3 compares two information tiers, so *what is in each tier* is a design
parameter of the experiment, not an implementation detail. Written down only as
prose it drifts: a prompt builder adds one more field, and the contrast being
measured quietly changes. Fixing it in the database makes the split auditable
and keeps it identical across every topology and model.

The split
---------
**Level 1 — what the numbers say about this stock.**
The announcement itself plus the stock's own quantitative record: how the market
reacted on the day, how the stock has traded, what it is worth, how fast it is
growing, and what the market as a whole has been doing.

The market block (SPY returns and realised volatility) sits in level 1 rather
than level 2 because the outcome is an *abnormal* return — measured against the
market — so the market's own behaviour is part of reading the stock's move, not
external context. It comes from the same price series as the stock features.

**Level 2 — what the world says on top of that.**
Everything textual and external: news coverage with sentiment and timing,
implied volatility, geopolitical risk, policy uncertainty, rates, oil, inflation,
and which scheduled macro releases fall inside the forecast window.

The boundary is therefore **numeric market record vs. text and external
conditions**, which is a cleaner cut than "company vs. macro" and maps onto a
real question: does unstructured context add anything beyond what the price and
fundamentals already encode?

One deliberate pairing crosses the boundary: level 1 carries *realised* market
volatility, level 2 carries *implied* volatility (VIX). The same quantity,
backward- and forward-looking, split across tiers on purpose.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "level_assignment"

#: (level, source table, field, block, what it is)
ASSIGNMENT = [
    # ── Level 1, block 1: the announcement ────────────────────────
    (1, "companies", "name", "identity", "company name"),
    (1, "companies", "sector", "identity", "GICS-style sector"),
    (1, "companies", "industry", "identity", "industry"),
    (1, "events", "fiscal_date", "announcement", "quarter being reported"),
    (1, "events", "report_date", "announcement", "announcement date"),
    (1, "events", "before_after_market", "announcement", "session of the release"),
    (1, "events", "t0", "announcement", "first trading day that prices it"),
    (1, "events", "eps_actual", "announcement", "reported EPS"),
    (1, "events", "eps_estimate", "announcement", "consensus EPS"),
    (1, "events", "eps_surprise_pct", "announcement", "surprise vs consensus"),
    (1, "events", "close_at_t0", "announcement", "RAW close at t0 (never adjusted)"),
    (1, "events", "market_cap_at_t0", "announcement", "market capitalisation"),
    # ── Level 1, block 2: the reaction ────────────────────────────
    (1, "event_features", "reaction_return", "reaction", "t0 total return"),
    (1, "event_features", "reaction_abnormal", "reaction", "t0 return minus market"),
    (1, "event_features", "volume_ratio", "reaction", "t0 volume vs 20-day median"),
    # ── Level 1, block 3: trailing record ─────────────────────────
    (1, "event_features", "ret_21d", "trailing", "1-month return"),
    (1, "event_features", "ret_63d", "trailing", "3-month return"),
    (1, "event_features", "ret_252d", "trailing", "12-month return"),
    (1, "event_features", "volatility_252d", "trailing", "realised volatility, 1 year"),
    (1, "event_features", "pct_of_52w_range", "trailing", "position in the 52-week range"),
    (1, "event_features", "drawdown_from_252d_high", "trailing", "drawdown from the 1-year high"),
    (1, "event_features", "pe_trailing", "valuation", "trailing P/E"),
    (1, "event_features", "ps_trailing", "valuation", "trailing P/S"),
    (1, "event_features", "revenue_yoy", "valuation", "revenue growth vs year-ago quarter"),
    (1, "event_features", "net_income_yoy", "valuation", "net-income growth vs year-ago quarter"),
    # ── Level 1, block 4: market situation ────────────────────────
    (1, "event_features", "spy_ret_21d", "market", "SPY 1-month return"),
    (1, "event_features", "spy_ret_63d", "market", "SPY 3-month return"),
    (1, "event_features", "spy_volatility_21d", "market", "SPY realised volatility, 21 days"),

    # ── Level 2: news ─────────────────────────────────────────────
    (2, "event_news", "title", "news", "headline"),
    (2, "event_news", "summary", "news", "article summary"),
    (2, "event_news", "published_et", "news", "publication timestamp (US Eastern)"),
    (2, "event_news", "relative_timing", "news", "before / after / around the announcement"),
    (2, "event_news", "sentiment", "news", "ticker-specific sentiment"),
    (2, "event_news", "source", "news", "outlet"),
    # ── Level 2: implied volatility ───────────────────────────────
    (2, "event_regime", "vix", "volatility", "VIX level (implied, forward-looking)"),
    (2, "event_regime", "vix_pctile_5y", "volatility", "VIX percentile, 5 years"),
    # ── Level 2: geopolitics ──────────────────────────────────────
    (2, "event_regime", "gpr_ai", "geopolitics", "AI-GPR index"),
    (2, "event_regime", "gpr_ai_ma30", "geopolitics", "AI-GPR, 30-day mean"),
    (2, "event_regime", "gpr_threats", "geopolitics", "threatened events"),
    (2, "event_regime", "gpr_acts", "geopolitics", "realised events"),
    (2, "event_regime", "gpr_oil", "geopolitics", "oil / energy supply disruption"),
    (2, "event_regime", "gpr_pctile_5y", "geopolitics", "AI-GPR percentile, 5 years"),
    # ── Level 2: policy uncertainty ───────────────────────────────
    (2, "event_regime", "tpu", "policy", "trade policy uncertainty"),
    (2, "event_regime", "tpu_ma7", "policy", "TPU, 7-day mean"),
    (2, "event_regime", "tpu_ma30", "policy", "TPU, 30-day mean"),
    (2, "event_regime", "tpu_pctile_5y", "policy", "TPU percentile, 5 years"),
    (2, "event_regime", "epu_us", "policy", "US economic policy uncertainty"),
    (2, "event_regime", "epu_pctile_5y", "policy", "EPU percentile, 5 years"),
    # ── Level 2: rates, commodities, inflation ────────────────────
    (2, "event_regime", "fed_funds", "macro", "policy rate"),
    (2, "event_regime", "ust_2y", "macro", "2-year Treasury yield"),
    (2, "event_regime", "ust_10y", "macro", "10-year Treasury yield"),
    (2, "event_regime", "yield_curve", "macro", "10y minus 2y; negative = inverted"),
    (2, "event_regime", "wti", "macro", "WTI crude"),
    (2, "event_regime", "brent", "macro", "Brent crude"),
    (2, "event_regime", "cpi", "macro", "CPI, lagged to its publication date"),
    (2, "event_regime", "cpi_observation", "macro", "month the CPI figure refers to"),
    (2, "event_regime", "unemployment", "macro", "unemployment rate, lagged"),
    (2, "event_regime", "retail_sales", "macro", "retail sales, lagged"),
    # ── Level 2: scheduled releases in the window ─────────────────
    (2, "event_macro_overlap", "event_type", "calendar", "FOMC / CPI / NFP / PCE in the window"),
    (2, "event_macro_overlap", "macro_date", "calendar", "date of that release"),
]

SCHEMA = """
DROP TABLE IF EXISTS level_assignment;
CREATE TABLE level_assignment (
    level        INTEGER NOT NULL,   -- 1 or 2
    source_table TEXT NOT NULL,
    field        TEXT NOT NULL,
    block        TEXT NOT NULL,      -- grouping used when rendering the prompt
    description  TEXT,
    PRIMARY KEY (source_table, field)
);

-- Everything level 1 needs, one row per event.
DROP VIEW IF EXISTS v_level1;
CREATE VIEW v_level1 AS
SELECT e.symbol, e.fiscal_date, e.report_date, e.before_after_market, e.t0,
       c.name, c.sector, c.industry,
       e.eps_actual, e.eps_estimate, e.eps_surprise_pct,
       e.close_at_t0, e.market_cap_at_t0,
       f.reaction_return, f.reaction_abnormal, f.volume_ratio,
       f.ret_21d, f.ret_63d, f.ret_252d, f.volatility_252d,
       f.pct_of_52w_range, f.drawdown_from_252d_high,
       f.pe_trailing, f.ps_trailing, f.revenue_yoy, f.net_income_yoy,
       f.spy_ret_21d, f.spy_ret_63d, f.spy_volatility_21d,
       e.split_after_t0, e.has_fundamentals, e.in_index_at_t0,
       e.label, e.abnormal_return
  FROM events e
  JOIN companies c USING (symbol)
  LEFT JOIN event_features f USING (symbol, fiscal_date);

-- The level-2 additions that are one-per-event. News and calendar overlaps are
-- one-to-many and stay in their own tables.
DROP VIEW IF EXISTS v_level2_regime;
CREATE VIEW v_level2_regime AS SELECT * FROM event_regime;
"""


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    args = ap.parse_args()

    con = config.connect()
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO level_assignment VALUES (?,?,?,?,?)", ASSIGNMENT)
    con.execute("DELETE FROM provenance WHERE table_name = ?", (TABLE,))
    config.record_provenance(
        con, TABLE, "study design", "19_define_levels.py", len(ASSIGNMENT),
        "Pre-registered level-1 / level-2 field assignment. Level 1 = the announcement "
        "plus the stock's quantitative record including market context; level 2 adds "
        "news, implied volatility, geopolitical risk, policy uncertainty, rates, oil, "
        "inflation and scheduled macro releases.")
    for k, v in {
        "level1_definition": (
            "The announcement plus the stock's own quantitative record: identity, EPS "
            "actual/estimate/surprise, raw close and market cap at t0, the t0 reaction, "
            "trailing returns and volatility, valuation and growth, and market-wide SPY "
            "returns and realised volatility. Numeric market record only."),
        "level2_definition": (
            "Level 1 plus everything textual and external: news with sentiment and "
            "pre/post-announcement timing, VIX, AI-GPR geopolitical risk, trade and "
            "economic policy uncertainty, rates and the yield curve, oil, lagged "
            "inflation and labour data, and scheduled macro releases inside the "
            "forecast window."),
        "level_boundary_rationale": (
            "Numeric market record vs. text and external conditions. Realised volatility "
            "is level 1, implied volatility (VIX) is level 2 — deliberately."),
    }.items():
        con.execute("INSERT OR REPLACE INTO study_meta (key, value) VALUES (?,?)", (k, v))
    con.commit()

    print("[19] level definition")
    for lvl in (1, 2):
        rows = con.execute(
            "SELECT block, COUNT(*) FROM level_assignment WHERE level=? GROUP BY 1 ORDER BY 1",
            (lvl,)).fetchall()
        total = sum(c for _, c in rows)
        print(f"\n  Level {lvl}: {total} fields")
        for block, c in rows:
            print(f"    {block:14s} {c:2d}")

    n1 = con.execute("SELECT COUNT(*) FROM v_level1").fetchone()[0]
    print(f"\n  v_level1        : {n1:,} rows (one per event)")
    print(f"  v_level2_regime : "
          f"{con.execute('SELECT COUNT(*) FROM v_level2_regime').fetchone()[0]:,} rows")
    print(f"  event_news      : "
          f"{con.execute('SELECT COUNT(*) FROM event_news').fetchone()[0]:,} items "
          f"(one-to-many)")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
