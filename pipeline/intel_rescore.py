"""인텔 스캔 후 경량 재스코어링: intel_score만 재계산하여 composite_score 업데이트."""

import json
import logging
from datetime import datetime, timedelta, timezone

from scoring.multi_layer_scorer import _load_scoring_config as _load_cfg
_INTEL_SCORING_CFG = _load_cfg()
_INTEL_DEFAULT_WEIGHTS = _INTEL_SCORING_CFG.get("weights", {})

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.pipeline.intel_rescore")


def run_intel_rescore() -> int:
    """오늘 scoring_results 중 최신 1건씩 intel_score 재계산 후 composite_score 업데이트.

    Returns: 업데이트된 ticker 수
    """
    from web.db.connection import get_db
    from scoring.intel_scorer import IntelScorer

    today = datetime.now(KST).strftime("%Y-%m-%d")
    updated = 0

    with get_db() as db:
        # 오늘 스코어링된 종목 조회 (id DESC = 최신순)
        rows = db.execute(
            """
            SELECT id, ticker, market, weights_used_json,
                   technical_score, fundamental_score, flow_score, macro_score
            FROM scoring_results
            WHERE scan_date = ?
            ORDER BY id DESC
            """,
            (today,),
        ).fetchall()

        if not rows:
            logger.debug("No scoring_results for today, skipping intel rescore")
            return 0

        # ticker별 최신 1건만 처리
        seen: set = set()
        to_update = []
        for row in rows:
            if row["ticker"] not in seen:
                seen.add(row["ticker"])
                to_update.append(dict(row))

        scorer = IntelScorer()

        for item in to_update:
            try:
                ticker = item["ticker"]
                market = item["market"]

                # 새 intel_score 계산
                intel_result = scorer.score(ticker, market)
                new_intel = intel_result.get("score", 0.5)

                # 가중치 로드
                try:
                    weights = json.loads(item["weights_used_json"] or "{}")
                except Exception:
                    weights = {}

                _mw = _INTEL_DEFAULT_WEIGHTS.get(market, _INTEL_DEFAULT_WEIGHTS.get("KRX", {}))
                w_tech  = weights.get("technical",   _mw.get("technical",   0.50))
                w_fund  = weights.get("fundamental", _mw.get("fundamental", 0.10))
                w_flow  = weights.get("flow",        _mw.get("flow",        0.20))
                w_intel = weights.get("intel",       _mw.get("intel",       0.10))
                w_macro = weights.get("macro",       _mw.get("macro",       0.10))

                # composite_score 재계산 (다른 축은 기존 값 유지)
                tech  = item["technical_score"]   or 0.5
                fund  = item["fundamental_score"] or 0.5
                flow  = item["flow_score"]        or 0.5
                macro = item["macro_score"]       or 0.5

                new_composite = round(min(1.0, max(0.0,
                    tech  * w_tech  +
                    fund  * w_fund  +
                    flow  * w_flow  +
                    new_intel * w_intel +
                    macro * w_macro
                )), 4)

                db.execute(
                    """
                    UPDATE scoring_results
                    SET intel_score = ?, composite_score = ?
                    WHERE id = ?
                    """,
                    (round(new_intel, 4), new_composite, item["id"]),
                )
                updated += 1

            except Exception as e:
                logger.warning(f"Intel rescore failed for {item['ticker']}: {e}")

    logger.info(f"Intel rescore complete: {updated}/{len(to_update)} tickers updated")
    return updated
