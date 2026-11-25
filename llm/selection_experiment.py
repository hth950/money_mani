"""Frozen inputs and durable spending ledger for the market-intel experiment."""

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from web.db import connection


LITE = "google/gemini-3.1-flash-lite-preview"
FLASH = "google/gemini-3-flash-preview"
MODELS = (LITE, FLASH)
CALIBRATION_PAIRS = 20
USD_SCALE = 1_000_000_000
BUDGET = 8 * USD_SCALE
logger = logging.getLogger(__name__)

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS selection_intel_budget_key (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        fingerprint TEXT NOT NULL,
        limit_nanos INTEGER NOT NULL CHECK(limit_nanos > 0)
    )""",
    """CREATE TABLE IF NOT EXISTS selection_intel_packets (
        id INTEGER PRIMARY KEY,
        captured_at TEXT NOT NULL,
        market TEXT NOT NULL,
        scan_type TEXT NOT NULL,
        news_digest TEXT NOT NULL,
        packet_json TEXT NOT NULL,
        production_scan_id INTEGER UNIQUE REFERENCES market_intel_scans(id) ON DELETE SET NULL
    )""",
    """CREATE TABLE IF NOT EXISTS selection_intel_pairs (
        packet_id INTEGER PRIMARY KEY REFERENCES selection_intel_packets(id),
        news_digest TEXT NOT NULL UNIQUE,
        stage TEXT NOT NULL CHECK(stage IN ('calibration', 'holdout')),
        started_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS selection_intel_attempts (
        packet_id INTEGER NOT NULL REFERENCES selection_intel_pairs(packet_id),
        model TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved', 'settled', 'uncertain', 'skipped')),
        reserved_nanos INTEGER NOT NULL CHECK(reserved_nanos >= 0),
        billed_nanos INTEGER CHECK(billed_nanos >= 0),
        finished_at TEXT,
        detail_json TEXT,
        PRIMARY KEY (packet_id, model)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_selection_intel_packets_capture
        ON selection_intel_packets(captured_at, id)""",
)


class ExperimentBlocked(RuntimeError):
    """A paid request cannot be dispatched within the experiment's controls."""


@dataclass(frozen=True)
class ProviderBudget:
    fingerprint: str
    limit: int
    remaining: int


def _json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(data):
    return hashlib.sha256(_json(data).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_experiment_tables(db):
    for statement in _SCHEMA:
        db.execute(statement)


def capture_packet(*, captured_at, market, scan_type, articles, messages, temperature, max_tokens,
                   production_model, detection_date=None) -> int:
    """Persist the exact input before production inference. Articles retain source URL and time."""
    articles = [
        {key: article.get(key) for key in ("url", "source", "date", "title", "snippet")}
        for article in articles
    ]
    headlines = sorted(_digest(article) for article in articles)
    from llm.selection_experiment_replay import freeze_replay_context

    with connection.get_db() as db:
        ensure_experiment_tables(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            context = freeze_replay_context(db, market, captured_at, detection_date)
        except (sqlite3.Error, TypeError, ValueError):
            logger.warning("Could not freeze optional market-intel replay context", exc_info=True)
            context = {"rank": None, "prior_titles": [], "rank_unavailable_reason": "could_not_freeze_replay_context"}
        packet = {
            "articles": articles,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "production_model": production_model,
            **context,
        }
        return db.execute(
            """INSERT INTO selection_intel_packets
               (captured_at, market, scan_type, news_digest, packet_json) VALUES (?, ?, ?, ?, ?)""",
            (captured_at, market, scan_type, _digest([market, headlines]), _json(packet)),
        ).lastrowid


def attach_production_scan(db, packet_id, scan_id):
    if packet_id is not None:
        db.execute(
            "UPDATE selection_intel_packets SET production_scan_id = ? WHERE id = ?",
            (scan_id, packet_id),
        )


def cost_nanos(cost) -> int:
    try:
        value = Decimal(str(cost))
        if not value.is_finite() or value < 0:
            raise ValueError("invalid USD cost")
        # Keep adversarial/unexpected response values inside SQLite's integer range.
        if value > Decimal("1000000"):
            raise ValueError("invalid USD cost")
        return int((value * USD_SCALE).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid USD cost") from exc


def dollars(nanos):
    return nanos / USD_SCALE


def _ledger(db):
    return db.execute(
        """SELECT COALESCE(SUM(CASE WHEN state = 'settled' THEN billed_nanos ELSE 0 END), 0) AS billed,
                  COALESCE(SUM(CASE WHEN state IN ('reserved', 'uncertain') THEN reserved_nanos ELSE 0 END), 0) AS held,
                  COALESCE(SUM(CASE WHEN state = 'settled' AND billed_nanos > reserved_nanos THEN 1 ELSE 0 END), 0) AS breaches
           FROM selection_intel_attempts"""
    ).fetchone()


def _calibration(db):
    return db.execute(
        """SELECT COUNT(*) FILTER(WHERE settled = 2) AS completed,
                  COUNT(*) FILTER(WHERE settled + reserved = 2) AS active_or_completed
           FROM (
               SELECT a.packet_id,
                      SUM(a.state = 'settled') AS settled, SUM(a.state = 'reserved') AS reserved
               FROM selection_intel_attempts a JOIN selection_intel_pairs p USING (packet_id)
               WHERE p.stage = 'calibration' GROUP BY a.packet_id
           )"""
    ).fetchone()


def _read_packet(row):
    return {**dict(row), **json.loads(row["packet_json"]), "packet_json": None}


def reserve_pair(stage, ceilings, provider_budget):
    """Atomically claim one cohort packet and reserve both calls before dispatch."""
    if stage not in {"calibration", "holdout"}:
        raise ValueError("unknown experiment stage")
    with connection.get_db() as db:
        ensure_experiment_tables(db)
        db.execute("BEGIN IMMEDIATE")
        calibration = _calibration(db)
        if stage == "calibration" and calibration["active_or_completed"] >= CALIBRATION_PAIRS:
            return None, "calibration_complete_or_in_flight"
        if stage == "holdout" and calibration["completed"] < CALIBRATION_PAIRS:
            return None, "calibration_incomplete"
        ledger = _ledger(db)
        if ledger["breaches"]:
            raise ExperimentBlocked("A charge exceeded its reservation. Further inference is disabled.")
        key = db.execute("SELECT fingerprint, limit_nanos FROM selection_intel_budget_key").fetchone()
        if key is not None and (key["fingerprint"] != provider_budget.fingerprint or key["limit_nanos"] != provider_budget.limit):
            raise ExperimentBlocked("Use the same dedicated key and lifetime cap bound to this experiment ledger.")
        if key is None and provider_budget.remaining != provider_budget.limit:
            raise ExperimentBlocked("Start with a fresh, dedicated key that has its full lifetime limit available.")
        if db.execute(
            """SELECT 1 FROM selection_intel_attempts WHERE state = 'uncertain'
               AND json_extract(detail_json, '$.reason') IN ('byok_usage', 'model_or_provider_mismatch') LIMIT 1"""
        ).fetchone():
            raise ExperimentBlocked("An earlier call reported a routing or credential mismatch. Further inference is disabled.")
        rows = db.execute(
            """SELECT n.* FROM selection_intel_packets n
               WHERE NOT EXISTS (SELECT 1 FROM selection_intel_pairs p WHERE p.news_digest = n.news_digest)
               AND json_extract(n.packet_json, '$.production_model') = ?
               ORDER BY n.captured_at, n.id""",
            (LITE,),
        )
        reason = "no_distinct_frozen_packets"
        available = min(BUDGET - ledger["billed"], provider_budget.limit - ledger["billed"],
                        provider_budget.remaining) - ledger["held"]
        for row in rows:
            packet = _read_packet(row)
            reservations = ceilings(packet)
            if sum(reservations.values()) > available:
                reason = "budget_remaining_below_pair_reservation"
                continue
            if key is None:
                db.execute("INSERT INTO selection_intel_budget_key VALUES (1, ?, ?)",
                           (provider_budget.fingerprint, provider_budget.limit))
            db.execute(
                "INSERT INTO selection_intel_pairs VALUES (?, ?, ?, ?)",
                (packet["id"], packet["news_digest"], stage, utc_now()),
            )
            db.executemany(
                """INSERT INTO selection_intel_attempts (packet_id, model, state, reserved_nanos)
                   VALUES (?, ?, 'reserved', ?)""",
                [(packet["id"], model, reservations[model]) for model in MODELS],
            )
            return packet, None
        return None, reason


def finish_attempt(packet_id, model, state, details, billed=None):
    with connection.get_db() as db:
        result = db.execute(
            """UPDATE selection_intel_attempts SET state = ?, detail_json = ?, billed_nanos = ?, finished_at = ?
               WHERE packet_id = ? AND model = ? AND state = 'reserved'""",
            (state, _json(details), billed, utc_now(), packet_id, model),
        )
        if result.rowcount != 1:
            raise ExperimentBlocked("Experiment reservation disappeared before settling a request.")


def read_experiment():
    empty = {"ledger": {"billed": 0, "held": 0, "breaches": 0}, "key_limit": None,
             "calibration": {"completed": 0, "active_or_completed": 0}, "packet_counts": [], "pairs": []}
    if not connection.DB_PATH.is_file():
        return empty
    with closing(sqlite3.connect(connection.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'selection_intel_attempts'").fetchone():
            return empty
        key = db.execute("SELECT limit_nanos FROM selection_intel_budget_key").fetchone()
        return {
            "ledger": dict(_ledger(db)),
            "key_limit": key[0] if key is not None else None,
            "calibration": dict(_calibration(db)),
            "packet_counts": [dict(row) for row in db.execute(
                """SELECT market, COUNT(*) AS captured, COUNT(DISTINCT news_digest) AS distinct_news
                   FROM selection_intel_packets GROUP BY market"""
            )],
            "pairs": [
                {**_read_packet(pair), "attempts": [dict(row) for row in db.execute(
                    "SELECT * FROM selection_intel_attempts WHERE packet_id = ? ORDER BY model", (pair["id"],)
                )]}
                for pair in db.execute(
                    """SELECT n.*, p.stage, p.started_at FROM selection_intel_packets n
                       JOIN selection_intel_pairs p ON p.packet_id = n.id ORDER BY p.started_at, n.id"""
                )
            ],
        }
