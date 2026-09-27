"""Build the forecastable event set. Derived — no API calls.

    python scripts/09_build_events.py [--horizon 5]

Run after scripts 02-08. Turns announcements plus prices into the units the
experiment actually forecasts, and computes the outcome each forecast is graded
against.

The event definition
--------------------
``t0`` is the first trading day that prices the announcement::

    BeforeMarket   announced before the open  -> t0 = report_date
    DuringMarket   announced intraday         -> t0 = report_date
    AfterMarket    announced after the close  -> t0 = next trading day

If ``report_date`` is not itself a trading day (weekend, holiday), t0 moves
forward to the next one.

The forecast is made **after t0 closes**. t0 is deliberately excluded from the
outcome window: the first session mainly prices the mechanical surprise in the
report, and the study targets what happens after that adjustment.

    window = t0+1 .. t0+horizon        (trading days, default 5)

    stock_return    = adj_close(t0+h) / adj_close(t0) - 1
    market_return   = product over window of (1 + SPY daily total return) - 1
    abnormal_return = stock_return - market_return
    label           = 1 if abnormal_return > 0 else 0

Both sides use adjusted closes, so both are total returns. Mixing an adjusted
stock return with a price-only index return would bias every event by roughly
the dividend yield over the window.

Events are dropped when t0 cannot be determined (timing unknown), when the
price series ends before t0+horizon, or when the benchmark is missing for any
day of the window. Every drop is logged to ``data_exclusions``.

Look-ahead guards written into each row
---------------------------------------
``close_at_t0`` is the **raw** close — the price actually quoted that day. The
adjusted close is retroactively scaled by every later split and dividend, so
showing it as a price level would leak future corporate actions (BKNG trades at
4,990 in January 2025 but its adjusted close reads 197, revealing a split that
happens 15 months later). Returns are unaffected, because the scaling factor
cancels in any ratio. See README 6.1 and 7.10.

``split_after_t0`` marks events where a split occurs later in the sample. Those
rows must never receive split-adjusted price levels or any corporate-action
field in the prompt. ``split_in_lookback`` marks a discontinuity in the raw
series before t0 — legitimate information, but it needs handling so the model
is not shown an apparent price collapse that was actually a split.

``market_cap_at_t0`` uses a **lagged** share count: the most recent quarter
whose earnings announcement precedes t0. A quarter's share count is only public
once the company reports, so attaching the concurrent quarter would leak. See
README 7.11.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "events"

#: Relative change in the adjustment factor that counts as a split rather than
#: a dividend. Dividend drift is well under 5% per event; splits are >= 2x.
SPLIT_FACTOR_TOLERANCE = 0.20
#: Trading days before t0 considered "the lookback the model may be shown".
LOOKBACK_DAYS = 60

#: How far a symbol's last price bar may trail the data horizon before the
#: security counts as having STOPPED TRADING.
#:
#: A vendor backfill does not finish in one session, so the panel ends ragged
#: even for companies that are perfectly alive. Measured on this build: the
#: horizon (the newest bar anywhere in prices_daily) is 2026-06-11, 266 symbols
#: carry a bar on that day and 252 stop one day earlier on 2026-06-10 — all of
#: them still trading. Only 11 symbols end genuinely early, and the nearest of
#: those (CTRA, acquired by Devon) stops 35 days before the horizon.
#:
#: So the two populations are separated by a wide empty gap, and the choice of
#: tolerance is not delicate: every value from 1 to 34 days classifies exactly
#: the same 11 symbols as stopped. 7 days (one week) sits in the middle of that
#: plateau. 0 days would wrongly delist the 252 symbols that are one bar short.
STOPPED_TRADING_GRACE_DAYS = 7


def build_index_membership(con, prices_end: dict[str, str]) -> int:
    """Reconstruct point-in-time S&P 500 membership into ``index_membership``.

    Two inputs are combined:

    ``sp500_index_changes.csv``
        Additions and removals with effective dates. Rows are marked
        ``verified`` where the date was confirmed against an S&P Dow Jones
        Indices press release, ``aggregator`` otherwise.

    the price series itself
        When a company stops trading — acquisition, take-private, ticker
        rename — the last observed bar is authoritative for when the security
        ceased to be investable, and is more reliable than a secondary change
        log.

    A company leaves the index when S&P removes it **or** when its security
    stops trading, whichever comes **first**. The two sources are therefore
    combined, not substituted: ``in_index_to`` is the earlier of the change-log
    date and the day after the last bar, and ``to_source`` names whichever one
    produced it (``verified`` / ``aggregator`` from the CSV, ``price_series``
    from the data). A press-release date is never discarded because the price
    data happened to run out.

    THE TRAP, and why this function is written the way it is
    -------------------------------------------------------
    "Stopped trading" must be measured against the **data horizon** — the
    newest date anywhere in ``prices_daily`` — and never against
    ``config.END_DATE``. Those are not the same thing, and when they differ the
    difference is silent and total.

    This code originally read ``if last < config.END_DATE``. The price backfill
    reached 2026-06-11 while the study window runs to 2026-06-30, so "the
    series ends early" was true for **529 of 529 symbols**. Every one of the 29
    removals was re-dated to the day the data stops, including four dates
    confirmed against S&P press releases and including companies dropped for
    market capitalisation that never stopped trading at all. BorgWarner left on
    2025-03-24 but was stored as leaving on 2026-06-11, and five of its later
    events were counted as index events. In total 36 of 2,209 eligible events
    (1.6 %) came from companies that were no longer members on their own
    reaction day.

    The fingerprint of that failure is an index that only ever grows: additions
    land on their real dates while removals all pile up at the end of the data.
    Reconstructing the daily member count showed min 499 / median 507 / max
    519 against a true ~503, with 220 of 546 days outside a plausible band.
    ``10_validate.py`` now reconstructs that count and fails on it, so the same
    class of mistake cannot pass silently again.

    Companies never mentioned in the change file are assumed to have been
    members for the whole window (``from_source = 'assumed_at_window_start'``).
    That assumption is the residual weakness: the base list approximates
    membership at the start of the window, so a company that both joined and
    left before the window would be missed entirely.
    """
    import csv as _csv

    path = config.THESIS_DIR / "sp500_index_changes.csv"
    if not path.exists():
        print(f"  no {path.name} — membership not reconstructed")
        return 0

    rows = list(_csv.DictReader(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")))

    frm: dict[str, tuple[str, str]] = {}
    to: dict[str, tuple[str, str]] = {}
    notes: dict[str, str] = {}
    for r in rows:
        sym, date, conf = r["symbol"], r["effective_date"], r["confidence"]
        if r["action"] == "add":
            frm[sym] = (date, conf)
        else:
            to[sym] = (date, conf)
        notes[sym] = r.get("note", "")

    # ── combine the change log with the price series ───────────────
    # Whichever source puts the company out of the index FIRST wins. The change
    # log is the default; the price series overrides it only when the security
    # demonstrably stopped trading BEFORE the logged date, and it also closes
    # membership for a company the change log never mentions at all.
    #
    # The comparison is against the data horizon, not config.END_DATE — see the
    # docstring: getting that wrong re-dated all 29 removals to the end of the
    # price data and put 36 non-members into the eligible sample.
    horizon = max(prices_end.values()) if prices_end else None
    n_overridden = n_orphan = 0
    for sym, last in prices_end.items():  # empty dict => horizon unused
        if _days_between(last, horizon) <= STOPPED_TRADING_GRACE_DAYS:
            continue  # still quoted at the horizon — the panel is merely ragged
        # First day on which the security was no longer investable.
        stopped = _plus_days(last, 1)
        logged = to.get(sym)
        if logged is None:
            # Stopped trading with no removal row. Without this the company
            # stays a member forever, which is one source of the upward drift
            # in the member count. Measured on this build: 1 symbol (PARA,
            # last bar 2025-08-06), and it costs no eligible event because
            # both of its events predate the leakage cut.
            to[sym] = (stopped, "price_series")
            n_orphan += 1
        elif stopped < logged[0]:
            to[sym] = (stopped, "price_series")
            n_overridden += 1
        # else: the logged date comes first and is kept, with its own
        # confidence — a verified press-release date is never thrown away.

    universe = [r[0] for r in con.execute("SELECT symbol FROM companies")]
    out = []
    for sym in universe:
        f = frm.get(sym)
        t = to.get(sym)
        out.append((
            sym,
            f[0] if f else None,
            t[0] if t else None,
            f[1] if f else "assumed_at_window_start",
            t[1] if t else None,
            notes.get(sym, ""),
        ))
    con.executemany(
        """INSERT INTO index_membership
               (symbol, in_index_from, in_index_to, from_source, to_source, note)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET
               in_index_from=excluded.in_index_from, in_index_to=excluded.in_index_to,
               from_source=excluded.from_source, to_source=excluded.to_source,
               note=excluded.note""", out)

    n_add, n_rem = len(frm), len(to)
    n_price = sum(1 for _, c in to.values() if c == "price_series")
    config.record_provenance(
        con, "index_membership", "S&P Dow Jones Indices (press releases) + secondary "
        "change logs", "sp500_index_changes.csv", len(out),
        f"{n_add} additions, {n_rem} removals. in_index_to is the EARLIER of the "
        f"change-log date and the day after the last price bar: {n_rem - n_price} come "
        f"from the change log, {n_price} from the price series ({n_overridden} of those "
        f"overrode a later logged date, {n_orphan} closed a company the change log never "
        f"mentions). Price data horizon {horizon}; a series counts as ended only if it "
        f"stops more than {STOPPED_TRADING_GRACE_DAYS} days before it. Companies absent "
        f"from the change file are assumed members for the whole window.")
    print(f"  membership: {n_add} additions, {n_rem} removals "
          f"({n_rem - n_price} dated from the change log, {n_price} from the price "
          f"series: {n_overridden} override, {n_orphan} unlogged)")
    return len(out)


def detect_split_dates(bars: list[tuple]) -> list[str]:
    """Dates on which the split/dividend adjustment factor jumps.

    ``bars`` is (date, close, adj_close) sorted by date. The factor
    ``adj_close/close`` is constant between corporate actions and steps at each
    one; dividends move it by fractions of a percent, splits by the split ratio.
    """
    dates = []
    prev = None
    for date, close, adj in bars:
        if not close or not adj:
            continue
        factor = adj / close
        if prev is not None and prev > 0:
            if abs(factor / prev - 1.0) > SPLIT_FACTOR_TOLERANCE:
                dates.append(date)
        prev = factor
    return dates


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--horizon", type=int, default=5,
                    help="Forecast horizon in trading days (default 5).")
    ap.add_argument("--benchmark", default="SPY")
    args = ap.parse_args()
    H = args.horizon

    con = config.connect()
    # This script is idempotent: clear what a previous run of it wrote.
    con.execute("DELETE FROM events")
    con.execute("DELETE FROM data_exclusions WHERE table_name = ?", (TABLE,))
    con.execute("DELETE FROM provenance WHERE table_name IN ('events', 'index_membership')")

    # ── price series per symbol ────────────────────────────────────
    bars: dict[str, list[tuple]] = {}
    for sym, date, close, adj in con.execute(
            "SELECT symbol, date, close, adj_close FROM prices_daily ORDER BY symbol, date"):
        bars.setdefault(sym, []).append((date, close, adj))
    splits = {s: set(detect_split_dates(v)) for s, v in bars.items()}
    n_split_symbols = sum(1 for v in splits.values() if v)
    prices_end = {s: v[-1][0] for s, v in bars.items() if v}

    build_index_membership(con, prices_end)
    membership = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT symbol, in_index_from, in_index_to FROM index_membership")}

    # ── benchmark daily total returns ──────────────────────────────
    bench = con.execute(
        "SELECT date, adj_close FROM market_benchmark WHERE symbol=? ORDER BY date",
        (args.benchmark,)).fetchall()
    if not bench:
        raise SystemExit(
            f"market_benchmark is empty for {args.benchmark} — "
            f"run 07_alphavantage_market_benchmark.py first")
    mkt_ret = {}
    for i in range(1, len(bench)):
        prev, cur = bench[i - 1][1], bench[i][1]
        if prev and cur:
            mkt_ret[bench[i][0]] = cur / prev - 1.0

    # ── lagged share counts ────────────────────────────────────────
    # A quarter's share count becomes public with that quarter's earnings
    # report, so map fiscal_date -> report_date and only use quarters already
    # reported before t0.
    reported_on = {}
    for sym, fiscal, report in con.execute(
            "SELECT symbol, fiscal_date, report_date FROM earnings WHERE report_date IS NOT NULL"):
        reported_on[(sym, fiscal)] = report
    shares: dict[str, list[tuple]] = {}
    for sym, fiscal, n in con.execute(
            "SELECT symbol, fiscal_date, shares FROM shares_outstanding ORDER BY symbol, fiscal_date"):
        shares.setdefault(sym, []).append((fiscal, n))

    def shares_known_at(sym: str, t0: str):
        """Most recent share count whose quarter was already reported before t0."""
        best = None
        for fiscal, n in shares.get(sym, []):
            report = reported_on.get((sym, fiscal))
            known = report if report else _plus_days(fiscal, 90)
            if known and known < t0:
                best = (fiscal, n)
        return best or (None, None)

    have_fundamentals = {
        r[0] for r in con.execute("SELECT DISTINCT symbol FROM income_statement")}

    # ── build ──────────────────────────────────────────────────────
    announcements = con.execute(
        """SELECT symbol, fiscal_date, report_date, before_after_market,
                  eps_actual, eps_estimate, eps_surprise_pct
             FROM earnings ORDER BY symbol, report_date""").fetchall()

    rows, dropped = [], {}

    def drop(sym, ref, reason, detail=""):
        dropped[reason] = dropped.get(reason, 0) + 1
        config.record_exclusion(con, TABLE, sym, ref, reason, detail)

    for sym, fiscal, report, timing, act, est, surp in announcements:
        series = bars.get(sym)
        if not series:
            drop(sym, fiscal, "no_price_series", "Symbol has no bars in prices_daily.")
            continue
        if not timing:
            drop(sym, fiscal, "timing_unknown",
                 "before_after_market is NULL; t0 cannot be assigned.")
            continue

        dates = [b[0] for b in series]
        pos = next((i for i, d in enumerate(dates) if d >= report), None)
        if pos is None:
            drop(sym, fiscal, "no_trading_day_after_report",
                 f"Series ends {dates[-1]}, before report_date {report}.")
            continue
        if timing == "AfterMarket" and dates[pos] == report:
            pos += 1
        if pos >= len(dates):
            drop(sym, fiscal, "no_reaction_day", "Series ends on the announcement day.")
            continue
        if pos + H >= len(dates):
            drop(sym, fiscal, f"fewer_than_{H}_days_after_t0",
                 f"t0={dates[pos]}, only {len(dates) - pos - 1} trading days remain.")
            continue

        t0 = dates[pos]
        window = dates[pos + 1: pos + H + 1]
        missing = [d for d in window if d not in mkt_ret]
        if missing:
            drop(sym, fiscal, "benchmark_missing_for_window",
                 f"No {args.benchmark} return for {', '.join(missing)}.")
            continue

        adj0, adjH = series[pos][2], series[pos + H][2]
        if not adj0 or not adjH:
            drop(sym, fiscal, "missing_adjusted_close", f"t0={t0}")
            continue

        stock = adjH / adj0 - 1.0
        market = 1.0
        for d in window:
            market *= 1.0 + mkt_ret[d]
        market -= 1.0
        abnormal = stock - market

        m_from, m_to = membership.get(sym, (None, None))
        in_index = (m_from is None or t0 >= m_from) and (m_to is None or t0 < m_to)

        sym_splits = splits.get(sym, set())
        lookback = set(dates[max(0, pos - LOOKBACK_DAYS): pos + 1])
        fiscal_shares, n_shares = shares_known_at(sym, t0)
        cap = adj0 * n_shares if n_shares else None

        rows.append((
            sym, fiscal, report, timing, t0, window[0], window[-1], H,
            stock, market, abnormal, 1 if abnormal > 0 else 0,
            series[pos][1], cap, fiscal_shares, act, est, surp,
            int(any(d > t0 for d in sym_splits)),
            int(bool(sym_splits & lookback)),
            int(sym in have_fundamentals),
            int(in_index),
        ))

    con.executemany(
        """INSERT INTO events (symbol, fiscal_date, report_date, before_after_market,
               t0, window_start, window_end, horizon_days,
               stock_return, market_return, abnormal_return, label,
               close_at_t0, market_cap_at_t0, shares_fiscal_date,
               eps_actual, eps_estimate, eps_surprise_pct,
               split_after_t0, split_in_lookback, has_fundamentals, in_index_at_t0)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)

    config.record_provenance(
        con, TABLE, "derived", f"prices_daily + earnings + market_benchmark ({args.benchmark}) "
        f"+ shares_outstanding", len(rows),
        f"Horizon {H} trading days from t0+1. Abnormal return = stock total return minus "
        f"{args.benchmark} total return. close_at_t0 is the RAW close (look-ahead guard); "
        f"market cap uses a share count already reported before t0.")
    con.commit()

    # ── report ─────────────────────────────────────────────────────
    n = len(rows)
    print(f"[09] events | horizon {H} trading days | benchmark {args.benchmark}")
    print(f"  built: {n} of {len(announcements)} announcements "
          f"({100 * n / len(announcements):.1f}%)")
    if dropped:
        print("  dropped:")
        for reason, count in sorted(dropped.items(), key=lambda x: -x[1]):
            print(f"    {reason:34s} {count}")

    in_idx = [r for r in rows if r[21] == 1]
    print(f"\n  in index at t0 : {len(in_idx)}/{n} ({100 * len(in_idx) / n:.1f}%)"
          f"   <- the point-in-time sample")
    print(f"  outside        : {n - len(in_idx)} (pre-addition or post-removal events)")
    print("\n  base rate (positive abnormal return):")
    for label_, subset in [("all events   ", rows), ("in-index only", in_idx)]:
        if subset:
            p = sum(1 for r in subset if r[11] == 1)
            print(f"    {label_} {p}/{len(subset)} = {100 * p / len(subset):.2f}%"
                  f"   Brier {(p / len(subset)) * (1 - p / len(subset)):.4f}")

    with_cap = sum(1 for r in rows if r[13])
    print(f"\n  market cap available   : {with_cap}/{n} ({100 * with_cap / n:.1f}%)")
    print(f"  fundamentals available : {sum(r[20] for r in rows)}/{n}")
    print(f"  split after t0 (hide!) : {sum(r[18] for r in rows)}")
    print(f"  split in 60d lookback  : {sum(r[19] for r in rows)}")
    print(f"  symbols with any split : {n_split_symbols}")

    print("\n  timing split:")
    for timing, count in con.execute(
            "SELECT before_after_market, COUNT(*) FROM events GROUP BY 1 ORDER BY 2 DESC"):
        print(f"    {timing:14s} {count:5d}  ({100 * count / n:.1f}%)")
    con.close()
    return 0


def _plus_days(iso: str, days: int) -> str:
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def _days_between(earlier: str, later: str) -> int:
    """Calendar days from *earlier* to *later*; negative if the order is flipped."""
    from datetime import date
    a = date(*(int(x) for x in earlier.split("-")))
    b = date(*(int(x) for x in later.split("-")))
    return (b - a).days


if __name__ == "__main__":
    raise SystemExit(main())
