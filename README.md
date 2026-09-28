# Compute-matched evaluation of multi-agent LLM structures on post-earnings forecasting

Materials to reproduce an experiment that compares three multi-agent LLM
structures (repetition, aggregator, debate) against a compute-matched
single-agent baseline at forecasting the sign of the five-day post-earnings
abnormal return of S&P 500 companies: 840 events, two evidence levels, two
open-weight models.

## Layout

```
dataset/    the event dataset: build scripts, curated inputs, schema
            (dataset/README.md is the entry point; dataset/SCHEMA.md documents
            every table of the rebuilt database)
docs/       the decision register and the blind checkpoint report
harness/    the experiment harness (git submodule; a fork of
            ForesightFlow/coordination-experiment, MIT)
```

The run's outputs live where the scoring scripts expect them, under
`dataset/scaffold/pilot5/` (per-event model outcomes, token counts, scored
rows, reports) and `dataset/scaffold/RESULTS_2026-09-09/` (the four result
reports of the primary analysis, the timing sensitivity and the
exploratory block). Scripts 24 and 27 resolve that folder relative to the
dataset directory, so nothing has to be configured.

Clone with the harness in one step:

```
git clone --recurse-submodules https://github.com/EmilHerzberg/multiagent-earnings-forecast.git
```

The source archives attached to a release do not contain the submodule; use
the clone command above or fetch the harness fork separately.

The dataset is rebuilt from the provider APIs, not shipped: the database
contains licensed vendor data and stays out of the repository. Building it
needs an EODHD subscription (prices, earnings, master data) and an Alpha
Vantage premium key (income statements, news); see `dataset/README.md`
section 1 for the minimum plans.

## Reproduction, in order

1. `dataset/`: `python run_all.py` builds the database (section 2 of
   `dataset/README.md`), `scripts/25_select_event_sample.py` reproduces the
   840-event sample (seed 20260903, fixed in the script), and
   `scripts/20_build_evidence_packs.py` writes the evidence packs the agents
   read.
2. `harness/`: `pilot/real-run.sh <1|2>` runs one evidence level against
   OpenRouter; the run is resumable and cost-capped.
3. `dataset/scripts/24_score_experiment.py` joins the harness outputs to the
   truth in the database and answers the three research questions.

## Evidence packs are not shipped

The packs embed vendor data (EPS figures, prices, ratios, news summaries)
that the providers do not license for redistribution. Script 20 rebuilds
them deterministically from a rebuilt database. Caveat: providers revise
figures after the fact (restated EPS, corrected bars), so a later rebuild can
differ from the packs the run used in individual values. The exact prompt
templates are in `harness/pilot/prompts.ts`.

## Decision references

Design decisions are numbered D1 … D32 in the working records and resolve in
`docs/DECISIONS.md`.

## Known errata of the frozen inputs

The curated membership files in `dataset/` are the ones the run used and are
kept unchanged. A later check against a current constituent list found:

- Apollo Global Management (APO) joined the index on 2024-12-23 and is
  missing from the universe; its six in-window earnings events are absent
  from the event pool.
- Paramount Global (PARA) became Paramount Skydance (PSKY) on 2025-08-07 and
  stayed in the index; the dataset treats it as having left.
- Eastman Chemical (EMN) left the index on 2025-11-04; the change log has no
  row for it.

None of the three companies is in the 840-event sample.

## Large run artefacts

Not in git because of their size: the per-call token ledgers
(`L1/L2_ledger5.jsonl`, 198 and 177 MB) and the per-event transcripts
(`L1/L2_transcript*.md`, 29 to 79 MB). They are attached to the GitHub
release of this repository. The raw request logs (`L1/L2_runs_raw.jsonl`,
343 and 533 MB) contain every prompt including licensed vendor text and are
available on request.

## Licence

MIT, see `LICENSE`. It covers the scripts, configuration, documentation and
run outputs in this repository. It does not cover third-party data files
redistributed here for convenience, which remain under their publishers'
terms: `ai_gpr_daily.csv` (Caldara & Iacoviello, Federal Reserve Board),
`tpu_daily.csv` (Caldara et al.), `epu_daily.csv` (Baker, Bloom & Davis,
policyuncertainty.com), `vix_daily.csv` (Cboe Global Markets), the S&P Dow
Jones Indices constituent changes recorded in `sp500_index_changes*.csv`, and
the SPDR S&P 500 ETF holdings file `raw_spy_holdings_2026-09-21.xlsx` (State
Street). The `harness/` submodule carries its own MIT licence.
