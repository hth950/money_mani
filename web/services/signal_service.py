"""Signal persistence service."""
import json
import logging
import time
from web.db.connection import get_db

logger = logging.getLogger("money_mani.web.services.signal")

_ticker_name_cache: dict[str, str] = {}
_ticker_name_failures: set[str] = set()
_NAME_BACKFILL_TARGETS = (
    ("scoring_results", "ticker_name"),
    ("signals", "ticker_name"),
    ("positions", "ticker_name"),
    ("paper_positions", "ticker_name"),
    ("signal_performance", "ticker_name"),
    ("decision_events", "ticker_name"),
    ("portfolio_snapshots", "name"),
)


def _missing_ticker_name(ticker, ticker_name) -> bool:
    name = str(ticker_name or "").strip()
    return not name or name == str(ticker).strip() or name.isdigit()


class SignalService:
    """Persist and query trading signals."""

    def save_signal(self, signal_info: dict) -> int:
        """Save a signal dict to the signals table. Returns signal ID.

        Skips if the same strategy+ticker+signal_type already exists today (DB-level dedup).
        """
        with get_db() as db:
            existing = db.execute(
                """SELECT id FROM signals
                   WHERE strategy_name = ? AND ticker = ? AND signal_type = ?
                     AND date(detected_at) = date('now')""",
                (signal_info.get("strategy_name", ""), signal_info["ticker"], signal_info["signal_type"]),
            ).fetchone()
            if existing:
                logger.debug(f"Signal already exists today: {signal_info.get('strategy_name')}/{signal_info['ticker']}")
                return existing["id"]

            cursor = db.execute(
                """INSERT INTO signals (strategy_name, ticker, ticker_name, market,
                   signal_type, price, indicators_json, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal_info.get("strategy_name", ""),
                    signal_info["ticker"],
                    signal_info.get("ticker_name", ""),
                    signal_info.get("market", "KRX"),
                    signal_info["signal_type"],
                    signal_info["price"],
                    json.dumps(signal_info.get("indicators", {}), ensure_ascii=False, default=str),
                    signal_info.get("source", "daily_scan"),
                ),
            )
            return cursor.lastrowid

    def save_signals(self, signals: list[dict], source: str = "daily_scan") -> int:
        """Save multiple signals. Returns count saved."""
        count = 0
        for sig in signals:
            sig["source"] = source
            try:
                self.save_signal(sig)
                count += 1
            except Exception as e:
                logger.error(f"Failed to save signal: {e}")
        return count

    def list_signals(self, ticker: str = None, signal_type: str = None,
                     date_from: str = None, date_to: str = None, limit: int = 100) -> list[dict]:
        """List signals with optional filters."""
        with get_db() as db:
            query = "SELECT * FROM signals WHERE 1=1"
            params = []
            if ticker:
                query += " AND ticker=?"
                params.append(ticker)
            if signal_type:
                query += " AND signal_type=?"
                params.append(signal_type)
            if date_from:
                query += " AND detected_at >= ?"
                params.append(date_from)
            if date_to:
                query += " AND detected_at <= ?"
                params.append(date_to + " 23:59:59")
            query += " ORDER BY detected_at DESC LIMIT ?"
            params.append(limit)
            rows = db.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_signal(self, signal_id: int) -> dict | None:
        with get_db() as db:
            row = db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            return dict(row) if row else None

    def _resolve_action_ticker_names(self, rows) -> dict[str, str]:
        """Resolve unique missing KRX names and backfill allowlisted ledgers."""
        unresolved = {
            str(row["ticker"]).strip()
            for row in rows
            if (row["market"] or "").upper() == "KRX"
            and _missing_ticker_name(row["ticker"], row["ticker_name"])
        }
        if not unresolved:
            return {}

        resolved = {
            ticker: _ticker_name_cache[ticker]
            for ticker in unresolved
            if ticker in _ticker_name_cache
        }
        to_lookup = sorted(
            unresolved - set(resolved) - _ticker_name_failures
        )
        if to_lookup:
            from market_data.krx_fetcher import KRXFetcher

            fetcher = KRXFetcher(delay=0)
            for ticker in to_lookup:
                try:
                    name = str(fetcher.get_ticker_name(ticker) or "").strip()
                except Exception as exc:
                    logger.warning("Ticker-name resolution failed for %s: %s", ticker, exc)
                    name = ""
                if name and name != ticker:
                    _ticker_name_cache[ticker] = name
                    resolved[ticker] = name
                else:
                    _ticker_name_failures.add(ticker)

        if resolved:
            try:
                # One transaction keeps all user-facing name-bearing ledgers in
                # sync while preserving any name that was already meaningful.
                with get_db() as db:
                    for ticker, name in resolved.items():
                        for table, name_column in _NAME_BACKFILL_TARGETS:
                            db.execute(
                                f"""UPDATE {table}
                                    SET {name_column}=?
                                    WHERE ticker=? AND UPPER(COALESCE(market, ''))='KRX'
                                      AND ({name_column} IS NULL
                                           OR TRIM({name_column})=''
                                           OR TRIM({name_column})=TRIM(ticker)
                                           OR (TRIM({name_column})<>''
                                               AND TRIM({name_column}) NOT GLOB '*[^0-9]*'))""",
                                (name, ticker),
                            )
            except Exception as exc:
                logger.warning("Ticker-name ledger backfill failed: %s", exc)
        return resolved

    def get_actions(
        self, days: int = 7, *, include_paper_risk: bool = False
    ) -> list[dict]:
        """Return latest scoring-based actions per ticker for the trading dashboard.

        Queries scoring_results for the latest scan_date (same as /scoring page).
        signal_id is NULL for BLOCKED decisions, so we derive action from composite_score.
        Returns one entry per ticker.

        The signals dashboard is a production recommendation surface, so paper
        trades do not alter its tier, risk, or signed snapshot by default.
        Paper-order flows opt in to the isolated paper ledger when they need to
        assess the risk of an additional simulated purchase.
        """
        import json as _json
        with get_db() as db:
            # Use latest scan_date (same as scoring page) for consistency
            latest = db.execute(
                "SELECT MAX(scan_date) FROM scoring_results WHERE source != 'backfill'"
            ).fetchone()
            scan_date = latest[0] if latest and latest[0] else None
            if not scan_date:
                return []

            rows = db.execute(
                """
                SELECT
                    sr.ticker,
                    sr.ticker_name,
                    sr.market,
                    sr.composite_score,
                    sr.decision,
                    sr.block_reason,
                    sr.opportunity_decision,
                    sr.risk_score,
                    sr.risk_level,
                    sr.risk_breakdown_json,
                    sr.recommendation_tier,
                    sr.hard_block_reason,
                    sr.risk_model_version,
                    sr.scan_date,
                    sr.score_breakdown_json,
                    p.position_status,
                    p.pnl_pct
                FROM scoring_results sr
                LEFT JOIN (
                    SELECT
                        ticker,
                        'open' AS position_status,
                        AVG(pnl_pct) AS pnl_pct
                    FROM positions
                    WHERE status = 'open'
                    GROUP BY ticker
                ) p ON sr.ticker = p.ticker
                WHERE sr.scan_date = ? AND sr.source != 'backfill'
                ORDER BY sr.composite_score DESC
                """,
                (scan_date,),
            ).fetchall()
            portfolio_state_rows = self._portfolio_state_rows(
                db, include_paper_risk=include_paper_risk
            )

        resolved_names = self._resolve_action_ticker_names(rows)
        portfolio_revision = self._portfolio_revision(portfolio_state_rows)
        live_context = None
        if any(
            (row["risk_model_version"] or "") in {"", "entry-risk-v1"}
            for row in rows
        ):
            live_context = self._live_portfolio_context(portfolio_state_rows)
        actions: list[dict] = []
        for row in rows:
            score = row["composite_score"] or 0.0
            decision = row["decision"] or ""
            opportunity_score = round(float(score) * 100, 4)

            if score >= 0.65:
                conviction = "HIGH"
            elif score >= 0.50:
                conviction = "MED"
            else:
                conviction = "LOW"

            # Map decision to action, consistent with scoring page
            try:
                block_reason = row["block_reason"] or ""
            except (KeyError, IndexError):
                block_reason = ""
            risk_score = row["risk_score"]
            hard_block_reason = row["hard_block_reason"] or ""
            recommendation_tier = row["recommendation_tier"] or ""
            opportunity_decision = row["opportunity_decision"] or (
                "EXECUTE" if score >= 0.65 else "WATCH" if score >= 0.40 else "SKIP"
            )
            effective_model_version = row["risk_model_version"] or "legacy-v0"

            # Rows written before the two-axis model are kept useful until the
            # next scheduled rescore.  A legacy policy block at a buy-quality
            # score is represented as conditional rather than silently hidden.
            if not recommendation_tier:
                if hard_block_reason:
                    recommendation_tier = "UNAVAILABLE"
                elif score >= 0.65 and decision == "BLOCKED":
                    recommendation_tier = "BUY_CONDITIONAL"
                    risk_score = 50.0 if risk_score is None else risk_score
                elif score >= 0.65:
                    recommendation_tier = "BUY_READY"
                    risk_score = 0.0 if risk_score is None else risk_score
                elif score >= 0.55:
                    recommendation_tier = "EARLY_WATCH"
                elif score >= 0.40:
                    recommendation_tier = "WATCH"
                else:
                    recommendation_tier = "AVOID"

            if recommendation_tier == "BUY_READY":
                action = "BUY"
            elif recommendation_tier == "BUY_CONDITIONAL":
                action = "BUY_CONDITIONAL"
            elif recommendation_tier == "AVOID":
                action = "NONE"
            else:
                action = "WATCH"

            try:
                breakdown = _json.loads(row["score_breakdown_json"] or "{}")
            except Exception:
                breakdown = {}
            try:
                risk_breakdown = _json.loads(row["risk_breakdown_json"] or "{}")
            except Exception:
                risk_breakdown = {}
            if (
                recommendation_tier == "BUY_CONDITIONAL"
                and not risk_breakdown
                and block_reason
            ):
                risk_breakdown = {
                    "legacy_policy": {
                        "score": risk_score,
                        "label": "기존 위험 정책",
                        "reasons": [block_reason],
                    }
                }

            # Current-model rows are re-evaluated against the selected ledger
            # context at read time. Dashboard reads exclude paper positions;
            # paper-order reads opt in so their signed risk snapshot changes
            # immediately after a simulated fill. Legacy rows keep their stored
            # score but still carry the selected ledger revision.
            if (
                not hard_block_reason
                and (row["risk_model_version"] or "") in {"", "entry-risk-v1"}
                and live_context is not None
            ):
                live = self._assess_live_entry_risk(
                    row=row,
                    opportunity_score=opportunity_score,
                    component_scores=breakdown,
                    stored_risk_breakdown=risk_breakdown,
                    context=live_context,
                )
                opportunity_decision = live["opportunity_decision"]
                risk_score = live["risk_score"]
                risk_breakdown = live["risk_breakdown"]
                recommendation_tier = live["recommendation_tier"]
                hard_block_reason = live["hard_block_reason"] or ""
                effective_model_version = live["risk_model_version"]

            # Data integrity always outranks a persisted recommendation tier.
            if hard_block_reason:
                recommendation_tier = "UNAVAILABLE"

            if recommendation_tier == "BUY_READY":
                action = "BUY"
            elif recommendation_tier == "BUY_CONDITIONAL":
                action = "BUY_CONDITIONAL"
            elif recommendation_tier == "AVOID":
                action = "NONE"
            else:
                action = "WATCH"

            effective_risk_level = (
                row["risk_level"] or self._risk_level(risk_score)
            )
            if effective_model_version == "entry-risk-v1":
                effective_risk_level = self._risk_level(risk_score)
            risk_snapshot = self._risk_snapshot(
                ticker=row["ticker"],
                market=row["market"] or "KRX",
                scan_date=str(row["scan_date"] or "")[:10],
                opportunity_score=opportunity_score,
                opportunity_decision=opportunity_decision,
                risk_score=risk_score,
                risk_level=effective_risk_level,
                risk_breakdown=risk_breakdown,
                recommendation_tier=recommendation_tier,
                hard_block_reason=hard_block_reason,
                risk_model_version=effective_model_version,
                portfolio_revision=portfolio_revision,
            )

            actions.append({
                "ticker": row["ticker"],
                "ticker_name": resolved_names.get(
                    str(row["ticker"]), row["ticker_name"] or row["ticker"]
                ),
                "market": row["market"] or "KRX",
                "action": action,
                "recommendation": decision,
                "opportunity_score": opportunity_score,
                "opportunity_decision": opportunity_decision,
                "risk_score": risk_score,
                "risk_level": effective_risk_level,
                "risk_breakdown": risk_breakdown,
                "recommendation_tier": recommendation_tier,
                "hard_block_reason": hard_block_reason,
                "risk_model_version": effective_model_version,
                "portfolio_revision": portfolio_revision,
                "risk_snapshot_hash": self._risk_snapshot_hash(risk_snapshot),
                "upgrade_conditions": self._upgrade_conditions(
                    recommendation_tier, opportunity_score, risk_score,
                    risk_breakdown, hard_block_reason,
                ),
                "conviction": conviction,
                "composite_score": score,
                "score_breakdown": breakdown,
                "signal_price": None,
                "last_signal_date": str(row["scan_date"] or "")[:10],
                "is_holding": row["position_status"] == "open",
                "pnl_pct": row["pnl_pct"],
            })

        return actions

    @staticmethod
    def _portfolio_state_rows(
        db, *, include_paper_risk: bool = False
    ) -> list[dict]:
        """Read only the ledger fields that affect entry-risk state.

        This helper deliberately performs no market/sector lookup so it is safe
        to use for the final compare inside a short SQLite write transaction.
        """
        rows = [
            {
                "ledger": "strategy",
                "id": row["id"],
                "market": row["market"] or "KRX",
                "ticker": row["ticker"],
                "quantity": 1,
                "updated_at": row["updated_at"],
            }
            for row in db.execute(
                """SELECT id, market, ticker, updated_at
                   FROM positions WHERE status='open'"""
            ).fetchall()
        ]
        if include_paper_risk:
            rows.extend(
                {
                    "ledger": "paper",
                    "id": row["id"],
                    "market": row["market"],
                    "ticker": row["ticker"],
                    "quantity": row["quantity"],
                    "updated_at": row["updated_at"],
                }
                for row in db.execute(
                    """SELECT id, market, ticker, quantity, updated_at
                       FROM paper_positions WHERE status='open'"""
                ).fetchall()
            )
        return rows

    @staticmethod
    def _portfolio_revision(rows: list[dict]) -> str:
        """Hash only non-secret ledger state that can change entry risk."""
        import hashlib

        canonical_rows = sorted(
            (
                str(row.get("ledger") or ""),
                int(row.get("id") or 0),
                str(row.get("market") or "KRX").upper(),
                str(row.get("ticker") or "").upper(),
                float(row.get("quantity") or 0),
                str(row.get("updated_at") or ""),
            )
            for row in rows
        )
        canonical = json.dumps(
            canonical_rows, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _live_portfolio_context(rows: list[dict]) -> dict:
        from scoring.risk_manager import PortfolioRiskManager

        manager = PortfolioRiskManager()
        positions = []
        seen = set()
        for row in rows:
            market = str(row.get("market") or "KRX").upper()
            ticker = str(row.get("ticker") or "").upper()
            key = (market, ticker)
            if not ticker or key in seen:
                continue
            seen.add(key)
            try:
                sector = manager._get_sector(ticker, market)
            except Exception:
                sector = "Unknown"
            positions.append({
                "market": market,
                "ticker": ticker,
                "sector": sector,
            })
        return {"manager": manager, "positions": positions}

    @staticmethod
    def _assess_live_entry_risk(
        *,
        row,
        opportunity_score: float,
        component_scores: dict,
        stored_risk_breakdown: dict,
        context: dict,
        volatility: dict | None = None,
        data_quality: dict | None = None,
    ) -> dict:
        from scoring.entry_risk_scorer import EntryRiskScorer

        manager = context["manager"]
        market = str(row["market"] or "KRX").upper()
        ticker = str(row["ticker"] or "").upper()
        portfolio_inputs = (
            stored_risk_breakdown.get("portfolio", {}).get("inputs", {})
            if isinstance(stored_risk_breakdown, dict) else {}
        )
        stored_volatility_inputs = (
            stored_risk_breakdown.get("volatility", {}).get("inputs", {})
            if isinstance(stored_risk_breakdown, dict) else {}
        )
        volatility_inputs = (
            volatility if isinstance(volatility, dict)
            else stored_volatility_inputs
        )
        sector = portfolio_inputs.get("sector")
        if not sector and opportunity_score >= 65:
            try:
                sector = manager._get_sector(ticker, market)
            except Exception:
                sector = "Unknown"
        sector = sector or "Unknown"
        legacy_soft_reason = None
        if opportunity_score >= 65:
            try:
                warnings = []
                config = getattr(manager, "config", {}) or {}
                max_positions = int(config.get("max_positions", 10) or 10)
                if (
                    max_positions > 0
                    and len(context["positions"]) >= max_positions
                ):
                    warnings.append(
                        f"포지션 한도 도달 "
                        f"({len(context['positions'])}/{max_positions})"
                    )
                daily_pnl = float(manager._get_daily_pnl())
                max_loss = float(config.get("max_daily_loss", -0.03))
                if daily_pnl < max_loss:
                    warnings.append(
                        f"일일 손실 한도 초과 "
                        f"({daily_pnl:.1%} < {max_loss:.1%})"
                    )
                legacy_soft_reason = " | ".join(warnings) or None
            except Exception as error:
                legacy_soft_reason = f"위험 점검 지연: {error}"
        return EntryRiskScorer().assess(
            opportunity_score=opportunity_score,
            component_scores=component_scores,
            positions=context["positions"],
            sector=sector,
            ticker=ticker,
            market=market,
            volatility=volatility_inputs,
            macro_event={"score": component_scores.get("macro")},
            data_quality=data_quality or {"score_available": True},
            hard_block_reason=row["hard_block_reason"],
            legacy_soft_reason=legacy_soft_reason,
        )

    @staticmethod
    def _risk_level(risk_score) -> str | None:
        if risk_score is None:
            return None
        score = float(risk_score)
        if score <= 35:
            return "LOW"
        if score <= 60:
            return "MEDIUM"
        if score <= 80:
            return "HIGH"
        return "VERY_HIGH"

    @staticmethod
    def _risk_snapshot(**values) -> dict:
        """Return the canonical, user-visible conditional-entry snapshot."""
        return values

    @staticmethod
    def _risk_snapshot_hash(snapshot: dict) -> str:
        import hashlib

        canonical = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _risk_reasons(risk_breakdown: dict) -> list[str]:
        reasons: list[str] = []
        def collect(value):
            if isinstance(value, dict):
                values = value.get("reasons") or value.get("reason") or []
                if isinstance(values, str):
                    values = [values]
                reasons.extend(str(value) for value in values if value)
                for key, nested in value.items():
                    if key not in {"reasons", "reason"}:
                        collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(risk_breakdown)
        return list(dict.fromkeys(reasons))

    @classmethod
    def _upgrade_conditions(
        cls, tier: str, opportunity_score: float, risk_score,
        risk_breakdown: dict, hard_block_reason: str,
    ) -> list[str]:
        conditions: list[str] = []
        if hard_block_reason:
            return [f"판단 불가 사유 해소: {hard_block_reason}"]
        if opportunity_score < 65:
            conditions.append(f"매수 매력도 {65 - opportunity_score:.1f}점 이상 개선")
        if risk_score is not None and float(risk_score) > 35:
            conditions.append(f"진입 위험도 {float(risk_score) - 35:.1f}점 이상 감소")
        if tier == "BUY_CONDITIONAL":
            conditions.extend(cls._risk_reasons(risk_breakdown)[:2])
        return conditions

    def get_exit_scores_for_holdings(self) -> list[dict]:
        """Return exit score info for all open positions from scoring_results."""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT sr.ticker, sr.ticker_name, sr.market,
                       sr.exit_score, sr.exit_decision,
                       sr.composite_score, sr.scan_date,
                       p.entry_price, p.entry_date, p.pnl_pct
                FROM scoring_results sr
                JOIN positions p ON sr.ticker = p.ticker AND p.status = 'open'
                WHERE sr.exit_score IS NOT NULL
                  AND sr.scan_date = (
                      SELECT MAX(scan_date) FROM scoring_results WHERE ticker = sr.ticker
                  )
                ORDER BY sr.exit_score ASC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def get_signal_summary(self, ticker: str, market: str) -> dict:
        """Generate an AI-powered plain-language summary for a ticker's current signals.

        Uses a module-level TTL cache (5 min) to avoid repeated LLM calls.
        Returns {"ticker": str, "summary": str, "generated_at": float}.
        """
        cache_key = f"{ticker}:{market}"
        now = time.time()
        cached = _summary_cache.get(cache_key)
        if cached and now - cached["generated_at"] < 300:
            return cached

        # Gather context
        actions = self.get_actions(days=7)
        item = next((a for a in actions if a["ticker"] == ticker), None)

        try:
            from llm.prompts import SIGNAL_SUMMARY_PROMPT
            from llm.client import OpenRouterClient
            breakdown = item.get("score_breakdown", {}) if item else {}
            composite = item.get("composite_score", 0.0) if item else 0.0
            action = item.get("action", "WATCH") if item else "WATCH"
            prompt = SIGNAL_SUMMARY_PROMPT.format(
                ticker=ticker,
                market=market,
                action=action,
                composite_score=f"{composite:.2f}",
                score_breakdown=json.dumps(breakdown, ensure_ascii=False),
            )
            client = OpenRouterClient()
            summary_text = client.chat(
                messages=[{"role": "user", "content": prompt}],
                model="fast",
                max_tokens=300,
                temperature=0.3,
            )
        except Exception as e:
            logger.warning(f"Signal summary LLM failed for {ticker}: {e}")
            summary_text = f"{ticker} 신호 요약을 생성할 수 없습니다."

        result = {"ticker": ticker, "summary": summary_text, "generated_at": now}
        _summary_cache[cache_key] = result
        return result


# Module-level summary cache: {cache_key: {ticker, summary, generated_at}}
_summary_cache: dict = {}
