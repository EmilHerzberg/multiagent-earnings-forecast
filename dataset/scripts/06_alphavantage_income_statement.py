"""Fetch income-statement fundamentals from Alpha Vantage.

    python scripts/06_alphavantage_income_statement.py [--period quarterly|annual|both]

Source
------
Alpha Vantage ``GET /query?function=INCOME_STATEMENT&symbol={SYMBOL}``

Per-symbol endpoint returning ``annualReports`` and ``quarterlyReports``.
Quarterly history reaches back to 2006 for most US large caps (81 quarters at
retrieval), and the endpoint charges one call regardless of how much of it is
kept. Statements are therefore retained from ``config.PRICE_HISTORY_FROM``, not
just the event window: trailing-twelve-month valuation and year-over-year growth
need the four quarters preceding each event, which for a January 2025 event lie
outside the window entirely.

Fields consumed::

    totalRevenue     -> revenue
    netIncome        -> net_income
    operatingIncome  -> operating_income
    ebit             -> ebit
    ebitda           -> ebitda
    grossProfit      -> gross_profit

``operatingIncome`` and ``ebit`` are delivered as separate line items and are
stored separately rather than collapsed: they coincide for most non-financial
companies but diverge when non-operating income is material, and equating them
would hide that.

Why there is no EPS here
------------------------
``INCOME_STATEMENT`` does not carry basic or diluted EPS.  The EPS in this
database comes from EODHD (script 02) and is the consensus-comparable street
figure — the correct counterpart to the estimate, but not GAAP basic/diluted.
The two are not reconcilable: do not divide ``net_income`` from this table by a
share count and compare it to ``earnings.eps_actual``.

Two caveats that belong in the limitations section
--------------------------------------------------
1. **Not point-in-time.** The endpoint returns the current state of each
   historical statement, including later restatements.  Figures for a 2025
   quarter are as reported today, not as first published.  For tests
   conditioning on information available at announcement, anchor on
   ``earnings.report_date`` and treat restatement as a known limitation.

2. **Survivorship.** Alpha Vantage drops fundamentals entirely for companies no
   longer listed — an empty payload, even for quarters they reported while
   trading.  Constituents delisted during the window therefore have earnings
   and prices but no income statement.  An inner join between the tables
   silently removes exactly those firms.  See README section 6.9.

Rate limiting
-------------
Free tier: 25 requests/day and 5/minute — insufficient for 501 symbols.
Premium tiers start at 75 requests/minute.  ``--rpm`` throttles accordingly.
Alpha Vantage signals throttling with HTTP 200 and a prose body; that is
detected and retried in ``config.alphavantage_income_statement``, and a
persistent throttle aborts rather than silently writing partial data.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "income_statement"
ENDPOINT = "/query?function=INCOME_STATEMENT"

FIELD_MAP = {
    "revenue": "totalRevenue",
    "net_income": "netIncome",
    "operating_income": "operatingIncome",
    "ebit": "ebit",
    "ebitda": "ebitda",
    "gross_profit": "grossProfit",
}


def parse_reports(symbol: str, reports, period: str, start: str, end: str) -> list[tuple]:
    out = []
    for r in reports or []:
        fiscal = config.iso_date(r.get("fiscalDateEnding"))
        if not fiscal or not (start <= fiscal <= end):
            continue
        values = [config.to_float(r.get(src)) for src in FIELD_MAP.values()]
        if all(v is None for v in values):
            continue
        out.append((symbol, fiscal, period, r.get("reportedCurrency"), *values))
    return out


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--period", choices=("quarterly", "annual", "both"), default="quarterly")
    ap.add_argument("--rpm", type=int, default=70,
                    help="Requests per minute (premium tier allows 75+).")
    ap.add_argument("--resume", action="store_true",
                    help="Skip symbols already present in income_statement.")
    ap.add_argument("--history-from", default=config.PRICE_HISTORY_FROM,
                    help="Earliest fiscal period to keep. Defaults to the price-history "
                         "start. The endpoint returns ~81 quarters per call regardless, "
                         "so extra history is free — and it is what makes trailing-twelve-"
                         "month and year-over-year features defined for events at the "
                         "start of the window.")
    args = ap.parse_args()
    args.start = args.history_from

    symbols = config.constituents()
    if args.limit:
        symbols = symbols[: args.limit]

    con = config.connect()
    if args.resume:
        done = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM income_statement")}
        symbols = [s for s in symbols if s not in done]
        print(f"  resume: {len(done)} symbols present, {len(symbols)} remaining")

    periods = ["quarterly", "annual"] if args.period == "both" else [args.period]
    delay = 60.0 / max(args.rpm, 1)

    print(f"[06] Alpha Vantage income statements | {len(symbols)} symbols "
          f"| periods={','.join(periods)} | {args.start}..{args.end}")

    written = failed = 0
    no_data: list[str] = []
    for i, symbol in enumerate(symbols, 1):
        try:
            payload = config.alphavantage_income_statement(symbol)
        except config.ProviderError as exc:
            message = str(exc)
            if "rate limit" in message.lower() or "premium" in message.lower():
                con.commit()
                print(f"\n  ABORT at {symbol} ({i}/{len(symbols)}): {message}")
                print("  Partial data committed. Re-run with --resume once quota allows.")
                break
            print(f"  {symbol}: {message}")
            failed += 1
            config.record_exclusion(con, TABLE, symbol, "", "provider_error", message[:200])
            continue

        rows: list[tuple] = []
        for period in periods:
            key = "quarterlyReports" if period == "quarterly" else "annualReports"
            rows += parse_reports(symbol, payload.get(key), period, args.start, args.end)

        if not rows:
            no_data.append(symbol)
            config.record_exclusion(
                con, TABLE, symbol, f"{args.start}..{args.end}", "no_reports_in_window",
                "Empty payload or no periods in window (typical for delisted symbols).")
        else:
            con.executemany(
                f"""INSERT INTO income_statement
                        (symbol, fiscal_date, period, reported_currency, {', '.join(FIELD_MAP)})
                    VALUES ({','.join('?' * (4 + len(FIELD_MAP)))})
                    ON CONFLICT(symbol, fiscal_date, period) DO UPDATE SET
                        {', '.join(f'{c}=excluded.{c}' for c in FIELD_MAP)}""",
                rows,
            )
            written += len(rows)

        if i % 25 == 0:
            con.commit()
            print(f"  {i}/{len(symbols)} | rows={written} | failed={failed}")
        if i < len(symbols):
            time.sleep(delay)

    config.record_provenance(
        con, TABLE, "Alpha Vantage", ENDPOINT, written,
        f"Revenue, net income, operating income, EBIT, EBITDA, gross profit. "
        f"Periods: {','.join(periods)}. Current-state statements including restatements; "
        f"not point-in-time. Delisted symbols return an empty payload.",
    )
    con.commit()

    n, s = con.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM income_statement").fetchone()
    print(f"\n  rows: {n} | symbols: {s} | failed: {failed}")
    for col in FIELD_MAP:
        c = con.execute(f"SELECT COUNT({col}) FROM income_statement").fetchone()[0]
        print(f"    {col:18s} {c:5d} / {n}  ({100 * c / n if n else 0:.1f}%)")
    if no_data:
        print(f"  no reports in window ({len(no_data)}): {', '.join(no_data)}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
