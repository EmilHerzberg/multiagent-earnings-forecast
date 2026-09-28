# Event dataset — S&P 500 earnings, prices and fundamentals (2025 – mid-2026)

Data pipeline for the experiment. Everything needed to rebuild the dataset
from the provider APIs is in this folder.

**Study window:** 2025-01-01 – 2026-06-30
**Universe:** 530 symbols — every company that was an S&P 500 member at any
point in the window (`sp500_constituents.txt`). Point-in-time membership is
reconstructed; see 7.1.

---

## 1. Requirements

```bash
python >= 3.10        # standard library only, no third-party packages
```

Two API subscriptions:

| Provider | Needed for | Minimum plan |
|---|---|---|
| [EODHD](https://eodhd.com/pricing) | earnings calendar, daily prices, company master data | EOD Historical + Fundamentals, or ALL-IN-ONE |
| [Alpha Vantage](https://www.alphavantage.co/premium/) | income statements | Premium (the free tier's 25 requests/day is insufficient for 530 symbols) |

Copy `.env.example` to `.env` next to this README and fill in the keys, or
export them as environment variables:

```
EODHD_API_KEY=your_key
ALPHA_VANTAGE_API_KEY=your_key
```

## 2. Building the dataset

```bash
python run_all.py
```

Or step by step — the scripts are numbered in dependency order, and 03/04
enrich rows created by 02:

```bash
python scripts/00_init_db.py --reset                    # schema, no API calls
python scripts/01_eodhd_company_master.py               # optional
python scripts/02_eodhd_earnings_eps.py
python scripts/03_eodhd_earnings_surprise.py --verify
python scripts/04_eodhd_earnings_timing.py
python scripts/05_eodhd_daily_prices.py
python scripts/06_alphavantage_income_statement.py
python scripts/07_alphavantage_market_benchmark.py      # SPY, 1 call
python scripts/08_alphavantage_shares_outstanding.py    # market-cap basis
python scripts/09_build_events.py                       # derived, no API calls
python scripts/10_validate.py
```

Check the wiring first with `python run_all.py --smoke 5` (5 symbols).

### API cost of a full build

| Provider | Calls | Note |
|---|---|---|
| EODHD `/calendar/earnings` | 6 | market-wide endpoint, quarterly chunks |
| EODHD `/eod` | 530 | one per constituent |
| EODHD `/fundamentals` | 530 | optional, `run_all.py --skip-master` to omit |
| Alpha Vantage `INCOME_STATEMENT` | 530 | ~8 min at 70 requests/minute |
| Alpha Vantage `TIME_SERIES_DAILY_ADJUSTED` | 1 | the SPY benchmark |
| Alpha Vantage `BALANCE_SHEET` | 530 | share counts for market cap |

Scripts 05 and 06 support `--resume` and skip symbols already loaded, so an
interrupted run can be continued without re-spending quota.

## 3. Layout

```
thesis/
├── README.md
├── run_all.py                 build everything
├── config.py                  study parameters + provider access layer
├── sp500_constituents.txt     the 530-symbol union universe
├── sp500_index_changes.csv    index additions/removals with effective dates
├── ticker_renames.csv         renames providers report as delistings
├── macro_event_calendar.csv   scheduled FOMC/CPI/NFP/PCE release dates
├── ai_gpr_daily.csv           cached AI-GPR geopolitical risk index
├── tpu_daily.csv              cached trade policy uncertainty
├── epu_daily.csv              cached US economic policy uncertainty
├── vix_daily.csv              cached VIX (Cboe)
└── scripts/
    ├── 00_init_db.py                       schema
    ├── 01_eodhd_company_master.py          companies      (optional)
    ├── 02_eodhd_earnings_eps.py            EPS actual + estimate
    ├── 03_eodhd_earnings_surprise.py       EPS surprise %
    ├── 04_eodhd_earnings_timing.py         before/after market
    ├── 05_eodhd_daily_prices.py            daily OHLCV
    ├── 06_alphavantage_income_statement.py revenue, net income, EBIT, EBITDA
    ├── 07_alphavantage_market_benchmark.py SPY, for abnormal returns
    ├── 08_alphavantage_shares_outstanding.py market-cap basis
    ├── 09_build_events.py                  t0, window, abnormal return, label
    ├── 11_derive_context_features.py       reaction, momentum, valuation, regime
    ├── 12_alphavantage_news.py             news + sentiment per event
    ├── 13_alphavantage_macro.py            macro series with publication lags
    ├── 14_geopolitical_risk.py             GPR index (Caldara & Iacoviello)
    ├── 15_macro_event_calendar.py          macro releases inside the window
    ├── 16_uncertainty_indices.py           trade + economic policy uncertainty
    ├── 17_vix.py                           VIX implied volatility
    ├── 18_derive_event_regime.py           regime snapshot per event
    ├── 19_define_levels.py                 pins the level-1 / level-2 split
    └── 10_validate.py                      integrity + coverage report
```

Scripts 00-09 build the **level-1** dataset (the announcement and its outcome).
Scripts 11-18 build the **level-2 context tier**; `run_all.py --level1-only`
skips them. Script 19 pins the level split (section 9).

One script per dataset. Each documents its endpoint, the response fields it
consumes, and the definition of what it stores. Script 09 makes no API calls —
it derives the forecastable events from the tables above.

## 4. Data sources

| Dataset | Provider | Endpoint |
|---|---|---|
| EPS actual / estimate | EODHD | `/api/calendar/earnings` |
| EPS surprise % | EODHD | `/api/calendar/earnings` (`percent`) |
| Announcement timing | EODHD | `/api/calendar/earnings` (`before_after_market`) |
| Daily OHLCV | EODHD | `/api/eod/{SYMBOL}.US` |
| Company master data | EODHD, Alpha Vantage fallback | `/api/fundamentals/{SYMBOL}.US?filter=General` ; `OVERVIEW` |
| Income statements | Alpha Vantage | `/query?function=INCOME_STATEMENT` |
| Market benchmark (SPY) | Alpha Vantage | `/query?function=TIME_SERIES_DAILY_ADJUSTED` |
| Shares outstanding | Alpha Vantage | `/query?function=BALANCE_SHEET` |
| Index membership | S&P DJI press releases + change logs | `sp500_index_changes.csv` |

The `provenance` table records endpoint and row count per table, so the origin
of any row can be traced back from the database itself.

## 5. Schema

**`companies`** — one row per constituent: `symbol`, `name`, `sector`,
`industry`, `country_iso`, `fiscal_year_end`, `isin`, `cik`, `ipo_date`, and
`data_source` recording which provider supplied the profile.

Sector and industry come from EODHD `/fundamentals?filter=General`, with Alpha
Vantage `OVERVIEW` as a per-symbol fallback. Both use the **Morningstar-style**
taxonomy — "Technology", "Consumer Defensive", "Financial Services", … — not
GICS. Alpha Vantage returns them upper-cased, so values are normalised to title
case on ingest.

That the two are interchangeable is verified rather than assumed: each provider
yields exactly **11 distinct sector labels** on this universe, and the union is
also 11 — no near-duplicates survived the merge.

Coverage: **528 of 530 (99.6 %)**. The two gaps, SATS and WBA, affect **8 of
2,953 events (0.27 %)** and are left NULL rather than guessed. `isin` stays at
71.1 % because Alpha Vantage does not supply it; it is not used in either
information tier.

**`earnings`** — one row per company-quarter, keyed `(symbol, fiscal_date)`:

| Column | Meaning |
|---|---|
| `fiscal_date` | end of the reported fiscal quarter |
| `report_date` | announcement date — the event-study anchor |
| `eps_actual` | reported EPS (street basis) |
| `eps_estimate` | consensus EPS estimate |
| `eps_surprise_pct` | `(actual − estimate) / \|estimate\| × 100` |
| `before_after_market` | `BeforeMarket` / `AfterMarket` / `DuringMarket` |

**`prices_daily`** — one row per company-day, keyed `(symbol, date)`:
`open`, `high`, `low`, `close`, `adj_close`, `volume`.

**`income_statement`** — one row per company-quarter, keyed
`(symbol, fiscal_date, period)`: `revenue`, `net_income`, `operating_income`,
`ebit`, `ebitda`, `gross_profit`, `reported_currency`.

**`market_benchmark`** — SPY daily bars (`adj_close` = total-return basis).

**`shares_outstanding`** — quarterly `commonStockSharesOutstanding` per company,
on the current split basis. Market cap is derived, not stored (see 5.1).

**`events`** — the forecastable units, one row per announcement that has a
complete outcome window. Keyed `(symbol, fiscal_date)`:

| Column | Meaning |
|---|---|
| `t0` | first trading day that prices the announcement (see 5.1) |
| `window_start` / `window_end` | `t0+1` … `t0+horizon` |
| `stock_return` / `market_return` | total returns over the window |
| `abnormal_return` | `stock_return − market_return` |
| `label` | `1` if `abnormal_return > 0`, else `0` |
| `close_at_t0` | **raw** close — the level safe to show a model |
| `market_cap_at_t0` | `adj_close(t0) × lagged share count` |
| `shares_fiscal_date` | which quarter the share count came from |
| `split_after_t0` | a split occurs later — must stay hidden from the prompt |
| `split_in_lookback` | the raw series jumps before `t0` |
| `has_fundamentals` | an `income_statement` row exists for this company |
| `in_index_at_t0` | **1 = the company was an S&P 500 member on `t0`** |

**`index_membership`** — per company, `in_index_from` / `in_index_to` and where
each date came from (`verified` / `aggregator` / `price_series` /
`assumed_at_window_start`).

A company leaves the index when S&P removes it **or** when its security stops
trading, whichever comes **first**, so `in_index_to` is the earlier of the
change-log date and the day after the last price bar, and `to_source` names
whichever of the two produced it: `verified` / `aggregator` when the change log
decided, `price_series` when the security stopped trading first (an
acquisition usually closes the tape days before the index change takes effect).
`price_series` also closes a company that stopped trading without appearing in
the change log at all. On the current build 23 of the 30 removal dates come
from the change log and 7 from the price series. A security counts as having
stopped trading only if its last bar precedes the newest date in `prices_daily`
by more than a week — the panel ends ragged because a vendor backfill does not
finish in one session, and comparing against the study-window end instead once
re-dated **every** removal to the end of the data.

Why 502 symbols carry `assumed_at_window_start`, how much rests on the
unverified dates, and how to close it without a data licence: see
the project's working notes (not part of this repository).

### Level-2 context tables

**`event_features`** — one row per event, everything measured **as of the close
of t0**: the announcement-day reaction (`reaction_return`, `reaction_abnormal`,
`volume_ratio`), trailing momentum and risk (`ret_21d/63d/252d`,
`volatility_252d`, `pct_of_52w_range`, `drawdown_from_252d_high`), valuation and
growth (`pe_trailing`, `ps_trailing`, `revenue_yoy`, `net_income_yoy`), and
market regime (`spy_ret_21d/63d`, `spy_volatility_21d`).

**`event_news`** / **`event_news_summary`** — news published between
`report_date − lookback` and the close of t0, with per-item relevance and
sentiment. `relative_timing` marks each item `pre_announcement`,
`post_announcement` or `ambiguous` (see 6.5).

**`macro_series`** — CPI, unemployment, retail sales, fed funds and 2y/10y
Treasury yields, each with a `public_from` date (see 7.14).

**`geopolitical_risk`** — the daily **AI-GPR** index (GPT-4o-mini scoring of ~5m
newspaper articles, Caldara & Iacoviello et al. 2026) with threats/acts and
oil/non-oil sub-indices, plus 7- and 30-day trailing means. `gpr_aer` retains
the original keyword-based index for comparison.

**`uncertainty_indices`** — daily Trade Policy Uncertainty (with the publisher's
7- and 30-day moving averages) and US Economic Policy Uncertainty.

**`vix`** — daily VIX from Cboe. Complements the *realised* volatility in
`event_features`: VIX is what the options market *expects*.

**`event_regime`** — all of the above snapshotted **as of the close of t0**, one
row per event, 24 indicators including the 10y−2y yield curve and five-year
percentile ranks. This is where the point-in-time rule for regime data lives, so
prompt-building code does not have to re-implement it.

**`macro_event_calendar`** / **`event_macro_overlap`** — scheduled FOMC, CPI,
NFP and PCE release dates, and which of them fall inside each event's forecast
window.

**`provenance`** — which endpoint populated which table.
**`data_exclusions`** — every deliberately dropped record, with its reason.
**`study_meta`** — window, universe size, and the conventions below.

### 5.1 How an event is defined

`t0` is the first trading day that prices the announcement:

| `before_after_market` | `t0` |
|---|---|
| `BeforeMarket` | `report_date` |
| `DuringMarket` | `report_date` |
| `AfterMarket` | next trading day after `report_date` |

If `report_date` falls on a non-trading day, `t0` moves forward to the next one.

The forecast is made **after `t0` closes**, and `t0` is excluded from the
outcome window — the first session mainly prices the mechanical surprise in the
report itself. The window is the following `horizon` trading days (default 5):

```
stock_return    = adj_close(t0+5) / adj_close(t0) − 1
market_return   = Π (1 + SPY daily total return) over t0+1 … t0+5, − 1
abnormal_return = stock_return − market_return
label           = 1 if abnormal_return > 0 else 0
```

Both sides use adjusted closes, so both are total returns. Pairing an adjusted
stock return with a price-only index return would bias every event by roughly
the dividend yield over the window.

## 6. Conventions the analysis must respect

### 6.1 `adj_close` for returns, **raw `close` for anything shown to a model**

This is two rules, and getting either backwards breaks something different.

**Returns must use `adj_close`.** `close` is the raw close as delivered.
Booking.com (BKNG) shows why: its raw close falls from ~4,194 to ~176 on
2026-04-06 (a split) while the adjusted close runs through continuously. A
return series built on `close` would record a −96 % day that never happened.

**Price *levels* shown to a model must use raw `close`.** The adjusted close is
retroactively scaled by every split and dividend that occurs *after* that date,
up to the retrieval date — so it embeds future information. For a BKNG event in
January 2025, `adj_close` reads ≈ 197 while the stock actually traded at
≈ 4,990. Presenting 197 as "the current price" hands the model a 24:1 split
that will not happen for another 15 months.

Measured on this dataset, inside the study window:

| Adjustment-factor drift | Symbols | Cause |
|---|---|---|
| > 50 % | 8 | splits (AMCR, BKNG, DD, FAST, NFLX, NOW, …) |
| 0.1 – 50 % | 404 | dividends |
| none | 88 | no distributions |

Returns are unaffected in all three groups — the factor cancels in any ratio
`adj(t)/adj(t−1)`. Only levels leak. `events.close_at_t0` therefore stores the
**raw** close, and `events.split_after_t0` flags the rows where a split occurs
later so a prompt builder can assert it is never revealed. See 7.10.

Note: `asset_splits`-style corporate-action records are **not** available for
this window from the price endpoint; splits are detectable only via the
`adj_close/close` ratio, which is what `09_build_events.py` does.

### 6.2 `AfterMarket` announcements react on the *next* trading day

About 40 % of announcements in this window are `AfterMarket`. Anchoring every
event on `report_date` regardless of timing mixes two different event windows
and attenuates measured abnormal returns.

| `before_after_market` | first reaction day |
|---|---|
| `BeforeMarket` | `report_date` |
| `DuringMarket` | `report_date` |
| `AfterMarket` | next trading day after `report_date` |
| `NULL` | ambiguous — exclude or handle explicitly |

`scripts/04_eodhd_earnings_timing.py::event_day_sql()` implements the rule.

### 6.3 `eps_actual` is street EPS, not GAAP EPS

`eps_actual` and `eps_estimate` come from the earnings calendar and are the
consensus-comparable figures — generally diluted and adjusted for non-recurring
items. That like-for-like basis is what makes the surprise well defined, but:

- there is **no basic/diluted split** available;
- `eps_actual` **cannot be reconciled** with `net_income` from
  `income_statement` divided by a share count.

### 6.4 News timing is classified, not assumed

A story published before the numbers were public is anticipation; one published
after is reaction. They mean different things even at identical sentiment, so
`event_news.relative_timing` separates them.

The announcement time is inferred from `before_after_market` (BeforeMarket →
09:30 ET, AfterMarket → 16:00 ET, DuringMarket → the whole session is
ambiguous), with a ±30-minute band around it.

`time_published` is **US Eastern, not UTC**. The provider does not document
this; it was established empirically — for after-close reporters, press releases
land at 16:01 and later on the report date, which under UTC would fall in the
middle of the session. Re-verify if the provider changes the field.

None of this affects the label: the forecast is made after t0's close and the
query already cuts at that point. The classification exists so a prompt can
present news honestly rather than as an undifferentiated blob.

### 6.5 The surprise denominator is the estimate, not the price

`eps_surprise_pct` explodes when the estimate is near zero. Winsorise, or use a
standardised measure (SUE, scaled by forecast dispersion or by price), before
putting it into a regression.

## 7. Known limitations

Properties of the dataset, not defects to be fixed silently.

**7.1 Index membership is reconstructed, not assumed.** Status: **largely
mitigated.**

The universe is the **union** of every company that was an S&P 500 member at
any point in the window — 530 symbols, not a single-date snapshot. Membership
intervals live in `index_membership`, built from `sp500_index_changes.csv`
(30 additions, 29 removals). `events.in_index_at_t0` marks whether the company
was actually a member on the reaction day, so **the point-in-time sample is
`WHERE in_index_at_t0 = 1`**.

Why this mattered: the original 501-symbol list turned out to approximate
membership at the *start* of the window, not the end. It therefore retained
most companies later removed (the eventual losers) and omitted 27 companies
added during the window (COIN, HOOD, APP, CVNA, DASH, …) — a conservative
distortion, but a distortion. Reconstruction adds those 27 plus 5 missing
removals (BWA, DFS, FMC, SOLS, TFX).

Date provenance is recorded per row. Effective dates for 2025-03-24,
2025-05-19, 2025-07-09, 2025-12-22 and 2026-06-22 were confirmed against S&P
Dow Jones Indices press releases (`confidence = verified`); the rest come from
secondary change logs (`confidence = aggregator`). Two aggregator dates were
found to be wrong when checked against the press releases — COIN/DFS and
DDOG/JNPR were both listed with the wrong effective date — so the unverified
dates should be treated as approximate.

Where a company stopped trading (acquisition, take-private, rename), the
removal date is instead derived from the last observed price bar, which is
authoritative for when the security ceased to be investable
(`to_source = 'price_series'`).

**Residual weakness:** companies absent from the change file are assumed to
have been members for the whole window. A company that both joined *and* left
before the window would be missed entirely, and the base list itself is an
approximation of membership on 2025-01-01. `10_validate.py` reports how many
events sit within 30 days of a membership boundary, which bounds the damage any
remaining date error can do.

**7.1b Three "delisted" companies were actually ticker renames.** Providers
model a rename as a delisting: the old symbol is flagged dead on the rename
date and a *new* symbol carries the full back-history. Detected by matching
company names of delisted symbols against long-history active symbols:

| Old | New | Company | Effect of not catching it |
|---|---|---|---|
| MMC | MRSH | Marsh & McLennan | series truncated at 2026-01-13 |
| BK | BNY | Bank of New York Mellon | truncated at 2026-05-20 |
| FI | FISV | Fiserv | truncated at 2025-11-10 |

All three are active S&P 500 members with complete data under the new symbol,
including 81 quarters of fundamentals. Left uncorrected, each would have lost
its later events *and* its financial statements — and would have been
misclassified as an index departure. The mapping is in `ticker_renames.csv`;
the new symbol is canonical throughout.

**7.2 Price coverage is truncated at 2026-06-11.** The stated window runs to
2026-06-30, but the last available bar is 2026-06-11 — the final ~three weeks
carry no price data. Earnings announcements *are* present through the end of
June, so late-June announcements exist without a complete outcome window.

Measured cost: **6 events**, dropped by `09_build_events.py` under
`fewer_than_5_days_after_t0`. Alpha Vantage does reach further (through
2026-08), but backfilling the tail from a second provider would mix adjustment
conventions mid-series and — more seriously — Alpha Vantage's delisted-symbol
coverage is unreliable (see 7.9, and the BK/PARA evidence in
`07_alphavantage_market_benchmark.py`). Losing 6 of ~2,800 events is the
cheaper trade. Treat the effective window as **2025-01-01 – 2026-06-11** and
state the truncation. `10_validate.py` reports the actual range on every build.

**7.3 Fundamentals are not point-in-time.** Alpha Vantage returns the current
state of each historical statement, including later restatements. Figures for a
2025 quarter are as reported today, not as first published. For tests
conditioning on information available at announcement, anchor on `report_date`
and treat restatement as a documented limitation.

**7.4 One constituent has no price series.** CBOE is absent from the provider's
price universe; its earnings rows are present. Logged in `data_exclusions`.

**7.5 A small number of corrupted vendor bars are filtered.** A bar is rejected
when its adjusted close deviates from the surrounding 21-bar median by more than
a factor of 3 — no S&P 500 constituent moves 3x in a day, but mis-mapped vendor
bars do. The filter runs on the *adjusted* close, so genuine splits are
unaffected. In the reference build exactly 3 of 178,487 bars were rejected, all
KLAC (2026-06-08/09/10), where the adjusted close collapses to ~210 and returns
to ~2,411 on 06-11. Every rejection is logged in `data_exclusions` with its
measured ratio; `--no-filter` disables the filter.

**7.6 Fiscal-period conventions differ between providers.** An exact
`(symbol, fiscal_date)` join from `earnings` to `income_statement` matched
2,487 of 2,972 rows (83.7 %) in the reference build. The shortfall is
convention mismatch on non-calendar fiscal years, not missing data. Join on the
nearest period end within a tolerance (e.g. ±15 days) rather than dropping rows.

**7.7 Some announcements carry no timing flag** (145 in the reference build), so
their event day is ambiguous — see 6.2.

**7.8 The income-statement table is survivorship-biased; earnings and prices are
not.** This is the most consequential limitation.

Alpha Vantage drops fundamentals entirely for companies no longer listed. All
twelve constituents acquired or delisted during the window — BK, CTRA, DAY, FI,
HES, HOLX, IPG, JNPR, K, MMC, PARA, WBA — return an empty payload, even for
quarters they reported while still trading. EODHD retains both their earnings
(2–6 reports each) and their price history, so `earnings` and `prices_daily`
cover all 501 constituents while `income_statement` covers 489.

The consequence: **an inner join from `earnings` to `income_statement` silently
drops exactly the delisted firms**, reintroducing survivorship bias into a
sample that is otherwise free of it. Use a LEFT JOIN and report the coverage, or
restrict the analysis to the 489 firms and state that restriction explicitly.

```sql
-- survivorship-safe: leavers retained, fundamentals NULL where unavailable
SELECT e.*, i.revenue, i.net_income, i.operating_income
  FROM earnings e
  LEFT JOIN income_statement i
    ON i.symbol = e.symbol AND i.fiscal_date = e.fiscal_date;
```

**7.9 No provider supplies fundamentals for the twelve delisted constituents.**
Verified across every accessible endpoint: Alpha Vantage returns an empty
payload from `INCOME_STATEMENT`, `EARNINGS` **and** `BALANCE_SHEET` for all
twelve (BK, CTRA, DAY, FI, HES, HOLX, IPG, JNPR, K, MMC, PARA, WBA); FMP's
income statement is restricted at the subscription tier used. EODHD retains
them, which is why their earnings and prices are complete.

Affected: **52 events, 1.7 % of the pool.** Market capitalisation is partially
recoverable — quarterly share counts exist for eleven of the twelve from the
EODHD-sourced data (all but WBA).

These events are deliberately **kept** in the pool with `has_fundamentals = 0`
rather than dropped. Dropping them would be precisely the survivorship bias
this design otherwise avoids, and at 1.7 % the cost of consistency is low. For
RQ1 it is immaterial: every topology sees the same events. A robustness check
re-running the analysis without these 52 events is cheap and settles the
question empirically rather than by assertion.

**7.10 Adjusted price levels embed future corporate actions.** Covered in
detail in 6.1. Status: **mitigated** — `events.close_at_t0` carries the raw
close, and `events.split_after_t0` (8 symbols) marks rows where split
information must be withheld from the prompt. Returns and labels were never
affected.

**7.11 Share counts are only public once the quarter is reported.** Attaching a
quarter's share count to an event before that quarter's earnings announcement
would leak. Status: **mitigated** — `09_build_events.py` selects the most
recent quarter whose `report_date` precedes `t0` (falling back to a 90-day lag
where no announcement is recorded), and stores which quarter was used in
`events.shares_fiscal_date`.

**7.12 The consensus estimate may not be the pre-announcement vintage.**
`eps_estimate` is stored as a single value; whether it is the consensus
immediately before the announcement or a later revision cannot be determined
from the response. The surprise cross-check (0 deviations) establishes internal
consistency, not timing correctness. Status: **not mitigable** without vintage
consensus data (I/B/E/S class). Documented; the surprise is a central input, so
this is a genuine residual risk.

**7.13 Model-side contamination.** Any model whose training data covers the
study window may have memorised outcomes. Status: **mitigated by design** — the
window begins 2025-01, after the training cutoffs of the intended open-weight
models. Verify the cutoff per checkpoint, and note that hosted "open weight"
endpoints can be silently updated.

**7.14 Macro releases carry a publication lag.** The Alpha Vantage economic
endpoints return the **observation date**, not the release date: the CPI
observation dated 2026-06-01 describes June but is published in mid-July.
Status: **mitigated** — `macro_series.public_from` adds a conservative lag per
series (CPI 45 d, unemployment 30 d, retail sales 45 d) and downstream use
requires `public_from < t0`. Fed funds and Treasury yields are market prices
quoted continuously, so they carry no lag.

This is a mitigation, not a solution: the true release date varies by a few days
each month, and the lags are deliberately generous. A feed with genuine release
timestamps would be better.

**7.15 The geopolitical risk index is a monthly-updated construct.** The GPR
index (Caldara & Iacoviello 2022) is built from same-day newspaper coverage, so
the value dated t0 was knowable at t0 — no look-ahead. But the series is
*published* monthly and revised, so a strictly point-in-time reconstruction
would need vintages that are not distributed. The revision affects the level
marginally, not the timing.

**7.16 News coverage is uneven.** Item counts vary by two orders of magnitude
across events (3 to 225 in a sample of 15). Coverage does *not* degrade with
company size — small constituents draw similar counts to large ones — but the
per-event cap (`--max-items`, default 15 by relevance) means high-attention
events are truncated. `event_news_summary.items_returned` records the
untruncated count so truncation is visible rather than silent.

### Leakage summary

| # | Vector | Risk | Damage | Status |
|---|---|---|---|---|
| 7.1 | Index composition (sample selection) | high | high | **largely mitigated** (PIT membership) |
| 7.1b | Ticker renames read as delistings | high | medium | **mitigated** (3 found and remapped) |
| 7.10 | Adjusted price levels | high | low | **mitigated** (raw close) |
| 7.3 | Restatements in fundamentals | medium | medium | partially — keep out of level 1 |
| 7.11 | Share count before filing | high | low | **mitigated** (lag) |
| 7.13 | Model training data | low | high | **mitigated** (window choice) |
| 7.12 | Consensus estimate vintage | medium | medium | **not mitigable** |
| 7.14 | Macro publication lag | high | low | **mitigated** (`public_from`) |
| 7.15 | GPR index revisions | low | low | documented |

On 7.1, the honest position: membership is now reconstructed rather than
assumed, and the point-in-time sample is `in_index_at_t0 = 1`. What remains is
date imprecision on the unverified changes and the assumption that the base
list matches membership at the window start. **RQ1 is insulated either way** —
it compares topologies on identical events, so any residual sample-level bias
shifts all arms together. The exposed claim is the absolute one ("beats the
base rate"), not the relative one ("debate beats ensemble").

## 9. The information tiers (pre-registered)

RQ3 compares two information tiers, so what is in each is a **design parameter**,
not an implementation detail. It is fixed in the database — `level_assignment`
lists every field with its tier, and `19_define_levels.py` is the single place
it is defined. Prompt-building code reads the views rather than deciding for
itself.

**The boundary is numeric market record vs. text and external conditions.**

### Level 1 — what the numbers say about this stock (28 fields)

| Block | Fields | Content |
|---|---|---|
| identity | 3 | name, sector, industry |
| announcement | 9 | quarter, dates, session, EPS actual/estimate/surprise, raw close, market cap |
| reaction | 3 | t0 return, abnormal t0 return, volume vs normal |
| trailing | 6 | 1/3/12-month returns, realised volatility, 52-week position, drawdown |
| valuation | 4 | trailing P/E, P/S, revenue and net-income growth year-on-year |
| market | 3 | SPY 1- and 3-month return, SPY realised volatility |

The market block sits in level 1 rather than level 2 because the outcome is an
*abnormal* return — measured against the market — so the market's own behaviour
is part of reading the stock's move, not external context. It also comes from
the same price series as the stock features.

Read it from the `v_level1` view: one row per event, everything joined.

### Level 2 — what the world says on top (32 further fields)

| Block | Fields | Content |
|---|---|---|
| news | 6 | headline, summary, timestamp, pre/post-announcement flag, sentiment, outlet |
| volatility | 2 | VIX and its 5-year percentile |
| geopolitics | 6 | AI-GPR, 30-day mean, threats, acts, oil sub-index, percentile |
| policy | 6 | trade-policy uncertainty (7d/30d means, percentile), US EPU and percentile |
| macro | 10 | fed funds, 2y/10y, yield curve, WTI, Brent, lagged CPI/unemployment/retail |
| calendar | 2 | scheduled FOMC/CPI/NFP/PCE releases inside the forecast window |

Read the one-per-event part from `v_level2_regime`; news and calendar overlaps
are one-to-many and stay in `event_news` and `event_macro_overlap`.

### One deliberate pairing across the boundary

Level 1 carries **realised** market volatility, level 2 carries **implied**
volatility (VIX). Same quantity, backward- and forward-looking, split on
purpose: whether the forward-looking measure adds anything is part of what RQ3
tests.

### Measured prompt size

Over 250 randomly drawn events (tokens ≈ characters / 4):

| | median | mean | p90 | max |
|---|---|---|---|---|
| Level 1 | 169 | 166 | 173 | 178 |
| Level 2 | 1,246 | 1,146 | 1,637 | 1,726 |

**Ratio at the median: 7.4×.** Level 1 is compact in tokens but not in
information — those 28 fields are essentially the full quantitative predictor
set. What it lacks is narrative. Level 2's spread is much wider because news
volume varies by event; the per-event cap keeps the tail bounded.

Note for compute matching: the tiers differ by design, so the information level
is a **separate axis** from the compute match. Matching happens *within* a tier,
across topologies.

## 8. Validation

```bash
python scripts/07_validate.py
```

Checks structural invariants (no NULL keys, no orphan rows, no non-positive
adjusted closes, no `high < low`, nothing outside the window) and prints
per-field completeness for every table.

It also re-derives `eps_surprise_pct` from `eps_actual` and `eps_estimate` as an
independent cross-check of the provider's value. In the reference build **0 of
2,959 rows deviated by more than 1 percentage point**, confirming the provider's
surprise field is internally consistent with its own EPS fields.

Reference build figures for comparison:

**Level 1 — the announcement and its outcome**

| Table | Rows | Symbols |
|---|---|---|
| `companies` | 530 | — |
| `earnings` | 3,141 | 530 |
| `prices_daily` | 842,950 | 529 |
| `income_statement` | 13,386 | 519 |
| `market_benchmark` | 1,631 | SPY |
| `shares_outstanding` | 7,260 | 519 |
| `index_membership` | 530 | 30 additions, 29 removals |
| **`events`** | **2,953** | **527** |

**Level 2 — context**

| Table | Rows | Note |
|---|---|---|
| `event_features` | 2,953 | 16 features, 91.7–100 % coverage |
| `event_news` | 28,277 | 2,953 events, 122 with no relevant news |
| `macro_series` | 5,853 | 6 indicators, 2020–2026 |
| `geopolitical_risk` | 2,373 | daily GPR, no event without a reading |
| `macro_event_calendar` | 60 | FOMC / CPI / NFP / PCE releases |
| `event_macro_overlap` | 2,780 | **65.8 % of events have ≥1 release in the window** |

News timing: 52.4 % pre-announcement, 45.9 % post, 1.7 % ambiguous. **0 items
published after `t0`** — the point-in-time cut is checked as a hard invariant.
45.8 % of events hit the 15-item relevance cap; `items_returned` records the
untruncated count.

Event-set headline figures:

| | |
|---|---|
| events built | 2,953 of 3,141 announcements (94.0 %) |
| **in index at `t0`** (point-in-time sample) | **2,869 (97.2 %)** |
| base rate — all events | 46.60 % · Brier 0.2488 |
| **base rate — in-index only** | **46.36 % · Brier 0.2487** |
| symbols with ≥4 events | 509 |
| timing split | BeforeMarket 57.6 % · AfterMarket 42.4 % |
| market cap available | 98.6 % |
| `split_after_t0` (withhold from prompt) | 42 events |
| events within 30 d of a membership boundary | 15 (10 on an unverified date) |

Events are dropped for: timing unknown (164), no trading day after the report
(10), fewer than 5 days after `t0` (8), no price series (6).

**The index-membership correction moves the base rate by 0.24 pp** (46.60 % →
46.36 %). That is the measured size of the sample-selection distortion, and the
boundary count bounds what any remaining date error could still do: at most 10
of 2,869 events could change inclusion.
