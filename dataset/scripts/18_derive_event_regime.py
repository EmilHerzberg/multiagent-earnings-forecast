"""Snapshot the macro / risk regime as of each event's t0. No API calls.

    python scripts/18_derive_event_regime.py

Run after scripts 09 and 13-17.

Purpose
-------
The regime indicators are stored as time series. This attaches, for every event,
the value of each one **that was public at the close of t0** — so the
point-in-time rule lives in one place instead of being re-implemented wherever a
prompt gets built.

Two selection rules apply, depending on the series:

*Continuously quoted* — VIX, Treasury yields, oil, and the newspaper indices
(AI-GPR, TPU, EPU, all built from that day's papers): take the most recent
observation **on or before t0**. Markets and newspapers are same-day public.

*Statistical releases* — CPI, unemployment, retail sales: take the most recent
observation whose ``public_from`` is **strictly before t0**, which respects the
publication lag set in script 13.

Percentiles
-----------
A level means little on its own. "GPR is 180" is not something a model can
reason about; "GPR is at the 78th percentile of the last five years" is. Each
headline indicator therefore also gets its percentile rank against the
**preceding** five years of its own history — computed from observations up to
t0 only, so the ranking itself carries no look-ahead either.

Derived fields
--------------
``yield_curve`` is the 10-year minus the 2-year yield. A negative value (an
inverted curve) is the classic recession signal and behaves differently from
either yield alone, which is why both maturities are stored and the spread is
formed here.
"""
from __future__ import annotations

import argparse
import bisect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "event_regime"

SCHEMA = """
DROP TABLE IF EXISTS event_regime;
CREATE TABLE event_regime (
    symbol      TEXT NOT NULL,
    fiscal_date TEXT NOT NULL,
    t0          TEXT NOT NULL,

    -- Implied volatility (Cboe)
    vix              REAL,
    vix_pctile_5y    REAL,

    -- Geopolitical risk (AI-GPR)
    gpr_ai           REAL,
    gpr_ai_ma30      REAL,
    gpr_threats      REAL,
    gpr_acts         REAL,
    gpr_oil          REAL,
    gpr_pctile_5y    REAL,

    -- Policy uncertainty
    tpu              REAL,
    tpu_ma7          REAL,
    tpu_ma30         REAL,
    tpu_pctile_5y    REAL,
    epu_us           REAL,
    epu_pctile_5y    REAL,

    -- Rates
    fed_funds        REAL,
    ust_2y           REAL,
    ust_10y          REAL,
    yield_curve      REAL,   -- 10y minus 2y; negative = inverted

    -- Commodities
    wti              REAL,
    brent            REAL,

    -- Statistical releases, lagged to their publication date
    cpi              REAL,
    cpi_observation  TEXT,   -- which month the CPI figure refers to
    unemployment     REAL,
    retail_sales     REAL,

    PRIMARY KEY (symbol, fiscal_date),
    FOREIGN KEY (symbol) REFERENCES companies(symbol)
);
"""

PCTILE_YEARS = 5


class Series:
    """A date-ordered numeric series with as-of lookup and trailing percentile."""

    def __init__(self, pairs: list[tuple[str, float]]):
        pairs = sorted((d, v) for d, v in pairs if d and v is not None)
        self.dates = [d for d, _ in pairs]
        self.values = [v for _, v in pairs]

    def as_of(self, date: str, strict: bool = False):
        """Most recent value on or before *date* (strictly before if ``strict``)."""
        i = (bisect.bisect_left(self.dates, date) if strict
             else bisect.bisect_right(self.dates, date))
        return self.values[i - 1] if i else None

    def percentile(self, date: str, years: int = PCTILE_YEARS):
        """Rank of the current value within the preceding *years* of history."""
        hi = bisect.bisect_right(self.dates, date)
        if not hi:
            return None
        cutoff = f"{int(date[:4]) - years}{date[4:]}"
        lo = bisect.bisect_left(self.dates, cutoff)
        window = self.values[lo:hi]
        if len(window) < 60:
            return None
        current = self.values[hi - 1]
        return sum(1 for v in window if v <= current) / len(window)


def load(con, sql: str) -> Series:
    return Series(con.execute(sql).fetchall())


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    args = ap.parse_args()

    con = config.connect()

    def has(table):
        return bool(con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()[0])

    missing = [t for t in ("vix", "geopolitical_risk", "uncertainty_indices", "macro_series")
               if not has(t)]
    if missing:
        raise SystemExit(f"missing tables: {', '.join(missing)} — run scripts 13-17 first")

    con.executescript(SCHEMA)
    con.execute("DELETE FROM provenance WHERE table_name = ?", (TABLE,))

    vix = load(con, "SELECT date, close FROM vix")
    gpr = load(con, "SELECT date, gpr_ai FROM geopolitical_risk")
    gpr_ma30 = load(con, "SELECT date, gpr_ai_ma30 FROM geopolitical_risk")
    gpr_thr = load(con, "SELECT date, gpr_threats FROM geopolitical_risk")
    gpr_act = load(con, "SELECT date, gpr_acts FROM geopolitical_risk")
    gpr_oil = load(con, "SELECT date, gpr_oil FROM geopolitical_risk")
    tpu = load(con, "SELECT date, tpu FROM uncertainty_indices")
    tpu7 = load(con, "SELECT date, tpu_ma7 FROM uncertainty_indices")
    tpu30 = load(con, "SELECT date, tpu_ma30 FROM uncertainty_indices")
    epu = load(con, "SELECT date, epu_us FROM uncertainty_indices")

    def macro(series_id, lagged=False):
        col = "public_from" if lagged else "observation_date"
        return Series(con.execute(
            f"SELECT {col}, value FROM macro_series WHERE series_id=?",
            (series_id,)).fetchall())

    fed = macro("FED_FUNDS")
    y2 = macro("UST_2Y")
    y10 = macro("UST_10Y")
    wti = macro("WTI")
    brent = macro("BRENT")
    cpi = macro("CPI", lagged=True)
    unemp = macro("UNEMPLOYMENT", lagged=True)
    retail = macro("RETAIL_SALES", lagged=True)
    # Which month a lagged figure refers to, for transparency in the prompt.
    cpi_obs = {r[0]: r[1] for r in con.execute(
        "SELECT public_from, observation_date FROM macro_series WHERE series_id='CPI'")}
    cpi_obs_series = Series([(k, 1.0) for k in cpi_obs])

    events = con.execute("SELECT symbol, fiscal_date, t0 FROM events ORDER BY t0").fetchall()
    rows = []
    for sym, fd, t0 in events:
        y2v, y10v = y2.as_of(t0), y10.as_of(t0)
        curve = (y10v - y2v) if (y2v is not None and y10v is not None) else None

        i = bisect.bisect_left(cpi_obs_series.dates, t0)
        cpi_ref = cpi_obs.get(cpi_obs_series.dates[i - 1]) if i else None

        rows.append((
            sym, fd, t0,
            vix.as_of(t0), vix.percentile(t0),
            gpr.as_of(t0), gpr_ma30.as_of(t0), gpr_thr.as_of(t0), gpr_act.as_of(t0),
            gpr_oil.as_of(t0), gpr.percentile(t0),
            tpu.as_of(t0), tpu7.as_of(t0), tpu30.as_of(t0), tpu.percentile(t0),
            epu.as_of(t0), epu.percentile(t0),
            fed.as_of(t0), y2v, y10v, curve,
            wti.as_of(t0), brent.as_of(t0),
            cpi.as_of(t0, strict=True), cpi_ref,
            unemp.as_of(t0, strict=True), retail.as_of(t0, strict=True),
        ))

    con.executemany(f"INSERT INTO event_regime VALUES ({','.join('?' * 27)})", rows)
    config.record_provenance(
        con, TABLE, "derived",
        "vix + geopolitical_risk + uncertainty_indices + macro_series", len(rows),
        "Regime indicators as of the close of t0. Continuously quoted series taken on or "
        "before t0; statistical releases only once public_from < t0. Percentiles ranked "
        f"against the preceding {PCTILE_YEARS} years, using observations up to t0 only.")
    con.commit()

    n = len(rows)
    print(f"[18] event regime | {n} events")
    cols = [d[1] for d in con.execute("PRAGMA table_info(event_regime)")][3:]
    thin = []
    for c in cols:
        k = con.execute(f"SELECT COUNT({c}) FROM event_regime").fetchone()[0]
        pct = 100 * k / n
        mark = "  <-- thin" if pct < 95 else ""
        print(f"    {c:20s} {k:5d}/{n} ({pct:5.1f}%){mark}")
        if pct < 95:
            thin.append(c)

    print("\n  sample (first event):")
    r = con.execute("""SELECT t0, ROUND(vix,1), ROUND(vix_pctile_5y,2), ROUND(gpr_ai,1),
                              ROUND(gpr_pctile_5y,2), ROUND(tpu_ma30,1), ROUND(epu_us,1),
                              ROUND(yield_curve,2), ROUND(wti,1)
                         FROM event_regime ORDER BY t0 LIMIT 1""").fetchone()
    print(f"    t0={r[0]} VIX={r[1]} (p{r[2]}) GPR={r[3]} (p{r[4]}) "
          f"TPU30={r[5]} EPU={r[6]} curve={r[7]} WTI={r[8]}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
