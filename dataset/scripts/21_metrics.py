"""Scoring for the forecasts: Brier score, Murphy decomposition, and the baselines.

Implements working document sections 6 and 7, restricted to what the exposé
actually commits to:

    Brier score                       (exposé §3, Brier 1950)
    Murphy decomposition              (exposé §3, Murphy 1973)
    base-rate baseline                (exposé §4)
    logistic regression on surprise   (exposé §4)

The compute-matched repetition rung — the third and most important comparison —
is not computed here: it comes out of the runner, which pairs each structure with
the baseline drawn at its own token budget (see `pilot/structures5.ts`).

Three further instruments are deliberately NOT part of this module and NOT part
of the committed analysis: the Ferro-Fricker bias correction, the bin-free CORP
decomposition, and the calendar-week block bootstrap. They live in
`22_robustness_optional.py` as optional post-hoc checks, and nothing here imports
them. What they guard against is stated as a limitation instead — see
`scaffold/METRICS_EXPLAINED.md`.

Only numpy is required.

    python scripts/21_metrics.py --self-test
    python scripts/21_metrics.py --baselines
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DB = Path(__file__).resolve().parents[1] / "thesis.db"

# Fixed in advance (working doc §6.4/§6.5) and never adjusted afterwards.
#
# TEN, and the reason it must be an EVEN number is the important part. 0.50 is
# the midpoint of the probability range, so an odd bin count puts 0.50 in the
# MIDDLE of a bin and forecasts of 0.45 and 0.55 land in the same one. That
# destroys the single most meaningful distinction these models make - leaning
# negative versus leaning positive - which is precisely what resolution is meant
# to detect. An even count places 0.50 on a bin edge and separates them.
#
# Ten rather than six or eight: measured on this project's own data, ten leaves
# four populated bins across the observed forecast range, holding roughly 75 to
# 825 events each at full scale, whereas six is badly lopsided. The extra bins
# cost almost nothing in bias - on a baseline with no real skill the apparent
# resolution was 0.00055 at five bins and 0.00057 at ten, against 0.00111 at
# twenty. Ten is also the conventional choice and the example already used in
# working document §6.4.
N_BINS = 10

# Forecasts are rounded to this many decimals before anything is grouped or
# scored. This is not cosmetic - it is the same class of bug as the bin edges
# below, one layer earlier.
#
# The multi-agent structures do not report a parsed number; they report an
# ARITHMETIC MEAN of their agents' answers, computed in floating point. Three
# agents who all say 0.7 produce 0.6999999999999998, which then falls one bin
# BELOW the 0.7 edge - the exact failure the edge-rounding was written to stop,
# reintroduced upstream where that fix could not reach. Worse, the sum depends
# on the order the answers arrived in, so 26% of three-agent combinations gave a
# different double depending on which worker replied first: the recorded
# forecast was not a function of the agents' answers alone.
#
# In `n_bins=None` mode the damage is larger still, because grouping is by
# DISTINCT value: mathematically identical forecasts split into separate groups
# and manufacture resolution out of float noise (measured: +42.8%).
#
# Nine decimals is far below anything a forecast can mean and far above the
# 1e-16 noise being removed. `pilot/structures5.ts` canonicalises with the same
# rule at the point the mean is formed, so the two sides agree.
FORECAST_DECIMALS = 9


def canonical(p) -> np.ndarray:
    """Round forecasts to `FORECAST_DECIMALS` so equal forecasts are equal."""
    return np.round(np.asarray(p, dtype=float), FORECAST_DECIMALS)


def _checked(p, y) -> tuple[np.ndarray, np.ndarray]:
    """Canonicalise, and refuse input that would score silently wrong.

    Every rejection here corresponds to something that produced a plausible
    wrong number before: numpy BROADCASTS a length-5 forecast against a
    length-1 outcome instead of complaining, a probability above 1 scores
    happily, and a single NaN forecast turns a whole column into NaN (in
    `n_bins=None` mode it did worse - `p == nan` matches nothing, so that
    element kept whatever was in uninitialised memory).
    """
    p = canonical(p)
    y = np.asarray(y, dtype=float)
    if p.shape != y.shape:
        raise ValueError(f"forecasts and outcomes differ in shape: {p.shape} vs {y.shape}")
    if p.size == 0:
        raise ValueError("no forecasts to score")
    if not np.all(np.isfinite(p)) or not np.all(np.isfinite(y)):
        raise ValueError("forecasts and outcomes must all be finite (no NaN, no inf)")
    if np.any(p < 0.0) or np.any(p > 1.0):
        raise ValueError(f"forecasts must lie in [0, 1]; got min {p.min()} max {p.max()}")
    if not np.all((y == 0.0) | (y == 1.0)):
        raise ValueError("outcomes must be 0 or 1")
    return p, y


# --------------------------------------------------------------------------- #
# 1. Brier score
# --------------------------------------------------------------------------- #
def brier(p, y) -> float:
    """Mean squared distance between the forecast and what happened.

    p: forecast probabilities, each between 0 and 1
    y: outcomes, 1 if the event happened and 0 if it did not

    Lower is better. A forecaster who always says 0.5 scores exactly 0.25, which
    is the no-information mark any useful system has to beat.
    """
    p, y = _checked(p, y)
    return float(np.mean((p - y) ** 2))


# --------------------------------------------------------------------------- #
# 2. Murphy decomposition
# --------------------------------------------------------------------------- #
@dataclass
class Decomposition:
    """The Brier score split into its three parts.

    The identity  brier = reliability - resolution + uncertainty  holds exactly
    for `brier`, which is the score of the BINNED forecast: every forecast
    replaced by the average of its bin. Murphy's result is stated for a forecast
    that takes finitely many distinct values, so the binned forecast is the thing
    the identity actually describes.

    `brier_raw` is the score of the forecasts as they were really issued, and
    `binning_gap` is the difference between the two. It is reported rather than
    hidden, because it is the price of grouping forecasts into bins at all.
    """
    brier: float          # score of the binned forecast; the identity uses this
    reliability: float    # are the stated numbers honest?      lower is better
    resolution: float     # does it tell cases apart?           higher is better
    uncertainty: float    # how hard the task is; fixed by the data
    n: int
    n_bins: int
    empty_bins: int
    brier_raw: float
    binning_gap: float
    singleton_bins: int    # groups holding exactly one event - see below

    # A group of one is a warning sign, and only in `n_bins=None` mode is it
    # likely. Such a group's observed frequency is 0 or 1 by construction, so it
    # sits as far from the base rate as arithmetic allows and contributes the
    # maximum to resolution whether or not the forecast had any skill. Scored
    # this way a forecaster who says a different number every time is credited
    # with near-perfect discrimination: on the real base-rate baseline the
    # distinct-value mode returned resolution 0.2045 where the honest answer is
    # 0.00057. The number is not wrong - it is what Murphy's formula gives on
    # groups of one - but it is uninformative, so it must be visible.

    def identity_holds(self, tol: float = 1e-9) -> bool:
        return abs(self.brier - (self.reliability - self.resolution + self.uncertainty)) < tol


def murphy(p, y, n_bins: int | None = N_BINS) -> Decomposition:
    """Split the Brier score into reliability, resolution and uncertainty.

    n_bins = 10    group forecasts into 10 equal-width bins (0-0.1, 0.1-0.2, ...)
    n_bins = None  group by DISTINCT forecast value instead. This is Murphy's
                   original setting, it makes the binning gap exactly zero, and
                   it fits these models well because they answer on a coarse grid.
    """
    p, y = _checked(p, y)
    n = len(p)
    base_rate = float(np.mean(y))

    # ---- decide which forecasts belong together
    if n_bins is None:
        values = np.unique(p)
        groups = [p == v for v in values]
        n_bins_used = len(values)
    else:
        # Bins are half-open: [lower, upper). A forecast lying exactly ON a bin
        # boundary therefore belongs to the bin ABOVE it, which is np.digitize's
        # default (right=False). The rule is arbitrary but it must be applied
        # consistently, and that is what the rounding below guarantees.
        #
        # np.linspace does NOT produce exact edges: with 10 bins it returns
        # 0.6000000000000001 and 0.7000000000000001 rather than 0.6 and 0.7. A
        # forecast of exactly 0.6 then compares as SMALLER than "its" edge and
        # silently falls into the bin below, while 0.4 and 0.5 (which happen to
        # be exact in binary) go above. That would apply the rule inconsistently.
        #
        # This is not a corner case here: these models answer on multiples of
        # 0.05, so 0.40, 0.50, 0.60 and 0.70 all sit exactly on edges — roughly
        # half of every answer they give.
        edges = np.round(np.linspace(0.0, 1.0, n_bins + 1), 9)
        which_bin = np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)
        groups = [which_bin == k for k in range(n_bins)]
        n_bins_used = n_bins

    # ---- per bin: what was said, and what actually happened
    reliability = 0.0
    resolution = 0.0
    empty = 0
    singletons = 0
    p_binned = np.full(n, np.nan)

    for in_this_bin in groups:
        count = int(in_this_bin.sum())
        if count == 0:
            empty += 1
            continue
        if count == 1:
            singletons += 1
        said = float(p[in_this_bin].mean())        # average forecast in the bin
        happened = float(y[in_this_bin].mean())    # share that actually occurred
        p_binned[in_this_bin] = said

        # reliability: how far the claim sat from reality, weighted by bin size
        reliability += count * (said - happened) ** 2
        # resolution: how far this bin's reality sat from the overall base rate
        resolution += count * (happened - base_rate) ** 2

    reliability /= n
    resolution /= n
    uncertainty = base_rate * (1.0 - base_rate)

    return Decomposition(
        brier=brier(p_binned, y),
        reliability=reliability,
        resolution=resolution,
        uncertainty=uncertainty,
        n=n,
        n_bins=n_bins_used,
        empty_bins=empty,
        brier_raw=brier(p, y),
        binning_gap=brier(p, y) - brier(p_binned, y),
        singleton_bins=singletons,
    )


# --------------------------------------------------------------------------- #
# 3. Baselines
# --------------------------------------------------------------------------- #
def base_rate_forecast(train_y, n_test: int) -> np.ndarray:
    """Always predict how often the event happened historically.

    Uses no information at all, so a system that cannot beat it has found nothing.
    Its Brier score equals the uncertainty term of the decomposition.
    """
    return np.full(n_test, float(np.mean(train_y)))


def logistic_surprise(train_x, train_y, test_x) -> np.ndarray:
    """Logistic regression using the earnings surprise as the only input.

    Two rules fixed in advance (working doc §7.2):
      * the surprise is standardised, so a 30-cent miss means the same thing for
        a company earning $2 a share as for one earning $20;
      * one formula is fitted across all stocks together, because a single
        company reports four times a year and a per-stock fit would rest on
        almost no data.

    Fitted by Newton-Raphson, which is the standard way to fit a logistic
    regression: start from a guess, repeatedly step towards the best fit, stop
    when the steps become tiny. Written out here so the analysis needs no
    machine-learning library.
    """
    train_x = np.asarray(train_x, dtype=float)
    train_y = np.asarray(train_y, dtype=float)
    test_x = np.asarray(test_x, dtype=float)

    # standardise using the TRAINING data only, so the test set leaks nothing
    mean = float(train_x.mean())
    sd = float(train_x.std())
    sd = sd if sd > 1e-12 else 1.0

    # each row is [1, standardised surprise]; the 1 gives the model an intercept
    X_train = np.column_stack([np.ones(len(train_x)), (train_x - mean) / sd])
    X_test = np.column_stack([np.ones(len(test_x)), (test_x - mean) / sd])

    beta = np.zeros(2)                     # [intercept, slope], start at zero
    for _ in range(100):
        predicted = 1.0 / (1.0 + np.exp(-(X_train @ beta)))   # the S-curve
        weights = np.clip(predicted * (1 - predicted), 1e-9, None)
        gradient = X_train.T @ (train_y - predicted)
        hessian = X_train.T @ (X_train * weights[:, None]) + 1e-8 * np.eye(2)
        step = np.linalg.solve(hessian, gradient)
        beta += step
        if np.max(np.abs(step)) < 1e-10:
            break

    return 1.0 / (1.0 + np.exp(-(X_test @ beta)))


# --------------------------------------------------------------------------- #
def self_test() -> None:
    """Check the arithmetic against the worked examples in the working document."""
    ok = True

    # §6.2: saying 0.7 and being right costs 0.09; being wrong costs 0.49
    assert abs(brier([0.7], [1]) - 0.09) < 1e-12
    assert abs(brier([0.7], [0]) - 0.49) < 1e-12
    assert abs(brier([0.5] * 8, [1, 0] * 4) - 0.25) < 1e-12
    print("  Brier examples from §6.2: ok")

    rng = np.random.default_rng(7)
    p = rng.uniform(0.05, 0.95, 400)
    y = (rng.uniform(size=400) < p).astype(float)     # calibrated by construction

    for bins in (5, 10, None):
        d = murphy(p, y, n_bins=bins)
        label = "distinct values" if bins is None else f"{bins} bins"
        if not d.identity_holds(1e-8):
            print(f"  IDENTITY FAILS for {label}")
            ok = False
    print("  brier = reliability - resolution + uncertainty holds: ok")

    d = murphy(p, y, n_bins=None)
    print(f"  distinct-value binning leaves a gap of {d.binning_gap:.2e} (must be 0)")
    if abs(d.binning_gap) > 1e-12:
        print("  DISTINCT-VALUE BINNING SHOULD BE EXACT")
        ok = False

    d = murphy(p, y)
    print(f"  well-calibrated data -> reliability {d.reliability:.4f} (small), "
          f"resolution {d.resolution:.4f} (large)")

    # A forecast sitting exactly on a bin edge must go to the bin ABOVE, for
    # every edge alike. np.linspace alone gets this wrong at 0.6 and 0.7 because
    # those edges are not exactly representable, and half of these models'
    # answers land on edges, so the check is worth having.
    edge_values = [0.1 * k for k in range(1, 10)]
    got = [murphy(np.array([v]), np.array([1.0]), n_bins=10).n_bins for v in edge_values]
    edges_r = np.round(np.linspace(0.0, 1.0, 11), 9)
    placed = [int(np.clip(np.digitize([v], edges_r[1:-1]), 0, 9)[0]) for v in edge_values]
    expected = list(range(1, 10))
    print(f"  forecasts exactly on an edge {[round(v,1) for v in edge_values]}")
    print(f"     land in bins {placed} (each must be the bin ABOVE its edge: {expected})")
    if placed != expected:
        print("  EDGE HANDLING IS INCONSISTENT")
        ok = False

    # a constant forecast at the base rate: no resolution, and brier = uncertainty
    y_const = np.array([1.0] * 46 + [0.0] * 54)
    d = murphy(np.full(100, 0.46), y_const)
    print(f"  always-the-base-rate -> resolution {d.resolution:.6f} (must be 0), "
          f"brier {d.brier:.4f} = uncertainty {d.uncertainty:.4f}")
    if d.resolution > 1e-12:
        print("  RESOLUTION SHOULD BE ZERO")
        ok = False

    # the logistic baseline must find a signal that really is there
    xs = rng.normal(size=600)
    ys = (rng.uniform(size=600) < 1 / (1 + np.exp(-0.8 * xs))).astype(float)
    pred = logistic_surprise(xs[:400], ys[:400], xs[400:])
    b_log = brier(pred, ys[400:])
    b_base = brier(base_rate_forecast(ys[:400], 200), ys[400:])
    print(f"  logistic on a real signal: brier {b_log:.4f} vs base rate {b_base:.4f}")
    if b_log >= b_base:
        print("  WARNING: the logistic failed to beat the base rate")
        ok = False

    print("\nSELF-TEST", "PASSED" if ok else "FAILED")


def baselines() -> None:
    """Score both baselines on the eligible events in thesis.db."""
    con = sqlite3.connect(DB)
    rows = con.execute(
        """SELECT eps_surprise_pct, label FROM v_level1
           WHERE t0 > '2025-04-28' AND in_index_at_t0 = 1
             AND eps_surprise_pct IS NOT NULL AND label IS NOT NULL
           ORDER BY t0"""
    ).fetchall()

    x = np.clip(np.array([r[0] for r in rows], dtype=float), -200, 200)
    y = np.array([r[1] for r in rows], dtype=float)
    n = len(y)
    cut = int(n * 0.6)      # fit on the earlier events, score on the later ones

    print(f"{n} eligible events, base rate {y.mean():.4f}")
    print(f"fitted on the first {cut} in time order, scored on the remaining {n - cut}\n")

    y_test = y[cut:]
    for name, pred in [
        ("base rate", base_rate_forecast(y[:cut], n - cut)),
        ("logistic on surprise", logistic_surprise(x[:cut], y[:cut], x[cut:])),
    ]:
        d = murphy(pred, y_test)
        # `brier` (binned) is the quantity the three terms actually decompose;
        # printing `brier_raw` here made the table fail its own identity by 79%
        # of the resolution it reported. Both are shown, with the gap between.
        print(f"  {name:22} brier {d.brier:.4f} (raw {d.brier_raw:.4f}, "
              f"gap {d.binning_gap:+.5f})   reliability {d.reliability:.4f}   "
              f"resolution {d.resolution:.5f}   uncertainty {d.uncertainty:.4f}")
        assert d.identity_holds(), "Murphy identity broken in the baselines table"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--baselines", action="store_true")
    a = ap.parse_args()
    if a.baselines:
        baselines()
    else:
        self_test()


if __name__ == "__main__":
    main()
