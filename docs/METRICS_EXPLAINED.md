# The measurement ideas, explained from scratch

_Written 2026-08-28; the appendix revised 2026-08-30, after an audit of the optional instruments found and fixed seven bugs in them. Everything here is implemented in `dataset/scripts/21_metrics.py` and specified in the design notes §6–§7 (the pre-run working record, not included in this repository). Each section builds one idea, then says where it sits in your experiment. The last section links them together._

**Scope, decided 2026-08-29.** The study uses four measures, which are exactly what the study design commits to:

> **Brier score** → **Murphy decomposition** (reliability, resolution, uncertainty) → **baselines** (base rate, logistic regression) → **the compute-matched repetition rung**

Three further instruments — the Ferro‑Fricker bias correction, the bin-free CORP decomposition, and the calendar-week block bootstrap — are **available but not committed to**. They live in `scripts/22_robustness_optional.py`, which nothing in the committed analysis depends on, and they are explained in the **appendix** below.

They need no new data and no re-run, being functions of probabilities and outcomes the experiment will already have produced. So the decision whether to use them can wait until the results exist and the remaining time and page budget are known. The methodology chapter promises none of them.

**One rule makes this legitimate.** Deciding *whether to run* a check is free. Deciding whether to *report* it is not. If a check is computed, its result goes into the write-up whether it supports the main finding or undermines it. Running several analyses and presenting the most flattering one is the garden of forking paths; running a pre-registered analysis and adding a clearly-labelled robustness check is ordinary good practice. The only thing separating them is whether the inconvenient result also gets written down.

The measures that remain are one chain of reasoning, each answering a question the previous one raises:

> How good is a forecast? → **Brier**
> But *why* is it good or bad? → **Murphy**
> But "good" compared to *what*? → **Baselines**
> And does *communication* help, rather than just more computation? → **the repetition rung**

---

## 1. The Brier score — grading a probability

### The problem

Your models don't answer "yes" or "no". They answer "0.62". How do you grade that? You can't mark it right or wrong: the stock either rose or it didn't, but "0.62" was neither.

### The idea

Take the probability you said, take what actually happened (1 if it happened, 0 if not), find the gap, and square it. Then average over all your forecasts.

> **Brier = average of (forecast − outcome)²**

Squaring does two things: it removes the sign (being 0.3 too high is as bad as 0.3 too low), and it punishes big errors much harder than small ones.

**Lower is better.** 0 is perfect, 1 is maximally wrong.

### Visualising it

One forecast, four possible situations:

| you said | what happened | gap | **penalty** | comment |
|---|---|---|---|---|
| 0.70 | it happened (1) | 0.30 | **0.09** | good call, small penalty |
| 0.70 | it did not (0) | 0.70 | **0.49** | confident and wrong — expensive |
| 0.50 | it happened | 0.50 | **0.25** | said nothing, paid the flat fee |
| 0.99 | it did not | 0.99 | **0.98** | near-certain and wrong — brutal |

**The number to memorise: 0.25.** A forecaster who always says 0.50 scores exactly 0.25, no matter what happens. That's the "I know nothing" line. Any system worth having must beat it.

### The property that makes it fair: it is "proper"

A scoring rule is **proper** when your best strategy is to say exactly what you believe. Under the Brier score, if you truly think 0.62, then saying 0.62 gives you the best expected score. Saying 0.80 to look confident makes it worse. Saying 0.50 to play safe also makes it worse.

This matters enormously for you, and it's a likely exam question:

> **"Couldn't a model game your metric by always answering 50 %?"**
> No. That guarantees 0.25. Any model with real information beats that by stating its true belief. The rule cannot be gamed — that is what "proper" means.

### In your experiment

This is the primary outcome. Every rung of your ladder — repetition, aggregator, debate — produces a probability per event, and all of them are graded with the same Brier score, which is what makes them comparable at all.

It is also why the study design insists on **probabilities rather than trading returns**: returns depend on position sizing, costs and luck, whereas a Brier score grades the forecast itself.

**Your real number so far:** the base rate scores **0.2487**. That is your reference point — and note how close it is to 0.25, which already tells you this task is hard.

---

## 2. The Murphy decomposition — *why* was the score what it was?

### The problem

Two systems both score 0.24. One is a careful forecaster with genuine insight but slight overconfidence. The other is a coward that always says "about 47 %" and is technically well-calibrated. Same score, completely different systems. One number hides this.

### The idea

Murphy (1973) proved the Brier score splits into exactly three parts:

> **Brier = Reliability − Resolution + Uncertainty**

**Reliability** — *are your numbers honest?* When you say 70 %, does it happen about 70 % of the time? **Lower is better** (0 = perfectly calibrated).

**Resolution** — *do you actually distinguish cases?* Do the events you call likely really happen more often than the ones you call unlikely? **Higher is better** — note it is *subtracted*, so more resolution lowers (improves) your Brier score.

**Uncertainty** — *how hard is the task?* Fixed by the data. Nothing you do changes it. It's the score of always predicting the base rate.

### Visualising it

Suppose you made 300 forecasts. Group them by what you said:

| you said (bin) | how many | your average | how often it actually happened | reliability gap | resolution gap |
|---|---:|---:|---:|---|---|
| around 0.2 | 100 | 0.20 | **0.15** | 0.05 → small ✓ | 0.15 vs base 0.47 → **big** ✓ |
| around 0.5 | 100 | 0.50 | 0.48 | 0.02 → tiny ✓ | 0.48 vs 0.47 → ~0 |
| around 0.8 | 100 | 0.80 | **0.78** | 0.02 → tiny ✓ | 0.78 vs 0.47 → **big** ✓ |

This is an excellent forecaster. **Reliability**: what it said matches what happened in every bin. **Resolution**: it sorted events into genuinely different groups — 15 %, 48 %, 78 % actual rates. It didn't just say "47 % to everything".

Now a coward:

| you said | how many | your average | actually happened | reliability | resolution |
|---|---:|---:|---:|---|---|
| around 0.47 | 300 | 0.47 | 0.47 | 0 → perfect ✓ | 0.47 vs 0.47 → **zero** ✗ |

**Perfectly calibrated, and completely useless.** It never separated anything. That's exactly the case resolution exists to expose — and why reliability alone would flatter it.

### The reliability diagram

Plot "what you said" against "what happened". Perfect calibration lies on the diagonal.

```
what actually happened
  1.0 |                                    ,'
      |                                  ,'      ,' = perfect calibration
  0.8 |                            ●   ,'
      |                              ,'         ● = one bin of your forecasts
  0.6 |                          ,'
      |                        ,'   ○            ○ = overconfident here:
  0.4 |                  ●   ,'                      you said 0.6, only 0.45 happened
      |                    ,'
  0.2 |          ●     ,'
      |             ,'
  0.0 |___________,'________________________
      0.0   0.2   0.4   0.6   0.8   1.0
                what you said
```

Points **above** the line: you were too pessimistic. **Below**: too confident. **Spread along** the line: good resolution. **All bunched in the middle**: no resolution.

### In your experiment

This is the heart of your analysis, and it is what makes the study interesting rather than a single league table. The study design's expectation is that communication buys little. The decomposition lets you say *how* it fails — or succeeds:

- If debate **improves reliability** but not resolution → agents talk each other into better-calibrated numbers without learning anything new.
- If debate **improves resolution** → communication genuinely surfaced information a lone analyst missed. That would be the strong positive result.
- If debate **destroys resolution** → the sycophantic collapse you already observed (spread 0.093 → 0.017): agents converge, stop distinguishing events, and resolution falls.

That last one is why the D14 spread diagnostic and the resolution term are two views of the same phenomenon.

---

## 3. The baselines — "better" than what?

A Brier of 0.2487 is meaningless alone. Better than what?

### 3.1 The base rate — the "know nothing" line

Always predict the historical frequency. Your events came out positive **46.6 %** of the time, so this baseline answers 0.466 to everything, forever.

It uses **no information whatsoever**: not the earnings, not the news, not the price. A system that cannot beat it has found nothing.

Its score is **0.2487** — and notice this *is* the uncertainty term from §2. The base rate baseline and "task difficulty" are the same number, which is a neat way to remember what uncertainty means.

### 3.2 The logistic regression — the "one good number" line

The harder bar. A logistic regression takes one input — the **earnings surprise**, the gap between reported and expected profit — and turns it into a probability.

*Logistic* means it fits an S-shaped curve rather than a straight line, because a probability must stay between 0 and 1 while a straight line eventually predicts 1.4 or −0.2.

```
probability
   1.0 |                          .-·—·—·—
       |                      .·'
   0.5 |- - - - - - - -·- - - - - - - -     an S-curve: squashed at both
       |            .·'                      ends, steep in the middle
   0.0 |__·—·—·—·'________________________
        big miss      0      big beat
                 earnings surprise
```

Two rules fixed in advance, both defensible in a viva:
- **The surprise is standardised.** A 30-cent miss means something different for a company earning $2 a share than for one earning $20.
- **One formula for all stocks**, not one per stock — each company reports four times a year, so a per-stock fit would rest on almost no data.

Its purpose, in the design notes' words: it shows what the single most informative number achieves. If your models read the full report, the guidance and the news and still can't beat it, the reading and the talking added nothing.

**But your real data changed this story.** The logistic baseline scores **0.2500** — very slightly *worse* than the base rate's 0.2487, with overlapping confidence intervals. (0.2500 is the score of the forecasts exactly as issued. Grouped into the ten bins the decomposition uses, the same forecasts score 0.2496; the 0.00045 difference is the price of binning and is printed as `binning_gap` rather than hidden.) **The earnings surprise carries essentially no signal** for the 5-day abnormal return here. So the two bars have collapsed into one: **the base rate is the bar.** That belongs in your write-up, since §7.2 currently implies the regression is the harder test.

### 3.3 The repetition rung — your actual research question

The third and most important comparison. Beating the base rate shows the models know *something*. Beating the **repetition rung at equal compute** is the only thing that shows *communication itself* helped, rather than merely spending more tokens. That is RQ1, and it's why the compute matching exists.

---

## 4. How they fit together

The chain, in one picture:

```
                     A FORECAST: "0.62"
                             |
                    ┌────────▼────────┐
                    │  BRIER SCORE    │   one number: how good?
                    │      0.2487     │
                    └────────┬────────┘
                             │  "but WHY?"
                    ┌────────▼──────────────────────────┐
                    │      MURPHY DECOMPOSITION         │
                    │  Brier = RELIABILITY              │  honest numbers?
                    │        − RESOLUTION               │  distinguishes cases?
                    │        + UNCERTAINTY              │  task difficulty
                    └────────┬──────────────────────────┘
                             │  "better than WHAT?"
                    ┌────────▼──────────────────────┐
                    │           BASELINES           │
                    │  base rate  → knows nothing   │
                    │  logistic   → one good number │
                    └────────┬──────────────────────┘
                             │  "but is it the TALKING that helped?"
                    ┌────────▼──────────────────────┐
                    │      REPETITION RUNG          │
                    │  same model, same budget,     │
                    │  no communication  →  RQ1     │
                    └───────────────────────────────┘
```

### The one-paragraph version, for your defence

> Forecasts are graded with the **Brier score**, a proper scoring rule, so no system can improve its score by hedging or by feigning confidence. A single score cannot say *why* a system performed as it did, so it is split by the **Murphy decomposition** into reliability, resolution and uncertainty — calibration, discriminating power, and the difficulty of the task itself. The number of bins is fixed in advance at ten — an even number, so that 0.50 falls on a bin edge instead of in the middle of a bin, which keeps “leaning negative” and “leaning positive” apart on either side of it — and not adjusted afterwards. Performance is judged against three reference points of increasing difficulty: the **base rate**, which uses no information; a **logistic regression** on the earnings surprise, which uses the single most informative number; and the **compute-matched repetition rung**, which is the only comparison that isolates the effect of communication rather than of additional computation.

### Three questions you should be ready for

**"Why not just report accuracy?"**
Accuracy needs a yes/no answer, so it throws away the confidence. A model saying 0.51 and one saying 0.99 would score identically, though one is far more useful and the other far more dangerous. The Brier score grades the probability itself.

**"Isn't resolution near zero a failure of your experiment?"**
No — it may be the finding. Five-day post-earnings abnormal returns are close to a coin flip, and even the earnings surprise carries no measurable signal in this sample. Reporting near-zero resolution honestly is the result; engineering the models toward wider spread would manufacture the appearance of skill. This is why the bias correction matters: it distinguishes real discrimination from noise dressed up as discrimination. The same caution runs the other way, though — both optional instruments have a floor of their own (A1, A2), so “above zero” is never the right comparison. The question is always “above what a forecaster who knows nothing would have scored on a sample this size”.

**"How do you know the debate didn't just get lucky?"**
Every comparison carries a 95 % interval from the week-clustered bootstrap, and structures are compared at matched token budgets rather than at matched call counts. A difference that fits inside the intervals is reported as no difference.

---

# Appendix — three optional instruments (available, not committed)

These are implemented in `scripts/22_robustness_optional.py` and verified against this project's own data, but they are **not part of the committed analysis**. They can be applied later to results that already exist.

Each section below states the limitation of the instrument as well as its use, because every one of the three turned out to have a failure mode that produces a *plausible* number rather than an error — and a plausible wrong number is the only kind that reaches a write-up.

**What to say if asked:** *"A bias correction, a bin-free decomposition and a clustered bootstrap are all implemented and available. The methodology commits to the Brier score and the Murphy decomposition; these are held as optional robustness checks rather than as claims, and the limitations they address are stated explicitly."* That is stronger than either never having looked or over-promising.

## A1. The Ferro‑Fricker correction (optional) — because thin bins lie

### The problem

Resolution is computed from "how often did it actually happen in this bin?". With 100 events in a bin, that fraction is reliable. With **3 events**, it is nearly meaningless — you might see 2 of 3 (67 %) purely by chance when the truth is 47 %.

And here is the sting: **that noise always pushes resolution *up*, never down.** Random variation makes bins look different from each other, and "bins look different" is exactly what resolution measures.

**So a system with no skill whatsoever will show a small positive resolution, just from thin bins.**

### Visualising it

Flip a fair coin. Assign each flip a random "forecast" of 0.3, 0.5 or 0.7 — pure noise, zero skill by construction:

| bin | events | heads observed | apparent rate |
|---|---:|---:|---:|
| 0.3 | 4 | 1 | 0.25 |
| 0.5 | 4 | 2 | 0.50 |
| 0.7 | 4 | 3 | **0.75** |

It looks like the forecaster discriminated beautifully — 25 %, 50 %, 75 %! It did nothing at all. Four flips per bin simply vary.

### The idea

Ferro & Fricker (2012) work out exactly how much inflation the thin-bin noise causes and subtract it. The correction is bigger for smaller bins, and it can push a resolution **below zero** — which is the honest way of saying *"not only no discrimination, the raw number was pure noise."*

### In your experiment — this one is not academic

You measured it on your own baseline:

| bins | raw resolution | **corrected** |
|---|---:|---:|
| 5 | 0.00055 | 0.00004 |
| 10 — the committed setting | 0.00057 | **−0.00022** |

Your logistic baseline has **no discrimination**. Raw, it appeared to have a little. With ten bins the corrected value goes negative — the honest verdict.

Why this is critical for you specifically: **the study expects resolution near zero.** So the artefact and the finding live in the same numerical neighbourhood. Without the correction you could report "the debate achieved resolution 0.002!" when 0.002 was thin-bin noise. This is exactly the trap your working document §6.5 anticipated, and now you have measured proof it's real in your data.

### It has to correct the bins that were actually scored

A correction is only as good as its bookkeeping, and this one had a quiet accounting error until 2026-08-30. It rebuilt the bins itself rather than using the decomposition's own rule — and since a computer cannot store 0.6 or 0.7 exactly, the two ended up with very slightly different bin edges. A forecast of exactly **0.60** therefore went into one bin when it was scored and a *different* bin when it was corrected.

That is not a corner case for your data: your models answer on multiples of 0.05, so 0.40, 0.50, 0.60 and 0.70 all sit exactly on an edge — roughly half of every answer they give. The corrected resolution came out **too high by 0.00028** at 883 events, which is about half of the entire 0.00057 in the table above, and by **0.0024** on a 100-event slice.

Two things changed. Forecasts are now rounded to nine decimals before anything is grouped, by a single shared rule (`FORECAST_DECIMALS` in `21_metrics.py`), so that three agents unanimously answering 0.7 — whose average a computer stores as `0.6999999999999998` — count as having said 0.7. And the correction reuses the decomposition's binning rule instead of restating it, then **re-checks at runtime** that the bins it corrected reproduce the reliability and resolution the decomposition reported. If they ever drift apart again, the result says so rather than quietly being wrong.

### When the correction refuses to answer at all

A bin holding **one** event is the case this formula cannot touch. It removes the sampling wobble of a bin's observed rate, estimated as `o(1−o)/(n−1)` — and with one event that divides by zero. The correction is *undefined* there, which is not the same as zero: one observation says nothing about how far it would have moved on a second draw.

That matters more than it sounds, because a singleton bin is exactly where the artefact is **largest**. Its observed rate is 0 or 1 by construction, so it sits as far from the base rate as arithmetic allows and pours its whole contribution into resolution whether or not the forecast had any skill.

The code used to skip such bins and report the rest as "corrected". Measured on a deliberately built case — 124 events, 120 of them in two fat bins that sit exactly on the base rate, plus four tail bins holding one event each:

| | value |
|---|---|
| the true resolution | **0** |
| raw resolution | 0.00806452, **all** of it from the four singleton bins |
| what the old code called "corrected" | 0.00600 — 74 % of the artefact survived |
| what it reports now | no corrected figure at all |

Four events out of 124 are 3 % of the sample and 100 % of the spurious resolution — which is why the share is measured against the **resolution**, not against the event count.

So the rule now is: always measure how much of the raw resolution sits in bins the correction cannot reach, always report that share, and **refuse to print a corrected number once the share passes a half.** Above a half the word "corrected" would be describing a figure that is mostly uncorrected, and a wrong label is worse than a missing number. What comes back instead is a plain-English refusal:

> corrected figures withheld: 4 bin(s) holding one event each account for 100 % of the raw resolution, and the Ferro‑Fricker term is undefined there because it divides by n_bin − 1. Reporting the remainder as 'corrected' would overstate what was removed.

Below the threshold the corrected figures are returned **together with** the number of singleton bins and the share they carry, so you can discount them yourself. On your real baselines at ten bins there are no singleton bins at all — the risk lives in per-structure slices of a hundred events, not in the full 883.

---

## A2. CORP (optional) — removing the arbitrary choice of bins

### The problem

Murphy's decomposition needs bins. But *how many?* Five? Ten? Twenty? **The answer changes your result.** And nothing in the mathematics tells you the right number — you just pick one. That is a researcher degree of freedom, and a reviewer is entitled to ask "what did the other choice give?"

Worse for you: your models answer on a **coarse grid** — 82 % of answers are multiples of 0.05, and 0.45 alone accounts for 16 of DeepSeek's 26. So some bins overflow while others sit empty. Fixed-width bins fit this data badly.

### The idea

CORP (Dimitriadis, Gneiting & Jordan, 2021) throws bins away entirely.

Instead it asks: **what is the best possible recalibration of these forecasts?** Sort the forecasts from lowest to highest, then find the smoothed, never-decreasing curve of outcomes that best fits — a technique called isotonic regression. That curve is what the forecasts *should* have said.

Then read the three quantities straight off:

- **Miscalibration (MCB)** = your score − the recalibrated score → *what your dishonesty cost you*
- **Discrimination (DSC)** = the base-rate score − the recalibrated score → *what your ranking was worth*
- **Uncertainty (UNC)** = the base-rate score → *the task's own difficulty*

Same identity: **Brier = MCB − DSC + UNC.**

### Visualising it

Six forecasts, sorted:

| you said | outcome | isotonic fit ("what you should have said") |
|---:|---:|---:|
| 0.20 | 0 | 0.00 |
| 0.30 | 0 | 0.00 |
| 0.45 | 1 | 0.50 ⎫ these two |
| 0.50 | 0 | 0.50 ⎭ get pooled |
| 0.70 | 1 | 1.00 |
| 0.80 | 1 | 1.00 |

The 0.45/0.50 pair had outcomes 1 then 0 — out of order. Isotonic regression **pools** them into a single 0.50 rather than letting the curve go backwards. No bin widths were chosen anywhere; the data decided the grouping.

### The trap: events that were given the *same* number

Isotonic regression fits a curve **of the forecast**. Two events both forecast at 0.45 must therefore come out of it with the same recalibrated value. If they don't, the answer depends on which of them the database happened to return first — and that is not a property of your data at all.

The obvious implementation gets this wrong, and this project's did until 2026-08-30. Pooling only happens where the sequence goes *down*; a run of tied forecasts whose outcomes arrive in the order 0, 0, 1, 1 never goes down, so nothing pools and every event is "recalibrated" to its own outcome. Four events, all forecast 0.5:

| the outcomes, in this order | old fit | discrimination |
|---|---|---:|
| 0, 0, 1, 1 | 0, 0, 1, 1 — each event handed its own outcome back | **0.25**, a flawless ranking |
| 1, 1, 0, 0 | 0.5, 0.5, 0.5, 0.5 — one pooled block | **0**, the truth |

On your own data this was not a rounding-level effect. The base-rate baseline says the *same number* to all 883 test events, so it ranks nothing by construction and its discrimination is exactly 0:

| | discrimination |
|---|---:|
| the truth | **0** |
| old code, events in date order | **0.00058** |
| old code, the identical rows reversed | **0.02665** |
| the code now, either order | **0.00000** |

0.00058 is the same size as the 0.00057 resolution your logistic baseline reports — the artefact and the finding sat in the same numerical range. And ties are the normal case here, not a corner: your models answer on a 0.05 grid, one of your two baselines is constant, and a debate that converges produces still more of them.

The fix is to pool tied forecasts into one block **before** the curve is fitted, weighted by how many events share the number. The fit then sees one entry per distinct forecast, and the row order cannot reach it. A related rounding rule sits underneath: forecasts are rounded to nine decimals first, so that three agents unanimously answering 0.7 — whose average a computer stores as `0.6999999999999998` — count as one forecast rather than as two.

### The limitation to state out loud: discrimination has a floor above zero

The curve is fitted on the same outcomes it is then scored against. So it is the best recalibration **in hindsight**, and it flatters the forecast a little by construction — the very same species of artefact that Ferro‑Fricker exists to strip out of the binned decomposition. CORP escapes the choice of bins; it does not escape being fitted in-sample.

Measured on this project's own data, with forecasts drawn **independently** of the outcomes so the true discrimination is exactly zero:

| sample | mean discrimination | came out positive in |
|---|---:|---:|
| n = 883, answers on the 0.05 grid | 0.00045 | 86 % of draws |
| n = 883, continuous answers | 0.00180 | 100 % |
| n = 100, answers on the 0.05 grid | 0.00384 | 85 % |

Your committed Murphy resolution for the logistic baseline is 0.00057. So on a per-structure slice of about a hundred events, a forecaster that knows *nothing* scores roughly seven times the effect you are looking for. **Compare a CORP discrimination against the floor for its own sample size, never against zero.** The function returns that warning in a `caveat` field so it travels with the number wherever the number goes.

This is a known property of in-sample CORP rather than a coding error, and it is deliberately left uncorrected: cross-fitting the curve would change the instrument the analysis plan names, which is a methodological decision rather than a bug fix. Saying so plainly is the defensible position — and it is a good answer to have ready if an examiner asks what CORP costs you in exchange for removing the bins.

### In your experiment

CORP is your **defence against the binning objection**. When someone asks "why ten bins?", the answer is: "we fixed ten in advance, *and* we report a method that needs no bins at all, and both agree."

It also suits your data far better, because it adapts to wherever the forecasts actually cluster instead of imposing equal widths on a lumpy grid.

**Your real numbers, after the tie fix:**

| baseline | MCB | DSC | reading |
|---|---:|---:|---|
| base rate | 0.0035 | **0.00000** | a constant forecast ranks nothing, and now the number says so exactly |
| logistic on surprise | 0.0055 | 0.00068 | above the 0.00045 floor for n = 883, but only just — not evidence of discrimination |

Both agree with the binned decomposition, which is the point of running them side by side.

---

## A3. The week-clustered bootstrap (optional) — how sure are we?

### The problem

You compute Brier 0.2487 for one system and 0.2461 for another. Is that a real difference, or would it vanish with a different sample of events?

The standard answer assumes every event is independent. **Yours are not.** Earnings cluster into a few weeks per quarter, and every company reporting in the same week shares the same market conditions — the same Fed decision, the same inflation print, the same risk mood. If that week was strange, *all* of those forecasts are wrong together.

Treating them as independent would make you far more confident than the data warrants. This is a classic way for a study to overstate its findings, and reviewers look for it.

### The idea

Resample **whole weeks**, not individual events.

1. Take your list of calendar weeks.
2. Draw weeks at random *with replacement* — some appear twice, some not at all.
3. Recompute the Brier score on that reshuffled set.
4. Do it 2,000 times.
5. The middle 95 % of those scores is your confidence interval.

Because whole weeks move together, the interval correctly reflects that events inside a week are not independent pieces of evidence.

### Visualising it

```
real data:      [wk1 wk2 wk3 wk4 wk5]
resample 1:     [wk3 wk1 wk1 wk5 wk2]   -> Brier 0.2471
resample 2:     [wk2 wk2 wk4 wk5 wk1]   -> Brier 0.2509
resample 3:     [wk5 wk3 wk2 wk2 wk4]   -> Brier 0.2483
...  x2000  ->  sort them  ->  95 % interval = [0.2466, 0.2516]
```

### When there is no interval to give

A resample can only be as rich as the number of weeks it has to draw from. With one week there is exactly **one** possible resample — the sample itself — so all 2,000 repetitions recompute the identical number and the "95 % interval" comes back with width **exactly zero**.

That is the dangerous failure, because zero width does not look like a failure. Printed next to *95 % CI* it claims **perfect precision** from fifty events, which is the precise opposite of what one week of data supports, and nobody queries a result that looks that good.

So below two weeks the bounds are returned as "no number", with a flag saying the interval is undefined and a sentence saying why. *No information about the precision* and *perfect precision* are opposite claims, and the code must not print the second when it means the first.

Just above that threshold the interval exists but is coarse, and it comes with a note that counts how coarse:

| distinct weeks | possible resamples | what you get |
|---:|---:|---|
| 1 | 1 | no interval — refused |
| 2 | 3 | an interval, plus "read it as an order of magnitude, not as a bound" |
| 3 | 10 | the same warning |
| 23 (your test set) | 4.1 × 10¹² | an ordinary interval, no note |

Twenty clusters is the usual rule-of-thumb minimum for a clustered bootstrap, and anything below it carries the note.

### In your experiment

Every headline number gets one of these intervals. Your base rate result is already reported as **0.2487, 95 % CI [0.2466, 0.2516]** — and the logistic baseline's interval [0.2469, 0.2541] overlaps it almost entirely, which is precisely how you know the difference between them is not real.

**One live problem.** Your 60/40 split leaves 883 test events but only **23 distinct weeks**. Since weeks are the unit, 23 is the effective sample size, not 883 — and your §11 wants 30–40. This is a real constraint on the analysis plan, and the fix is in the split and the event window, not in collecting more events per week.

23 clears the two-week floor and the twenty-cluster warning, so the headline intervals come back clean. The exposure is one level down: **a per-structure or per-sector slice can easily hold only a handful of weeks**, and one that holds a single week will now return no interval rather than a flattering one. When that happens, report the refusal — an absent interval is a finding about your sample, not a gap to be filled with the naive event-level version.

---


---

## What the committed analysis does without them

| optional check | the problem it addresses | how the committed analysis handles it |
|---|---|---|
| Ferro‑Fricker | thin bins inflate resolution upwards | ten bins fixed in advance; the decomposition reports how many bins held a single event, and a small positive resolution is reported as *possibly noise*, not as a finding |
| CORP | the bin count is an arbitrary choice | ten bins fixed before the run and never adjusted; grouping by distinct forecast value is available as a check that needs no choice |
| week bootstrap | events in the same week are not independent | point estimates only; the limitations state that the effective sample is closer to 47 calendar weeks than to 2,267 events, so small differences are not claimed as real |

The third is the one that costs the most. Without an interval, a small difference between two structures cannot be shown to be real — so the honest phrasing throughout the results is comparative and cautious rather than declarative.
