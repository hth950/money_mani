"""통합 재스코어링: 오늘 scoring_results의 모든 종목을 최신 캐시 기반으로 재계산."""

import json
import logging
from datetime import datetime, timedelta, timezone
from scoring.multi_layer_scorer import _load_scoring_config

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.pipeline.rescore")

# scoring.yaml에서 시장별 기본 가중치 로드
_scoring_cfg = _load_scoring_config()
_DEFAULT_WEIGHTS = _scoring_cfg.get("weights", {})


def _resolve_decision(composite: float, ticker: str, market: str,
                      signal_action: str | None, risk_mgr) -> tuple[str, str | None]:
    """Apply score thresholds, then gate only executable BUY decisions.

    Portfolio buy limits must not turn WATCH/SKIP or SELL recommendations into
    BLOCKED rows: those decisions do not open a new long position.
    """
    if composite >= 0.65:
        decision = "EXECUTE"
    elif composite >= 0.40:
        return "WATCH", None
    else:
        return "SKIP", None

    if (signal_action or "").upper() != "BUY":
        return decision, None
    allowed, block_reason = risk_mgr.check_can_buy(ticker, market)
    return ("EXECUTE", None) if allowed else ("BLOCKED", block_reason)


def _rescore_item(item: dict, collectors: tuple, risk_mgr, scoring_service,
                  *, technical_override: float | None = None,
                  signal_action_override: str | None = None,
                  trigger: str = "scheduled",
                  record_event: bool = False) -> dict | None:
    """Recalculate and persist one row, optionally auditing a signal trigger."""
    fund_col, flow_col, macro_col, intel_col = collectors
    ticker = item["ticker"]
    market = item["market"]
    technical_score = technical_override
    if technical_score is None:
        technical_score = item.get("technical_score")
    if technical_score is None:
        technical_score = 0.5

    fund_score = fund_col.score(ticker, market).get("score", 0.5)
    flow_score = flow_col.score(ticker, market).get("score", 0.5)
    macro_score = macro_col.score(market=market).get("score", 0.5)
    intel_score = intel_col.score(ticker, market).get("score", 0.5)

    try:
        weights = json.loads(item.get("weights_used_json") or "{}")
    except Exception:
        weights = {}
    market_weights = _DEFAULT_WEIGHTS.get(
        market, _DEFAULT_WEIGHTS.get("KRX", {})
    )
    effective_weights = {
        axis: weights.get(axis, market_weights.get(axis, default))
        for axis, default in {
            "technical": 0.50,
            "fundamental": 0.10,
            "flow": 0.20,
            "intel": 0.10,
            "macro": 0.10,
        }.items()
    }
    composite = round(
        min(
            1.0,
            max(
                0.0,
                technical_score * effective_weights["technical"]
                + fund_score * effective_weights["fundamental"]
                + flow_score * effective_weights["flow"]
                + intel_score * effective_weights["intel"]
                + macro_score * effective_weights["macro"],
            ),
        ),
        4,
    )
    signal_action = signal_action_override or item.get("signal_action")
    if signal_action:
        signal_action = signal_action.upper()
    decision, block_reason = _resolve_decision(
        composite, ticker, market, signal_action, risk_mgr
    )
    scores = {
        "technical": round(float(technical_score), 4),
        "fundamental": round(float(fund_score), 4),
        "flow": round(float(flow_score), 4),
        "intel": round(float(intel_score), 4),
        "macro": round(float(macro_score), 4),
        "composite": composite,
    }
    event_id = scoring_service.update_scoring_result(
        item["id"],
        scores,
        decision,
        block_reason=block_reason,
        weights=effective_weights,
        signal_action=signal_action,
        recommendation=decision,
        execution_state="BLOCKED" if decision == "BLOCKED" else "RESCORE_ONLY",
        provenance={"pipeline": "rescore", "trigger": trigger},
        data_quality={"score_method": "collector_refresh"},
        append_decision_event=record_event,
    )
    if event_id is None:
        return None
    return {
        "event_id": event_id,
        "composite": composite,
        "decision": decision,
        "block_reason": block_reason,
    }



def run_rescore(tickers: list[str] | None = None) -> int:
    """오늘 scoring_results 전 종목(또는 지정 종목)을 최신 캐시로 재스코어링.

    각 Collector의 TTLCache가 살아있으면 API 재호출 없이 빠르게 실행.
    캐시 만료 시에만 실제 API 호출.

    Returns: 업데이트된 종목 수
    """
    from web.db.connection import get_db
    from scoring.data_collectors import FundamentalCollector, FlowCollector, MacroCollector
    from scoring.intel_scorer import IntelScorer

    today = datetime.now(KST).strftime("%Y-%m-%d")
    updated = 0

    with get_db() as db:
        rows = db.execute(
            """SELECT sr.id, sr.ticker, sr.ticker_name, sr.market, sr.scan_date,
                      sr.technical_score, sr.weights_used_json,
                      (SELECT de.signal_action FROM decision_events de
                       WHERE de.scoring_result_id=sr.id
                       ORDER BY de.id DESC LIMIT 1) AS signal_action
               FROM scoring_results sr
               WHERE sr.scan_date = ? ORDER BY sr.id DESC""",
            (today,),
        ).fetchall()

        if not rows:
            # No daily scan today (e.g., weekend): fall back to most recent scan date
            recent = db.execute(
                "SELECT MAX(scan_date) as latest FROM scoring_results"
            ).fetchone()
            latest_date = recent["latest"] if recent else None
            if latest_date and latest_date != today:
                logger.info(f"Rescore: no rows for today ({today}), using latest scan_date={latest_date}")
                rows = db.execute(
                    """SELECT sr.id, sr.ticker, sr.ticker_name, sr.market, sr.scan_date,
                              sr.technical_score, sr.weights_used_json,
                              (SELECT de.signal_action FROM decision_events de
                               WHERE de.scoring_result_id=sr.id
                               ORDER BY de.id DESC LIMIT 1) AS signal_action
                       FROM scoring_results sr
                       WHERE sr.scan_date = ? ORDER BY sr.id DESC""",
                    (latest_date,),
                ).fetchall()

    if not rows:
        logger.info("Rescore: no rows found")
        return 0

    # ticker별 최신 1건만
    seen: set[str] = set()
    to_update: list[dict] = []
    for row in rows:
        if row["ticker"] not in seen:
            seen.add(row["ticker"])
            if tickers is None or row["ticker"] in tickers:
                to_update.append(dict(row))

    fund_col = FundamentalCollector()
    flow_col = FlowCollector()
    macro_col = MacroCollector()
    intel_col = IntelScorer()

    from scoring.risk_manager import PortfolioRiskManager
    risk_mgr = PortfolioRiskManager()
    from web.services.scoring_service import ScoringService
    scoring_service = ScoringService()
    collectors = (fund_col, flow_col, macro_col, intel_col)

    for item in to_update:
        try:
            result = _rescore_item(
                item, collectors, risk_mgr, scoring_service, trigger="scheduled"
            )
            if result is not None:
                updated += 1
        except Exception as e:
            logger.warning(f"Rescore failed for {item['ticker']}: {e}")

    logger.info(f"Rescore complete: {updated}/{len(to_update)} tickers updated")

    # Compute exit scores for open positions (non-critical)
    try:
        from web.services.position_service import PositionService
        from scoring.exit_scorer import ExitScorer
        import yfinance as yf

        exit_scorer = ExitScorer()
        if exit_scorer.enabled:
            open_positions = PositionService().get_open_positions()
            for pos in open_positions:
                try:
                    p_ticker = pos["ticker"]
                    p_market = pos["market"]
                    p_entry_price = pos["entry_price"]
                    p_entry_date = pos["entry_date"]
                    if p_market == "KRX":
                        from market_data.krx_fetcher import download_yahoo_ohlcv
                        df = download_yahoo_ohlcv(
                            p_ticker, period="6mo", auto_adjust=True, yf_module=yf
                        )
                    else:
                        df = yf.download(
                            p_ticker, period="6mo", progress=False, auto_adjust=True
                        )
                    if df is not None and len(df) >= 20:
                        result = exit_scorer.evaluate(p_ticker, p_market, p_entry_price, p_entry_date, df)
                        with get_db() as db:
                            db.execute(
                                "UPDATE scoring_results SET exit_score=?, exit_decision=? "
                                "WHERE ticker=? AND scan_date=(SELECT MAX(scan_date) FROM scoring_results WHERE ticker=?)",
                                (result["exit_score"], result["decision"], p_ticker, p_ticker),
                            )
                        logger.info(f"Exit score updated: {p_ticker} score={result['exit_score']} [{result['decision']}]")
                except Exception as ex:
                    logger.debug(f"Exit score failed for {pos.get('ticker')}: {ex}")
    except Exception:
        pass  # non-critical

    # Save macro snapshot once per rescore run (non-critical)
    try:
        from web.services.macro_service import MacroService
        from scoring.data_collectors import MacroCollector
        macro_result = MacroCollector().score(market="KRX")
        MacroService().save_snapshot(macro_result, market="KRX")
    except Exception:
        pass  # non-critical

    return updated


def rescore_ticker_by_signal(ticker: str, market: str, signal_type: str) -> bool:
    """consensus 전환 시 해당 종목만 즉시 재스코어링.

    technical_score는 consensus 방향으로 근사값 사용 (BUY→0.75, SELL→0.25).
    """
    from web.db.connection import get_db
    from scoring.data_collectors import FundamentalCollector, FlowCollector, MacroCollector
    from scoring.intel_scorer import IntelScorer
    from scoring.risk_manager import PortfolioRiskManager
    from web.services.scoring_service import ScoringService

    today = datetime.now(KST).strftime("%Y-%m-%d")
    proxy_map = {"BUY": 0.75, "SELL": 0.25, "HOLD": 0.50}
    new_tech = proxy_map.get(signal_type.upper(), 0.50)

    with get_db() as db:
        row = db.execute(
            """SELECT id, ticker, ticker_name, market, scan_date,
                      technical_score, weights_used_json
               FROM scoring_results
               WHERE ticker=? AND market=? AND scan_date=?
               ORDER BY id DESC
               LIMIT 1""",
            (ticker, market, today),
        ).fetchone()
        if not row:
            return False
        item = dict(row)

    collectors = (
        FundamentalCollector(), FlowCollector(), MacroCollector(), IntelScorer()
    )
    result = _rescore_item(
        item,
        collectors,
        PortfolioRiskManager(),
        ScoringService(),
        technical_override=new_tech,
        signal_action_override=signal_type,
        trigger="consensus_signal",
        record_event=True,
    )
    if result is None:
        return False
    logger.info(
        "Consensus rescore done: %s %s → composite=%s decision=%s",
        ticker,
        signal_type,
        result["composite"],
        result["decision"],
    )
    return True
