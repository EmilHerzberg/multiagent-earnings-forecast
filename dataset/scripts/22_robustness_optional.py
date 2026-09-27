"""OPTIONAL post-hoc robustness checks. NOT part of the committed analysis.

The thesis commits to what is in `21_metrics.py`: the Brier score, the Murphy
decomposition, the two baselines, and the compute-matched repetition rung. That
is what the exposé promises and what the methodology chapter describes.

This file is separate, and nothing in `21_metrics.py` imports it. It holds three
further instruments that can be applied AFTERWARDS, to results that already
exist, if time and the page budget allow:

    1. Ferro & Fricker (2012)  — bias correction for the resolution term
    2. CORP, Dimitriadis et al. (2021) — a decomposition needing no bins
    3. block bootstrap by calendar week — confidence intervals that respect
       the fact that events in one week are not independent

None of them needs new data or a re-run: they are functions of the probabilities
and outcomes the experiment has already produced.

    THE RULE THAT KEEPS THIS HONEST
    -------------------------------
    Deciding WHETHER to run these is free — a matter of time and space.
    Deciding whether to REPORT them is not. If a check is computed, its result
    is reported, whether it supports the main finding or undermines it.

    Running several analyses and presenting the most flattering one is the
    "garden of forking paths". Running a pre-registered analysis and then adding
    a clearly-labelled robustness check is ordinary good practice. The only thing
    separating the two is whether the inconvenient result also gets written down.

    python scripts/22_robustness_optional.py --self-test
    python scripts/22_robustness_optional.py --demo
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DB = HERE.parent / "thesis.db"


def _load_metrics():
    """Import the committed module. The dependency runs one way only:
    robustness may use metrics, metrics never uses robustness."""
    spec = importlib.util.spec_from_file_location("metrics", HERE / "21_metrics.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["metrics"] = m
    spec.loader.exec_module(m)
    return m


metrics = _load_metrics()
brier = metrics.brier


# --------------------------------------------------------------------------- #
# 1. Ferro & Fricker (2012): the resolution term is biased upwards
# --------------------------------------------------------------------------- #
# A bin holding ONE event is the case this correction cannot handle. What it
# removes is the sampling variance of the bin's observed frequency, estimated as
# o(1-o)/(n-1); with n = 1 that divides by zero, because one observation says
# nothing about how far it would have moved on a second draw. The correction is
# UNDEFINED there - which is not the same as zero.
#
# The old code skipped those bins and reported the rest as "corrected". That is
# the worst of the three available answers, because a singleton bin is exactly
# where the artefact is LARGEST: its observed frequency is 0 or 1 by
# construction, so it sits as far from the base rate as arithmetic allows and
# pours its whole contribution into resolution whether or not the forecast had
# any skill. Measured on a no-skill forecaster with 120 events in two fat bins
# plus four singleton tail bins, the "corrected" resolution still came out at
# 0.0081 against a true value of 0 - 80% of the artefact survived, and the number
# is fourteen times the resolution this thesis reports for its own baseline
# (0.00057). With EVERY bin a singleton the correction moved the wrong way:
# corrected > raw in 99.9% of draws.
#
# So the rule is: measure how much of the raw resolution comes from bins the
# correction cannot touch, always report it, and REFUSE to print a corrected
# figure once that share passes a half. Above a half, the word "corrected" would
# be describing a number that is mostly uncorrected, and a wrong label is worse
# than a missing number. The threshold is a judgement call, so it is stated here
# rather than buried; below it the corrected figures are returned together with
# the share they could not reach, so a reader can discount them.
FF_MAX_UNCORRECTED_RESOLUTION_SHARE = 0.5


def _murphy_bins(p, n_bins: int) -> np.ndarray:
    """Assign forecasts to bins the way `metrics.murphy` does - not a near-copy.

    This used to re-derive its own bins from a bare
    `np.linspace(0, 1, n_bins + 1)`, while murphy() rounds those edges to
    `metrics.FORECAST_DECIMALS` places. That is not pedantry: np.linspace returns
    0.6000000000000001 and 0.7000000000000001 rather than 0.6 and 0.7, so a
    forecast of exactly 0.6 fell in the bin BELOW here and the bin ABOVE there.
    The bias term was then computed over a different grouping than the resolution
    it was being subtracted from. On the 0.05 grid these models actually answer
    on, the corrected resolution came out too HIGH by +0.00028 at n=883 - half
    the entire resolution the thesis reports - and by +0.0024 on an n=100 slice.

    The rounding rule is reproduced from the constant murphy() itself uses, and
    ferro_fricker() then VERIFIES the outcome instead of trusting it: it
    recomputes reliability and resolution from these bins and compares them with
    what murphy() returned. If the two ever drift apart, `bins_match_murphy`
    goes False.
    """
    p = metrics.canonical(p)
    edges = np.round(np.linspace(0.0, 1.0, n_bins + 1), metrics.FORECAST_DECIMALS)
    return np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)


def ferro_fricker(p, y, n_bins: int = metrics.N_BINS) -> dict:
    """Remove the upward bias that thin bins introduce into resolution.

    Within a bin, the share of events that actually happened is itself an
    estimate from few observations, and that sampling noise makes bins look more
    different from one another than they are. Since resolution measures exactly
    how different the bins are, noise inflates it — always upwards, never down.

    A forecaster with no skill at all therefore shows a small positive
    resolution. That matters here because this thesis expects resolution near
    zero, so the artefact and the finding live in the same numerical range.

    The corrected resolution can come out NEGATIVE. That is not an error: it is
    the honest reading of "no discrimination whatsoever, and the raw figure was
    sampling noise".

    Bins holding a single event are the one case the formula cannot address; see
    FF_MAX_UNCORRECTED_RESOLUTION_SHARE above. The returned dict always carries
    `singleton_bins`, `singleton_event_share` and `uncorrected_resolution_share`,
    and when the correction could not reach most of the raw resolution,
    `correction_applied` is False and the corrected figures are NaN.
    """
    p = metrics.canonical(p)
    y = np.asarray(y, float)
    raw = metrics.murphy(p, y, n_bins=n_bins)      # this also validates p against y
    n = len(p)
    base = float(np.mean(y))

    which = _murphy_bins(p, n_bins)

    bias = 0.0
    bias_variance_route = 0.0    # the same quantity by a different formula; see below
    singleton_bins = 0
    singleton_events = 0
    uncorrected_resolution = 0.0
    check_reliability = 0.0
    check_resolution = 0.0
    for k in range(n_bins):
        m = which == k
        nk = int(m.sum())
        if nk == 0:
            continue
        ok = float(y[m].mean())
        said = float(p[m].mean())
        check_reliability += nk * (said - ok) ** 2
        check_resolution += nk * (ok - base) ** 2
        if nk <= 1:
            # variance of one observation is undefined, so this bin's whole
            # contribution to resolution stays in, uncorrected. Book it, do not
            # quietly treat the missing correction as a correction of zero.
            singleton_bins += 1
            singleton_events += nk
            uncorrected_resolution += nk * (ok - base) ** 2
            continue
        bias += nk * (ok * (1 - ok) / (nk - 1))
        # o(1-o)/(n-1) is the unbiased estimate of the variance of this bin's
        # mean, so numpy's ddof=1 variance is the same number by a different
        # route. Disagreement means the closed form above was mistyped.
        bias_variance_route += float(np.var(y[m], ddof=1))
    a = bias / n
    b = base * (1 - base) / (n - 1) if n > 1 else 0.0
    check_reliability /= n
    check_resolution /= n
    uncorrected_resolution /= n

    # The share is of the RAW RESOLUTION, not of the events, because those two
    # answer different questions: four singleton bins among 124 events are 3% of
    # the sample but were 80% of the spurious resolution.
    uncorrected_share = (
        uncorrected_resolution / raw.resolution if raw.resolution > 1e-15 else 0.0
    )
    applied = uncorrected_share <= FF_MAX_UNCORRECTED_RESOLUTION_SHARE

    # the same term leaves reliability and resolution, the same term enters
    # resolution and uncertainty, so brier = rel - res + unc still holds
    rel_c = raw.reliability - a if applied else float("nan")
    res_c = raw.resolution - a + b if applied else float("nan")
    unc_c = raw.uncertainty + b            # b does not depend on the bins at all

    note = None
    if not applied:
        note = (
            f"corrected figures withheld: {singleton_bins} bin(s) holding one event each "
            f"account for {uncorrected_share:.0%} of the raw resolution, and the "
            f"Ferro-Fricker term is undefined there because it divides by n_bin - 1. "
            f"Reporting the remainder as 'corrected' would overstate what was removed."
        )
    elif singleton_bins:
        note = (
            f"{singleton_bins} bin(s) holding one event each were left uncorrected; "
            f"they carry {uncorrected_share:.0%} of the raw resolution and "
            f"{singleton_events / n:.1%} of the events."
        )

    # These checks can FAIL. The obvious one - brier == rel - res + unc after
    # correction - cannot: a and b cancel out of that expression for ANY value of
    # a and b, so it merely re-derives murphy()'s own identity and is blind to a
    # wrong bias term. It was reported as True while the bias term was wrong by a
    # factor of two. It is kept below as part of `score_reproduced`, measured
    # against a Brier recomputed here from the definition, because it still
    # catches a broken murphy - but the two checks that guard THIS function are
    # the other two, and both of them fire on the bugs found in it.
    checks = {
        "bins_match_murphy": (
            abs(check_reliability - raw.reliability) < 1e-12
            and abs(check_resolution - raw.resolution) < 1e-12
        ),
        "bias_term_reproduced": abs(bias - bias_variance_route) < 1e-12,
        "score_reproduced": (
            abs(float(np.mean((p - y) ** 2)) - (raw.brier + raw.binning_gap)) < 1e-9
            and (not applied or abs(raw.brier - (rel_c - res_c + unc_c)) < 1e-9)
        ),
    }
    return {
        "n": n, "n_bins": n_bins,
        "singleton_bins": singleton_bins,
        "singleton_events": singleton_events,
        "singleton_event_share": singleton_events / n,
        "uncorrected_resolution_share": uncorrected_share,
        "correction_applied": applied,
        "note": note,
        "brier": raw.brier,
        "reliability_raw": raw.reliability, "reliability_corrected": rel_c,
        "resolution_raw": raw.resolution, "resolution_corrected": res_c,
        "uncertainty_raw": raw.uncertainty, "uncertainty_corrected": unc_c,
        "checks": checks,
        "identity_holds": all(checks.values()),
    }


# --------------------------------------------------------------------------- #
# 2. CORP (Dimitriadis, Gneiting & Jordan, 2021): no bins at all
# --------------------------------------------------------------------------- #
def _pav(values: np.ndarray, weights=None) -> np.ndarray:
    """Pool adjacent violators: the closest non-decreasing sequence to `values`.

    Walk left to right; wherever a value dips below its predecessor, merge the
    two into their average and step back to check the merge did not create a new
    dip. The result is the isotonic (never-decreasing) fit.

    `weights` says how many original observations each entry stands for, so a
    block already pooled from ten events outweighs a lone one when the two merge.
    corp() uses it to pass one entry per DISTINCT forecast value; see there for
    why that has to happen before the fit rather than inside it.
    """
    val = np.asarray(values, dtype=float).copy()
    weight = np.ones(len(val)) if weights is None else np.asarray(weights, dtype=float).copy()
    starts = list(range(len(val)))
    i = 0
    while i < len(val) - 1:
        if val[i] <= val[i + 1] + 1e-15:
            i += 1
            continue
        total = weight[i] + weight[i + 1]
        val[i] = (val[i] * weight[i] + val[i + 1] * weight[i + 1]) / total
        weight[i] = total
        val = np.delete(val, i + 1)
        weight = np.delete(weight, i + 1)
        starts.pop(i + 1)
        if i > 0:
            i -= 1
    out = np.empty(len(values))
    for b, start in enumerate(starts):
        end = starts[b + 1] if b + 1 < len(starts) else len(values)
        out[start:end] = val[b]
    return out


def corp(p, y) -> dict:
    """Decompose without choosing any bins.

    Sort the forecasts and find the best non-decreasing recalibration of them by
    isotonic regression. That curve is what the forecasts "should" have said.
    Three Brier scores then give the three parts directly:

        miscalibration = own score − recalibrated score   (what dishonesty cost)
        discrimination = base-rate score − recalibrated   (what the ranking was worth)
        uncertainty    = base-rate score                  (the task's difficulty)

    Because no bin width is chosen anywhere, the result cannot be an artefact of
    that choice — which is the objection the binned decomposition is open to.

        LIMITATION: `discrimination` has a positive floor. It is not zero for a
        forecast that knows nothing.
        ------------------------------------------------------------------------
        The isotonic curve is fitted on the same outcomes it is then scored
        against, so it is the best recalibration IN HINDSIGHT and flatters the
        forecast a little by construction - the same kind of artefact that
        ferro_fricker() exists to remove from the binned decomposition.

        Measured on this project's own data with forecasts drawn INDEPENDENTLY of
        the outcomes, where the true discrimination is exactly zero:

            n = 883, answers on the 0.05 grid   mean 0.00045, positive in 86% of draws
            n = 883, continuous answers         mean 0.00180, positive in 100%
            n = 100, answers on the 0.05 grid   mean 0.00384, positive in 85%

        The committed Murphy resolution for the logistic baseline is 0.00057, so
        on a per-structure slice of about a hundred events the no-information
        floor is roughly seven times the effect being looked for. A CORP
        discrimination below the floor for its own sample size is not evidence of
        anything. The returned dict carries that warning in `caveat` so it
        travels with the number instead of living only in this docstring.

        This is a known property of in-sample CORP rather than a coding error,
        and it is deliberately NOT corrected here: cross-fitting the isotonic
        curve would change the instrument the analysis plan names, which is a
        methodological decision and not a bug fix.
    """
    p = metrics.canonical(p)
    y = np.asarray(y, float)
    n = len(p)

    # Pool TIED forecasts into one block BEFORE fitting. Isotonic regression fits
    # a function OF the forecast, so two events given the same number must come
    # back with the same recalibrated number - otherwise the answer depends on
    # which of them the database happened to return first.
    #
    # Running the fit on y sorted by p, as this used to, does not do that:
    # pool-adjacent-violators only merges on a DECREASE, so a run of tied
    # forecasts whose outcomes happen to arrive in 0,0,1,1 order is left
    # untouched and every event is "recalibrated" to its own outcome. A CONSTANT
    # forecast was then credited with discrimination 0.00058 on the real
    # base-rate baseline - the same size as the Murphy resolution the thesis is
    # trying to detect - and reversing the row order of the identical data
    # changed the answer to 0. These models answer on a 0.05 grid and one of the
    # two baselines is constant, so ties are the normal case here, not a corner.
    #
    # One entry per distinct forecast value, weighted by how many events share
    # it, makes the fit a function of the data alone.
    values, inverse = np.unique(p, return_inverse=True)
    counts = np.bincount(inverse, minlength=len(values)).astype(float)
    group_mean = np.bincount(inverse, weights=y, minlength=len(values)) / counts
    recal = _pav(group_mean, counts)[inverse]
    base = float(np.mean(y))

    bs, bs_recal, bs_base = brier(p, y), brier(recal, y), brier(np.full(n, base), y)
    miscalibration = bs - bs_recal
    discrimination = bs_base - bs_recal

    # The old self-check was
    #     abs(bs - ((bs - bs_recal) - (bs_base - bs_recal) + bs_base)).
    # Every bs_recal and every bs_base cancels, leaving abs(bs - bs) = 0: it
    # returned True for a "recalibration" of all zeros, so it could not fail for
    # any input and validated nothing.
    #
    # These can fail, because each states a property a correct isotonic fit must
    # have, read off the fit itself rather than off the arithmetic that produced
    # the components:
    #   * the recalibrated curve never goes down as the forecast goes up;
    #   * equal forecasts get equal recalibrated values (the tie bug above);
    #   * the fit is the best isotonic function of p on this sample, and both p
    #     itself and the constant base rate ARE isotonic functions of p, so it
    #     cannot score worse than either - which makes both components
    #     non-negative, and both went negative under the broken fit.
    order = np.argsort(p, kind="mergesort")
    tie_spread = max(
        (float(recal[p == v].max() - recal[p == v].min()) for v in values), default=0.0
    )
    checks = {
        "recal_non_decreasing": bool(np.all(np.diff(recal[order]) >= -1e-12)),
        "recal_equal_on_tied_forecasts": tie_spread < 1e-12,
        "miscalibration_non_negative": miscalibration >= -1e-12,
        "discrimination_non_negative": discrimination >= -1e-12,
        "score_reproduced": abs(
            float(np.mean((p - y) ** 2)) - (miscalibration - discrimination + bs_base)
        ) < 1e-12,
    }
    return {
        "n": n, "brier": bs,
        "miscalibration": miscalibration,
        "discrimination": discrimination,
        "uncertainty": bs_base,
        "discrimination_is_in_sample": True,
        "caveat": (
            "discrimination is fitted and scored on the same outcomes, so it has a "
            "positive floor even for a forecast that knows nothing: about 0.0005 at "
            "n=883 on a 0.05 answer grid and about 0.004 at n=100. Compare against "
            "that floor, not against zero."
        ),
        "checks": checks,
        "identity_holds": all(checks.values()),
    }


# --------------------------------------------------------------------------- #
# 3. Block bootstrap over calendar weeks
# --------------------------------------------------------------------------- #
# A bootstrap over clusters can only be as rich as the number of clusters it has
# to draw from. With W distinct weeks there are at most C(2W-1, W) different
# resamples: one week gives exactly ONE, so every draw recomputes the identical
# statistic on the identical sample and the "95% interval" comes back with width
# exactly zero. Printed next to the words "95% CI" that claims perfect precision
# from fifty events, which is the opposite of what the data supports, so below
# two weeks the bounds are NaN and `ci_defined` is False rather than quietly
# wrong. Two weeks give three possible resamples, three weeks give ten.
#
# Between two weeks and the second threshold the interval exists but is a short
# lattice of possible values rather than a smooth distribution, so it is returned
# WITH a note saying how many distinct resamples it rests on. Twenty is the usual
# rule-of-thumb minimum cluster count for a cluster bootstrap; the demo has 23,
# and the risk is per-subgroup slices, where one structure in one reporting week
# can fall to one or two.
MIN_WEEKS_FOR_CI = 2
FEW_CLUSTERS_WARNING_BELOW = 20


def week_bootstrap(p, y, weeks, statistic=None, n_boot: int = 2000, seed: int = 0) -> dict:
    """Confidence interval that resamples whole WEEKS rather than single events.

    Earnings cluster into a few weeks a quarter, and every company reporting in
    the same week faces the same market conditions. Treating the events as
    independent would make the interval too narrow and the results look more
    certain than the data supports.

    Resampling whole weeks keeps those events together, so the interval reflects
    that a week is closer to one piece of evidence than to fifty.

    With fewer than `MIN_WEEKS_FOR_CI` weeks there is no interval to compute: the
    bounds come back NaN, `ci_defined` is False and `ci_note` says why.
    """
    statistic = statistic or brier
    rng = np.random.default_rng(seed)
    p = np.asarray(p, float)
    y = np.asarray(y, float)
    weeks = np.asarray(weeks)
    unique_weeks = np.unique(weeks)
    n_weeks = len(unique_weeks)
    index_by_week = {w: np.flatnonzero(weeks == w) for w in unique_weeks}

    out = {
        "point": float(statistic(p, y)),
        "n_events": len(p), "n_weeks": n_weeks, "n_boot": n_boot,
        "ci_defined": True, "ci_note": None,
    }

    if n_weeks < MIN_WEEKS_FOR_CI:
        out.update({
            "mean": float("nan"),
            "ci_low": float("nan"), "ci_high": float("nan"),
            "ci_defined": False,
            "ci_note": (
                f"no interval: all {len(p)} events fall in {n_weeks} week(s), so every "
                f"resample is the same sample. The width would be exactly zero, which "
                f"reads as perfect precision rather than as no information about it."
            ),
        })
        return out

    draws = np.empty(n_boot)
    for b in range(n_boot):
        chosen = rng.choice(unique_weeks, size=n_weeks, replace=True)
        idx = np.concatenate([index_by_week[w] for w in chosen])
        draws[b] = statistic(p[idx], y[idx])
    lo, hi = np.percentile(draws, [2.5, 97.5])

    if n_weeks < FEW_CLUSTERS_WARNING_BELOW:
        out["ci_note"] = (
            f"only {n_weeks} weeks: the bootstrap has at most "
            f"{math.comb(2 * n_weeks - 1, n_weeks)} distinct resamples to draw from, so "
            f"this is a coarse lattice of values rather than a smooth interval. Read it "
            f"as an order of magnitude, not as a bound."
        )
    out.update({
        "mean": float(draws.mean()),
        "ci_low": float(lo), "ci_high": float(hi),
    })
    return out


def iso_week_label(day: str) -> str:
    """The ISO year and week a date belongs to, e.g. '2026-W01'.

    Both halves have to come from isocalendar(). Pairing the CALENDAR year with
    the ISO week number, as this used to, is wrong at every year boundary: 29 Dec
    2025 is a Monday in ISO week 1 of ISO year 2026, so it was labelled
    '2025-W01' - the same label as 2 Jan 2025, an event fifty-one weeks earlier,
    which put the two in one bootstrap cluster. At the same time 29 Dec 2025 and
    2 Jan 2026, which genuinely ARE the same working week, were split into two.
    Both mistakes misstate the correlation structure the block bootstrap exists
    to capture, and both are silent.
    """
    iso = datetime.date.fromisoformat(str(day)[:10]).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_labels_for(event_ids) -> list[str]:
    """Recover the calendar week of each event, so the bootstrap stays possible.

    Event ids look like SYMBOL_YYYY-MM-DD[_Lx], which is enough to look the event
    up and read its prediction date. Nothing extra has to be stored during the
    run for this to work later — which is the point: the option stays open.
    """
    con = sqlite3.connect(DB)
    lookup = {
        f"{sym}_{rep}": t0
        for sym, rep, t0 in con.execute("SELECT symbol, report_date, t0 FROM events")
    }
    out = []
    for eid in event_ids:
        key = str(eid).split("_L")[0]
        t0 = lookup.get(key)
        out.append("unknown" if t0 is None else iso_week_label(t0))
    return out


# --------------------------------------------------------------------------- #
def self_test() -> None:
    ok = True
    rng = np.random.default_rng(11)

    # FF: a forecaster with NO skill, in thin bins, shows spurious resolution.
    # The correction must pull it towards zero.
    n = 120
    p = rng.choice([0.35, 0.45, 0.55, 0.65], size=n)          # unrelated to outcome
    y = (rng.uniform(size=n) < 0.46).astype(float)
    ff = ferro_fricker(p, y, n_bins=10)
    print(f"  no-skill forecaster, 10 thin bins:")
    print(f"     resolution raw {ff['resolution_raw']:.5f} -> corrected {ff['resolution_corrected']:.5f}")
    if not ff["correction_applied"] or ff["resolution_corrected"] >= ff["resolution_raw"]:
        print("     CORRECTION DID NOT REDUCE THE SPURIOUS RESOLUTION"); ok = False
    if not ff["identity_holds"]:
        print("     IDENTITY BROKEN BY THE CORRECTION"); ok = False

    # FF has to correct the SAME bins murphy() scored. Worked by hand: eight
    # events at 0.55 (murphy bin 5, four of them up) and eight at 0.60 (bin 6,
    # two up).
    #     base rate      6/16 = 0.375
    #     reliability    [8*(0.55-0.50)^2 + 8*(0.60-0.25)^2] / 16 = 0.0625
    #     resolution     [8*(0.50-0.375)^2 + 8*(0.25-0.375)^2] / 16 = 0.015625
    #     a = [8*0.50*0.50/7 + 8*0.25*0.75/7] / 16 = 0.5/16 = 0.03125
    #     b = 0.375*0.625/15 = 0.015625
    # so reliability_corrected = 0.0625 - 0.03125 = 0.03125 and
    # resolution_corrected = 0.015625 - 0.03125 + 0.015625 = 0 exactly.
    # With the old unrounded linspace edges 0.60 fell into bin 5, all sixteen
    # events pooled into ONE bin, a halved to 0.015625, and the corrected
    # resolution came back 0.015625 instead of 0.
    p_edge = np.array([0.55] * 8 + [0.60] * 8)
    y_edge = np.array([1., 1., 1., 1., 0., 0., 0., 0., 1., 1., 0., 0., 0., 0., 0., 0.])
    ff_edge = ferro_fricker(p_edge, y_edge, n_bins=10)
    print(f"  forecasts sitting on the 0.60 bin edge: reliability_corrected "
          f"{ff_edge['reliability_corrected']:.6f} (must be 0.031250), resolution_corrected "
          f"{ff_edge['resolution_corrected']:.6f} (must be 0.000000)")
    if (abs(ff_edge["reliability_corrected"] - 0.03125) > 1e-12
            or abs(ff_edge["resolution_corrected"]) > 1e-12):
        print("     FF IS CORRECTING A DIFFERENT PARTITION THAN MURPHY SCORED"); ok = False

    # ...and the guard protecting that must be able to fail. Bin the same data
    # with the old unrounded edges and `bins_match_murphy` has to go False.
    saved_bins = globals()["_murphy_bins"]
    try:
        globals()["_murphy_bins"] = lambda pp, nb: np.clip(
            np.digitize(np.asarray(pp, float), np.linspace(0.0, 1.0, nb + 1)[1:-1]), 0, nb - 1)
        sabotaged = ferro_fricker(p_edge, y_edge, n_bins=10)
    finally:
        globals()["_murphy_bins"] = saved_bins
    print(f"  same data binned the old way: bins_match_murphy "
          f"{sabotaged['checks']['bins_match_murphy']} (must be False)")
    if sabotaged["checks"]["bins_match_murphy"] or sabotaged["identity_holds"]:
        print("     THE BINNING GUARD CANNOT FAIL, SO IT CHECKS NOTHING"); ok = False

    # FF must refuse when singleton bins carry the resolution. 120 events split
    # evenly over two fat bins (30 of 60 up in each) plus four tail bins holding
    # one event each, two up and two down. Base rate = 62/124 = 0.5, so both fat
    # bins sit exactly ON the base rate and contribute nothing: the entire raw
    # resolution is 4 * 1 * (1 - 0.5)^2 / 124 = 0.00806452, and ALL of it comes
    # from bins where the correction would divide by zero.
    p_sing = np.concatenate([np.full(60, 0.45), np.full(60, 0.55),
                             np.array([0.05, 0.15, 0.85, 0.95])])
    y_sing = np.concatenate([np.array([1.] * 30 + [0.] * 30),
                             np.array([1.] * 30 + [0.] * 30),
                             np.array([1., 0., 1., 0.])])
    ff_sing = ferro_fricker(p_sing, y_sing, n_bins=10)
    print(f"  4 singleton bins among 124 events: raw resolution "
          f"{ff_sing['resolution_raw']:.8f} (must be 0.00806452), "
          f"{ff_sing['uncorrected_resolution_share']:.0%} of it uncorrectable (must be 100%), "
          f"correction_applied {ff_sing['correction_applied']} (must be False)")
    if (abs(ff_sing["resolution_raw"] - 0.00806452) > 1e-8
            or abs(ff_sing["uncorrected_resolution_share"] - 1.0) > 1e-12
            or ff_sing["singleton_bins"] != 4
            or ff_sing["correction_applied"]
            or not np.isnan(ff_sing["resolution_corrected"])):
        print("     SINGLETON BINS WERE SILENTLY REPORTED AS CORRECTED"); ok = False

    # CORP: on well-calibrated data, miscalibration small and discrimination large
    p2 = rng.uniform(0.05, 0.95, 400)
    y2 = (rng.uniform(size=400) < p2).astype(float)
    c = corp(p2, y2)
    print(f"  calibrated data, CORP: miscalibration {c['miscalibration']:.4f} (small), "
          f"discrimination {c['discrimination']:.4f} (large)")
    if not c["identity_holds"]:
        print("     CORP IDENTITY BROKEN"); ok = False
    if c["miscalibration"] > 0.02:
        print("     MISCALIBRATION UNEXPECTEDLY LARGE"); ok = False

    # CORP on a CONSTANT forecast. All hundred events share one covariate value,
    # so isotonic regression has a single pooled block and its fit is the mean of
    # the outcomes - which IS the base-rate forecast. Both scores are therefore
    # the same number, and miscalibration = discrimination = 0 exactly: a
    # forecast that says the same thing every time ranks nothing. And because the
    # answer is a property of the data, reversing the row order cannot move it.
    y_half = np.array([0.] * 50 + [1.] * 50)
    c_const = corp(np.full(100, 0.5), y_half)
    c_rev = corp(np.full(100, 0.5), y_half[::-1])
    print(f"  constant forecast 0.5 over fifty 0s then fifty 1s: miscalibration "
          f"{c_const['miscalibration']:.6f}, discrimination {c_const['discrimination']:.6f} "
          f"(both must be exactly 0); reversed row order gives "
          f"{c_rev['miscalibration']:.6f} and {c_rev['discrimination']:.6f}")
    if (c_const["miscalibration"] != 0.0 or c_const["discrimination"] != 0.0
            or c_rev["miscalibration"] != 0.0 or c_rev["discrimination"] != 0.0):
        print("     A CONSTANT FORECAST WAS CREDITED WITH DISCRIMINATION"); ok = False

    # The CORP self-check must be capable of failing. Replace the isotonic fit
    # with all zeros: that is not a recalibration of anything and it scores WORSE
    # than the base rate, so miscalibration and discrimination both go negative,
    # which a correct CORP can never do. The old tautological check said True.
    saved_pav = globals()["_pav"]
    try:
        globals()["_pav"] = lambda values, weights=None: np.zeros(len(values))
        c_bad = corp(np.array([0.2, 0.4, 0.6, 0.8]), np.array([0., 1., 0., 1.]))
    finally:
        globals()["_pav"] = saved_pav
    print(f"  deliberately broken isotonic fit: miscalibration {c_bad['miscalibration']:.2f}, "
          f"discrimination {c_bad['discrimination']:.2f} (both negative, impossible), "
          f"identity_holds {c_bad['identity_holds']} (must be False)")
    if c_bad["identity_holds"]:
        print("     THE CORP SELF-CHECK CANNOT FAIL, SO IT CHECKS NOTHING"); ok = False

    # bootstrap: clustered intervals must be WIDER than pretending independence
    # 20 weeks x 20 events, with BOTH the forecast and the outcome decided per
    # week. That is perfect intra-week correlation: the twenty events in a week
    # carry one piece of information between them, not twenty. It is the extreme
    # form of the situation clustering exists for, so the effect is unmistakable.
    # Theory says the clustered interval should be about sqrt(20) times wider,
    # because the effective sample is 20 weeks rather than 400 events.
    weeks = np.repeat(np.arange(20), 20)
    p3 = np.repeat(rng.uniform(0.2, 0.8, 20), 20)
    y3 = np.repeat((rng.uniform(size=20) < 0.5).astype(float), 20)
    clustered = week_bootstrap(p3, y3, weeks, n_boot=400)
    naive = week_bootstrap(p3, y3, np.arange(400), n_boot=400)     # every event its own "week"
    wc = clustered["ci_high"] - clustered["ci_low"]
    wn = naive["ci_high"] - naive["ci_low"]
    print(f"  bootstrap width: clustered {wc:.4f} vs treating events as independent {wn:.4f}"
          f"  ({wc/max(wn,1e-9):.1f}x wider)")
    if wc <= wn:
        print("     CLUSTERING SHOULD WIDEN THE INTERVAL"); ok = False

    # One week is not a bootstrap. Fifty events that all report in the same week
    # give exactly one possible resample, so the honest answer is "no interval",
    # not an interval of width zero. Two weeks give three possible resamples
    # (AA, AB, BB) and must carry that warning.
    p4 = rng.uniform(0.2, 0.8, 50)
    y4 = (rng.uniform(size=50) < 0.5).astype(float)
    one = week_bootstrap(p4, y4, np.array(["2025-W23"] * 50), n_boot=200)
    two = week_bootstrap(p4, y4, np.array(["2025-W23"] * 25 + ["2025-W24"] * 25), n_boot=200)
    print(f"  50 events in ONE week: ci_defined {one['ci_defined']} (must be False), "
          f"bounds [{one['ci_low']}, {one['ci_high']}] (must be nan, not a zero-width interval)")
    if one["ci_defined"] or not (np.isnan(one["ci_low"]) and np.isnan(one["ci_high"])):
        print("     A ZERO-WIDTH INTERVAL WAS REPORTED AS A 95% CI"); ok = False
    if two["ci_note"] is None or "3 distinct resamples" not in two["ci_note"]:
        print("     TWO CLUSTERS SHOULD BE FLAGGED AS A THREE-POINT LATTICE"); ok = False

    # ISO week labels across a year boundary. 29 and 31 Dec 2025 are the Monday
    # and Wednesday of the week ISO calls week 1 of 2026, and 2 Jan 2026 is the
    # Friday of that SAME week: one cluster of three. 2 Jan 2025 (a Thursday) and
    # 5 Jan 2025 (the Sunday) are week 1 of 2025; the Monday after starts week 2.
    dates = ["2025-12-29", "2025-12-31", "2026-01-02", "2025-01-02", "2025-01-05", "2025-01-06"]
    expected = ["2026-W01", "2026-W01", "2026-W01", "2025-W01", "2025-W01", "2025-W02"]
    got = [iso_week_label(d) for d in dates]
    print(f"  week labels across a year boundary {dates}")
    print(f"     ->       {got}")
    print(f"     must be  {expected}")
    if got != expected:
        print("     WEEK LABELS DO NOT FOLLOW THE ISO YEAR"); ok = False

    print("\nSELF-TEST", "PASSED" if ok else "FAILED")


def demo() -> None:
    """Apply all three to the real baselines, to show the option works end to end."""
    con = sqlite3.connect(DB)
    rows = con.execute(
        """SELECT eps_surprise_pct, label, t0 FROM v_level1
           WHERE t0 > '2025-04-28' AND in_index_at_t0 = 1
             AND eps_surprise_pct IS NOT NULL AND label IS NOT NULL ORDER BY t0"""
    ).fetchall()
    x = np.clip(np.array([r[0] for r in rows], float), -200, 200)
    y = np.array([r[1] for r in rows], float)
    wk = np.array([iso_week_label(r[2]) for r in rows])
    n, cut = len(y), int(len(y) * 0.6)
    yt, wt = y[cut:], wk[cut:]

    print(f"{n} eligible events; fitted on {cut}, scored on {n-cut}\n")
    for name, pred in [
        ("base rate", metrics.base_rate_forecast(y[:cut], n - cut)),
        ("logistic on surprise", metrics.logistic_surprise(x[:cut], y[:cut], x[cut:])),
    ]:
        committed = metrics.murphy(pred, yt)
        ff = ferro_fricker(pred, yt)
        c = corp(pred, yt)
        bs = week_bootstrap(pred, yt, wt, n_boot=400)
        print(f"  {name}")
        print(f"     COMMITTED   brier {committed.brier_raw:.4f}  reliability {committed.reliability:.4f}  resolution {committed.resolution:.5f}")
        print(f"     +Ferro-Fricker             reliability {ff['reliability_corrected']:.4f}  resolution {ff['resolution_corrected']:.5f}")
        if ff["note"]:
            print(f"        note: {ff['note']}")
        print(f"     +CORP (no bins)         miscalibration {c['miscalibration']:.4f}  discrimination {c['discrimination']:.5f}")
        print(f"        caveat: {c['caveat']}")
        print(f"     +week bootstrap   95% CI [{bs['ci_low']:.4f}, {bs['ci_high']:.4f}] over {bs['n_weeks']} weeks")
        if bs["ci_note"]:
            print(f"        note: {bs['ci_note']}")
        print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    demo() if a.demo else self_test()
