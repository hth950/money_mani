"""Pure, explainable entry-risk scoring for the two-axis recommendation model."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "model_version": "entry-risk-v1",
    "low_max": 35,
    "medium_max": 60,
    "high_max": 80,
    "opportunity_execute": 65,
    "opportunity_early_watch": 55,
    "opportunity_watch": 40,
    "assumed_position_weight": 0.20,
    "max_sector_weight": 0.30,
    "weights": {
        "portfolio": 0.30,
        "timing": 0.20,
        "volatility": 0.15,
        "macro_event": 0.15,
        "disagreement": 0.10,
        "data_quality": 0.10,
    },
}


def _merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def _load_config() -> dict[str, Any]:
    try:
        with (Path(__file__).parent.parent / "config" / "risk.yaml").open() as handle:
            full = yaml.safe_load(handle) or {}
        section = full.get("entry_risk", {})
        if isinstance(section, Mapping):
            section = dict(section)
            # The existing portfolio setting remains the default comparison limit.
            section.setdefault("max_sector_weight", full.get("max_sector_weight", 0.30))
            return _merge(DEFAULT_CONFIG, section)
    except (OSError, yaml.YAMLError):
        pass
    return deepcopy(DEFAULT_CONFIG)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _score(value: Any) -> float | None:
    """Accept a score expressed as either 0..1 or 0..100."""
    number = _number(value)
    if number is None:
        return None
    if 0 <= number <= 1:
        number *= 100
    return round(min(100.0, max(0.0, number)), 2)


def _fraction(value: Any) -> float | None:
    """Accept a percentage as a fraction or a whole-number percent."""
    number = _number(value)
    if number is None:
        return None
    if abs(number) > 1:
        number /= 100
    return number


def _mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _component(
    score: float, weight: float, reasons: list[str], inputs: Mapping[str, Any]
) -> dict[str, Any]:
    score = round(min(100.0, max(0.0, score)), 2)
    return {
        "score": score,
        "weight": round(weight, 4),
        "weighted_score": round(score * weight, 2),
        "reasons": reasons,
        "inputs": dict(inputs),
    }


class EntryRiskScorer:
    """Calculate an explainable 0..100 entry risk without database access.

    ``assess`` deliberately takes plain mappings/lists so scans, rescores and
    order previews can all use the same deterministic model.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        source: Mapping[str, Any]
        if config is None:
            self.config = _load_config()
        else:
            source = config.get("entry_risk", config) if isinstance(config, Mapping) else {}
            self.config = _merge(DEFAULT_CONFIG, source)

    def assess(
        self,
        *,
        opportunity_score: float | None,
        component_scores: Mapping[str, float] | None = None,
        positions: Sequence[Mapping[str, Any]] | None = None,
        sector: str | None = None,
        ticker: str | None = None,
        market: str | None = None,
        volatility: Mapping[str, Any] | None = None,
        macro_event: Mapping[str, Any] | None = None,
        data_quality: Mapping[str, Any] | None = None,
        hard_block_reason: str | None = None,
        legacy_soft_reason: str | None = None,
    ) -> dict[str, Any]:
        """Return risk, recommendation tier, evidence, and a stable snapshot hash."""
        scores = _mapping(component_scores)
        vol = _mapping(volatility)
        macro = _mapping(macro_event)
        quality = _mapping(data_quality)
        opportunity = _score(opportunity_score)
        opportunity = 0.0 if opportunity is None else opportunity
        hard_block = self._hard_block(
            hard_block_reason, quality, ticker=ticker, market=market
        )
        weights = _mapping(self.config.get("weights"))
        breakdown = {
            "portfolio": self._portfolio(
                positions, sector, ticker, market,
                float(weights.get("portfolio", .30)), legacy_soft_reason,
            ),
            "timing": self._timing(scores, float(weights.get("timing", .20))),
            "volatility": self._volatility(vol, float(weights.get("volatility", .15))),
            "macro_event": self._macro(macro, scores, float(weights.get("macro_event", .15))),
            "disagreement": self._disagreement(scores, float(weights.get("disagreement", .10))),
            "data_quality": self._data_quality(quality, float(weights.get("data_quality", .10))),
        }
        risk = round(min(100.0, max(0.0, sum(item["weighted_score"] for item in breakdown.values()))), 2)
        portfolio_inputs = breakdown["portfolio"]["inputs"]
        material_soft_warning = bool(
            portfolio_inputs.get("duplicate_ticker")
            or portfolio_inputs.get("legacy_soft_reason")
            or (
                portfolio_inputs.get("expected_sector_weight") is not None
                and portfolio_inputs["expected_sector_weight"]
                > portfolio_inputs["max_sector_weight"]
            )
            or macro.get("event_imminent") is True
        )
        if material_soft_warning:
            # A material, named warning must be visible as conditional even
            # when otherwise strong axes dilute it below the aggregate cutoff.
            risk = max(risk, float(self.config.get("low_max", 35)) + 1)
        level = self._level(risk)
        tier = self._tier(opportunity, risk, hard_block)
        result = {
            "opportunity_score": opportunity,
            "opportunity_decision": self._opportunity_decision(opportunity),
            "risk_score": risk,
            "risk_level": level,
            "risk_breakdown": breakdown,
            "recommendation_tier": tier,
            "hard_block_reason": hard_block,
            "risk_model_version": str(
                self.config.get("model_version", "entry-risk-v1")
            ),
            "upgrade_conditions": self._upgrades(opportunity, risk, breakdown, hard_block),
        }
        snapshot = {key: value for key, value in result.items() if key != "upgrade_conditions"}
        result["risk_snapshot_hash"] = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return result

    def _portfolio(self, positions: Sequence[Mapping[str, Any]] | None, sector: str | None,
                   ticker: str | None, market: str | None, weight: float,
                   legacy_soft_reason: str | None) -> dict[str, Any]:
        rows = [row for row in (positions or []) if isinstance(row, Mapping)]
        clean_sector = (sector or "").strip().casefold()
        candidate = (ticker or "").strip().casefold()
        candidate_market = (market or "").strip().casefold()
        known_sector = bool(clean_sector and clean_sector not in {"unknown", "미상"})
        same_sector = 0
        duplicate = False
        for row in rows:
            row_market = str(row.get("market") or "").strip().casefold()
            if candidate and str(row.get("ticker") or "").strip().casefold() == candidate and (not candidate_market or not row_market or row_market == candidate_market):
                duplicate = True
            if known_sector and str(row.get("sector") or "").strip().casefold() == clean_sector:
                same_sector += 1
        assumed = max(0.0, min(1.0, float(self.config.get("assumed_position_weight", .20))))
        sector_weight = (same_sector + 1) * assumed if known_sector else None
        maximum = max(.01, min(1.0, float(self.config.get("max_sector_weight", .30))))
        score = 0.0 if sector_weight is not None else 20.0
        reasons: list[str] = []
        if sector_weight is None:
            reasons.append("섹터 정보가 없어 집중도를 완전히 계산할 수 없습니다")
        elif sector_weight > maximum:
            score = max(
                75.0,
                min(100.0, ((sector_weight - maximum) / maximum) * 100.0),
            )
            reasons.append(f"예상 섹터 비중 {sector_weight:.0%}가 기준 {maximum:.0%}를 넘습니다")
        else:
            reasons.append(f"예상 섹터 비중 {sector_weight:.0%} (기준 {maximum:.0%})")
        if duplicate:
            score = max(score, 85.0)
            reasons.append("동일 종목을 이미 보유 중입니다")
        if legacy_soft_reason:
            score = max(score, 75.0)
            reasons.append(str(legacy_soft_reason))
        return _component(score, weight, reasons, {
            "positions_count": len(rows),
            "same_sector_positions": same_sector,
            "assumed_position_weight": assumed,
            "expected_sector_weight": sector_weight,
            "max_sector_weight": maximum,
            "duplicate_ticker": duplicate,
            "sector": sector or "Unknown",
            "legacy_soft_reason": legacy_soft_reason,
        })

    def _timing(self, scores: Mapping[str, Any], weight: float) -> dict[str, Any]:
        technical = _score(scores.get("technical", scores.get("technical_score")))
        rsi = _number(scores.get("rsi", scores.get("rsi14")))
        values: list[float] = []
        reasons: list[str] = []
        if technical is not None:
            values.append(100.0 - technical)
            reasons.append(f"기술 점수 {technical:.0f}점")
        if rsi is not None:
            rsi_risk = min(100.0, max(0.0, (rsi - 50.0) * 2.0))
            values.append(rsi_risk)
            if rsi >= 70:
                reasons.append(f"RSI {rsi:.0f}: 과매수 진입 위험")
        if not values:
            values = [50.0]
            reasons.append("기술적 타이밍 데이터가 없어 중립 위험으로 처리했습니다")
        return _component(sum(values) / len(values), weight, reasons, {"technical_score": technical, "rsi": rsi})

    def _volatility(self, volatility: Mapping[str, Any], weight: float) -> dict[str, Any]:
        atr = _fraction(volatility.get("atr_pct"))
        percentile = _score(volatility.get("volatility_percentile"))
        gap = _fraction(volatility.get("gap_pct"))
        values: list[float] = []
        if atr is not None:
            values.append(min(100.0, max(0.0, (abs(atr) - .01) / .05 * 100.0)))
        if percentile is not None:
            values.append(percentile)
        if gap is not None:
            values.append(min(100.0, abs(gap) / .06 * 100.0))
        reasons = []
        if not values:
            values = [45.0]
            reasons.append("변동성 데이터가 없어 중립 위험으로 처리했습니다")
        else:
            reasons.append("ATR/변동성 백분위/갭을 반영했습니다")
        return _component(sum(values) / len(values), weight, reasons, {"atr_pct": atr, "volatility_percentile": percentile, "gap_pct": gap})

    def _macro(self, macro: Mapping[str, Any], scores: Mapping[str, Any], weight: float) -> dict[str, Any]:
        macro_score = _score(macro.get("macro_score", scores.get("macro")))
        confidence = _score(macro.get("intel_confidence", scores.get("intel_confidence")))
        imminent = bool(macro.get("event_imminent", False))
        values: list[float] = []
        reasons: list[str] = []
        if macro_score is not None:
            values.append(100.0 - macro_score)
            reasons.append(f"매크로 점수 {macro_score:.0f}점")
        if confidence is not None:
            values.append(100.0 - confidence)
        if imminent:
            values.append(80.0)
            reasons.append("중요 이벤트가 임박했습니다")
        if not values:
            values = [50.0]
            reasons.append("매크로·이벤트 데이터가 없어 중립 위험으로 처리했습니다")
        return _component(sum(values) / len(values), weight, reasons, {"macro_score": macro_score, "intel_confidence": confidence, "event_imminent": imminent})

    def _disagreement(self, scores: Mapping[str, Any], weight: float) -> dict[str, Any]:
        axes = ["technical", "fundamental", "flow", "intel", "macro"]
        values = [_score(scores.get(axis)) for axis in axes]
        values = [value for value in values if value is not None]
        agreement = _score(scores.get("strategy_agreement", scores.get("strategy_consensus")))
        reasons: list[str] = []
        risks: list[float] = []
        if len(values) >= 2:
            mean = sum(values) / len(values)
            spread = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
            risks.append(min(100.0, spread * 2.0))
            reasons.append(f"{len(values)}개 축 점수의 분산을 반영했습니다")
        if agreement is not None:
            risks.append(100.0 - agreement)
        if not risks:
            risks = [30.0]
            reasons.append("축별 합의 데이터가 부족해 보수적으로 처리했습니다")
        return _component(sum(risks) / len(risks), weight, reasons, {"axis_scores": values, "strategy_agreement": agreement})

    def _data_quality(self, quality: Mapping[str, Any], weight: float) -> dict[str, Any]:
        unavailable = [key for key in ("price_available", "fundamentals_available", "flow_available", "intel_available") if quality.get(key) is False]
        delayed = bool(quality.get("price_delayed", False) or quality.get("data_delayed", False))
        age = _number(quality.get("data_age_minutes", quality.get("price_age_minutes")))
        score = min(80.0, len(unavailable) * 25.0)
        reasons = [f"{key} 데이터가 없습니다" for key in unavailable]
        if delayed:
            score += 20.0
            reasons.append("데이터가 지연되었습니다")
        if age is not None and age > 1440:
            score += min(30.0, (age - 1440) / 1440 * 30.0)
            reasons.append(f"가격 데이터 경과 시간 {age:.0f}분")
        if not quality:
            score = max(score, 25.0)
            reasons.append("데이터 품질 메타데이터가 없습니다")
        return _component(score, weight, reasons or ["데이터 품질 이상 없음"], {"unavailable": unavailable, "delayed": delayed, "data_age_minutes": age})

    @staticmethod
    def _hard_block(
        explicit: str | None,
        quality: Mapping[str, Any],
        *,
        ticker: str | None,
        market: str | None,
    ) -> str | None:
        if explicit:
            return str(explicit)
        if str(market or "").strip().upper() not in {"KRX", "US"}:
            return "잘못된 시장 정보입니다"
        if not str(ticker or "").strip():
            return "잘못된 티커입니다"
        if quality.get("all_price_sources_failed") is True:
            return "모든 시세 소스 조회에 실패했습니다"
        if quality.get("price_available") is False:
            return "현재가를 확인할 수 없습니다"
        if quality.get("snapshot_valid") is False:
            return "점수 스냅샷 무결성 검증에 실패했습니다"
        if quality.get("market_valid") is False:
            return "잘못된 시장 정보입니다"
        if quality.get("ticker_valid") is False:
            return "잘못된 티커입니다"
        if quality.get("trading_halted") is True or quality.get("halted") is True:
            return "거래 정지 종목입니다"
        price = _number(quality.get("current_price"))
        return "현재가가 0 이하입니다" if price is not None and price <= 0 else None

    def _level(self, risk: float) -> str:
        if risk <= float(self.config["low_max"]): return "LOW"
        if risk <= float(self.config["medium_max"]): return "MEDIUM"
        if risk <= float(self.config["high_max"]): return "HIGH"
        return "VERY_HIGH"

    def _opportunity_decision(self, opportunity: float) -> str:
        if opportunity >= float(self.config["opportunity_execute"]): return "EXECUTE"
        if opportunity >= float(self.config["opportunity_watch"]): return "WATCH"
        return "SKIP"

    def _tier(self, opportunity: float, risk: float, hard_block: str | None) -> str:
        if hard_block: return "UNAVAILABLE"
        if opportunity < float(self.config["opportunity_watch"]): return "AVOID"
        if risk > float(self.config["high_max"]): return "WATCH"
        if opportunity >= float(self.config["opportunity_execute"]):
            return "BUY_READY" if risk <= float(self.config["low_max"]) else "BUY_CONDITIONAL"
        if opportunity >= float(self.config["opportunity_early_watch"]) and risk <= float(self.config["medium_max"]): return "EARLY_WATCH"
        return "WATCH"

    def _upgrades(self, opportunity: float, risk: float, breakdown: Mapping[str, Any], hard_block: str | None) -> list[dict[str, Any]]:
        if hard_block:
            return [{"component": "hard_block", "message": hard_block, "current_score": None, "target_score": None}]
        result: list[dict[str, Any]] = []
        if opportunity < float(self.config["opportunity_execute"]):
            result.append({"component": "opportunity", "message": "매수 매력도가 65점 이상이어야 합니다", "current_score": opportunity, "target_score": float(self.config["opportunity_execute"])})
        for name, item in sorted(breakdown.items(), key=lambda pair: pair[1]["weighted_score"], reverse=True):
            if item["score"] > 0 and len(result) < 3:
                result.append({"component": name, "message": item["reasons"][0], "current_score": item["score"], "target_score": 35.0})
        return result
