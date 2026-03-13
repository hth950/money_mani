"""가중치 자동 최적화 제안 스크립트.

correlation_analysis 결과를 기반으로 scoring.yaml의 가중치를 재배분하는
제안을 생성하여 Discord로 발송. 자동 적용하지 않음.

실행 조건:
- scoring_results 테이블에 최소 100건 이상
- 월 1회 스케줄 (매월 첫째 주 일요일 10:00 KST)

제약:
- 가중치 변경폭 ±0.05 이내 (과적합 방지)
- 전체 가중치 합 = 1.0 유지
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.scripts.weight_optimizer")

MIN_SAMPLES = 100          # 최소 샘플 수 (약 4-5개월 paper trading 필요)
MAX_CHANGE = 0.05          # 최대 변경폭 ±5%
MIN_WEIGHT = 0.05          # 최소 가중치 (0% 방지)


def load_current_weights() -> dict:
    """config/scoring.yaml에서 현재 가중치 로드."""
    try:
        import yaml
        config_path = Path(__file__).parent.parent / "config" / "scoring.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        # scoring.weights 섹션 확인
        weights = config.get("scoring", {}).get("weights", {})
        if not weights:
            # 기본값
            weights = {
                "technical": 0.30,
                "fundamental": 0.25,
                "flow": 0.20,
                "intel": 0.15,
                "macro": 0.10,
            }
        return weights
    except Exception as e:
        logger.error(f"Failed to load weights: {e}")
        return {}


def propose_weights(current: dict, correlations: dict) -> dict:
    """상관계수 기반 가중치 재배분 제안.

    알고리즘:
    1. |r| 기반 가중치 비례 배분
    2. 변경폭 ±MAX_CHANGE 클리핑
    3. 합산 1.0 정규화

    Returns:
        {"technical": 0.28, "fundamental": 0.27, ...}
    """
    col_to_key = {
        "tech_score": "technical",
        "fundamental_score": "fundamental",
        "flow_score": "flow",
        "intel_score": "intel",
        "macro_score": "macro",
    }

    # 유효한 상관계수만 추출
    valid = {}
    for col, key in col_to_key.items():
        if key not in current:
            continue
        r = correlations.get(col)
        if r is not None and not (r != r):  # NaN 제외
            valid[key] = abs(r)

    if not valid:
        return {}

    # 비례 배분 계산
    total_r = sum(valid.values())
    if total_r == 0:
        return {}

    proposed = {}
    for key, weight in current.items():
        if key in valid:
            target = valid[key] / total_r
        else:
            target = weight

        # ±MAX_CHANGE 클리핑
        delta = target - weight
        delta = max(-MAX_CHANGE, min(MAX_CHANGE, delta))
        proposed[key] = max(MIN_WEIGHT, weight + delta)

    # 합산 1.0 정규화
    total = sum(proposed.values())
    proposed = {k: round(v / total, 4) for k, v in proposed.items()}

    return proposed


def run_optimization(days: int = 90) -> dict:
    """가중치 최적화 제안 실행."""
    from scripts.correlation_analysis import run_analysis

    result = run_analysis(days=days)
    sample_count = result.get("sample_count", 0)

    if sample_count < MIN_SAMPLES:
        return {
            "skipped": True,
            "reason": f"데이터 부족: {sample_count}건 (최소 {MIN_SAMPLES}건 필요)",
            "sample_count": sample_count,
        }

    current_weights = load_current_weights()
    if not current_weights:
        return {"error": "current weights not found"}

    proposed_weights = propose_weights(current_weights, result.get("correlations", {}))

    return {
        "date": result.get("date"),
        "sample_count": sample_count,
        "current_weights": current_weights,
        "proposed_weights": proposed_weights,
        "correlations": result.get("correlations", {}),
        "warnings": result.get("warnings", []),
    }


def send_discord_proposal(result: dict):
    """Discord로 가중치 제안 발송."""
    if result.get("skipped") or result.get("error"):
        msg = result.get("reason") or result.get("error", "알 수 없는 오류")
        try:
            from alerts.discord_webhook import DiscordNotifier
            DiscordNotifier().send(content=f"⚙️ 월간 가중치 최적화: {msg}")
        except Exception:
            pass
        return

    current = result.get("current_weights", {})
    proposed = result.get("proposed_weights", {})

    lines = ["```"]
    lines.append(f"{'축':<15} {'현재':>6} → {'제안':>6}  {'변화':>7}")
    lines.append("-" * 42)

    key_labels = {
        "technical": "Technical",
        "fundamental": "Fundamental",
        "flow": "Flow",
        "intel": "Intel",
        "macro": "Macro",
    }

    for key, label in key_labels.items():
        cur = current.get(key, 0)
        prop = proposed.get(key, cur)
        delta = prop - cur
        arrow = "↑" if delta > 0.001 else ("↓" if delta < -0.001 else "=")
        lines.append(f"{label:<15} {cur:>5.1%} → {prop:>5.1%}  {delta:>+6.1%} {arrow}")

    lines.append("```")
    lines.append("*scoring.yaml 수동 수정이 필요합니다*")

    try:
        from alerts.discord_webhook import DiscordNotifier
        notifier = DiscordNotifier()
        embed = {
            "title": f"⚙️ 월간 가중치 최적화 제안 ({result.get('date', 'N/A')})",
            "description": "\n".join(lines),
            "color": 0x3498DB,  # 파랑
            "fields": [
                {"name": "샘플 수", "value": f"{result.get('sample_count', 0)}건", "inline": True},
                {"name": "⚠️ 주의", "value": "이 제안은 참고용입니다. 직접 적용하지 않습니다.", "inline": False},
            ],
            "timestamp": datetime.now(KST).isoformat(),
        }
        notifier.send(embed=embed)
        logger.info("Weight optimization proposal sent to Discord")
    except Exception as e:
        logger.error(f"Failed to send Discord proposal: {e}")


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="가중치 최적화 제안")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--send-discord", action="store_true")
    args = parser.parse_args()

    result = run_optimization(days=args.days)

    if result.get("skipped"):
        print(f"스킵: {result.get('reason')}")
    elif result.get("error"):
        print(f"오류: {result.get('error')}")
    else:
        print(f"\n=== 가중치 최적화 제안 ({result.get('date')}) ===")
        print(f"샘플: {result.get('sample_count')}건")
        current = result.get("current_weights", {})
        proposed = result.get("proposed_weights", {})
        for key in current:
            cur = current[key]
            prop = proposed.get(key, cur)
            delta = prop - cur
            print(f"  {key:<15}: {cur:.1%} → {prop:.1%}  ({delta:+.1%})")

    if args.send_discord:
        send_discord_proposal(result)
