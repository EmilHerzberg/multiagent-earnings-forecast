# What is in `thesis.db` — every table and every view

_A reference for the 22 tables and 2 views that hold the thesis data. Written for someone new to Python and new to databases. Every number below was read out of the database itself; nothing is estimated._

The file is `thesis/thesis.db`, 134 MB. It is opened **read-only** everywhere in this document — see §8 for how, and why that matters.

---

## 0. The nine words you need first

Nothing below assumes you already know these. Each is defined once, here, and then used.

| word | what it means |
|---|---|
| **row** | one record. One line in a spreadsheet. In `prices_daily`, one row is one company on one day. |
| **column** | one field, the same on every row. `close` is a column; it holds a number on every row of `prices_daily`. |
| **table** | rows and columns stored on disk. The data itself. |
| **view** | a *stored query*, not stored data. It looks and reads exactly like a table, but nothing is saved — every time you read it, the database re-runs the query underneath. It costs no disk space, and it cannot go stale relative to the tables it reads, because it *is* those tables. This database has two views; they are §7. |
| **primary key** | the column, or set of columns, that makes a row unique. The database refuses to store two rows with the same key. In `companies` the key is `symbol`: there cannot be two rows for AAPL. |
| **composite key** | a primary key made of more than one column. In `prices_daily` it is `(symbol, date)` — AAPL can appear thousands of times and 2025-01-31 can appear hundreds of times, but AAPL-on-2025-01-31 exactly once. |
| **foreign key** | a column that points at another table's primary key. `events.symbol` is declared a foreign key to `companies.symbol`, which is the database writing down "every event belongs to a company in that table". |
| **index** | a lookup shortcut the database builds so it does not have to scan every row. Purely about speed — an index never changes an answer, only how long it takes to get. Primary keys get one automatically (those are the `sqlite_autoindex_…` names); the six hand-made ones are named `ix_…` and are noted per table below. |
| **NULL** | "no value here". It means **two different things** depending on the column, and telling them apart is the point of every NULL count in this document: *not applicable* (the question does not arise for this row) versus *not obtained* (the question arises and the answer could not be got). Each section says which. |

One more, because it explains an otherwise baffling table: a **key/value table** has exactly two columns, one holding the name of a setting and one holding its value. It has as many rows as there are settings — ten, in `study_meta` — and it is not "nearly empty", it is the wrong shape to hold a lot of rows in the first place.

---

## 1. The map

Twenty-two tables, in five layers. Data flows top to bottom: each layer is built from the ones above it.

```
  IDENTITY AND UNIVERSE          who is in the study
    companies                    530   one row per company
    index_membership             530   when each one entered / left the S&P 500

  RAW MARKET AND FUNDAMENTAL     what was measured, straight from the providers
    prices_daily             842,950   one company-day
    market_benchmark           1,631   one SPY day
    earnings                   3,141   one company-quarter announcement
    income_statement          13,386   one company-quarter statement
    shares_outstanding         7,260   one company-quarter share count

  DERIVED EVENT TABLES           the units the experiment forecasts, computed once
    events                     2,953   THE central table: one forecastable event
    event_features             2,953   its numbers, as known at the close of t0
    event_regime               2,953   the macro / risk weather at t0
    event_news_summary         2,953   how much news there was, and how positive
    event_news                28,277   the individual news items
    event_macro_overlap        2,780   scheduled macro releases inside the window

  EXTERNAL CONTEXT SERIES        the world, as daily time series
    macro_series               9,118   8 economic series, with publication lags
    geopolitical_risk          2,373   AI-GPR daily index
    uncertainty_indices        2,373   trade- and economic-policy uncertainty
    vix                        1,662   implied volatility
    macro_event_calendar          60   scheduled FOMC / CPI / NFP / PCE dates

  BOOKKEEPING                    the audit trail; no market data at all
    level_assignment              60   which field belongs to level 1 vs level 2
    provenance                    23   which API endpoint filled which table
    data_exclusions              240   every record deliberately dropped, and why
    study_meta                    10   the study's own settings (key/value)

  AND TWO VIEWS                  what the experiment actually reads — §7
    v_level1                   2,953   events + companies + event_features, joined
    v_level2_regime            2,953   a renaming of event_regime
```

**Why 22 and not 23.** Ask SQLite to list its tables and you get 23. The extra one is `sqlite_sequence`, which SQLite maintains for itself: it records the highest `id` ever handed out by the two tables declared `AUTOINCREMENT` (`provenance` and `data_exclusions`). Right now it holds `provenance → 38` and `data_exclusions → 999`, both larger than those tables' actual row counts of 23 and 240, because the build scripts delete and rewrite their rows on every rerun and AUTOINCREMENT never reuses a number. It is a counter, not data. Ignore it.

---

## 2. The join key

**Across this database an event is identified by the pair `(symbol, fiscal_date)`** — the ticker, and the last day of the fiscal quarter being reported. For example `('AAPL', '2024-12-31')`.

Five tables use exactly that pair as their primary key, which is what makes them join cleanly one-to-one: `events`, `event_features`, `event_regime`, `event_news_summary`, and `shares_outstanding`. All three joins out of `events` match **2,953 of 2,953** rows — verified, not assumed:

```sql
SELECT count(*) FROM events JOIN event_features     USING (symbol, fiscal_date);  -- 2953
SELECT count(*) FROM events JOIN event_regime       USING (symbol, fiscal_date);  -- 2953
SELECT count(*) FROM events JOIN event_news_summary USING (symbol, fiscal_date);  -- 2953
SELECT count(*) FROM events JOIN companies          USING (symbol);               -- 2953
```

Where a table deviates from the pair:

| table | its key | why it differs |
|---|---|---|
| `companies`, `index_membership` | `symbol` | one row per company, not per quarter |
| `prices_daily`, `market_benchmark` | `(symbol, date)` | one row per trading **day** |
| `income_statement` | `(symbol, fiscal_date, period)` | `period` would allow annual and quarterly side by side. In practice all 13,386 rows are `'quarterly'`. |
| `event_news` | `(symbol, fiscal_date, published_et, url)` | many articles per event, so the pair alone is not unique; the timestamp and URL finish the key |
| `event_macro_overlap` | `(symbol, fiscal_date, macro_date, event_type)` | several macro releases can fall inside one window |
| `vix`, `geopolitical_risk`, `uncertainty_indices` | `date` | one row per day, no company involved |
| `macro_series` | `(series_id, observation_date)` | eight series stacked in one table |
| `macro_event_calendar` | `(date, event_type)` | two different releases can land on the same day |
| `provenance`, `data_exclusions` | `id` | log tables — an auto-numbered counter, because nothing else about a log line is guaranteed unique |
| `study_meta` | `key` | key/value |
| `level_assignment` | `(source_table, field)` | one row per field of the prompt |

### 2.1 Two dates, and the one seam where the other one is used

An event carries **two** dates and they never coincide — not once in all 2,953 rows:

| | what it is | example (Agilent, first quarter of 2025) |
|---|---|---|
| `fiscal_date` | the last day of the quarter being reported | 2025-01-31 |
| `report_date` | the day the company published the figures | 2025-02-26 |
| `t0` | the reaction day, the first session on which the market could act | 2025-02-27 |

`fiscal_date` is the key inside this database. `report_date` exists in only three
places — `earnings`, `events` and the view `v_level1` — and **none of the event
context tables has it at all**:

```
earnings              fiscal_date   report_date
events                fiscal_date   report_date
v_level1              fiscal_date   report_date
event_features        fiscal_date        -
event_regime          fiscal_date        -
event_news            fiscal_date        -
event_news_summary    fiscal_date        -
event_macro_overlap   fiscal_date        -
```

**The seam.** The experiment harness does not use `fiscal_date`. It builds its own
identifier as `{symbol}_{report_date}_{level}` — `MSFT_2025-10-29_L2` — and
`scripts/24_score_experiment.py` splits that back apart and joins on
`(symbol, report_date)`. That works, and it is checked: both pairs are unique,
2,953 rows to 2,953 distinct `(symbol, fiscal_date)` and 2,953 distinct
`(symbol, report_date)`.

But it means the bridge between the experiment and the database runs through
**`v_level1` and nothing else**, because that is the only object carrying both
dates. Take the harness's identifier to `event_features` or `event_regime` and
there is nothing to join on.

**Why a wrong join cannot quietly half-work.** The obvious mistake is to join on
the wrong date column. It returns *nothing*, which is loud, rather than a subset,
which would not be:

```sql
-- correct
SELECT count(*) FROM events e JOIN event_features f
  ON e.symbol = f.symbol AND e.fiscal_date = f.fiscal_date;   -- 2953

-- the mistake
SELECT count(*) FROM events e JOIN event_features f
  ON e.symbol = f.symbol AND e.report_date = f.fiscal_date;   -- 0
SELECT count(*) FROM events e JOIN event_regime g
  ON e.symbol = g.symbol AND e.report_date = g.fiscal_date;   -- 0
```

Zero is not luck. 192 of the 2,953 `report_date` values do fall on a month end and
so *look* like a `fiscal_date`, but the join tests the symbol as well, and a
company does not report on the closing day of one of its own other quarters.

### 2.2 Why there is no `event_id`

A natural question, since a single-column surrogate key is the usual advice. It
would not buy anything here, and it would cost a great deal.

The uniqueness a surrogate key normally provides is **already enforced**: every one
of the 22 tables has a real `PRIMARY KEY`, not a convention. SQLite rejects a
duplicate row on insert. `events`, `event_features`, `event_regime` and
`event_news_summary` all declare `PRIMARY KEY (symbol, fiscal_date)`.

The composite key also *says* what it identifies. `('AAPL', '2025-09-30')` tells you
the company and the quarter; `event_id = 4711` tells you nothing until you look it
up. For a dataset that has to be explained out loud, that is an advantage rather
than a compromise.

Against that, adding one would mean migrating eight tables, touching the **14 of the 26
pipeline scripts** that reference `fiscal_date` (00, 02, 03, 04, 06, 08, 09, 10, 11, 12,
15, 18, 19, 20), rebuilding the database and re-verifying every downstream figure — to
gain a property the schema already has.


---

# Layer 1 — Identity and universe

## `companies` — 530 rows

**One row is one company that was in the S&P 500 at some point during the study window.**

Key: `symbol`. Built in two steps, which is worth knowing if a company ever looks empty: `scripts/00_init_db.py:198` creates the 530 rows from the constituent list with `INSERT OR IGNORE INTO companies (symbol)`, so at first each row holds nothing but a ticker. `scripts/01_eodhd_company_master.py` then fills the rest in — it contains **no INSERT at all**, only an `UPDATE`, so a symbol missing from the constituent list would never appear here no matter how often that script ran.

The 530 is deliberately larger than 500. It is the **union** of everyone who was a member at any point between 2025-01-01 and 2026-06-30, including companies added mid-window and companies removed mid-window. Taking today's 500 instead would be survivorship bias: the companies that got dropped are exactly the ones that did badly, and excluding them quietly flatters every result. `study_meta.constituent_basis` records this decision in the database itself.

| column | type | meaning |
|---|---|---|
| `symbol` | TEXT | the ticker; primary key |
| `name` | TEXT | company name |
| `sector` | TEXT | e.g. Technology, Healthcare — Morningstar-style taxonomy, **not** GICS |
| `industry` | TEXT | finer classification, e.g. "Diagnostics & Research" |
| `country_iso` | TEXT | listing country |
| `fiscal_year_end` | TEXT | month name, e.g. "October" |
| `isin` | TEXT | international security identifier |
| `cik` | TEXT | the company's SEC filer number |
| `ipo_date` | TEXT | first listing date |
| `data_source` | TEXT | `EODHD` (377 rows) or `AlphaVantage` (151 rows) |

**NULLs, and what they mean here:**

- `isin` and `ipo_date` are NULL on **153** rows. This is *not applicable to the source*, not a gap in effort: Alpha Vantage's company endpoint does not return either field, so all 151 Alpha-Vantage-sourced rows lack them, plus the 2 rows below. 151 + 2 = 153 exactly.
- `name`, `sector`, `industry`, `country_iso`, `fiscal_year_end`, `cik` and `data_source` are NULL on **2** rows: `SATS` and `WBA`. This one *is* "could not obtain" — neither provider returned master data for them. Both were logged in `data_exclusions` with reason `no_master_data`. They still carry 5 and 3 events respectively, which is why the `name` column of `v_level1` has 8 NULLs.

Sector spread, for the "broad range of industries" claim: Technology 91, Industrials 74, Financial Services 71, Healthcare 62, Consumer Cyclical 59, Consumer Defensive 37, Utilities 31, Real Estate 31, Communication Services 25, Basic Materials 24, Energy 23, unknown 2.

**Easy to get wrong:** the sector labels are the Morningstar-style scheme ("Consumer Defensive", "Financial Services"), not the GICS names ("Consumer Staples", "Financials"). Both providers use the same scheme, so the two sources are interchangeable, but a reader who assumes GICS will look for sectors that are not there.

---

## `index_membership` — 530 rows

**One row is one company's entry and exit dates for the S&P 500.**

Key: `symbol`. Written by `scripts/09_build_events.py` (from `sp500_index_changes.csv`).

This is what makes point-in-time membership possible: it is the table that lets `events.in_index_at_t0` be computed, rather than assuming that whoever is in the index today was in it in January 2025.

| column | type | meaning |
|---|---|---|
| `symbol` | TEXT | ticker; primary key |
| `in_index_from` | TEXT | date the company joined |
| `in_index_to` | TEXT | date it left |
| `from_source` | TEXT | `verified` (28) or `assumed_at_window_start` (502) |
| `to_source` | TEXT | `verified` (23), `price_series` (7), or NULL |
| `note` | TEXT | free text, e.g. "replaced Hologic", "acquired by Synopsys" |

**NULLs here are the interesting part, and they are "not applicable", not "unknown":**

- `in_index_from` NULL on **502** rows means *already a member when the window opened*. There is no join date to record because the join happened before 2025-01-01.
- `in_index_to` NULL on **500** rows means *still a member at the window end*. `to_source` is NULL on the same 500 rows for the same reason.

So the table describes 28 additions and 30 removals over eighteen months, against a stable core of ~500.

**Easy to get wrong:** `from_source = 'assumed_at_window_start'` is an honest admission, not a verified fact. It says "we did not find a press release, and we are treating this company as a member from day one". For the 502 large, long-standing constituents that is safe. It is written down so a reader can see the difference between the 28 dates that came from an S&P press release and the 502 that were assumed.

---

# Layer 2 — Raw market and fundamental data

## `prices_daily` — 842,950 rows

**One row is one company on one trading day.**

Key: `(symbol, date)`. Covers **2020-01-02 to 2026-06-11**, 529 distinct symbols. Extra index: `ix_prices_date` on `date`. Written by `scripts/05_eodhd_daily_prices.py`.

The history starts five years before the event window even though the events all sit in 2025–26. That is not padding: a 252-trading-day (one year) momentum figure for an event in January 2025 needs prices from January 2024, and a 52-week range needs the same. Without the back-history roughly 70 % of events would have no 12-month lookback. The provider charges one call per symbol regardless of the date range asked for, so the extra five years was free.

| column | type | meaning |
|---|---|---|
| `symbol` | TEXT | ticker |
| `date` | TEXT | trading day, `YYYY-MM-DD` |
| `open` | REAL | opening price, **rescaled** onto the adjusted basis |
| `high` | REAL | high, rescaled onto the adjusted basis |
| `low` | REAL | low, rescaled onto the adjusted basis |
| `close` | REAL | the **RAW** close, exactly as the provider delivered it |
| `adj_close` | REAL | split- and dividend-adjusted close — **use this for returns** |
| `volume` | INTEGER | shares traded |

No NULLs anywhere in this table.

**The thing most likely to be got wrong, with the number that shows it.** `adj_close` is retroactively rescaled every time a company splits its stock, so it is **not a price level** and must never be presented as one. Booking Holdings (BKNG) split roughly 25-for-1 in April 2026. Look at what the two columns say about 2 January 2020:

| column | value on 2020-01-02 | what it is |
|---|---|---|
| `close` | 2,074.58 | what the ticker actually printed that day |
| `adj_close` | 81.26 | 2,074.58 restated in post-split units |

Nobody ever bought BKNG at $81 in 2020. The $81 exists only so that a return computed across the April 2026 split comes out right. So: **`adj_close` for returns and ratios, `close` for anything a human reads as a price.** The split itself is visible in the raw column and invisible in the adjusted one:

| date | `close` (raw) | `adj_close` |
|---|---|---|
| 2026-04-02 | 4,194.31 | 167.35 |
| 2026-04-06 | 176.19 | 175.75 |

The raw column falls 96 % overnight; the adjusted column rises 5 %, which is what actually happened to anyone holding the stock.

Also worth knowing: `open`, `high` and `low` arrive from the provider on the raw basis and are *stored here already scaled onto the adjusted basis* (multiplied by `adj_close / close`). This is so that a series that mixes them is internally consistent across a split. `close` is the one column deliberately left raw.

**Coverage note:** 529 symbols, not 530. `CBOE` returned no bars at all and is logged in `data_exclusions` with reason `no_price_data`. A further 3 individual KLAC days in June 2026 were dropped as `level_outlier_vs_local_median` — the provider delivered an adjusted close around 211 against a local median around 1,940, a ratio of 0.11, which is a vendor error rather than a 89 % crash. The 842,950 count is *after* those removals.

---

## `market_benchmark` — 1,631 rows

**One row is one trading day of SPY, the S&P 500 tracking fund.**

Key: `(symbol, date)`; `symbol` is `'SPY'` on every row. Covers **2020-01-02 to 2026-06-30**. Written by `scripts/07_alphavantage_market_benchmark.py`.

This is the "market" in *abnormal* return. The outcome the thesis forecasts is the stock's return **minus the market's return over the same days**, so a market series is not optional.

| column | type | meaning |
|---|---|---|
| `symbol` | TEXT | always `SPY` |
| `date` | TEXT | trading day |
| `open` `high` `low` | REAL | as delivered |
| `close` | REAL | RAW close |
| `adj_close` | REAL | total-return basis — **this is the one used for the market return** |
| `volume` | INTEGER | shares traded |
| `dividend_amount` | REAL | dividend paid that day, 0.0 on most days |
| `split_coefficient` | REAL | 1.0 except on a split |

No NULLs.

**Why a separate table from `prices_daily`, and why a different provider.** The EODHD price universe used for the stocks covers common stock only — it holds no ETFs or indices, so SPY, IVV, VOO and QQQ are all simply absent from it. Alpha Vantage supplies SPY in a single call.

**Easy to get wrong:** the stock side and the market side must be computed on the **same** basis. Both use `adj_close`, so both are total returns (price plus reinvested dividends). Pairing an adjusted stock return against a price-only index return would inject a systematic bias the size of the dividend yield — small, constant, and in the same direction every time, which is the worst kind.

---

## `earnings` — 3,141 rows

**One row is one company's earnings announcement for one fiscal quarter.**

Key: `(symbol, fiscal_date)`. `report_date` spans **2025-01-10 to 2026-06-30**. Extra index: `ix_earnings_report_date`. Written by `scripts/02_eodhd_earnings_eps.py`, then enriched in place by `03_eodhd_earnings_surprise.py` (the surprise column) and `04_eodhd_earnings_timing.py` (the timing column).

This is the raw announcement feed. `events` is the filtered, usable subset of it — see the arithmetic in the `events` section.

| column | type | meaning |
|---|---|---|
| `symbol` | TEXT | ticker |
| `fiscal_date` | TEXT | **end of the quarter being reported** — not the announcement date |
| `report_date` | TEXT | the day the announcement was made |
| `eps_actual` | REAL | reported earnings per share |
| `eps_estimate` | REAL | the analyst consensus going in |
| `eps_surprise_pct` | REAL | `(actual − estimate) / abs(estimate) × 100` |
| `before_after_market` | TEXT | `BeforeMarket`, `AfterMarket` or `DuringMarket` |

**NULLs:**

| column | NULLs | what a NULL means |
|---|---|---|
| `eps_actual` | 7 | not obtained — the provider had no reported figure |
| `eps_estimate` | 4 | not obtained — no consensus was published |
| `eps_surprise_pct` | 16 | partly not applicable: the formula is *undefined* when the estimate is zero, and impossible when either input is missing |
| `before_after_market` | **164** | not obtained — the provider does not know the session. These 164 announcements are the single largest reason a row here never became an event. |

**Two things a reader gets wrong.**

First, `eps_actual` and `eps_estimate` are **street** EPS — the consensus-comparable figure, generally diluted and adjusted for one-off items. They are *not* the GAAP basic or diluted EPS from the 10-Q, and they cannot be reconciled against `income_statement.net_income` divided by `shares_outstanding.shares`. They are matched to each other on a like-for-like basis, which is precisely what makes the surprise well defined.

Second, `eps_surprise_pct` divides by the **estimate**, not by the share price. When a company was expected to earn about zero, the ratio explodes: the largest value in this database reaches exactly 34,000 % (Boeing, quarter ending 2025-12-31). Any regression on this column has to winsorise or clip it first — `scripts/21_metrics.py` clips to ±200 and says so in the code rather than hiding it.

---

## `income_statement` — 13,386 rows

**One row is one company's income statement for one fiscal quarter.**

Key: `(symbol, fiscal_date, period)`. Covers `fiscal_date` **2020-01-31 to 2026-06-30**, 519 distinct symbols. Written by `scripts/06_alphavantage_income_statement.py`.

Kept back to 2020 for the same reason as the prices: trailing-twelve-month valuation and year-over-year growth for a January 2025 event need the four quarters *before* the event window.

| column | type | meaning |
|---|---|---|
| `symbol` | TEXT | ticker |
| `fiscal_date` | TEXT | quarter end |
| `period` | TEXT | `'quarterly'` on all 13,386 rows |
| `reported_currency` | TEXT | e.g. USD |
| `revenue` | REAL | total revenue |
| `net_income` | REAL | bottom line |
| `operating_income` | REAL | operating income |
| `ebit` | REAL | earnings before interest and tax |
| `ebitda` | REAL | EBIT plus depreciation and amortisation |
| `gross_profit` | REAL | revenue minus cost of goods |

**NULLs — all of them "not obtained":** `ebit` 170, `ebitda` 10, `operating_income` 6, `net_income` 5, `gross_profit` 5, `revenue` 4. The provider omitted the line item for that filing.

**Easy to get wrong:** `operating_income` and `ebit` look like duplicates and are stored separately on purpose. They coincide for most non-financial companies but diverge whenever non-operating income is material, and collapsing them into one column would hide exactly the companies where the distinction matters.

Also: 519 symbols, not 530. 22 companies returned no statements inside the window and are logged in `data_exclusions` with reason `no_reports_in_window`.

---

## `shares_outstanding` — 7,260 rows

**One row is one company's share count as of one fiscal quarter end.**

Key: `(symbol, fiscal_date)`. Covers **2020-03-31 to 2026-06-30**, 519 symbols. No NULLs. Written by `scripts/08_alphavantage_shares_outstanding.py`.

| column | type | meaning |
|---|---|---|
| `symbol` | TEXT | ticker |
| `fiscal_date` | TEXT | the quarter the count refers to |
| `shares` | REAL | common shares outstanding, **on the current split basis** |
| `reported_currency` | TEXT | e.g. USD |

This table exists to make market capitalisation computable *as of a past date*. The obvious alternative — Alpha Vantage's `OVERVIEW` endpoint, which hands you `MarketCapitalization` directly — is a **current snapshot**. Attaching today's market cap to a February 2025 event is a look-ahead error: the number would encode everything that happened after the forecast was supposed to be made.

**Easy to get wrong, and here is the failure in numbers.** The share counts are *retroactively split-adjusted* — expressed on the current basis, just like `adj_close`. So the cap must pair them with `adj_close`, never with the raw `close`. Take BKNG's event on 2025-02-21, before its 2026 split:

| ingredient | value |
|---|---|
| `shares` at 2024-12-31 | 835,650,000 |
| `adj_close` on 2025-02-21 | 197.2065 |
| `close` (raw) on 2025-02-21 | 4,990.64 |
| **adj_close × shares** | **164,795,611,725** ← the stored `events.market_cap_at_t0`, exactly |
| close × shares | 4,170,428,316,000 |

The wrong pairing values Booking.com at **$4.17 trillion** — larger than Apple — because every split gets counted twice, once in the share count and once in the price. The right pairing gives $164.8 bn, which is the correct answer. Note that this is checkable: the stored value matches `adj_close × shares` to the last digit.

---

# Layer 3 — The derived event tables

Nothing in this layer calls an API. Every row is computed from the layers above, once, in one place — so that the look-ahead rules are applied consistently instead of being re-implemented inside whatever code happens to build a prompt.

## `events` — 2,953 rows

**One row is one forecastable earnings event: a company, a quarter, the day the market first prices it, and what actually happened over the following five trading days.**

This is the centre of the database. Everything else either feeds it or hangs off it.

Key: `(symbol, fiscal_date)`. 527 distinct symbols. `t0` spans **2025-01-10 to 2026-06-04**. Extra index: `ix_events_t0`. Written by `scripts/09_build_events.py`.

### How 3,141 announcements became 2,953 events

| step | rows |
|---|---|
| announcements in `earnings` | 3,141 |
| − `timing_unknown` (no `before_after_market`, so t0 cannot be assigned) | −164 |
| − `no_trading_day_after_report` | −10 |
| − `fewer_than_5_days_after_t0` (window would run past the data) | −8 |
| − `no_price_series` | −6 |
| **= events** | **2,953** |

All 188 removals are individually listed in `data_exclusions` with their reason. That is the table's whole purpose: a dropped record leaves a receipt.

### What `t0` is, and why it is not `report_date`

`t0` is the **first trading day that prices the announcement**:

| `before_after_market` | t0 |
|---|---|
| `BeforeMarket` (1,700 events) | `report_date` itself |
| `DuringMarket` | `report_date` itself |
| `AfterMarket` (1,253 events) | the **next** trading day |

and if `report_date` falls on a weekend or holiday, t0 moves forward to the next trading day. In 1,256 of the 2,953 events `t0` is not `report_date`. Aligning everything to `report_date` regardless — the obvious shortcut — would mix two different event windows and blur the measured abnormal returns, and with 42 % of the sample announcing after the close it is not a marginal effect.

**The forecast is made after t0 closes, and t0 is deliberately excluded from the outcome window.** The first session mostly prices the mechanical surprise in the reported number; the study is about what happens *after* that adjustment. So:

```
window = t0+1 … t0+5 trading days

stock_return    = adj_close(t0+5) / adj_close(t0) − 1
market_return   = compounded SPY total return over the same days
abnormal_return = stock_return − market_return
label           = 1 if abnormal_return > 0 else 0
```

A worked example, Apple's December-2024 quarter:

| field | value |
|---|---|
| `report_date` | 2025-01-30, `AfterMarket` |
| `t0` | 2025-01-31 |
| `window_start` … `window_end` | 2025-02-03 … 2025-02-07 |
| `stock_return` | −0.03547 (−3.55 %) |
| `market_return` | −0.00174 (−0.17 %) |
| `abnormal_return` | −0.03372 (−3.37 %) |
| `label` | **0** |

Apple fell 3.5 % while the market was flat, so it underperformed and the label is 0.

### Columns

| column | type | meaning |
|---|---|---|
| `symbol` | TEXT | ticker |
| `fiscal_date` | TEXT | quarter being reported |
| `report_date` | TEXT | announcement date |
| `before_after_market` | TEXT | `BeforeMarket` 1,700 / `AfterMarket` 1,253 (no `DuringMarket` survived) |
| `t0` | TEXT | first trading day that prices it |
| `window_start` | TEXT | t0+1 |
| `window_end` | TEXT | t0+5 |
| `horizon_days` | INTEGER | 5 on every row |
| `stock_return` | REAL | total return over the window |
| `market_return` | REAL | SPY over the same days |
| `abnormal_return` | REAL | stock − market. **Ground truth.** |
| `label` | INTEGER | 1 if `abnormal_return > 0` else 0. **Ground truth.** 1,376 ones, 1,577 zeros (46.6 % base rate) |
| `close_at_t0` | REAL | **RAW** close on t0 |
| `market_cap_at_t0` | REAL | `adj_close(t0) × shares` from the most recent quarter known at t0 |
| `shares_fiscal_date` | TEXT | which quarter that share count came from |
| `eps_actual`, `eps_estimate`, `eps_surprise_pct` | REAL | copied from `earnings` |
| `split_after_t0` | INTEGER | 1 on 42 events: a split happens later, so the future must stay hidden |
| `split_in_lookback` | INTEGER | 1 on 16 events: the raw series jumps before t0 |
| `has_fundamentals` | INTEGER | 1 on 2,919: an `income_statement` row exists |
| `in_index_at_t0` | INTEGER | 1 on 2,832: the company was an S&P 500 member on t0 |

**NULLs:** `market_cap_at_t0` and `shares_fiscal_date` are NULL on the **same 34 rows**, which are exactly the 34 rows with `has_fundamentals = 0`. Meaning: no share count was available for that company, so no cap could be computed. Not applicable rather than unmeasured. `eps_estimate` 3 and `eps_surprise_pct` 4 NULLs carry over from `earnings`.

### Three things a reader gets wrong here

1. **`label` and `abnormal_return` are the answer key.** They are computed from prices *after* the forecast is due. They exist so a forecast can be graded. They must never reach a model. §7 covers how that is enforced.
2. **`close_at_t0` is the RAW close**, deliberately — it is the number a human saw on the screen that day, and it is what gets shown in a prompt. That is also why `split_after_t0` exists: for those 42 events the raw price is about to change basis, and the flag marks them so a later split cannot be leaked backwards through a price the model reads.
3. **`in_index_at_t0 = 1` is not automatic.** 121 events belong to companies that were *not* index members on their t0 — they joined later or had already left. The point-in-time sample is the `in_index_at_t0 = 1` subset. The 121 are kept in the table rather than deleted so that the choice stays visible and reversible.

---

## `event_features` — 2,953 rows

**One row is the quantitative picture of one event, measured strictly as of the close of t0.**

Key: `(symbol, fiscal_date)`. Written by `scripts/11_derive_context_features.py`.

Nothing from t0+1 onwards enters any column here. That is the whole discipline of the table: it exists so the look-ahead rule is applied once, in one file, rather than in every place a prompt gets built.

| column | type | meaning |
|---|---|---|
| `symbol`, `fiscal_date`, `t0` | TEXT | the event |
| **the announcement reaction** | | |
| `reaction_return` | REAL | the stock's total return on t0 itself |
| `reaction_abnormal` | REAL | that, minus SPY on the same day |
| `volume_ratio` | REAL | t0 volume ÷ trailing 20-day median volume. 2.24 means "more than twice the usual attention" |
| **trailing momentum and risk, ending at t0** | | |
| `ret_21d`, `ret_63d`, `ret_252d` | REAL | returns over roughly 1, 3 and 12 months |
| `volatility_252d` | REAL | annualised standard deviation of daily returns |
| `pct_of_52w_range` | REAL | 0 = at the 52-week low, 1 = at the high |
| `drawdown_from_252d_high` | REAL | how far below the year's peak, negative |
| **valuation and growth** | | |
| `pe_trailing` | REAL | price ÷ trailing-twelve-month earnings |
| `ps_trailing` | REAL | price ÷ trailing-twelve-month revenue |
| `revenue_yoy` | REAL | revenue growth vs the same quarter a year earlier |
| `net_income_yoy` | REAL | net income growth, same basis |
| **market regime** | | |
| `spy_ret_21d`, `spy_ret_63d` | REAL | what the market itself has been doing |
| `spy_volatility_21d` | REAL | the market's realised volatility |

**NULLs, and the two different meanings side by side:**

| column | NULLs | meaning |
|---|---|---|
| `pe_trailing` | 186 | **not applicable**, both ways: 34 have no fundamentals at all, and on the other 152 the code sets a P/E only `if cap and ttm_ni and ttm_ni > 0` — so a company without four usable quarters, or one whose trailing-twelve-month earnings are not positive, gets NULL rather than a negative or meaningless ratio |
| `net_income_yoy` | 245 | 34 with no fundamentals; the other 211 have revenue but no comparable prior-year net income |
| `ps_trailing`, `revenue_yoy` | 34 each | exactly the 34 events with `has_fundamentals = 0` — not applicable |
| `ret_252d` | 10 | **not obtained**: the company had less than a year of price history before t0 |
| `ret_63d` | 2, `volatility_252d` 2, `ret_21d` 1 | same reason, shorter windows |

**Easy to get wrong:** `reaction_return` and `reaction_abnormal` measure t0, and t0 is *not* in the outcome window. They look like leakage and are not — that is exactly why the study design excludes t0 from the window in the first place. They are the single strongest legitimate input after the surprise itself.

---

## `event_regime` — 2,953 rows

**One row is the macro and risk weather at one event's t0 — the state of the world when the forecast was due.**

Key: `(symbol, fiscal_date)`. 27 columns. No NULLs anywhere. Written by `scripts/18_derive_event_regime.py`, which reads the four external-series tables of layer 4.

The point of this table is that the context series are stored as long daily time series, and asking "what was the VIX at this event?" correctly requires a point-in-time rule. That rule lives here, once, instead of everywhere.

**The two selection rules, which differ by series type:**

- *Continuously quoted* — VIX, Treasury yields, oil, and the newspaper indices (AI-GPR, TPU, EPU, which are built from that morning's papers): take the most recent observation **on or before t0**. Markets and newspapers are same-day public.
- *Statistical releases* — CPI, unemployment, retail sales: take the most recent observation whose `public_from` is **strictly before t0**. A CPI figure dated 1 June refers to June but is not published until mid-July; attaching it to an event in early July would show a model a number that did not exist yet.

| column | type | meaning |
|---|---|---|
| `symbol`, `fiscal_date`, `t0` | TEXT | the event |
| `vix` | REAL | Cboe implied-volatility index at t0 |
| `vix_pctile_5y` | REAL | where that sits in the preceding five years, 0…1 |
| `gpr_ai` | REAL | AI-scored geopolitical risk; 100 = the 1985–2019 average |
| `gpr_ai_ma30` | REAL | its 30-day mean |
| `gpr_threats`, `gpr_acts` | REAL | risk from threatened vs realised events |
| `gpr_oil` | REAL | oil / energy-supply sub-index |
| `gpr_pctile_5y` | REAL | percentile of `gpr_ai` |
| `tpu`, `tpu_ma7`, `tpu_ma30` | REAL | trade-policy uncertainty and its published moving averages |
| `tpu_pctile_5y` | REAL | its percentile |
| `epu_us` | REAL | US economic-policy uncertainty |
| `epu_pctile_5y` | REAL | its percentile |
| `fed_funds` | REAL | effective federal funds rate, % |
| `ust_2y`, `ust_10y` | REAL | Treasury yields, % |
| `yield_curve` | REAL | 10-year minus 2-year. **Negative = inverted**, the classic recession signal |
| `wti`, `brent` | REAL | crude oil, USD/barrel |
| `cpi` | REAL | headline consumer price index, as an index level |
| `cpi_observation` | TEXT | **which month that CPI figure refers to** — usually 2–3 months before t0 |
| `unemployment` | REAL | unemployment rate, % |
| `retail_sales` | REAL | advance retail sales |

**Why the percentile columns exist.** A level means nothing on its own. "GPR is 180" is not something a model can reason about. "GPR is at the 72nd percentile of the last five years" is. Each headline indicator therefore carries its rank against the **preceding** five years of its own history, computed from observations up to t0 only — so the ranking itself carries no look-ahead either.

**Easy to get wrong:** `cpi_observation` is not decoration and not a duplicate of `t0`. In the sample row, an event on 2025-01-10 carries `cpi = 315.493` with `cpi_observation = '2024-11-01'`. The most recent inflation reading the world had on 10 January 2025 was the *November* figure. Reading `cpi` without `cpi_observation` invites the mistake of thinking the model was shown current inflation.

---

## `event_news` — 28,277 rows

**One row is one news article, attached to one event.**

Key: `(symbol, fiscal_date, published_et, url)` — four columns, because an event has many articles and only the timestamp plus the URL make an article unique. `published_et` spans **2025-01-03 06:15:00 to 2026-06-04 21:09:01**, in US Eastern time. Extra index: `ix_news_event`. Written by `scripts/12_alphavantage_news.py`.

| column | type | meaning |
|---|---|---|
| `symbol`, `fiscal_date` | TEXT | the event this article is attached to |
| `published_et` | TEXT | `YYYY-MM-DD HH:MM:SS`, US Eastern |
| `relative_timing` | TEXT | `pre_announcement` 14,816 / `post_announcement` 12,978 / `ambiguous` 483 |
| `title` | TEXT | headline |
| `summary` | TEXT | provider's summary |
| `source` | TEXT | outlet — MarketWatch 1,855, MarketBeat 1,551, TradingView 1,338, … |
| `url` | TEXT | link; part of the key |
| `relevance` | REAL | provider's score for how much this article is about this ticker |
| `sentiment` | REAL | ticker-specific sentiment, −1…+1 |
| `sentiment_label` | TEXT | Bullish 11,364 / Somewhat-Bullish 6,202 / Neutral 5,867 / Somewhat-Bearish 2,705 / Bearish 2,139 |
| `overall_sentiment` | REAL | sentiment of the whole article, not just the ticker's part |
| `topics` | TEXT | comma-separated provider tags, e.g. `earnings,energy_transportation` |

**NULLs:** only `summary`, on 3 rows. Not obtained.

**The point-in-time cut.** The fetch window ends at the close of **t0** — the moment the forecast is due — so nothing published afterwards can enter, and reaches back 7 days before the announcement to capture the run-up as well as the reaction.

**Why `relative_timing` matters more than it looks.** A story written before the numbers were public is *anticipation*; one written after is *reaction*. They say completely different things even when the sentiment score is identical, and 12,978 of these 28,277 items are post-announcement. Aggregating them into one average sentiment without the split throws that away — which is why `event_news_summary` keeps three separate means.

**Easy to get wrong:** only **2,831 of the 2,953 events** have any news at all. The other 122 have none, and the join is therefore one-to-many-**or-zero**, not one-to-many. A plain `JOIN` silently loses those 122 events; a `LEFT JOIN` keeps them.

---

## `event_news_summary` — 2,953 rows

**One row is the news picture for one event, condensed to counts and averages.**

Key: `(symbol, fiscal_date)`. One row per event — including the 122 events with no news, which is what makes this table joinable one-to-one where `event_news` is not. Written by the same script, `scripts/12_alphavantage_news.py`.

| column | type | meaning |
|---|---|---|
| `symbol`, `fiscal_date` | TEXT | the event |
| `items_returned` | INTEGER | how many articles the provider offered, **before truncation** — up to 1,000 |
| `items_stored` | INTEGER | how many were actually kept — capped at 15 |
| `n_pre`, `n_post`, `n_ambiguous` | INTEGER | the timing split of the stored items |
| `mean_sentiment` | REAL | average over all stored items |
| `mean_sentiment_pre` | REAL | average over the pre-announcement ones only |
| `mean_sentiment_post` | REAL | average over the post-announcement ones only |
| `lookback_days` | INTEGER | 7 on every row |

**NULLs, and here the distinction really is load-bearing:**

| column | NULLs | meaning |
|---|---|---|
| `mean_sentiment` | 122 | **no news existed** for this event. `items_stored = 0` on exactly these 122 rows. |
| `mean_sentiment_pre` | 374 | no *pre-announcement* article — an average over an empty set is undefined, not zero |
| `mean_sentiment_post` | 649 | no *post-announcement* article, same reasoning |

All three are "not applicable", never "not obtained". A NULL here does not mean neutral sentiment. Replacing them with 0.0 would invent 649 pieces of neutral news that never existed.

**Easy to get wrong:** `items_returned` and `items_stored` are not the same and the gap is often large — one event had 1,000 returned and 15 stored. `items_returned` is a legitimate signal about *attention*; `items_stored` is a storage cap. Do not read the cap as a measurement.

---

## `event_macro_overlap` — 2,780 rows

**One row is one scheduled macro release that falls inside one event's five-day forecast window.**

Key: `(symbol, fiscal_date, macro_date, event_type)`. Extra index: `ix_overlap_event`. Written by `scripts/15_macro_event_calendar.py`.

| column | type | meaning |
|---|---|---|
| `symbol`, `fiscal_date` | TEXT | the event |
| `macro_date` | TEXT | the release date, 2025-01-15 … 2026-06-10 |
| `event_type` | TEXT | `NFP` 832, `CPI` 685, `PCE` 633, `FOMC_DECISION` 630 |
| `days_after_t0` | INTEGER | 1 to 8 |

No NULLs.

**Why this exists.** The outcome is a five-day abnormal return. A CPI print or a Fed decision landing inside those five days moves the whole market — and while the *abnormal* return already nets out the market move, a macro shock still changes cross-sectional dispersion, factor rotation, and how much attention is left for one company's earnings. "There is an FOMC decision two days from now" is genuine context for a forecaster.

It also matters for interpreting the result: if the multi-agent structures differ mainly on events with a macro release in the window, that is a finding about macro-conditioned reasoning, not about earnings drift.

**Easy to get wrong:** 2,780 rows do **not** mean 2,780 events. They cover **1,944 distinct events**; the other **1,009 events have no scheduled release in their window at all**, and an event can appear more than once (say, both CPI and an FOMC decision). Counting rows here as if they were events overstates macro exposure by 43 %.

Also: `days_after_t0` reaches 8 even though the window is only 5 *trading* days, because it counts calendar days. The maximum case is exactly what you would expect: an event with `t0 = 2025-08-28` has `window_end = 2025-09-05` — five trading days later, but eight calendar days, because a weekend and Labor Day sit in between.

---

# Layer 4 — External context series

These four tables are plain daily time series with no company dimension at all. They exist so `event_regime` can be derived; nothing reads them directly at experiment time.

## `macro_series` — 9,118 rows

**One row is one observation of one economic series.**

Key: `(series_id, observation_date)`. Covers **2020-01-01 to 2026-06-30**. Extra index: `ix_macro_public` on `public_from`. Written by `scripts/13_alphavantage_macro.py` (eight API calls in total).

| column | type | meaning |
|---|---|---|
| `series_id` | TEXT | which series (see the table below) |
| `observation_date` | TEXT | the date the value *describes* |
| `value` | REAL | the number |
| `public_from` | TEXT | **the earliest date this observation can be assumed public** |
| `publication_lag_days` | INTEGER | `public_from` − `observation_date` |
| `description` | TEXT | plain-English label |

No NULLs.

The eight series, with the lag that makes each one safe to use:

| `series_id` | rows | lag (days) | what it is |
|---|---|---|---|
| `FED_FUNDS` | 2,373 | 0 | effective federal funds rate, daily |
| `BRENT` | 1,642 | 0 | Brent crude, daily |
| `UST_10Y` | 1,624 | 0 | 10-year Treasury yield, daily |
| `UST_2Y` | 1,624 | 0 | 2-year Treasury yield, daily |
| `WTI` | 1,623 | 0 | WTI crude, daily |
| `RETAIL_SALES` | 78 | 45 | advance retail sales, monthly |
| `CPI` | 77 | 45 | headline consumer price index, monthly |
| `UNEMPLOYMENT` | 77 | 30 | unemployment rate, monthly |

**Easy to get wrong — this is the leakage vector the table is built to close.** The provider returns the **observation date**, not the release date. The CPI observation dated 2026-06-01 describes June but is not published until roughly mid-July. Joining on `observation_date <= t0` would hand a model a number that did not exist yet. That is why `public_from` exists, and why the rule differs by series: market prices carry a lag of 0 because they are public the moment they are quoted; the three monthly statistical releases carry 45, 45 and 30 days.

The two Treasury maturities are stored as separate series rather than as a spread so the **yield curve slope** can be derived from them — an inverted curve behaves differently from either yield alone, so collapsing them would destroy the signal.

---

## `geopolitical_risk` — 2,373 rows

**One row is one calendar day of the AI-GPR geopolitical risk index.**

Key: `date`. Covers **2020-01-01 to 2026-06-30** — calendar days, weekends included, because it is built from newspapers rather than markets. No NULLs. Written by `scripts/14_geopolitical_risk.py`.

Source: Caldara, Iacoviello et al. (2026), Board of Governors of the Federal Reserve System.

| column | type | meaning |
|---|---|---|
| `date` | TEXT | calendar day |
| `gpr_ai` | REAL | the AI-scored index; **100 = the 1985–2019 average** |
| `gpr_aer` | REAL | the original 2022 keyword-counting index, on the same data |
| `gpr_threats` | REAL | risk from *threatened* events |
| `gpr_acts` | REAL | risk from *realised* events |
| `gpr_oil` | REAL | oil / energy-supply-disruption sub-index |
| `gpr_nonoil` | REAL | the remainder |
| `gpr_ai_ma7` | REAL | trailing 7-day mean, computed here, current day included |
| `gpr_ai_ma30` | REAL | trailing 30-day mean, computed here |

**Why the AI index rather than the original.** The 2022 GPR index counts newspaper articles matching a keyword dictionary. AI-GPR replaces the dictionary with semantic reading: GPT-4o-mini scores roughly five million articles from the New York Times, Washington Post and Chicago Tribune for geopolitical risk intensity. That removes false positives from articles using "war" or "attack" in non-geopolitical senses, and catches relevant articles using none of the dictionary words.

**Easy to get wrong:** `gpr_aer` is not an alternative measurement, it is the *old* method reproduced on the same data, stored so that the choice between the two is auditable rather than asserted. And `gpr_ai` is an index, not a percentage — 155 means "about 55 % above the long-run average", which is why `event_regime` also carries a percentile.

---

## `uncertainty_indices` — 2,373 rows

**One row is one calendar day of two policy-uncertainty indices.**

Key: `date`. Covers **2020-01-01 to 2026-06-30**. No NULLs. Written by `scripts/16_uncertainty_indices.py`.

| column | type | meaning |
|---|---|---|
| `date` | TEXT | calendar day |
| `tpu` | REAL | Trade Policy Uncertainty, daily — Caldara et al. (2020), *JME* 109 |
| `tpu_ma7` | REAL | 7-day mean, **as published** by the source |
| `tpu_ma30` | REAL | 30-day mean, as published |
| `epu_us` | REAL | US Economic Policy Uncertainty — Baker, Bloom & Davis (2016), *QJE* 131(4) |

**Why both.** TPU counts articles combining trade-policy terms (tariff, import duty, dumping) with uncertainty terms. EPU is broader: fiscal, regulatory and monetary policy uncertainty as one number. They are kept separate because trade shocks hit equities *unevenly* — importers, exporters and domestic-facing firms move in opposite directions on the same headline — which is exactly the cross-sectional context a market-wide return cannot express.

**Easy to get wrong:** the two TPU moving averages are stored **as the publisher computed them**, not recomputed here — unlike `geopolitical_risk.gpr_ai_ma7/ma30`, which *are* computed locally. The distinction is written into both scripts' docstrings and matters if you ever need to reproduce a number.

---

## `vix` — 1,662 rows

**One row is one trading day of the VIX volatility index.**

Key: `date`. Covers **2020-01-02 to 2026-06-30** — trading days only, hence 1,662 against the newspaper indices' 2,373. Range of `close`: 11.86 to 82.69 (the high is March 2020). No NULLs. Written by `scripts/17_vix.py`, straight from Cboe.

| column | type | meaning |
|---|---|---|
| `date` | TEXT | trading day |
| `open`, `high`, `low` | REAL | intraday |
| `close` | REAL | **the level to use** — VIX has no adjustment concept, so there is no `adj_close` and none is needed |

**Why the publisher rather than a data vendor.** Alpha Vantage does not carry index symbols — `^VIX` returns nothing — and the only alternatives there are leveraged ETFs (VIXY, UVXY) whose prices decay against the index through roll costs, making them a poor stand-in. Taking the series from Cboe avoids both the proxy error and a second vendor dependency.

**Easy to get wrong:** `event_features` already carries *realised* volatility, meaning how much the stock and market actually moved. VIX is *implied* volatility, meaning what the options market expects over the coming 30 days. They are not redundant — the gap between them is informative, and for a five-day forecast made at t0 the forward-looking one is the more relevant. This is also why the level assignment puts realised volatility in level 1 and VIX in level 2, deliberately; `study_meta.level_boundary_rationale` says so in as many words.

---

## `macro_event_calendar` — 60 rows

**One row is one scheduled macroeconomic release.**

Key: `(date, event_type)`. Covers **2025-01-10 to 2026-06-25**. No NULLs. Written by `scripts/15_macro_event_calendar.py` from `macro_event_calendar.csv`.

| column | type | meaning |
|---|---|---|
| `date` | TEXT | release date |
| `event_type` | TEXT | `CPI` 17, `NFP` 16, `PCE` 15, `FOMC_DECISION` 12 |
| `description` | TEXT | e.g. "US Non-Farm Payrolls Release (Employment Situation)" |
| `source` | TEXT | where the date came from |
| `confidence` | TEXT | `ingested` or `official_schedule` |

Sixty rows because that is how many of these four releases happened in eighteen months: roughly one CPI, one NFP and one PCE per month, plus eight FOMC meetings a year. It is a calendar, not a sample.

**Why it is not look-ahead.** Release *dates* are announced months in advance — the Fed, BLS and BEA all publish forward schedules — so knowing that CPI lands on a particular Tuesday was public information long before t0. The `confidence` column distinguishes 2025 rows (from the project's economic-calendar ingest) from 2026 rows (taken from the agencies' own advance schedules, because that ingest stopped at the end of 2025).

This table is the input; `event_macro_overlap` is the output of joining it against each event's window.

---

# Layer 5 — Bookkeeping

No market data here. These four tables are the audit trail — the part of the database that exists to be defended rather than analysed.

## `level_assignment` — 60 rows

**One row is one field of the prompt, and which information tier it belongs to.**

Key: `(source_table, field)`. Written by `scripts/19_define_levels.py`.

| column | type | meaning |
|---|---|---|
| `level` | INTEGER | 1 (28 rows) or 2 (32 rows) |
| `source_table` | TEXT | which table the field comes from |
| `field` | TEXT | the column name |
| `block` | TEXT | the grouping used when rendering the prompt |
| `description` | TEXT | plain-English label, e.g. "first trading day that prices it" |

The blocks:

| level | blocks (fields) |
|---|---|
| 1 | announcement (9), trailing (6), valuation (4), identity (3), market (3), reaction (3) |
| 2 | macro (10), geopolitics (6), news (6), policy (6), calendar (2), volatility (2) |

**Why this is a table and not a convention.** The research question compares two information tiers, so *what is in each tier* is a design parameter of the experiment, not an implementation detail. Written down only as prose it drifts — a prompt builder adds one more field, and the contrast being measured quietly changes without anyone noticing. Fixing it in the database makes the split auditable and keeps it identical across every topology and every model. `scripts/20_build_evidence_packs.py` reads its tier definition from here rather than hard-coding it.

---

## `provenance` — 23 rows

**One row is one script run that wrote to one table, with the exact endpoint it called.**

Key: `id` (auto-numbered). Written by every fetching script via a shared helper.

| column | type | meaning |
|---|---|---|
| `id` | INTEGER | auto-increment counter |
| `table_name` | TEXT | which table was filled |
| `source_api` | TEXT | e.g. `EODHD`, `Alpha Vantage`, `Cboe Global Markets`, `derived` |
| `endpoint` | TEXT | the exact URL path or file, e.g. `/api/eod/{SYMBOL}.US` |
| `rows_written` | INTEGER | how many rows that run produced |
| `run_utc` | TEXT | timestamp |
| `notes` | TEXT | free text |

23 rows covering **20 distinct (table, endpoint) pairs**. A table filled from two different endpoints gets one row per endpoint, which is why `earnings` appears three times. The other three are re-runs of an endpoint that had already been recorded — `companies`, `income_statement` and `shares_outstanding` each carry two rows with a byte-identical endpoint string. So this table is a log of ingest RUNS, not a list of sources, and counting its rows does not give you the number of sources. Derived tables get a row too, with `source_api = 'derived'` and the input tables listed in `endpoint`:

```
events         derived   prices_daily + earnings + market_benchmark (SPY) + shares_outstanding   2953
event_features derived   prices_daily + market_benchmark + income_statement + events             2953
event_regime   derived   vix + geopolitical_risk + uncertainty_indices + macro_series            2953
```

That is the whole dependency graph of layer 3, written down inside the database.

**Easy to get wrong:** `rows_written` is per *run*, not a running total. `shares_outstanding` appears twice, with 7,208 and 52 — a first pass and a top-up, summing to the 7,260 rows in the table. Reading either number alone as the table's size is wrong.

---

## `data_exclusions` — 240 rows

**One row is one record that was deliberately dropped, with the reason.**

Key: `id` (auto-numbered). Written by whichever script did the dropping.

| column | type | meaning |
|---|---|---|
| `id` | INTEGER | counter |
| `table_name` | TEXT | which table the record would have gone into |
| `symbol` | TEXT | the company |
| `ref` | TEXT | the excluded key — a date, a date range, or a `fiscal_date` |
| `reason` | TEXT | short machine-readable code |
| `detail` | TEXT | the evidence, in numbers |
| `run_utc` | TEXT | timestamp |

No NULLs. The full breakdown, which sums exactly to 240:

| table | reason | rows |
|---|---|---|
| `events` | `timing_unknown` | 164 |
| `income_statement` | `no_reports_in_window` | 22 |
| `shares_outstanding` | `no_shares_data` | 22 |
| `events` | `no_trading_day_after_report` | 10 |
| `events` | `fewer_than_5_days_after_t0` | 8 |
| `events` | `no_price_series` | 6 |
| `companies` | `no_master_data` | 4 |
| `prices_daily` | `level_outlier_vs_local_median` | 3 |
| `prices_daily` | `no_price_data` | 1 |

**This is the table that turns a silent gap into a defensible one.** Every dropped record leaves a receipt with its evidence. A representative `detail`:

```
KLAC  2026-06-08  level_outlier_vs_local_median
      adj_close=210.8060 local_median=1940.0400 ratio=0.109
```

That is a vendor error — a stock does not fall 89 % and recover — and the row says so with the numbers that justify the call, rather than the price simply not being there.

**Easy to get wrong:** the 188 `events` exclusions are not lost data, they are the arithmetic that turns 3,141 announcements into 2,953 events (see the `events` section). And `ref` is not one type of thing: it holds a single date for `prices_daily`, a `fiscal_date` for `events`, and a range like `2020-01-01..2026-06-30` for a symbol excluded wholesale.

---

## `study_meta` — 10 rows

**One row is one setting of the study.** Two columns: `key` and `value`.

Key: `key`. Written by `scripts/00_init_db.py` (the first seven) and `scripts/19_define_levels.py` (the last three).

Ten rows because there are ten settings. It is a **key/value table**, not a data table — the row count is a property of how many decisions were pinned, and it will never grow with the data.

| `key` | what it records |
|---|---|
| `study_window_start` | `2025-01-01` |
| `study_window_end` | `2026-06-30` |
| `n_constituents` | `530` |
| `constituent_basis` | why 530 and not 500: the union of everyone who was a member at any point, so that removals stay in the sample |
| `price_basis` | that `adj_close` is the return series, `close` is raw, and OHL are scaled onto the adjusted basis |
| `eps_basis` | that EPS is street, not GAAP, and cannot be reconciled to net income ÷ shares |
| `initialised_utc` | `2026-08-10T10:35:55+00:00` |
| `level1_definition` | the full prose definition of tier 1 |
| `level2_definition` | the full prose definition of tier 2 |
| `level_boundary_rationale` | "Numeric market record vs. text and external conditions. Realised volatility is level 1, implied volatility (VIX) is level 2 — deliberately." |

**Why it is worth having at all.** These are the four or five sentences a viva examiner will ask you to justify, and they live *inside the data file* rather than in a document that can drift away from it. A copy of `thesis.db` alone answers "what window?", "why 530?", and "which price column?" without any accompanying prose.

---

# 7. The two views

These get more space than the tables above, because they are what the experiment actually reads. Every consumer — the evidence-pack builder, the metrics script, the robustness script, the scorer — queries a view, not the underlying tables.

**A reminder of what a view is**, because it is the one concept here with no spreadsheet analogue: a view is a **stored query**, not stored data. `v_level1` occupies no meaningful disk space and holds no rows of its own. Every time something selects from it, SQLite runs the SELECT below and hands back the result. Two consequences follow, and both are the reason the views exist:

1. **It cannot go stale.** Rebuild `event_features` and `v_level1` is different the next time it is read, automatically. There is no copy to forget to refresh.
2. **It is a single place to change the definition.** If a field moves in or out of level 1, one `CREATE VIEW` changes and every consumer follows. Nothing has to be kept in sync by hand.

Both views are created by `scripts/19_define_levels.py`, which drops and recreates them each run.

---

## `v_level1` — 2,953 rows, 34 columns

**One row is everything a level-1 prompt is allowed to know about one event, plus the answer key for grading it.**

```sql
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
  LEFT JOIN event_features f USING (symbol, fiscal_date)
```

### What it joins, and why

Three tables, because a level-1 prompt needs three things that live in three places:

| from | contributes | join |
|---|---|---|
| `events` (`e`) | the announcement, t0, the EPS figures, the price and cap at t0, the four guard flags, and the outcome | the driving table |
| `companies` (`c`) | name, sector, industry — the identity block of the prompt | `JOIN … USING (symbol)` |
| `event_features` (`f`) | the reaction, trailing returns and volatility, valuation, growth, market regime | `LEFT JOIN … USING (symbol, fiscal_date)` |

**Why `JOIN` for companies and `LEFT JOIN` for features.** A plain `JOIN` keeps only rows that match on both sides; a `LEFT JOIN` keeps every row of the left table and fills the right side with NULLs where there is no match. Every event has a company, so the plain join is safe and loses nothing — all 2,953 survive. The `LEFT JOIN` on features is defensive: if a future rebuild ever produced an event without features, the event would still appear here with NULL features rather than vanishing from the sample without a trace. Today both joins match 2,953 of 2,953, so the two behave identically — but the intent differs, and the defensive one is the correct choice for the side that could in principle be incomplete.

The view is one row per event because `event_features` is keyed on the same `(symbol, fiscal_date)` pair. That is not an accident, and `scripts/24_score_experiment.py` checks it at runtime: it aborts if `v_level1` ever returns more than one row for a `(symbol, report_date)`, because a duplicate would silently double-count an event in the scoring.

### The 34 columns

| block | columns |
|---|---|
| event identity | `symbol`, `fiscal_date`, `report_date`, `before_after_market`, `t0` |
| company identity | `name`, `sector`, `industry` — **8 NULLs each**, being the 8 events belonging to SATS and WBA |
| the announcement | `eps_actual`, `eps_estimate` (3 NULL), `eps_surprise_pct` (4 NULL) |
| price at t0 | `close_at_t0` (raw), `market_cap_at_t0` (34 NULL) |
| the t0 reaction | `reaction_return`, `reaction_abnormal`, `volume_ratio` |
| trailing | `ret_21d` (1), `ret_63d` (2), `ret_252d` (10), `volatility_252d` (2), `pct_of_52w_range`, `drawdown_from_252d_high` |
| valuation and growth | `pe_trailing` (186), `ps_trailing` (34), `revenue_yoy` (34), `net_income_yoy` (245) |
| market regime | `spy_ret_21d`, `spy_ret_63d`, `spy_volatility_21d` |
| guard flags | `split_after_t0`, `has_fundamentals`, `in_index_at_t0` |
| **the answer key** | **`label`, `abnormal_return`** |

NULL counts pass through unchanged from the underlying tables; the meanings are the ones given in the `events` and `event_features` sections above.

### `label` and `abnormal_return` must never reach a model — how that is actually enforced

These two columns are the ground truth. They are computed from prices *after* the forecast is due. If either one appeared in a prompt, the entire experiment would measure nothing.

They are nonetheless *in* the view, and deliberately so: the same query has to serve both prompt-building and grading, and separating them into two views would create the risk of the two drifting apart and grading the wrong events. So the enforcement is not "the columns are absent" — it is at the layer where text gets rendered. Stating it precisely, because this is a viva question:

- `scripts/20_build_evidence_packs.py` selects `SELECT * FROM v_level1`, so both columns *are* fetched, and they are copied into the output JSON as two top-level fields, `label` and `abnormalReturn`, marked in the source with the comment `# ground truth, NEVER shown to a model`. The runner uses those fields to grade; the model never sees the JSON.
- The two functions that build the text a model reads, `level1_text(r)` and `level2_text(con, r)`, never reference `r["label"]` or `r["abnormal_return"]`. They touch 20 other columns of the row and not those two.
- **The check is in the self-test.** The test fixture is constructed with `label=1` and `abnormal_return=0.1234567` — a value chosen so that if it ever leaked through the ordinary percentage formatter it would render as the distinctive string `12.35%`. The test then renders a full level-2 pack and asserts that none of the four strings `"0.1234567"`, `"12.35%"`, `"abnormal_return"` or `"label"` appears anywhere in it. Any future edit that adds a convenient `r['abnormal_return']` to a sentence fails the test immediately.

Run it with:

```
python scripts/20_build_evidence_packs.py --self-test
```

The self-test also asserts that the level-2 text contains the level-1 text **verbatim** and adds to it, so the two tiers differ by addition only — which is what makes the tier comparison a clean contrast rather than two different prompts.

### Who reads this view

| script | what it takes |
|---|---|
| `20_build_evidence_packs.py` | everything; renders the prompt text and carries the label separately for grading |
| `21_metrics.py` | `eps_surprise_pct, label`, filtered to `t0 > '2025-04-28' AND in_index_at_t0 = 1` — the logistic-regression baseline |
| `22_robustness_optional.py` | `eps_surprise_pct, label, t0` — the optional robustness checks |
| `24_score_experiment.py` | `symbol, report_date, label` — the grading lookup, read-only, with a duplicate check |

That `t0 > '2025-04-28'` filter is the model-release cutoff (only events after the newest model's training cutoff can be forecast without contamination), and combined with `in_index_at_t0 = 1` it leaves **2,172 of the 2,953 events** eligible.

---

## `v_level2_regime` — 2,953 rows, 27 columns

```sql
CREATE VIEW v_level2_regime AS SELECT * FROM event_regime
```

**One row is the macro and risk regime at one event's t0 — identical, column for column, to `event_regime`.**

It joins nothing. It selects everything from one table and renames it. That is not a mistake, and the reason is worth stating clearly because "why does this view exist?" is an obvious question.

**Why a pass-through view is the right thing here.** It is a **stable name for a moving target**. The consumers ask for "the level-2 regime block"; they should not have to know that it currently happens to live in a table called `event_regime`. If the level-2 regime block later needs to join in something extra — say a second uncertainty measure, or a column from `event_features` — the change is one `CREATE VIEW` statement and every consumer follows without edit. Without the view, the same change means finding and editing every query in the codebase, and being wrong about one of them.

It also makes the pair symmetrical: `v_level1` and `v_level2_regime` are the two things a prompt builder reads, and they are named as a pair. `scripts/20_build_evidence_packs.py` reads it with a single query per event:

```sql
SELECT * FROM v_level2_regime WHERE symbol = ? AND fiscal_date = ?
```

Its 27 columns and their meanings are exactly those listed under `event_regime` above; there are no NULLs in any of them.

**Two things to be clear about.**

First, **level 2 is not this view.** Level 2 is level 1 *plus* this regime block *plus* the news (`event_news`, `event_news_summary`) *plus* the calendar overlaps (`event_macro_overlap`). Those last two are one-to-many — an event has many news items and can have several macro releases — so they cannot be folded into a one-row-per-event view without duplicating events. They stay in their own tables and the pack builder queries them separately. The `CREATE VIEW` comment in `19_define_levels.py` says exactly this.

Second, **this view carries no outcome columns**, unlike `v_level1`. There is no `label` and no `abnormal_return` in `event_regime`, so there is nothing here to leak.

---

# 8. Looking at it yourself

Four snippets. Copy, paste, run. All of them open the database the same way, and it is worth understanding why.

## Opening it, read-only

```python
import sqlite3

con = sqlite3.connect("file:thesis.db?mode=ro", uri=True)
```

Two pieces of that line matter:

- **`uri=True`** tells Python to read the string as a URI — an address with options after a `?` — rather than as a plain filename. Without it, Python would look for a file whose name is literally `file:thesis.db?mode=ro`, and fail.
- **`mode=ro`** is the option that URI enables: open **read-only**. Any `INSERT`, `UPDATE`, `DELETE` or `DROP` fails with an error instead of doing damage.

Every snippet here uses both, and so does every analysis script in this project, because the database is the *result* of a long and expensive build. `21_metrics.py`, `22_robustness_optional.py`, `24_score_experiment.py` and `25_select_event_sample.py` all open it this way. A stray `DELETE` typed into an interactive session while exploring would mean rerunning the whole fetch — hundreds of API calls, and some of the source series are moving targets that would not come back identical. Read-only makes that class of accident impossible rather than unlikely.

## Listing everything in it

```python
for row in con.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"):
    print(row)
```

`sqlite_master` is SQLite's own catalogue: a table describing every table, view and index in the file. The `NOT LIKE 'sqlite_%'` clause hides SQLite's internal objects, which is what removes `sqlite_sequence` and gets you 22 tables and 2 views rather than 23 and 2.

To add row counts:

```python
names = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
for n in sorted(names):
    print(f"{n:24} {con.execute(f'SELECT count(*) FROM {n}').fetchone()[0]:>9,}")
```

## Reading one event with its context

```python
con.row_factory = sqlite3.Row          # lets you write r["symbol"] instead of r[0]

r = con.execute(
    "SELECT * FROM v_level1 WHERE symbol = ? AND report_date = ?",
    ("AAPL", "2025-01-30")).fetchone()

print(r["name"], r["sector"])
print("t0", r["t0"], "close", r["close_at_t0"])
print("EPS", r["eps_actual"], "vs", r["eps_estimate"],
      f"({r['eps_surprise_pct']:.1f}% surprise)")
print("outcome (NEVER show a model):", r["abnormal_return"], "label", r["label"])

g = con.execute(
    "SELECT * FROM v_level2_regime WHERE symbol = ? AND fiscal_date = ?",
    (r["symbol"], r["fiscal_date"])).fetchone()
print("VIX", g["vix"], f"({g['vix_pctile_5y']:.0%} of 5y)", "yield curve", g["yield_curve"])

for n in con.execute(
        "SELECT published_et, relative_timing, source, sentiment_label, title "
        "FROM event_news WHERE symbol = ? AND fiscal_date = ? "
        "ORDER BY published_et", (r["symbol"], r["fiscal_date"])):
    print(" ", n["published_et"], n["relative_timing"], n["source"], n["sentiment_label"])
```

The `?` placeholders are how values are passed into SQL. Never build a query by pasting values into the string — the placeholder version is both safer and faster, and it handles quoting for you.

`row_factory = sqlite3.Row` is a small quality-of-life change with a real benefit: without it a row is a plain tuple and you address columns by position, so `r[29]` is the label and you have to count. With it you write `r["label"]`, and inserting a column into the view later cannot silently shift what your code reads.

## Seeing a view's definition

```python
print(con.execute(
    "SELECT sql FROM sqlite_master WHERE name = ?", ("v_level1",)).fetchone()[0])
```

That prints the exact `CREATE VIEW` statement quoted in §7. The same query works on any table name and prints its `CREATE TABLE`, including the inline comments — which is where several of the definitions in this document came from, and the fastest way to check that this file has not drifted from the database:

```python
print(con.execute(
    "SELECT sql FROM sqlite_master WHERE name = ?", ("events",)).fetchone()[0])
```

---

## Where each object comes from

One table, for looking up which script to read when a column is not clear.

| object | written by |
|---|---|
| `companies` | `00_init_db.py` creates the rows, `01_eodhd_company_master.py` fills them |
| `earnings` | `02_eodhd_earnings_eps.py`, then `03_…_surprise.py`, `04_…_timing.py` |
| `prices_daily` | `05_eodhd_daily_prices.py` |
| `income_statement` | `06_alphavantage_income_statement.py` |
| `market_benchmark` | `07_alphavantage_market_benchmark.py` |
| `shares_outstanding` | `08_alphavantage_shares_outstanding.py` |
| `events`, `index_membership` | `09_build_events.py` |
| `event_features` | `11_derive_context_features.py` |
| `event_news`, `event_news_summary` | `12_alphavantage_news.py` |
| `macro_series` | `13_alphavantage_macro.py` |
| `geopolitical_risk` | `14_geopolitical_risk.py` |
| `macro_event_calendar`, `event_macro_overlap` | `15_macro_event_calendar.py` |
| `uncertainty_indices` | `16_uncertainty_indices.py` |
| `vix` | `17_vix.py` |
| `event_regime` | `18_derive_event_regime.py` |
| `level_assignment`, `v_level1`, `v_level2_regime` | `19_define_levels.py` |
| `study_meta` | `00_init_db.py` and `19_define_levels.py` |
| `provenance`, `data_exclusions` | every script that fetches or drops |

`scripts/10_validate.py` writes nothing. It re-checks the structural invariants — keys, ranges, orphaned rows, non-positive prices — reports completeness per table, independently recomputes the EPS surprise as a cross-check of the provider's value, and exits non-zero if a hard invariant is broken. It is the fastest way to confirm the database is still what this document says it is.
