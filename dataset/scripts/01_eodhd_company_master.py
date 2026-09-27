"""Fetch company master data (sector, industry, identifiers) into ``companies``.

    python scripts/01_eodhd_company_master.py [--source auto|eodhd|alphavantage]

Sources
-------
Primary — EODHD ``GET /api/fundamentals/{SYMBOL}.US?filter=General``
    Returns ``Name``, ``Sector``, ``Industry``, ``CountryISO``, ``ISIN``, ``CIK``,
    ``FiscalYearEnd``, ``IPODate``. The ``filter`` keeps the response small.

Fallback — Alpha Vantage ``GET /query?function=OVERVIEW&symbol={SYMBOL}``
    Returns ``Sector``, ``Industry``, ``Name``, ``CIK``, ``Country``,
    ``FiscalYearEnd`` among ~55 fields.

``--source auto`` (the default) tries EODHD and falls back per symbol. If EODHD
answers with an entitlement error (401/402/403) it stops asking and uses the
fallback for the remainder, rather than burning 530 failing calls.

Both providers use the same sector taxonomy — the Morningstar-style scheme
("Technology", "Consumer Defensive", "Financial Services", …), not GICS — so
values from the two are interchangeable. Alpha Vantage returns them upper-cased
(``TECHNOLOGY``), EODHD in title case, so both are normalised to title case here
and the originating provider is recorded in ``companies.data_source``.

Why only OVERVIEW is used from Alpha Vantage
--------------------------------------------
``OVERVIEW`` is a **current snapshot**, which is why it is rejected as a market-
cap source (see script 08 — attaching today's cap to a 2025 event is
look-ahead). Sector and industry are different: they are classifications, not
market variables. A reclassification is rare and would not encode anything about
the outcome being forecast, so a current snapshot is acceptable here in a way it
is not for a price-derived quantity.

Coverage limits
---------------
Alpha Vantage returns an empty payload for delisted companies, so it cannot fill
sector for constituents that left the market — the same survivorship gap as the
fundamentals endpoints (README 7.9). EODHD retains them. Where neither provider
answers, the fields stay NULL rather than being guessed.

Optional step: nothing downstream depends on it, the ``companies`` rows already
exist from script 00. Sector is the usual control variable in cross-sectional
earnings work, which is why it is worth the calls.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "companies"
EODHD_ENDPOINT = "/api/fundamentals/{SYMBOL}.US?filter=General"
AV_ENDPOINT = "/query?function=OVERVIEW"


def title_case(value):
    """Normalise 'CONSUMER DEFENSIVE' and 'Consumer Defensive' to one form."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v or v.lower() in ("none", "n/a", "-"):
        return None
    # Only re-case if the provider shouted; leave mixed-case values intact.
    return v.title() if v.isupper() else v


def from_eodhd(symbol: str) -> dict | None:
    g = config.eodhd_fundamentals_general(symbol)
    if not g:
        return None
    return {
        "name": g.get("Name"),
        "sector": title_case(g.get("Sector")),
        "industry": title_case(g.get("Industry")),
        "country_iso": g.get("CountryISO"),
        "fiscal_year_end": g.get("FiscalYearEnd"),
        "isin": g.get("ISIN"),
        "cik": g.get("CIK"),
        "ipo_date": config.iso_date(g.get("IPODate")),
        "data_source": "EODHD",
    }


def from_alphavantage(symbol: str) -> dict | None:
    o = config.alphavantage_overview(symbol)
    if not o or not o.get("Symbol"):
        return None
    return {
        "name": o.get("Name"),
        "sector": title_case(o.get("Sector")),
        "industry": title_case(o.get("Industry")),
        "country_iso": o.get("Country"),
        "fiscal_year_end": o.get("FiscalYearEnd"),
        "isin": None,          # not provided
        "cik": o.get("CIK"),
        "ipo_date": None,      # not provided
        "data_source": "AlphaVantage",
    }


def ensure_source_column(con) -> None:
    cols = {d[1] for d in con.execute("PRAGMA table_info(companies)")}
    if "data_source" not in cols:
        con.execute("ALTER TABLE companies ADD COLUMN data_source TEXT")


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--source", choices=("auto", "eodhd", "alphavantage"), default="auto")
    ap.add_argument("--rpm", type=int, default=70, help="Requests per minute.")
    ap.add_argument("--only-missing", action="store_true",
                    help="Skip symbols that already have a sector.")
    args = ap.parse_args()

    symbols = config.constituents()
    con = config.connect()
    ensure_source_column(con)
    if args.only_missing:
        have = {r[0] for r in con.execute(
            "SELECT symbol FROM companies WHERE sector IS NOT NULL")}
        symbols = [s for s in symbols if s not in have]
        print(f"  only-missing: {len(have)} already have a sector, {len(symbols)} to fetch")
    if args.limit:
        symbols = symbols[: args.limit]

    delay = 60.0 / max(args.rpm, 1)
    print(f"[01] company master data | {len(symbols)} symbols | source={args.source}")

    eodhd_available = args.source in ("auto", "eodhd")
    counts = {"EODHD": 0, "AlphaVantage": 0}
    missing: list[str] = []

    for i, symbol in enumerate(symbols, 1):
        record = None

        if eodhd_available:
            try:
                record = from_eodhd(symbol)
            except config.NotFound:
                record = None
            except config.ProviderError as exc:
                if any(code in str(exc) for code in ("401", "402", "403")):
                    eodhd_available = False
                    print(f"  EODHD returned an entitlement error; "
                          f"{'falling back to Alpha Vantage' if args.source == 'auto' else 'aborting'}")
                    print(f"    {str(exc).splitlines()[0]}")
                    if args.source == "eodhd":
                        break
                else:
                    print(f"  {symbol} (EODHD): {exc}")

        if record is None and args.source in ("auto", "alphavantage"):
            try:
                record = from_alphavantage(symbol)
            except config.ProviderError as exc:
                print(f"  {symbol} (Alpha Vantage): {exc}")

        if record is None:
            missing.append(symbol)
            config.record_exclusion(con, TABLE, symbol, "", "no_master_data",
                                    "Neither provider returned a profile.")
        else:
            con.execute(
                """UPDATE companies SET name=COALESCE(?, name), sector=?, industry=?,
                       country_iso=COALESCE(?, country_iso),
                       fiscal_year_end=COALESCE(?, fiscal_year_end),
                       isin=COALESCE(?, isin), cik=COALESCE(?, cik),
                       ipo_date=COALESCE(?, ipo_date), data_source=?
                    WHERE symbol=?""",
                (record["name"], record["sector"], record["industry"],
                 record["country_iso"], record["fiscal_year_end"], record["isin"],
                 record["cik"], record["ipo_date"], record["data_source"], symbol))
            counts[record["data_source"]] += 1

        if i % 50 == 0:
            con.commit()
            print(f"  {i}/{len(symbols)} | EODHD {counts['EODHD']} | "
                  f"AV {counts['AlphaVantage']} | missing {len(missing)}")
        if i < len(symbols):
            time.sleep(delay)

    endpoint = " ; ".join(
        e for e, n in ((EODHD_ENDPOINT, counts["EODHD"]), (AV_ENDPOINT, counts["AlphaVantage"]))
        if n)
    config.record_provenance(
        con, TABLE, "EODHD / Alpha Vantage", endpoint or EODHD_ENDPOINT,
        sum(counts.values()),
        f"Company master data. EODHD {counts['EODHD']}, Alpha Vantage fallback "
        f"{counts['AlphaVantage']}. Both use the Morningstar-style sector taxonomy; "
        f"values normalised to title case. Provider recorded per row in data_source.")
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    filled = con.execute("SELECT COUNT(sector) FROM companies").fetchone()[0]
    print(f"\n  sector coverage: {filled}/{total} ({100 * filled / total:.1f}%)")
    for src, n in con.execute(
            "SELECT COALESCE(data_source,'(none)'), COUNT(*) FROM companies GROUP BY 1"):
        print(f"    {src:14s} {n}")
    if missing:
        print(f"  no profile from either provider ({len(missing)}): {', '.join(missing)}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
