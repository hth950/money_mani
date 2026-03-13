"""통합 재스코어링: 오늘 scoring_results의 모든 종목을 최신 캐시 기반으로 재계산."""

import json
import logging
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.pipeline.rescore")


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
            "SELECT id, ticker, market, technical_score, weights_used_json "
            "FROM scoring_results WHERE scan_date = ? ORDER BY id DESC",
            (today,),
        ).fetchall()

    if not rows:
        logger.info("Rescore: no rows for today")
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

    with get_db() as db:
        for item in to_update:
            try:
                ticker = item["ticker"]
                market = item["market"]

                # 각 축 재계산 (캐시 우선, 만료 시 API 호출)
                fund_score = fund_col.score(ticker, market).get("score", 0.5)
                flow_score = flow_col.score(ticker, market).get("score", 0.5)
                macro_score = macro_col.score(ticker, market).get("score", 0.5)
                intel_score = intel_col.score(ticker, market).get("score", 0.5)
                tech_score = item["technical_score"] or 0.5  # 기술적은 daily 값 유지

                try:
                    weights = json.loads(item["weights_used_json"] or "{}")
                except Exception:
                    weights = {}

                w_tech = weights.get("technical", 0.27)
                w_fund = weights.get("fundamental", 0.23)
                w_flow = weights.get("flow", 0.18)
                w_intel = weights.get("intel", 0.22)
                w_macro = weights.get("macro", 0.10)

                new_composite = round(
                    min(
                        1.0,
                        max(
                            0.0,
                            tech_score * w_tech
                            + fund_score * w_fund
                            + flow_score * w_flow
                            + intel_score * w_intel
                            + macro_score * w_macro,
                        ),
                    ),
                    4,
                )

                db.execute(
                    """
                    UPDATE scoring_results
                    SET fundamental_score=?, flow_score=?, macro_score=?,
                        intel_score=?, composite_score=?
                    WHERE id=?
                    """,
                    (
                        round(fund_score, 4),
                        round(flow_score, 4),
                        round(macro_score, 4),
                        round(intel_score, 4),
                        new_composite,
                        item["id"],
                    ),
                )
                updated += 1

            except Exception as e:
                logger.warning(f"Rescore failed for {item['ticker']}: {e}")

    logger.info(f"Rescore complete: {updated}/{len(to_update)} tickers updated")
    return updated


def rescore_ticker_by_signal(ticker: str, market: str, signal_type: str) -> bool:
    """consensus 전환 시 해당 종목만 즉시 재스코어링.

    technical_score는 consensus 방향으로 근사값 사용 (BUY→0.75, SELL→0.25).
    """
    from web.db.connection import get_db
    from scoring.data_collectors import FundamentalCollector, FlowCollector, MacroCollector
    from scoring.intel_scorer import IntelScorer

    today = datetime.now(KST).strftime("%Y-%m-%d")
    proxy_map = {"BUY": 0.75, "SELL": 0.25, "HOLD": 0.50}
    new_tech = proxy_map.get(signal_type.upper(), 0.50)

    with get_db() as db:
        row = db.execute(
            "SELECT id, weights_used_json FROM scoring_results "
            "WHERE ticker=? AND scan_date=? ORDER BY id DESC LIMIT 1",
            (ticker, today),
        ).fetchone()
        if not row:
            return False

        try:
            weights = json.loads(row["weights_used_json"] or "{}")
        except Exception:
            weights = {}

        fund_score = FundamentalCollector().score(ticker, market).get("score", 0.5)
        flow_score = FlowCollector().score(ticker, market).get("score", 0.5)
        macro_score = MacroCollector().score(ticker, market).get("score", 0.5)
        intel_score = IntelScorer().score(ticker, market).get("score", 0.5)

        new_composite = round(
            min(
                1.0,
                max(
                    0.0,
                    new_tech * weights.get("technical", 0.27)
                    + fund_score * weights.get("fundamental", 0.23)
                    + flow_score * weights.get("flow", 0.18)
                    + intel_score * weights.get("intel", 0.22)
                    + macro_score * weights.get("macro", 0.10),
                ),
            ),
            4,
        )

        db.execute(
            """
            UPDATE scoring_results
            SET technical_score=?, fundamental_score=?, flow_score=?,
                macro_score=?, intel_score=?, composite_score=?
            WHERE id=?
            """,
            (
                new_tech,
                round(fund_score, 4),
                round(flow_score, 4),
                round(macro_score, 4),
                round(intel_score, 4),
                new_composite,
                row["id"],
            ),
        )
    logger.info(f"Consensus rescore done: {ticker} {signal_type} → composite={new_composite}")
    return True
