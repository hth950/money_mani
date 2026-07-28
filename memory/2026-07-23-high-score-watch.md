# DEBUG REPORT — 높은 점수 종목이 관망으로 표시되는 이유

- **Symptom:** `/signals`에서 종합점수가 65% 이상, 특히 UUP 67%인 종목이 매수 추천이 아니라 관망에 표시된다.
- **Root cause:** 점수 임계값 통과 후 `PortfolioRiskManager`가 신규 진입 위험을 별도로 검사한다. 차단된 `BLOCKED` 결정은 `SignalService.get_actions()`에서 화면 액션 `WATCH`로 변환된다. UUP은 이미 열린 전략 포지션이 있어 중복 진입이 차단됐다. MMM과 AMZN은 섹터 집중도 한도 때문에 차단됐다.
- **Evidence:** 2026-07-23 운영 DB에서 UUP은 `composite_score=0.6676`, `decision=BLOCKED`, `block_reason=이미 포지션 보유 중: UUP`이었다. 실제 액션 API 변환 결과도 `recommendation=BLOCKED`, `action=WATCH`, `is_holding=true`였다.
- **UI concern:** `/signals` 카드는 `block_reason`을 반환하거나 표시하지 않아, 점수가 임계값보다 높은데도 단순 관망처럼 보인다. 로직은 의도대로지만 설명이 부족하다.
- **Fix:** 사용자 요청이 원인 설명이므로 코드 변경은 하지 않았다. 후속 개선 시 `BLOCKED`를 `진입 차단`으로 표시하고 사유 배지를 노출하는 것이 적절하다.
- **Regression test:** 해당 없음(진단 전용).
- **Status:** DONE_WITH_CONCERNS
