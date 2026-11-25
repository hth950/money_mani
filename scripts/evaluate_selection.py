"""Print the read-only market selection evaluation as JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.services.selection_evaluation_service import SelectionEvaluationService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Read an existing SQLite backup instead of the configured database")
    parser.add_argument("--market", choices=("KRX", "US"), help="Evaluate just one market")
    parser.add_argument("--start-date", help="First included scan date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Last included scan date (YYYY-MM-DD)")
    parser.add_argument("--as-of", help="Outcome cutoff as an ISO timestamp or the end of a KST date")
    parser.add_argument("--output", type=Path, help="Write JSON to a file instead of standard output")
    args = parser.parse_args()
    if args.db is not None and not args.db.is_file():
        parser.error("--db must point to an existing SQLite database")
    try:
        report = SelectionEvaluationService(args.db).build_report(
            market=args.market, start_date=args.start_date, end_date=args.end_date, as_of=args.as_of,
        )
    except ValueError as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        try:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(rendered)
        except FileExistsError:
            parser.error("--output already exists")
    else:
        sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
