#!/usr/bin/env python3
"""Report or explicitly run the frozen market-intelligence model comparison."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.selection_experiment import CALIBRATION_PAIRS, ExperimentBlocked
from llm.selection_experiment_report import build_report, review_worksheet


def main():
    parser = argparse.ArgumentParser(
        description="Compare the existing Gemini Lite and Flash on identical frozen news; default is an offline report.",
        epilog="Paid mode requires a fresh, exclusive OPENROUTER_EXPERIMENT_KEY with a non-resetting provider cap of $8 "
               "or less and BYOK included in the key's limit. Point MONEY_MANI_DB_PATH at the durable capture database.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="Opt into paid model completions on the dedicated key.")
    mode.add_argument("--review", action="store_true", help="Print a model-blind packet and annotation worksheet.")
    parser.add_argument("--stage", choices=("calibration", "holdout"), default="calibration",
                        help="Holdout uses distinct packets after 20 calibration pairs have settled.")
    parser.add_argument("--limit", type=int, default=CALIBRATION_PAIRS,
                        help="Maximum packet pairs to start this invocation; both stages share a lifetime $8 cap.")
    parser.add_argument("--annotations", type=Path, help="JSON worksheet containing human review annotations.")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    try:
        annotations = json.loads(args.annotations.read_text(encoding="utf-8")) if args.annotations else None
        run = None
        if args.run:
            from llm.selection_experiment_runner import run_comparison

            run = run_comparison(args.stage, args.limit)
        report = review_worksheet() if args.review else build_report(annotations)
        if run is not None:
            report["run"] = run
    except (ExperimentBlocked, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
