"""Derive per-event context features. No API calls.

    python scripts/11_derive_context_features.py

Run after script 09. Everything here comes from tables already in the database;
the point is to compute it once, consistently, with the look-ahead rules
applied in one place rather than in prompt-building code.

Every feature is measured **as of the close of t0** — the moment the forecast is
made. Nothing from t0+1 onwards enters any column in this table.

Feature groups
--------------
**Announcement reaction (the strongest single input after the surprise).**
The move on t0 itself. The study design excludes t0 from the outcome window
precisely because it prices the mechanical surprise, which makes it clean input
rather than leakage. ``reaction_return`` is the t0 total return,
``reaction_abnormal`` subtracts SPY over the same day, and ``volume_ratio``
compares t0 volume with its trailing 20-day median — a crude but effective
measure of how much attention the report drew.

**Trailing momentum and risk.** Returns over 21 / 63 / 252 trading days ending
at t0, annualised realised volatility over 252 days, and where the price sits
in its 52-week range. These need history *before* the event window, which is
why prices are fetched from ``config.PRICE_HISTORY_FROM`` (five years back).

**Valuation and growth.** Trailing P/E and P/S from the market cap at t0 and the
last four reported quarters, plus year-over-year revenue and net-income growth
for the quarter being announced. Only statements whose own announcement
preceded t0 are used, so nothing unpublished leaks in.

**Market regime.** SPY trailing return and realised volatility at t0, so the
model can tell a calm tape from a violent one.

Missing values are left NULL rather than imputed. A prompt builder should say
"not available" instead of showing a fabricated number — the honest signal is
part of what the model has to reason about.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "event_features"

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_features (
    symbol      TEXT NOT NULL,
    fiscal_date TEXT NOT NULL,
    t0          TEXT NOT NULL,

    -- Announcement reaction, measured on t0 itself
    reaction_return    REAL,   -- t0 total return
    reaction_abnormal  REAL,   -- t0 return minus SPY on the same day
    volume_ratio       REAL,   -- t0 volume / trailing 20-day median volume

    -- Trailing momentum and risk, ending at t0
    ret_21d            REAL,
    ret_63d            REAL,
    ret_252d           REAL,
    volatility_252d    REAL,   -- annualised standard deviation of daily returns
    pct_of_52w_range   REAL,   -- 0 = at the 52-week low, 1 = at the high
    drawdown_from_252d_high REAL,

    -- Valuation and growth, using only statements published before t0
    pe_trailing        REAL,
    ps_trailing        REAL,
    revenue_yoy        REAL,
    net_income_yoy     REAL,

    -- Market regime at t0
    spy_ret_21d        REAL,
    spy_ret_63d        REAL,
    spy_volatility_21d REAL,

    PRIMARY KEY (symbol, fiscal_date),
    FOREIGN KEY (symbol) REFERENCES companies(symbol)
);
"""

TRADING_DAYS = 252


def returns(series: list[float]) -> list[float]:
    return [series[i] / series[i - 1] - 1.0
            for i in range(1, len(series)) if series[i - 1]]


def annualised_vol(rets: list[float]) -> float | None:
    if len(rets) < 20:
        return None
    return statistics.pstdev(rets) * (TRADING_DAYS ** 0.5)


def pct_change(a, b):
    """Growth from *b* to *a*; undefined when the base is zero or negative."""
    if a is None or b is None or b <= 0:
        return None
    return a / b - 1.0


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    args = ap.parse_args()

    con = config.connect()
    con.executescript(SCHEMA)
    con.execute("DELETE FROM event_features")
    con.execute("DELETE FROM provenance WHERE table_name = ?", (TABLE,))

    # ── price series ───────────────────────────────────────────────
    bars: dict[str, list[tuple]] = {}
    for sym, date, adj, vol in con.execute(
            "SELECT symbol, date, adj_close, volume FROM prices_daily ORDER BY symbol, date"):
        bars.setdefault(sym, []).append((date, adj, vol))
    idx = {s: {d: i for i, (d, _, _) in enumerate(v)} for s, v in bars.items()}

    spy = con.execute(
        "SELECT date, adj_close FROM market_benchmark ORDER BY date").fetchall()
    spy_idx = {d: i for i, (d, _) in enumerate(spy)}

    # ── fundamentals, keyed by the date they became public ─────────
    # A statement is usable only once its quarter has been announced.
    reported_on = {(s, f): r for s, f, r in con.execute(
        "SELECT symbol, fiscal_date, report_date FROM earnings WHERE report_date IS NOT NULL")}
    fundamentals: dict[str, list[tuple]] = {}
    for sym, fiscal, rev, ni in con.execute(
            """SELECT symbol, fiscal_date, revenue, net_income FROM income_statement
                WHERE period = 'quarterly' ORDER BY symbol, fiscal_date"""):
        fundamentals.setdefault(sym, []).append((fiscal, rev, ni))

    events = con.execute(
        "SELECT symbol, fiscal_date, t0 FROM events ORDER BY symbol, t0").fetchall()

    rows = []
    for sym, fiscal, t0 in events:
        series = bars.get(sym)
        i = idx.get(sym, {}).get(t0)
        if series is None or i is None:
            continue

        adj = [b[1] for b in series]
        vols = [b[2] for b in series]

        def trailing_return(n: int):
            j = i - n
            if j < 0 or not adj[j] or not adj[i]:
                return None
            return adj[i] / adj[j] - 1.0

        # ── reaction on t0 ─────────────────────────────────────────
        reaction = trailing_return(1)
        si = spy_idx.get(t0)
        spy_day = None
        if si is not None and si > 0 and spy[si - 1][1]:
            spy_day = spy[si][1] / spy[si - 1][1] - 1.0
        reaction_abn = (reaction - spy_day
                        if reaction is not None and spy_day is not None else None)

        prior_vol = [v for v in vols[max(0, i - 20):i] if v]
        volume_ratio = (vols[i] / statistics.median(prior_vol)
                        if vols[i] and len(prior_vol) >= 10 else None)

        # ── trailing momentum and risk ─────────────────────────────
        window = [a for a in adj[max(0, i - TRADING_DAYS + 1): i + 1] if a and a > 0]
        vol_252 = annualised_vol(returns(window)) if len(window) >= 60 else None
        hi = max(window) if window else None
        lo = min(window) if window else None
        pct_range = ((adj[i] - lo) / (hi - lo)
                     if hi and lo and hi > lo and adj[i] else None)
        drawdown = (adj[i] / hi - 1.0) if hi and adj[i] else None

        # ── valuation and growth, published-before-t0 only ─────────
        pe = ps = rev_yoy = ni_yoy = None
        quarters = [(f, rev, ni) for f, rev, ni in fundamentals.get(sym, [])
                    if (reported_on.get((sym, f)) or _plus_days(f, 90)) < t0]
        if quarters:
            last4 = quarters[-4:]
            ttm_rev = sum(r for _, r, _ in last4 if r is not None) if len(last4) == 4 else None
            ttm_ni = sum(n for _, _, n in last4 if n is not None) if len(last4) == 4 else None
            cap = con.execute(
                "SELECT market_cap_at_t0 FROM events WHERE symbol=? AND fiscal_date=?",
                (sym, fiscal)).fetchone()[0]
            if cap and ttm_ni and ttm_ni > 0:
                pe = cap / ttm_ni
            if cap and ttm_rev and ttm_rev > 0:
                ps = cap / ttm_rev
            latest_f, latest_rev, latest_ni = quarters[-1]
            year_ago = next(((r, n) for f, r, n in quarters
                             if abs(_days_between(f, latest_f) - 365) <= 45), None)
            if year_ago:
                rev_yoy = pct_change(latest_rev, year_ago[0])
                ni_yoy = pct_change(latest_ni, year_ago[1])

        # ── market regime ──────────────────────────────────────────
        spy_21 = spy_63 = spy_vol = None
        if si is not None:
            if si - 21 >= 0 and spy[si - 21][1]:
                spy_21 = spy[si][1] / spy[si - 21][1] - 1.0
            if si - 63 >= 0 and spy[si - 63][1]:
                spy_63 = spy[si][1] / spy[si - 63][1] - 1.0
            recent = [v for _, v in spy[max(0, si - 20): si + 1] if v]
            spy_vol = annualised_vol(returns(recent))

        rows.append((sym, fiscal, t0, reaction, reaction_abn, volume_ratio,
                     trailing_return(21), trailing_return(63), trailing_return(252),
                     vol_252, pct_range, drawdown, pe, ps, rev_yoy, ni_yoy,
                     spy_21, spy_63, spy_vol))

    con.executemany(
        f"""INSERT INTO event_features VALUES ({','.join('?' * 19)})""", rows)
    config.record_provenance(
        con, TABLE, "derived", "prices_daily + market_benchmark + income_statement + events",
        len(rows),
        "Per-event context measured as of the close of t0. Fundamentals restricted to "
        "quarters announced before t0; no value uses data from t0+1 onwards.")
    con.commit()

    n = len(rows)
    print(f"[11] context features | {n} events")
    cols = [d[1] for d in con.execute("PRAGMA table_info(event_features)")][3:]
    print("  coverage:")
    for c in cols:
        k = con.execute(f"SELECT COUNT({c}) FROM event_features").fetchone()[0]
        flag = "  <-- thin" if k < n * 0.9 else ""
        print(f"    {c:24s} {k:5d}/{n} ({100 * k / n:5.1f}%){flag}")
    con.close()
    return 0


def _plus_days(iso: str, days: int) -> str:
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def _days_between(a: str, b: str) -> int:
    from datetime import date
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    return abs((date(yb, mb, db) - date(ya, ma, da)).days)


if __name__ == "__main__":
    raise SystemExit(main())
