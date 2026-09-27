"""Build the thesis database end to end from the provider APIs.

    python run_all.py                 # full build
    python run_all.py --smoke 5       # 5 symbols, end-to-end check
    python run_all.py --skip-master   # skip the optional company master data

Requires EODHD_API_KEY and ALPHA_VANTAGE_API_KEY (see README).
Runs the ingestion scripts in dependency order and stops at the first failure.

Approximate cost of a full build:

    EODHD           6 calls (earnings calendar)
                  501 calls (daily prices)
                  501 calls (company master data, optional)
    Alpha Vantage 530 calls (income statements)
                  530 calls (shares outstanding)
                    1 call  (SPY benchmark)
                    6 calls (macro series)
                 ~2950 calls (news, one per event)  <- the long pole, ~45-55 min
    matteoiacoviello.com
                    2 downloads (AI-GPR index, trade policy uncertainty)
    policyuncertainty.com
                    1 download (US economic policy uncertainty)
    cboe.com        1 download (VIX)

    All four downloads are cached as CSV, so a rebuild needs no network for them.

Level-1 data is everything up to script 09. Scripts 11-15 build the level-2
context tier; skip them with --level1-only if only the base dataset is needed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE / "scripts"

PIPELINE = [
    ("00_init_db.py", ["--reset"], "schema"),
    ("01_eodhd_company_master.py", [], "company master data (optional)"),
    ("02_eodhd_earnings_eps.py", [], "EPS actual + estimate"),
    ("03_eodhd_earnings_surprise.py", ["--verify"], "EPS surprise %"),
    ("04_eodhd_earnings_timing.py", [], "announcement timing"),
    ("05_eodhd_daily_prices.py", [], "daily OHLCV"),
    ("06_alphavantage_income_statement.py", [], "income statements"),
    ("07_alphavantage_market_benchmark.py", [], "market benchmark (SPY)"),
    ("08_alphavantage_shares_outstanding.py", [], "shares outstanding"),
    ("09_build_events.py", [], "event set (derived)"),
    ("11_derive_context_features.py", [], "context features (derived)"),
    ("12_alphavantage_news.py", [], "news + sentiment per event"),
    ("13_alphavantage_macro.py", [], "macro series"),
    ("14_geopolitical_risk.py", [], "geopolitical risk index"),
    ("15_macro_event_calendar.py", [], "macro releases in the forecast window"),
    ("16_uncertainty_indices.py", [], "trade + economic policy uncertainty"),
    ("17_vix.py", [], "VIX implied volatility"),
    ("18_derive_event_regime.py", [], "regime snapshot per event (derived)"),
    ("19_define_levels.py", [], "pin the level-1 / level-2 split"),
    ("10_validate.py", [], "validation report"),
]

NO_LIMIT = {"00_init_db.py", "07_alphavantage_market_benchmark.py",
            "09_build_events.py", "11_derive_context_features.py",
            "13_alphavantage_macro.py", "14_geopolitical_risk.py",
            "15_macro_event_calendar.py", "16_uncertainty_indices.py", "17_vix.py",
            "18_derive_event_regime.py", "19_define_levels.py", "10_validate.py"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", type=int, metavar="N", help="Limit each step to N symbols.")
    ap.add_argument("--skip-master", action="store_true",
                    help="Skip company master data (saves 530 EODHD calls).")
    ap.add_argument("--level1-only", action="store_true",
                    help="Stop after the event set; skip the level-2 context scripts.")
    args = ap.parse_args()

    for script, extra, label in PIPELINE:
        if args.skip_master and script.startswith("01_"):
            print(f"\n=== SKIP {script} ({label})")
            continue
        cmd = [sys.executable, str(SCRIPTS / script), *extra]
        if args.smoke and script not in NO_LIMIT:
            cmd += ["--limit", str(args.smoke)]

        print(f"\n{'=' * 68}\n=== {script} — {label}\n{'=' * 68}")
        result = subprocess.run(cmd, cwd=HERE)
        if result.returncode != 0:
            print(f"\nFAILED: {script} exited {result.returncode}")
            return result.returncode

    print("\nPipeline complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
