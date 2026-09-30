"""Pull the S&P 500 membership history from EODHD and reconcile it with the
curated membership inputs of this dataset.

    python scripts/29_eodhd_index_membership.py                 # fetch + report + emit
    python scripts/29_eodhd_index_membership.py --offline       # reuse the cached response
    python scripts/29_eodhd_index_membership.py --no-emit       # report only
    python scripts/29_eodhd_index_membership.py --replace       # make EODHD the build input

Why this script exists
----------------------
The point-in-time membership used by ``09_build_events.py`` rests on two
curated files: ``sp500_constituents.txt`` (the 530-symbol working list; the
companies without an addition row are treated as members from the window
start) and ``sp500_index_changes.csv`` (59 additions and removals, dated from
S&P Dow Jones Indices press releases). Both files were built before the
experiment from the SPY holdings and press-release research (README section
7.1); the window-start membership was checked afterwards by
``31_membership_anchor_rollback.py`` and ``32_membership_historical_list_check.py``.

EODHD's index fundamentals carry that history for the S&P 500 since April
2012. This script fetches it, turns it into the same two input files, and
reports where the provider agrees or disagrees with what the study used.
Somebody reproducing the dataset can therefore take membership from the data
provider with one call instead of re-doing the press-release research, and
the result lands in ``index_membership`` in exactly the shape the study used,
because the builder and the schema are untouched: only its *inputs* are
produced here.

Source
------
EODHD ``GET /api/fundamentals/GSPC.INDX?filter=HistoricalTickerComponents``

One call. The response is a mapping of index -> record with::

    Code          constituent ticker, e.g. "AAPL" (class shares as "BRK-B")
    Name          company name
    StartDate     date the ticker was added to the index
    EndDate       date the ticker was removed, or null while still a member
    IsActiveNow   1 while a current member
    IsDelisted    1 if the security has since been delisted

Coverage begins 2012-04-04 (earlier StartDates exist for long-standing
members; tickers that left before 2012 are absent). The provider does not
name its upstream source, so the S&P DJI press releases in
``sp500_index_changes.csv`` remain the citable primary source; EODHD is a
second, independent compilation. Requires a plan that includes the
Fundamentals API (a 401 means the key lacks that entitlement).

Date convention (an assumption this script makes visible)
---------------------------------------------------------
The curated file records the *effective* date: the first trading day on
which the change is in force ("prior to the open of trading on ..."). This
script assumes EODHD's ``StartDate``/``EndDate`` follow the same convention.
The report prints the day-difference distribution against the verified
press-release dates separately for additions and removals; a systematic
+/-1 shift there would mean the provider records the last day *as* a member
instead, and ``--end-date-shift`` corrects for it.

What is written
---------------
``eodhd_sp500_membership.json``   the raw response (cache; ``--offline`` reads it)
``sp500_constituents.eodhd.txt``  the union universe derived from EODHD
``sp500_index_changes.eodhd.csv`` the in-window changes derived from EODHD

Both derived files use the curated files' exact format. A derived change row
is marked ``verified`` and carries the curated note when the curated file has
the same symbol, action and date confirmed against a press release; otherwise
``aggregator`` (the header's definition: a secondary compilation, date not
independently confirmed) with a note naming EODHD. ``--replace`` copies the
derived files over the curated ones (originals kept as ``*.pre-eodhd.bak``)
so that a fresh ``run_all.py`` builds membership from EODHD. Nothing in this
script writes to ``thesis.db``; it opens the database read-only for the
comparison.

Ticker handling
---------------
Renames in ``ticker_renames.csv`` are applied (old -> new) and the two
records merged, because the provider models a rename as a removal plus an
addition. Class shares are normalised to the hyphen form the study uses.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
import sys
import urllib.parse
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402

ENDPOINT = "/fundamentals/GSPC.INDX?filter=HistoricalTickerComponents"
RAW_CACHE = config.THESIS_DIR / "eodhd_sp500_membership.json"
CURATED_CONSTITUENTS = config.CONSTITUENTS_FILE
CURATED_CHANGES = config.THESIS_DIR / "sp500_index_changes.csv"
RENAMES = config.THESIS_DIR / "ticker_renames.csv"
DERIVED_CONSTITUENTS = config.THESIS_DIR / "sp500_constituents.eodhd.txt"
DERIVED_CHANGES = config.THESIS_DIR / "sp500_index_changes.eodhd.csv"
SAMPLE_FILE = config.THESIS_DIR / "event_sample.txt"

#: The S&P 500 holds 500 companies as ~503 tickers (multi-class listings).
EXPECTED_TICKERS = 503


# ── small helpers ───────────────────────────────────────────────────


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _read_csv_rows(path: Path) -> list[dict]:
    """Rows of a curated CSV whose header comment lines start with ``#``."""
    if not path.exists():
        return []
    return list(csv.DictReader(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")))


def _read_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def _norm_symbol(code: str) -> str:
    """EODHD ticker -> the study's symbol form (``BRK-B``, no exchange suffix)."""
    code = code.strip().upper()
    if code.endswith(".US"):
        code = code[:-3]
    return code.replace(".", "-")


# ── fetch ───────────────────────────────────────────────────────────


def fetch(cache: Path, offline: bool) -> dict:
    """Return the raw provider payload, from the cache or the API."""
    if offline:
        if not cache.exists():
            raise SystemExit(f"--offline but no cache at {cache}")
        return json.loads(cache.read_text(encoding="utf-8"))
    token = config.api_key("EODHD_API_KEY")
    url = f"{config.EODHD_BASE}{ENDPOINT}&api_token={urllib.parse.quote(token)}&fmt=json"
    try:
        payload = config.http_json(url)
    except config.ProviderError as exc:
        raise SystemExit(
            f"EODHD refused the request.\n  {exc}\n"
            "  The index-constituent history needs a plan that includes the "
            "Fundamentals API (Fundamentals or All-In-One).\n"
            f"  If {cache.name} exists from an earlier run, use --offline.") from exc
    wrapped = {
        "endpoint": ENDPOINT,
        "fetched_utc": config.utcnow(),
        "HistoricalTickerComponents": payload,
    }
    cache.write_text(json.dumps(wrapped, indent=1), encoding="utf-8")
    print(f"  cached raw response -> {cache.name}")
    return wrapped


def parse_records(raw: dict) -> list[dict]:
    """Flatten the provider payload into ``{symbol, name, start, end, active, delisted}``."""
    body = raw.get("HistoricalTickerComponents", raw)
    if isinstance(body, dict) and "HistoricalTickerComponents" in body:
        body = body["HistoricalTickerComponents"]
    items = list(body.values()) if isinstance(body, dict) else list(body or [])
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("Code"):
            continue
        out.append({
            "symbol": _norm_symbol(str(it["Code"])),
            "name": (it.get("Name") or "").strip(),
            "start": _d(it.get("StartDate")),
            "end": _d(it.get("EndDate")),
            "active": bool(int(it.get("IsActiveNow") or 0)),
            "delisted": bool(int(it.get("IsDelisted") or 0)),
        })
    if not out:
        raise SystemExit("the response holds no constituent records - inspect the cache")
    return out


# ── derive ──────────────────────────────────────────────────────────


def load_renames() -> dict[str, str]:
    return {r["old_symbol"].strip(): r["new_symbol"].strip() for r in _read_csv_rows(RENAMES)}


def merge_records(records: list[dict], renames: dict[str, str],
                  end_shift: int) -> dict[str, dict]:
    """One membership span per study symbol.

    Renamed tickers are folded onto the canonical symbol (earliest start,
    latest end, open-ended if either is). A ticker with several spans (left
    and rejoined) keeps every span in ``spans``; the summary fields describe
    the span that touches the study window when one does.
    """
    by_sym: dict[str, list[dict]] = {}
    for r in records:
        sym = renames.get(r["symbol"], r["symbol"])
        end = r["end"] + timedelta(days=end_shift) if r["end"] and end_shift else r["end"]
        by_sym.setdefault(sym, []).append({**r, "symbol": sym, "end": end})

    start_w, end_w = _d(config.START_DATE), _d(config.END_DATE)
    merged: dict[str, dict] = {}
    for sym, spans in by_sym.items():
        spans.sort(key=lambda s: (s["start"] or date.min))
        # fold rename halves: spans that abut (gap <= 7 days) are one membership
        folded: list[dict] = []
        for s in spans:
            if folded and folded[-1]["end"] and s["start"] and \
                    0 <= (s["start"] - folded[-1]["end"]).days <= 7:
                folded[-1]["end"] = s["end"]
                folded[-1]["name"] = folded[-1]["name"] or s["name"]
                folded[-1]["active"] = folded[-1]["active"] or s["active"]
                continue
            folded.append(dict(s))
        in_window = [s for s in folded
                     if (s["start"] is None or s["start"] <= end_w)
                     and (s["end"] is None or s["end"] > start_w)]
        pick = in_window[-1] if in_window else folded[-1]
        merged[sym] = {**pick, "spans": folded, "touches_window": bool(in_window),
                       "multi_span": len(folded) > 1}
    return merged


def derive(merged: dict[str, dict]) -> tuple[list[str], list[dict], list[str]]:
    """(union universe, in-window change rows, members at the window start)."""
    start_w, end_w = _d(config.START_DATE), _d(config.END_DATE)
    universe, changes, base = [], [], []
    for sym, m in sorted(merged.items()):
        if not m["touches_window"]:
            continue
        universe.append(sym)
        for s in m["spans"]:
            if s["start"] and start_w <= s["start"] <= end_w:
                changes.append({"effective_date": _iso(s["start"]), "action": "add",
                                "symbol": sym, "company": s["name"]})
            if s["end"] and start_w <= s["end"] <= end_w:
                changes.append({"effective_date": _iso(s["end"]), "action": "remove",
                                "symbol": sym, "company": s["name"]})
            if (s["start"] is None or s["start"] < start_w) and \
                    (s["end"] is None or s["end"] >= start_w):
                base.append(sym)
    changes.sort(key=lambda r: (r["effective_date"], r["action"] != "add", r["symbol"]))
    return universe, changes, sorted(set(base))


def members_on(merged: dict[str, dict], day: date) -> set[str]:
    return {sym for sym, m in merged.items()
            for s in m["spans"]
            if (s["start"] is None or s["start"] <= day) and (s["end"] is None or s["end"] > day)}


# ── compare ─────────────────────────────────────────────────────────


def _section(title: str) -> None:
    print(f"\n{'-' * 68}\n{title}\n{'-' * 68}")


def _list(label: str, items, limit: int = 40) -> None:
    items = sorted(items)
    print(f"  {label}: {len(items)}" + (f"  {', '.join(map(str, items[:limit]))}"
                                        + (" ..." if len(items) > limit else "")
                                        if items else ""))


def compare(merged: dict[str, dict], universe: list[str], changes: list[dict],
            base: list[str]) -> dict:
    """Print the reconciliation report; return the match map used when emitting."""
    start_w, end_w = _d(config.START_DATE), _d(config.END_DATE)
    cur_universe = set(_read_list(CURATED_CONSTITUENTS))
    cur_rows = _read_csv_rows(CURATED_CHANGES)
    cur_key = {(r["symbol"], r["action"]): r for r in cur_rows}

    _section("EODHD response")
    all_spans = [s for m in merged.values() for s in m["spans"]]
    starts = [s["start"] for s in all_spans if s["start"]]
    print(f"  tickers (after rename folding) : {len(merged)}")
    print(f"  spans                          : {len(all_spans)}")
    print(f"  earliest StartDate             : {min(starts) if starts else '-'}")
    print(f"  currently active (provider)    : {sum(m['active'] for m in merged.values())}")
    print(f"  touching the study window      : {len(universe)}")
    multi = [s for s, m in merged.items() if m["multi_span"] and m["touches_window"]]
    _list("window tickers with several spans (left and rejoined)", multi)

    _section(f"Members on {config.START_DATE} (the dataset's window-start membership)")
    cur_added = {r["symbol"] for r in cur_rows if r["action"] == "add"}
    cur_base = cur_universe - cur_added
    eodhd_base = set(base)
    print(f"  EODHD members at window start  : {len(eodhd_base)}  (expected ~{EXPECTED_TICKERS})")
    print(f"  curated 'assumed at start'     : {len(cur_base)}")
    _list("in EODHD base but not in curated base", eodhd_base - cur_base)
    _list("in curated base but not in EODHD base", cur_base - eodhd_base)

    _section("Union universe over the window")
    _list("in EODHD universe but not in sp500_constituents.txt", set(universe) - cur_universe)
    _list("in sp500_constituents.txt but not in EODHD universe", cur_universe - set(universe))

    _section("Changes inside the window")
    matches: dict[tuple[str, str], dict] = {}
    exact, shifted, only_eodhd = [], [], []
    deltas = {"add": [], "remove": []}
    for r in changes:
        key = (r["symbol"], r["action"])
        c = cur_key.get(key)
        if c is None:
            only_eodhd.append(f"{r['effective_date']} {r['action']} {r['symbol']}")
            continue
        delta = (_d(r["effective_date"]) - _d(c["effective_date"])).days
        deltas[r["action"]].append(delta)
        if delta == 0:
            exact.append(key)
            matches[key] = c
        else:
            shifted.append(f"{r['symbol']} {r['action']}: EODHD {r['effective_date']} vs "
                           f"curated {c['effective_date']} ({c['confidence']}) "
                           f"delta {delta:+d} d")
    only_curated = [f"{c['effective_date']} {c['action']} {c['symbol']} ({c['confidence']})"
                    for k, c in cur_key.items() if k not in {(r["symbol"], r["action"]) for r in changes}]
    print(f"  EODHD rows in window           : {len(changes)} "
          f"({sum(r['action'] == 'add' for r in changes)} add, "
          f"{sum(r['action'] == 'remove' for r in changes)} remove)")
    print(f"  curated rows                   : {len(cur_rows)} "
          f"({sum(r['action'] == 'add' for r in cur_rows)} add, "
          f"{sum(r['action'] == 'remove' for r in cur_rows)} remove)")
    print(f"  same symbol/action/date        : {len(exact)}")
    print(f"  same symbol/action, other date : {len(shifted)}")
    for s in shifted:
        print(f"      {s}")
    _list("only in EODHD", only_eodhd)
    _list("only in curated file", only_curated)
    for action in ("add", "remove"):
        if deltas[action]:
            print(f"  day delta EODHD-curated ({action:6s}): "
                  f"{dict(sorted(Counter(deltas[action]).items()))}")

    _section("Versus index_membership in thesis.db (read-only)")
    try:
        con = config.connect(readonly=True)
    except Exception as exc:  # pragma: no cover - no database yet
        print(f"  (no database: {exc})")
        con = None
    if con is not None:
        db = {r[0]: r[1:] for r in con.execute(
            "SELECT symbol, in_index_from, in_index_to, from_source, to_source "
            "FROM index_membership")}
        if not db:
            print("  index_membership is empty")
        else:
            agree = from_diff = to_diff = to_price = 0
            details = []
            for sym, (f, t, fs, ts) in sorted(db.items()):
                m = merged.get(sym)
                if m is None or not m["touches_window"]:
                    details.append(f"{sym}: in DB, not in EODHD window universe")
                    continue
                span = m
                e_from = _iso(span["start"]) if span["start"] and span["start"] >= start_w else None
                e_to = _iso(span["end"]) if span["end"] and span["end"] <= end_w else None
                ok = True
                if e_from != f:
                    from_diff += 1
                    ok = False
                    details.append(f"{sym}: from DB {f} ({fs}) vs EODHD {e_from}")
                if e_to != t:
                    if ts == "price_series" and t and e_to and t < e_to:
                        to_price += 1  # expected: the security stopped trading first
                    else:
                        to_diff += 1
                        ok = False
                        details.append(f"{sym}: to DB {t} ({ts}) vs EODHD {e_to}")
                agree += ok
            print(f"  symbols agreeing on both dates : {agree} / {len(db)}")
            print(f"  in_index_from differs          : {from_diff}")
            print(f"  in_index_to differs            : {to_diff}")
            print(f"  DB earlier via price series    : {to_price} (expected, not a conflict)")
            for d in details[:60]:
                print(f"      {d}")
            if len(details) > 60:
                print(f"      ... {len(details) - 60} more")

            # Would the eligibility flag of any event change under EODHD dates?
            ev = con.execute("SELECT symbol, report_date, t0, in_index_at_t0 FROM events "
                             "WHERE t0 > '2025-04-28'").fetchall()
            sample = {(r["symbol"], r["report_date"]) for r in _read_csv_rows(SAMPLE_FILE)}
            flips, flips_sample = [], []
            for sym, rd, t0, flag in ev:
                m = merged.get(sym)
                e_flag = int(m is not None and any(
                    (s["start"] is None or _iso(s["start"]) <= t0)
                    and (s["end"] is None or _iso(s["end"]) > t0) for s in m["spans"]))
                if e_flag != flag:
                    flips.append(f"{sym} t0={t0} DB={flag} EODHD={e_flag}")
                    if (sym, rd) in sample:
                        flips_sample.append(f"{sym} {rd}")
            print(f"  post-cutoff events             : {len(ev)}")
            _list("events whose in_index_at_t0 would flip under EODHD dates", flips)
            _list("of which in the 840-event sample", flips_sample)
        con.close()

    _section("Daily ticker count reconstructed from EODHD")
    counts = []
    day = start_w
    while day <= end_w:
        counts.append(len(members_on(merged, day)))
        day += timedelta(days=1)
    print(f"  min {min(counts)} / median {int(statistics.median(counts))} / max {max(counts)} "
          f"over {len(counts)} days (true index ~{EXPECTED_TICKERS})")
    return matches


# ── emit ────────────────────────────────────────────────────────────


def emit(universe: list[str], changes: list[dict], matches: dict, fetched_utc: str,
         end_shift: int) -> None:
    n_verified = 0
    lines = [
        "effective_date,action,symbol,company,confidence,note"]
    rows_out = []
    for r in changes:
        c = matches.get((r["symbol"], r["action"]))
        if c is not None and c.get("confidence") == "verified":
            conf, note = "verified", (c.get("note") or "")
            note = (note + "; " if note else "") + "date also given by EODHD"
            n_verified += 1
        else:
            conf, note = "aggregator", "EODHD HistoricalTickerComponents; not checked against a press release"
        rows_out.append([r["effective_date"], r["action"], r["symbol"], r["company"], conf, note])
    header = f"""# S&P 500 constituent changes during the study window ({config.START_DATE} .. {config.END_DATE}).
#
# DERIVED FROM EODHD by scripts/29_eodhd_index_membership.py.
# Source: EODHD {ENDPOINT} (fetched {fetched_utc}); raw response in
# eodhd_sp500_membership.json. EndDate shift applied: {end_shift:+d} day(s).
#
# Same format as sp500_index_changes.csv, so 09_build_events.py consumes it
# unchanged once copied over that file (--replace does this).
#
# confidence:
#   verified    symbol, action and date also appear in sp500_index_changes.csv
#               confirmed against an S&P Dow Jones Indices press release
#               ({n_verified} rows; the press-release URLs are in that file)
#   aggregator  date taken from EODHD only; not independently confirmed
#
# EODHD does not name its upstream source. The press releases remain the
# citable primary source; this file documents that the provider's compilation
# gives the same picture, and supplies the changes the curated file lacks.
"""
    DERIVED_CHANGES.write_text(header + "\n".join(lines + [",".join(
        '"' + v.replace('"', '""') + '"' if ("," in v or '"' in v) else v for v in row)
        for row in rows_out]) + "\n", encoding="utf-8")
    cheader = f"""# Every company that EODHD lists as an S&P 500 member at any point in
# {config.START_DATE} .. {config.END_DATE}. DERIVED FROM EODHD by
# scripts/29_eodhd_index_membership.py (fetched {fetched_utc}).
# Membership dates live in sp500_index_changes.eodhd.csv; an event only counts
# while the company was actually a member.
# Renamed tickers appear under their new symbol (see ticker_renames.csv).
# Symbols without price history inside the window (recent spin-offs) are kept
# here; 05_eodhd_daily_prices.py logs them as missing series, which is harmless.
"""
    DERIVED_CONSTITUENTS.write_text(cheader + "\n".join(universe) + "\n", encoding="utf-8")
    print(f"\n  wrote {DERIVED_CHANGES.name} ({len(rows_out)} rows, {n_verified} verified) "
          f"and {DERIVED_CONSTITUENTS.name} ({len(universe)} symbols)")


def replace_curated() -> None:
    for derived, curated in ((DERIVED_CONSTITUENTS, CURATED_CONSTITUENTS),
                             (DERIVED_CHANGES, CURATED_CHANGES)):
        if curated.exists():
            bak = curated.with_suffix(curated.suffix + ".pre-eodhd.bak")
            shutil.copy2(curated, bak)
            print(f"  kept {curated.name} as {bak.name}")
        shutil.copy2(derived, curated)
        print(f"  {derived.name} -> {curated.name}")
    print("  next: python scripts/00_init_db.py --reset && python run_all.py "
          "(or re-run 09_build_events.py) so index_membership is rebuilt from these files")


# ── main ────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="Read the cached response instead of calling the API.")
    ap.add_argument("--cache", type=Path, default=RAW_CACHE,
                    help=f"Raw-response cache path (default {RAW_CACHE.name}).")
    ap.add_argument("--no-emit", action="store_true",
                    help="Report only; do not write the derived input files.")
    ap.add_argument("--replace", action="store_true",
                    help="Copy the derived files over sp500_constituents.txt and "
                         "sp500_index_changes.csv (originals kept as *.pre-eodhd.bak).")
    ap.add_argument("--end-date-shift", type=int, default=0, metavar="DAYS",
                    help="Add DAYS to every EODHD EndDate (use +1 if the report shows "
                         "removals systematically one day early).")
    args = ap.parse_args()

    print(f"EODHD S&P 500 membership history  (window {config.START_DATE} .. {config.END_DATE})")
    raw = fetch(args.cache, args.offline)
    fetched = raw.get("fetched_utc", "unknown")
    records = parse_records(raw)
    print(f"  {len(records)} raw records, fetched {fetched}")

    merged = merge_records(records, load_renames(), args.end_date_shift)
    universe, changes, base = derive(merged)
    matches = compare(merged, universe, changes, base)

    if not args.no_emit:
        emit(universe, changes, matches, fetched, args.end_date_shift)
    if args.replace:
        if args.no_emit:
            raise SystemExit("--replace needs the derived files; drop --no-emit")
        _section("Replacing the curated input files")
        replace_curated()
    return 0


if __name__ == "__main__":
    sys.exit(main())
