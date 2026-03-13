"""주간 스코어-수익률 상관분석 리포트.

매주 일요일 09:00 KST에 스케줄러가 실행.
scripts/correlation_analysis.py의 로직을 활용하여 Discord에 임베드 전송.
"""

import logging
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.pipeline.correlation_report")

WARN_THRESHOLD = 0.10
MIN_SAMPLES = 30  # 최소 30건 미만 시 skip


class CorrelationReport:
    """주간 상관분석 리포트 생성 및 Discord 전송."""

    def run(self) -> dict:
        """분석 실행 및 Discord 전송."""
        today = datetime.now(KST).strftime("%Y-%m-%d")
        logger.info(f"=== Weekly Correlation Report ({today}) ===")

        try:
            from scripts.correlation_analysis import run_analysis
            result = run_analysis(days=90)
        except Exception as e:
            logger.error(f"Correlation analysis failed: {e}", exc_info=True)
            return {"error": str(e)}

        sample_count = result.get("sample_count", 0)

        if sample_count < MIN_SAMPLES:
            logger.info(f"Skipping report: only {sample_count} samples (need {MIN_SAMPLES})")
            self._send_insufficient_data_notice(sample_count)
            return {"skipped": True, "sample_count": sample_count}

        self._send_discord_report(result)
        return result

    def _send_discord_report(self, result: dict):
        """Discord에 상관분석 임베드 전송."""
        try:
            from alerts.discord_webhook import DiscordNotifier
            notifier = DiscordNotifier()

            correlations = result.get("correlations", {})
            warnings = result.get("warnings", [])

            axis_labels = {
                "tech_score": "Technical",
                "fundamental_score": "Fundamental",
                "flow_score": "Flow",
                "intel_score": "Intel",
                "macro_score": "Macro",
                "composite_score": "Composite",
            }

            lines = []
            for col, label in axis_labels.items():
                r = correlations.get(col)
                if r is None:
                    lines.append(f"- {label:12s}: 데이터 없음")
                else:
                    icon = "OK" if abs(r) >= WARN_THRESHOLD else "WARN"
                    lines.append(f"- {label:12s}: r={r:+.3f} [{icon}]")

            color = 0xFF0000 if warnings else 0x00B050  # 경고 빨강, 정상 초록

            embed = {
                "title": f"[주간 상관분석 리포트] ({result.get('date', 'N/A')})",
                "description": "\n".join(lines),
                "color": color,
                "fields": [
                    {
                        "name": "샘플 수",
                        "value": f"{result.get('sample_count', 0)}건 ({result.get('days_analyzed', 90)}일)",
                        "inline": True,
                    },
                ],
                "timestamp": datetime.now(KST).isoformat(),
            }

            if warnings:
                embed["fields"].append({
                    "name": "[경고]",
                    "value": "\n".join(warnings[:5]),  # 최대 5개
                    "inline": False,
                })

            notifier.send(embed=embed)
            logger.info("Weekly correlation report sent to Discord")
        except Exception as e:
            logger.error(f"Failed to send correlation report: {e}")

    def _send_insufficient_data_notice(self, count: int):
        """데이터 부족 시 간단한 알림."""
        try:
            from alerts.discord_webhook import DiscordNotifier
            notifier = DiscordNotifier()
            notifier.send(
                content=f"[주간 상관분석] 데이터 부족 ({count}건, 최소 {MIN_SAMPLES}건 필요) - 스킵"
            )
        except Exception:
            pass
