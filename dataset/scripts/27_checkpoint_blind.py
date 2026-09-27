"""Blind operational check of an OPEN run — the D32 checkpoint (2026-09-06).

    python scripts/27_checkpoint_blind.py                    # reads scaffold/pilot5/L1_* and L2_*
    python scripts/27_checkpoint_blind.py --budget 120       # adds a budget line to the verdict
    python scripts/27_checkpoint_blind.py --self-test

The pre-registered plan (ANALYSIS_PLAN_PREREGISTERED.md §3) forbids any
interim look at RESULTS while the run is open: no score exists before both
levels are complete, and nothing about the run may be decided from an outcome.
It does permit — and the operator needs — fault monitoring. This script is
the fence between the two. It answers only OPERATIONAL questions:

  * did every planned ladder run book, and if not, why (outage vs. drop)?
  * do the APIs answer — rate limits, retries, provider fallbacks?
  * are the files consistent — token identities, no duplicates, no orphans?
  * what does one ladder cost in dollars, tokens, calls and seconds, per
    model and level, and what does that project to for the rest of the run?

WHAT IT NEVER DOES, by construction (the self-test asserts it):
  * it never opens thesis.db and never reads the evidence-pack JSON — the two
    places the labels live;
  * it never prints a forecast: `probability` fields in the outcomes and the
    ledger are not read, and status lines (which carry forecasts for fault
    monitoring) are parsed for their timing and never echoed;
  * it computes no score of any kind.

Drops (malformed answers, D16) ARE reported here because they are attrition
the plan counts anyway (§7) and because an operator has to know whether the
pipeline is producing answers at all. They are REPORTED, not gated: the D32
go/no-go criteria are infrastructure criteria only — see the verdict block.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PILOT_DIR = Path(__file__).resolve().parents[1] / "scaffold" / "pilot5"
TOTAL_EVENTS_DEFAULT = 840          # the pinned sample (D28)
LEVELS = (1, 2)
RUNGS = ("repetition", "aggregator", "debate")


# --------------------------------------------------------------------------- #
# Reading the run files. Only operational fields are touched — see the header.
# --------------------------------------------------------------------------- #
def _read_jsonl(path: Path, keep: tuple[str, ...] | None = None, derive=None) -> list[dict]:
    """Stream a JSONL file; with `keep`, retain only those keys per record.

    The raw log stores every prompt and every response in full (~15 KB per
    call, ~1.5 GB for the whole run), so holding whole records in memory is
    not an option — and none of that text is operational anyway. `derive`,
    if given, computes extra fields from the full record before it is dropped
    (used for the D24 hashes: the texts are reduced to digests on the fly).
    """
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn final line; the harness purges it on resume
            slim = {k: d.get(k) for k in keep if k in d} if keep else d
            if derive:
                slim.update(derive(d))
            out.append(slim)
    return out


def _raw_hashes(d: dict) -> dict:
    """D24 digests: the prompt and the answer, as short hashes, text discarded."""
    import hashlib
    prompt = f"{d.get('system_prompt') or ''}\x00{d.get('user_prompt') or ''}"
    content = d.get("content")
    return {
        "prompt_sha": hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:16],
        "content_sha": hashlib.sha1(content.encode("utf-8")).hexdigest()[:16] if isinstance(content, str) and content else None,
    }


RAW_KEEP = ("ts", "ok", "evidence_id", "model_key", "provider_pinned", "provider_returned", "attempts", "http_status")
LEDGER_KEEP = ("modelKey", "eventId", "malformed", "failureKind", "attempt", "read", "write", "costUsd", "totalTokens")
OUTCOMES_KEEP = ("modelKey", "eventId", "provider", "outcomes", "matched")


_SESSION = re.compile(r"^=== (?P<mode>\w+) session (?P<ts>\S+) cap=\$(?P<cap>[\d.]+) level=(?P<level>\d) "
                      r"evidence=(?P<evidence>\w+) concurrency=(?P<conc>\d+) repeats=(?P<rep>\d+)"
                      r"(?: harness=(?P<harness>\S+))? ===$")
_LIMIT = re.compile(r"^event limit (?P<n>\d+) \(D32 checkpoint\): (?P<events>\d+) events = (?P<planned>\d+) of (?P<all>\d+) ladder runs")
_PLANNED = re.compile(r"^(?P<n>\d+) ladder runs over (?P<models>\d+) models")
_RESUME = re.compile(r"^resuming: (?P<done>\d+) of (?P<total>\d+) done, (?P<togo>\d+) to go")
# A booked ladder: the forecasts in the middle are deliberately NOT captured.
_BOOKED = re.compile(r"^\[full\] \(\d+/\d+\) (?P<model>\S+) \| (?P<event>\S+) \| .* \| (?P<secs>\d+)s \| \$")
_FAIL = re.compile(r"^\[full\] (?P<model>\S+) \| (?P<event>\S+) \| FAIL (?P<msg>.*)$")


def classify_fail(msg: str) -> str:
    low = msg.lower()
    if "cost cap" in low:
        return "cost cap"
    if re.search(r"status 429|\b429\b", msg):
        return "429"
    if re.search(r"status 5\d\d", msg):
        return "5xx"
    if "timeout" in low or "abort" in low or "econnreset" in low or "fetch failed" in low:
        return "timeout/network"
    return "other"


def _parse_status(lines: list[str]) -> dict:
    sessions, booked, fails, warnings = [], [], [], []
    event_limit = planned = resume_total = planned_line = None
    cap_hits = 0
    for raw in lines:
        line = raw.rstrip("\n")
        m = _SESSION.match(line)
        if m:
            sessions.append({"mode": m["mode"], "ts": m["ts"], "cap": float(m["cap"]), "level": int(m["level"]),
                             "evidence": m["evidence"], "concurrency": int(m["conc"]), "repeats": int(m["rep"]),
                             "harness": m["harness"] or "unknown"})
            # each session re-declares its plan; the LAST one governs
            event_limit = planned = resume_total = planned_line = None
            continue
        m = _LIMIT.match(line)
        if m:
            event_limit, planned = int(m["events"]), int(m["planned"])
            continue
        m = _RESUME.match(line)
        if m:
            resume_total = int(m["total"])
            continue
        m = _PLANNED.match(line)
        if m:
            planned_line = int(m["n"])
            continue
        m = _BOOKED.match(line)
        if m:
            booked.append((m["model"], m["event"], int(m["secs"])))
            continue
        m = _FAIL.match(line)
        if m:
            fails.append((m["model"], m["event"], classify_fail(m["msg"]), m["msg"][:100]))
            continue
        if line.startswith("!!"):
            if "cost cap" in line.lower():
                cap_hits += 1
            else:
                warnings.append(line[:160])
    if planned is None:
        planned = resume_total if resume_total is not None else planned_line
    return {"sessions": sessions, "booked_lines": booked, "fails_raw": fails, "warnings": warnings,
            "cap_hits": cap_hits, "event_limit": event_limit, "planned": planned}


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "mean": None, "median": None, "max": None}
    return {"n": len(xs), "mean": sum(xs) / len(xs), "median": statistics.median(xs), "max": max(xs)}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def analyse_level(d: Path, level: int) -> dict | None:
    """Everything operational about one level, or None when it has not run."""
    pre = f"L{level}_"
    status_path = d / f"{pre}run_status.txt"
    outcomes = _read_jsonl(d / f"{pre}outcomes.jsonl", OUTCOMES_KEEP)
    ledger = _read_jsonl(d / f"{pre}ledger5.jsonl", LEDGER_KEEP)
    raws = _read_jsonl(d / f"{pre}runs_raw.jsonl", RAW_KEEP, _raw_hashes)
    if not status_path.exists() and not outcomes and not ledger and not raws:
        return None
    st = _parse_status(status_path.read_text(encoding="utf-8").splitlines() if status_path.exists() else [])

    # ---- outcomes: coverage, drops, providers. `probability` is never read.
    booked: Counter = Counter()
    provider_booked: Counter = Counter()
    drops: Counter = Counter()
    drop_reasons: Counter = Counter()
    event_drops: Counter = Counter()
    zero_run_matches = 0
    seen_keys: Counter = Counter()
    per_event_models: dict[str, set] = defaultdict(set)
    for o in outcomes:
        key = (o.get("modelKey"), o.get("eventId"))
        seen_keys[key] += 1
        booked[key[0]] += 1
        provider_booked[(key[0], o.get("provider") or "unknown")] += 1
        per_event_models[key[1]].add(key[0])
        outs = o.get("outcomes") or {}
        dropped_rungs = 0
        for rung in RUNGS:
            r = outs.get(rung) or {}
            if r.get("dropped"):
                drops[(key[0], rung)] += 1
                drop_reasons[str(r.get("dropReason") or "unspecified")] += 1
                dropped_rungs += 1
        if dropped_rungs == len(RUNGS):
            event_drops[key[0]] += 1
        for mm in o.get("matched") or []:
            if (mm.get("runsUsed") or 0) == 0:
                zero_run_matches += 1
    models = sorted({m for m, _ in seen_keys} | {m for m, _, _ in st["booked_lines"]}
                    | {m for m, _, _, _ in st["fails_raw"]} | {r.get("modelKey") for r in ledger if r.get("modelKey")})
    for m in models:
        booked.setdefault(m, 0)
        event_drops.setdefault(m, 0)
    events = sorted({e for _, e in seen_keys} | {e for _, e, _ in st["booked_lines"]} | {e for _, e, _, _ in st["fails_raw"]})
    booked_keys = set(seen_keys)
    missing = sorted((m, e) for m in models for e in events if (m, e) not in booked_keys)
    events_all_models = sum(1 for e in events if all((m, e) in booked_keys for m in models))
    duplicate_outcomes = sum(1 for k, n in seen_keys.items() if n > 1)

    # the FAIL list, de-duplicated per cell (a cell may fail on several resumes);
    # a cell that later booked is not an open failure
    fails, fail_seen = [], set()
    fail_classes: Counter = Counter()
    for m, e, cls, _msg in st["fails_raw"]:
        fail_classes[(m, cls)] += 1
        if (m, e) in booked_keys or (m, e) in fail_seen:
            continue
        fail_seen.add((m, e))
        fails.append((m, e, cls))

    # ---- ledger: malformed attempts, retries, identities, cost per ladder
    malformed: Counter = Counter()
    retried_calls: Counter = Counter()
    identity_violations = 0
    cost_by_key: dict = defaultdict(float)
    tokens_by_key: dict = defaultdict(int)
    calls_by_key: Counter = Counter()
    truncated = 0
    for r in ledger:
        m, key = r.get("modelKey"), (r.get("modelKey"), r.get("eventId"))
        if r.get("malformed"):
            kind = r.get("failureKind") or "unknown"
            malformed[(m, kind)] += 1
            if kind == "truncated":
                truncated += 1
        if (r.get("attempt") or 1) > 1:
            retried_calls[m] += 1
        rd, wr = r.get("read") or {}, r.get("write") or {}
        if "providerPrompt" in rd and sum(rd.get(k, 0) or 0 for k in ("D", "A", "B", "B_SELF")) != rd["providerPrompt"]:
            identity_violations += 1
        elif "providerCompletion" in wr and (wr.get("C1", 0) or 0) + (wr.get("C2", 0) or 0) != wr["providerCompletion"]:
            identity_violations += 1
        cost_by_key[key] += float(r.get("costUsd") or 0)
        tokens_by_key[key] += int(r.get("totalTokens") or 0)
        calls_by_key[key] += 1
    for m in models:
        retried_calls.setdefault(m, 0)
    orphan_ledger_keys = sum(1 for k in cost_by_key if k not in booked_keys)
    cost, tokens, calls = {}, {}, {}
    for m in models:
        keys = [k for k in cost_by_key if k[0] == m and k in booked_keys]
        cost[m] = _stats([cost_by_key[k] for k in keys])
        tokens[m] = _stats([tokens_by_key[k] for k in keys])
        calls[m] = _stats([calls_by_key[k] for k in keys])
    ledger_total_usd = sum(cost_by_key.values())

    # ---- status: seconds per booked ladder
    secs = {m: _stats([s for mm, _, s in st["booked_lines"] if mm == m]) for m in models}

    # ---- raw log: HTTP-level health and wall clock
    failed_by: Counter = Counter()
    recovered_by: Counter = Counter()
    fallbacks = failed = 0
    tss = []
    # D24 independent-sampling check: within one (event, model, provider), the
    # calls that were sent the IDENTICAL prompt (the three workers, or the
    # extra repetition draws) must not come back byte-identical — at
    # temperature 0.7 that is request collapsing on the host, not sampling.
    groups: dict = defaultdict(list)
    for r in raws:
        if r.get("ts"):
            try:
                tss.append(_ts(r["ts"]))
            except ValueError:
                pass
        m, prov = r.get("model_key"), r.get("provider_pinned") or "unknown"
        if r.get("ok") is False:
            failed += 1
            failed_by[(m, prov, r.get("http_status"))] += 1
            continue
        if (r.get("attempts") or 1) > 1:
            recovered_by[(m, prov)] += 1
        ret = r.get("provider_returned")
        if ret and ret != prov:
            fallbacks += 1
        if r.get("content_sha"):
            groups[(r.get("evidence_id"), m, prov, r.get("prompt_sha"))].append(r["content_sha"])
    collapsed = 0
    collapsed_by: Counter = Counter()
    for (_ev, m, prov, _p), shas in groups.items():
        dup = len(shas) - len(set(shas))
        if dup:
            collapsed += dup
            collapsed_by[(m, prov)] += dup
    wall_hours = ((max(tss) - min(tss)).total_seconds() / 3600.0) if len(tss) >= 2 else 0.0
    booked_total = sum(booked.values())
    ladders_per_hour = (booked_total / wall_hours) if wall_hours > 0 else None

    return {
        "level": level, "sessions": st["sessions"], "event_limit": st["event_limit"], "planned": st["planned"],
        "models": models, "events": events, "booked": dict(booked), "booked_total": booked_total,
        "provider_booked": dict(provider_booked), "missing": missing, "events_all_models": events_all_models,
        "fails": fails, "fail_classes": dict(fail_classes), "cap_hits": st["cap_hits"], "warnings": st["warnings"],
        "drops": dict(drops), "drop_reasons": dict(drop_reasons), "event_drops": dict(event_drops),
        "zero_run_matches": zero_run_matches, "malformed": dict(malformed), "truncated": truncated,
        "retried_calls": dict(retried_calls), "identity_violations": identity_violations,
        "duplicate_outcomes": duplicate_outcomes, "orphan_ledger_keys": orphan_ledger_keys,
        "cost": cost, "tokens": tokens, "calls": calls, "secs": secs, "ledger_total_usd": ledger_total_usd,
        "http": {"calls": len(raws), "failed": failed, "failed_by": dict(failed_by),
                 "recovered_by": dict(recovered_by), "fallbacks": fallbacks,
                 "collapsed": collapsed, "collapsed_by": dict(collapsed_by), "same_prompt_groups": len(groups)},
        "wall_hours": wall_hours, "ladders_per_hour": ladders_per_hour,
    }


def project(levels: dict, total_events: int) -> dict:
    """Remaining ladders, dollars and hours per level, from this level's own rates."""
    out = {}
    for L, lv in levels.items():
        if lv is None:
            continue
        remaining = {m: total_events - lv["booked"].get(m, 0) for m in lv["models"]}
        usd = 0.0
        usd_known = True
        for m, n in remaining.items():
            mean = lv["cost"][m]["mean"]
            if mean is None:
                usd_known = False
                continue
            usd += n * mean
        rate = lv["ladders_per_hour"]
        hours = (sum(remaining.values()) / rate) if rate else None
        out[L] = {"remaining": remaining, "usd": usd if usd_known else None, "hours": hours,
                  "spent": lv["ledger_total_usd"]}
    return out


def _f(x, nd=4, unit=""):
    return "n.a." if x is None else f"{x:.{nd}f}{unit}"


def render(levels: dict, proj: dict, total_events: int, budget: float | None) -> str:
    o: list[str] = []
    w = o.append
    w("# Blind operational check — D32 checkpoint\n")
    w(f"_generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. Reads only "
      "`L*_run_status.txt`, `L*_outcomes.jsonl`, `L*_ledger5.jsonl`, `L*_runs_raw.jsonl`. "
      "Contains no forecast, no ground truth and no score (plan §3)._\n")
    for L in LEVELS:
        lv = levels.get(L)
        w(f"\n## Level {L}\n")
        if lv is None:
            w("_no run files for this level_\n")
            continue
        w("### Sessions\n")
        w("| # | started (UTC) | cap $ | concurrency | repeats | evidence | harness |")
        w("|---|---|---:|---:|---:|---|---|")
        for i, s in enumerate(lv["sessions"], 1):
            w(f"| {i} | {s['ts']} | {s['cap']:.2f} | {s['concurrency']} | {s['repeats']} | {s['evidence']} | {s['harness']} |")
        commits = {s["harness"] for s in lv["sessions"]}
        if len(commits) > 1:
            w(f"\n**harness commit changed between sessions: {sorted(commits)} — a logged deviation is required (plan §3/IV-2)**")
        if lv["event_limit"] is not None:
            w(f"\nevent limit: **{lv['event_limit']} events** -> {lv['planned']} ladder runs planned (checkpoint)")
        elif lv["planned"] is not None:
            w(f"\nplanned ladder runs: {lv['planned']}")
        w("\n### Coverage\n")
        w("| model | booked ladders | by provider |")
        w("|---|---:|---|")
        for m in lv["models"]:
            provs = ", ".join(f"{p} {n}" for (mm, p), n in sorted(lv["provider_booked"].items()) if mm == m)
            w(f"| {m} | {lv['booked'].get(m, 0)} | {provs} |")
        w(f"\nevents seen: {len(lv['events'])}; events booked on ALL models: {lv['events_all_models']}; "
          f"booked ladders in total: {lv['booked_total']}")
        if lv["missing"]:
            w(f"\n**{len(lv['missing'])} planned cell(s) without an outcomes line:**")
            cause = {(m, e): cls for m, e, cls in lv["fails"]}
            for m, e in lv["missing"]:
                why = cause.get((m, e))
                w(f"- {m} | {e} — " + (f"last FAIL: {why} (outage; no outcomes line, retried on the next resume)" if why
                                        else "no FAIL line recorded (interrupted or never started)"))
        else:
            w("\nevery planned cell has an outcomes line.")
        w("\n### Failures and retries\n")
        if lv["fail_classes"]:
            w("outage FAIL lines (all sessions, incl. cells that later booked): " +
              ", ".join(f"{m} {cls} x{n}" for (m, cls), n in sorted(lv["fail_classes"].items())))
        else:
            w("outage FAIL lines: none")
        h = lv["http"]
        w(f"\nHTTP calls {h['calls']}; failed after the client's own retries {h['failed']}"
          + ("; by model x provider x status: " + ", ".join(f"{m}@{p} {st} x{n}" for (m, p, st), n in sorted(h["failed_by"].items(), key=str))
             if h["failed_by"] else "")
          + f"; recovered after retries {sum(h['recovered_by'].values())}"
          + ("; by model x provider: " + ", ".join(f"{m}@{p} x{n}" for (m, p), n in sorted(h["recovered_by"].items()))
             if h["recovered_by"] else "")
          + f"; provider fallbacks (returned != pinned) {h['fallbacks']}")
        w(f"\nD24 independent sampling: byte-identical answers to an identical prompt: {h['collapsed']} "
          f"across {h['same_prompt_groups']} same-prompt groups"
          + ("; by model x provider: " + ", ".join(f"{m}@{p} x{n}" for (m, p), n in sorted(h["collapsed_by"].items()))
             if h["collapsed_by"] else ""))
        w("\nmalformed attempts (ledger, D16): " + (", ".join(f"{m} {k} x{n}" for (m, k), n in sorted(lv["malformed"].items()))
                                                   if lv["malformed"] else "none")
          + "; retried calls (attempt > 1): " + (", ".join(f"{m} {n}" for m, n in sorted(lv["retried_calls"].items())) or "none"))
        w("\ndropped rungs (a malformed answer after its retry): " +
          (", ".join(f"{m} {r} x{n}" for (m, r), n in sorted(lv["drops"].items())) if lv["drops"] else "none")
          + "; whole-event drops (a worker failed): " + (", ".join(f"{m} {n}" for m, n in sorted(lv["event_drops"].items())) or "none")
          + f"; matched comparisons with runsUsed = 0: {lv['zero_run_matches']}")
        if lv["drop_reasons"]:
            w("\ndrop reasons: " + "; ".join(f"{n} x {r}" for r, n in sorted(lv["drop_reasons"].items(), key=lambda kv: -kv[1])))
        w("\n### Integrity\n")
        w(f"identity violations: {lv['identity_violations']} | duplicate outcomes lines: {lv['duplicate_outcomes']} | "
          f"orphan ledger keys: {lv['orphan_ledger_keys']} | cost-cap stops: {lv['cap_hits']} | other `!!` lines: {len(lv['warnings'])}")
        for wl in lv["warnings"]:
            w(f"- {wl}")
        w("\n### Cost and time per booked ladder\n")
        w("| model | ladders | calls/ladder | tokens/ladder | $ mean | $ median | $ max | s median | s max |")
        w("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for m in lv["models"]:
            c, t, k, s = lv["cost"][m], lv["tokens"][m], lv["calls"][m], lv["secs"][m]
            w(f"| {m} | {c['n']} | {_f(k['mean'], 1)} | {_f(t['mean'], 0)} | {_f(c['mean'])} | {_f(c['median'])} | "
              f"{_f(c['max'])} | {_f(s['median'], 0)} | {_f(s['max'], 0)} |")
        w(f"\nledger total ${lv['ledger_total_usd']:.4f}; wall clock {lv['wall_hours']:.2f} h; "
          f"throughput {_f(lv['ladders_per_hour'], 1)} ladders/h at this level")

    w(f"\n## Projection to {total_events} events x 2 models per level\n")
    w("| level | remaining ladders | projected remaining $ | projected hours (this level alone) |")
    w("|---|---|---:|---:|")
    total_usd, total_spent, hours_list = 0.0, 0.0, []
    usd_known = True
    for L, p in sorted(proj.items()):
        rem = ", ".join(f"{m} {n}" for m, n in sorted(p["remaining"].items()))
        w(f"| L{L} | {rem} | {_f(p['usd'], 2)} | {_f(p['hours'], 1)} |")
        total_spent += p["spent"]
        if p["usd"] is None:
            usd_known = False
        else:
            total_usd += p["usd"]
        if p["hours"] is not None:
            hours_list.append(p["hours"])
    if proj:
        grand = (total_spent + total_usd) if usd_known else None
        w(f"\nspent so far (ledger, all levels) ${total_spent:.2f}; projected remaining {_f(total_usd if usd_known else None, 2)}; "
          f"projected TOTAL {_f(grand, 2)}; wall clock if the levels run concurrently {_f(max(hours_list) if hours_list else None, 1)} h, "
          f"sequentially {_f(sum(hours_list) if hours_list else None, 1)} h. "
          "Rates are per-level means of the booked ladders and carry the checkpoint's own noise.")
    else:
        grand = None

    w("\n## Verdict — the D32 criteria (infrastructure only; nothing here is a result)\n")
    present = [lv for lv in levels.values() if lv]
    missing_n = sum(len(lv["missing"]) for lv in present)
    all_outages = all(all((m, e) in {(fm, fe) for fm, fe, _ in lv["fails"]} for m, e in lv["missing"]) for lv in present)
    w(f"- C1 every planned ladder booked: {'PASS' if missing_n == 0 else 'FAIL'}"
      + ("" if missing_n == 0 else f" — {missing_n} cell(s) open" + (", all outages: re-run the same command to retry" if all_outages
                                                                        else ", at least one without a recorded cause")))
    idv = sum(lv["identity_violations"] for lv in present)
    w(f"- C2 ledger identities: {'PASS' if idv == 0 else 'FAIL'} ({idv} violation(s))")
    fb = sum(lv["http"]["fallbacks"] for lv in present)
    w(f"- C3 provider pinning, no fallback (D29): {'PASS' if fb == 0 else 'FAIL'} ({fb} call(s) served by a provider other than the pinned one)")
    caps = sum(lv["cap_hits"] for lv in present)
    w(f"- C4 no cost-cap stop: {'PASS' if caps == 0 else 'FAIL'} ({caps})")
    dup = sum(lv["duplicate_outcomes"] + lv["orphan_ledger_keys"] for lv in present)
    w(f"- C5 files consistent (no duplicate outcomes, no orphan ledger records): {'PASS' if dup == 0 else 'FAIL'} ({dup})")
    col = sum(lv["http"]["collapsed"] for lv in present)
    w(f"- C7 independent sampling (D24): {'PASS' if col == 0 else 'FAIL'} ({col} byte-identical answer(s) to an identical prompt)")
    if budget is not None:
        if grand is None:
            w(f"- C6 projected total within budget ${budget:.0f}: n.a. (no cost data yet)")
        else:
            w(f"- C6 projected total within budget ${budget:.0f}: {'PASS' if grand <= budget else 'FAIL'} (projected ${grand:.2f})")
    mal = sum(sum(lv["malformed"].values()) for lv in present)
    drp = sum(sum(lv["drops"].values()) for lv in present)
    w(f"- R1 malformed attempts {mal}, dropped rungs {drp}: REPORTED, not gated — a D16 finding that the plan counts as attrition (§7)")
    trunc = sum(lv["truncated"] for lv in present)
    w(f"- R2 truncated answers (finish_reason = length) {trunc}: REPORTED — the token cap is the harness's stated responsibility; "
      "raising it would be a logged deviation (§12)")
    return "\n".join(o) + "\n"


# --------------------------------------------------------------------------- #
# Self-test: hand-derived fixture, numbers checked by hand in the comments
# --------------------------------------------------------------------------- #
def _write_fixture(d: Path) -> None:
    """One level (L1) of a checkpoint run: 3 events x 2 models = 6 planned.

    Booked: d/A, q/A, d/B, d/C, q/C = 5. Not booked: q/B (FAIL 429 = outage).
    Drops: q/A debate (format); d/C all three rungs (worker malformed).
    Ledger: q/A one format retry; d/C one truncated + one format attempt;
            d/B one identity violation.
    Raw: 12 HTTP calls, 1 failed (429), 1 recovered after retries, 1 fallback.
    Wall clock: 10:00:10 -> 11:00:10 = 1.0 h for 5 ladders.
    """
    Q, D = "qwen3-32b@on", "deepseek-chat-v3-0324"
    A, B, C = "A_2025-01-01_L1", "B_2025-02-02_L1", "C_2025-03-03_L1"
    status = [
        "=== FULL session 2026-09-06T10:00:00.000Z cap=$6 level=1 evidence=REAL concurrency=6 repeats=1 harness=abc1234 ===",
        "event limit 3 (D32 checkpoint): 3 events = 6 of 12 ladder runs; lift PILOT5_EVENT_LIMIT to resume the rest",
        "6 ladder runs over 2 models, concurrency 6, order seed-shuffled (D31)",
        "[calib] qwen3-32b@on: template overhead 3 tok/call (provider 100 vs local 97)",
        f"[full] (1/6) {D} | {A} | repetition=0.5731(comm 0) aggregator=0.5731(comm 2463) vs rep@3run=0.5731 debate=0.4419(comm 6737) vs rep@12run=0.6125 | 184s | $0.0306",
        f"[full] (2/6) {Q} | {A} | repetition=0.4837(comm 0) aggregator=0.4837(comm 2976) vs rep@3run=0.4837 debate=DROP(comm 6238) | 269s | $0.0687",
        f"[full] {Q} | {B} | FAIL OpenRouter call failed (status 429): {{\"error\":{{\"message\":\"Provider returned error\",\"code\":429}}}} (no outcomes line written; retried on the next resume)",
        f"[full] (3/6) {D} | {B} | repetition=0.5731(comm 0) aggregator=0.5731(comm 2048) vs rep@5run=0.5731 debate=0.5731(comm 5662) vs rep@17run=0.5731 | 177s | $0.1386",
        f"[full] (4/6) {D} | {C} | repetition=DROP(comm 0) aggregator=DROP(comm 0) debate=DROP(comm 0) | 40s | $0.1481",
        f"[full] (5/6) {Q} | {C} | repetition=0.4837(comm 0) aggregator=0.4837(comm 3000) vs rep@3run=0.4837 debate=0.4837(comm 6000) vs rep@10run=0.4837 | 300s | $0.1881",
        "FULL done. 5/6 ladder runs this session (0 already on disk), 4 structure drops, total=$0.1881",
        "[report] wrote L1_report.md",
    ]
    (d / "L1_run_status.txt").write_text("\n".join(status) + "\n", encoding="utf-8")

    def oc(model, ev, provider, drops=(), reason=None, matched=("aggregator", "debate")):
        outs = {}
        for s in RUNGS:
            if s in drops:
                outs[s] = {"probability": None, "dropped": True, "dropReason": reason}
            else:
                outs[s] = {"probability": 0.5731, "dropped": False}
        m = [{"structure": s, "runsUsed": 3 if s == "aggregator" else 12, "probability": 0.6125,
              "structureProbability": 0.4419} for s in matched if s not in drops]
        return {"modelKey": model, "eventId": ev, "provider": provider, "outcomes": outs,
                "matched": m, "repetitionRuns": [{"probability": 0.4837, "totalTokens": 1000}] * 12}

    outcomes = [
        oc(D, A, "SiliconFlow"),
        oc(Q, A, "DeepInfra", drops=("debate",), reason="debate round 2: format"),
        oc(D, B, "SiliconFlow"),
        oc(D, C, "SiliconFlow", drops=RUNGS, reason="worker_2 malformed after 2 attempts (format)", matched=()),
        oc(Q, C, "SiliconFlow"),
    ]
    (d / "L1_outcomes.jsonl").write_text("".join(json.dumps(o) + "\n" for o in outcomes), encoding="utf-8")

    def rec(model, ev, role, cost, *, attempt=1, ok=True, kind=None, finish="stop",
            prompt=565, comp=418, break_identity=False):
        read = {"D": prompt, "A": 0, "B": 0, "B_SELF": 0, "providerPrompt": prompt + (10 if break_identity else 0)}
        return {"structure": "workers" if role.startswith("worker") else role, "eventId": ev, "modelKey": model,
                "agentRole": role, "round": 0, "attempt": attempt, "ok": ok, "malformed": not ok,
                "failureKind": kind, "finishReason": finish, "read": read,
                "write": {"C1": 3, "C2": comp - 3, "providerCompletion": comp},
                "totalTokens": prompt + comp, "costUsd": cost, "durationMs": 15000,
                "probability": 0.5731 if ok else None, "responseText": "<decision>0.5731</decision>"}

    ledger = [
        rec(D, A, "worker_1", 0.01), rec(D, A, "worker_2", 0.01), rec(D, A, "aggregator", 0.0106),
        rec(Q, A, "worker_1", 0.02, ok=False, kind="format"), rec(Q, A, "worker_1", 0.0181, attempt=2),
        rec(D, B, "worker_1", 0.03), rec(D, B, "worker_2", 0.0399, break_identity=True),
        rec(D, C, "worker_2", 0.005, ok=False, kind="truncated", finish="length"),
        rec(D, C, "worker_2", 0.0045, attempt=2, ok=False, kind="format"),
        rec(Q, C, "worker_1", 0.02), rec(Q, C, "worker_2", 0.02),
    ]
    (d / "L1_ledger5.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger), encoding="utf-8")

    def raw(ts, model, ev, pinned, *, ok=True, attempts=1, status=None, returned=None, content=None):
        # Every call of one (event, model) shares the same prompt here, so the
        # D24 independent-sampling check groups them; contents are distinct per
        # call unless a test passes the same `content` twice on purpose.
        r = {"ts": ts, "ok": ok, "evidence_id": ev, "model_key": model, "provider_pinned": pinned,
             "attempts": attempts, "system_prompt": "S", "user_prompt": "U", "cost_usd": 0.01,
             "duration_ms": 15000}
        if ok:
            r["provider_returned"] = returned or pinned
            r["content"] = content or f"<reason>{ts}</reason><decision>0.5731</decision>"
        else:
            r["http_status"] = status
            r["error"] = "rate-limited upstream"
        return r

    raws = [
        # d/A: two byte-identical answers to the identical prompt = the D24 request-collapse signature
        raw("2026-09-06T10:00:10.000Z", D, A, "SiliconFlow", content="<reason>same</reason><decision>0.5731</decision>"),
        raw("2026-09-06T10:01:00.000Z", D, A, "SiliconFlow", content="<reason>same</reason><decision>0.5731</decision>"),
        raw("2026-09-06T10:02:00.000Z", D, A, "SiliconFlow"),
        raw("2026-09-06T10:03:00.000Z", Q, A, "DeepInfra", attempts=3), raw("2026-09-06T10:04:00.000Z", Q, A, "DeepInfra"),
        raw("2026-09-06T10:05:00.000Z", Q, B, "DeepInfra", ok=False, attempts=7, status=429),
        raw("2026-09-06T10:06:00.000Z", D, B, "SiliconFlow"), raw("2026-09-06T10:07:00.000Z", D, B, "SiliconFlow"),
        raw("2026-09-06T10:08:00.000Z", D, C, "SiliconFlow"), raw("2026-09-06T10:09:00.000Z", D, C, "SiliconFlow"),
        raw("2026-09-06T10:10:00.000Z", Q, C, "SiliconFlow", returned="DeepInfra"),
        raw("2026-09-06T11:00:10.000Z", Q, C, "SiliconFlow"),
    ]
    (d / "L1_runs_raw.jsonl").write_text("".join(json.dumps(r) + "\n" for r in raws), encoding="utf-8")


def self_test() -> None:
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(f"  {'ok' if cond else 'FAIL'}: {msg}")
        ok = ok and bool(cond)

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture(d)
        levels = {L: analyse_level(d, L) for L in LEVELS}
        L1, L2 = levels[1], levels[2]
        check(L2 is None, "a level with no files is reported as absent, not as zero")

        # --- sessions and the checkpoint declaration
        check(len(L1["sessions"]) == 1 and L1["sessions"][0]["harness"] == "abc1234", "session header parsed (harness commit)")
        check(L1["event_limit"] == 3 and L1["planned"] == 6, "event limit and planned ladders read from the status log")

        # --- coverage
        check(L1["booked"]["deepseek-chat-v3-0324"] == 3 and L1["booked"]["qwen3-32b@on"] == 2, "booked per model = 3 / 2")
        check(L1["missing"] == [("qwen3-32b@on", "B_2025-02-02_L1")], "the one unbooked cell is q/B")
        check(L1["events_all_models"] == 2, "events booked on BOTH models = 2 (A, C)")

        # --- failures
        check(L1["fails"] == [("qwen3-32b@on", "B_2025-02-02_L1", "429")], "the FAIL line is an outage, classified 429")
        check(L1["drops"][("qwen3-32b@on", "debate")] == 1 and L1["drops"][("deepseek-chat-v3-0324", "repetition")] == 1,
              "drops per model x rung")
        check(L1["event_drops"] == {"deepseek-chat-v3-0324": 1, "qwen3-32b@on": 0}, "whole-event drops (worker failure) = d:1, q:0")
        check(L1["drop_reasons"]["worker_2 malformed after 2 attempts (format)"] == 3, "drop reasons counted per rung")
        check(L1["malformed"] == {("qwen3-32b@on", "format"): 1, ("deepseek-chat-v3-0324", "truncated"): 1,
                                  ("deepseek-chat-v3-0324", "format"): 1}, "malformed attempts by model x kind")
        check(L1["retried_calls"] == {"qwen3-32b@on": 1, "deepseek-chat-v3-0324": 1}, "ledger attempts>1 per model")

        # --- raw / HTTP
        check(L1["http"]["calls"] == 12 and L1["http"]["failed"] == 1, "12 HTTP calls, 1 failed")
        check(L1["http"]["failed_by"][("qwen3-32b@on", "DeepInfra", 429)] == 1, "failed call keyed by model x provider x status")
        check(L1["http"]["recovered_by"][("qwen3-32b@on", "DeepInfra")] == 1, "one call recovered after retries")
        check(L1["http"]["fallbacks"] == 1, "one provider fallback (returned != pinned) detected")
        check(L1["http"]["collapsed"] == 1 and L1["http"]["collapsed_by"] == {("deepseek-chat-v3-0324", "SiliconFlow"): 1},
              "D24: one byte-identical answer to an identical prompt, keyed by model x provider")
        check(L1["http"]["same_prompt_groups"] == 5, "same-prompt groups counted (one per booked/attempted event x model)")
        check(L1["provider_booked"] == {("qwen3-32b@on", "DeepInfra"): 1, ("qwen3-32b@on", "SiliconFlow"): 1,
                                        ("deepseek-chat-v3-0324", "SiliconFlow"): 3}, "booked ladders per model x provider")

        # --- integrity
        check(L1["identity_violations"] == 1, "one ledger identity violation")
        check(L1["duplicate_outcomes"] == 0 and L1["orphan_ledger_keys"] == 0, "no duplicates, no orphans")

        # --- cost and time (hand-derived)
        cd, cq = L1["cost"]["deepseek-chat-v3-0324"], L1["cost"]["qwen3-32b@on"]
        check(abs(cd["mean"] - 0.0366667) < 1e-6 and abs(cd["median"] - 0.0306) < 1e-9 and abs(cd["max"] - 0.0699) < 1e-9,
              "deepseek $/ladder mean 0.03667, median 0.0306, max 0.0699")
        check(abs(cq["mean"] - 0.03905) < 1e-9, "qwen $/ladder mean 0.03905")
        check(abs(L1["ledger_total_usd"] - 0.1881) < 1e-9, "ledger total $0.1881")
        check(L1["secs"]["deepseek-chat-v3-0324"]["median"] == 177 and L1["secs"]["qwen3-32b@on"]["median"] == 284.5,
              "seconds per ladder, median: d 177, q 284.5")
        check(abs(L1["wall_hours"] - 1.0) < 1e-9 and abs(L1["ladders_per_hour"] - 5.0) < 1e-9, "wall clock 1.0 h, 5 ladders/h")

        proj = project(levels, total_events=840)
        check(proj[1]["remaining"] == {"deepseek-chat-v3-0324": 837, "qwen3-32b@on": 838}, "remaining ladders per model")
        check(abs(proj[1]["usd"] - 63.4142) < 1e-3, "projected remaining L1 cost 837*0.036667 + 838*0.03905 = 63.41")
        check(abs(proj[1]["hours"] - 335.0) < 1e-9, "projected remaining L1 hours 1675/5 = 335")

        text = render(levels, proj, total_events=840, budget=120.0)
        # --- the blinding fence: no forecast, no label, no score
        for leak in ("0.5731", "0.4419", "0.6125", "0.4837", "label", "brier", "Brier"):
            check(leak not in text, f"the report never contains {leak!r}")
        check("qwen3-32b@on | B_2025-02-02_L1" in text and "429" in text, "the unbooked cell and its cause are named")
        check("identity violations: 1" in text, "the integrity line is rendered")
        check("C4 no cost-cap stop: PASS" in text and "C2 ledger identities: FAIL" in text, "verdict lines rendered")
        check("byte-identical answers to an identical prompt: 1 across 5 same-prompt groups" in text
              and "C7 independent sampling (D24): FAIL (1 byte-identical" in text,
              "the D24 line and verdict are rendered")

    print("\nSELF-TEST", "PASSED" if ok else "FAILED")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=str(PILOT_DIR), help="directory holding L1_*/L2_* run files")
    ap.add_argument("--total-events", type=int, default=TOTAL_EVENTS_DEFAULT)
    ap.add_argument("--budget", type=float, default=None, help="USD budget for the WHOLE run, for the verdict")
    ap.add_argument("--out", default=None, help="also write the report to this markdown file")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to a codepage that mangles § and —
    except (AttributeError, ValueError):
        pass
    if a.self_test:
        self_test()
        return
    levels = {L: analyse_level(Path(a.dir), L) for L in LEVELS}
    proj = project(levels, total_events=a.total_events)
    text = render(levels, proj, total_events=a.total_events, budget=a.budget)
    sys.stdout.write(text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
