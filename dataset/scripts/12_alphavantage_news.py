"""Fetch news and sentiment per event from Alpha Vantage.

    python scripts/12_alphavantage_news.py [--lookback-days 7] [--max-items 15]

Source
------
Alpha Vantage ``GET /query?function=NEWS_SENTIMENT&tickers=&time_from=&time_to=``

One call per event. Each item carries ``title``, ``summary``, ``source``,
``time_published``, an overall sentiment, and a per-ticker block with
``relevance_score`` and ``ticker_sentiment_score``.

Point-in-time cut
-----------------
``time_to`` is set to the close of **t0**, the moment the forecast is made, so
nothing published afterwards can enter. ``time_from`` reaches back
``--lookback-days`` before the announcement to capture the run-up as well as the
reaction.

Timing relative to the announcement
-----------------------------------
Knowing *when* a story appeared relative to the report matters for
interpretation: a piece written before the numbers were public is anticipation,
one written after is reaction, and the two say different things even when the
sentiment score is identical. Each item is classified into ``relative_timing``:

    pre_announcement    published before the report was out
    post_announcement   published after
    ambiguous           within the uncertainty band around the release

The announcement time is inferred from ``before_after_market``, since the
provider gives the session but not a timestamp:

    BeforeMarket   assumed released by 09:30 ET on report_date
    DuringMarket   assumed intraday; the whole session is ambiguous
    AfterMarket    assumed released at 16:00 ET on report_date

**Timezone.** ``time_published`` is US Eastern, not UTC. This is not stated in
the provider documentation and was established empirically: for a sample of
after-close reporters, press-release items land at 16:01 and later on the
report date (e.g. "Nutanix Reports Fourth Quarter … Results" at 16:01), which is
only consistent with Eastern time — under UTC those stamps would fall in the
middle of the trading session. A ±``AMBIGUITY_MINUTES`` band around the assumed
release absorbs the residual uncertainty; re-verify this if the provider changes
the field.

None of this affects leakage into the label — the forecast is made after t0's
close, and ``time_to`` already enforces that. The classification exists so the
prompt can present the news honestly rather than as an undifferentiated blob.

Volume
------
Item counts vary by two orders of magnitude across events (3 to 225 in a
sample). ``--max-items`` keeps the top N by relevance so prompt size stays
predictable; the untruncated count is preserved in ``event_news_summary`` so it
is visible that truncation happened.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

TABLE = "event_news"
ENDPOINT = "/query?function=NEWS_SENTIMENT"

#: Minimum per-ticker relevance for an item to count as being about the company.
MIN_RELEVANCE = 0.3
#: Half-width of the uncertainty band around the assumed release time, minutes.
AMBIGUITY_MINUTES = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_news (
    symbol           TEXT NOT NULL,
    fiscal_date      TEXT NOT NULL,
    published_et     TEXT NOT NULL,   -- YYYY-MM-DD HH:MM:SS, US Eastern
    -- pre_announcement / post_announcement / ambiguous, see module docstring
    relative_timing  TEXT NOT NULL,
    title            TEXT,
    summary          TEXT,
    source           TEXT,
    url              TEXT,
    relevance        REAL,            -- provider relevance of this item to the ticker
    sentiment        REAL,            -- ticker-specific sentiment, -1 .. +1
    sentiment_label  TEXT,
    overall_sentiment REAL,           -- sentiment of the article as a whole
    topics           TEXT,            -- comma-separated provider topics
    PRIMARY KEY (symbol, fiscal_date, published_et, url),
    FOREIGN KEY (symbol) REFERENCES companies(symbol)
);
CREATE INDEX IF NOT EXISTS ix_news_event ON event_news(symbol, fiscal_date);

CREATE TABLE IF NOT EXISTS event_news_summary (
    symbol            TEXT NOT NULL,
    fiscal_date       TEXT NOT NULL,
    items_returned    INTEGER,   -- before truncation
    items_stored      INTEGER,
    n_pre             INTEGER,
    n_post            INTEGER,
    n_ambiguous       INTEGER,
    mean_sentiment    REAL,
    mean_sentiment_pre  REAL,
    mean_sentiment_post REAL,
    lookback_days     INTEGER,
    PRIMARY KEY (symbol, fiscal_date)
);
"""


def announcement_boundary(report_date: str, timing: str):
    """(lower, upper) Eastern timestamps bracketing the release, as 'YYYY-MM-DD HH:MM'.

    Items before *lower* are anticipation, after *upper* are reaction, between
    the two are ambiguous.
    """
    if timing == "BeforeMarket":
        centre = f"{report_date} 09:30"
    elif timing == "AfterMarket":
        centre = f"{report_date} 16:00"
    else:  # DuringMarket — the whole session is uncertain
        return f"{report_date} 09:30", f"{report_date} 16:00"
    return _shift(centre, -AMBIGUITY_MINUTES), _shift(centre, AMBIGUITY_MINUTES)


def _shift(stamp: str, minutes: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.strptime(stamp, "%Y-%m-%d %H:%M")
            + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


def classify(published: str, lower: str, upper: str) -> str:
    if published < lower:
        return "pre_announcement"
    if published > upper:
        return "post_announcement"
    return "ambiguous"


def rename_aliases() -> dict[str, tuple[str, str]]:
    """canonical symbol -> (old symbol, rename date), from ticker_renames.csv.

    News is filed under whichever ticker was live when the story ran, so a
    renamed company's earlier coverage sits under the old symbol and querying
    the canonical one returns nothing. Verified: for the week of 2025-01-08,
    ``BK`` returns 3 relevant items and ``BNY`` returns 0; ``MMC`` returns 1 and
    ``MRSH`` returns 0. Prices, earnings and fundamentals are unaffected —
    those providers back-fill the full history under the new symbol.
    """
    import csv as _csv

    path = config.THESIS_DIR / "ticker_renames.csv"
    if not path.exists():
        return {}
    rows = _csv.DictReader(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#"))
    return {r["new_symbol"]: (r["old_symbol"], r["rename_date"]) for r in rows}


def query_symbol(symbol: str, t0: str, aliases: dict) -> str:
    """The ticker the story would have been filed under at *t0*."""
    alias = aliases.get(symbol)
    if alias and t0 < alias[1]:
        return alias[0]
    return symbol


def parse_stamp(raw: str) -> str | None:
    """'20250314T230902' -> '2025-03-14 23:09:02'."""
    if not raw or len(raw) < 15 or "T" not in raw:
        return None
    d, t = raw.split("T", 1)
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]} {t[0:2]}:{t[2:4]}:{t[4:6]}"


def main() -> int:
    ap = config.add_common_args(argparse.ArgumentParser(description=__doc__))
    ap.add_argument("--lookback-days", type=int, default=7,
                    help="Calendar days before report_date to start collecting.")
    ap.add_argument("--max-items", type=int, default=15,
                    help="Keep the top N items by relevance per event.")
    ap.add_argument("--rpm", type=int, default=70)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    con = config.connect()
    con.executescript(SCHEMA)
    if not args.resume:
        # Idempotent full run: clear what a previous pass of this script wrote.
        con.execute("DELETE FROM event_news")
        con.execute("DELETE FROM event_news_summary")
    con.execute("DELETE FROM provenance WHERE table_name = ?", (TABLE,))

    events = con.execute(
        """SELECT symbol, fiscal_date, report_date, before_after_market, t0
             FROM events ORDER BY t0""").fetchall()
    if args.resume:
        done = {(r[0], r[1]) for r in con.execute(
            "SELECT symbol, fiscal_date FROM event_news_summary")}
        events = [e for e in events if (e[0], e[1]) not in done]
        print(f"  resume: {len(done)} events done, {len(events)} remaining")
    if args.limit:
        events = events[: args.limit]

    aliases = rename_aliases()
    delay = 60.0 / max(args.rpm, 1)
    print(f"[12] news | {len(events)} events | lookback {args.lookback_days}d "
          f"| max {args.max_items} items/event")
    if aliases:
        print(f"  rename aliases active for: {', '.join(sorted(aliases))}")

    stored = failed = empty = 0
    for i, (sym, fiscal, report, timing, t0) in enumerate(events, 1):
        start = _minus_days(report, args.lookback_days).replace("-", "")
        ticker = query_symbol(sym, t0, aliases)
        try:
            feed = config.alphavantage_news(
                ticker, f"{start}T0000", f"{t0.replace('-', '')}T2359")
        except config.ProviderError as exc:
            message = str(exc)
            if "rate limit" in message.lower() or "premium" in message.lower():
                con.commit()
                print(f"\n  ABORT at {sym} {fiscal} ({i}/{len(events)}): {message}")
                print("  Partial data committed. Re-run with --resume once quota allows.")
                break
            failed += 1
            config.record_exclusion(con, TABLE, sym, fiscal, "provider_error", message[:200])
            continue

        lower, upper = announcement_boundary(report, timing)
        items = []
        for it in feed:
            published = parse_stamp(it.get("time_published", ""))
            if not published:
                continue
            rel = sent = None
            label = None
            for ts in it.get("ticker_sentiment", []):
                if ts.get("ticker") == ticker:
                    rel = config.to_float(ts.get("relevance_score"))
                    sent = config.to_float(ts.get("ticker_sentiment_score"))
                    label = ts.get("ticker_sentiment_label")
                    break
            if rel is None or rel < MIN_RELEVANCE:
                continue
            items.append((
                sym, fiscal, published, classify(published, lower, upper),
                it.get("title"), it.get("summary"), it.get("source"), it.get("url"),
                rel, sent, label, config.to_float(it.get("overall_sentiment_score")),
                ",".join(t.get("topic", "") for t in it.get("topics", [])),
            ))

        returned = len(items)
        items.sort(key=lambda r: -(r[8] or 0))
        items = items[: args.max_items]
        if not items:
            empty += 1

        con.executemany(
            f"INSERT OR REPLACE INTO event_news VALUES ({','.join('?' * 13)})", items)

        sents = [r[9] for r in items if r[9] is not None]
        pre = [r[9] for r in items if r[3] == "pre_announcement" and r[9] is not None]
        post = [r[9] for r in items if r[3] == "post_announcement" and r[9] is not None]
        con.execute(
            "INSERT OR REPLACE INTO event_news_summary VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sym, fiscal, returned, len(items),
             sum(1 for r in items if r[3] == "pre_announcement"),
             sum(1 for r in items if r[3] == "post_announcement"),
             sum(1 for r in items if r[3] == "ambiguous"),
             sum(sents) / len(sents) if sents else None,
             sum(pre) / len(pre) if pre else None,
             sum(post) / len(post) if post else None,
             args.lookback_days))
        stored += len(items)

        if i % 25 == 0:
            con.commit()
            print(f"  {i}/{len(events)} | items={stored} | empty={empty} | failed={failed}")
        if i < len(events):
            time.sleep(delay)

    config.record_provenance(
        con, TABLE, "Alpha Vantage", ENDPOINT, stored,
        f"News per event, cut at the close of t0. Lookback {args.lookback_days} days, "
        f"top {args.max_items} by relevance, minimum relevance {MIN_RELEVANCE}. "
        f"time_published is US Eastern (established empirically, see script docstring); "
        f"items within {AMBIGUITY_MINUTES} min of the assumed release are marked ambiguous.")
    con.commit()

    n_ev = con.execute("SELECT COUNT(*) FROM event_news_summary").fetchone()[0]
    n_it = con.execute("SELECT COUNT(*) FROM event_news").fetchone()[0]
    print(f"\n  events with news : {n_ev} | items stored: {n_it} | no relevant news: {empty}")
    if n_it:
        print("  timing split:")
        for t, c in con.execute(
                "SELECT relative_timing, COUNT(*) FROM event_news GROUP BY 1 ORDER BY 2 DESC"):
            print(f"    {t:18s} {c:6d} ({100 * c / n_it:.1f}%)")
        trunc = con.execute(
            "SELECT COUNT(*) FROM event_news_summary WHERE items_returned > items_stored"
        ).fetchone()[0]
        print(f"  events truncated by --max-items: {trunc}")
    con.close()
    return 0


def _minus_days(iso: str, days: int) -> str:
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso.split("-"))
    return (date(y, m, d) - timedelta(days=days)).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
