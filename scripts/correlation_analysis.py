"""스코어 vs 수익률 Spearman 순위상관 분석.

매주 또는 수동으로 실행하여 각 스코어링 축의 예측력을 검증.
상관계수 < 0.1이면 가중치 재조정 경고.
"""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가 (직접 실행 시)
sys.path.insert(0, str(Path(__file__).parent.parent))

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.scripts.correlation_analysis")

WARN_THRESHOLD = 0.10  # 상관계수 < 0.1 → 경고
AXES = ["tech_score", "fundamental_score", "flow_score", "intel_score", "macro_score", "composite_score"]


def spearman_r(x: list[float], y: list[float]) -> float:
    """Spearman 순위상관계수 계산 (scipy 없이 순수 Python)."""
    n = len(x)
    if n < 3:
        return float("nan")

    def rank(arr):
        sorted_arr = sorted(enumerate(arr), key=lambda t: t[1])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and sorted_arr[j + 1][1] == sorted_arr[j][1]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[sorted_arr[k][0]] = avg_rank
            i = j + 1
        return ranks

    rx = rank(x)
    ry = rank(y)

    d_sq_sum = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    r = 1 - (6 * d_sq_sum) / (n * (n * n - 1))
    return round(r, 4)


def run_analysis(days: int = 90) -> dict:
    """최근 N일 데이터로 상관분석 실행.

    Returns:
        {
            "date": "YYYY-MM-DD",
            "sample_count": int,
            "correlations": {"tech_score": 0.23, ...},
            "warnings": ["fundamental_score: r=0.08 < 0.10 (재조정 필요)"],
        }
    """
    from web.db.connection import get_db

    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    today = datetime.now(KST).strftime("%Y-%m-%d")

    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT
                    sr.tech_score,
                    sr.fundamental_score,
                    sr.flow_score,
                    sr.intel_score,
                    sr.macro_score,
                    sr.composite_score,
                    sp.return_pct
                FROM scoring_results sr
                JOIN signal_performance sp ON sr.ticker = sp.ticker
                    AND sr.signal_date = sp.signal_date
                WHERE sr.signal_date >= ?
                  AND sp.return_pct IS NOT NULL
                ORDER BY sr.signal_date
                """,
                (since,),
            ).fetchall()
    except Exception as e:
        logger.error(f"DB query failed: {e}")
        return {"error": str(e)}

    if len(rows) < 10:
        logger.warning(f"Insufficient data: {len(rows)} rows (need at least 10)")
        return {
            "date": today,
            "sample_count": len(rows),
            "correlations": {},
            "warnings": [f"데이터 부족: {len(rows)}건 (최소 10건 필요)"],
        }

    returns = [row["return_pct"] for row in rows]
    correlations = {}
    warnings = []

    axis_map = {
        "tech_score": "Technical",
        "fundamental_score": "Fundamental",
        "flow_score": "Flow",
        "intel_score": "Intel",
        "macro_score": "Macro",
        "composite_score": "Composite",
    }

    for col, label in axis_map.items():
        scores = [row[col] for row in rows if row[col] is not None]
        if len(scores) < 10:
            correlations[col] = None
            continue
        r = spearman_r(scores, returns[:len(scores)])
        correlations[col] = r
        if abs(r) < WARN_THRESHOLD:
            warnings.append(f"{label}: r={r:.3f} < {WARN_THRESHOLD} → 가중치 재조정 검토 필요")

    return {
        "date": today,
        "sample_count": len(rows),
        "days_analyzed": days,
        "correlations": correlations,
        "warnings": warnings,
    }


def save_results(result: dict, output_dir: Path = None):
    """결과를 JSON 파일로 저장."""
    if output_dir is None:
        output_dir = Path(__file__).parent.parent / "data" / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = output_dir / f"correlation_{result['date']}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"Results saved to {filename}")
    return filename


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="스코어-수익률 상관분석")
    parser.add_argument("--days", type=int, default=90, help="분석 기간 (일)")
    parser.add_argument("--save", action="store_true", help="JSON 파일 저장")
    args = parser.parse_args()

    result = run_analysis(days=args.days)

    print(f"\n=== 스코어 vs 수익률 상관분석 ({result.get('date', 'N/A')}) ===")
    print(f"샘플 수: {result.get('sample_count', 0)}건 ({result.get('days_analyzed', 0)}일)")
    print()

    for col, r in result.get("correlations", {}).items():
        if r is None:
            print(f"  {col:25s}: 데이터 없음")
        else:
            status = "OK" if abs(r) >= WARN_THRESHOLD else "WARN"
            print(f"  {col:25s}: r={r:+.3f} [{status}]")

    if result.get("warnings"):
        print("\n경고:")
        for w in result["warnings"]:
            print(f"  [!] {w}")

    if args.save:
        saved_to = save_results(result)
        print(f"\n결과 저장: {saved_to}")
