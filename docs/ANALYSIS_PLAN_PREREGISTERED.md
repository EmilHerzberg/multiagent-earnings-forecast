# Pre-registered analysis plan

_Frozen on 2026-09-05 in the working repository, before the full run, and amended the same day under D31 (both amendments are marked in place). This copy differs from the frozen text only in how the surrounding documents are named; no evaluation choice was changed. Fixed before the full run, as the study design's milestone requires: "the analysis
plan is written down and fixed before the full runs, so that the evaluation choices
cannot be adjusted afterwards to fit the results." The git commit that introduces
this file is its timestamp. From that commit until submission, every evaluation
choice in this document is binding; §12 says what happens if one has to change._

Precedence on conflict: the study design (the document fixed before data
collection) governs this plan, this plan governs the analysis. The design
notes (the pre-run working record, not included in this repository) hold the
rationale behind each pin and are cited throughout as "notes §n"; decisions
D1–D29 are summarised in `DECISIONS.md`. The three choices this
plan itself settles (§5.2, §5.3, and the inference stance carried by §2's
"inference scope" row and §4's reading rules) were made by the author on
2026-09-05 and are logged as **D30**. Post-freeze amendments are logged as
**D31** and marked in place where they apply.

---

## 1. The research questions, verbatim from the study design

- **RQ1.** "When both are given the same total amount of computation, does
  structured communication between agents improve prediction quality over
  communication-free repetition of the same model?"
- **RQ2.** "How much additional computation does communication cost in each
  structure?"
- **RQ3 (secondary).** "Do the results change when the amount of input
  information grows, and do they differ between reasoning models and
  non-reasoning models?"

The second half of RQ3 — the model contrast — is answered **descriptively
only**. The two roster models were deliberately chosen maximally dissimilar
(family, scale, architecture, serving precision), which makes the model axis a
robustness probe for the within-model findings rather than a controlled
reasoning contrast. This reallocation is documented in notes §8.5 and no
conclusion of the study rests on the cross-model comparison.

## 2. What is already fixed, and where

This plan does not restate the design; it points at each pin once and then
builds only the evaluation on top.

| pinned | value | where |
|---|---|---|
| task and outcome | P(abnormal return over 5 trading days > 0); AR = stock − S&P 500, beta = 1; y = 1 iff AR > 0, exact zero counts as 0 | notes §2, D8 |
| ladder | repetition (N = 3, no communication), aggregator (blind combiner), debate (2 revision rounds, full-reasoning forwarding) | notes §3, D1, D11, D12 |
| generation parameters | temperature 0.7, max 8,000 tokens/call, mean as the combiner rule, retry minimum one | D14, B8, D19, D16 |
| prompts | frozen 2026-08-26 (D1–D13), amended twice after the freeze, each logged: D21 (2026-08-27, peer headers to prose) and D27 (2026-08-29, probability kept out of the reasoning text); no wording change after this plan | `pilot/prompts.ts` |
| compute matching | unit = total billed tokens (prompt + completion); baseline = the prefix of the repetition pool, in generation order, that fits the structure's budget; never re-sorted | notes §4, D23, D25 |
| token accounting | the ledger's categories (historically "five buckets"): read side D/A/B/B_SELF, write side C1/C2. B_SELF joined the original five later (D12) and is deliberately NOT part of the headline communication cost — it is reported as its own additional metric (D30). Provider counts are the totals, the local tokenizer only splits; identities enforced per call | notes §5, D12, D18, D30 |
| sample | 840 events = 210 symbols × first four eligible quarters, seed 20260903 over the sorted 445-symbol pool, pinned byte-for-byte in `event_sample.txt`; base rate 48.69 % (population 46.50 %), disclosed and decomposed. **Study window (D31, 2026-09-06): ends 2026-05-30.** Costless by construction — the pinned sample's latest t0 is 2026-05-21 and its latest outcome window closes 2026-05-29, nine trading days before the data horizon (2026-06-11); zero events are affected under either reading, verified from date columns only, labels untouched | D28, D31 |
| eligibility | `t0 > 2025-04-28` (release of qwen3-32b, the newest roster model) and `in_index_at_t0 = 1` | D9's frozen-artifact argument applied to the final roster (D20/D24 — D9's own text still names 2025-05-28, from the roster that included r1); D28; `25_select_event_sample.py` |
| models and serving | qwen3-32b (reasoning on) split across DeepInfra + SiliconFlow, both fp8, events pinned per provider by hash; deepseek-chat-v3-0324 on SiliconFlow; `allow_fallbacks:false` everywhere | D20, D24, D29 |
| forecasts | canonicalised to 9 decimals before any binning or comparison (derived requirement, not a style choice) | `21_metrics.py`, edge tests §6c |
| decomposition | Murphy with **10 bins, fixed** (even count so 0.50 is a bin edge); small positive resolution treated as possibly noise | notes §6.5 |
| inference scope | **point estimates; no committed standard errors, confidence intervals or significance tests** (author re-affirmed 2026-09-05, D30) | notes §11 |

## 3. The data the run will produce, and the blinding rule

The run covers all 840 events × 2 models × 2 evidence levels. Per (event,
model, level) the harness books one ladder: the three structure outcomes, the
repetition pool in generation order (large enough to cover the most expensive
structure's budget), the compute-matched draws, and the five-bucket ledger of
every call. Interrupted work resumes; an event that keeps failing on
infrastructure is retried on later resumes and, if still unbooked when the run
is closed, is counted as attrition in §7 — it is never scored partially.

**Blinding.** No file an open run writes joins a forecast to its outcome, and
none contains a score. The labels live in `thesis.db` and — as a copy the pack
builder writes — in the evidence-pack JSON; no run-time artefact reads either.
(The run transcripts printed the ground truth above each event's forecasts
until 2026-09-05; that block is now opt-in via an explicit `--reveal-truth`
flag, for use only after the run is closed. Amended under D31 after the
adversarial review, `REVIEW_ANALYSIS_PLAN_2026-09-05.md` L2-01.) During the
run, the operator sees per-event forecasts in status lines — they are needed
for fault monitoring — but no outcome and no score; the operator of course
knows the market and could guess outcomes, which no rule can prevent and this
plan does not claim to. The first score exists when `24_score_experiment.py`
runs after **both levels of the full run are complete**. There are no interim
scoring passes, and nothing about the run (its extent, its settings, which
events are retried) is decided from any outcome. The cost cap is an
operational stop, blind to results; if it ever fires, the analysis uses
whatever booked, and §12 records the event.

**Operational checkpoint (D32, 2026-09-06 — declared before the first paid
call).** The run pauses after its first 50 events in the seed-shuffled
processing order (both models, both levels; `PILOT5_EVENT_LIMIT=50`; the set
pinned in `checkpoint_events_D32.csv`) for a blind operational check by
`27_checkpoint_blind.py`: booking coverage, outages and retries, malformed and
drop counts, file integrity, cost and time per ladder, projection to 840. The
script reads no label, prints no forecast and computes no score; the scorer,
transcripts and report are not opened. Go/no-go criteria are infrastructure
criteria only; drop rates are reported, never gated. A GO resumes the same run
with the same command — the 50 events are part of the run, not a pilot, and
are never re-run or replaced.

**Run-configuration pins (D31/IV).** The real run uses **`PILOT5_REPEATS = 1`**
— the harness can repeat the whole event set with `#rN`-suffixed ids, and that
knob staying at its default is now a pinned choice, not an accident; any other
value would be a logged amendment BEFORE the start. And the **software is
pinned by record**: the commits of the harness and of the scorer are written
into the run's status log at session start and into `results.md` at scoring
time (the token summaries already carry the harness commit in their
provenance header); a code change to either between run start and scoring is a
logged deviation under §12. (The recording lines themselves are Block-III
code.)

## 4. RQ1 — the primary analysis

**Primary endpoint.** The mean **paired** Brier difference, structure minus
compute-matched baseline, per cell. Cells: 2 communicating structures
(aggregator, debate) × 2 models × 2 levels = **8 primary cells**. Negative
means the communicating structure was better at the same billed token budget.
A pair exists for an event only if both sides produced a usable number; the
pairing is per event, and the scorer asserts pairing integrity. **"Usable
number", defined (D31/IV):** a structure produced a usable number for an event
iff it is not marked dropped AND a probability was parsed from its output; a
matched baseline produced one iff at least one run fit its budget
(`runsUsed ≥ 1`) AND its probability is present. Anything else is excluded
and counted under its reason in §7 — the term appears in the pair rule here
and in RQ3's common-set rule (§6.1) and means the same thing in both.

**Secondary, within RQ1, all pre-specified:**
- the win share (fraction of events with a negative paired difference) per
  cell. **Tie rule, stated explicitly (D31; the author's deliberate choice):**
  an event counts as a win only if the structure is STRICTLY better than the
  matched baseline; an exact tie — the structure forecasting the same number
  as the free baseline — counts against the structure, because coordination
  that only matches what repetition already gives for the same budget has
  gained nothing. Ties are not rare (4.9 % of pairs in the dry run, from
  agents and averaged baseline landing on the same round number), so this is
  a real choice, made before the run and leaning against coordination, not a
  rounding accident;
- the Murphy decomposition (10 bins) of both sides of every cell, with the
  identity asserted before it is reported;
- the unmatched rung scores as context, explicitly marked not comparable to
  each other;
- the two reference baselines on the same events: the historical base rate and
  the logistic regression on the standardised earnings surprise. Their
  construction is pinned here, because the only existing implementation was a
  demonstration, not the committed baseline (D31; review L2-02): **both are
  fitted on the 685 events with `t0 <= 2025-04-28`** — genuinely before the
  experiment window, so no test event is ever in the training data (notes §7.2's
  rule) — **and predict every sample event**; they are computed by the scorer
  at scoring time, so no fresh label contact happens before the run. (The
  earlier demonstration split the eligible window itself 60/40 in time order;
  its numbers appear in §10 as historical disclosure only and bind nothing.)

**Negative control — a tare check on the matching machinery (specified under
D31; author-approved with the rationale recorded here).** Why it exists: every
RQ1 comparison runs through one mechanism — the compute-matching engine takes
a token budget and draws, from the repetition pool in generation order, the
prefix of runs that fits it. If that engine miscounts (a token-accounting slip,
a prefix-boundary error, a billed-vs-usable mix-up), every matched baseline is
quietly wrong and RQ1 is poisoned with no visible symptom. The control is the
same move as putting the empty container on a scale before weighing: feed the
engine a budget whose correct answer is KNOWN — the repetition rung's own
billed cost (`billedTotal`) — and it must hand back (essentially) the
repetition rung itself, a paired difference of zero. On events without retried
calls the result must be exactly zero, which is not a triviality but a wiring
check: the zero is produced by the full budget-and-prefix path, not assumed.
On events with retries the billed and usable totals part ways, and the control
exercises exactly that seam. It costs nothing: no API call, pure post-hoc
arithmetic on data the run records anyway. It is **reported, not gated** —
mean and spread per model and level, no pass/fail verdict and no authority
over any result; a conspicuous non-zero is investigated and written up, not
adjudicated by a pre-set threshold that would only fake objectivity. It is not
in the study design and answers no research question; it is quality assurance for
the instrument the research questions stand on.

**Reading rules — fixed now because there is no test to hide behind.**
Point estimates only (D30). The pre-registered language:

1. **Direction before size.** A difference is described as *pointing* somewhere
   only if its sign agrees across **both models** for the same structure and
   level. The model axis is the robustness probe (notes §8.5); a
   sign that flips between models is reported as "not consistent across
   models" and carries no directional claim.
2. **The effective sample is weeks, not events.** Earnings cluster in calendar
   weeks and share market conditions; the sample spans 52 distinct ISO weeks —
   measured on t0, the reaction day the clustering argument is actually about
   (and, as it happens, also 52 on the report date; both counted 2026-09-05).
   Differences are therefore reported as point estimates and are never, on
   their own, called *real*. **Deliberately, no numeric threshold is
   pre-registered** (D31): the study design mandates that evaluation choices be
   fixed before the runs, not that a significance-like cutoff exist, and the
   study's inference scope is point estimation (D30). A magnitude is
   described in words, relative to what week-level clustering can produce,
   and the strongest language a difference can earn without interval evidence
   is descriptive. The calendar-week block bootstrap (§8.4) remains the
   post-hoc instrument for intervals; if it is computed, its result is
   reported whether or not it supports the main finding, and only a
   difference whose interval excludes zero may be called *robust*.
   (D31 record: the freeze-time version of this rule anchored "small" to the
   base-rate-versus-logistic gap. The adversarial review showed that anchor
   was ambiguous — four defensible readings spanning a factor of seven — and
   the wrong kind of quantity for a paired difference, and it promised more
   than the study design requires. Removed before any run data existed.)
3. **Thin cells.** Twenty events is a reading aid, not a criterion (the
   scorer's own wording): nothing is filtered by it, but any cell resting on
   fewer than 20 paired events is flagged and its decomposition is not
   interpreted.
4. **No re-binning, no re-grouping, no post-hoc subsets** beyond the
   sensitivity checks pre-registered in §8.

**Reporting precision (D31/IV).** Brier scores, paired differences and Murphy
components are quoted to **4 decimal places** in the write-up's text and tables;
token counts as integers; shares as one decimal of a percent. Display
precision is a freedom too — a difference that only exists in the fifth
decimal must not become visible by choosing five decimals for one table and
three for another. (Computation stays at full precision; forecasts stay
canonicalised to 9 decimals per §2 — this pin governs display only.)

## 5. RQ2 — the cost of communication

Everything in RQ2 is descriptive and read verbatim from the token ledger; no
inference is attached.

**5.1 Reported per cell (structure × model × level), in absolute tokens
(notes §5.3 — never as shares):** the five-bucket breakdown, and per event the
mean and total of the ledger's fields — `commCost`, `billedCommCost`,
`commCostInclSelf`, `billedCommCostInclSelf`, `B_SELF`, `total`,
`billedTotal`.

**5.2 The headline figure (D30, settling what D12 deferred).** The headline
communication cost is **A + B + the full cost of combiner-only agents,
excluding B_SELF** — on any run containing retried calls, the **billed**
variant (`billedCommCost`), because discarded attempts were paid for.
Rationale: notes §5.2 defines communication as tokens that exist because agents
communicate *with each other*, and D12's own reasoning records that re-reading
one's own earlier answer is memory emulation, not inter-agent communication;
this also keeps the figure comparable with Nechepurenko & Shuvalov.
**B_SELF is additionally reported as its own named metric** (the author's
explicit addition), so the memory-emulation cost of the debate is a visible
number, not a footnote inside a variant. The inclusive figure is printed in
the same table, and any statement that changes truth value between the
exclusive and inclusive figure is flagged as fragile rather than resolved by
choosing the friendlier column.

**5.3 The verdict rule for the title question (D30).** "Earns its tokens" is
answered by **putting RQ1 and RQ2 side by side**: a structure earns its tokens
if it beats the communication-free baseline *at the same billed budget* (RQ1),
and RQ2 states what the communication share of that budget was. **No
efficiency ratio** (Brier per token) is computed: the expected Brier
differences are near zero, and a ratio with a near-zero numerator manufactures
precision the data does not contain.

## 6. RQ3 — input levels, and the model contrast

**6.1 The level contrast** is computed on the **common event set** only
(D17): an event enters for an arm and model only if both levels produced a
usable number there. Endpoint: Brier(L2) − Brier(L1) per (model, arm), with
the counts of L1-only and L2-only events reported alongside.

**6.2 The equal-runs control is co-primary (D23 addendum).** A level-2 prompt
is longer, so the same budget buys fewer repetition runs at level 2 — the
baseline is mechanically weaker there for arithmetic reasons. The control
rebuilds both baselines from the first k runs of their own pools, k = the
smaller of the two run counts (in practice level 2's), so the averaged
ensemble is the same size on both sides — level 1 never averages more runs
than level 2 — and only the context differs.

**The gate, mechanically (D31 — the freeze-time "survives the control" had no
executable form; review L3-03).** For a structure S and a model M, the
controlled level effect is

    Δ_S  =  [Brier_S(L2) − Brier_S(L1)]  −  [Brier_ctrl(L2) − Brier_ctrl(L1)]

where ctrl is the equal-runs baseline above, all four terms on the common
event set (D17). A level statement about S is made only if the sign of Δ_S
agrees across both models. The raw (uncontrolled) deltas are reported beside
it; where raw and controlled disagree, the controlled figure governs and the
disagreement is stated.

**The baseline reference curve** (pool quality at every prefix length, per
model and level) is a **committed** scorer output (D31): the freeze-time plan
listed the same object both here as control context and in §9 as exploratory,
a contradiction the review flagged (L3-04); resolved in favour of committed,
since the curve is what makes this control checkable. Reading the curve as
"averaging more runs helps on its own" remains a descriptive observation and
is reported whenever it is made.

**6.3 The model contrast** (RQ3's second half) is reported descriptively per
§1: same tables, both models, no inferential language, and the notes §8.5 framing
stated where it appears.

## 7. Attrition and exclusions — all counted, none silent

- **Failure taxonomy (D16):** truncated / format / infrastructure, logged per
  call. What a malformed-after-retry call costs depends on WHOSE call it was,
  because the three round-0 workers are shared by all three rungs (that
  sharing is the design, D16): a failed **worker** drops the whole event for
  that model and level; a failed **aggregator** drops only the aggregator
  rung; a failed **debate** call drops only the debate rung. Every drop is
  counted under its reason. (This sentence originally claimed a drop never
  touches the event's other structures — wrong for the dominant worker case
  and corrected under D31; review finding L2-08.)
- **A pair** needs both sides; **an RQ3 event** needs both levels (D17);
  a matched baseline with zero affordable runs is excluded and counted.
- **Cross-level attrition, stated explicitly (D31/IV):** an event that books
  at one level but permanently fails at the other stays FULLY counted in the
  booked level's RQ1 cells — RQ1 is per level and loses nothing — and is
  missing only from RQ3's common set (D17). One level's failure never
  disqualifies the other level's data.
- **The pinned sample outranks nothing — it stops the machine (D31/IV):** the
  pack builder hard-aborts if any of the 840 pinned events fails its
  eligibility re-check at build time (conceivable after a data correction).
  If that ever fires, the abort stands: the event is NEVER silently replaced
  or skipped; the cause is investigated, the outcome is logged as an
  amendment, and only then does anything run. A sample that can quietly
  reshape itself is not pinned.
- The results document **opens with the sample-flow table**: 2,953 events,
  minus leakage-ineligible, minus not-in-index, to 2,172 eligible; the 840
  drawn; then every run-time loss with its reason, down to the n of each cell.
- The 840 were drawn with deliberate slack above the study design's 800 maximum
  because D17 makes format failures cost events on both sides (D28).
- **No result-dependent stopping.** The run ends when all 840 events are
  booked at both levels or when the author closes it for operational reasons —
  meaning things like the cost cap or persistent infrastructure failure — with
  the reason written down BEFORE the stop; never because of anything a score
  shows, since no score exists until the run is closed (§3). The D32 checkpoint
  pause after the first 50 events is such an operational stop, its reason and
  its inspection list written down before the start (§3, D32). **Task order
  (D31/II-8):** the runner works through the (event × model) tasks in a
  seed-shuffled order (fixed seed in the code, reproducible across resumes),
  so an early close leaves an approximately random subset of events rather
  than an alphabetical prefix. The shuffle changes only the processing order:
  WHICH events run is fixed by the pinned sample (its own seed, D28), and the
  within-event generation order that compute matching consumes is untouched.

## 8. Pre-registered robustness and sensitivity checks

These are secondary, computed once, and reported however they come out.

1. **Provider invariance (D29).** qwen's events are split ~50/50 across two
   fp8 endpoints by hash. The RQ1 primary endpoints are recomputed per
   provider half. Expectation: agreement within noise; a systematic gap would
   indicate serving-stack sensitivity and would be reported as a limitation.
   Descriptive only, no headline weight.
2. **Announcement-timing conflicts.** For ~3.2 % of AfterMarket events the
   larger move fell on the report date (notes §8.1's rule: report-date move more
   than twice the t0 move and above three per cent). The RQ1 primary endpoints
   are recomputed excluding the sample's such events; the count is stated.
3. **Distinct-value grouping — demoted to an optional instrument (D31).** The
   freeze-time plan committed to recomputing the decomposition grouped by
   distinct forecast values. The author moved it to the same footing as the
   bootstrap in item 4: NOT computed as part of the committed analysis,
   available post-hoc, to be looked at (if at all) only after all runs are
   collected, and — like every optional instrument — reported however it
   comes out if it is computed. Recorded for that future reading: the check's
   premise ("the models answer on a coarse grid") is false for the averaged
   arms this plan decomposes — averaging three runs produces many
   near-unique values, and on real dry-run data one-event groups inflated
   apparent resolution four- to seven-fold (review L3-10). Any computed
   result is read with that caveat attached.
4. **The optional instruments stay optional.** Ferro–Fricker, CORP and the
   calendar-week block bootstrap (`22_robustness_optional.py`) remain
   available post-hoc instruments, not committed analysis (notes §6.5, §11,
   re-affirmed in D30). The undertaking is restated verbatim: **if any of them
   is computed, its result is reported whether or not it supports the main
   finding.**

**Disagreement rule (D31/II-7).** If a committed sensitivity check (items 1–2)
**flips the sign** of a primary cell's paired difference, that cell's finding
carries the label **"not robust"** in the write-up: the primary result is not
replaced, but it may never again be stated without the label. Size changes
without a sign flip are context, not a label. The trigger is deliberately the
sign and nothing else — any magnitude threshold would reintroduce through the
back door the numeric cutoff that D31/II-1 removed. A computed optional
instrument that contradicts a primary result is covered by the
report-regardless undertaking above; the label applies to the committed
checks.

## 9. Exploratory analyses — labelled, fenced, no headline

Everything below is labelled EXPLORATORY wherever it appears, is not in the
study design, and generates no confirmatory language:

- ~~the ensemble curve~~ (moved under D31: the curve is now the committed
  reference curve of §6.2, not an exploratory extra — the freeze-time plan
  listed it in both places; the exploratory READING of it, "averaging more
  runs helps on its own", stays here and is reported whenever made);
- the **leak rate** (`reasonStatesOwnNumber`): how often a worker states its
  probability inside its reasoning despite D27;
- **debate dynamics**: round-to-round spread and convergence per model (the
  pilot's 82 % qwen collapse is a development observation, not a result);
- any sector or size slices, if examined at all.

## 10. Disclosure of prior data contact

Written down so no one has to ask:

- **Development events.** All prompt engineering, the pilot ($0.03), the
  edge-case audit and the calibration probes used `MSFT_2025-10-29` — the
  single development pack. The end-to-end dry run of 2026-09-04 used ten
  further stocks (BG, CMS, DG, DVN, GNRC, JBL, JPM, SYK, VRTX, WAT), where
  the full scoring chain ran and its (meaningless, n = 41) numbers were seen.
  **None of these eleven symbols has any event in the 840-event sample —
  verified by direct lookup, zero overlap.** No model forecast exists for any
  sample event at the time this plan is frozen.
- **Aggregate label statistics of the sample were seen** before this plan:
  D28 computed and disclosed the sample's base rate (48.69 %), its
  decomposition, and the alternatives' base rates — that disclosure is why
  the selection rule was kept rather than switched. No per-event label has
  been paired with any forecast.
- **Baseline performance was seen once during development** (base rate 0.2487,
  logistic 0.2496). Precisely labelled (D31): those numbers came from a
  demonstration pass — a 60/40 in-time split WITHIN the eligible window, run
  on the database as it stood BEFORE the 2026-09-03 membership fix — so they
  neither reproduce against the frozen database nor use the committed
  construction in §4, and they bind nothing. They are disclosed as what was
  seen, nothing more. Both baselines are deterministic given the data, so the
  sighting cannot tune them toward the sample.
- The pilot's own numbers were invalidated by the edge audit (24 defects,
  `EDGE_AUDIT_2026-08-29.md`) and nothing quantitative from before 2026-08-30
  carries into the write-up.

## 11. What the write-up will and will not claim

The design measures directional skill at matched compute under honest token
accounting. It supports statements of the form "at equal billed budget,
structure X scored (better/worse/indistinguishably) than repetition for model
M at level L, and its communication cost was T tokens per event". It does not
support — and the write-up will not contain — significance claims, cross-model
causal statements about reasoning, or efficiency ratios. If every cell lands
within noise, that **is** the result: the compute-matched null was the finding
the literature review said is missing for a realistic financial task, and the
uncertainty framing of notes §2.4 anticipated exactly this outcome.

## 12. Deviations

Any deviation from this plan after its freeze commit is logged in
the decision register with its date, what changed, and why — before
or upon occurrence, not retroactively — and every deviation is listed in the
write-up itself. A deviation that touches a primary endpoint (§4) or a reading
rule additionally requires the author's explicit sign-off recorded in the log
entry. Silent drift is the failure mode this document exists to prevent.
