"""Write next-open decision outcomes to a new SQLite copy of a backup.

Example:
    python scripts/relabel_decisions.py --input-db backup.db --output-db relabelled.db \
        --cache-dir output/cache --as-of 2026-11-25

CSV files have a date index and Open and Close columns. KRX_^KS11.csv and
US_^GSPC.csv contain the benchmark bars. Use the same split-adjusted,
dividend-unadjusted price convention as --fetch-missing.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from web.services.decision_label_service import (
    LABEL_VERSION,
    ROUND_TRIP_SLIPPAGE_PCT,
    ROUND_TRIP_TRANSACTION_COST_PCT,
    DecisionLabelService,
    exchange_schedule,
    fetch_daily_bars,
    normalise_daily_bars,
    observation_cutoff,
)

logger = logging.getLogger(__name__)


class BarLoader:
    """Read daily CSVs from a supplied cache, optionally filling from Yahoo."""

    def __init__(self, cache_dir: Path | None, fetch_missing: bool):
        self.cache_dir = cache_dir
        self.fetch_missing = fetch_missing

    @lru_cache(maxsize=256)
    def _cached(self, market: str, ticker: str) -> pd.DataFrame:
        if self.cache_dir is None or not re.fullmatch(r"[A-Za-z0-9._^=\-]+", ticker):
            return pd.DataFrame()
        prefix = f"{market}_{ticker}"
        paths = sorted(self.cache_dir.glob(f"{prefix}_[0-9]*_[0-9]*.csv"))
        full_history = self.cache_dir / f"{prefix}.csv"
        if full_history.is_file():
            paths.append(full_history)
        frames = []
        for path in paths:
            try:
                frames.append(normalise_daily_bars(pd.read_csv(path, index_col=0)))
            except (OSError, ValueError) as exc:
                logger.warning("Unable to read %s: %s", path, exc)
        if not frames:
            return pd.DataFrame()
        return normalise_daily_bars(pd.concat(frames))

    def __call__(self, market: str, ticker: str, start: str, end: str) -> pd.DataFrame:
        cached = self._cached(market, ticker)
        if not cached.empty:
            cached = cached[(cached.index >= pd.Timestamp(start)) & (cached.index < pd.Timestamp(end))]
        if not self.fetch_missing:
            return cached
        expected = exchange_schedule(market).loc[start:end].index
        expected = expected[expected < pd.Timestamp(end)]
        if cached.reindex(expected, columns=["open", "close"]).notna().all().all():
            return cached
        try:
            fetched = normalise_daily_bars(fetch_daily_bars(market, ticker, start, end))
        except Exception as exc:
            logger.warning("Unable to fill cache gaps for %s:%s: %s", market, ticker, exc)
            return cached
        return cached.combine_first(fetched)


def relabel_backup(input_db: Path, output_db: Path, *, cache_dir: Path | None,
                   fetch_missing: bool, as_of=None, batch_size: int = 200) -> dict:
    if output_db.is_symlink():
        raise FileExistsError(f"Output already exists: {output_db}")
    input_db, output_db = input_db.resolve(strict=True), output_db.resolve()
    if output_db.exists():
        raise FileExistsError(f"Output already exists: {output_db}")
    if cache_dir is not None and not cache_dir.is_dir():
        raise ValueError(f"Cache directory does not exist: {cache_dir}")
    if not input_db.is_file():
        raise ValueError(f"Input must be a SQLite backup file: {input_db}")
    if not cache_dir and not fetch_missing:
        raise ValueError("Provide --cache-dir or --fetch-missing")
    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    as_of = observation_cutoff(as_of)

    descriptor, name = tempfile.mkstemp(prefix=f".{output_db.name}.", suffix=".tmp", dir=output_db.parent)
    os.close(descriptor)
    temporary_db = Path(name)

    @contextmanager
    def copied_db():
        with closing(sqlite3.connect(temporary_db)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db

    try:
        with closing(sqlite3.connect(input_db.as_uri() + "?mode=ro", uri=True)) as source:
            source.execute("PRAGMA query_only=ON")
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_events'").fetchone():
                raise ValueError("Input backup is missing decision_events")
            with copied_db() as copy:
                source.backup(copy)
                copy.execute("PRAGMA journal_mode=DELETE")
        loader = BarLoader(cache_dir, fetch_missing)
        service = DecisionLabelService(price_loader=loader, benchmark_loader=loader, db_provider=copied_db)
        result = {
            "input_db": str(input_db), "output_db": str(output_db), "label_version": LABEL_VERSION,
            "transaction_cost_pct": ROUND_TRIP_TRANSACTION_COST_PCT, "slippage_pct": ROUND_TRIP_SLIPPAGE_PCT,
            "decision_outcomes": service.relabel_all(as_of=as_of, batch_size=batch_size),
            "shadow_outcomes": service.relabel_all(as_of=as_of, batch_size=batch_size, shadow=True),
        }
        with copied_db() as copy:
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Relabelled copy failed SQLite quick_check")
        # Publish only a completed result, failing if the destination appeared meanwhile.
        os.link(temporary_db, output_db)
        return result
    finally:
        temporary_db.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-db", required=True, type=Path, help="Existing SQLite backup to open read-only")
    parser.add_argument("--output-db", required=True, type=Path, help="New SQLite destination for the relabelled copy")
    parser.add_argument("--cache-dir", type=Path,
                        help="CSV directory with MARKET_TICKER.csv or MARKET_TICKER_YYYYMMDD_YYYYMMDD.csv files")
    parser.add_argument("--fetch-missing", action="store_true", help="Fetch missing public daily bars from Yahoo")
    parser.add_argument("--as-of", help="ISO timestamp or KST date to use as the observation cutoff")
    parser.add_argument("--batch-size", type=int, default=200, help="Maximum number of events per fetch batch")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        result = relabel_backup(**vars(args))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Relabel failed: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
