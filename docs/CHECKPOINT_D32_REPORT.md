# Blind operational check — D32 checkpoint

_generated 2026-09-06 14:56 UTC. Reads only `L*_run_status.txt`, `L*_outcomes.jsonl`, `L*_ledger5.jsonl`, `L*_runs_raw.jsonl`. Contains no forecast, no ground truth and no score (plan §3)._


## Level 1

### Sessions

| # | started (UTC) | cap $ | concurrency | repeats | evidence | harness |
|---|---|---:|---:|---:|---|---|
| 1 | 2026-09-06T12:41:01.665Z | 8.00 | 6 | 1 | REAL | 2e2edb9 |
| 2 | 2026-09-06T13:04:36.771Z | 8.00 | 6 | 1 | REAL | 9138b31 |

**harness commit changed between sessions: ['2e2edb9', '9138b31'] — a logged deviation is required (plan §3/IV-2)**

event limit: **50 events** -> 100 ladder runs planned (checkpoint)

### Coverage

| model | booked ladders | by provider |
|---|---:|---|
| deepseek-chat-v3-0324 | 50 | SiliconFlow 50 |
| qwen3-32b@on | 50 | DeepInfra 23, SiliconFlow 27 |

events seen: 50; events booked on ALL models: 50; booked ladders in total: 100

every planned cell has an outcomes line.

### Failures and retries

outage FAIL lines: none

HTTP calls 2170; failed after the client's own retries 0; recovered after retries 41; by model x provider: deepseek-chat-v3-0324@SiliconFlow x20, qwen3-32b@on@DeepInfra x5, qwen3-32b@on@SiliconFlow x16; provider fallbacks (returned != pinned) 0

malformed attempts (ledger, D16): none; retried calls (attempt > 1): deepseek-chat-v3-0324 0, qwen3-32b@on 0

dropped rungs (a malformed answer after its retry): none; whole-event drops (a worker failed): deepseek-chat-v3-0324 0, qwen3-32b@on 0; matched comparisons with runsUsed = 0: 0

### Integrity

identity violations: 0 | duplicate outcomes lines: 0 | orphan ledger keys: 0 | cost-cap stops: 0 | other `!!` lines: 0

### Cost and time per booked ladder

| model | ladders | calls/ladder | tokens/ladder | $ mean | $ median | $ max | s median | s max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| deepseek-chat-v3-0324 | 50 | 25.0 | 34142 | 0.0166 | 0.0166 | 0.0183 | 251 | 410 |
| qwen3-32b@on | 50 | 18.4 | 56324 | 0.0194 | 0.0214 | 0.0487 | 420 | 1178 |

ledger total $1.7982; wall clock 1.84 h; throughput 54.5 ladders/h at this level

## Level 2

### Sessions

| # | started (UTC) | cap $ | concurrency | repeats | evidence | harness |
|---|---|---:|---:|---:|---|---|
| 1 | 2026-09-06T12:43:51.307Z | 60.00 | 4 | 1 | REAL | 2e2edb9 |
| 2 | 2026-09-06T13:04:47.616Z | 8.00 | 6 | 1 | REAL | 9138b31 |

**harness commit changed between sessions: ['2e2edb9', '9138b31'] — a logged deviation is required (plan §3/IV-2)**

event limit: **50 events** -> 100 ladder runs planned (checkpoint)

### Coverage

| model | booked ladders | by provider |
|---|---:|---|
| deepseek-chat-v3-0324 | 50 | SiliconFlow 50 |
| qwen3-32b@on | 50 | DeepInfra 23, SiliconFlow 27 |

events seen: 50; events booked on ALL models: 50; booked ladders in total: 100

every planned cell has an outcomes line.

### Failures and retries

outage FAIL lines: none

HTTP calls 1905; failed after the client's own retries 0; recovered after retries 53; by model x provider: deepseek-chat-v3-0324@SiliconFlow x31, qwen3-32b@on@DeepInfra x3, qwen3-32b@on@SiliconFlow x19; provider fallbacks (returned != pinned) 0

malformed attempts (ledger, D16): none; retried calls (attempt > 1): deepseek-chat-v3-0324 0, qwen3-32b@on 0

dropped rungs (a malformed answer after its retry): none; whole-event drops (a worker failed): deepseek-chat-v3-0324 0, qwen3-32b@on 0; matched comparisons with runsUsed = 0: 0

### Integrity

identity violations: 0 | duplicate outcomes lines: 0 | orphan ledger keys: 0 | cost-cap stops: 0 | other `!!` lines: 0

### Cost and time per booked ladder

| model | ladders | calls/ladder | tokens/ladder | $ mean | $ median | $ max | s median | s max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| deepseek-chat-v3-0324 | 50 | 20.1 | 64924 | 0.0238 | 0.0241 | 0.0285 | 252 | 384 |
| qwen3-32b@on | 50 | 18.0 | 82829 | 0.0200 | 0.0236 | 0.0331 | 338 | 607 |

ledger total $2.1865; wall clock 1.43 h; throughput 70.1 ladders/h at this level

## Projection to 840 events x 2 models per level

| level | remaining ladders | projected remaining $ | projected hours (this level alone) |
|---|---|---:|---:|
| L1 | deepseek-chat-v3-0324 790, qwen3-32b@on 790 | 28.41 | 29.0 |
| L2 | deepseek-chat-v3-0324 790, qwen3-32b@on 790 | 34.55 | 22.5 |

spent so far (ledger, all levels) $3.98; projected remaining 62.96; projected TOTAL 66.94; wall clock if the levels run concurrently 29.0 h, sequentially 51.5 h. Rates are per-level means of the booked ladders and carry the checkpoint's own noise.

## Verdict — the D32 criteria (infrastructure only; nothing here is a result)

- C1 every planned ladder booked: PASS
- C2 ledger identities: PASS (0 violation(s))
- C3 provider pinning, no fallback (D29): PASS (0 call(s) served by a provider other than the pinned one)
- C4 no cost-cap stop: PASS (0)
- C5 files consistent (no duplicate outcomes, no orphan ledger records): PASS (0)
- C6 projected total within budget $120: PASS (projected $66.94)
- R1 malformed attempts 0, dropped rungs 0: REPORTED, not gated — a D16 finding that the plan counts as attrition (§7)
- R2 truncated answers (finish_reason = length) 0: REPORTED — the token cap is the harness's stated responsibility; raising it would be a logged deviation (§12)
