"""Validate the assembled database and print a coverage report.

    python scripts/07_validate.py

Checks structural invariants (keys, ranges, orphans, non-positive prices),
reports field completeness per table, and independently recomputes the EPS
surprise as a cross-check of the provider value.  Exits non-zero if a hard
invariant is violated.  Makes no API calls.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

failures: list[str] = []
warnings: list[str] = []

# ── how many tickers an S&P 500 should hold on any given day ────────
#
# 503, not 500. The index tracks 500 COMPANIES, but a few of them list two
# share classes and the index counts each line separately — GOOG and GOOGL,
# FOX and FOXA, NWS and NWSA. The published ticker count has sat at 503 for
# most of this study window and has ranged 503-505 in recent years.
EXPECTED_MEMBER_TICKERS = 503

# Tolerance, in tickers, on that count for any single day of the window.
#
# The reconstruction in `index_membership` is not the index itself, so the band
# has to absorb three honest sources of slack:
#   * the base list approximates membership at the START of the window, so the
#     reconstruction opens at 502 rather than 503;
#   * an addition and the removal it pairs with are sometimes logged a day or
#     two apart, which over- or under-counts in between;
#   * a company can leave without the change log noticing (PARA, August 2025),
#     permanently costing one member until a matching addition is found.
# Measured on the current build the count runs 500..502, so +/-8 leaves 5
# tickers of headroom below and 9 above.
#
# Why not wider: this check exists because `build_index_membership()` once
# compared the end of each price series against `config.END_DATE` instead of
# against the last date in `prices_daily`. The price backfill stopped 19 days
# short of the window, so every removal was re-dated to the end of the data and
# the index only ever grew — 499 min, 519 max, and 36 events from companies
# that had already left counted as index events. A band of +/-16 would have
# swallowed that silently, which is the definition of a check that catches
# nothing. +/-8 rejects it on 172 of the 546 days.
#
# Why not tighter: the S&P 500 has held 500 companies since 1976, but the
# ticker count is not frozen, and a legitimate multi-class listing or a
# corrected base list must not turn this red.
MEMBER_COUNT_TOLERANCE = 8


def check(condition: bool, message: str, hard: bool = True) -> None:
    if not condition:
        (failures if hard else warnings).append(message)


def section(title: str) -> None:
    print(f"\n{'-' * 68}\n{title}\n{'-' * 68}")


def main() -> int:
    con = config.connect(readonly=True)
    q = lambda sql, *a: con.execute(sql, a).fetchall()
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]

    start = one("SELECT value FROM study_meta WHERE key='study_window_start'")
    end = one("SELECT value FROM study_meta WHERE key='study_window_end'")

    section("Study design")
    n_comp = one("SELECT COUNT(*) FROM companies")
    print(f"  window       : {start} .. {end}")
    print(f"  constituents : {n_comp}")

    section("Table sizes")
    for t in ("companies", "earnings", "prices_daily", "income_statement",
              "market_benchmark", "shares_outstanding", "index_membership", "events",
              "event_features", "event_news", "event_news_summary", "macro_series",
              "geopolitical_risk", "uncertainty_indices", "vix", "event_regime",
              "macro_event_calendar", "event_macro_overlap",
              "provenance", "data_exclusions"):
        exists = one("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", t)
        if exists:
            print(f"  {t:22s} {one(f'SELECT COUNT(*) FROM {t}'):>9,}")
        else:
            print(f"  {t:22s} {'-':>9}")

    # ── companies ──────────────────────────────────────────────────
    section("companies")
    for col in ("name", "sector", "industry", "isin", "cik"):
        c = one(f"SELECT COUNT({col}) FROM companies")
        print(f"    {col:20s} {c:>6,} / {n_comp:,}  ({100 * c / n_comp:.1f}%)")

    # ── earnings ───────────────────────────────────────────────────
    section("earnings")
    n = one("SELECT COUNT(*) FROM earnings")
    syms = one("SELECT COUNT(DISTINCT symbol) FROM earnings")
    print(f"  rows {n:,} across {syms} symbols")
    for col in ("eps_actual", "eps_estimate", "eps_surprise_pct",
                "before_after_market", "report_date"):
        c = one(f"SELECT COUNT({col}) FROM earnings")
        print(f"    {col:20s} {c:>6,} / {n:,}  ({100 * c / n:.1f}%)")

    check(one("SELECT COUNT(*) FROM earnings WHERE fiscal_date IS NULL") == 0,
          "earnings has NULL fiscal_date")
    check(one("""SELECT COUNT(*) FROM earnings e LEFT JOIN companies c USING(symbol)
                  WHERE c.symbol IS NULL""") == 0,
          "earnings references unknown symbols")
    check(one("SELECT COUNT(*) FROM earnings WHERE report_date NOT BETWEEN ? AND ?",
              start, end) == 0,
          "earnings rows outside the study window", hard=False)

    dev = one("""SELECT COUNT(*) FROM earnings
                  WHERE eps_actual IS NOT NULL AND eps_estimate IS NOT NULL
                    AND eps_estimate != 0 AND eps_surprise_pct IS NOT NULL
                    AND ABS(eps_surprise_pct
                            - (eps_actual - eps_estimate)/ABS(eps_estimate)*100) > 1.0""")
    checked = one("""SELECT COUNT(*) FROM earnings
                      WHERE eps_actual IS NOT NULL AND eps_estimate IS NOT NULL
                        AND eps_estimate != 0 AND eps_surprise_pct IS NOT NULL""")
    print(f"  surprise cross-check : {dev} of {checked} rows deviate >1pp")
    check(dev == 0, f"{dev} surprise values disagree with the recomputation", hard=False)

    print("  reports per symbol:")
    for cnt, k in q("""SELECT n, COUNT(*) FROM
                       (SELECT symbol, COUNT(*) n FROM earnings GROUP BY symbol)
                       GROUP BY n ORDER BY n"""):
        print(f"    {cnt:2d} reports : {k:3d} symbols")

    # ── prices ─────────────────────────────────────────────────────
    section("prices_daily")
    n = one("SELECT COUNT(*) FROM prices_daily")
    syms = one("SELECT COUNT(DISTINCT symbol) FROM prices_daily")
    d0 = one("SELECT MIN(date) FROM prices_daily")
    d1 = one("SELECT MAX(date) FROM prices_daily")
    print(f"  rows {n:,} across {syms} symbols, {d0} .. {d1}")

    check(one("SELECT COUNT(*) FROM prices_daily WHERE adj_close IS NULL OR adj_close <= 0") == 0,
          "prices_daily contains non-positive adjusted closes")
    check(one("""SELECT COUNT(*) FROM prices_daily
                  WHERE high IS NOT NULL AND low IS NOT NULL AND high < low""") == 0,
          "bars with high < low")
    check(d1 <= end, f"prices extend past the study window ({d1})")
    if d1 < end:
        warnings.append(f"prices end {d1}, before the window end {end} — the final "
                        f"stretch of the window has no price data")

    rows = q("SELECT symbol, COUNT(*) n FROM prices_daily GROUP BY symbol ORDER BY n")
    print(f"  bars per symbol: min {rows[0][1]} ({rows[0][0]}) | "
          f"median {rows[len(rows)//2][1]} | max {rows[-1][1]} ({rows[-1][0]})")
    short = [r for r in rows if r[1] < 300]
    print(f"  {len(short)} symbols with <300 bars (delisted/acquired during the window):")
    for sym, cnt in short:
        print(f"    {sym:6s} {cnt:3d} bars, last "
              f"{one('SELECT MAX(date) FROM prices_daily WHERE symbol=?', sym)}")

    # ── income statement ───────────────────────────────────────────
    section("income_statement")
    n = one("SELECT COUNT(*) FROM income_statement")
    if n:
        syms = one("SELECT COUNT(DISTINCT symbol) FROM income_statement")
        print(f"  rows {n:,} across {syms} symbols")
        for col in ("revenue", "net_income", "operating_income", "ebit",
                    "ebitda", "gross_profit"):
            c = one(f"SELECT COUNT({col}) FROM income_statement")
            print(f"    {col:20s} {c:>6,} / {n:,}  ({100 * c / n:.1f}%)")
        gap = n_comp - syms
        if gap:
            warnings.append(
                f"income_statement covers {syms} of {n_comp} constituents — the {gap} "
                f"missing are delisted names the provider drops entirely (survivorship; "
                f"use a LEFT JOIN, see README 7.8)")
    else:
        warnings.append("income_statement is empty — run 06_alphavantage_income_statement.py")

    # ── linkage ────────────────────────────────────────────────────
    section("Cross-table linkage")
    tot = one("SELECT COUNT(*) FROM earnings WHERE report_date IS NOT NULL")
    linked = one("""SELECT COUNT(*) FROM earnings e
                     WHERE EXISTS (SELECT 1 FROM prices_daily p
                                    WHERE p.symbol=e.symbol AND p.date >= e.report_date)""")
    print(f"  announcements with a price bar on/after report_date : {linked:,} / {tot:,}")
    aligned = one("""SELECT COUNT(*) FROM earnings e
                      JOIN income_statement i
                        ON i.symbol=e.symbol AND i.fiscal_date=e.fiscal_date""")
    pct = 100 * aligned / tot if tot else 0
    print(f"  earnings rows with a matching income_statement period: {aligned:,} / {tot:,} ({pct:.1f}%)")
    if n and pct < 90:
        warnings.append(
            f"only {pct:.1f}% of earnings rows align with an income_statement period on "
            f"fiscal_date — providers differ on non-calendar fiscal years; join on nearest "
            f"period end within a tolerance rather than dropping rows")

    # ── market benchmark ───────────────────────────────────────────
    section("market_benchmark")
    n = one("SELECT COUNT(*) FROM market_benchmark")
    if n:
        sym = one("SELECT symbol FROM market_benchmark LIMIT 1")
        d0 = one("SELECT MIN(date) FROM market_benchmark")
        d1 = one("SELECT MAX(date) FROM market_benchmark")
        print(f"  {sym}: {n:,} bars, {d0} .. {d1}")
        check(one("SELECT COUNT(*) FROM market_benchmark WHERE adj_close IS NULL OR adj_close<=0") == 0,
              "market_benchmark has non-positive adjusted closes")
    else:
        warnings.append("market_benchmark is empty — run 07_alphavantage_market_benchmark.py")

    # ── index membership ───────────────────────────────────────────
    section("index_membership")
    mem = q("SELECT symbol, in_index_from, in_index_to, from_source, to_source "
            "FROM index_membership")
    if not mem:
        warnings.append("index_membership is empty — run 09_build_events.py")
    else:
        from collections import Counter as _Counter
        print(f"  {len(mem)} symbols")
        print("    in_index_from : " + ", ".join(
            f"{k}={v}" for k, v in sorted(_Counter(r[3] for r in mem).items())))
        print("    in_index_to   : " + ", ".join(
            f"{k or 'still a member'}={v}"
            for k, v in sorted(_Counter(r[4] for r in mem).items(), key=lambda x: str(x[0]))))
        check(all(r[1] is None or r[2] is None or r[1] < r[2] for r in mem),
              "a company's in_index_to is not after its in_index_from")

        # Reconstruct how many tickers the index held on each day of the window.
        # This catches a SYSTEMATIC pattern of wrong dates, not an isolated one.
        # Everything else here validates shapes and keys, which a wrong date
        # passes cleanly: the row is well formed, it just says the wrong thing.
        # The count is the only quantity with a known right answer to compare
        # against (~503), and a systematic error shows up in it as drift —
        # additions landing on their real dates while removals pile up
        # somewhere else.
        #
        # Do not over-trust it. Measured: replacing 4 of the 23 verified removal
        # dates with later price-implied ones moves the daily count by a single
        # ticker, well inside the tolerance below, and this check stays green.
        # The per-symbol check that follows it exists for exactly that gap.
        # See build_index_membership() in 09_build_events.py.
        import datetime as _dt
        _d = _dt.date.fromisoformat
        day, last_day, daily = _d(start), _d(end), []
        while day <= last_day:
            iso = day.isoformat()
            daily.append((iso, sum(1 for _s, f, t, _fs, _ts in mem
                                   if (f is None or iso >= f) and (t is None or iso < t))))
            day += _dt.timedelta(days=1)
        ns = sorted(c for _, c in daily)
        lo = EXPECTED_MEMBER_TICKERS - MEMBER_COUNT_TOLERANCE
        hi = EXPECTED_MEMBER_TICKERS + MEMBER_COUNT_TOLERANCE
        outside = [(x, c) for x, c in daily if not lo <= c <= hi]
        print(f"  daily member count over {len(daily)} days : min {ns[0]} | "
              f"median {ns[len(ns) // 2]} | max {ns[-1]}   (a real S&P 500 holds "
              f"~{EXPECTED_MEMBER_TICKERS})")
        print(f"  days outside {lo}..{hi} : {len(outside)}")
        detail = ""
        if outside:
            worst = max(outside, key=lambda x: abs(x[1] - EXPECTED_MEMBER_TICKERS))
            detail = f"; worst {worst[0]} with {worst[1]}"
            print(f"    first {outside[0][0]} with {outside[0][1]}, worst {worst[0]} "
                  f"with {worst[1]}")
        check(not outside,
              f"the reconstructed index is outside {lo}..{hi} tickers on {len(outside)} of "
              f"{len(daily)} days{detail} — one or more membership dates is wrong "
              f"(see build_index_membership() in 09_build_events.py)")
        # A verified removal date must never be pushed LATER than the change
        # log says. Earlier is allowed and expected — a company can stop trading
        # before the index committee acts, and then the price series is the
        # better answer — but later means a confirmed date was discarded, and
        # every day of the difference wrongly counts the company as a member.
        #
        # This is the gap the band check above cannot see, because a handful of
        # symbols moves the daily count by about one ticker. It is written
        # against the CSV rather than a hard-coded list so it keeps working as
        # more dates get verified.
        import csv as _csv
        changes_path = config.THESIS_DIR / "sp500_index_changes.csv"
        late = []
        if changes_path.exists():
            to_by_sym = {r[0]: r[2] for r in mem}
            for row in _csv.DictReader(
                    line for line in changes_path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")):
                if row["action"] != "remove" or row["confidence"] != "verified":
                    continue
                built = to_by_sym.get(row["symbol"])
                if built is not None and built > row["effective_date"]:
                    late.append((row["symbol"], row["effective_date"], built))
            print(f"  verified removal dates honoured : "
                  f"{sum(1 for _ in late) and 'NO' or 'yes'} ({len(late)} pushed later)")
            for sym, want, got in late[:5]:
                print(f"    {sym}: change log says {want}, index_membership says {got}")
        check(not late,
              f"{len(late)} verified removal date(s) were overridden by a LATER date — "
              f"a confirmed date must never be discarded "
              f"(see build_index_membership() in 09_build_events.py)")

        drift = daily[-1][1] - daily[0][1]
        print(f"  net change first day to last : {drift:+d}")
        check(abs(drift) <= MEMBER_COUNT_TOLERANCE,
              f"the reconstructed index drifts {drift:+d} tickers across the window — "
              f"additions and removals are not balancing", hard=False)

    # ── events ─────────────────────────────────────────────────────
    section("events")
    n = one("SELECT COUNT(*) FROM events")
    if not n:
        warnings.append("events is empty — run 09_build_events.py")
    else:
        syms = one("SELECT COUNT(DISTINCT symbol) FROM events")
        h = one("SELECT DISTINCT horizon_days FROM events")
        print(f"  {n:,} events across {syms} symbols, horizon {h} trading days")

        # Hard invariants on the event construction.
        check(one("SELECT COUNT(*) FROM events WHERE t0 < report_date") == 0,
              "events with t0 before report_date")
        check(one("SELECT COUNT(*) FROM events WHERE window_start <= t0") == 0,
              "events whose window starts on or before t0 (t0 must be excluded)")
        check(one("SELECT COUNT(*) FROM events WHERE window_end <= window_start") == 0,
              "events with a non-increasing window")
        check(one("""SELECT COUNT(*) FROM events
                      WHERE label != (CASE WHEN abnormal_return > 0 THEN 1 ELSE 0 END)""") == 0,
              "label disagrees with the sign of abnormal_return")
        check(one("""SELECT COUNT(*) FROM events
                      WHERE ABS(abnormal_return - (stock_return - market_return)) > 1e-9""") == 0,
              "abnormal_return is not stock_return - market_return")
        # AfterMarket must react strictly after report_date; the others on it.
        check(one("""SELECT COUNT(*) FROM events
                      WHERE before_after_market = 'AfterMarket' AND t0 <= report_date""") == 0,
              "AfterMarket events whose t0 is not after report_date")
        check(one("""SELECT COUNT(*) FROM events
                      WHERE before_after_market IN ('BeforeMarket','DuringMarket')
                        AND t0 < report_date""") == 0,
              "Before/DuringMarket events with t0 before report_date")
        # Look-ahead guard: the share count must predate t0.
        check(one("""SELECT COUNT(*) FROM events
                      WHERE shares_fiscal_date IS NOT NULL AND shares_fiscal_date >= t0""") == 0,
              "market cap uses a share count from a quarter not yet ended at t0")

        n_idx = one("SELECT COUNT(*) FROM events WHERE in_index_at_t0 = 1")
        print(f"  in index at t0 (point-in-time sample) : {n_idx:,}/{n:,} "
              f"({100*n_idx/n:.1f}%)")
        for label_, where in [("all events   ", ""),
                              ("in-index only", " WHERE in_index_at_t0 = 1")]:
            t = one(f"SELECT COUNT(*) FROM events{where}")
            p = one(f"SELECT SUM(label) FROM events{where}")
            if t:
                print(f"    base rate {label_} {p}/{t} = {100*p/t:.2f}%"
                      f"   Brier {(p/t)*(1-p/t):.4f}")

        # How much could a wrong membership date still matter? Only events close
        # to a boundary can flip, so this bounds the residual risk from the
        # unverified effective dates.
        near = one("""SELECT COUNT(*) FROM events e
                       JOIN index_membership m ON m.symbol = e.symbol
                      WHERE (m.in_index_from IS NOT NULL
                             AND ABS(JULIANDAY(e.t0) - JULIANDAY(m.in_index_from)) <= 30)
                         OR (m.in_index_to IS NOT NULL
                             AND ABS(JULIANDAY(e.t0) - JULIANDAY(m.in_index_to)) <= 30)""")
        unverified = one("""SELECT COUNT(*) FROM events e
                             JOIN index_membership m ON m.symbol = e.symbol
                            WHERE (m.from_source = 'aggregator'
                                   AND ABS(JULIANDAY(e.t0)-JULIANDAY(m.in_index_from)) <= 30)
                               OR (m.to_source = 'aggregator'
                                   AND ABS(JULIANDAY(e.t0)-JULIANDAY(m.in_index_to)) <= 30)""")
        print(f"  events within 30d of a membership boundary : {near}"
              f"  (of which {unverified} rest on an unverified date)")
        if unverified:
            warnings.append(
                f"{unverified} events sit within 30 days of a membership date that was "
                f"not confirmed against an S&P press release — their inclusion could flip "
                f"if the date is wrong (see README 7.1)")

        for col, label_ in [("market_cap_at_t0", "market cap"),
                            ("close_at_t0", "raw close at t0"),
                            ("eps_surprise_pct", "surprise")]:
            c = one(f"SELECT COUNT({col}) FROM events")
            print(f"  {label_:22s} {c:,}/{n:,} ({100*c/n:.1f}%)")
        print(f"  has_fundamentals       {one('SELECT SUM(has_fundamentals) FROM events'):,}/{n:,}")
        print(f"  split_after_t0 (hide)  {one('SELECT SUM(split_after_t0) FROM events'):,}")
        print(f"  split_in_lookback      {one('SELECT SUM(split_in_lookback) FROM events'):,}")

        print("  events per symbol:")
        rows = q("SELECT symbol, COUNT(*) c FROM events GROUP BY symbol")
        from collections import Counter
        for k, v in sorted(Counter(c for _, c in rows).items()):
            print(f"    {k} events : {v:3d} symbols")
        print(f"  symbols with >=4 events : {sum(1 for _, c in rows if c >= 4)}")

    # ── level-2 context ────────────────────────────────────────────
    def table_exists(name):
        return bool(one("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", name))

    n_ev = one("SELECT COUNT(*) FROM events") or 1

    if table_exists("event_features"):
        section("event_features (level 2)")
        nf = one("SELECT COUNT(*) FROM event_features")
        print(f"  {nf:,} rows")
        check(nf == n_ev, f"event_features has {nf} rows for {n_ev} events", hard=False)
        cols = [d[1] for d in con.execute("PRAGMA table_info(event_features)")][3:]
        thin = []
        for c in cols:
            k = one(f"SELECT COUNT({c}) FROM event_features")
            pct = 100 * k / nf if nf else 0
            print(f"    {c:26s} {k:5d}/{nf} ({pct:5.1f}%)")
            if pct < 85:
                thin.append(f"{c} {pct:.0f}%")
        if thin:
            warnings.append("thin context features: " + ", ".join(thin))
        # No feature may be computed from data after t0.
        check(one("SELECT COUNT(*) FROM event_features f JOIN events e USING(symbol, fiscal_date) "
                  "WHERE f.t0 != e.t0") == 0,
              "event_features.t0 disagrees with events.t0")

    if table_exists("event_news_summary"):
        section("event_news (level 2)")
        ne = one("SELECT COUNT(*) FROM event_news_summary")
        ni = one("SELECT COUNT(*) FROM event_news")
        empty = one("SELECT COUNT(*) FROM event_news_summary WHERE items_stored = 0")
        trunc = one("SELECT COUNT(*) FROM event_news_summary WHERE items_returned > items_stored")
        print(f"  {ne:,}/{n_ev:,} events queried | {ni:,} items | {empty} with no relevant news")
        print(f"  truncated by the per-event cap: {trunc} ({100 * trunc / ne if ne else 0:.1f}%)")
        for t, c in q("SELECT relative_timing, COUNT(*) FROM event_news GROUP BY 1 ORDER BY 2 DESC"):
            print(f"    {t:20s} {c:6,} ({100 * c / ni if ni else 0:.1f}%)")
        # Hard invariant: nothing published after t0 may be present.
        late = one("""SELECT COUNT(*) FROM event_news n JOIN events e USING(symbol, fiscal_date)
                       WHERE SUBSTR(n.published_et, 1, 10) > e.t0""")
        check(late == 0, f"{late} news items are published after t0 — point-in-time cut breached")
        print(f"  items published after t0 (must be 0): {late}")

    if table_exists("macro_series"):
        section("macro_series (level 2)")
        for sid, cnt, lag, lo, hi in q("""SELECT series_id, COUNT(*), publication_lag_days,
                                                 MIN(observation_date), MAX(observation_date)
                                            FROM macro_series GROUP BY 1 ORDER BY 1"""):
            print(f"    {sid:14s} {cnt:5d} obs  lag {lag:2d}d  {lo} .. {hi}")
        check(one("SELECT COUNT(*) FROM macro_series WHERE public_from < observation_date") == 0,
              "macro_series has public_from before its observation date")

    if table_exists("geopolitical_risk"):
        section("geopolitical_risk (level 2)")
        cnt, lo, hi = con.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM geopolitical_risk").fetchone()
        print(f"  {cnt:,} days, {lo} .. {hi}")
        gap = one("""SELECT COUNT(*) FROM events e
                      WHERE NOT EXISTS (SELECT 1 FROM geopolitical_risk g WHERE g.date = e.t0)""")
        print(f"  events whose t0 has no GPR reading: {gap}")
        if gap > n_ev * 0.05:
            warnings.append(f"{gap} events have no GPR value at t0")

    if table_exists("event_regime"):
        section("event_regime (level 2)")
        nr = one("SELECT COUNT(*) FROM event_regime")
        print(f"  {nr:,} rows")
        check(nr == n_ev, f"event_regime has {nr} rows for {n_ev} events", hard=False)
        cols = [d[1] for d in con.execute("PRAGMA table_info(event_regime)")][3:]
        thin = [c for c in cols
                if (one(f"SELECT COUNT({c}) FROM event_regime") / nr if nr else 0) < 0.95]
        filled = sum(1 for c in cols
                     if (one(f"SELECT COUNT({c}) FROM event_regime") / nr if nr else 0) >= 0.95)
        print(f"  {filled}/{len(cols)} indicators at >=95% coverage")
        if thin:
            warnings.append("thin regime indicators: " + ", ".join(thin))
        # Lagged releases must never be dated on or after t0.
        check(one("""SELECT COUNT(*) FROM event_regime
                      WHERE cpi_observation IS NOT NULL AND cpi_observation >= t0""") == 0,
              "event_regime uses a CPI observation dated on or after t0")
        r = con.execute("""SELECT ROUND(AVG(vix),1), ROUND(AVG(gpr_ai),1), ROUND(AVG(tpu_ma30),1),
                                  ROUND(AVG(epu_us),1), ROUND(AVG(yield_curve),2),
                                  ROUND(AVG(wti),1) FROM event_regime""").fetchone()
        print(f"  means across events: VIX {r[0]} | GPR_AI {r[1]} | TPU30 {r[2]} | "
              f"EPU {r[3]} | curve {r[4]} | WTI {r[5]}")
        inv = one("SELECT COUNT(*) FROM event_regime WHERE yield_curve < 0")
        print(f"  events with an inverted yield curve: {inv} ({100 * inv / nr:.1f}%)")

    if table_exists("event_macro_overlap"):
        section("macro releases in the forecast window (level 2)")
        hit = one("SELECT COUNT(DISTINCT symbol || fiscal_date) FROM event_macro_overlap")
        print(f"  events with >=1 scheduled release inside the window: "
              f"{hit:,}/{n_ev:,} ({100 * hit / n_ev:.1f}%)")
        for t, c in q("SELECT event_type, COUNT(*) FROM event_macro_overlap GROUP BY 1 ORDER BY 2 DESC"):
            print(f"    {t:15s} {c}")
        check(one("""SELECT COUNT(*) FROM event_macro_overlap o JOIN events e USING(symbol, fiscal_date)
                      WHERE o.macro_date < e.window_start OR o.macro_date > e.window_end""") == 0,
              "event_macro_overlap contains releases outside the forecast window")

    section("Exclusions")
    for reason, cnt in q("SELECT reason, COUNT(*) FROM data_exclusions GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {reason:34s} {cnt}")

    section("Provenance")
    for r in q("SELECT table_name, source_api, endpoint, rows_written FROM provenance ORDER BY id"):
        print(f"  {r[0]:18s} {r[1]:15s} {r[3]:>8,}  {r[2]}")

    section("Result")
    for w in warnings:
        print(f"  NOTE  {w}")
    for f in failures:
        print(f"  FAIL  {f}")
    if not failures:
        print(f"  All hard invariants passed ({len(warnings)} note(s)).")
    con.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
