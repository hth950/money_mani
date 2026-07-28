"""Manual paper-trading ledger, valuation, and scoring service."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from web.db.connection import get_db
from web.services.paper_quote_service import (
    PaperQuoteService,
    PaperQuoteUnavailable,
)

logger = logging.getLogger("money_mani.web.services.paper_trading")
KST = timezone(timedelta(hours=9))


class PaperTradingConflict(RuntimeError):
    """The requested order conflicts with current recommendation/holdings state."""


class PaperTradingNotFound(RuntimeError):
    """A requested paper position or trade does not exist."""


def _json_loads(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (TypeError, ValueError):
        return default if default is not None else {}


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _round_money(value: float) -> float:
    return round(float(value), 6)


class PaperTradingService:
    """Operate a single-user paper portfolio without a cash balance.

    All broker integration in this service is read-only price retrieval.  The
    immutable ``paper_trades`` table is the source of truth for simulated fills.
    """

    def __init__(self, quote_service=None, scorer=None, exit_scorer=None):
        self.quote_service = quote_service or PaperQuoteService()
        self._scorer = scorer
        self._exit_scorer = exit_scorer
        self.fees = self._load_fees()

    @staticmethod
    def _load_fees() -> dict:
        defaults = {
            "KRX": {"buy": 0.00015, "sell": 0.00195},
            "US": {"buy": 0.0, "sell": 0.0},
        }
        try:
            path = Path(__file__).parents[2] / "config" / "scoring.yaml"
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            configured = config.get("paper_trading", {}).get("fees", {})
            for market in defaults:
                for side in ("buy", "sell"):
                    value = configured.get(market, {}).get(side)
                    if value is not None:
                        rate = float(value)
                        if rate < 0 or not math.isfinite(rate):
                            raise ValueError(f"Invalid {market} {side} fee")
                        defaults[market][side] = rate
        except Exception as exc:
            logger.warning("Paper fee config unavailable; using defaults: %s", exc)
        return defaults

    @staticmethod
    def _payload_value(payload, key, default=None):
        if isinstance(payload, dict):
            return payload.get(key, default)
        return getattr(payload, key, default)

    @staticmethod
    def _entry_date(value) -> str:
        """Convert SQLite's UTC datetime text to the local KST trading date."""
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(KST).date().isoformat()
        except (TypeError, ValueError):
            return str(value or "")[:10]

    def _normalize_order(self, payload) -> dict:
        order = {
            "side": str(self._payload_value(payload, "side", "")).strip().upper(),
            "market": str(self._payload_value(payload, "market", "")).strip().upper(),
            "ticker": str(self._payload_value(payload, "ticker", "")).strip().upper(),
            "quantity": self._payload_value(payload, "quantity"),
            "risk_acknowledged": self._payload_value(
                payload, "risk_acknowledged", False
            ) is True,
            "risk_snapshot_hash": str(
                self._payload_value(payload, "risk_snapshot_hash", "") or ""
            ).strip().lower(),
        }
        if order["side"] not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if order["market"] not in {"KRX", "US"}:
            raise ValueError("market must be KRX or US")
        if isinstance(order["quantity"], bool) or not isinstance(order["quantity"], int):
            raise ValueError("quantity must be an integer")
        if order["quantity"] < 1:
            raise ValueError("quantity must be at least 1")
        if not order["ticker"]:
            raise ValueError("ticker is required")
        return order

    @staticmethod
    def _request_hash(order: dict) -> str:
        canonical = _json_dumps(
            {
                key: order[key]
                for key in (
                    "side", "market", "ticker", "quantity",
                    "risk_acknowledged", "risk_snapshot_hash",
                )
            }
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _fee(self, market: str, side: str, gross: float) -> float:
        rate = self.fees[market][side.lower()]
        return _round_money(gross * rate)

    @staticmethod
    def _scoring_revision(item: dict) -> str:
        """Hash persisted recommendation inputs for a transaction-time CAS."""
        fields = {
            key: item.get(key)
            for key in (
                "id", "scan_date", "market", "ticker", "composite_score",
                "decision", "opportunity_decision", "score_breakdown_json",
                "risk_score", "risk_level", "risk_breakdown_json",
                "recommendation_tier", "hard_block_reason",
                "risk_model_version", "updated_at",
            )
        }
        return hashlib.sha256(
            _json_dumps(fields).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _latest_recommendation(db, market: str, ticker: str) -> dict | None:
        latest = db.execute(
            "SELECT MAX(scan_date) FROM scoring_results WHERE source != 'backfill'"
        ).fetchone()
        scan_date = latest[0] if latest and latest[0] else None
        if not scan_date:
            return None
        row = db.execute(
            """
            SELECT * FROM scoring_results
            WHERE scan_date=? AND market=? AND ticker=? AND source != 'backfill'
            ORDER BY id DESC LIMIT 1
            """,
            (scan_date, market, ticker),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        scores = {
            "technical": item.get("technical_score"),
            "fundamental": item.get("fundamental_score"),
            "flow": item.get("flow_score"),
            "intel": item.get("intel_score"),
            "macro": item.get("macro_score"),
        }
        breakdown = _json_loads(item.get("score_breakdown_json"), {})
        if isinstance(breakdown, dict):
            for key in scores:
                if scores[key] is None and breakdown.get(key) is not None:
                    scores[key] = breakdown[key]
        return {
            "_scoring_revision": PaperTradingService._scoring_revision(item),
            "scan_date": str(item.get("scan_date") or "")[:10],
            "recommendation": item.get("decision"),
            "block_reason": item.get("block_reason") or "",
            "composite_score": item.get("composite_score"),
            "scores": scores,
            "ticker_name": item.get("ticker_name") or ticker,
            "opportunity_score": round(
                float(item.get("composite_score") or 0) * 100, 4
            ),
            "opportunity_decision": item.get("opportunity_decision"),
            "risk_score": item.get("risk_score"),
            "risk_level": item.get("risk_level"),
            "risk_breakdown": _json_loads(item.get("risk_breakdown_json"), {}),
            "recommendation_tier": item.get("recommendation_tier"),
            "hard_block_reason": item.get("hard_block_reason"),
            "risk_model_version": item.get("risk_model_version"),
        }

    @staticmethod
    def _canonical_risk_snapshot(snapshot: dict) -> dict:
        from web.services.signal_service import SignalService

        score = float(snapshot.get("composite_score") or 0)
        tier = snapshot.get("recommendation_tier")
        risk_score = snapshot.get("risk_score")
        hard_block_reason = snapshot.get("hard_block_reason") or ""
        decision = snapshot.get("recommendation") or ""
        if hard_block_reason:
            tier = "UNAVAILABLE"
        elif not tier:
            if hard_block_reason:
                tier = "UNAVAILABLE"
            elif score >= 0.65 and decision == "BLOCKED":
                tier, risk_score = "BUY_CONDITIONAL", (
                    50.0 if risk_score is None else risk_score
                )
            elif score >= 0.65:
                tier, risk_score = "BUY_READY", (
                    0.0 if risk_score is None else risk_score
                )
            elif score >= 0.55:
                tier = "EARLY_WATCH"
            elif score >= 0.40:
                tier = "WATCH"
            else:
                tier = "AVOID"
        risk_breakdown = snapshot.get("risk_breakdown") or {}
        if tier == "BUY_CONDITIONAL" and not risk_breakdown:
            legacy_reason = snapshot.get("block_reason") or (
                "기존 위험 정책에 의해 조건부 후보로 분류됨"
            )
            risk_breakdown = {
                "legacy_policy": {
                    "score": risk_score,
                    "label": "기존 위험 정책",
                    "reasons": [legacy_reason],
                }
            }
        canonical = SignalService._risk_snapshot(
            ticker=snapshot.get("ticker"),
            market=snapshot.get("market"),
            scan_date=snapshot.get("scan_date"),
            opportunity_score=round(score * 100, 4),
            opportunity_decision=snapshot.get("opportunity_decision") or (
                "EXECUTE" if score >= 0.65 else "WATCH" if score >= 0.40 else "SKIP"
            ),
            risk_score=risk_score,
            risk_level=snapshot.get("risk_level") or SignalService._risk_level(risk_score),
            risk_breakdown=risk_breakdown,
            recommendation_tier=tier,
            hard_block_reason=hard_block_reason,
            risk_model_version=snapshot.get("risk_model_version") or "legacy-v0",
            portfolio_revision=snapshot.get("portfolio_revision") or "",
        )
        snapshot.update(canonical)
        snapshot["risk_snapshot_hash"] = SignalService._risk_snapshot_hash(canonical)
        return snapshot

    def _reassess_buy_snapshot_with_quote(
        self, recommendation: dict, quote: dict
    ) -> dict:
        """Recalculate current-model volatility risk using the latest quote.

        All OHLCV/sector work is intentionally done before the ledger write
        transaction. Persisted opportunity scores are not changed here; this
        refresh only updates the separate entry-risk axis.
        """
        if recommendation.get("risk_model_version") != "entry-risk-v1":
            return recommendation

        from scoring.technical_scorer import TechnicalScorer
        from web.services.signal_service import SignalService

        volatility = None
        try:
            frame = self.quote_service.get_ohlcv(
                recommendation["market"], recommendation["ticker"]
            )
            if frame is not None and not frame.empty:
                close_column = "Close" if "Close" in frame.columns else "close"
                previous_close = float(frame[close_column].iloc[-1])
                live_frame = self._with_live_quote(frame, float(quote["price"]))
                details = TechnicalScorer().score(
                    recommendation["ticker"], live_frame
                ).get("details", {})
                volatility = {
                    "atr_pct": details.get("atr_pct"),
                    "gap_pct": (
                        float(quote["price"]) / previous_close - 1
                        if previous_close > 0 else None
                    ),
                }
        except Exception as error:
            logger.info(
                "Order-time volatility unavailable for %s %s: %s",
                recommendation["market"], recommendation["ticker"], error,
            )

        with get_db() as db:
            portfolio_rows = SignalService._portfolio_state_rows(db)
        context = SignalService._live_portfolio_context(portfolio_rows)
        stored_breakdown = recommendation.get("risk_breakdown") or {}
        live = SignalService._assess_live_entry_risk(
            row=recommendation,
            opportunity_score=float(recommendation.get("opportunity_score") or 0),
            component_scores=recommendation.get("scores") or {},
            stored_risk_breakdown=stored_breakdown,
            context=context,
            volatility=volatility,
            data_quality={
                "score_available": True,
                "price_available": True,
                "price_delayed": bool(quote.get("is_delayed")),
            },
        )
        recommendation.update({
            "opportunity_decision": live.get("opportunity_decision"),
            "risk_score": live.get("risk_score"),
            "risk_level": live.get("risk_level"),
            "risk_breakdown": live.get("risk_breakdown") or {},
            "recommendation_tier": live.get("recommendation_tier"),
            "hard_block_reason": live.get("hard_block_reason"),
            "risk_model_version": live.get("risk_model_version"),
            "portfolio_revision": SignalService._portfolio_revision(
                portfolio_rows
            ),
        })
        return self._canonical_risk_snapshot(recommendation)

    @staticmethod
    def _validate_buy_snapshot(
        recommendation: dict,
        order: dict | None,
        *,
        accepted_conditional_hashes: set[str] | None = None,
    ) -> None:
        tier = recommendation["recommendation_tier"]
        if recommendation.get("hard_block_reason"):
            raise PaperTradingConflict(
                f"현재 매수할 수 없습니다: {recommendation['hard_block_reason']}"
            )
        if tier == "BUY_CONDITIONAL":
            supplied_hash = (order or {}).get("risk_snapshot_hash") or ""
            if not (order or {}).get("risk_acknowledged"):
                raise PaperTradingConflict(
                    "조건부 후보는 위험 내용을 확인하고 '위험을 이해했습니다'에 동의해야 합니다."
                )
            valid_hashes = accepted_conditional_hashes or {
                recommendation["risk_snapshot_hash"]
            }
            if supplied_hash not in valid_hashes:
                raise PaperTradingConflict(
                    "진입 위험 정보가 변경되었습니다. 최신 위험 내용을 다시 확인해 주세요."
                )
        elif tier != "BUY_READY":
            label = {
                "EARLY_WATCH": "관심 후보",
                "WATCH": "관망",
                "AVOID": "제외",
                "UNAVAILABLE": "판단 불가",
            }.get(tier, tier)
            raise PaperTradingConflict(
                f"{recommendation['market']} {recommendation['ticker']}는 "
                f"현재 {label} 상태이므로 매수할 수 없습니다."
            )

    def _require_buy_recommendation(
        self,
        market: str,
        ticker: str,
        order: dict | None = None,
        *,
        quote: dict | None = None,
        allow_prequote_hash: bool = False,
    ) -> dict:
        with get_db() as connection:
            recommendation = self._latest_recommendation(connection, market, ticker)
        if not recommendation:
            raise PaperTradingConflict(
                f"{market} {ticker}는 현재 BUY 추천 종목이 아니므로 매수할 수 없습니다."
            )
        self._require_fresh_recommendation(recommendation)

        # Use the same live portfolio-aware snapshot shown on /signals.  The
        # action service recomputes entry risk for entry-risk-v1 rows and signs
        # the current strategy+paper ledger revision, so a previous approval
        # becomes stale immediately after another fill.
        from web.services.signal_service import SignalService

        live_action = next(
            (
                item for item in SignalService().get_actions(days=7)
                if str(item.get("market") or "").upper() == market
                and str(item.get("ticker") or "").upper() == ticker
            ),
            None,
        )
        if live_action:
            recommendation.update({
                "opportunity_decision": live_action.get("opportunity_decision"),
                "risk_score": live_action.get("risk_score"),
                "risk_level": live_action.get("risk_level"),
                "risk_breakdown": live_action.get("risk_breakdown") or {},
                "recommendation_tier": live_action.get("recommendation_tier"),
                "hard_block_reason": live_action.get("hard_block_reason"),
                "risk_model_version": live_action.get("risk_model_version"),
                "portfolio_revision": live_action.get("portfolio_revision"),
            })
        recommendation["market"] = market
        recommendation["ticker"] = ticker
        recommendation = self._canonical_risk_snapshot(recommendation)
        prequote_hash = recommendation["risk_snapshot_hash"]
        prequote_tier = recommendation["recommendation_tier"]
        if quote is not None:
            recommendation = self._reassess_buy_snapshot_with_quote(
                recommendation, quote
            )
        accepted_hashes = {recommendation["risk_snapshot_hash"]}
        if (
            allow_prequote_hash
            and prequote_tier == "BUY_CONDITIONAL"
        ):
            accepted_hashes.add(prequote_hash)
        self._validate_buy_snapshot(
            recommendation,
            order,
            accepted_conditional_hashes=accepted_hashes,
        )
        return recommendation

    @staticmethod
    def _require_fresh_recommendation(recommendation: dict) -> None:
        """Reject stale production recommendations while keeping local replay usable."""
        if os.getenv("MONEY_MANI_ENV", "development").strip().lower() != "production":
            return
        try:
            config_path = Path(__file__).parents[2] / "config" / "risk.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            max_age = int(
                config.get("entry_risk", {}).get(
                    "max_recommendation_age_days", 7
                )
            )
            scan_date = datetime.fromisoformat(
                str(recommendation.get("scan_date") or "")[:10]
            ).date()
        except Exception as error:
            raise PaperTradingConflict(
                "추천 시점을 확인할 수 없어 매수할 수 없습니다."
            ) from error
        age = datetime.now(KST).date() - scan_date
        if age.days < 0 or age.days > max_age:
            raise PaperTradingConflict(
                f"추천이 {age.days}일 경과해 만료되었습니다. 최신 스캔 후 다시 확인해 주세요."
            )

    @staticmethod
    def _get_open_position(db, market: str, ticker: str):
        return db.execute(
            """SELECT * FROM paper_positions
               WHERE market=? AND ticker=? AND status='open' LIMIT 1""",
            (market, ticker),
        ).fetchone()

    def _require_sellable(self, market: str, ticker: str, quantity: int, db=None):
        if db is None:
            with get_db() as connection:
                row = self._get_open_position(connection, market, ticker)
                item = dict(row) if row else None
        else:
            row = self._get_open_position(db, market, ticker)
            item = dict(row) if row else None
        if not item:
            raise PaperTradingConflict(f"{market} {ticker}의 모의 보유 수량이 없습니다.")
        if quantity > int(item["quantity"]):
            raise PaperTradingConflict(
                f"매도 수량 {quantity}주가 보유 수량 {item['quantity']}주를 초과합니다."
            )
        return item

    def preview_order(self, payload) -> dict:
        order = self._normalize_order(payload)
        quote = self.quote_service.get_quote(order["market"], order["ticker"])
        if order["side"] == "BUY":
            snapshot = self._require_buy_recommendation(
                order["market"],
                order["ticker"],
                order=order,
                quote=quote,
                allow_prequote_hash=True,
            )
            ticker_name = snapshot["ticker_name"]
        else:
            position = self._require_sellable(
                order["market"], order["ticker"], order["quantity"]
            )
            ticker_name = position["ticker_name"]
            with get_db() as db:
                snapshot = self._latest_recommendation(
                    db, order["market"], order["ticker"]
                )

        gross = _round_money(quote["price"] * order["quantity"])
        fee = self._fee(order["market"], order["side"], gross)
        total = gross + fee if order["side"] == "BUY" else gross - fee
        return {
            **order,
            "ticker_name": ticker_name,
            "estimated_price": quote["price"],
            "gross_amount": gross,
            "fee": fee,
            "estimated_total": _round_money(total),
            "price_source": quote["source"],
            "price_at": quote["price_at"],
            "is_delayed": bool(quote["is_delayed"]),
            "recommendation_snapshot": snapshot,
        }

    def place_order(self, payload) -> dict:
        order = self._normalize_order(payload)
        key = str(self._payload_value(payload, "idempotency_key", "")).strip()
        if len(key) < 8:
            raise ValueError("idempotency_key must contain at least 8 characters")
        request_hash = self._request_hash(order)

        replay = self._find_idempotent_trade(key, request_hash)
        if replay:
            replay["idempotent_replay"] = True
            replay["quote_changed"] = False
            return replay

        # All market/sector work stays outside the write transaction. The
        # transaction only performs pure compare-and-swap checks against the
        # persisted recommendation and portfolio ledgers.
        quote = self.quote_service.get_quote(order["market"], order["ticker"])
        validated_snapshot = None
        if order["side"] == "BUY":
            validated_snapshot = self._require_buy_recommendation(
                order["market"],
                order["ticker"],
                order=order,
                quote=quote,
            )
        else:
            self._require_sellable(order["market"], order["ticker"], order["quantity"])
        gross = _round_money(quote["price"] * order["quantity"])
        fee = self._fee(order["market"], order["side"], gross)
        order_exit = self._best_effort_order_exit(order, quote)

        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM paper_trades WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise PaperTradingConflict(
                        "동일한 idempotency_key가 다른 주문에 이미 사용되었습니다."
                    )
                trade_id = int(existing["id"])
                position_id = int(existing["position_id"])
            elif order["side"] == "BUY":
                from web.services.signal_service import SignalService

                fresh = self._latest_recommendation(
                    db, order["market"], order["ticker"]
                )
                if (
                    not fresh
                    or fresh.get("_scoring_revision")
                    != validated_snapshot.get("_scoring_revision")
                ):
                    raise PaperTradingConflict(
                        "추천 정보가 변경되었습니다. 최신 추천을 다시 확인해 주세요."
                    )
                current_portfolio_revision = SignalService._portfolio_revision(
                    SignalService._portfolio_state_rows(db)
                )
                if (
                    current_portfolio_revision
                    != validated_snapshot.get("portfolio_revision")
                ):
                    raise PaperTradingConflict(
                        "진입 위험 정보가 변경되었습니다. 최신 위험 내용을 다시 확인해 주세요."
                    )
                snapshot = validated_snapshot
                position = self._get_open_position(db, order["market"], order["ticker"])
                added_cost = gross + fee
                if position:
                    old_qty = int(position["quantity"])
                    new_qty = old_qty + order["quantity"]
                    avg_price = (
                        float(position["avg_price"]) * old_qty
                        + quote["price"] * order["quantity"]
                    ) / new_qty
                    remaining_cost = float(position["remaining_cost"]) + added_cost
                    position_id = int(position["id"])
                    db.execute(
                        """
                        UPDATE paper_positions
                        SET quantity=?, avg_price=?, remaining_cost=?,
                            total_buy_fees=total_buy_fees+?, ticker_name=?,
                            updated_at=datetime('now')
                        WHERE id=?
                        """,
                        (new_qty, avg_price, remaining_cost, fee,
                         snapshot["ticker_name"], position_id),
                    )
                else:
                    cursor = db.execute(
                        """
                        INSERT INTO paper_positions
                          (market, ticker, ticker_name, status, quantity,
                           avg_price, remaining_cost, total_buy_fees)
                        VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
                        """,
                        (order["market"], order["ticker"], snapshot["ticker_name"],
                         order["quantity"], quote["price"], added_cost, fee),
                    )
                    position_id = int(cursor.lastrowid)
                trade_id = self._insert_trade(
                    db, position_id, order, snapshot, quote, gross, fee,
                    key, request_hash, allocated_cost=None, realized_pnl=None,
                )
                current = db.execute(
                    "SELECT * FROM paper_positions WHERE id=?", (position_id,)
                ).fetchone()
                self._insert_mark(
                    db, dict(current), quote, snapshot, order_exit, "order"
                )
            else:
                position = self._require_sellable(
                    order["market"], order["ticker"], order["quantity"], db=db
                )
                previous_scoring, previous_exit = self._latest_mark_snapshots(
                    db, int(position["id"])
                )
                old_qty = int(position["quantity"])
                allocated_cost = float(position["remaining_cost"]) * (
                    order["quantity"] / old_qty
                )
                realized_pnl = gross - fee - allocated_cost
                new_qty = old_qty - order["quantity"]
                remaining_cost = (
                    0.0 if new_qty == 0
                    else max(0.0, float(position["remaining_cost"]) - allocated_cost)
                )
                status = "closed" if new_qty == 0 else "open"
                db.execute(
                    """
                    UPDATE paper_positions
                    SET status=?, quantity=?, remaining_cost=?,
                        cumulative_realized_pnl=cumulative_realized_pnl+?,
                        total_sell_fees=total_sell_fees+?,
                        closed_at=CASE WHEN ?='closed' THEN datetime('now') ELSE NULL END,
                        updated_at=datetime('now')
                    WHERE id=?
                    """,
                    (status, new_qty, remaining_cost, realized_pnl, fee,
                     status, position["id"]),
                )
                snapshot = self._latest_recommendation(
                    db, order["market"], order["ticker"]
                ) or previous_scoring
                snapshot.setdefault("ticker_name", position["ticker_name"])
                trade_id = self._insert_trade(
                    db, int(position["id"]), order, snapshot, quote, gross, fee,
                    key, request_hash, allocated_cost, realized_pnl,
                )
                position_id = int(position["id"])
                current = db.execute(
                    "SELECT * FROM paper_positions WHERE id=?", (position_id,)
                ).fetchone()
                self._insert_mark(
                    db, dict(current), quote, snapshot,
                    order_exit or previous_exit, "order"
                )

        result = {
            "trade": self._get_trade(trade_id),
            "position": self.get_position(position_id),
            "idempotent_replay": bool(existing),
        }
        preview_price = self._payload_value(payload, "preview_price")
        if preview_price is None:
            preview_price = self._payload_value(payload, "estimated_price")
        try:
            result["quote_changed"] = (
                preview_price is not None
                and not math.isclose(float(preview_price), float(quote["price"]), rel_tol=1e-9)
            )
        except (TypeError, ValueError):
            result["quote_changed"] = False
        return result

    @staticmethod
    def _with_live_quote(frame, price: float):
        """Return a copy whose final bar reflects the current quote."""
        frame = frame.copy()
        close_column = "Close" if "Close" in frame.columns else "close"
        frame.loc[frame.index[-1], close_column] = price
        for name in ("High", "high"):
            if name in frame.columns:
                frame.loc[frame.index[-1], name] = max(
                    float(frame[name].iloc[-1]), price
                )
        for name in ("Low", "low"):
            if name in frame.columns:
                frame.loc[frame.index[-1], name] = min(
                    float(frame[name].iloc[-1]), price
                )
        return frame

    def _best_effort_order_exit(self, order: dict, quote: dict) -> dict | None:
        """Compute an order-time Exit mark without holding the ledger lock."""
        try:
            with get_db() as db:
                row = self._get_open_position(db, order["market"], order["ticker"])
                position = dict(row) if row else None
            if order["side"] == "BUY":
                if position:
                    old_qty = int(position["quantity"])
                    entry_price = (
                        float(position["avg_price"]) * old_qty
                        + quote["price"] * order["quantity"]
                    ) / (old_qty + order["quantity"])
                    entry_date = self._entry_date(position["opened_at"])
                else:
                    entry_price = quote["price"]
                    entry_date = datetime.now(KST).date().isoformat()
            else:
                if not position:
                    return None
                entry_price = float(position["avg_price"])
                entry_date = self._entry_date(position["opened_at"])
            frame = self.quote_service.get_ohlcv(order["market"], order["ticker"])
            if frame is None or frame.empty:
                return None
            frame = self._with_live_quote(frame, quote["price"])
            if self._exit_scorer is None:
                from scoring.exit_scorer import ExitScorer

                self._exit_scorer = ExitScorer()
            return self._exit_scorer.evaluate(
                ticker=order["ticker"], market=order["market"],
                entry_price=entry_price, entry_date=entry_date, ohlcv_df=frame,
            )
        except Exception as exc:
            logger.info(
                "Order-time Exit scoring unavailable for %s %s: %s",
                order["market"], order["ticker"], exc,
            )
            return None

    def _insert_trade(
        self, db, position_id, order, snapshot, quote, gross, fee,
        key, request_hash, allocated_cost, realized_pnl,
    ) -> int:
        snapshot = snapshot or {}
        from web.services.signal_service import SignalService

        conditional_acknowledged = bool(
            order["side"] == "BUY"
            and snapshot.get("recommendation_tier") == "BUY_CONDITIONAL"
            and order.get("risk_acknowledged")
            and order.get("risk_snapshot_hash")
            == snapshot.get("risk_snapshot_hash")
        )
        acknowledged_reasons = SignalService._risk_reasons(
            snapshot.get("risk_breakdown") or {}
        )
        cursor = db.execute(
            """
            INSERT INTO paper_trades
              (position_id, side, market, ticker, ticker_name, quantity,
               price, gross_amount, fee, allocated_cost, realized_pnl,
               price_source, price_at, is_delayed, recommendation_date,
               recommendation, composite_score, score_snapshot_json,
               risk_score, risk_snapshot_json, risk_acknowledged_at,
               risk_acknowledgement_version, risk_snapshot_hash,
               idempotency_key, request_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position_id, order["side"], order["market"], order["ticker"],
                snapshot.get("ticker_name") or order["ticker"], order["quantity"],
                quote["price"], gross, fee, allocated_cost, realized_pnl,
                quote["source"], str(quote["price_at"]), int(bool(quote["is_delayed"])),
                snapshot.get("scan_date"), snapshot.get("recommendation"),
                snapshot.get("composite_score"), _json_dumps(snapshot),
                snapshot.get("risk_score"),
                _json_dumps({
                    **snapshot,
                    "acknowledged_reasons": acknowledged_reasons
                    if conditional_acknowledged else [],
                }) if snapshot.get("risk_snapshot_hash") else None,
                datetime.now(timezone.utc).isoformat()
                if conditional_acknowledged else None,
                snapshot.get("risk_model_version")
                if conditional_acknowledged else None,
                snapshot.get("risk_snapshot_hash"),
                key, request_hash,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _latest_mark_snapshots(db, position_id: int) -> tuple[dict, dict]:
        row = db.execute(
            """
            SELECT composite_score, technical_score, fundamental_score,
                   flow_score, intel_score, macro_score, score_decision,
                   exit_score, exit_decision, exit_reason,
                   score_snapshot_json, exit_snapshot_json
            FROM paper_position_marks
            WHERE position_id=? ORDER BY id DESC LIMIT 1
            """,
            (position_id,),
        ).fetchone()
        if not row:
            return {}, {}
        scoring = _json_loads(row["score_snapshot_json"], {})
        if not scoring:
            scoring = {
                "composite_score": row["composite_score"],
                "decision": row["score_decision"],
                "scores": {
                    "technical": row["technical_score"],
                    "fundamental": row["fundamental_score"],
                    "flow": row["flow_score"],
                    "intel": row["intel_score"],
                    "macro": row["macro_score"],
                },
            }
        exit_result = _json_loads(row["exit_snapshot_json"], {})
        if not exit_result and row["exit_decision"]:
            exit_result = {
                "exit_score": row["exit_score"],
                "decision": row["exit_decision"],
                "reason": row["exit_reason"],
            }
        return scoring, exit_result

    def _insert_mark(self, db, position, quote, scoring, exit_result, refresh_source):
        scoring = scoring or {}
        scores = scoring.get("scores") or {}
        quantity = int(position["quantity"])
        market_value = quote["price"] * quantity
        sell_fee = self._fee(position["market"], "SELL", market_value)
        unrealized = market_value - sell_fee - float(position["remaining_cost"])
        decision = scoring.get("decision") or scoring.get("recommendation")
        if decision not in {"EXECUTE", "WATCH", "SKIP"}:
            composite = scoring.get("composite_score")
            decision = (
                "EXECUTE" if composite is not None and composite >= 0.65
                else "WATCH" if composite is not None and composite >= 0.40
                else "SKIP" if composite is not None
                else None
            )
        exit_result = exit_result or {}
        db.execute(
            """
            INSERT INTO paper_position_marks
              (position_id, current_price, market_value, estimated_sell_fee,
               unrealized_pnl, composite_score, technical_score,
               fundamental_score, flow_score, intel_score, macro_score,
               score_decision, score_snapshot_json, exit_score, exit_decision,
               exit_reason, exit_snapshot_json, price_source, price_at,
               is_delayed, refresh_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position["id"], quote["price"], market_value, sell_fee, unrealized,
                scoring.get("composite_score"), scores.get("technical"),
                scores.get("fundamental"), scores.get("flow"), scores.get("intel"),
                scores.get("macro"), decision, _json_dumps(scoring),
                exit_result.get("exit_score"), exit_result.get("decision"),
                exit_result.get("reason"), _json_dumps(exit_result), quote["source"],
                str(quote["price_at"]), int(bool(quote["is_delayed"])), refresh_source,
            ),
        )

    def _find_idempotent_trade(self, key: str, request_hash: str) -> dict | None:
        with get_db() as db:
            row = db.execute(
                "SELECT id, position_id, request_hash FROM paper_trades WHERE idempotency_key=?",
                (key,),
            ).fetchone()
        if not row:
            return None
        if row["request_hash"] != request_hash:
            raise PaperTradingConflict(
                "동일한 idempotency_key가 다른 주문에 이미 사용되었습니다."
            )
        return {
            "trade": self._get_trade(int(row["id"])),
            "position": self.get_position(int(row["position_id"])),
        }

    def _get_trade(self, trade_id: int) -> dict:
        with get_db() as db:
            row = db.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            raise PaperTradingNotFound("모의 체결 내역을 찾을 수 없습니다.")
        item = dict(row)
        item["is_delayed"] = bool(item["is_delayed"])
        item["score_snapshot"] = _json_loads(item.pop("score_snapshot_json"), {})
        item["risk_snapshot"] = _json_loads(item.get("risk_snapshot_json"), {})
        item.pop("request_hash", None)
        item["net_amount"] = _round_money(
            item["gross_amount"] + item["fee"]
            if item["side"] == "BUY" else item["gross_amount"] - item["fee"]
        )
        return item

    def _latest_mark_join_query(self, where: str) -> str:
        return f"""
            SELECT p.*, m.current_price, m.market_value, m.estimated_sell_fee,
                   m.unrealized_pnl, m.composite_score, m.technical_score,
                   m.fundamental_score, m.flow_score, m.intel_score, m.macro_score,
                   m.score_decision, m.score_snapshot_json, m.exit_score,
                   m.exit_decision, m.exit_reason, m.exit_snapshot_json,
                   m.price_source, m.price_at, m.is_delayed,
                   m.refresh_source, m.created_at AS marked_at,
                   (SELECT pt.composite_score FROM paper_trades pt
                    WHERE pt.position_id=p.id AND pt.side='BUY'
                    ORDER BY pt.id ASC LIMIT 1) AS entry_composite_score,
                   (SELECT pt.score_snapshot_json FROM paper_trades pt
                    WHERE pt.position_id=p.id AND pt.side='BUY'
                    ORDER BY pt.id ASC LIMIT 1) AS entry_score_snapshot_json
            FROM paper_positions p
            LEFT JOIN paper_position_marks m ON m.id=(
                SELECT pm.id FROM paper_position_marks pm
                WHERE pm.position_id=p.id ORDER BY pm.id DESC LIMIT 1
            )
            {where}
        """

    def _position_dict(self, row) -> dict:
        item = dict(row)
        item["scores"] = {
            "technical": item.pop("technical_score", None),
            "fundamental": item.pop("fundamental_score", None),
            "flow": item.pop("flow_score", None),
            "intel": item.pop("intel_score", None),
            "macro": item.pop("macro_score", None),
        }
        item["score_snapshot"] = _json_loads(item.pop("score_snapshot_json", None), {})
        item["exit_snapshot"] = _json_loads(item.pop("exit_snapshot_json", None), {})
        item["entry_score_snapshot"] = _json_loads(
            item.pop("entry_score_snapshot_json", None), {}
        )
        item["is_delayed"] = bool(item.get("is_delayed")) if item.get("is_delayed") is not None else None
        try:
            opened = datetime.fromisoformat(str(item["opened_at"]).replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            opened = opened.astimezone(KST)
            now = datetime.now(KST)
            item["holding_days"] = max(0, (now.date() - opened.date()).days)
        except (TypeError, ValueError):
            item["holding_days"] = 0
        cost = float(item.get("remaining_cost") or 0)
        unrealized = item.get("unrealized_pnl")
        item["unrealized_pnl_pct"] = (
            round(float(unrealized) / cost * 100, 4)
            if unrealized is not None and cost > 0 else 0.0
        )
        item["total_fees"] = _round_money(
            float(item.get("total_buy_fees") or 0) + float(item.get("total_sell_fees") or 0)
        )
        item["paid_fees"] = item["total_fees"]
        item["fees_paid"] = item["total_fees"]
        item["estimated_exit_fee"] = item.get("estimated_sell_fee")
        item["investment_cost"] = float(item.get("remaining_cost") or 0)
        item["cost"] = item["investment_cost"]
        item["realized_pnl"] = float(item.get("cumulative_realized_pnl") or 0)
        current_composite = item.get("composite_score")
        entry_composite = item.get("entry_composite_score")
        item["composite_score_change"] = (
            round(float(current_composite) - float(entry_composite), 4)
            if current_composite is not None and entry_composite is not None else None
        )
        entry_scores = item["entry_score_snapshot"].get("scores") or {}
        item["score_changes"] = {
            key: (
                round(float(item["scores"][key]) - float(entry_scores[key]), 4)
                if item["scores"].get(key) is not None and entry_scores.get(key) is not None
                else None
            )
            for key in item["scores"]
        }
        return item

    def get_position(self, position_id: int) -> dict:
        with get_db() as db:
            row = db.execute(
                self._latest_mark_join_query("WHERE p.id=?"), (position_id,)
            ).fetchone()
        if not row:
            raise PaperTradingNotFound("모의 포지션을 찾을 수 없습니다.")
        return self._position_dict(row)

    def list_positions(self, status: str = "open") -> list[dict]:
        if status not in {"open", "closed", "all"}:
            raise ValueError("status must be open, closed, or all")
        where = "" if status == "all" else "WHERE p.status=?"
        params = () if status == "all" else (status,)
        with get_db() as db:
            rows = db.execute(
                self._latest_mark_join_query(where) + " ORDER BY p.updated_at DESC, p.id DESC",
                params,
            ).fetchall()
        return [self._position_dict(row) for row in rows]

    def _buy_recommendations(self) -> list[dict]:
        # Reuse the exact action mapping and KRX ticker-name resolver used by
        # /signals. The order transaction still independently revalidates the
        # latest EXECUTE row in SQLite.
        from web.services.signal_service import SignalService

        actions = [
            item for item in SignalService().get_actions(days=7)
            if item.get("recommendation_tier") in {
                "BUY_READY", "BUY_CONDITIONAL"
            }
        ]
        with get_db() as db:
            rows = db.execute(
                """
                SELECT id, market, ticker, quantity FROM paper_positions
                WHERE status='open'
                """
            ).fetchall()
        holdings = {
            (row["market"], row["ticker"]): {
                "paper_position_id": int(row["id"]),
                "held_quantity": int(row["quantity"]),
            }
            for row in rows
        }
        result = []
        seen = set()
        for action in actions:
            item = dict(action)
            key = (item["market"], item["ticker"])
            if key in seen:
                continue
            seen.add(key)
            holding = holdings.get(key, {})
            scores = item.get("score_breakdown") or {}
            result.append({
                "ticker": item["ticker"],
                "ticker_name": item.get("ticker_name") or item["ticker"],
                "market": item["market"],
                "action": "BUY",
                "recommendation": item.get("recommendation") or "EXECUTE",
                "composite_score": item.get("composite_score"),
                "scores": {
                    axis: scores.get(axis) for axis in
                    ("technical", "fundamental", "flow", "intel", "macro")
                },
                "scan_date": str(item.get("last_signal_date") or "")[:10],
                "signal_price": item.get("signal_price"),
                "opportunity_score": item.get("opportunity_score"),
                "risk_score": item.get("risk_score"),
                "risk_level": item.get("risk_level"),
                "risk_breakdown": item.get("risk_breakdown") or {},
                "recommendation_tier": item.get("recommendation_tier"),
                "risk_snapshot_hash": item.get("risk_snapshot_hash"),
                "upgrade_conditions": item.get("upgrade_conditions") or [],
                "paper_position_id": holding.get("paper_position_id"),
                "held_quantity": holding.get("held_quantity", 0),
            })
        return result

    def get_overview(self) -> dict:
        positions = self.list_positions("open")
        summaries = {}
        with get_db() as db:
            for market, currency in (("KRX", "KRW"), ("US", "USD")):
                realized = db.execute(
                    "SELECT COALESCE(SUM(cumulative_realized_pnl), 0) FROM paper_positions WHERE market=?",
                    (market,),
                ).fetchone()[0]
                fees = db.execute(
                    "SELECT COALESCE(SUM(fee), 0) FROM paper_trades WHERE market=?",
                    (market,),
                ).fetchone()[0]
                market_positions = [p for p in positions if p["market"] == market]
                invested = sum(float(p["remaining_cost"]) for p in market_positions)
                market_value = sum(float(p.get("market_value") or 0) for p in market_positions)
                unrealized = sum(float(p.get("unrealized_pnl") or 0) for p in market_positions)
                summaries[market] = {
                    "currency": currency,
                    "position_count": len(market_positions),
                    "investment_cost": _round_money(invested),
                    "invested_cost": _round_money(invested),
                    "market_value": _round_money(market_value),
                    "realized_pnl": _round_money(realized),
                    "unrealized_pnl": _round_money(unrealized),
                    "total_pnl": _round_money(realized + unrealized),
                    "fees": _round_money(fees),
                    "total_fees": _round_money(fees),
                    "paid_fees": _round_money(fees),
                    "estimated_sell_fees": _round_money(sum(
                        float(p.get("estimated_sell_fee") or 0)
                        for p in market_positions
                    )),
                }
        sell = [
            p for p in positions
            if p.get("exit_decision") in {"SELL_WATCH", "SELL_EXECUTE"}
        ]
        sell.sort(
            key=lambda item: (
                item.get("exit_score") is None,
                1.0 if item.get("exit_score") is None else item["exit_score"],
            )
        )
        return {
            "summaries": summaries,
            "buy_recommendations": self._buy_recommendations(),
            "sell_recommendations": sell,
            "positions": positions,
        }

    def list_trades(self, limit: int = 50, offset: int = 0) -> dict:
        with get_db() as db:
            total = int(db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0])
            rows = db.execute(
                "SELECT id FROM paper_trades ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {
            "items": [self._get_trade(int(row["id"])) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_position_marks(self, position_id: int, days: int = 30) -> dict:
        with get_db() as db:
            exists = db.execute(
                "SELECT 1 FROM paper_positions WHERE id=?", (position_id,)
            ).fetchone()
            if not exists:
                raise PaperTradingNotFound("모의 포지션을 찾을 수 없습니다.")
            rows = db.execute(
                """
                SELECT * FROM paper_position_marks
                WHERE position_id=? AND created_at >= datetime('now', ?)
                ORDER BY id ASC
                """,
                (position_id, f"-{days} days"),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["is_delayed"] = bool(item["is_delayed"])
            item["scores"] = {
                key: item.pop(f"{key}_score")
                for key in ("technical", "fundamental", "flow", "intel", "macro")
            }
            item["score_snapshot"] = _json_loads(item.pop("score_snapshot_json"), {})
            item["exit_snapshot"] = _json_loads(item.pop("exit_snapshot_json"), {})
            items.append(item)
        return {"position_id": position_id, "items": items}

    def refresh_open_positions(self, refresh_source: str = "manual") -> dict:
        """Refresh every open paper position, preserving the last good mark.

        Returns ``{total, succeeded, failed, errors}``.  Per-position failures
        are recorded and returned rather than raised.  A database-wide failure
        before positions can be enumerated is allowed to propagate to JobService.
        """
        if refresh_source not in {"manual", "scheduled"}:
            raise ValueError("refresh_source must be manual or scheduled")
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM paper_positions WHERE status='open' ORDER BY id"
            ).fetchall()
        positions = [dict(row) for row in rows]
        result = {"total": len(positions), "succeeded": 0, "failed": 0, "errors": []}

        for position in positions:
            try:
                quote = self.quote_service.get_quote(position["market"], position["ticker"])
                frame = self.quote_service.get_ohlcv(position["market"], position["ticker"])
                if frame is None or frame.empty:
                    raise PaperQuoteUnavailable("점수 계산용 OHLCV가 없습니다.")
                # Exit hard-stop and intraday valuation must see the current quote,
                # not only yesterday's close.
                frame = self._with_live_quote(frame, quote["price"])

                if self._scorer is None:
                    from scoring.multi_layer_scorer import MultiLayerScorer

                    self._scorer = MultiLayerScorer()
                if self._exit_scorer is None:
                    from scoring.exit_scorer import ExitScorer

                    self._exit_scorer = ExitScorer()
                score_result = self._scorer.score(
                    position["ticker"], position["market"], ohlcv_df=frame
                )
                scoring = {
                    "composite_score": score_result.get("composite_score"),
                    "decision": score_result.get("decision"),
                    "scores": score_result.get("scores") or {},
                    "details": score_result.get("details") or {},
                    "weights": score_result.get("weights") or {},
                }
                exit_result = self._exit_scorer.evaluate(
                    ticker=position["ticker"], market=position["market"],
                    entry_price=float(position["avg_price"]),
                    entry_date=self._entry_date(position["opened_at"]), ohlcv_df=frame,
                )
                with get_db() as db:
                    current = db.execute(
                        "SELECT * FROM paper_positions WHERE id=? AND status='open'",
                        (position["id"],),
                    ).fetchone()
                    if not current:
                        result["succeeded"] += 1
                        continue
                    self._insert_mark(
                        db, dict(current), quote, scoring, exit_result, refresh_source
                    )
                    db.execute(
                        """UPDATE paper_positions
                           SET last_refresh_attempt_at=datetime('now'),
                               last_refresh_error=NULL, updated_at=datetime('now')
                           WHERE id=?""",
                        (position["id"],),
                    )
                result["succeeded"] += 1
            except Exception as exc:
                logger.warning(
                    "Paper position refresh failed for %s %s: %s",
                    position["market"], position["ticker"], exc,
                )
                error = {
                    "position_id": int(position["id"]),
                    "market": position["market"],
                    "ticker": position["ticker"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
                result["errors"].append(error)
                result["failed"] += 1
                with get_db() as db:
                    db.execute(
                        """UPDATE paper_positions
                           SET last_refresh_attempt_at=datetime('now'),
                               last_refresh_error=?, updated_at=datetime('now')
                           WHERE id=?""",
                        (error["error"][:1000], position["id"]),
                    )
        return result
