"""EXPLORATORY (plan §9) — forecast quality by stated confidence.

Author's question (2026-09-09, after Block III): when a model put its
probability in a given band — 0.65–0.70, 0.70–0.75, ... in 0.05 steps, i.e.
twenty bins — was it right more often in the confident bands than in the
hesitant ones?

Two readings of "right", both reported:

  1. CALIBRATION (reliability table): in every 0.05-wide forecast bin, the
     share of events that actually went UP against the mean forecast in the
     bin. A calibrated forecaster sits on the diagonal. This is the 20-bin
     version of the 10-bin Murphy table in results.md.
  2. DIRECTIONAL HIT RATE by confidence: confidence = |p - 0.5|. Bands of
     0.05. A forecast "hits" if p > 0.5 and the event went up, or p < 0.5 and
     it went down. p == 0.5 exactly is no call and is counted separately.
     The base-rate reference for a hit rate is max(base rate, 1 - base rate).

Reads `scored.jsonl` (the primary Block-III output) and nothing else; no new
label contact. Only the three genuine rungs (repetition, aggregator, debate)
are used — the matched baselines are prefixes of the repetition pool and
would double-count the same runs. Everything here is labelled EXPLORATORY,
is not in the exposé and carries no confirmatory language (plan §9); thin
bins (< 20 forecasts) are flagged per reading rule 3 and not interpreted.

    python scripts/28_exploratory_confidence_bins.py \
        --scored scaffold/pilot5/scored.jsonl --out scaffold/pilot5/exploratory/confidence_bins.md
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

RUNGS = ("repetition", "aggregator", "debate")
THIN = 20
BIN = 0.05


def load(path: Path) -> list[dict]:
    rows = [json.loads(l) for l in path.open(encoding="utf-8")]
    rows = [r for r in rows if r["arm"] in RUNGS and r["probability"] is not None]
    if not rows:
        raise SystemExit("no rows")
    return rows


def fbin(p: float) -> int:
    """0.05-wide forecast bin index, 0..19; p == 1.0 joins the top bin."""
    return min(int(p / BIN + 1e-9), 19)


def cbin(p: float) -> int:
    """Confidence band index of |p - 0.5|: 0 = [0, .05), ..., 9 = [.45, .50]."""
    return min(int(abs(p - 0.5) / BIN + 1e-9), 9)


def reliability(rows: list[dict]) -> list[tuple]:
    acc = defaultdict(lambda: [0, 0.0, 0, 0.0])          # n, sum p, sum label, sum brier
    for r in rows:
        a = acc[fbin(r["probability"])]
        a[0] += 1
        a[1] += r["probability"]
        a[2] += r["label"]
        a[3] += r["brier"]
    out = []
    for b in range(20):
        n, sp, sl, sb = acc.get(b, [0, 0.0, 0, 0.0])
        lo, hi = b * BIN, (b + 1) * BIN
        if n:
            out.append((lo, hi, n, sp / n, sl / n, sl / n - sp / n, sb / n))
        else:
            out.append((lo, hi, 0, None, None, None, None))
    return out


def hit_rates(rows: list[dict]) -> tuple[list[tuple], int]:
    acc = defaultdict(lambda: [0, 0, 0.0])                # n, hits, sum brier
    no_call = 0
    for r in rows:
        p = r["probability"]
        if abs(p - 0.5) < 1e-12:
            no_call += 1
            continue
        a = acc[cbin(p)]
        a[0] += 1
        a[1] += int((p > 0.5) == (r["label"] == 1))
        a[2] += r["brier"]
    out = []
    for b in range(10):
        n, h, sb = acc.get(b, [0, 0, 0.0])
        lo, hi = b * BIN, (b + 1) * BIN
        out.append((lo, hi, n, (h / n) if n else None, (sb / n) if n else None))
    return out, no_call


def fmt(x, nd=4):
    return "—" if x is None else f"{x:.{nd}f}"


def pct(x):
    return "—" if x is None else f"{100 * x:.1f} %"


def render(rows: list[dict]) -> str:
    w = []
    base = sum(r["label"] for r in rows) / len(rows)
    ref = max(base, 1 - base)
    w.append("# EXPLORATORY — forecast quality by stated confidence (plan §9)")
    w.append("")
    w.append("Not in the exposé, no confirmatory language, no headline. Computed once on")
    w.append("`scored.jsonl` (primary Block-III output), three genuine rungs only")
    w.append(f"(repetition, aggregator, debate): {len(rows)} forecasts on 840 events × 2")
    w.append("models × 2 levels. Bins with fewer than 20 forecasts are marked `thin` and")
    w.append("are not interpreted (reading rule 3).")
    w.append("")
    w.append(f"Share of events that went up, over these forecasts: {base:.4f}. A forecaster")
    w.append(f"who always called the majority side would hit {pct(ref)} of the time — that is")
    w.append("the reference every hit rate below has to clear.")
    w.append("")

    groups: list[tuple[str, list[dict]]] = [("ALL models, ALL rungs", rows)]
    for mk in sorted({r["modelKey"] for r in rows}):
        groups.append((f"{mk}, all rungs", [r for r in rows if r["modelKey"] == mk]))
    for mk in sorted({r["modelKey"] for r in rows}):
        for lvl in ("L1", "L2"):
            groups.append((f"{mk} / {lvl}, all rungs",
                           [r for r in rows if r["modelKey"] == mk and r["level"] == lvl]))

    w.append("## 1. Calibration — twenty 0.05-wide forecast bins")
    w.append("")
    w.append("`observed` is the share of events in the bin that actually went up; `gap` =")
    w.append("observed − mean forecast (positive: the model was too pessimistic in the bin,")
    w.append("negative: too optimistic). `brier` is the mean Brier score inside the bin.")
    w.append("")
    for name, g in groups:
        w.append(f"### {name} (n = {len(g)})")
        w.append("")
        w.append("| forecast bin | n | mean forecast | observed | gap | brier | |")
        w.append("|---|---:|---:|---:|---:|---:|---|")
        for lo, hi, n, mp, ob, gap, br in reliability(g):
            if n == 0:
                continue
            flag = "thin" if n < THIN else ""
            w.append(f"| [{lo:.2f}, {hi:.2f}) | {n} | {fmt(mp)} | {fmt(ob)} | "
                     f"{'—' if gap is None else f'{gap:+.4f}'} | {fmt(br)} | {flag} |")
        w.append("")

    w.append("## 2. Directional hit rate by confidence (|p − 0.5| in 0.05 bands)")
    w.append("")
    w.append("A forecast hits if it called the side the event actually took. `p = 0.50`")
    w.append("exactly is no call and is listed, not scored. Compare every hit rate with")
    w.append(f"the majority-side reference of {pct(ref)}.")
    w.append("")
    for name, g in groups:
        hr, nc = hit_rates(g)
        w.append(f"### {name} (n = {len(g)}, no-call at exactly 0.50: {nc})")
        w.append("")
        w.append("| confidence band |p−0.5| | n | hit rate | brier | |")
        w.append("|---|---:|---:|---:|---|")
        for lo, hi, n, h, br in hr:
            if n == 0:
                continue
            flag = "thin" if n < THIN else ""
            w.append(f"| [{lo:.2f}, {hi:.2f}) | {n} | {pct(h)} | {fmt(br)} | {flag} |")
        w.append("")

    w.append("## 3. Per rung — hit rate by confidence, both models pooled")
    w.append("")
    for rung in RUNGS:
        g = [r for r in rows if r["arm"] == rung]
        hr, nc = hit_rates(g)
        w.append(f"### {rung} (n = {len(g)}, no-call: {nc})")
        w.append("")
        w.append("| confidence band |p−0.5| | n | hit rate | brier | |")
        w.append("|---|---:|---:|---:|---|")
        for lo, hi, n, h, br in hr:
            if n == 0:
                continue
            flag = "thin" if n < THIN else ""
            w.append(f"| [{lo:.2f}, {hi:.2f}) | {n} | {pct(h)} | {fmt(br)} | {flag} |")
        w.append("")

    w.append("## 4. Coarse summary — three confidence classes")
    w.append("")
    w.append("Hesitant: |p−0.5| < 0.10 (forecasts between 0.40 and 0.60). Moderate: 0.10–0.20.")
    w.append("Confident: ≥ 0.20 (forecasts ≤ 0.30 or ≥ 0.70).")
    w.append("")
    w.append("| group | class | n | hit rate | brier |")
    w.append("|---|---|---:|---:|---:|")
    for name, g in groups[:3]:
        cls = defaultdict(lambda: [0, 0, 0.0])
        for r in g:
            p = r["probability"]
            d = abs(p - 0.5)
            if d < 1e-12:
                continue
            k = "hesitant" if d < 0.10 else ("moderate" if d < 0.20 else "confident")
            a = cls[k]
            a[0] += 1
            a[1] += int((p > 0.5) == (r["label"] == 1))
            a[2] += r["brier"]
        for k in ("hesitant", "moderate", "confident"):
            n, h, sb = cls[k]
            w.append(f"| {name} | {k} | {n} | {pct(h / n) if n else '—'} | {fmt(sb / n) if n else '—'} |")
    w.append("")
    return "\n".join(w) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scored", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    rows = load(a.scored)
    text = render(rows)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
