"""DART 공시 이벤트 캘린더 기반 스코어 조정.

실적발표/유상증자/자사주 매입 등 주요 공시 이벤트를 감지하여
runtime multiplier 방식으로 스코어를 조정.

사용법:
    if config.dart_events.enabled:
        multipliers = DartEventScorer().get_multipliers(ticker)
        fund_score *= multipliers.get('fundamental', 1.0)
        flow_score *= multipliers.get('flow', 1.0)

config/scoring.yaml 에 다음 섹터 추가:
    dart_events:
      enabled: false   # 기본 비활성화, 안전 롤아웃
      poll_hour: 6     # 매일 06:00 KST 폴링
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.scoring.dart_events")

# 이벤트 캐시: 일 1회 갱신
_event_cache: dict[str, list[dict]] = {}  # ticker -> [event, ...]
_event_cache_date: str = ""
_event_cache_lock = threading.Lock()


class DartEventScorer:
    """DART 주요사항보고서 기반 이벤트 감지 및 스코어 multiplier 계산."""

    # 이벤트 유형별 multiplier 설정
    EVENT_MULTIPLIERS = {
        "유상증자": {
            "fundamental": 0.85,  # 희석 위험
            "flow": 0.80,
            "description": "유상증자 (주식 희석 위험)",
        },
        "자사주취득": {
            "fundamental": 1.10,  # 주주환원 긍정적
            "flow": 1.05,
            "description": "자사주 매입 (주주환원)",
        },
        "자사주소각": {
            "fundamental": 1.15,
            "flow": 1.08,
            "description": "자사주 소각 (주주가치 향상)",
        },
        "실적발표임박": {
            # D-3 ~ D+1: 불확실성 → 전체 신뢰도 하향
            "fundamental": 0.90,
            "flow": 0.90,
            "description": "실적발표 D-3~D+1 (불확실성 구간)",
        },
        "전환사채": {
            "fundamental": 0.88,
            "flow": 0.85,
            "description": "전환사채 발행 (희석 위험)",
        },
    }

    def get_multipliers(self, ticker: str) -> dict:
        """ticker의 현재 활성 이벤트 기반 multiplier 반환.

        Returns:
            {"fundamental": 1.0, "flow": 1.0}  # 이벤트 없으면 1.0 (중립)
        """
        result = {"fundamental": 1.0, "flow": 1.0}

        if not self._is_enabled():
            return result

        events = self._get_events_for_ticker(ticker)
        if not events:
            return result

        for event in events:
            event_type = event.get("type", "")
            multiplier_cfg = self.EVENT_MULTIPLIERS.get(event_type, {})

            # multiplicative (복수 이벤트 시 곱)
            result["fundamental"] *= multiplier_cfg.get("fundamental", 1.0)
            result["flow"] *= multiplier_cfg.get("flow", 1.0)

            logger.debug(f"Event multiplier for {ticker}: {event_type} → {multiplier_cfg}")

        # Clamp [0.5, 1.5]
        result["fundamental"] = max(0.5, min(1.5, result["fundamental"]))
        result["flow"] = max(0.5, min(1.5, result["flow"]))

        return result

    def _is_enabled(self) -> bool:
        """config/scoring.yaml의 dart_events.enabled 확인."""
        try:
            import os, yaml
            cfg_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'scoring.yaml')
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f)
            return cfg.get('dart_events', {}).get('enabled', False)
        except Exception:
            return False

    def _get_events_for_ticker(self, ticker: str) -> list[dict]:
        """ticker의 오늘 활성 이벤트 목록 반환 (캐시 사용)."""
        global _event_cache, _event_cache_date

        today = datetime.now(KST).strftime("%Y-%m-%d")

        with _event_cache_lock:
            if _event_cache_date != today or not _event_cache:
                # 캐시 만료 → 갱신
                self._refresh_event_cache()

            return _event_cache.get(ticker, [])

    def _refresh_event_cache(self):
        """DART API에서 오늘의 주요 공시 이벤트 갱신."""
        global _event_cache, _event_cache_date

        today = datetime.now(KST).strftime("%Y-%m-%d")
        start_date = (datetime.now(KST) - timedelta(days=7)).strftime("%Y%m%d")
        end_date = datetime.now(KST).strftime("%Y%m%d")

        new_cache: dict[str, list[dict]] = {}

        try:
            from scoring.dart_fundamental import DARTFundamentalClient
            fetcher = DARTFundamentalClient()

            # majorRport: 주요사항보고서 조회
            data = fetcher._get("list.json", {
                "bgn_de": start_date,
                "end_de": end_date,
                "pblntf_ty": "A",  # 주요사항보고서
                "page_no": "1",
                "page_count": "100",
            })

            if data.get("status") == "000" and data.get("list"):
                corp_map = fetcher.get_corp_code_map()
                # corp_code → ticker 역매핑
                ticker_map = {v: k for k, v in corp_map.items()}

                for item in data["list"]:
                    corp_code = item.get("corp_code", "")
                    report_nm = item.get("report_nm", "")
                    rcept_dt = item.get("rcept_dt", "")

                    ticker_code = ticker_map.get(corp_code)
                    if not ticker_code:
                        continue

                    event_type = self._classify_event(report_nm)
                    if event_type:
                        new_cache.setdefault(ticker_code, []).append({
                            "type": event_type,
                            "report_name": report_nm,
                            "date": rcept_dt,
                        })

        except Exception as e:
            logger.warning(f"Failed to refresh DART event cache: {e}")

        # 실적발표 일정 (DART scheduledReport 또는 수동 조회)
        # TODO: DART 실적발표 일정 API 연동

        _event_cache = new_cache
        _event_cache_date = today
        logger.info(f"DART event cache refreshed: {sum(len(v) for v in new_cache.values())} events for {len(new_cache)} tickers")

    def _classify_event(self, report_nm: str) -> Optional[str]:
        """공시 제목으로 이벤트 유형 분류."""
        report_nm_lower = report_nm

        if any(k in report_nm_lower for k in ["유상증자", "주주배정", "일반공모증자"]):
            return "유상증자"
        if "자기주식취득" in report_nm_lower or "자사주취득" in report_nm_lower:
            return "자사주취득"
        if "자기주식소각" in report_nm_lower or "자사주소각" in report_nm_lower:
            return "자사주소각"
        if any(k in report_nm_lower for k in ["전환사채", "신주인수권부사채", "교환사채"]):
            return "전환사채"

        return None


def refresh_event_cache():
    """스케줄러에서 매일 06:00 KST에 호출."""
    scorer = DartEventScorer()
    with _event_cache_lock:
        scorer._refresh_event_cache()
