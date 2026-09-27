"""Join the model forecasts to the truth and answer the three research questions.

This is the seam between the two halves of the pipeline. The TypeScript harness
writes what the models said (`*_outcomes.jsonl`) and what that cost
(`*_structure_tokens.jsonl`); `thesis.db` holds what actually happened. Until
this file existed nothing read the harness output at all, so no number in the
thesis could be produced end to end.

    RQ1  does a communicating structure beat the single-agent baseline at the
         same token budget? Compared PAIRED, on the same event, because the
         events differ enormously in difficulty and an unpaired mean would
         mostly measure which events each side happened to be asked about.
    RQ2  what did the coordination itself cost, in tokens?
    RQ3  does more context (level 2) help more than coordination does?

Everything it computes about probabilities comes from `21_metrics.py`. A second
Brier formula or a second rounding rule in this file would be a defect: the two
copies would agree until one of them was edited.

    python scripts/24_score_experiment.py --run OUT.jsonl,TOK.jsonl [--run ...]
    python scripts/24_score_experiment.py --self-test
    python scripts/24_score_experiment.py --case MSFT_2025-10-29_L2 --model qwen3-32b@on

Exit code is 0 only if every integrity check passed. Any violation aborts.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

import numpy as np

HERE = Path(__file__).resolve().parent
THESIS = HERE.parent
DB_DEFAULT = THESIS / "thesis.db"
PILOT_DIR = THESIS / "scaffold" / "pilot5"

# The digit prefix makes `import 21_metrics` a syntax error, so the module is
# loaded by path. Same pattern as `23_metrics_edge_tests.py`.
_spec = importlib.util.spec_from_file_location("metrics", HERE / "21_metrics.py")
metrics = importlib.util.module_from_spec(_spec)
sys.modules["metrics"] = metrics
_spec.loader.exec_module(metrics)

brier = metrics.brier
murphy = metrics.murphy
canonical = metrics.canonical
_checked = metrics._checked

# The leakage rule (working doc §5.1): only events whose t0 falls after the
# newest model's release date, so nothing the model could have memorised about
# the outcome is in its training data. Same literal as `21_metrics.baselines()`.
MODEL_RELEASE_CUTOFF = "2025-04-28"

STRUCTURES = ("repetition", "aggregator", "debate")
# The two structures that are compute-matched against the single-agent rung.
# `repetition` is not: it IS the single-agent rung, so it has nothing to pair with.
COMMUNICATING = ("aggregator", "debate")
BASELINE_ARM = {s: f"matched_baseline_{s}" for s in COMMUNICATING}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LEVEL_RE = re.compile(r"^L\d+$")


class IntegrityError(Exception):
    """A check that must stop the run. Never caught except to print and exit 1."""


def abort(msg: str) -> None:
    raise IntegrityError(msg)


# --------------------------------------------------------------------------- #
# 1. The join key
# --------------------------------------------------------------------------- #
def parse_event_id(eid: str) -> tuple[str, str, str]:
    """Split `MSFT_2025-10-29_L2` into ('MSFT', '2025-10-29', 'L2').

    THE LEVEL COMES FROM HERE, never from the filename. Filenames are a human
    archiving convention - `L1_outcomes.jsonl` is just what someone called the
    file - and this directory already contains `outcomes.jsonl` and
    `L2_outcomes.jsonl` holding the same bytes. The eventId is written by the
    runner and travels with the data.

    The split is from the LEFT for the ticker and from the RIGHT for the level,
    because the date in the middle contains no underscore but a ticker could:
    no ticker in this database does (checked - BRK-B uses a hyphen), but a
    left-to-right split would quietly hand back the wrong date if one ever did,
    and a wrong date joins to a different event rather than failing.
    """
    if not isinstance(eid, str) or eid.count("_") < 2:
        abort(f"eventId {eid!r} is not {{ticker}}_{{report_date}}_{{level}} - it has "
              f"fewer than two underscores, so the level is missing")
    symbol, rest = eid.split("_", 1)
    report_date, _, level = rest.rpartition("_")
    if not symbol:
        abort(f"eventId {eid!r} has an empty ticker")
    if not DATE_RE.match(report_date):
        abort(f"eventId {eid!r} - the middle part {report_date!r} is not a YYYY-MM-DD "
              f"report date, so this id does not have the expected shape")
    if not LEVEL_RE.match(level):
        abort(f"eventId {eid!r} - the suffix {level!r} is not a level like L1 or L2")
    return symbol, report_date, level


def load_truth(db_path: Path) -> dict[tuple[str, str], int]:
    """(symbol, report_date) -> label, from v_level1. Read-only, always."""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    truth: dict[tuple[str, str], int] = {}
    for symbol, report_date, label in con.execute(
            "SELECT symbol, report_date, label FROM v_level1"):
        key = (symbol, report_date)
        # v_level1 is one row per company-quarter, so a duplicate here would mean
        # one eventId resolving to two different labels and the scorer silently
        # picking whichever came last. Verified 2953 rows to 2953 distinct pairs.
        if key in truth:
            abort(f"v_level1 holds more than one row for {symbol} {report_date}; "
                  f"an eventId would not resolve to exactly one event")
        truth[key] = label
    con.close()
    return truth


def sample_flow_counts(db_path: Path) -> dict[str, int]:
    """The three database-side lines of the sample-flow table.

    A viva examiner will ask where the difference between 2953 events in the
    database and the N in the results table went. These are the first two steps
    of that answer; the rest are counted from the run itself.
    """
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    total = con.execute("SELECT COUNT(*) FROM v_level1").fetchone()[0]
    after_release = con.execute(
        "SELECT COUNT(*) FROM v_level1 WHERE t0 > ?", (MODEL_RELEASE_CUTOFF,)).fetchone()[0]
    eligible = con.execute(
        "SELECT COUNT(*) FROM v_level1 WHERE t0 > ? AND in_index_at_t0 = 1",
        (MODEL_RELEASE_CUTOFF,)).fetchone()[0]
    con.close()
    return {"total": total, "after_release": after_release, "eligible": eligible}


# --------------------------------------------------------------------------- #
# 2. Reading a run, and proving the two files belong together
# --------------------------------------------------------------------------- #
def read_jsonl(path: Path) -> list[dict]:
    """Read a JSON-lines file. `newline=""` because these files are CRLF."""
    if not path.exists():
        abort(f"input file not found: {path}")
    out = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                abort(f"{path.name} line {i} is not valid JSON: {exc}")
    if not out:
        abort(f"{path.name} is empty")
    return out


def check_provenance(tokens_path: Path, meta: dict) -> str:
    """Verify the token summary against the ledger it claims to summarise.

    The summary is a derived file: if it was generated from an older ledger, or
    from a different event's ledger, every token count in RQ2 is wrong and
    nothing about the numbers looks odd. The header carries the ledger's line
    count and SHA-256 so that can be checked rather than assumed.

    If the ledger is NOT in the same folder as the summary the check cannot run.
    That is the situation for L2 in this pilot: the header names
    `L2_ledger5.jsonl` and the folder holds the same bytes under the name
    `ledger5.jsonl`. Rather than guess which file was meant - or stay quiet - the
    check is skipped and the skip is printed in results.md, so a reader can see
    that this run's token counts were taken on trust.
    """
    if "_meta" not in meta:
        abort(f"{tokens_path.name} has no _meta header on line 1; without it the "
              f"token counts cannot be traced back to a ledger")
    m = meta["_meta"]
    for key in ("ledgerPath", "ledgerLines", "ledgerSha256"):
        if key not in m:
            abort(f"{tokens_path.name} _meta is missing {key!r}")

    # ledgerPath is an absolute Windows path with backslashes. PureWindowsPath
    # understands both separators, so this also works if it is ever posix.
    ledger_name = PureWindowsPath(str(m["ledgerPath"])).name
    candidate = tokens_path.parent / ledger_name
    if not candidate.exists():
        return (f"{tokens_path.name}: provenance NOT CHECKED - the header names "
                f"`{ledger_name}`, which is not in {tokens_path.parent}. "
                f"Its {m['ledgerLines']} lines and sha {str(m['ledgerSha256'])[:16]} "
                f"were taken on trust.")

    data = candidate.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    lines = sum(1 for ln in data.split(b"\n") if ln.strip())
    if lines != m["ledgerLines"]:
        abort(f"{tokens_path.name}: header says the ledger has {m['ledgerLines']} "
              f"lines, {candidate.name} has {lines}")
    if sha != m["ledgerSha256"]:
        abort(f"{tokens_path.name}: header says the ledger hashes to "
              f"{m['ledgerSha256']}, {candidate.name} hashes to {sha}")
    return (f"{tokens_path.name}: provenance OK - {candidate.name}, "
            f"{lines} lines, sha {sha[:16]}, harness commit {m.get('harnessCommit', '?')}")


@dataclass
class Run:
    outcomes_path: Path
    tokens_path: Path
    records: list[dict]
    tokens: dict[tuple[str, str, str], dict]
    provenance: str


def load_run(outcomes_path: Path, tokens_path: Path) -> Run:
    records = read_jsonl(outcomes_path)
    token_lines = read_jsonl(tokens_path)
    provenance = check_provenance(tokens_path, token_lines[0])

    tokens: dict[tuple[str, str, str], dict] = {}
    for row in token_lines[1:]:
        key = (row["eventId"], row["modelKey"], row["structure"])
        if key in tokens:
            abort(f"{tokens_path.name} has two rows for {key}")
        tokens[key] = row

    # The two files are passed as a pair on the command line, so it is entirely
    # possible to pair L1 outcomes with L2 token counts by mistake. Nothing about
    # that would crash - RQ1 would read fine and RQ2 would report another level's
    # costs - so the pairs each file covers must be identical.
    outcome_pairs = {(r["eventId"], r["modelKey"]) for r in records}
    token_pairs = {(k[0], k[1]) for k in tokens}
    if outcome_pairs != token_pairs:
        only_o = sorted(outcome_pairs - token_pairs)[:3]
        only_t = sorted(token_pairs - outcome_pairs)[:3]
        abort(f"{outcomes_path.name} and {tokens_path.name} do not describe the same "
              f"run: {len(outcome_pairs - token_pairs)} pairs only in outcomes "
              f"(e.g. {only_o}), {len(token_pairs - outcome_pairs)} only in tokens "
              f"(e.g. {only_t})")
    for eid, mk in sorted(outcome_pairs):
        for s in STRUCTURES:
            if (eid, mk, s) not in tokens:
                abort(f"{tokens_path.name} has no {s!r} row for {eid} / {mk}; "
                      f"all three structures are emitted for every pair")
    return Run(outcomes_path, tokens_path, records, tokens, provenance)


# --------------------------------------------------------------------------- #
# 3. Scoring
# --------------------------------------------------------------------------- #
def ensemble_mean(ps: list[float]) -> float:
    """The mean of a set of single-agent forecasts, rounded the project's way.

    This is NOT a second metric. It is the same combination the harness applies
    (`structures5.ts combine(..., 'mean')`, decision B5/D19), needed here only to
    rebuild the baseline at a SHORTER prefix for the RQ3 equal-runs control. The
    rounding is `canonical()` imported from 21_metrics.py, so there is exactly
    one rule in the project for when two forecasts count as the same forecast.

    It is verified rather than trusted: `cross_check_recombination` below rebuilds
    every probability the harness already recorded and aborts on any mismatch.
    """
    if not ps:
        abort("ensemble_mean() on an empty prefix")
    return float(canonical(sum(ps) / len(ps)))


def single_brier(p: float, y: int) -> float:
    """Brier score of one forecast. Same `brier()` as everywhere else."""
    return brier([p], [y])


@dataclass
class ScoredRow:
    eventId: str
    symbol: str
    reportDate: str
    level: str
    modelKey: str
    arm: str
    probability: float | None
    label: int
    brier: float | None
    runsUsed: int | None = None
    matchedTokens: int | None = None
    structureTokens: int | None = None
    residualTokens: int | None = None
    excludedReason: str | None = None

    def to_json(self) -> dict:
        d = {
            "eventId": self.eventId, "symbol": self.symbol, "reportDate": self.reportDate,
            "level": self.level, "modelKey": self.modelKey, "arm": self.arm,
            "probability": self.probability, "label": self.label, "brier": self.brier,
        }
        for name in ("runsUsed", "matchedTokens", "structureTokens", "residualTokens",
                     "excludedReason"):
            v = getattr(self, name)
            if v is not None:
                d[name] = v
        return d


@dataclass
class Pair:
    """One compute-matched comparison that is actually usable in RQ1."""
    eventId: str
    symbol: str
    reportDate: str
    level: str
    modelKey: str
    structure: str
    p_structure: float
    p_baseline: float
    label: int
    runsUsed: int
    # D29/D31: the provider that served this event's ladder (from the outcomes
    # line; None on pre-D29 data). Carried per pair so §8.1's provider-halves
    # recomputation groups the SAME pairs the primary cells use.
    provider: str | None = None


@dataclass
class Analysis:
    rows: list[ScoredRow] = field(default_factory=list)
    pairs: list[Pair] = field(default_factory=list)
    # (eventId, modelKey) -> the drawn single-agent pool, in generation order
    pools: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    provenance: list[str] = field(default_factory=list)
    dropped: dict = field(default_factory=lambda: defaultdict(int))
    zero_runs: dict = field(default_factory=lambda: defaultdict(int))
    no_pair: dict = field(default_factory=lambda: defaultdict(int))
    sample_flow: dict = field(default_factory=dict)
    rq1: dict = field(default_factory=dict)
    rq1_unmatched: dict = field(default_factory=dict)
    rq2: dict = field(default_factory=dict)
    rq3: dict = field(default_factory=dict)
    rq3_control: dict = field(default_factory=dict)
    rq3_common_events: int = 0
    exploratory_on: bool = False
    # --sample only (2026-09-05, review L2-07): the drawn-event bookkeeping the
    # plan's §7 promises. None when no sample file was given (self-tests, ad-hoc
    # scoring of dev data).
    sample_info: dict | None = None
    # ---- Block III (D31) additions ------------------------------------------
    # per (eventId, modelKey): billed tokens of each pool run, generation order
    # (None entries when an old outcomes file lacks them).
    pools_tokens: dict = field(default_factory=dict)
    # per (mk, level): the tare check on the matching engine (plan §4).
    tare: dict = field(default_factory=dict)
    # per RQ1 cell: base-rate and logistic Brier on the cell's paired events.
    baselines: dict = field(default_factory=dict)
    baselines_meta: dict = field(default_factory=dict)
    # per (mk, level): committed reference curve (plan §6.2; formerly the
    # --exploratory-gated curve).
    reference_curve: dict = field(default_factory=dict)
    # per (mk, structure): the executable RQ3 gate (plan §6.2, D31/II-2).
    rq3_gate: dict = field(default_factory=dict)
    # per RQ1 cell: paired difference recomputed per provider half (plan §8.1).
    provider_split: dict = field(default_factory=dict)
    # --exclude-events bookkeeping (plan §8.2 sensitivity mechanism).
    excluded: dict | None = None
    scorer_commit: str = "unknown"


def cross_check_recombination(rec: dict, pool: list[float]) -> None:
    """Rebuild every probability the harness recorded, from the runs it names.

    The repetition rung is the mean of the first N workers and each matched
    baseline is the mean of the first `runsUsed` runs of the same pool. Rebuilding
    them here is the one thing that proves this file understands the harness
    output the same way the harness meant it - and it is what licenses the equal-
    runs control in RQ3, which rebuilds the same means at a shorter prefix.

    On the pilot files all twelve recorded probabilities reproduce exactly.
    A mismatch means either the pool order was disturbed or the harness stopped
    combining by mean (decision B5 commits it to the mean), so it aborts.
    """
    eid, mk = rec["eventId"], rec["modelKey"]
    rounds = rec.get("roundProbabilities") or []
    rep = rec["outcomes"]["repetition"]
    if rounds and not rep.get("dropped") and rep.get("probability") is not None:
        n_workers = len(rounds[0])
        if n_workers > len(pool):
            abort(f"{eid}/{mk}: {n_workers} workers in roundProbabilities but only "
                  f"{len(pool)} runs in repetitionRuns")
        got = ensemble_mean(pool[:n_workers])
        want = float(canonical(rep["probability"]))
        if got != want:
            abort(f"{eid}/{mk}: repetition says {want} but the mean of its "
                  f"{n_workers} workers is {got}")
    for mt in rec.get("matched") or []:
        used, p = mt.get("runsUsed", 0), mt.get("probability")
        if used <= 0 or p is None:
            continue
        if used > len(pool):
            abort(f"{eid}/{mk}: matched {mt['structure']} claims runsUsed={used} but "
                  f"repetitionRuns holds only {len(pool)} runs")
        got, want = ensemble_mean(pool[:used]), float(canonical(p))
        if got != want:
            abort(f"{eid}/{mk}: matched {mt['structure']} baseline says {want} but the "
                  f"mean of its first {used} runs is {got}")


def build_rows(runs: list[Run], truth: dict, analysis: Analysis,
               exclude: set[tuple[str, str]] | None = None,
               exclude_label: str = "") -> None:
    """Turn the harness output into one scored row per (event, model, arm).

    `exclude` is the §8.2 sensitivity mechanism (D31): a set of
    (symbol, report_date) pairs dropped BEFORE any scoring, with the count and
    the caller's reason carried into the report header. Nothing else changes,
    so a run with and without --exclude-events differs only in those events.
    """
    seen: dict[tuple[str, str], Path] = {}
    if exclude is not None:
        analysis.excluded = {"label": exclude_label, "listed": len(exclude),
                             "dropped": defaultdict(int)}

    for run in runs:
        analysis.provenance.append(run.provenance)
        for rec in run.records:
            eid, mk = rec["eventId"], rec["modelKey"]
            # A duplicate would be counted twice in every mean. It is easy to
            # produce: this folder holds `outcomes.jsonl` and `L2_outcomes.jsonl`
            # with identical bytes, and passing both --run flags is a plausible
            # slip that nothing else would notice.
            if (eid, mk) in seen:
                abort(f"{eid} / {mk} appears twice: once in {seen[(eid, mk)].name} and "
                      f"again in {run.outcomes_path.name}. Every event-model pair must "
                      f"appear once or it is double-counted.")
            seen[(eid, mk)] = run.outcomes_path

            symbol, report_date, level = parse_event_id(eid)
            if exclude is not None and (symbol, report_date) in exclude:
                analysis.excluded["dropped"][(mk, level)] += 1
                continue
            if (symbol, report_date) not in truth:
                abort(f"{eid} does not resolve to any v_level1 row "
                      f"(looked for symbol={symbol!r} report_date={report_date!r})")
            label = truth[(symbol, report_date)]
            if label is None:
                abort(f"{eid} resolves to a v_level1 row whose label is NULL; there is "
                      f"nothing to score it against")
            if label not in (0, 1):
                abort(f"{eid} has label {label!r}; labels must be 0 or 1")

            pool = [r["probability"] for r in rec.get("repetitionRuns") or []]
            analysis.pools[(eid, mk)] = pool
            cross_check_recombination(rec, pool)
            # Block III (D31): per-run billed tokens (for the tare check) and
            # the serving provider (for §8.1). Both may be absent on pre-D29
            # harness output; absence is handled, never fabricated.
            pool_tokens = [r.get("totalTokens") for r in rec.get("repetitionRuns") or []]
            analysis.pools_tokens[(eid, mk)] = pool_tokens
            event_provider = rec.get("provider")

            outcomes = rec.get("outcomes") or {}
            matched_by_structure = {m["structure"]: m for m in (rec.get("matched") or [])}

            # ---- tare check (plan §4, D31/II-3): feed the prefix-by-budget
            # rule the repetition rung's OWN billed cost; it must hand back the
            # rung itself, difference zero. Same prefix rule as the harness:
            # cumulative run tokens <= budget, generation order, never sorted.
            rep_out = outcomes.get("repetition") or {}
            rounds0 = (rec.get("roundProbabilities") or [[]])[0]
            if (not rep_out.get("dropped") and rep_out.get("probability") is not None
                    and rounds0 and pool_tokens
                    and all(isinstance(t, (int, float)) for t in pool_tokens)):
                n_w = len(rounds0)
                budget = sum(pool_tokens[:n_w])
                used, cum = 0, 0
                for t in pool_tokens:
                    if cum + t > budget:
                        break
                    cum += t
                    used += 1
                p_tare = ensemble_mean(pool[:used]) if used else None
                p_rep = float(canonical(rep_out["probability"]))
                analysis.tare.setdefault((mk, level), []).append({
                    "used": used, "workers": n_w,
                    "diff": (single_brier(p_tare, label) - single_brier(p_rep, label))
                            if p_tare is not None else None,
                    "exact": p_tare == p_rep,
                })

            def row(arm, prob, **kw):
                """Score one arm, or record why it could not be scored."""
                if prob is None:
                    analysis.rows.append(ScoredRow(
                        eid, symbol, report_date, level, mk, arm, None, label, None, **kw))
                    return
                # `_checked` is the same gate the metrics use: it refuses a
                # probability outside [0,1] or a non-0/1 outcome instead of
                # returning a plausible number from bad input.
                try:
                    p_arr, _ = _checked([prob], [label])
                except ValueError as exc:
                    abort(f"{eid} / {mk} / {arm}: {exc}")
                p = float(p_arr[0])
                analysis.rows.append(ScoredRow(
                    eid, symbol, report_date, level, mk, arm, p, label,
                    single_brier(p, label), **kw))

            # --- the repetition rung: the shared workers, combined.
            # No structureTokens on this row on purpose. The `repetition` line of
            # the token summary counts the WHOLE drawn pool, including the extra
            # runs drawn later to match the debate's budget, so it is not the cost
            # of this three-worker answer and printing it here would invite the
            # reader to divide one by the other.
            rep = outcomes.get("repetition", {})
            n_workers = len(rec["roundProbabilities"][0]) if rec.get("roundProbabilities") else None
            if rep.get("dropped"):
                analysis.dropped[(level, mk, "repetition")] += 1
                row("repetition", None, runsUsed=n_workers,
                    excludedReason=f"dropped: {rep.get('dropReason', 'malformed')}")
            else:
                row("repetition", rep.get("probability"), runsUsed=n_workers)

            for s in COMMUNICATING:
                out = outcomes.get(s, {})
                tok = run.tokens[(eid, mk, s)]
                mt = matched_by_structure.get(s)

                # The budget the harness matched against and the BILLED total the
                # token summary reports must be the same number. They are computed
                # by different code paths over the same ledger, so a disagreement
                # means one of the two files is stale. Billed, not usable: the
                # pre-registered budget counts discarded retry attempts (plan §2
                # "compute matching", DATA_FLOW "billedTotal ... is the figure
                # compute matching uses"). Until 2026-09-09 this line compared
                # against `total`, which every dry run (0 malformed attempts) let
                # pass; the real run's first retried structure stopped it (D32
                # deviation 5).
                if mt is not None and mt.get("structureTokens") != tok["billedTotal"]:
                    abort(f"{eid} / {mk} / {s}: matched.structureTokens="
                          f"{mt.get('structureTokens')} but the token summary reports "
                          f"billedTotal={tok['billedTotal']}. The two files disagree "
                          f"about what this structure cost.")

                struct_p = out.get("probability")
                if out.get("dropped") or struct_p is None:
                    analysis.dropped[(level, mk, s)] += 1
                    row(s, None, structureTokens=tok["billedTotal"],
                        excludedReason=f"dropped: {out.get('dropReason', 'malformed')}")
                else:
                    row(s, struct_p, structureTokens=tok["billedTotal"])

                arm = BASELINE_ARM[s]
                if mt is None:
                    analysis.no_pair[(level, mk, s)] += 1
                    row(arm, None, structureTokens=tok["billedTotal"],
                        excludedReason="no compute-matched pair was drawn "
                                       "(the repetition rung was dropped)")
                    continue
                extra = dict(runsUsed=mt.get("runsUsed"),
                             matchedTokens=mt.get("matchedTokens"),
                             structureTokens=mt.get("structureTokens"),
                             residualTokens=mt.get("residualTokens"))
                if not mt.get("runsUsed"):
                    # Not one single-agent run fitted inside the structure's
                    # budget, so there is no baseline to compare against. Counted
                    # and excluded rather than scored at some stand-in value.
                    analysis.zero_runs[(level, mk, s)] += 1
                    row(arm, None, excludedReason="runsUsed = 0", **extra)
                    continue
                row(arm, mt.get("probability"), **extra)

                # A pair enters RQ1 only if BOTH sides produced a number.
                if struct_p is not None and not out.get("dropped") and mt.get("probability") is not None:
                    analysis.pairs.append(Pair(
                        eid, symbol, report_date, level, mk, s,
                        float(canonical(struct_p)), float(canonical(mt["probability"])),
                        label, int(mt["runsUsed"]), provider=event_provider))


# --------------------------------------------------------------------------- #
# 4. RQ1 - paired, at equal cost
# --------------------------------------------------------------------------- #
def decomp(p, y) -> dict:
    """Murphy decomposition, with the identity asserted before it is reported."""
    d = murphy(p, y)
    assert d.identity_holds(), (
        f"Murphy identity broken: brier {d.brier} vs "
        f"{d.reliability - d.resolution + d.uncertainty}")
    return {"brier": d.brier, "brier_raw": d.brier_raw, "binning_gap": d.binning_gap,
            "reliability": d.reliability, "resolution": d.resolution,
            "uncertainty": d.uncertainty, "empty_bins": d.empty_bins,
            "singleton_bins": d.singleton_bins, "n": d.n}


def compute_rq1(analysis: Analysis) -> None:
    groups: dict = defaultdict(list)
    for pr in analysis.pairs:
        groups[(pr.modelKey, pr.level, pr.structure)].append(pr)

    for key, prs in sorted(groups.items()):
        y = [p.label for p in prs]
        ps = [p.p_structure for p in prs]
        pb = [p.p_baseline for p in prs]
        b_s, b_b = brier(ps, y), brier(pb, y)
        # The paired difference is built event by event, which is what "paired"
        # means: each structure answer minus the baseline answer on THE SAME
        # event. For the mean it is algebraically the same as the difference of
        # the two means, and the assert says so - if that ever fails, the two
        # sides have been misaligned and are no longer describing one event each.
        diffs = [single_brier(a, yy) - single_brier(b, yy) for a, b, yy in zip(ps, pb, y)]
        mean_diff = float(np.mean(diffs))
        assert abs(mean_diff - (b_s - b_b)) < 1e-12, "paired difference is not paired"
        analysis.rq1[key] = {
            "n": len(prs), "brier_structure": b_s, "brier_baseline": b_b,
            "mean_paired_diff": mean_diff,
            "wins": int(sum(1 for d in diffs if d < 0)),
            "mean_runs_used": float(np.mean([p.runsUsed for p in prs])),
            "murphy_structure": decomp(ps, y), "murphy_baseline": decomp(pb, y),
        }

    # The unmatched rungs, for context only. These are every event on which the
    # rung produced a number, NOT a compute-matched pair, so they are not
    # comparable to each other: repetition here is three workers, debate is nine
    # calls. They are printed because a reader will otherwise wonder what the
    # rungs scored outside the pairing.
    ctx: dict = defaultdict(list)
    for r in analysis.rows:
        if r.arm in STRUCTURES and r.brier is not None:
            ctx[(r.modelKey, r.level, r.arm)].append((r.probability, r.label))
    for key, vals in sorted(ctx.items()):
        p = [v[0] for v in vals]
        y = [v[1] for v in vals]
        analysis.rq1_unmatched[key] = {"n": len(vals), "brier": brier(p, y)}


# --------------------------------------------------------------------------- #
# 5. RQ2 - what coordination cost, read verbatim
# --------------------------------------------------------------------------- #
# The seven cost fields the analysis plan names in §5.1, plus the read/write
# buckets (D/A/B + C1/C2; B_SELF is already among the seven) so the promised
# five-bucket breakdown can be rendered without a second pass over the files.
# Extended 2026-09-05: the plan promised all of these per cell, but the render
# printed only three of the seven (review finding L2-05).
RQ2_FIELDS = ("commCost", "billedCommCost", "commCostInclSelf",
              "billedCommCostInclSelf", "B_SELF", "total", "billedTotal",
              "D", "A", "B", "C1", "C2")


def compute_rq2(runs: list[Run], analysis: Analysis) -> None:
    """Sum the token summary's own fields. No arithmetic of our own.

    Every number here is copied out of `*_structure_tokens.jsonl`, which in turn
    copies it off `summarize()` in the harness. Deriving communication cost in
    Python - say, as A + B - would give a second answer to a question that already
    has one, and the two would drift. The only operation applied is adding up
    across events, which the totals column makes visible.

    Absolute tokens, not shares of the total: a share hides that a debate costing
    six thousand communication tokens and one costing sixty thousand can have the
    same percentage.
    """
    acc: dict = defaultdict(lambda: defaultdict(int))
    counts: dict = defaultdict(int)
    for run in runs:
        for (eid, mk, s), row in run.tokens.items():
            _, _, level = parse_event_id(eid)
            key = (mk, level, s)
            counts[key] += 1
            for f in RQ2_FIELDS:
                acc[key][f] += row[f]
    for key, sums in sorted(acc.items()):
        n = counts[key]
        analysis.rq2[key] = {"n": n,
                             **{f"total_{f}": sums[f] for f in RQ2_FIELDS},
                             **{f"mean_{f}": sums[f] / n for f in RQ2_FIELDS}}


# --------------------------------------------------------------------------- #
# 6. RQ3 - level 2 minus level 1, on the events both levels saw
# --------------------------------------------------------------------------- #
def compute_rq3(analysis: Analysis) -> None:
    """Decision D17: the delta is computed on the COMMON event set only.

    Level 2 was not run on every event level 1 was run on, and a structure can be
    dropped at one level and not the other. Taking the mean at each level over
    whatever it happened to cover and subtracting would mix the level effect with
    the difference in which events each level was asked about - and that mixture
    is invisible in the result. So an event counts only if both levels produced a
    usable number for that arm and model.
    """
    by_arm: dict = defaultdict(dict)   # (model, arm) -> {(symbol, date): {level: row}}
    for r in analysis.rows:
        if r.brier is None:
            continue
        by_arm[(r.modelKey, r.arm)].setdefault((r.symbol, r.reportDate), {})[r.level] = r

    common_events: set = set()
    for (mk, arm), events in sorted(by_arm.items()):
        common = {ev: lv for ev, lv in events.items() if "L1" in lv and "L2" in lv}
        if not common:
            continue
        common_events |= set(common)
        y = [lv["L1"].label for lv in common.values()]
        p1 = [lv["L1"].probability for lv in common.values()]
        p2 = [lv["L2"].probability for lv in common.values()]
        b1, b2 = brier(p1, y), brier(p2, y)
        analysis.rq3[(mk, arm)] = {
            "n": len(common), "brier_L1": b1, "brier_L2": b2, "delta": b2 - b1,
            "n_L1_only": sum(1 for lv in events.values() if "L2" not in lv),
            "n_L2_only": sum(1 for lv in events.values() if "L1" not in lv),
        }
    analysis.rq3_common_events = len(common_events)

    # The D23 control. A level-2 prompt is longer, so the same structure budget
    # buys FEWER single-agent runs at level 2 than at level 1 - on the pilot, 12
    # against 18 for DeepSeek's debate. A raw L2-minus-L1 on the baseline arm
    # therefore mixes "more context" with "a thinner ensemble". This rebuilds both
    # baselines from the first k runs of their own pools, k = the smaller of the
    # two run counts, so the ensemble size is the same on both sides and only the
    # context differs.
    for s in COMMUNICATING:
        arm = BASELINE_ARM[s]
        for (mk, a), events in sorted(by_arm.items()):
            if a != arm:
                continue
            rows_out = []
            for ev, lv in sorted(events.items()):
                if "L1" not in lv or "L2" not in lv:
                    continue
                r1, r2 = lv["L1"], lv["L2"]
                k = min(r1.runsUsed or 0, r2.runsUsed or 0)
                if k < 1:
                    continue
                pool1 = analysis.pools[(r1.eventId, mk)]
                pool2 = analysis.pools[(r2.eventId, mk)]
                if k > len(pool1) or k > len(pool2):
                    abort(f"{ev} / {mk} / {arm}: equal-runs control needs {k} runs but "
                          f"the pools hold {len(pool1)} and {len(pool2)}")
                rows_out.append((ensemble_mean(pool1[:k]), ensemble_mean(pool2[:k]),
                                 r1.label, k))
            if not rows_out:
                continue
            y = [r[2] for r in rows_out]
            b1 = brier([r[0] for r in rows_out], y)
            b2 = brier([r[1] for r in rows_out], y)
            analysis.rq3_control[(mk, s)] = {
                "n": len(rows_out), "brier_L1": b1, "brier_L2": b2, "delta": b2 - b1,
                "runs_each_side": float(np.mean([r[3] for r in rows_out])),
            }


# --------------------------------------------------------------------------- #
# 7. EXPLORATORY - the ensemble curve, behind a flag
# --------------------------------------------------------------------------- #
def compute_reference_curve(analysis: Analysis) -> None:
    """Score the single-agent pool at every prefix length, with no coordination.

    COMMITTED output since D31 (plan §6.2): this curve is the context that makes
    the equal-runs control checkable, so it is always computed and rendered.
    Until D31 it was gated behind --exploratory; only its READING as "averaging
    more runs helps on its own" remains an exploratory interpretation, rendered
    with the EXPLORATORY label when the flag is set.
    """
    by_level: dict = defaultdict(list)
    for (eid, mk), pool in analysis.pools.items():
        _, _, level = parse_event_id(eid)
        label = next((r.label for r in analysis.rows
                      if r.eventId == eid and r.modelKey == mk), None)
        if label is None or not pool:
            continue
        by_level[(mk, level)].append((pool, label))

    for key, entries in sorted(by_level.items()):
        curve = []
        longest = max(len(p) for p, _ in entries)
        for k in range(1, longest + 1):
            usable = [(ensemble_mean(p[:k]), y) for p, y in entries if len(p) >= k]
            if not usable:
                continue
            curve.append({"k": k, "n_events": len(usable),
                          "brier": brier([u[0] for u in usable], [u[1] for u in usable])})
        analysis.reference_curve[key] = curve


def compute_tare(analysis: Analysis) -> None:
    """Aggregate the per-event tare rows (plan §4, D31/II-3) into per-cell sums.

    Reported, never gated: no pass/fail verdict lives here. `n_exact` counts
    events where the prefix draw reproduced the repetition probability to the
    canonical digit; on retry-free events anything else means the budget or
    prefix arithmetic is broken somewhere.
    """
    for key in sorted(list(analysis.tare.keys())):
        rows = analysis.tare[key]
        diffs = [r["diff"] for r in rows if r["diff"] is not None]
        analysis.tare[key] = {
            "n": len(rows),
            "n_exact": sum(1 for r in rows if r["exact"]),
            "n_prefix_ne_workers": sum(1 for r in rows if r["used"] != r["workers"]),
            "mean_diff": float(np.mean(diffs)) if diffs else None,
            "max_abs_diff": float(max(abs(d) for d in diffs)) if diffs else None,
        }


def compute_baselines(analysis: Analysis, db_path: Path) -> None:
    """The two reference baselines of plan §4, per RQ1 cell (D31).

    Fitted STRICTLY before the experiment window - `t0 <= MODEL_RELEASE_CUTOFF`
    - so no test event is ever in the training data (WD §7.2's rule; the old
    `baselines()` demo split the eligible window itself and must not be used).
    Surprises are clipped to ±200 on both sides, mirroring the demo's guard
    against percent-vs-fraction outliers. Sample events with no surprise value
    are excluded from the logistic only, and counted.
    """
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    train = con.execute(
        "SELECT eps_surprise_pct, label FROM v_level1 "
        "WHERE t0 <= ? AND eps_surprise_pct IS NOT NULL AND label IS NOT NULL",
        (MODEL_RELEASE_CUTOFF,)).fetchall()
    surprise = {(s, rd): x for s, rd, x in con.execute(
        "SELECT symbol, report_date, eps_surprise_pct FROM v_level1")}
    con.close()

    if len(train) < 2 or len({int(y) for _, y in train}) < 2:
        analysis.baselines_meta = {
            "n_train": len(train), "cutoff": MODEL_RELEASE_CUTOFF,
            "note": "training set empty or single-class; baselines not computed"}
        return
    tx = np.clip(np.array([t[0] for t in train], dtype=float), -200, 200)
    ty = np.array([t[1] for t in train], dtype=float)
    base_const = float(np.mean(ty))
    analysis.baselines_meta = {"n_train": len(train), "cutoff": MODEL_RELEASE_CUTOFF,
                               "train_base_rate": base_const}

    groups: dict = defaultdict(list)
    for pr in analysis.pairs:
        groups[(pr.modelKey, pr.level, pr.structure)].append(pr)
    for key, prs in sorted(groups.items()):
        y_all = [p.label for p in prs]
        entry = {"n": len(prs),
                 "brier_base_rate": brier([base_const] * len(prs), y_all)}
        xs, ys = [], []
        for p in prs:
            x = surprise.get((p.symbol, p.reportDate))
            if x is None:
                continue
            xs.append(x)
            ys.append(p.label)
        entry["n_logistic"] = len(xs)
        entry["no_surprise"] = len(prs) - len(xs)
        if xs:
            preds = metrics.logistic_surprise(
                tx, ty, np.clip(np.array(xs, dtype=float), -200, 200))
            entry["brier_logistic"] = brier(list(preds), ys)
        analysis.baselines[key] = entry


def compute_rq3_gate(analysis: Analysis) -> None:
    """The executable RQ3 level gate (plan §6.2, D31/II-2).

    For each communicating structure S and model: on the events where S AND its
    baseline arm are usable at BOTH levels (so all four terms describe the same
    events), the controlled level effect is
        delta_S = [B_S(L2) - B_S(L1)] - [B_ctrl(L2) - B_ctrl(L1)]
    with ctrl rebuilt from the first k = min(runsUsed L1, L2) pool runs on both
    sides - level 1 never averages more runs than level 2. A level statement
    about S requires the sign of delta_S to agree across both models; that
    verdict is rendered, not decided here.
    """
    by_arm: dict = defaultdict(dict)
    for r in analysis.rows:
        if r.brier is None:
            continue
        by_arm[(r.modelKey, r.arm)].setdefault((r.symbol, r.reportDate), {})[r.level] = r

    for s in COMMUNICATING:
        for (mk, arm), events in sorted(by_arm.items()):
            if arm != s:
                continue
            base_events = by_arm.get((mk, BASELINE_ARM[s]), {})
            rows_out = []
            for ev, lv in sorted(events.items()):
                if "L1" not in lv or "L2" not in lv:
                    continue
                blv = base_events.get(ev, {})
                if "L1" not in blv or "L2" not in blv:
                    continue
                r1, r2 = blv["L1"], blv["L2"]
                k = min(r1.runsUsed or 0, r2.runsUsed or 0)
                if k < 1:
                    continue
                pool1 = analysis.pools[(r1.eventId, mk)]
                pool2 = analysis.pools[(r2.eventId, mk)]
                if k > len(pool1) or k > len(pool2):
                    abort(f"{ev} / {mk} / {s}: gate control needs {k} runs but the "
                          f"pools hold {len(pool1)} and {len(pool2)}")
                rows_out.append((lv["L1"].probability, lv["L2"].probability,
                                 ensemble_mean(pool1[:k]), ensemble_mean(pool2[:k]),
                                 lv["L1"].label))
            if not rows_out:
                continue
            y = [r[4] for r in rows_out]
            bs1 = brier([r[0] for r in rows_out], y)
            bs2 = brier([r[1] for r in rows_out], y)
            bc1 = brier([r[2] for r in rows_out], y)
            bc2 = brier([r[3] for r in rows_out], y)
            analysis.rq3_gate[(mk, s)] = {
                "n": len(rows_out),
                "delta_raw": bs2 - bs1,
                "delta_ctrl": bc2 - bc1,
                "delta_controlled": (bs2 - bs1) - (bc2 - bc1),
            }


def compute_provider_split(analysis: Analysis) -> None:
    """Plan §8.1 (D29/D31): the RQ1 paired difference per provider half.

    Only cells whose pairs carry >= 2 distinct providers are split - in this
    design that is the split model (qwen) only; single-provider models produce
    no row here rather than a misleading one. Pre-D29 outcomes carry no
    provider at all; the render says so instead of showing an empty table.
    """
    groups: dict = defaultdict(lambda: defaultdict(list))
    for pr in analysis.pairs:
        if pr.provider:
            groups[(pr.modelKey, pr.level, pr.structure)][pr.provider].append(
                single_brier(pr.p_structure, pr.label)
                - single_brier(pr.p_baseline, pr.label))
    for key, per in sorted(groups.items()):
        if len(per) < 2:
            continue
        halves = {prov: {"n": len(d), "mean_paired_diff": float(np.mean(d))}
                  for prov, d in sorted(per.items())}
        signs = {v["mean_paired_diff"] > 0 for v in halves.values()
                 if v["mean_paired_diff"] != 0}
        analysis.provider_split[key] = {"halves": halves,
                                        "sign_flip": len(signs) > 1}


# --------------------------------------------------------------------------- #
# 8. Driver
# --------------------------------------------------------------------------- #
def read_event_sample(path: Path) -> set[tuple[str, str]]:
    """The pinned (symbol, report_date) pairs of event_sample.txt.

    Comment lines and the header are skipped; anything else must be exactly
    `SYMBOL,YYYY-MM-DD` - a malformed line aborts, because a silently skipped
    line would make the drawn-vs-booked arithmetic below quietly wrong.
    """
    pairs: set[tuple[str, str]] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line == "symbol,report_date":
            continue
        parts = line.split(",")
        if len(parts) != 2 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
            abort(f"{path}:{lineno}: not a SYMBOL,DATE line: {raw!r}")
        pairs.add((parts[0], parts[1]))
    return pairs


def compute_sample_info(a: Analysis, sample_path: Path) -> None:
    """Drawn vs booked, per level - the run-side rows of the §7 flow table.

    `booked` counts distinct drawn events with at least one scored row at that
    level; `missing` is the drawn remainder; `extraneous` counts booked events
    that are NOT in the sample (zero on the real run; nonzero when dev data is
    scored with --sample, which is worth seeing rather than hiding).
    """
    drawn = read_event_sample(sample_path)
    info: dict = {"path": str(sample_path), "drawn": len(drawn), "levels": {}}
    for level in sorted({r.level for r in a.rows}):
        booked_events = {(r.symbol, r.reportDate) for r in a.rows if r.level == level}
        info["levels"][level] = {
            "booked": len(booked_events & drawn),
            "missing": len(drawn - booked_events),
            "extraneous": len(booked_events - drawn),
        }
    a.sample_info = info


def _git_commit(anywhere_inside_repo: Path) -> str:
    """The short commit of the repo containing `anywhere_inside_repo`.

    IV-2 (D31): the scorer records its own version in results.md so a later
    reader can tell WHICH scorer produced the numbers. 'unknown' rather than a
    crash when git is unavailable - version recording must never block scoring.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(anywhere_inside_repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def analyse(run_specs: list[tuple[Path, Path]], db_path: Path,
            exploratory: bool = False, sample_path: Path | None = None,
            exclude: set[tuple[str, str]] | None = None,
            exclude_label: str = "") -> Analysis:
    truth = load_truth(db_path)
    runs = [load_run(o, t) for o, t in run_specs]
    a = Analysis()
    a.scorer_commit = _git_commit(HERE)
    a.sample_flow = sample_flow_counts(db_path)
    build_rows(runs, truth, a, exclude=exclude, exclude_label=exclude_label)
    if sample_path is not None:
        compute_sample_info(a, sample_path)
    compute_rq1(a)
    compute_rq2(runs, a)
    compute_rq3(a)
    # Block III (D31): committed additions, all unconditional.
    compute_tare(a)
    compute_baselines(a, db_path)
    compute_reference_curve(a)
    compute_rq3_gate(a)
    compute_provider_split(a)
    if exploratory:
        a.exploratory_on = True
    return a


def write_scored(a: Analysis, out_dir: Path) -> Path:
    """One line per (eventId, modelKey, arm). No model text, on purpose.

    The transcripts run about 162 KB per event per model pair - roughly 260 MB at
    full scale - and they already exist in `*_ledger5.jsonl`. What this file
    carries instead is (eventId, modelKey, structure), which is exactly the key
    that reads them back out; `--case` does that.
    """
    path = out_dir / "scored.jsonl"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in a.rows:
            fh.write(json.dumps(r.to_json()) + "\n")
    return path


def render_results(a: Analysis) -> str:
    L = []
    w = L.append
    w("# Experiment results")
    w("")
    w("Generated by `scripts/24_score_experiment.py`. Every probability is scored")
    w("with `brier()` and decomposed with `murphy()` from `scripts/21_metrics.py`;")
    w("this file contains no second copy of either.")
    w("")
    w(f"Scorer commit: `{a.scorer_commit}` (IV-2/D31: the version that produced")
    w("these numbers; a scorer change between run start and scoring is a logged")
    w("deviation).")
    w("")
    if a.excluded is not None:
        drops = sum(a.excluded["dropped"].values())
        per = ", ".join(f"{mk}/{lvl}: {n}"
                        for (mk, lvl), n in sorted(a.excluded["dropped"].items()))
        w(f"**SENSITIVITY RECOMPUTATION (plan §8.2):** {a.excluded['listed']} listed")
        w(f"events excluded before scoring — reason: {a.excluded['label'] or '(none given)'}.")
        w(f"Event-model-level records dropped: {drops}" + (f" ({per})." if per else "."))
        w("This file is NOT the primary result; compare it against the run without")
        w("the exclusion.")
        w("")

    w("## Where the sample went")
    w("")
    sf = a.sample_flow
    d_release = sf["total"] - sf["after_release"]
    d_index = sf["after_release"] - sf["eligible"]
    n_dropped = sum(a.dropped.values())
    n_zero = sum(a.zero_runs.values())
    n_nopair = sum(a.no_pair.values())
    w("```")
    w(f"all events in the database                      {sf['total']:>5}")
    w(f"  - t0 before the model release (leakage rule)  {-d_release:>5}   -> {sf['after_release']}")
    w(f"  - not in the index at t0                      {-d_index:>5}   -> {sf['eligible']}")
    w(f"  = events entering the experiment                    {sf['eligible']:>6}")
    if a.sample_info is not None:
        si = a.sample_info
        w(f"  drawn for the run (pinned sample)             {si['drawn']:>5}")
        for lvl, c in sorted(si["levels"].items()):
            extra = f", {c['extraneous']} booked outside the sample" if c["extraneous"] else ""
            w(f"    {lvl}: booked {c['booked']}, missing {c['missing']}{extra}")
    w(f"  - structures dropped (malformed)              {n_dropped:>5}")
    w(f"  - comparisons with runsUsed = 0               {n_zero:>5}")
    w(f"  - comparisons with no baseline drawn at all   {n_nopair:>5}")
    w(f"  - for RQ3: events present in L1 and L2        {a.rq3_common_events:>5}")
    w("```")
    w("")
    n_events = len({(r.symbol, r.reportDate) for r in a.rows})
    n_models = len({r.modelKey for r in a.rows})
    w("The first three lines are counted in `thesis.db`; the rest are counted from")
    w("the run. The line `events entering the experiment` is what the FULL run will")
    w(f"cover. This run covered {n_events} of them, on {n_models} model(s), so the")
    w("lines below it describe those events only.")
    if a.dropped:
        w("")
        w("Dropped structures, by level / model / structure:")
        w("")
        for k, v in sorted(a.dropped.items()):
            w(f"- {k[0]} / {k[1]} / {k[2]}: {v}")
    if a.zero_runs:
        w("")
        w("Comparisons excluded because no single-agent run fitted the budget:")
        w("")
        for k, v in sorted(a.zero_runs.items()):
            w(f"- {k[0]} / {k[1]} / {k[2]}: {v}")
    w("")

    w("## Provenance of the token counts")
    w("")
    for line in a.provenance:
        w(f"- {line}")
    w("")

    w("## RQ1 - structure against the compute-matched baseline, paired")
    w("")
    w("Both sides answered the same event under the same token budget. A negative")
    w("`diff` means the structure scored LOWER, i.e. better. `wins` counts the")
    w("events on which the structure beat its baseline.")
    w("")
    w("| model | level | structure | n | brier struct | brier base | diff | wins | mean runs |")
    w("|---|---|---|---:|---:|---:|---:|---:|---:|")
    for (mk, lvl, s), v in sorted(a.rq1.items()):
        w(f"| {mk} | {lvl} | {s} | {v['n']} | {v['brier_structure']:.4f} | "
          f"{v['brier_baseline']:.4f} | {v['mean_paired_diff']:+.4f} | "
          f"{v['wins']}/{v['n']} | {v['mean_runs_used']:.1f} |")
    w("")
    w("### Murphy decomposition of each side")
    w("")
    w("`brier` is the score of the BINNED forecast, which is the one the identity")
    w("`brier = reliability - resolution + uncertainty` describes; `raw` is the")
    w("forecast as issued and `gap` the difference. The identity is asserted before")
    w("any of these rows is printed.")
    w("")
    # Twenty is a READING AID, not a criterion: it decides whether the caveat
    # below is printed and nothing else. It filters no row and changes no
    # number. Do not promote it to a rule by quoting it in the thesis - the
    # sample size at which a result is worth interpreting is a methodological
    # choice that belongs in the pre-registered analysis plan, fixed before the
    # run rather than read off this constant afterwards.
    small = [k for k, v in a.rq1.items() if v["n"] < 20]
    if small:
        w(f"**Read the decomposition with care here.** {len(small)} of {len(a.rq1)} rows")
        w("rest on fewer than 20 events. Reliability, resolution and uncertainty are")
        w("all computed by grouping forecasts into bins and asking how often the")
        w("events in each bin happened - with a handful of events there is nothing to")
        w("group. At n = 1 the arithmetic degenerates completely: the base rate is 0")
        w("or 1, so uncertainty and resolution are exactly 0 and reliability simply")
        w("equals the Brier score. Those rows are not wrong, they are empty, and they")
        w("only become informative at the full-run sample size.")
        w("")
        w("**Twenty is a reading aid, not a criterion.** Nothing is filtered by it and")
        w("no number below changes because of it; every row is reported either way,")
        w("and the count that matters is printed in each row's own `n` column. The")
        w("sample size at which a result is worth interpreting is a methodological")
        w("question and belongs in the pre-registered analysis plan, decided before")
        w("the run rather than inferred afterwards from this paragraph.")
        w("")
    w("| model | level | structure | side | brier | raw | gap | reliability | resolution | uncertainty | empty bins | singletons |")
    w("|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for (mk, lvl, s), v in sorted(a.rq1.items()):
        for side, dk in (("structure", "murphy_structure"), ("baseline", "murphy_baseline")):
            d = v[dk]
            w(f"| {mk} | {lvl} | {s} | {side} | {d['brier']:.4f} | {d['brier_raw']:.4f} | "
              f"{d['binning_gap']:+.5f} | {d['reliability']:.4f} | {d['resolution']:.5f} | "
              f"{d['uncertainty']:.4f} | {d['empty_bins']} | {d['singleton_bins']} |")
    w("")
    w("### The reference baselines on the same events (plan §4, D31)")
    w("")
    bm = a.baselines_meta
    if bm.get("note"):
        w(f"Not computed: {bm['note']} (n_train = {bm.get('n_train', 0)}).")
        w("")
    elif a.baselines:
        w(f"Both fitted on the {bm['n_train']} events with t0 <= {bm['cutoff']} —")
        w("strictly before the experiment window, so no scored event is ever in the")
        w(f"training data. Training base rate {bm['train_base_rate']:.4f}. Orientation")
        w("only: neither baseline gates anything (D31/II-1).")
        w("")
        w("| model | level | structure | n | brier base rate | n logistic | brier logistic | no surprise |")
        w("|---|---|---|---:|---:|---:|---:|---:|")
        for (mk, lvl, s), v in sorted(a.baselines.items()):
            bl = f"{v['brier_logistic']:.4f}" if "brier_logistic" in v else "—"
            w(f"| {mk} | {lvl} | {s} | {v['n']} | {v['brier_base_rate']:.4f} | "
              f"{v['n_logistic']} | {bl} | {v['no_surprise']} |")
        w("")
    w("### The tare check on the matching machinery (plan §4, D31/II-3)")
    w("")
    if not a.tare:
        w("Not computable: no event carried per-run token counts (pre-D29 outcomes).")
        w("")
    else:
        w("The prefix-by-budget engine fed the repetition rung's own billed cost must")
        w("hand back the rung itself — difference zero. Reported, not gated: no")
        w("pass/fail verdict; a conspicuous non-zero is investigated, not adjudicated.")
        w("")
        w("| model | level | n | exact reproductions | prefix != workers | mean diff | max abs diff |")
        w("|---|---|---:|---:|---:|---:|---:|")
        for (mk, lvl), v in sorted(a.tare.items()):
            md = f"{v['mean_diff']:+.6f}" if v["mean_diff"] is not None else "—"
            mx = f"{v['max_abs_diff']:.6f}" if v["max_abs_diff"] is not None else "—"
            w(f"| {mk} | {lvl} | {v['n']} | {v['n_exact']} | "
              f"{v['n_prefix_ne_workers']} | {md} | {mx} |")
        w("")
    w("### The rungs outside the pairing, for context")
    w("")
    w("These are NOT compute-matched and NOT comparable to each other - repetition")
    w("here is three workers, debate is nine calls. They say what each rung scored")
    w("on every event where it produced a number.")
    w("")
    w("| model | level | rung | n | brier |")
    w("|---|---|---|---:|---:|")
    for (mk, lvl, s), v in sorted(a.rq1_unmatched.items()):
        w(f"| {mk} | {lvl} | {s} | {v['n']} | {v['brier']:.4f} |")
    w("")

    w("## RQ2 - communication cost, in tokens")
    w("")
    w("Read verbatim from `*_structure_tokens.jsonl`; this file performs no")
    w("arithmetic on them beyond adding across events. Absolute counts, not shares:")
    w("a share would hide the difference between a debate spending six thousand")
    w("tokens on communication and one spending sixty thousand.")
    w("")
    w("`commCost` is what an agent read that came from other agents (A + B).")
    w("`B_SELF` - an agent re-reading its OWN earlier message - is listed separately,")
    w("because whether reading yourself counts as communication is a definition and")
    w("not a measurement.")
    w("")
    w("The HEADLINE figure (D30) is `billedCommCost` - A + B + the combiner's full")
    w("cost, as billed, excluding B_SELF. The inclusive variants stand beside it in")
    w("the same table, per the plan's fragility rule: a statement that changes truth")
    w("value between the exclusive and the inclusive column is fragile, not settled.")
    w("")
    w("**Cost per event (means over n events of the cell):**")
    w("")
    w("| model | level | structure | n | commCost | **billedCommCost** | B_SELF | commCostInclSelf | billedCommCostInclSelf | total | billedTotal |")
    w("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for (mk, lvl, s), v in sorted(a.rq2.items()):
        w(f"| {mk} | {lvl} | {s} | {v['n']} | {v['mean_commCost']:.1f} | "
          f"**{v['mean_billedCommCost']:.1f}** | {v['mean_B_SELF']:.1f} | "
          f"{v['mean_commCostInclSelf']:.1f} | {v['mean_billedCommCostInclSelf']:.1f} | "
          f"{v['mean_total']:.1f} | {v['mean_billedTotal']:.1f} |")
    w("")
    w("**Run totals (summed over the cell's events):**")
    w("")
    w("| model | level | structure | commCost | billedCommCost | B_SELF | commCostInclSelf | billedCommCostInclSelf | total | billedTotal |")
    w("|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for (mk, lvl, s), v in sorted(a.rq2.items()):
        w(f"| {mk} | {lvl} | {s} | {v['total_commCost']} | {v['total_billedCommCost']} | "
          f"{v['total_B_SELF']} | {v['total_commCostInclSelf']} | "
          f"{v['total_billedCommCostInclSelf']} | {v['total_total']} | {v['total_billedTotal']} |")
    w("")
    w("**Five-bucket breakdown (mean tokens per event; D/A/B/B_SELF are what a model")
    w("read, C1/C2 what it wrote - the identities D+A+B+B_SELF = prompt and")
    w("C1+C2 = completion hold per call in the ledger):**")
    w("")
    w("| model | level | structure | D | A | B | B_SELF | C1 | C2 |")
    w("|---|---|---|---:|---:|---:|---:|---:|---:|")
    for (mk, lvl, s), v in sorted(a.rq2.items()):
        w(f"| {mk} | {lvl} | {s} | {v['mean_D']:.0f} | {v['mean_A']:.0f} | "
          f"{v['mean_B']:.0f} | {v['mean_B_SELF']:.0f} | {v['mean_C1']:.0f} | {v['mean_C2']:.0f} |")
    w("")

    w("## RQ3 - level 2 minus level 1")
    w("")
    w("Computed on the common event set only (decision D17): an event counts only")
    w("if BOTH levels produced a usable number for that arm. A negative delta means")
    w("level 2 scored better.")
    w("")
    w("| model | arm | n common | brier L1 | brier L2 | delta | L1 only | L2 only |")
    w("|---|---|---:|---:|---:|---:|---:|---:|")
    for (mk, arm), v in sorted(a.rq3.items()):
        w(f"| {mk} | {arm} | {v['n']} | {v['brier_L1']:.4f} | {v['brier_L2']:.4f} | "
          f"{v['delta']:+.4f} | {v['n_L1_only']} | {v['n_L2_only']} |")
    w("")
    w("### The equal-runs control (D23)")
    w("")
    w("A level-2 prompt is longer, so the same budget buys fewer single-agent runs")
    w("at level 2 than at level 1. Without this control the baseline delta above")
    w("mixes 'more context' with 'a thinner ensemble'. Here both sides are rebuilt")
    w("from the first k runs of their own pool, k being the smaller of the two run")
    w("counts, so only the context differs.")
    w("")
    w("| model | structure | n | runs each side | brier L1 | brier L2 | delta |")
    w("|---|---|---:|---:|---:|---:|---:|")
    for (mk, s), v in sorted(a.rq3_control.items()):
        w(f"| {mk} | {s} | {v['n']} | {v['runs_each_side']:.1f} | {v['brier_L1']:.4f} | "
          f"{v['brier_L2']:.4f} | {v['delta']:+.4f} |")
    w("")

    w("### The level gate (plan §6.2, D31/II-2)")
    w("")
    if not a.rq3_gate:
        w("Not computable: no structure has both levels plus a usable equal-runs")
        w("control on a common event set.")
        w("")
    else:
        w("delta_S = [structure's level delta] - [equal-runs baseline's level delta],")
        w("all four terms on the SAME events. A level statement about a structure")
        w("requires the sign of delta_S to agree across both models.")
        w("")
        w("| model | structure | n | delta raw | delta control | **delta_S (controlled)** |")
        w("|---|---|---:|---:|---:|---:|")
        for (mk, s), v in sorted(a.rq3_gate.items()):
            w(f"| {mk} | {s} | {v['n']} | {v['delta_raw']:+.4f} | "
              f"{v['delta_ctrl']:+.4f} | **{v['delta_controlled']:+.4f}** |")
        w("")
        models = sorted({mk for (mk, _) in a.rq3_gate})
        for s in sorted({s for (_, s) in a.rq3_gate}):
            per = {mk: a.rq3_gate[(mk, s)]["delta_controlled"]
                   for mk in models if (mk, s) in a.rq3_gate}
            if len(per) < 2:
                w(f"- {s}: only one model present — cross-model consistency not evaluable;"
                  " no level statement.")
                continue
            signs = {d > 0 for d in per.values() if d != 0}
            if len(signs) == 1 and all(d != 0 for d in per.values()):
                direction = ("level 2 worse" if signs.pop() else "level 2 better")
                w(f"- {s}: signs agree across models ({direction}) — a level statement"
                  " is PERMITTED by the gate (wording per reading rules, §4).")
            else:
                w(f"- {s}: signs do NOT agree across models — no level statement.")
        w("")

    w("### Baseline reference curve (committed, D31)")
    w("")
    w("The single-agent pool scored at every prefix length k, per model and level —")
    w("the context that makes the equal-runs control checkable (plan §6.2).")
    w("")
    for (mk, lvl), curve in sorted(a.reference_curve.items()):
        w(f"**{mk} / {lvl}**")
        w("")
        w("| runs k | events | brier |")
        w("|---:|---:|---:|")
        for pt in curve:
            w(f"| {pt['k']} | {pt['n_events']} | {pt['brier']:.4f} |")
        w("")

    w("## Provider halves (plan §8.1, D29/D31)")
    w("")
    if not a.provider_split:
        if any(p.provider for p in a.pairs):
            w("No cell has two providers — every model was served by a single")
            w("endpoint in this data; there is nothing to split.")
        else:
            w("Not computable: the outcomes carry no provider field (pre-D29")
            w("harness output).")
        w("")
    else:
        w("The RQ1 paired difference recomputed per serving provider, same pairs as")
        w("the primary cells. Descriptive; a sign flip forces the 'not robust' label")
        w("on that cell's finding (plan §8, D31/II-7).")
        w("")
        w("| model | level | structure | provider | n | mean paired diff | sign flip? |")
        w("|---|---|---|---|---:|---:|---|")
        for (mk, lvl, s), v in sorted(a.provider_split.items()):
            for prov, h in sorted(v["halves"].items()):
                flip = "**YES**" if v["sign_flip"] else "no"
                w(f"| {mk} | {lvl} | {s} | {prov} | {h['n']} | "
                  f"{h['mean_paired_diff']:+.4f} | {flip} |")
        w("")

    if a.exploratory_on:
        w("## EXPLORATORY - the averaging-alone reading of the reference curve")
        w("")
        w("The curve itself is committed (§6.2 above). Reading it as 'a debate")
        w("advantage that disappears once the baseline is given the same number of")
        w("runs was averaging, not coordination' is an interpretation the exposé")
        w("does not commit to. EXPLORATORY: reported as such, never a headline.")
        w("")
        for (mk, lvl), curve in sorted(a.reference_curve.items()):
            if not curve:
                continue
            k1 = curve[0]["brier"]
            kend = curve[-1]
            w(f"- EXPLORATORY {mk} / {lvl}: brier {k1:.4f} at k=1 -> "
              f"{kend['brier']:.4f} at k={kend['k']} "
              f"(averaging alone moved it {kend['brier'] - k1:+.4f}).")
        w("")
    return "\n".join(L) + "\n"


def print_console(a: Analysis) -> None:
    print("\n=== provenance ===")
    for line in a.provenance:
        print("  " + line)

    sf = a.sample_flow
    print("\n=== sample flow ===")
    print(f"  all events in the database                      {sf['total']:>5}")
    print(f"    - t0 before the model release (leakage rule)  {sf['after_release'] - sf['total']:>5}"
          f"   -> {sf['after_release']}")
    print(f"    - not in the index at t0                      {sf['eligible'] - sf['after_release']:>5}"
          f"   -> {sf['eligible']}")
    print(f"    = events entering the experiment                    {sf['eligible']:>6}")
    print(f"    - structures dropped (malformed)              {sum(a.dropped.values()):>5}")
    print(f"    - comparisons with runsUsed = 0               {sum(a.zero_runs.values()):>5}")
    print(f"    - comparisons with no baseline drawn          {sum(a.no_pair.values()):>5}")
    print(f"    - for RQ3: events present in L1 and L2        {a.rq3_common_events:>5}")

    print("\n=== RQ1  structure vs compute-matched baseline (paired, same event) ===")
    print(f"  {'model':24} {'lvl':4} {'structure':11} {'n':>3} {'struct':>8} {'base':>8} "
          f"{'diff':>9} {'wins':>6} {'runs':>6}")
    for (mk, lvl, s), v in sorted(a.rq1.items()):
        print(f"  {mk:24} {lvl:4} {s:11} {v['n']:>3} {v['brier_structure']:>8.4f} "
              f"{v['brier_baseline']:>8.4f} {v['mean_paired_diff']:>+9.4f} "
              f"{str(v['wins']) + '/' + str(v['n']):>6} {v['mean_runs_used']:>6.1f}")

    print("\n  Murphy decomposition (identity asserted before printing)")
    print(f"  {'model':24} {'lvl':4} {'structure':11} {'side':9} {'brier':>8} {'raw':>8} "
          f"{'gap':>9} {'rel':>8} {'res':>9} {'unc':>8}")
    for (mk, lvl, s), v in sorted(a.rq1.items()):
        for side, dk in (("structure", "murphy_structure"), ("baseline", "murphy_baseline")):
            d = v[dk]
            print(f"  {mk:24} {lvl:4} {s:11} {side:9} {d['brier']:>8.4f} {d['brier_raw']:>8.4f} "
                  f"{d['binning_gap']:>+9.5f} {d['reliability']:>8.4f} {d['resolution']:>9.5f} "
                  f"{d['uncertainty']:>8.4f}")

    print("\n  the rungs outside the pairing (NOT compute-matched, context only)")
    for (mk, lvl, s), v in sorted(a.rq1_unmatched.items()):
        print(f"  {mk:24} {lvl:4} {s:11} n={v['n']:<3} brier {v['brier']:.4f}")

    print("\n=== RQ2  communication cost in tokens (verbatim from the token summary) ===")
    print(f"  {'model':24} {'lvl':4} {'structure':11} {'n':>3} {'commCost':>10} "
          f"{'billedComm':>11} {'B_SELF':>9} {'total':>10}")
    for (mk, lvl, s), v in sorted(a.rq2.items()):
        print(f"  {mk:24} {lvl:4} {s:11} {v['n']:>3} {v['total_commCost']:>10} "
              f"{v['total_billedCommCost']:>11} {v['total_B_SELF']:>9} {v['total_total']:>10}")

    print("\n=== RQ3  level 2 minus level 1, common event set only (D17) ===")
    if not a.rq3:
        print("  no event appears at both levels in this run - nothing to compare")
    print(f"  {'model':24} {'arm':30} {'n':>3} {'L1':>8} {'L2':>8} {'delta':>9}")
    for (mk, arm), v in sorted(a.rq3.items()):
        print(f"  {mk:24} {arm:30} {v['n']:>3} {v['brier_L1']:>8.4f} {v['brier_L2']:>8.4f} "
              f"{v['delta']:>+9.4f}")
    print("\n  equal-runs control (D23): both baselines rebuilt at k = min(runs L1, runs L2)")
    if not a.rq3_control:
        print("  no event pair with runs on both sides - control not computable")
    for (mk, s), v in sorted(a.rq3_control.items()):
        print(f"  {mk:24} {s:30} {v['n']:>3} {v['brier_L1']:>8.4f} {v['brier_L2']:>8.4f} "
              f"{v['delta']:>+9.4f}   k={v['runs_each_side']:.1f}")

    if a.exploratory_on:
        print("\n=== EXPLORATORY  averaging-alone reading of the committed curve ===")
        for (mk, lvl), curve in sorted(a.reference_curve.items()):
            pts = "  ".join(f"k={p['k']}:{p['brier']:.4f}(n={p['n_events']})" for p in curve)
            print(f"  EXPLORATORY {mk} {lvl}: {pts}")


# --------------------------------------------------------------------------- #
# 9. --case: everything about one event and one model
# --------------------------------------------------------------------------- #
def run_case(event_id: str, model_key: str, db_path: Path, search_dir: Path) -> int:
    """Print the arms, then every model call with its visible and hidden text.

    This is how the author quotes an individual case while writing, and it is the
    reason `scored.jsonl` can stay lean: the text is one command away.
    """
    truth = load_truth(db_path)
    symbol, report_date, level = parse_event_id(event_id)
    if (symbol, report_date) not in truth:
        abort(f"{event_id} does not resolve to any v_level1 row")
    label = truth[(symbol, report_date)]

    rec, src = None, None
    for path in sorted(search_dir.glob("*outcomes.jsonl")):
        for r in read_jsonl(path):
            if r.get("eventId") == event_id and r.get("modelKey") == model_key:
                rec, src = r, path
                break
        if rec:
            break
    if rec is None:
        abort(f"no outcomes record for {event_id} / {model_key} in {search_dir}")

    print(f"\n=== {event_id} / {model_key} ===")
    print(f"  symbol {symbol}   report date {report_date}   level {level}   label {label}")
    print(f"  read from {src.name}")

    print("\n  arms")
    print(f"  {'arm':32} {'prob':>8} {'label':>6} {'brier':>8}   note")
    outcomes = rec.get("outcomes") or {}
    matched = {m["structure"]: m for m in (rec.get("matched") or [])}
    lines: list[tuple[str, float | None, str]] = []
    for s in STRUCTURES:
        o = outcomes.get(s, {})
        note = "" if not o.get("dropped") else f"dropped: {o.get('dropReason', 'malformed')}"
        lines.append((s, o.get("probability"), note))
    for s in COMMUNICATING:
        mt = matched.get(s)
        if mt is None:
            lines.append((BASELINE_ARM[s], None, "no compute-matched pair drawn"))
        else:
            note = (f"runsUsed {mt['runsUsed']}, matched {mt['matchedTokens']} of "
                    f"{mt['structureTokens']} tokens, residual {mt['residualTokens']}")
            if not mt.get("runsUsed"):
                note += "  (excluded from RQ1)"
            lines.append((BASELINE_ARM[s], mt.get("probability"), note))
    for arm, p, note in lines:
        if p is None:
            print(f"  {arm:32} {'-':>8} {label:>6} {'-':>8}   {note}")
        else:
            pc = float(canonical(p))
            print(f"  {arm:32} {pc:>8.4f} {label:>6} {single_brier(pc, label):>8.4f}   {note}")

    ledgers = sorted(search_dir.glob("*ledger5.jsonl"))
    calls = []
    used = None
    for path in ledgers:
        hits = [r for r in read_jsonl(path)
                if r.get("eventId") == event_id and r.get("modelKey") == model_key]
        if hits:
            calls, used = hits, path
            break
    if used is None:
        print(f"\n  no *_ledger5.jsonl in {search_dir} holds this event - no text to show")
        return 0

    print(f"\n  {len(calls)} model calls, in file order, from {used.name}")
    for i, c in enumerate(calls, 1):
        print("\n" + "-" * 78)
        print(f"  call {i}/{len(calls)}  structure={c.get('structure')}  "
              f"agentRole={c.get('agentRole')}  round={c.get('round')}  "
              f"attempt={c.get('attempt')}  ok={c.get('ok')}  malformed={c.get('malformed')}")
        print(f"  probability={c.get('probability')}  totalTokens={c.get('totalTokens')}")
        print("\n  --- responseText (what the other agents could see) ---")
        print(c.get("responseText") or "  (empty)")
        hidden = c.get("reasoningText") or ""
        print("\n  --- reasoningText (hidden from the other agents) ---")
        print(hidden if hidden.strip() else "  (empty - this model returned no hidden reasoning)")
    return 0


# --------------------------------------------------------------------------- #
# 10. Self-test
# --------------------------------------------------------------------------- #
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          + (f"\n          {detail}" if detail and not condition else ""))


def check_aborts(name: str, fn, needle: str = "") -> None:
    """The rule must ABORT. A test that only checks 'it did not crash' is worthless."""
    try:
        fn()
    except IntegrityError as exc:
        if needle and needle.lower() not in str(exc).lower():
            check(name, False, f"aborted, but on the wrong thing: {exc}")
        else:
            check(name, True)
        return
    check(name, False, "did NOT abort - the violation would have been scored")


def _tok_row(eid, mk, structure, total, comm, billed_comm, b_self):
    """One token-summary row with the full field set the emitter writes."""
    return {"eventId": eid, "modelKey": mk, "structure": structure,
            "D": 1000, "A": 100, "B": comm - 100 if comm else 0, "B_SELF": b_self,
            "C1": 10, "C2": 500, "C2_hidden": 0,
            "commCost": comm, "commCostInclSelf": comm + b_self,
            "billedCommCost": billed_comm, "billedCommCostInclSelf": billed_comm + b_self,
            "total": total, "billedTotal": total, "prompt": total - 500,
            "completion": 500, "calls": 3, "malformedAttempts": 0, "costUsd": 0.001}


MODEL = "m-test"

# Hand-computed constants. Labels [1, 1, 0, 0]:
#   debate    0.7 0.6 0.4 0.3  -> (0.09+0.16+0.16+0.09)/4 = 0.125
#   baseline  0.6 0.6 0.5 0.5  -> (0.16+0.16+0.25+0.25)/4 = 0.205
#   paired difference                                     = -0.080
FIX_L1 = [
    # eid, label, pool (6 runs), debate p, matched debate (runsUsed, p)
    ("BRK-B_2026-01-05_L1", 1, [0.9, 0.9, 0.9, 0.15, 0.15, 0.5], 0.7, 5, 0.6),
    ("MSFT_2026-01-06_L1", 1, [0.9, 0.9, 0.9, 0.15, 0.15, 0.5], 0.6, 5, 0.6),
    # These two need a 3-run prefix mean of 0.1 (for the equal-runs control) and a
    # full-prefix mean of 0.5 (for the hand-computed baseline of 0.205), which
    # takes six runs: 0.3 + 2.7 = 3.0, and 3.0/6 = 0.5.
    ("AAPL_2026-01-07_L1", 0, [0.1, 0.1, 0.1, 0.9, 0.9, 0.9], 0.4, 6, 0.5),
    ("NVDA_2026-01-08_L1", 0, [0.1, 0.1, 0.1, 0.9, 0.9, 0.9], 0.3, 6, 0.5),
]
FIX_L2 = [
    ("BRK-B_2026-01-05_L2", 1, [0.5, 0.5, 0.5, 0.5], 0.8, 3, 0.5),
    ("MSFT_2026-01-06_L2", 1, [0.6, 0.6, 0.6, 0.6], 0.7, 3, 0.6),
    ("AAPL_2026-01-07_L2", 0, [0.4, 0.4, 0.4, 0.4], 0.3, 3, 0.4),
]


def _outcome_record(eid, pool, deb_p, deb_runs, deb_base, deb_tokens,
                    agg_tokens, run_tokens, deb_dropped=False):
    """One outcomes.jsonl line, shaped exactly like the harness writes it."""
    agg_runs = deb_runs
    agg_base = ensemble_mean(pool[:agg_runs]) if agg_runs else None
    rep = ensemble_mean(pool[:3])
    matched = [
        {"structure": "aggregator", "structureTokens": agg_tokens, "runsUsed": agg_runs,
         "matchedTokens": agg_runs * run_tokens,
         "residualTokens": agg_tokens - agg_runs * run_tokens,
         "probability": agg_base, "structureProbability": 0.5},
        {"structure": "debate", "structureTokens": deb_tokens, "runsUsed": deb_runs,
         "matchedTokens": deb_runs * run_tokens,
         "residualTokens": deb_tokens - deb_runs * run_tokens,
         "probability": deb_base,
         "structureProbability": None if deb_dropped else deb_p},
    ]
    deb_out = ({"probability": None, "dropped": True, "dropReason": "malformed"}
               if deb_dropped else {"probability": deb_p, "dropped": False})
    return {
        "modelKey": MODEL, "eventId": eid,
        "outcomes": {"repetition": {"probability": rep, "dropped": False},
                     "aggregator": {"probability": 0.5, "dropped": False},
                     "debate": deb_out},
        "roundProbabilities": [pool[:3]],
        "matched": matched,
        "repetitionRuns": [{"probability": p, "totalTokens": run_tokens} for p in pool],
    }


def _write_fixture(root: Path) -> dict:
    """Build a miniature but complete run: db, two levels, ledger, token summary."""
    db = root / "fixture.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE v_level1 (symbol TEXT, report_date TEXT, t0 TEXT, "
                "label INTEGER, in_index_at_t0 INTEGER, eps_surprise_pct REAL)")
    # Surprises for the test-side events; NVDA deliberately has none, so the
    # logistic baseline's exclusion counter is exercised.
    fix_surprise = {"BRK-B": 2.0, "MSFT": 1.0, "AAPL": -1.0, "NVDA": None}
    rows = []
    for eid, label, *_ in FIX_L1:
        sym, rd, _ = parse_event_id(eid)
        rows.append((sym, rd, "2026-01-20", label, 1, fix_surprise[sym]))
    rows += [
        ("AMD", "2026-01-09", "2026-01-20", 1, 1, 0.5),   # debate dropped here
        ("INTC", "2026-01-10", "2026-01-20", 0, 1, -0.5),  # runsUsed = 0 here
        ("NUL", "2026-01-11", "2026-01-20", None, 1, 0.0),  # NULL label, must abort
        ("OLD", "2025-01-01", "2025-01-02", 1, 1, 5.0),   # before the leakage cutoff
        ("OLDB", "2025-02-01", "2025-02-02", 0, 1, -3.0),  # before the leakage cutoff
        ("OUT", "2026-02-01", "2026-02-02", 1, 0, 1.0),   # not in the index at t0
        # Pre-cutoff training pool for the D31 baselines: with OLD and OLDB the
        # usable training set is 8 rows, 3 positive -> base rate 0.375 exactly.
        # Higher surprise leans positive but the classes overlap, so the
        # logistic fit cannot run away on separable data. TRG's NULL surprise
        # exercises the training-side exclusion.
        ("TRA", "2025-02-10", "2025-02-11", 1, 1, 4.0),
        ("TRB", "2025-02-17", "2025-02-18", 0, 1, 3.0),
        ("TRC", "2025-02-24", "2025-02-25", 0, 1, -2.0),
        ("TRD", "2025-03-03", "2025-03-04", 0, 1, -1.0),
        ("TRE", "2025-03-10", "2025-03-11", 1, 1, 0.5),
        ("TRF", "2025-03-17", "2025-03-18", 0, 1, 1.0),
        ("TRG", "2025-03-24", "2025-03-25", 1, 1, None),
    ]
    con.executemany("INSERT INTO v_level1 VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    ledger = root / "fix_ledger5.jsonl"
    ledger.write_bytes(b'{"a":1}\n{"a":2}\n{"a":3}\n')
    sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
    meta = {"_meta": {"ledgerPath": "C:\\somewhere\\else\\fix_ledger5.jsonl",
                      "ledgerLines": 3, "ledgerSha256": sha,
                      "harnessCommit": "fixture", "generatedAt": "2026-01-01T00:00:00Z"}}

    # --- level 1: the four hand-computed events, plus the two exclusion cases
    l1_records, l1_tokens = [], []
    for eid, _label, pool, deb_p, deb_runs, deb_base in FIX_L1:
        l1_records.append(_outcome_record(eid, pool, deb_p, deb_runs, deb_base,
                                          17000, 5000, 700))
    # A fifth event whose debate is dropped. The harness would not emit a matched
    # entry for a dropped structure, but this fixture does - with a null
    # structureProbability - so that a scorer which substituted 0.5 for the
    # missing forecast would be REACHED by the RQ1 loop and would report
    # (0.50 + 0.25)/5 = 0.15 instead of 0.125. A test that merely removed the
    # event could not tell that mistake from a correct exclusion.
    l1_records.append(_outcome_record("AMD_2026-01-09_L1", [0.55] * 6, 0.55, 5, 0.55,
                                      17000, 5000, 700, deb_dropped=True))
    # A sixth event where no single-agent run fitted the budget.
    r6 = _outcome_record("INTC_2026-01-10_L1", [0.5] * 6, 0.9, 5, 0.5, 17000, 5000, 700)
    for m in r6["matched"]:
        if m["structure"] == "debate":
            m.update({"runsUsed": 0, "matchedTokens": 0, "residualTokens": 17000,
                      "probability": None})
    l1_records.append(r6)
    for rec in l1_records:
        eid = rec["eventId"]
        l1_tokens.append(_tok_row(eid, MODEL, "repetition", 18000, 0, 0, 0))
        l1_tokens.append(_tok_row(eid, MODEL, "aggregator", 5000, 2000, 1900, 0))
        l1_tokens.append(_tok_row(eid, MODEL, "debate", 17000, 6000, 5800, 3000))

    l2_records, l2_tokens = [], []
    for eid, _label, pool, deb_p, deb_runs, deb_base in FIX_L2:
        l2_records.append(_outcome_record(eid, pool, deb_p, deb_runs, deb_base,
                                          40000, 12000, 3000))
        l2_tokens.append(_tok_row(eid, MODEL, "repetition", 20000, 0, 0, 0))
        l2_tokens.append(_tok_row(eid, MODEL, "aggregator", 12000, 2500, 2400, 0))
        l2_tokens.append(_tok_row(eid, MODEL, "debate", 40000, 7000, 6800, 3500))

    def dump(name, lines):
        p = root / name
        with open(p, "w", encoding="utf-8", newline="\r\n") as fh:   # CRLF, like the harness
            for ln in lines:
                fh.write(json.dumps(ln) + "\n")
        return p

    return {
        "db": db, "ledger": ledger, "meta": meta,
        "l1_out": dump("L1_outcomes.jsonl", l1_records),
        "l1_tok": dump("L1_structure_tokens.jsonl", [meta] + l1_tokens),
        "l2_out": dump("L2_outcomes.jsonl", l2_records),
        "l2_tok": dump("L2_structure_tokens.jsonl", [meta] + l2_tokens),
        "l1_records": l1_records, "l1_tokens": l1_tokens,
        "l2_records": l2_records, "l2_tokens": l2_tokens,
    }


def _redump(root: Path, name: str, lines: list) -> Path:
    p = root / name
    with open(p, "w", encoding="utf-8", newline="\r\n") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")
    return p


def self_test() -> int:
    root = Path(tempfile.mkdtemp(prefix="score24_"))
    fx = _write_fixture(root)
    db = fx["db"]

    def near(a, b):
        # Never test two decimals with ==; compare the gap against a tolerance.
        return abs(a - b) < 1e-12

    print("\n=== 1. The arithmetic, against hand-computed constants ===")
    a = analyse([(fx["l1_out"], fx["l1_tok"]), (fx["l2_out"], fx["l2_tok"])], db)
    r = a.rq1[(MODEL, "L1", "debate")]
    check("RQ1 L1 debate n = 4 (the dropped and the runsUsed=0 events are out)",
          r["n"] == 4, f"got n={r['n']}")
    check("RQ1 L1 debate structure brier = 0.125",
          near(r["brier_structure"], 0.125), f"got {r['brier_structure']!r}")
    check("RQ1 L1 debate baseline brier = 0.205",
          near(r["brier_baseline"], 0.205), f"got {r['brier_baseline']!r}")
    check("RQ1 L1 debate paired difference = -0.080",
          near(r["mean_paired_diff"], -0.080), f"got {r['mean_paired_diff']!r}")
    check("the Murphy identity holds on both sides of the pair",
          near(r["murphy_structure"]["brier"],
               r["murphy_structure"]["reliability"] - r["murphy_structure"]["resolution"]
               + r["murphy_structure"]["uncertainty"])
          and near(r["murphy_baseline"]["brier"],
                   r["murphy_baseline"]["reliability"] - r["murphy_baseline"]["resolution"]
                   + r["murphy_baseline"]["uncertainty"]))

    print("\n=== 2. Exclusions - each gives a DIFFERENT number, not just a different n ===")
    # If the dropped debate on AMD were scored at 0.5 the brier would be
    # (0.50 + 0.25)/5 = 0.15. 0.125 proves it was excluded, not merely absent.
    check("a dropped structure is excluded, not scored at 0.5 (0.125, not 0.15)",
          near(r["brier_structure"], 0.125) and not near(r["brier_structure"], 0.15))
    check("the drop is COUNTED, not silently absent",
          a.dropped[("L1", MODEL, "debate")] == 1,
          f"dropped counter = {dict(a.dropped)}")
    dropped_rows = [x for x in a.rows if x.eventId.startswith("AMD_") and x.arm == "debate"]
    check("the dropped structure still gets a row in scored.jsonl, with the reason",
          len(dropped_rows) == 1 and dropped_rows[0].brier is None
          and "dropped" in (dropped_rows[0].excludedReason or ""),
          f"{dropped_rows}")
    # If INTC (runsUsed=0, structure 0.9, label 0) were included the brier would
    # be (0.50 + 0.81)/5 = 0.262.
    check("runsUsed = 0 does not move RQ1 (0.125, not 0.262)",
          near(r["brier_structure"], 0.125) and not near(r["brier_structure"], 0.262))
    check("runsUsed = 0 is counted separately",
          a.zero_runs[("L1", MODEL, "debate")] == 1, f"{dict(a.zero_runs)}")

    print("\n=== 3. RQ3 - the common event set, and the equal-runs control ===")
    # Common = BRK-B, MSFT, AAPL. NVDA is level 1 only and must not enter.
    #   L1 debate 0.7 0.6 0.4 -> (0.09+0.16+0.16)/3 = 0.13666667
    #   L2 debate 0.8 0.7 0.3 -> (0.04+0.09+0.09)/3 = 0.07333333
    #   delta                                       = -0.06333333
    # Including NVDA at L1 would give 0.125 there and a delta of -0.05166667.
    d = a.rq3[(MODEL, "debate")]
    check("RQ3 uses the 3 common events, not all 6 at L1", d["n"] == 3, f"n={d['n']}")
    check("RQ3 debate L1 brier = 0.13666667 (NVDA excluded)",
          near(d["brier_L1"], 0.41 / 3), f"got {d['brier_L1']!r}")
    check("RQ3 debate delta = -0.06333333, not -0.05166667",
          near(d["delta"], 0.22 / 3 - 0.41 / 3), f"got {d['delta']!r}")
    # NVDA and INTC scored a debate at L1 and have no L2 counterpart. AMD's debate
    # was dropped, so it has no scored row at either level and is not counted here.
    check("RQ3 reports how many events were level-1 only", d["n_L1_only"] == 2,
          f"got {d['n_L1_only']}")
    # Uncontrolled baseline delta: L1 0.6 0.6 0.5 vs L2 0.5 0.6 0.4 -> 0.19 both,
    # delta 0.0000. At equal runs k=3: L1 becomes 0.9 0.9 0.1 -> 0.01, delta +0.18.
    du = a.rq3[(MODEL, "matched_baseline_debate")]
    dc = a.rq3_control[(MODEL, "debate")]
    check("uncontrolled baseline delta = 0.0000", near(du["delta"], 0.0), f"{du['delta']!r}")
    check("equal-runs control rebuilds at k = min(5, 3) = 3",
          near(dc["runs_each_side"], 3.0), f"got {dc['runs_each_side']!r}")
    check("equal-runs control L1 brier = 0.01 (the 3-run prefix, not the 5-run one)",
          near(dc["brier_L1"], 0.01), f"got {dc['brier_L1']!r}")
    check("equal-runs control delta = +0.18, not 0.0000",
          near(dc["delta"], 0.18), f"got {dc['delta']!r}")

    print("\n=== 4. The join ===")
    check("a hyphenated ticker parses (BRK-B)",
          parse_event_id("BRK-B_2026-01-05_L2") == ("BRK-B", "2026-01-05", "L2"))
    check("the level comes from the eventId, not the filename",
          all(x.level == "L2" for x in a.rows if x.eventId.endswith("_L2")))
    check_aborts("an eventId with no level suffix aborts",
                 lambda: parse_event_id("MSFT_2026-01-06"), "fewer than two underscores")
    check_aborts("an eventId whose middle part is not a date aborts",
                 lambda: parse_event_id("MSFT_notadate_L1"), "not a YYYY-MM-DD")
    check_aborts("an eventId with a junk level suffix aborts",
                 lambda: parse_event_id("MSFT_2026-01-06_XX"), "not a level")

    bad = list(fx["l1_records"]) + [_outcome_record("ZZZZ_2026-01-11_L1", [0.5] * 6, 0.5,
                                                    5, 0.5, 17000, 5000, 700)]
    bad_tok = list(fx["l1_tokens"])
    for s, tot in (("repetition", 18000), ("aggregator", 5000), ("debate", 17000)):
        bad_tok.append(_tok_row("ZZZZ_2026-01-11_L1", MODEL, s, tot, 0, 0, 0))
    check_aborts("an eventId not in v_level1 aborts",
                 lambda: analyse([(_redump(root, "bad_out.jsonl", bad),
                                   _redump(root, "bad_tok.jsonl", [fx["meta"]] + bad_tok))], db),
                 "does not resolve")

    nul = list(fx["l1_records"]) + [_outcome_record("NUL_2026-01-11_L1", [0.5] * 6, 0.5,
                                                    5, 0.5, 17000, 5000, 700)]
    nul_tok = list(fx["l1_tokens"])
    for s, tot in (("repetition", 18000), ("aggregator", 5000), ("debate", 17000)):
        nul_tok.append(_tok_row("NUL_2026-01-11_L1", MODEL, s, tot, 0, 0, 0))
    check_aborts("an event whose v_level1 label is NULL aborts",
                 lambda: analyse([(_redump(root, "nul_out.jsonl", nul),
                                   _redump(root, "nul_tok.jsonl", [fx["meta"]] + nul_tok))], db),
                 "label is null")

    dup = list(fx["l1_records"]) + [fx["l1_records"][0]]
    check_aborts("a duplicate eventId + modelKey aborts (it would double-count)",
                 lambda: analyse([(_redump(root, "dup_out.jsonl", dup), fx["l1_tok"])], db),
                 "appears twice")

    check_aborts("pairing L1 outcomes with L2 token counts aborts",
                 lambda: analyse([(fx["l1_out"], fx["l2_tok"])], db),
                 "do not describe the same run")

    hi = [json.loads(json.dumps(x)) for x in fx["l1_records"]]
    hi[0]["outcomes"]["debate"]["probability"] = 1.5
    check_aborts("a forecast outside [0,1] aborts, via _checked",
                 lambda: analyse([(_redump(root, "hi_out.jsonl", hi), fx["l1_tok"])], db),
                 "must lie in [0, 1]")

    print("\n=== 5. Provenance of the token counts ===")
    check("a correct header verifies against the ledger beside it",
          any("provenance OK" in p for p in a.provenance), f"{a.provenance}")
    bad_sha = json.loads(json.dumps(fx["meta"]))
    bad_sha["_meta"]["ledgerSha256"] = "0" * 64
    check_aborts("a wrong ledger sha aborts",
                 lambda: analyse([(fx["l1_out"],
                                   _redump(root, "sha_tok.jsonl", [bad_sha] + fx["l1_tokens"]))], db),
                 "hashes to")
    bad_lines = json.loads(json.dumps(fx["meta"]))
    bad_lines["_meta"]["ledgerLines"] = 99
    check_aborts("a wrong ledger line count aborts",
                 lambda: analyse([(fx["l1_out"],
                                   _redump(root, "lin_tok.jsonl", [bad_lines] + fx["l1_tokens"]))], db),
                 "lines")
    # A header line that is present but carries no _meta. Dropping line 1 instead
    # would let the "all three structures present" check fire first, and then the
    # test would be passing on the strength of a different guard.
    check_aborts("a token summary with no _meta header aborts",
                 lambda: analyse([(fx["l1_out"],
                                   _redump(root, "nom_tok.jsonl",
                                           [{"note": "hand-made, no provenance"}] + fx["l1_tokens"]))], db),
                 "no _meta")
    absent = json.loads(json.dumps(fx["meta"]))
    absent["_meta"]["ledgerPath"] = "C:\\nowhere\\absent_ledger5.jsonl"
    a_skip = analyse([(fx["l1_out"],
                       _redump(root, "abs_tok.jsonl", [absent] + fx["l1_tokens"]))], db)
    check("a ledger that is not beside the summary SKIPS the check and says so",
          any("NOT CHECKED" in p for p in a_skip.provenance), f"{a_skip.provenance}")
    check("the skip appears in results.md rather than staying silent",
          "NOT CHECKED" in render_results(a_skip))

    print("\n=== 6. RQ2 is a pass-through, not a derivation ===")
    base_total = a.rq2[(MODEL, "L1", "debate")]["total_commCost"]
    bumped = [json.loads(json.dumps(t)) for t in fx["l1_tokens"]]
    for t in bumped:
        if t["structure"] == "debate" and t["eventId"].startswith("BRK-B"):
            t["commCost"] += 1000
            break
    a2 = analyse([(fx["l1_out"], _redump(root, "bump_tok.jsonl", [fx["meta"]] + bumped))], db)
    moved = a2.rq2[(MODEL, "L1", "debate")]["total_commCost"] - base_total
    check("commCost + 1000 in the input moves the reported total by exactly 1000",
          moved == 1000, f"it moved by {moved}")
    check("B_SELF is reported separately from commCost",
          a.rq2[(MODEL, "L1", "debate")]["total_B_SELF"] == 6 * 3000
          and a.rq2[(MODEL, "L1", "debate")]["total_commCost"] == 6 * 6000,
          f"{a.rq2[(MODEL, 'L1', 'debate')]}")
    # 2026-09-05 (review L2-05): the plan's §5.1 promises all seven cost fields
    # AND the five-bucket breakdown per cell. These checks go red if a field is
    # dropped from RQ2_FIELDS or a column disappears from the render.
    deb = a.rq2[(MODEL, "L1", "debate")]
    check("the inclusive variants are accumulated (commCostInclSelf = comm + B_SELF)",
          deb["total_commCostInclSelf"] == 6 * (6000 + 3000), f"{deb}")
    check("the read buckets are accumulated (total_D = 6 events x 1000)",
          deb["total_D"] == 6 * 1000 and deb["total_A"] == 6 * 100
          and deb["total_C1"] == 6 * 10 and deb["total_C2"] == 6 * 500, f"{deb}")
    _rendered_rq2 = render_results(a)
    check("results.md renders the inclusive columns",
          "billedCommCostInclSelf" in _rendered_rq2 and "commCostInclSelf" in _rendered_rq2)
    check("results.md renders the five-bucket breakdown table",
          "Five-bucket breakdown" in _rendered_rq2
          and "| D | A | B | B_SELF | C1 | C2 |" in _rendered_rq2)

    print("\n=== 6b. the --sample drawn/booked/missing bookkeeping (plan §7) ===")
    # Five drawn pairs: the four FIX_L1 events plus one that never ran. AMD and
    # INTC DID run at L1 but are not drawn -> extraneous. Expected L1 line:
    # booked 4 of 5, missing 1 (XONE), extraneous 2 (AMD, INTC).
    sample_file = root / "sample_fixture.txt"
    sample_file.write_text(
        "# comment line\nsymbol,report_date\nBRK-B,2026-01-05\nMSFT,2026-01-06\n"
        "AAPL,2026-01-07\nNVDA,2026-01-08\nXONE,2026-01-12\n", encoding="utf-8")
    a_s = analyse([(fx["l1_out"], fx["l1_tok"]), (fx["l2_out"], fx["l2_tok"])], db,
                  sample_path=sample_file)
    si = a_s.sample_info
    check("5 pairs drawn from the sample file (comments and header skipped)",
          si is not None and si["drawn"] == 5, f"{si}")
    check("L1: booked 4, missing 1, extraneous 2 (AMD + INTC ran but are undrawn)",
          si["levels"]["L1"] == {"booked": 4, "missing": 1, "extraneous": 2},
          f"{si['levels']}")
    check("the drawn line appears in results.md only when --sample is given",
          "drawn for the run (pinned sample)" in render_results(a_s)
          and "drawn for the run" not in render_results(a))
    sample_file.write_text("BRK-B;2026-01-05\n", encoding="utf-8")
    check_aborts("a malformed sample line aborts instead of being skipped",
                 lambda: read_event_sample(sample_file),
                 "not a SYMBOL,DATE line")

    print("\n=== 7. The recombination cross-check ===")
    broken = [json.loads(json.dumps(x)) for x in fx["l1_records"]]
    broken[0]["matched"][1]["probability"] = 0.61      # not the mean of its first 5 runs
    check_aborts("a baseline probability that is not the mean of its own runs aborts",
                 lambda: analyse([(_redump(root, "rec_out.jsonl", broken), fx["l1_tok"])], db),
                 "mean of its first")
    mism = [json.loads(json.dumps(x)) for x in fx["l1_records"]]
    mism[0]["matched"][1]["structureTokens"] = 17001
    check_aborts("matched.structureTokens disagreeing with the token summary aborts",
                 lambda: analyse([(_redump(root, "tok_out.jsonl", mism), fx["l1_tok"])], db),
                 "disagree about what this structure cost")

    # 7b (D32 deviation 5, 2026-09-09): a structure that contains a RETRIED call.
    # The pre-registered budget is BILLED tokens, discarded attempts included
    # (plan §2 table "compute matching", DATA_FLOW: "`billedTotal` ... is the
    # figure compute matching uses"), and the harness accordingly writes
    # matched.structureTokens = billedTotal. Every dry run had 0 malformed
    # attempts, so total == billedTotal and the check above passed while
    # comparing against the wrong column. The real run has 10 such structures;
    # the first (PAYC_2025-11-05_L1 / qwen / debate: 36481 billed vs 33496
    # usable) stopped the scorer at its first real invocation.
    retried_tok = [json.loads(json.dumps(x)) for x in fx["l1_tokens"]]
    first_eid = fx["l1_records"][0]["eventId"]
    for t in retried_tok:
        if t["eventId"] == first_eid and t["structure"] == "debate":
            t["billedTotal"] = t["total"] + 2985
            t["malformedAttempts"] = 1
    retried_ok = [json.loads(json.dumps(x)) for x in fx["l1_records"]]
    retried_ok[0]["matched"][1]["structureTokens"] += 2985       # what the harness writes
    retried_ok[0]["matched"][1]["residualTokens"] += 2985
    a_ret, ret_err = None, ""
    try:
        a_ret = analyse([(_redump(root, "ret_out.jsonl", retried_ok),
                          _redump(root, "ret_tok.jsonl", [fx["meta"]] + retried_tok))], db)
    except IntegrityError as exc:
        ret_err = str(exc)
    check("a retried call: matched.structureTokens == billedTotal is accepted",
          a_ret is not None, ret_err)
    ret_tokens = next((r.structureTokens for r in a_ret.rows
                       if r.eventId == first_eid and r.arm == "debate"), None) if a_ret else None
    check("the scored debate row carries the BILLED structure cost",
          ret_tokens == 17000 + 2985, f"{ret_tokens}")
    retried_bad = [json.loads(json.dumps(x)) for x in fx["l1_records"]]   # == usable total
    check_aborts("a retried call: matched.structureTokens == usable total (not billed) aborts",
                 lambda: analyse([(_redump(root, "retb_out.jsonl", retried_bad),
                                   _redump(root, "retb_tok.jsonl", [fx["meta"]] + retried_tok))], db),
                 "disagree about what this structure cost")

    print("\n=== 8. Sample flow, and the exploratory flag ===")
    sf = a.sample_flow
    check("sample flow counts the database rows: 17 total, 8 after the cutoff, 7 in index",
          (sf["total"], sf["after_release"], sf["eligible"]) == (17, 8, 7), f"{sf}")
    # D31: the curve is COMMITTED (always computed and rendered, no EXPLORATORY
    # label); only the averaging-alone READING stays behind the flag.
    curve = a.reference_curve[(MODEL, "L1")]
    # At k=1 the "ensemble" is each pool's first run alone. BRK-B and MSFT start
    # at 0.9 with label 1, AAPL and NVDA at 0.1 with label 0, AMD at 0.55 with
    # label 1, INTC at 0.5 with label 0:
    #   (0.01 + 0.01 + 0.01 + 0.01 + 0.2025 + 0.25)/6 = 0.4925/6 = 0.08208333
    check("committed curve k=1 brier = 0.08208333 over all 6 events, WITHOUT any flag",
          near(curve[0]["brier"], 0.4925 / 6) and curve[0]["n_events"] == 6,
          f"got {curve[0]}")
    check("the committed curve is rendered without the flag",
          "Baseline reference curve (committed, D31)" in render_results(a))
    check("EXPLORATORY does not appear in results.md without the flag",
          "EXPLORATORY" not in render_results(a))
    a3 = analyse([(fx["l1_out"], fx["l1_tok"])], db, exploratory=True)
    check("with the flag, the averaging-alone READING is labelled EXPLORATORY",
          "EXPLORATORY" in render_results(a3)
          and "averaging-alone" in render_results(a3))

    print("\n=== 8b. Block III (D31): tare, baselines, gate, provider halves, exclude ===")
    # ---- tare: on the clean fixture every event must reproduce exactly.
    t1, t2 = a.tare[(MODEL, "L1")], a.tare[(MODEL, "L2")]
    check("tare: 6 L1 + 3 L2 events, ALL exact, prefix always = workers, diffs 0",
          t1 == {"n": 6, "n_exact": 6, "n_prefix_ne_workers": 0,
                 "mean_diff": 0.0, "max_abs_diff": 0.0}
          and t2["n"] == 3 and t2["n_exact"] == 3, f"L1={t1} L2={t2}")
    # A zero-cost 4th run makes the prefix overrun the workers: the tare must
    # SEE that (mean of 4 runs != repetition), not smooth it away.
    warp = [json.loads(json.dumps(x)) for x in fx["l1_records"]]
    for rec9 in warp:
        if rec9["eventId"].startswith("BRK-B"):
            rec9["repetitionRuns"][3]["totalTokens"] = 0
    a_w = analyse([(_redump(root, "warp_out.jsonl", warp), fx["l1_tok"])], db)
    tw = a_w.tare[(MODEL, "L1")]
    # BRK-B: mean(0.9,0.9,0.9,0.15)=0.7125 vs repetition 0.9, label 1:
    # (1-0.7125)^2 - (1-0.9)^2 = 0.08265625 - 0.01 = +0.07265625
    check("tare mutation: a free 4th run is caught (5/6 exact, max diff 0.07265625)",
          tw["n_exact"] == 5 and tw["n_prefix_ne_workers"] == 1
          and near(tw["max_abs_diff"], 0.07265625), f"{tw}")
    check("tare is rendered", "tare check on the matching machinery" in render_results(a))

    # ---- baselines: trained STRICTLY before the cutoff, hand-checked base rate.
    bm = a.baselines_meta
    check("baselines train on exactly the 8 usable pre-cutoff rows (base rate 0.375)",
          bm["n_train"] == 8 and near(bm["train_base_rate"], 0.375), f"{bm}")
    bd = a.baselines[(MODEL, "L1", "debate")]
    # 4 paired events, base-rate forecast 0.375: (0.625^2*2 + 0.375^2*2)/4
    check("base-rate brier on the debate cell = 0.265625 by hand",
          bd["n"] == 4 and near(bd["brier_base_rate"], 0.265625), f"{bd}")
    check("logistic: NVDA's missing surprise is excluded and counted, pred is a probability",
          bd["n_logistic"] == 3 and bd["no_surprise"] == 1
          and 0.0 < bd["brier_logistic"] < 1.0, f"{bd}")
    check("baselines are rendered with the training pin",
          "reference baselines on the same events" in render_results(a)
          and "strictly before the experiment window" in render_results(a))

    # ---- the RQ3 gate, all four numbers by hand (common events BRK-B/MSFT/AAPL):
    # debate raw: L2 (0.04+0.09+0.09)/3 - L1 (0.09+0.16+0.16)/3 = -0.0633333;
    # control: +0.18 (asserted in section 5); controlled: -0.2433333.
    gd = a.rq3_gate[(MODEL, "debate")]
    check("gate debate: raw -0.0633, ctrl +0.18, controlled -0.2433 on n=3",
          gd["n"] == 3 and near(gd["delta_raw"], -0.19 / 3)
          and near(gd["delta_ctrl"], 0.18)
          and near(gd["delta_controlled"], -0.19 / 3 - 0.18), f"{gd}")
    ga = a.rq3_gate[(MODEL, "aggregator")]
    check("gate aggregator: raw 0 (0.25 both levels), controlled -0.18",
          near(ga["delta_raw"], 0.0) and near(ga["delta_controlled"], -0.18), f"{ga}")
    check("gate verdict: single model -> consistency not evaluable, no level statement",
          "only one model present" in render_results(a))

    # ---- provider halves: absent field explained; injected split hand-checked.
    check("no provider field -> split empty and the render says why",
          a.provider_split == {} and "no provider field" in render_results(a))
    prov = [json.loads(json.dumps(x)) for x in fx["l1_records"]]
    for rec9 in prov:
        sym9 = rec9["eventId"].split("_")[0]
        rec9["provider"] = "P1" if sym9 in ("BRK-B", "AAPL") else "P2"
    a_p = analyse([(_redump(root, "prov_out.jsonl", prov), fx["l1_tok"])], db)
    ph = a_p.provider_split[(MODEL, "L1", "debate")]["halves"]
    # debate diffs: BRK-B -0.07, AAPL -0.09 (P1); MSFT 0, NVDA -0.16 (P2)
    check("provider halves: P1 n=2 mean -0.08, P2 n=2 mean -0.08, no sign flip",
          ph["P1"]["n"] == 2 and near(ph["P1"]["mean_paired_diff"], -0.08)
          and ph["P2"]["n"] == 2 and near(ph["P2"]["mean_paired_diff"], -0.08)
          and not a_p.provider_split[(MODEL, "L1", "debate")]["sign_flip"], f"{ph}")
    flip = [json.loads(json.dumps(x)) for x in prov]
    for rec9 in flip:
        sym9 = rec9["eventId"].split("_")[0]
        rec9["provider"] = "PX" if sym9 == "BRK-B" else "PY"
        if sym9 == "BRK-B":
            rec9["outcomes"]["debate"]["probability"] = 0.5   # diff becomes +0.09
    a_f = analyse([(_redump(root, "flip_out.jsonl", flip), fx["l1_tok"])], db)
    check("provider halves: opposite signs are flagged as a flip",
          a_f.provider_split[(MODEL, "L1", "debate")]["sign_flip"]
          and "**YES**" in render_results(a_f), f"{a_f.provider_split}")

    # ---- --exclude-events: the §8.2 mechanism drops before scoring, loudly.
    a_x = analyse([(fx["l1_out"], fx["l1_tok"]), (fx["l2_out"], fx["l2_tok"])], db,
                  exclude={("BRK-B", "2026-01-05")}, exclude_label="why-test")
    check("exclusion drops BRK-B from both levels (debate cell n 4 -> 3)",
          a_x.rq1[(MODEL, "L1", "debate")]["n"] == 3
          and sum(a_x.excluded["dropped"].values()) == 2, f"{a_x.excluded}")
    check("the recomputation is marked, with the reason",
          "SENSITIVITY RECOMPUTATION" in render_results(a_x)
          and "why-test" in render_results(a_x))

    # ---- version line (IV-2).
    check("the scorer commit is recorded and rendered",
          a.scorer_commit != "" and "Scorer commit:" in render_results(a))

    print("\n=== 9. scored.jsonl is lean and complete ===")
    out = write_scored(a, root)
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    check("one line per (eventId, modelKey, arm), 5 arms x 9 event-model pairs = 45",
          len(lines) == 45 and len({(x["eventId"], x["modelKey"], x["arm"]) for x in lines}) == 45,
          f"{len(lines)} lines")
    check("no model text in scored.jsonl",
          not any("responseText" in x or "reasoningText" in x for x in lines))
    check("scored.jsonl carries the key back into the ledger",
          all({"eventId", "modelKey", "arm"} <= set(x) for x in lines))

    print(f"\nSELF-TEST: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
    return 1 if FAIL else 0


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="append", default=[],
                    metavar="OUTCOMES,STRUCTURE_TOKENS",
                    help="a comma-separated pair of files from one run; repeatable")
    ap.add_argument("--out-dir", default=None,
                    help="where scored.jsonl and results.md go (default: beside the first run)")
    ap.add_argument("--db", default=str(DB_DEFAULT), help="thesis.db, opened read-only")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--case", default=None, metavar="EVENT_ID")
    ap.add_argument("--model", default=None, metavar="MODEL_KEY")
    ap.add_argument("--case-dir", default=str(PILOT_DIR),
                    help="folder searched by --case for outcomes and ledger files")
    ap.add_argument("--sample", default=None, metavar="EVENT_SAMPLE_TXT",
                    help="the pinned event sample; adds drawn/booked/missing "
                         "per level to the sample-flow table (use on the real run)")
    ap.add_argument("--exclude-events", default=None, metavar="FILE",
                    help="SYMBOL,DATE lines to drop before scoring (plan §8.2 "
                         "sensitivity mechanism); output is marked as a recomputation")
    ap.add_argument("--exclude-reason", default="", metavar="TEXT",
                    help="why the events are excluded; printed in the header")
    ap.add_argument("--exploratory", action="store_true",
                    help="add the EXPLORATORY ensemble curve (not in the expose)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.case:
        if not args.model:
            print("--case also needs --model", file=sys.stderr)
            return 2
        return run_case(args.case, args.model, Path(args.db), Path(args.case_dir))

    if not args.run:
        ap.print_help()
        return 2

    specs = []
    for spec_str in args.run:
        parts = spec_str.split(",")
        if len(parts) != 2:
            print(f"--run wants OUTCOMES,STRUCTURE_TOKENS; got {spec_str!r}", file=sys.stderr)
            return 2
        specs.append((Path(parts[0].strip()), Path(parts[1].strip())))

    a = analyse(specs, Path(args.db), exploratory=args.exploratory,
                sample_path=Path(args.sample) if args.sample else None,
                exclude=(read_event_sample(Path(args.exclude_events))
                         if args.exclude_events else None),
                exclude_label=args.exclude_reason)
    out_dir = Path(args.out_dir) if args.out_dir else specs[0][0].resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    scored = write_scored(a, out_dir)
    results = out_dir / "results.md"
    results.write_text(render_results(a), encoding="utf-8", newline="\n")

    print_console(a)
    print(f"\nwrote {scored}  ({len(a.rows)} rows)")
    print(f"wrote {results}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except IntegrityError as exc:
        print(f"\nABORT (integrity check failed): {exc}", file=sys.stderr)
        sys.exit(1)
    except AssertionError as exc:
        print(f"\nABORT (assertion failed): {exc}", file=sys.stderr)
        sys.exit(1)
