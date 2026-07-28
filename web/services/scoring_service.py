"""Scoring results service for web API."""

import json
import logging
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.scoring_service")


class ScoringService:

    @staticmethod
    def _risk_snapshot(
        *,
        ticker,
        market,
        scan_date,
        composite_score,
        opportunity_decision,
        risk_score,
        risk_level,
        risk_breakdown,
        recommendation_tier,
        hard_block_reason,
        risk_model_version,
    ):
        if all(
            value is None
            for value in (
                risk_score, risk_breakdown, recommendation_tier,
                hard_block_reason, risk_model_version,
            )
        ):
            return None
        return {
            "ticker": ticker,
            "market": market,
            "scan_date": scan_date,
            "opportunity_score": (
                round(float(composite_score) * 100, 4)
                if composite_score is not None else None
            ),
            "opportunity_decision": opportunity_decision,
            "risk_score": risk_score,
            "risk_level": risk_level,
            "risk_breakdown": risk_breakdown,
            "recommendation_tier": recommendation_tier,
            "hard_block_reason": hard_block_reason,
            "risk_model_version": risk_model_version,
        }

    @classmethod
    def _risk_snapshot_json(cls, **values):
        snapshot = cls._risk_snapshot(**values)
        return (
            json.dumps(snapshot, ensure_ascii=False)
            if snapshot is not None else None
        )

    def save_scoring_result(self, ticker, market, scan_date, scores, decision,
                            ticker_name=None, block_reason=None, weights=None,
                            signal_action=None, recommendation=None,
                            execution_state=None, score_details=None,
                            consensus_count=None, consensus_strategies=None,
                            provenance=None, data_quality=None, signal_id=None,
                            signal_price=None, opportunity_decision=None,
                            risk_score=None, risk_level=None, risk_breakdown=None,
                            recommendation_tier=None, hard_block_reason=None,
                            risk_model_version=None):
        """Save the current UI row and an immutable decision snapshot.

        ``scoring_results`` remains a latest-value compatibility table because
        existing dashboards query it directly.  Every call also appends to
        ``decision_events`` so a rescore cannot erase the original rationale.
        """
        event_id = None
        try:
            with get_db() as db:
                # Append-only audit record.  Keep this best-effort so an older
                # database can still serve the legacy scoring table until the
                # startup migration has created decision_events.
                try:
                    event_cursor = db.execute("""
                        INSERT INTO decision_events
                        (scoring_result_id, signal_id, signal_price, ticker, ticker_name, market,
                         signal_action, recommendation, execution_state, scan_date,
                         composite_score, score_breakdown_json, score_details_json,
                         weights_used_json, consensus_count, consensus_strategies_json,
                         block_reason, opportunity_decision, risk_score, risk_level,
                         risk_breakdown_json, risk_snapshot_json,
                         recommendation_tier, hard_block_reason,
                         risk_model_version, provenance_json, data_quality_json)
                        VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        signal_id,
                        signal_price if signal_price is not None else scores.get("signal_price"),
                        ticker,
                        ticker_name or ticker,
                        market,
                        signal_action,
                        recommendation or decision,
                        execution_state or "NOT_EXECUTED",
                        scan_date,
                        scores.get("composite"),
                        json.dumps(scores, ensure_ascii=False),
                        json.dumps(score_details, ensure_ascii=False) if score_details is not None else None,
                        json.dumps(weights, ensure_ascii=False) if weights else None,
                        consensus_count,
                        json.dumps(consensus_strategies, ensure_ascii=False)
                        if consensus_strategies is not None else None,
                        block_reason,
                        opportunity_decision or decision,
                        risk_score,
                        risk_level,
                        json.dumps(risk_breakdown, ensure_ascii=False)
                        if risk_breakdown is not None else None,
                        self._risk_snapshot_json(
                            ticker=ticker,
                            market=market,
                            scan_date=scan_date,
                            composite_score=scores.get("composite"),
                            opportunity_decision=opportunity_decision or decision,
                            risk_score=risk_score,
                            risk_level=risk_level,
                            risk_breakdown=risk_breakdown,
                            recommendation_tier=recommendation_tier,
                            hard_block_reason=hard_block_reason,
                            risk_model_version=risk_model_version,
                        ),
                        recommendation_tier,
                        hard_block_reason,
                        risk_model_version,
                        json.dumps(provenance, ensure_ascii=False) if provenance is not None else None,
                        json.dumps(data_quality, ensure_ascii=False) if data_quality is not None else None,
                    ))
                    event_id = event_cursor.lastrowid
                except Exception as event_error:
                    logger.warning("Failed to append decision event for %s: %s", ticker, event_error)

                db.execute("""
                    DELETE FROM scoring_results
                    WHERE ticker = ? AND scan_date = ?
                """, (ticker, scan_date))
                scoring_cursor = db.execute("""
                    INSERT INTO scoring_results
                    (ticker, ticker_name, market, scan_date, technical_score, fundamental_score,
                     flow_score, intel_score, macro_score, composite_score, score_breakdown_json,
                     decision, block_reason, opportunity_decision, risk_score, risk_level,
                     risk_breakdown_json, recommendation_tier, hard_block_reason,
                     risk_model_version, weights_used_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ticker, ticker_name or ticker, market, scan_date,
                    scores.get("technical"), scores.get("fundamental"),
                    scores.get("flow"), scores.get("intel"),
                    scores.get("macro"),
                    scores.get("composite"),
                    json.dumps(scores, ensure_ascii=False),
                    decision, block_reason,
                    opportunity_decision or decision,
                    risk_score,
                    risk_level,
                    json.dumps(risk_breakdown, ensure_ascii=False)
                    if risk_breakdown is not None else None,
                    recommendation_tier,
                    hard_block_reason,
                    risk_model_version,
                    json.dumps(weights, ensure_ascii=False) if weights else None,
                ))
                if event_id:
                    db.execute(
                        "UPDATE decision_events SET scoring_result_id=? WHERE id=?",
                        (scoring_cursor.lastrowid, event_id),
                    )
            return event_id
        except Exception as e:
            logger.error(f"Failed to save scoring result: {e}")
            # The connection context rolled the whole transaction back.  Do
            # not leak the lastrowid of an audit row that no longer exists.
            return None

    def update_scoring_result(
        self,
        scoring_result_id,
        scores,
        decision,
        *,
        block_reason=None,
        weights=None,
        signal_action=None,
        recommendation=None,
        execution_state="RESCORE_ONLY",
        score_details=None,
        consensus_count=None,
        consensus_strategies=None,
        provenance=None,
        data_quality=None,
        signal_id=None,
        signal_price=None,
        append_decision_event=True,
        opportunity_decision=None,
        risk_score=None,
        risk_level=None,
        risk_breakdown=None,
        recommendation_tier=None,
        hard_block_reason=None,
        risk_model_version=None,
    ):
        """Atomically update a latest score row and append its audit event.

        Rescore paths use this method so the displayed decision, block reason,
        and breakdown cannot diverge.  Signal-triggered calls append an event
        atomically; scheduled cache refreshes set ``append_decision_event`` to
        false so they do not inflate the outcome cohort every few minutes.
        """
        try:
            with get_db() as db:
                current = db.execute(
                    "SELECT ticker, ticker_name, market, scan_date "
                    "FROM scoring_results WHERE id=?",
                    (scoring_result_id,),
                ).fetchone()
                if not current:
                    return None

                update_cursor = db.execute(
                    """
                    UPDATE scoring_results
                    SET technical_score=?, fundamental_score=?, flow_score=?,
                        intel_score=?, macro_score=?, composite_score=?,
                        score_breakdown_json=?, decision=?, block_reason=?,
                        opportunity_decision=?, risk_score=?, risk_level=?,
                        risk_breakdown_json=?, recommendation_tier=?, hard_block_reason=?,
                        risk_model_version=?, weights_used_json=?
                    WHERE id=?
                    """,
                    (
                        scores.get("technical"),
                        scores.get("fundamental"),
                        scores.get("flow"),
                        scores.get("intel"),
                        scores.get("macro"),
                        scores.get("composite"),
                        json.dumps(scores, ensure_ascii=False),
                        decision,
                        block_reason,
                        opportunity_decision or decision,
                        risk_score,
                        risk_level,
                        json.dumps(risk_breakdown, ensure_ascii=False)
                        if risk_breakdown is not None else None,
                        recommendation_tier,
                        hard_block_reason,
                        risk_model_version,
                        json.dumps(weights, ensure_ascii=False)
                        if weights is not None else None,
                        scoring_result_id,
                    ),
                )
                if update_cursor.rowcount != 1:
                    raise RuntimeError(
                        f"scoring result disappeared during rescore: {scoring_result_id}"
                    )

                event_id = 0
                if append_decision_event:
                    event_cursor = db.execute(
                        """
                        INSERT INTO decision_events
                        (scoring_result_id, signal_id, signal_price, ticker, ticker_name, market,
                         signal_action, recommendation, execution_state, scan_date,
                         composite_score, score_breakdown_json, score_details_json,
                         weights_used_json, consensus_count, consensus_strategies_json,
                         block_reason, opportunity_decision, risk_score, risk_level,
                         risk_breakdown_json, risk_snapshot_json,
                         recommendation_tier, hard_block_reason,
                         risk_model_version, provenance_json, data_quality_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            scoring_result_id,
                            signal_id,
                            signal_price if signal_price is not None
                            else scores.get("signal_price"),
                            current["ticker"],
                            current["ticker_name"] or current["ticker"],
                            current["market"],
                            signal_action,
                            recommendation or decision,
                            execution_state,
                            current["scan_date"],
                            scores.get("composite"),
                            json.dumps(scores, ensure_ascii=False),
                            json.dumps(score_details, ensure_ascii=False)
                            if score_details is not None else None,
                            json.dumps(weights, ensure_ascii=False)
                            if weights is not None else None,
                            consensus_count,
                            json.dumps(consensus_strategies, ensure_ascii=False)
                            if consensus_strategies is not None else None,
                            block_reason,
                            opportunity_decision or decision,
                            risk_score,
                            risk_level,
                            json.dumps(risk_breakdown, ensure_ascii=False)
                            if risk_breakdown is not None else None,
                            self._risk_snapshot_json(
                                ticker=current["ticker"],
                                market=current["market"],
                                scan_date=current["scan_date"],
                                composite_score=scores.get("composite"),
                                opportunity_decision=opportunity_decision or decision,
                                risk_score=risk_score,
                                risk_level=risk_level,
                                risk_breakdown=risk_breakdown,
                                recommendation_tier=recommendation_tier,
                                hard_block_reason=hard_block_reason,
                                risk_model_version=risk_model_version,
                            ),
                            recommendation_tier,
                            hard_block_reason,
                            risk_model_version,
                            json.dumps(provenance, ensure_ascii=False)
                            if provenance is not None else None,
                            json.dumps(data_quality, ensure_ascii=False)
                            if data_quality is not None else None,
                        ),
                    )
                    event_id = event_cursor.lastrowid
            return event_id
        except Exception as e:
            logger.error("Failed to update scoring result %s: %s", scoring_result_id, e)
            return None

    def mark_decision_event(self, event_id, execution_state, execution_error=None):
        """Record the execution outcome without changing the original snapshot."""
        if not event_id:
            return False
        try:
            with get_db() as db:
                db.execute("""
                    UPDATE decision_events
                    SET execution_state = ?, execution_error = ?
                    WHERE id = ?
                """, (execution_state, execution_error, event_id))
            return True
        except Exception as e:
            logger.warning("Failed to update decision event %s: %s", event_id, e)
            return False

    def link_decision_event_signal(self, event_id, signal_id):
        """Attach the persisted signal row created after the scoring snapshot."""
        if not event_id or not signal_id:
            return False
        try:
            with get_db() as db:
                db.execute(
                    "UPDATE decision_events SET signal_id=? WHERE id=?",
                    (signal_id, event_id),
                )
            return True
        except Exception as e:
            logger.warning("Failed to link decision event %s to signal: %s", event_id, e)
            return False

    def get_decision_events(self, ticker=None, limit=100):
        """Return immutable decision snapshots for audit/debug views."""
        try:
            with get_db() as db:
                if ticker:
                    rows = db.execute("""
                        SELECT * FROM decision_events
                        WHERE ticker = ?
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    """, (ticker, limit)).fetchall()
                else:
                    rows = db.execute("""
                        SELECT * FROM decision_events
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    """, (limit,)).fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.warning("Failed to read decision events: %s", e)
            return []

    def get_today_results(self, scan_date=None):
        """Get today's scoring results. Falls back to latest scan date if no data for today."""
        if not scan_date:
            from datetime import datetime, timedelta, timezone
            scan_date = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT * FROM scoring_results
                    WHERE scan_date = ? AND source != 'backfill'
                    ORDER BY composite_score DESC
                """, (scan_date,)).fetchall()
                # Fallback: if no data for today, use latest available scan date
                if not rows:
                    latest = db.execute("""
                        SELECT MAX(scan_date) FROM scoring_results
                        WHERE source != 'backfill'
                    """).fetchone()
                    if latest and latest[0]:
                        rows = db.execute("""
                            SELECT * FROM scoring_results
                            WHERE scan_date = ? AND source != 'backfill'
                            ORDER BY composite_score DESC
                        """, (latest[0],)).fetchall()
            results = []
            for r in rows:
                row = dict(r)
                # 보유 중인 종목은 BLOCKED 대신 실제 점수 기반 결정으로 표시
                if row.get("decision") == "BLOCKED" and "이미 포지션 보유 중" in (row.get("block_reason") or ""):
                    score = row.get("composite_score") or 0.0
                    if score >= 0.65:
                        row["decision"] = "EXECUTE"
                    elif score >= 0.40:
                        row["decision"] = "WATCH"
                    else:
                        row["decision"] = "SKIP"
                    row["is_holding"] = True
                else:
                    row["is_holding"] = False
                results.append(row)
            return results
        except Exception as e:
            logger.warning(f"Failed to get today results: {e}")
            return []

    def get_history(self, days=30):
        """Get scoring history for last N days."""
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT scan_date, market,
                           COUNT(*) as total,
                           SUM(CASE WHEN decision='EXECUTE' THEN 1 ELSE 0 END) as execute_count,
                           SUM(CASE WHEN decision='WATCH' THEN 1 ELSE 0 END) as watch_count,
                           SUM(CASE WHEN decision='SKIP' THEN 1 ELSE 0 END) as skip_count,
                           SUM(CASE WHEN decision='BLOCKED' THEN 1 ELSE 0 END) as blocked_count,
                           AVG(composite_score) as avg_score
                    FROM scoring_results
                    GROUP BY scan_date, market
                    ORDER BY scan_date DESC
                    LIMIT ?
                """, (days * 2,)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Failed to get history: {e}")
            return []

    def get_ticker_history(self, ticker, limit=30):
        """Get scoring history for a specific ticker."""
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT * FROM scoring_results
                    WHERE ticker = ?
                    ORDER BY scan_date DESC LIMIT ?
                """, (ticker, limit)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Failed to get ticker history: {e}")
            return []

    def get_summary(self, days=30):
        """Get daily summary data for charts."""
        try:
            with get_db() as db:
                rows = db.execute("""
                    SELECT scan_date,
                           COUNT(*) as total,
                           SUM(CASE WHEN decision='EXECUTE' THEN 1 ELSE 0 END) as execute_count,
                           SUM(CASE WHEN decision='WATCH' THEN 1 ELSE 0 END) as watch_count,
                           AVG(composite_score) as avg_score,
                           AVG(technical_score) as avg_tech,
                           AVG(fundamental_score) as avg_fund,
                           AVG(flow_score) as avg_flow,
                           AVG(intel_score) as avg_intel,
                           AVG(macro_score) as avg_macro
                    FROM scoring_results
                    GROUP BY scan_date
                    ORDER BY scan_date DESC
                    LIMIT ?
                """, (days,)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"Failed to get summary: {e}")
            return []
