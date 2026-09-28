# Design decisions D1–D32

This register is a distilled record of the design decisions behind the experiment, kept so that references such as "D28" in the thesis resolve to a stable entry. Each entry keeps the binding decision, its reason, and the alternatives that were rejected. Session narrative, chat exchanges and working notes are deliberately omitted.

## D1 — The aggregator is blind
**Date:** 2026-08-25
**Decision:** The aggregating agent receives only the workers' reasoning sections — no evidence, no peer probabilities.
**Why:** The exposé commits to this mechanism, and blindness is what makes the rung attributable: with the evidence the aggregator would be a fourth independent analyst. The risk that relevant evidence never reaches it is disclosed, and mitigated by a shared instruction requiring data-grounded arguments on every rung.
**Rejected:**
- Aggregator with the evidence: destroys attribution.
- Filter-workers forwarding curated evidence: voids the shared-worker baseline.

## D2 — Leakage controlled by model selection
**Date:** 2026-08-25
**Decision:** Only models whose training data ends before the events are used; evidence is not date-scrubbed and no memory warnings appear in the prompt.
**Why:** Dated context is necessary at input level 2, and the cutoff already makes outcome memories impossible. An addendum separates leakage (model-side, shielded by the cutoff) from injection (experimenter-side, shielded by sterile role text).
**Rejected:**
- Keeping dates out of the base instruction: the wrong layer.

## D3 — The base instruction, sentence by sentence
**Date:** 2026-08-25
**Decision:** Persona line, situation line, "using only the information given to you", abnormal-return definition, grounding requirement, calibration line, anti-hedging line — byte-identical for every agent on every rung.
**Why:** The bland persona sets the register without injecting method hints, and the grounding sentence makes each argument cite its data, which is what the blind aggregator consumes. Uniformity keeps the ladder comparison fair.
**Rejected:**
- A more specific persona: injects market knowledge.
- A longer abnormal-return explanation: teaches the model how to analyse.

Superseded in part by D8 and by D4/D5.

## D4 — Calibration line kept
**Date:** 2026-08-25
**Decision:** A calibration sentence stays in the base instruction ("when you say 70%, such events should occur about 70% of the time").
**Why:** It defines the unit the output is graded in, not how to analyse. Without a shared scale, RQ3's reliability differences would mix scale interpretation with capability.
**Rejected:**
- Dropping the line: adds cross-model scale noise.
- Keeping it with a pilot A/B test.

Superseded by D5.

## D5 — Calibration sentence reworked
**Date:** 2026-08-25
**Decision:** The line is restated abstractly over p and names the Brier score as a proper scoring rule.
**Why:** The old wording planted the number 70% in every prompt — the anchoring hazard D6 rejects for examples. Naming the proper rule activates the honest-reporting incentive.
**Rejected:**
- Leaving the metric unnamed: jargon without added semantics.

## D6 — Format contract: empty template only
**Date:** 2026-08-25
**Decision:** An empty tagged template with no example answer; the parser's tolerance for percentage forms stays unadvertised.
**Why:** Any example value sits in every call and anchors the answer distribution, the midpoint included. The tolerance is a silent safety net, not a second contract.
**Rejected:**
- A filled-in example answer.
- Advertising the tolerant parse as a second format.

## D7 — Question line after the evidence
**Date:** 2026-08-25
**Decision:** The question is restated after the evidence, followed by a one-sentence format reminder.
**Why:** At level 2 the evidence exceeds 1,200 tokens and the opening instructions lose grip. About 25 tokens per call, identical on every rung.
**Rejected:**
- Restating the question without the format reminder.

## D8 — Abnormal return defined precisely
**Date:** 2026-08-25
**Decision:** "Abnormal return means the stock's five-day return minus the five-day return of the market (S&P 500)" — no beta adjustment.
**Why:** The variant chosen changes the model's reasoning on boundary cases, and outcome and base rate are already stock minus market, so prompt, measurement and baseline are one quantity. Residual beta exposure is identical across all cells.
**Rejected:**
- A vague definition: each model would resolve the term differently.
- An explanatory example: itself an analysis pattern.

## D9 — Frozen-artifact argument and event eligibility
**Date:** 2026-08-25
**Decision:** The training cutoff is bounded above by the release date, and an event is eligible for a model iff its t0 lies after that date.
**Why:** An upper bound is what a safety argument needs, so vendor cutoff documentation becomes unnecessary; the rule leaves 1,956 events over 52 calendar weeks with the base rate essentially unchanged. It holds only for the artefact actually served, so checkpoint and provider pinning become load-bearing controls.
**Rejected:**
- Carrying undocumented cutoffs as a blocking uncertainty.

## D10 — Aggregator role instruction
**Date:** 2026-08-25
**Decision:** The role block states the asymmetry, that peer numbers are withheld, that cited data comes from the source material, and the combining task.
**Why:** Stating the asymmetry stops the model treating absent evidence as an error, and the provenance sentence licenses reasoning about cited data instead of discounting it. Specifying the combining function keeps both rungs at equal specification.
**Rejected:**
- None recorded. A same-day amendment removed the role's persona restatement.

## D11 — Debate revision instruction
**Date:** 2026-08-26
**Decision:** Debaters revise where peers convince them, keep their estimate where they do not, state disagreement openly, and do not converge merely to converge.
**Why:** LLM debates collapse sycophantically, and the scaffold study's wording keeps the rung comparable; collapse despite an explicit counter-instruction is a stronger result. Cost: preserved diversity could not then be credited to debate as such.
**Rejected:**
- Neutral revise-or-keep wording: cleaner attribution, breaks comparability.
- The Du et al. wording: presupposes change, never licenses holding.

## D12 — Debaters see their own previous answer
**Date:** 2026-08-26
**Decision:** Each debater is shown its own previous-round answer, booked in a separate bucket, never merged into the peer-message bucket.
**Why:** Calls are stateless, so a genuine revision needs the earlier answer, which D11 presupposes. Re-reading one's own output is not inter-agent communication; both exclusive and inclusive figures are published.
**Rejected:**
- Merging self-context into the communication bucket.

Headline choice settled by D30.

## D13 — Navigation headers kept
**Date:** 2026-08-26
**Decision:** Short navigational headers are kept, round number included; peer identities stay stable and the total number of rounds is never revealed.
**Why:** They are content-free at 5–10 tokens each, and without the total the round number gives no strategic signal. Stable identities let an agent notice whether a peer moved.
**Rejected:**
- Dropping the round number for purity: unnecessary given the missing total.

Superseded in part by D21.

## D14 — Structural parameters
**Date:** 2026-08-26
**Decision:** N=3 agents inside the communicating structures, R=2 revision rounds, temperature 0.7 everywhere, with per-round probabilities recorded as a spread diagnostic.
**Why:** Three is canonical and the smallest N at which a majority and a dissenter can coexist, and two rounds give a trajectory rather than one snapshot. Temperature above zero is structural — at zero the repetition rung cannot exist — and 0.7 is the convention.
**Rejected:**
- N=2: no dissenter possible.
- A single revision round: no trajectory.
- Tuning temperature: an added researcher degree of freedom.

## D15 — Tags count as C2, the bare number as C1
**Date:** 2026-08-26
**Decision:** Only the bare number is booked as C1; the decision tags go to C2.
**Why:** The tags exist only because this study chose a tagged contract, so counting them in C1 would make it shift with the output format. Two tokens against hundreds: definitional, not material.
**Rejected:**
- Counting the tags as part of the answer.

## D16 — Malformed handling and retries
**Date:** 2026-08-26
**Decision:** An unparseable answer triggers a resend of the identical prompt and a malformed worker drops the whole event for that model. The amendment pre-registers a minimum of one retry, floors attempts at two, and adds a taxonomy (infrastructure, truncated, format).
**Why:** A corrective hint would be a different prompt and would favour models unreliable at formatting, and replacing a shared worker would break the sharing guarantee. Retrying cannot be answer-shopping, since a malformed call holds no parseable probability and the trigger is mechanical.
**Rejected:**
- A corrective reminder on the retry.
- Replacing a failed worker instead of dropping the event.
- Lumping all failures together as "malformed".

## D17 — Cross-model comparison on the common event set
**Date:** 2026-08-26
**Decision:** The primary cross-model comparison uses only events every model completed; per-model counts, drops and results are reported beside it.
**Why:** Drops happen per model, so model quality would otherwise be confounded with which events each completed. Fixing the rule before the run removes the freedom to choose afterwards.
**Rejected:**
- Per-model sets only: differences become partly a difference in samples.
- Common set everywhere: discards information RQ1 and RQ2 do not need.

## D18 — Fixed template overhead booked to D
**Date:** 2026-08-26
**Decision:** A probe per model measures the fixed chat-template overhead; that constant goes entirely to the baseline bucket, and only the length-proportional residual is spread.
**Why:** The overhead does not grow with prompt length and a lone forecaster pays it too, so spreading it inflated the communication cost in one direction. A second bias (D18-b) was ours: per-segment tokenisation over-counted boundary tokens, so counts now come from cumulative prefixes.
**Rejected:**
- Keeping the proportional split and disclosing the bias.

## D19 — Probabilities combined by the arithmetic mean
**Date:** 2026-08-26
**Decision:** The arithmetic mean combines probabilities, in the communication-free rung and the debate's final round alike.
**Why:** The exposé commits to averaging because it preserves the information the Brier score evaluates. The score is convex, so the mean never scores worse than the typical individual and gains exactly the forecasters' diversity.
**Rejected:**
- The median: discards two of three values at N=3 and suppresses the dissent under study.

## D20 — Roster: two deliberately dissimilar arms
**Date:** 2026-08-26
**Decision:** A 32B dense reasoning model with reasoning on and a 671B mixture-of-experts non-reasoning model, differing as much as possible in family, scale, architecture and serving precision.
**Why:** This meets the exposé's requirement of one reasoning and one non-reasoning model, and since RQ1 and RQ2 are within-model, varying the model buys a robustness probe. With four dimensions varying at once, a difference between arms cannot be attributed to reasoning.
**Rejected:**
- Two closely matched models: cleaner contrast, weaker external validity.

Superseded in part by D24; extended by D29. Note on "serving precision": when this decision was taken the reasoning arm was served at fp8 and the non-reasoning arm at fp4, so the two differed in precision as well. D24 moved the non-reasoning arm to an fp8 endpoint two days later, and the full run was made with both arms at fp8. From D24 onward the arms differ in family, scale and architecture, not in serving precision; the pre-registered plan's wording "(family, scale, architecture, serving precision)" predates the run and is stale on that fourth point.

## D21 — Peer headers changed to prose
**Date:** 2026-08-27
**Decision:** The header introducing another agent's text changes from a delimiter form to prose ("Analyst 2 wrote:").
**Why:** A pilot failure appeared to imitate its input's delimiter style, so this was logged as a precaution, not a fix, since the failure did not reproduce. A larger pilot refuted the hypothesis — workers, whose prompts have no peer headers, also failed — and located the cause in one arm's format compliance (0.0 % versus 12.0 %).
**Rejected:**
- Forbidding other delimiters in the format contract: treats the symptom.
- Accepting the drop rate under D17: costs events for no benefit.

## D22 — Hidden and visible output split by local proportions
**Date:** 2026-08-28
**Decision:** C1, visible C2 and hidden C2 are counted with one local tokenizer and rescaled to the provider's completion total, not derived by subtraction.
**Why:** Over 120 calls the reasoning-text ratio was a tight constant (CV 6.6 %) while the subtraction ratio was chaotic (CV 47 %). One ruler measures every part, so its bias cancels per call; the scope is only the division of C2.
**Rejected:**
- Treating the provider's reasoning-token count as authoritative for the split.
- Applying the ~9 % correction an independent reference implies: hides an unverifiable assumption.

## D23 — Compute matching
**Date:** 2026-08-28
**Decision:** The largest communicating structure's spend sets the ceiling per event and model; the communication-free rung is drawn in generation order until it is covered, shared workers first, and each structure is compared against the prefix fitting its own budget, in total tokens.
**Why:** Total tokens follows the exposé's reasoning that more tokens mean more compute, and gives the baseline about 14 runs where completion-only gives about 10 — a stronger, conservative null. The fitting prefix under-funds the baseline and records the shortfall, and runs are never re-sorted.
**Rejected:**
- The prefix that covers the budget: over-funds the baseline.
- Whichever prefix is closer: the bias direction varies event by event.

## D24 — The non-reasoning arm re-pinned
**Date:** 2026-08-28
**Decision:** That arm moves to another endpoint after the incumbent host collapsed concurrent identical requests; an independent-samples check becomes a permanent report column.
**Why:** The host returned byte-identical text for three concurrent identical prompts at temperature 0.7 — caching, not sampling — which removes the diversity the communication-free rung rests on. Well-formed collapsed answers would have looked like a fact about the model.
**Rejected:**
- Format non-compliance, quantisation and rate limiting: each ruled out by measurement.
- Falling back to a third model.

Consequence: the new endpoint serves the model at fp8 instead of fp4, so after this decision both arms run at the same serving precision (see the note under D20).

## D25 — Compute matching re-affirmed on real data
**Date:** 2026-08-28
**Decision:** The unit stays total tokens and the prefix rule stays "fits", after a challenge proposing completion-only matching.
**Why:** Completion-only matching is not level-invariant either, since answer length moves with input volume in opposite directions for the two arms. At level 2, 88 % of one debate's consumption is input — the peer messages RQ2 reports as the cost of communication.
**Rejected:**
- A completion-token unit.
- Deciding on cost grounds: the spread was immaterial and was set aside.

## D26 — No instruction to be "more discriminating"
**Date:** 2026-08-28
**Decision:** No prompt sentence tells the model to be decisive when confident; resolution is reported as measured.
**Why:** Such a sentence targets the distribution resolution is computed from, and a proper scoring rule already makes honest reporting optimal. Low resolution on a near-coin-flip problem is itself a finding, and the premise failed anyway: the clustering was seen within one deliberately ambiguous event.
**Rejected:**
- The proposed discrimination instruction.

## D27 — The probability is kept out of the reasoning text
**Date:** 2026-08-29
**Decision:** One line in the shared format instruction states that the probability belongs only in the decision section; the residual leak rate is counted per event.
**Why:** Only the tag was stripped, so numbers stated in prose passed through and anchored an aggregator told it could not see them; 3 of 12 worker answers leaked and one aggregator reproduced a leaked number exactly. The shared block reaches every condition identically and keeps the coordination bucket textually equal.
**Rejected:**
- Masking numbers in code: alters model output, misses verbal statements.
- Dropping the claim from the role block: honest, but collapses the rung.
- Changing nothing: leaves the prompt untrue and discards a quarter of events.

## D28 — The event sample
**Date:** 2026-09-03
**Decision:** 210 symbols drawn at random with seed 20260903 from the sorted pool of 445 eligible symbols, first four eligible quarters each — 840 events.
**Why:** Calendar weeks, not events, set the effective sample size, and 800 events already cover 54 of 56 weeks; ten symbols of slack absorb the events D17 costs. A random draw already covers the sector and size range asked for and leaves no discretion, and sorting before sampling makes it reproducible.
**Rejected:**
- Stratified sampling: requires choosing strata and weights.
- The full eligible pool: triples cost and wall clock for two extra weeks.
- All-quarters variant: matches the population base rate exactly, hence a post-hoc rule choice.

Disclosed: sample base rate 48.69 % against the population's 46.50 %, arising at the four-quarter rule, not the draw.

## D29 — The reasoning arm load-split across two endpoints
**Date:** 2026-09-05
**Decision:** That arm runs on two endpoints of identical precision, each event pinned to one by a hash of its base identifier, so both levels and all repeats of an event share one provider.
**Why:** One host throttled the arm into a retry spiral and splitting halves its load; both endpoints passed the D24 collapse test with eight of eight distinct responses. The one-token template offset lands on the shared baseline bucket and cancels within every event.
**Rejected:**
- Per-call splitting: mixes two token bases inside one structure's ledger.
- Splitting across two precisions: makes the repetition rung a mix of two models.
- A full swap to one endpoint: discards verified capacity and the cross-provider check.

## D30 — The pre-registered analysis plan is frozen
**Date:** 2026-09-05
**Decision:** The plan is frozen before the full runs, settling three deferred choices: the self-context bucket stays out of the headline communication cost as its own metric; point estimates only; quality at matched budget and communication cost presented side by side.
**Why:** Freezing fulfils the exposé's milestone that evaluation choices be fixed in writing before the runs. The documented stance is point estimation with pre-registered language — sign consistency across models, thin-cell flags — rather than a test weak at roughly 52 effective observations.
**Rejected:**
- Committing the calendar-week block bootstrap: kept as an optional post-hoc instrument.
- Full multiplicity-corrected testing: contradicts the stance and is weak here.
- A ratio of Brier improvement per 1,000 communication tokens: flips sign on noise.

## D31 — Amendment to the frozen plan after the adversarial review
**Date:** 2026-09-05 (Parts 1–5); 2026-09-06 (Parts 6–7)
**Decision:** The plan is amended in place with marked references: false or miscited statements corrected; the numeric noise anchor removed with no replacement; the RQ3 level gate and baseline reference curve committed; a tare-check negative control added; ties counted against coordination; the work list seed-shuffled with seed 20260905; a sign flip in a committed sensitivity check forcing the label "not robust"; the study window set to 2026-05-30.
**Why:** Correcting false statements is not a loosening — the harness was fixed first, then the text rewritten to what is true. The anchor was never mandated by the exposé and went before any run data existed; the robustness trigger is the sign alone, since a threshold would reintroduce the cutoff just removed.
**Rejected:**
- Recomputing the stale baseline numbers rather than pinning the committed construction.
- A smallest-effect-of-interest of 0.005 Brier: a quasi-test without justification.
- Counting exact ties as neither win nor loss: matching free repetition earns nothing.
- A timing rule for the exploratory fence: dropped entirely.
- Re-drawing the sample after the window was set: byte-identical, and breaches the pinned-sample rule.

## D32 — A blind operational checkpoint after 50 events
**Date:** 2026-09-06
**Decision:** The run pauses after the first 50 distinct events of the seed-shuffled order — the same 50 at both levels and on both models — for a check that opens no forecast, label or score; go/no-go is infrastructure only, declared before any event of the run exists.
**Why:** Before committing the whole budget it must be established that the pipeline works end to end and that time, spend and attrition are known. The pause is fixed in advance, so it cannot depend on what the outputs show, and the plan permits operational stops whose reason is recorded first. Malformed attempts and dropped rungs are reported, not gated: a high rate is a finding, not a reason to touch the prompts.
**Rejected:**
- A separate 50-event sample file: would make two runs out of one experiment.

**Deviations logged:**
- Incident before the start (2026-09-06): a wrapper test accidentally launched real paid sessions; both killed, no ladder completed, records purged as orphans, and the wrapper gained a dry-run mode. An incident, not signed off as a decision.
- Deviation 1 (2026-09-07): an endpoint throttled the reasoning arm into a retry spiral; retries now queue behind the shared throttle and its spacing was raised. No data affected. Author-approved beforehand.
- Deviation 2 (2026-09-07): per-level concurrency raised from 6 to 10, a setting only, since the throttle governs the request rate. Author-approved.
- Deviation 3 (2026-09-08): 15 ladders hung silently; a 180-second attempt deadline and a 60-minute ladder watchdog were added, a hang now counting as an outage. Reported under the standing pause-fix-resume approval.
- Interruption (2026-09-08, external): a forced system restart killed both processes; integrity was intact and in-flight ladders were re-run. No setting changed; resumed on the author's decision.
- Deviation 4 (2026-09-09): a resume aborted because a raw log exceeded the runtime's string limit; the reader and purge now work on buffers and chunks. Harness fix, no method change, no data touched.
- Deviation 5 (2026-09-09): the scorer's structure-cost check compared usable tokens against the harness's pre-registered billed total; check and scored rows now use the billed total, no reading rule changed. The author signed it off on return.
