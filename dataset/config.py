"""Shared configuration and provider access layer for the thesis dataset.

Every ingestion script imports from here, so the study window, the constituent
universe and the provider endpoints are each defined in exactly one place.

Reproducing the dataset requires two API subscriptions:

    EODHD            earnings calendar, daily prices, company master data
    Alpha Vantage    income statements

Keys are read from a ``.env`` file next to this module (or from the
environment) and are never hard-coded or committed:

    EODHD_API_KEY=...
    ALPHA_VANTAGE_API_KEY=...
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Study design ────────────────────────────────────────────────────

#: Start of the study window (inclusive). Events are drawn from this range.
START_DATE = "2025-01-01"
#: End of the study window (inclusive).
END_DATE = "2026-06-30"

#: Price history is fetched from further back than the event window so that
#: trailing features (12-month momentum, 252-day volatility, 52-week range) are
#: defined for events at the very start of the window. Five years also covers a
#: full market cycle, which matters for percentile-style regime features.
#: Costs nothing extra: the EODHD price endpoint is one call per symbol
#: regardless of how much history the call requests.
PRICE_HISTORY_FROM = "2020-01-01"

THESIS_DIR = Path(__file__).resolve().parent

#: The assembled database.
DB_PATH = THESIS_DIR / "thesis.db"

#: Constituent universe (see README for the point-in-time caveat).
CONSTITUENTS_FILE = THESIS_DIR / "sp500_constituents.txt"


def constituents() -> list[str]:
    """Return the S&P 500 constituent symbols used throughout the study."""
    return [
        line.strip()
        for line in CONSTITUENTS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Credentials ─────────────────────────────────────────────────────


def api_key(name: str) -> str:
    """Read an API key from ``.env`` (this directory, then the parent) or the
    environment."""
    for env in (THESIS_DIR / ".env", THESIS_DIR.parent / ".env"):
        if env.exists():
            for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith(f"{name}=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    if os.environ.get(name):
        return os.environ[name]
    raise SystemExit(
        f"{name} not found.\n"
        f"Add it to {THESIS_DIR / '.env'} as  {name}=your_key  or export it."
    )


# ── HTTP layer ──────────────────────────────────────────────────────


class ProviderError(RuntimeError):
    """A provider returned an error that the caller must handle."""


class NotFound(ProviderError):
    """The provider has no data for this symbol (HTTP 404)."""


def _redact(url: str) -> str:
    """Strip credentials from a URL before it appears in a log or traceback."""
    return re.sub(r"((?:api_token|apikey)=)[^&]+", r"\1***", url)


def http_json(url: str, timeout: int = 90, retries: int = 3, backoff: float = 2.0):
    """GET *url* and parse the JSON body, with bounded retries.

    Retries transient conditions (timeouts, connection resets, 429, 5xx).
    Surfaces 401/402/403 immediately — those indicate a subscription problem
    that retrying cannot fix — and 404 as :class:`NotFound`.
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "thesis-data-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read()[:300].decode("utf-8", "replace")
            if exc.code == 404:
                raise NotFound(f"404 for {_redact(url)}") from exc
            if exc.code in (401, 402, 403):
                raise ProviderError(
                    f"HTTP {exc.code} — the API key lacks entitlement for this endpoint.\n"
                    f"  URL : {_redact(url)}\n  Body: {body}"
                ) from exc
            last = ProviderError(f"HTTP {exc.code}: {body}")
        except Exception as exc:
            last = ProviderError(f"{type(exc).__name__}: {exc}")
        if attempt < retries - 1:
            time.sleep(backoff * (2**attempt))
    raise last  # type: ignore[misc]


# ── EODHD ───────────────────────────────────────────────────────────

EODHD_BASE = "https://eodhd.com/api"


def eodhd_earnings_calendar(date_from: str, date_to: str) -> list[dict]:
    """``GET /api/calendar/earnings?from=&to=`` — market-wide earnings calendar.

    One call returns every company reporting in the range, so the study window
    costs ~6 calls rather than one per constituent.

    Response records carry::

        code                 symbol with exchange suffix, e.g. "AAPL.US"
        report_date          announcement date
        date                 end of the reported fiscal quarter
        actual               reported EPS
        estimate             consensus EPS estimate
        percent              surprise, in percent of the estimate
        before_after_market  BeforeMarket / AfterMarket / DuringMarket
    """
    token = api_key("EODHD_API_KEY")
    url = (f"{EODHD_BASE}/calendar/earnings?api_token={token}&fmt=json"
           f"&from={date_from}&to={date_to}")
    payload = http_json(url)
    return payload.get("earnings", []) if isinstance(payload, dict) else (payload or [])


def eodhd_eod_prices(symbol: str, date_from: str, date_to: str,
                     exchange: str = "US") -> list[dict]:
    """``GET /api/eod/{SYMBOL}.{EXCHANGE}`` — daily bars.

    Returns ``date, open, high, low, close, adjusted_close, volume``.
    ``close`` is raw; ``adjusted_close`` is adjusted for splits and dividends.
    Raises :class:`NotFound` when the provider has no series for the symbol.
    """
    token = api_key("EODHD_API_KEY")
    url = (f"{EODHD_BASE}/eod/{urllib.parse.quote(symbol)}.{exchange}"
           f"?api_token={token}&fmt=json&period=d&from={date_from}&to={date_to}")
    return http_json(url) or []


def eodhd_fundamentals_general(symbol: str, exchange: str = "US") -> dict:
    """``GET /api/fundamentals/{SYMBOL}.{EXCHANGE}?filter=General`` — master data.

    Returns ``Name``, ``Sector``, ``Industry``, ``CountryISO``, ``ISIN``,
    ``CIK``, ``FiscalYearEnd``, ``IPODate``.  The ``filter`` keeps the response
    small; the full document is large and unnecessary here.
    """
    token = api_key("EODHD_API_KEY")
    url = (f"{EODHD_BASE}/fundamentals/{urllib.parse.quote(symbol)}.{exchange}"
           f"?api_token={token}&fmt=json&filter=General")
    return http_json(url) or {}


# ── Alpha Vantage ───────────────────────────────────────────────────

ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"

#: Prose bodies Alpha Vantage returns for transient conditions rather than for
#: a genuinely bad request. Observed: the same symbol succeeds on retry.
_AV_TRANSIENT = ("invalid api call", "please retry", "higher api call frequency")


def alphavantage_income_statement(symbol: str, retries: int = 3) -> dict:
    """``GET /query?function=INCOME_STATEMENT&symbol=`` — income statements.

    Returns ``annualReports`` and ``quarterlyReports``; each period carries
    ``fiscalDateEnding``, ``reportedCurrency``, ``totalRevenue``, ``netIncome``,
    ``operatingIncome``, ``ebit``, ``ebitda``, ``grossProfit`` and further line
    items.

    Alpha Vantage signals both throttling and transient load-shedding with
    HTTP 200 and a prose body, the latter indistinguishable at a glance from an
    unknown symbol.  Transient bodies are retried with backoff; the endpoint is
    read-only, so retrying is safe.
    """
    key = api_key("ALPHA_VANTAGE_API_KEY")
    url = (f"{ALPHAVANTAGE_BASE}?function=INCOME_STATEMENT"
           f"&symbol={urllib.parse.quote(symbol)}&apikey={key}")
    last: Exception | None = None
    for attempt in range(retries):
        payload = http_json(url)
        try:
            _raise_on_alphavantage_message(payload)
            return payload
        except ProviderError as exc:
            last = exc
            if not _is_transient_alphavantage(payload):
                raise
            time.sleep(2.0 * (attempt + 1))
    raise last  # type: ignore[misc]


def alphavantage_daily_adjusted(symbol: str, full: bool = True) -> dict:
    """``GET /query?function=TIME_SERIES_DAILY_ADJUSTED&symbol=`` — daily bars.

    Used for the market benchmark (SPY).  Returns ``open, high, low, close,
    adjusted close, volume, dividend amount, split coefficient`` per day.
    Premium endpoint.
    """
    key = api_key("ALPHA_VANTAGE_API_KEY")
    url = (f"{ALPHAVANTAGE_BASE}?function=TIME_SERIES_DAILY_ADJUSTED"
           f"&symbol={urllib.parse.quote(symbol)}"
           f"&outputsize={'full' if full else 'compact'}&apikey={key}")
    payload = http_json(url)
    _raise_on_alphavantage_message(payload)
    series = next((v for k, v in payload.items() if "Time Series" in k), None)
    if series is None:
        raise ProviderError(f"no time series in response for {symbol}: {str(payload)[:200]}")
    return series


def alphavantage_news(ticker: str, time_from: str, time_to: str,
                      limit: int = 1000, retries: int = 3) -> list[dict]:
    """``GET /query?function=NEWS_SENTIMENT`` — news with sentiment for one ticker.

    ``time_from`` / ``time_to`` take the provider's ``YYYYMMDDTHHMM`` form and
    bound ``time_published``, which makes an exact point-in-time cut possible.

    Note: ``time_published`` is US Eastern, not UTC — established empirically,
    see ``12_alphavantage_news.py``.
    """
    key = api_key("ALPHA_VANTAGE_API_KEY")
    url = (f"{ALPHAVANTAGE_BASE}?function=NEWS_SENTIMENT"
           f"&tickers={urllib.parse.quote(ticker)}"
           f"&time_from={time_from}&time_to={time_to}"
           f"&limit={limit}&sort=RELEVANCE&apikey={key}")
    last: Exception | None = None
    for attempt in range(retries):
        payload = http_json(url)
        try:
            _raise_on_alphavantage_message(payload)
            return payload.get("feed", []) or []
        except ProviderError as exc:
            last = exc
            if not _is_transient_alphavantage(payload):
                raise
            time.sleep(2.0 * (attempt + 1))
    raise last  # type: ignore[misc]


def alphavantage_overview(symbol: str) -> dict:
    """``GET /query?function=OVERVIEW&symbol=`` — company profile.

    Used only for ``Sector`` / ``Industry`` and identifiers. The market-cap and
    share-count fields it also returns are a *current snapshot* and must not be
    attached to a historical event — see ``08_alphavantage_shares_outstanding``.
    Returns ``{}`` for delisted symbols.
    """
    key = api_key("ALPHA_VANTAGE_API_KEY")
    url = (f"{ALPHAVANTAGE_BASE}?function=OVERVIEW"
           f"&symbol={urllib.parse.quote(symbol)}&apikey={key}")
    payload = http_json(url)
    _raise_on_alphavantage_message(payload)
    return payload or {}


def alphavantage_macro(function: str, **params) -> list[dict]:
    """``GET /query?function={CPI|FEDERAL_FUNDS_RATE|TREASURY_YIELD|...}``.

    Returns the ``data`` list of ``{date, value}`` observations.
    """
    key = api_key("ALPHA_VANTAGE_API_KEY")
    extra = "".join(f"&{k}={v}" for k, v in params.items())
    payload = http_json(f"{ALPHAVANTAGE_BASE}?function={function}{extra}&apikey={key}")
    _raise_on_alphavantage_message(payload)
    return payload.get("data", []) or []


def alphavantage_balance_sheet(symbol: str, retries: int = 3) -> dict:
    """``GET /query?function=BALANCE_SHEET&symbol=`` — quarterly balance sheets.

    Used here only for ``commonStockSharesOutstanding``, the share count needed
    to derive market capitalisation.  Same transient-error handling as
    :func:`alphavantage_income_statement`.
    """
    key = api_key("ALPHA_VANTAGE_API_KEY")
    url = (f"{ALPHAVANTAGE_BASE}?function=BALANCE_SHEET"
           f"&symbol={urllib.parse.quote(symbol)}&apikey={key}")
    last: Exception | None = None
    for attempt in range(retries):
        payload = http_json(url)
        try:
            _raise_on_alphavantage_message(payload)
            return payload
        except ProviderError as exc:
            last = exc
            if not _is_transient_alphavantage(payload):
                raise
            time.sleep(2.0 * (attempt + 1))
    raise last  # type: ignore[misc]


def _is_transient_alphavantage(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    text = " ".join(str(payload.get(f, "")) for f in ("Note", "Information", "Error Message"))
    return any(marker in text.lower() for marker in _AV_TRANSIENT)


def _raise_on_alphavantage_message(payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    for field in ("Note", "Information", "Error Message"):
        if field in payload:
            raise ProviderError(f"Alpha Vantage {field}: {payload[field]}")


# ── Numeric / date helpers ──────────────────────────────────────────


def to_float(value) -> float | None:
    """Parse a provider numeric field. Alpha Vantage encodes NULL as ``"None"``."""
    if value is None or value in ("", "None", "-", "NaN"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def iso_date(value) -> str | None:
    """Normalise a provider date to ``YYYY-MM-DD``."""
    if value is None:
        return None
    s = str(value).strip()
    return s[:10] if s else None


def adjust_ohlc(o, h, l, close, adj_close):
    """Scale open/high/low onto the adjusted-close basis.

    Providers adjust only the close.  Open/high/low arrive on the raw basis, so
    a series mixing them is internally inconsistent across any split.  The
    correct treatment is to apply the same factor the close received::

        factor = adjusted_close / close
    """
    if not close or adj_close is None:
        return o, h, l
    factor = adj_close / close
    scale = lambda x: round(x * factor, 6) if x is not None else None
    return scale(o), scale(h), scale(l)


# ── Database ────────────────────────────────────────────────────────


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA foreign_keys=ON")
    return con


def record_provenance(con, table, source_api, endpoint, rows, notes=""):
    """Persist which endpoint populated which table."""
    con.execute(
        "INSERT INTO provenance (table_name, source_api, endpoint, rows_written, "
        "run_utc, notes) VALUES (?,?,?,?,?,?)",
        (table, source_api, endpoint, rows, utcnow(), notes),
    )


def record_exclusion(con, table, symbol, ref, reason, detail=""):
    """Log a deliberately dropped record so no filtering is silent."""
    con.execute(
        "INSERT INTO data_exclusions (table_name, symbol, ref, reason, detail, run_utc) "
        "VALUES (?,?,?,?,?,?)",
        (table, symbol, ref, reason, detail, utcnow()),
    )


def add_common_args(parser):
    parser.add_argument("--start", default=START_DATE, help="Window start (YYYY-MM-DD).")
    parser.add_argument("--end", default=END_DATE, help="Window end (YYYY-MM-DD).")
    parser.add_argument("--limit", type=int, help="Process only the first N symbols.")
    return parser
