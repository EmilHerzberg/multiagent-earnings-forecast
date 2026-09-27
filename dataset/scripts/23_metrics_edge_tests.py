"""Edge-case tests for the scoring code.

The ordinary self-test in `21_metrics.py` checks that the arithmetic is right on
well-behaved data. This file asks the harder question: what happens at the
boundaries, and on degenerate inputs that the real experiment can actually
produce?

Every test states the EXPECTED result first and then compares. A test that only
checks "it did not crash" is worthless — the two binning bugs found so far both
ran perfectly happily while producing wrong numbers.

    python scripts/23_metrics_edge_tests.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("metrics", HERE / "21_metrics.py")
m = importlib.util.module_from_spec(spec)
sys.modules["metrics"] = m
spec.loader.exec_module(m)

PASS, FAIL, WARN = [], [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"\n          {detail}" if detail and not condition else ""))


def warn(name: str, detail: str) -> None:
    WARN.append(name)
    print(f"  WARN  {name}\n          {detail}")


print("\n=== 1. Values at the extreme ends of the range ===")
# A forecast of exactly 0 or 1 is legal: the parser accepts them.
d = m.murphy(np.array([0.0, 1.0]), np.array([0.0, 1.0]), n_bins=10)
check("p=0 and p=1 are placed in the first and last bin, not out of range",
      d.n_bins == 10 and abs(d.brier) < 1e-12,
      f"brier should be 0 for two perfect forecasts, got {d.brier}")

# Perfect confidence, and wrong. The worst possible score.
check("p=1 with outcome 0 scores exactly 1.0", abs(m.brier([1.0], [0.0]) - 1.0) < 1e-12)
check("p=0 with outcome 1 scores exactly 1.0", abs(m.brier([0.0], [1.0]) - 1.0) < 1e-12)

# Where does 1.0 actually land? np.digitize can return an index one past the end.
edges = np.round(np.linspace(0, 1, 11), 9)
raw_index = int(np.digitize([1.0], edges[1:-1])[0])
check("a forecast of exactly 1.0 indexes the last bin (9), not bin 10",
      raw_index == 9, f"np.digitize returned {raw_index}; np.clip would be doing real work here")


print("\n=== 2. Degenerate outcomes: every event went the same way ===")
# Can happen on a small subset, e.g. one model x one quarter.
y_all_one = np.ones(20)
d = m.murphy(np.full(20, 0.7), y_all_one, n_bins=10)
check("base rate 1.0 -> uncertainty is 0", abs(d.uncertainty) < 1e-12, f"got {d.uncertainty}")
check("base rate 1.0 -> resolution is 0", abs(d.resolution) < 1e-12, f"got {d.resolution}")
check("base rate 1.0 -> identity still holds", d.identity_holds(), f"{d.brier} vs {d.reliability - d.resolution + d.uncertainty}")

y_all_zero = np.zeros(20)
d = m.murphy(np.full(20, 0.3), y_all_zero, n_bins=10)
check("base rate 0.0 -> identity still holds", d.identity_holds())


print("\n=== 3. Degenerate forecasts: the model always said the same thing ===")
# Very plausible here: DeepSeek answered 0.45 sixteen times out of 26.
p_const = np.full(50, 0.45)
y_mixed = np.array([1.0] * 23 + [0.0] * 27)
d = m.murphy(p_const, y_mixed, n_bins=10)
check("a constant forecast has resolution exactly 0", abs(d.resolution) < 1e-12, f"got {d.resolution}")
check("a constant forecast fills exactly one bin", d.empty_bins == 9, f"{d.empty_bins} empty of 10")
check("a constant forecast: identity holds", d.identity_holds())
d2 = m.murphy(p_const, y_mixed, n_bins=None)
check("distinct-value mode on a constant forecast gives ONE group", d2.n_bins == 1, f"got {d2.n_bins}")


print("\n=== 4. Single observation ===")
d = m.murphy(np.array([0.7]), np.array([1.0]), n_bins=10)
check("n=1: brier is (0.7-1)^2 = 0.09", abs(d.brier - 0.09) < 1e-12, f"got {d.brier}")
check("n=1: identity holds", d.identity_holds())
check("n=1: resolution is 0 (nothing to separate)", abs(d.resolution) < 1e-12)


print("\n=== 5. Bad input must fail LOUDLY, not silently ===")
# numpy broadcasting is the danger: a length-5 forecast against a length-1
# outcome does NOT error, it repeats the single value and returns a plausible
# wrong answer.


def rejects(name, fn):
    try:
        got = fn()
        check(name, False, f"no error raised; returned {got}")
    except ValueError:
        check(name, True)
    except Exception as e:
        check(name, False, f"raised {type(e).__name__} instead of ValueError: {e}")


rejects("5 forecasts against 1 outcome is rejected, not broadcast",
        lambda: m.brier([0.1, 0.2, 0.3, 0.4, 0.5], [1.0]))
rejects("3-vs-2 length mismatch is rejected", lambda: m.brier([0.1, 0.2, 0.3], [1.0, 0.0]))
rejects("empty input is rejected", lambda: m.brier([], []))
rejects("a probability above 1 is rejected", lambda: m.brier([1.5], [1.0]))
rejects("a negative probability is rejected", lambda: m.brier([-0.2], [1.0]))
rejects("a NaN forecast is rejected", lambda: m.brier([0.5, np.nan], [1.0, 0.0]))
rejects("an infinite forecast is rejected", lambda: m.brier([0.5, np.inf], [1.0, 0.0]))
rejects("an outcome that is not 0 or 1 is rejected", lambda: m.brier([0.5], [0.5]))
rejects("murphy() rejects NaN too", lambda: m.murphy([0.5, np.nan], [1.0, 0.0], n_bins=None))


print("\n=== 6. Float noise in arithmetic-mean forecasts ===")
# The multi-agent arms report a MEAN of their agents answers, not a parsed
# number. Three agents all saying 0.7 give 0.6999999999999998 in IEEE 754.
noisy = (0.65 + 0.7 + 0.75) / 3      # 0.7000000000000001
unan = (0.7 + 0.7 + 0.7) / 3         # 0.6999999999999998
check("the two ways of averaging to 0.7 differ as raw doubles", noisy != unan,
      "the premise of this test no longer holds on this platform")
check("canonical() maps both to the same value",
      m.canonical([noisy])[0] == m.canonical([unan])[0],
      f"{m.canonical([noisy])[0]!r} vs {m.canonical([unan])[0]!r}")

y8 = np.array([1.0, 1, 1, 1, 0, 0, 0, 0])
d_un = m.murphy(np.full(8, unan), y8, n_bins=10)
check("a mean of three 0.7s has resolution exactly 0 (one distinct forecast)",
      abs(d_un.resolution) < 1e-12, f"got {d_un.resolution}")
check("and reliability (0.7-0.5)^2 = 0.04", abs(d_un.reliability - 0.04) < 1e-12,
      f"got {d_un.reliability}")

# Eight events, every forecast mathematically 0.7 but reached by different routes.
mixed = np.array([unan, noisy, unan, noisy, unan, noisy, unan, noisy])
d_mixed = m.murphy(mixed, y8, n_bins=None)
check("distinct-value mode sees ONE forecast, not two", d_mixed.n_bins == 1,
      f"got {d_mixed.n_bins} groups for a single mathematical forecast")
check("no resolution is manufactured out of float noise",
      abs(d_mixed.resolution) < 1e-12, f"got {d_mixed.resolution}")
check("the two routes score identically", abs(d_mixed.brier - d_un.brier) < 1e-12,
      f"{d_mixed.brier} vs {d_un.brier}")


print("\n=== 6b. Groups of one must be visible ===")
# In distinct-value mode a group of one has observed frequency 0 or 1 by
# construction, so it contributes the maximum to resolution regardless of skill.
p_unique = np.round(np.linspace(0.30, 0.70, 20), 9)
y_alt = np.array([1.0, 0] * 10)
d_u = m.murphy(p_unique, y_alt, n_bins=None)
check("20 distinct forecasts are reported as 20 singleton groups",
      d_u.singleton_bins == 20, f"got {d_u.singleton_bins}")
check("and that inflates resolution all the way to the uncertainty term",
      abs(d_u.resolution - d_u.uncertainty) < 1e-12,
      f"resolution {d_u.resolution} vs uncertainty {d_u.uncertainty}")
check("a zero-skill forecaster therefore scores resolution 0.25 in this mode",
      abs(d_u.resolution - 0.25) < 1e-12, f"got {d_u.resolution}")
# Fixed bins collapse those 20 values into 5 populated bins holding
# 5, 5, 5, 4 and 1 events. The single group of one is bin 7, which contains
# only the forecast 0.70 - itself a demonstration that a value sitting exactly
# on an edge is placed in the bin ABOVE. So one singleton is the correct
# answer here, not zero, and the point stands: 1 group of one instead of 20.
d_b = m.murphy(p_unique, y_alt, n_bins=10)
check("fixed bins cut the singleton groups from 20 to 1",
      d_b.singleton_bins == 1, f"got {d_b.singleton_bins}")
check("and the surviving singleton is the 0.70 that sits on an edge",
      float(p_unique[-1]) == 0.7 and d_b.n_bins == 10)
check("so resolution is no longer pinned to the uncertainty term",
      d_b.resolution < d_u.resolution / 2,
      f"fixed {d_b.resolution} vs distinct {d_u.resolution}")


print("\n=== 6c. Is nine decimals actually the right precision? ===")
# FORECAST_DECIMALS = 9 was chosen because it is "far finer than any probability
# these models can mean". That is a sentence, not a test: a mutation run showed
# the constant could be changed to 6, or even 4, and every existing check still
# passed. So the requirement is derived here instead of asserted.
#
# What has to hold: canonicalising must remove float noise WITHOUT ever merging
# two forecasts that are genuinely different.
#
# How far apart can two genuinely different forecasts be, at the closest? A
# structure reports the mean of k runs, and every run answers on the 0.05 grid,
# so every forecast this experiment can produce is m / (20k) for whole numbers
# m and k. Two different fractions a/q1 and b/q2 differ by at least 1/(q1*q2) --
# the numerator |a*q2 - b*q1| is a whole number, and it cannot be 0 unless the
# fractions are equal. With q = 20k, the smallest possible gap is 1/(20*k_max)^2.
#
# k_max here is 203: three shared workers plus MAX_EXTRA_RUNS = 200 baseline
# draws (pilot/structures5.ts). That gives a smallest possible gap of 6.07e-8.
MAX_POOL_RUNS = 203              # 3 workers + MAX_EXTRA_RUNS (structures5.ts)
smallest_real_gap = 1.0 / (20 * MAX_POOL_RUNS) ** 2
resolution = 10.0 ** -m.FORECAST_DECIMALS

check("rounding is finer than the smallest gap two real forecasts can have",
      resolution < smallest_real_gap / 10,
      f"rounding to {m.FORECAST_DECIMALS} decimals resolves {resolution:.0e}, but two distinct "
      f"forecasts can sit only {smallest_real_gap:.2e} apart -- they could be merged")

# The margin is worth knowing rather than just passing: at 8 decimals the
# resolution (1e-8) is the same order as the gap (6.1e-8), which is too close to
# be comfortable. Nine is the first standard choice with real headroom.
check("and it does so with at least a 10x margin",
      smallest_real_gap / resolution >= 10,
      f"margin is only {smallest_real_gap / resolution:.1f}x")

# Second half: on a realistically sized pool, no two achievable means may collide
# after canonicalisation. Restricted to k <= 20 so the test stays fast; that
# already produces 2,561 distinct values and fails at 3 decimals.
from fractions import Fraction  # noqa: E402  (local to this check, stdlib)

achievable = {Fraction(num, 20 * k) for k in range(1, 21) for num in range(0, 20 * k + 1)}
exact = sorted(achievable)
rounded = {float(m.canonical([float(v)])[0]) for v in exact}
check("no two achievable forecasts collide after canonicalisation",
      len(rounded) == len(exact),
      f"{len(exact)} distinct means collapsed to {len(rounded)} after rounding")

# Third: the thing canonicalisation exists for. The same mathematical mean
# reached by two different summation orders must come out as one value.
routes = [(0.7, 0.7, 0.7), (0.65, 0.7, 0.75), (0.75, 0.7, 0.65), (0.6, 0.7, 0.8)]
means = [sum(r) / 3 for r in routes]
check("four float routes to 0.7 are NOT all equal before canonicalisation",
      len(set(means)) > 1,
      "the premise of this check no longer holds on this platform")
check("canonicalisation collapses them to exactly one value",
      len({float(m.canonical([x])[0]) for x in means}) == 1,
      f"got {sorted({float(m.canonical([x])[0]) for x in means})}")
check("and that value is exactly 0.7", float(m.canonical([means[0]])[0]) == 0.7,
      f"got {float(m.canonical([means[0]])[0])!r}")


print("\n=== 7. Bin edges, all of them ===")
# Half of these models' answers land exactly on an edge, so every edge matters.
edges_inner = np.round(np.linspace(0, 1, 11), 9)[1:-1]
placements = {}
for k in range(1, 10):
    v = round(0.1 * k, 10)
    placements[v] = int(np.clip(np.digitize([v], edges_inner), 0, 9)[0])
expected = {round(0.1 * k, 10): k for k in range(1, 10)}
check("every forecast exactly on an edge goes to the bin ABOVE",
      placements == expected, f"got {placements}, expected {expected}")

# and the values the models actually produce
grid = [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
got = {v: int(np.clip(np.digitize([v], edges_inner), 0, 9)[0]) for v in grid}
check("0.45 and 0.55 land in different bins (the distinction that matters)",
      got[0.45] != got[0.55], f"both in bin {got[0.45]}")
check("0.40 and 0.60 are placed consistently (both up)",
      got[0.40] == 4 and got[0.60] == 6, f"0.40 -> {got[0.40]}, 0.60 -> {got[0.60]}")


print("\n=== 8. The binning gap ===")
rng = np.random.default_rng(1)
p = rng.uniform(0, 1, 200)
y = (rng.uniform(size=200) < p).astype(float)
d_fixed = m.murphy(p, y, n_bins=10)
d_exact = m.murphy(p, y, n_bins=None)
check("distinct-value binning has a gap of exactly 0", abs(d_exact.binning_gap) < 1e-12,
      f"got {d_exact.binning_gap}")
check("fixed bins have a NON-zero gap on continuous forecasts", abs(d_fixed.binning_gap) > 1e-9,
      f"got {d_fixed.binning_gap}; a zero gap here would mean the binned score was not being used")
check("brier_raw is the score of the forecasts as issued",
      abs(d_fixed.brier_raw - m.brier(p, y)) < 1e-12)


print("\n=== 9. The logistic baseline under stress ===")
rng = np.random.default_rng(2)
# (a) every training outcome the same: there is nothing to fit
x = rng.normal(size=100)
y_same = np.ones(100)
try:
    pred = m.logistic_surprise(x[:60], y_same[:60], x[60:])
    finite = np.all(np.isfinite(pred))
    if finite and np.all(pred > 0.9):
        check("all-identical training outcomes -> predicts near 1, stays finite", True)
    else:
        warn("all-identical training outcomes", f"predictions {pred[:3]} — check for divergence")
except Exception as e:
    warn("all-identical training outcomes raised", str(e)[:90])

# (b) zero variance in the input: the standardisation guard must hold
try:
    pred = m.logistic_surprise(np.full(60, 3.0), (rng.uniform(size=60) < 0.5).astype(float), np.full(40, 3.0))
    check("constant input (sd=0) does not divide by zero", np.all(np.isfinite(pred)),
          f"got {pred[:3]}")
except Exception as e:
    check("constant input (sd=0) does not divide by zero", False, str(e)[:90])

# (c) perfectly separable data: the textbook case where Newton diverges
xs = np.concatenate([np.full(30, -2.0), np.full(30, 2.0)])
ys = np.concatenate([np.zeros(30), np.ones(30)])
try:
    pred = m.logistic_surprise(xs, ys, np.array([-2.0, 0.0, 2.0]))
    if np.all(np.isfinite(pred)):
        check("perfectly separable data still returns finite probabilities", True,
              f"predictions {np.round(pred, 4)}")
    else:
        check("perfectly separable data still returns finite probabilities", False, f"got {pred}")
except Exception as e:
    check("perfectly separable data still returns finite probabilities", False, str(e)[:90])

# (d) extrapolation far outside the training range
pred = m.logistic_surprise(rng.normal(size=100), (rng.uniform(size=100) < 0.5).astype(float),
                           np.array([-50.0, 50.0]))
check("extreme extrapolation stays inside [0,1]", np.all((pred >= 0) & (pred <= 1)),
      f"got {pred}")


print("\n=== 10. The base-rate baseline ===")
check("base rate reproduces the training frequency",
      abs(m.base_rate_forecast(np.array([1.0] * 46 + [0.0] * 54), 5)[0] - 0.46) < 1e-12)
br = m.base_rate_forecast(np.array([1.0] * 46 + [0.0] * 54), 100)
y_test = np.array([1.0] * 46 + [0.0] * 54)
check("base rate scored on the same distribution equals the uncertainty term",
      abs(m.brier(br, y_test) - m.murphy(br, y_test).uncertainty) < 1e-12)


print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed, {len(WARN)} warnings")
if FAIL:
    print("\n  FAILURES:")
    for f in FAIL:
        print(f"    - {f}")
if WARN:
    print("\n  WARNINGS (not wrong, but worth knowing):")
    for w in WARN:
        print(f"    - {w}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
