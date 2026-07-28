# DEBUG REPORT — 최근 매수 추천이 없는 이유

- **Symptom:** 2026-07-28 `/signals`에 매수 추천이 하나도 표시되지 않는다.
- **Root cause:** 당일 스캔과 스코어링은 정상 실행됐고 14개 후보 중 SAP(0.6656), BAC(0.6529), AAPL(0.6522) 3개가 `EXECUTE` 점수 기준을 통과했다. 그러나 `PortfolioRiskManager`가 세 종목을 모두 섹터 집중도 초과로 `BLOCKED` 처리했다. 현재 자동 전략 포지션은 섹터가 `Unknown`인 UUP 1개뿐이며, 신규 알려진 섹터 종목 하나를 더하면 `(0+1)/(1+1)=50%`로 계산되어 설정 한도 30%를 넘는다. 작은 포트폴리오에서 첫 알려진 섹터 진입조차 막는 구조다.
- **Evidence:** 운영 로그는 `Multi-layer scoring: 14 candidates scored (EXECUTE=3, WATCH=10, SKIP=1)` 뒤 AAPL/BAC/SAP를 각각 섹터 집중도로 차단했다. 실제 액션 결과는 BUY 0개, WATCH 13개, NONE 1개였다. 웹·스케줄러는 정상이며 최신 스캔은 2026-07-28 08:00 KST에 완료됐다.
- **Market context:** US macro score는 0.7266이고 VIX는 2026-07-27 기준 18.67이므로 극단적 위험 회피 환경이 무추천의 직접 원인은 아니다. 세 후보의 기술 점수는 0.44~0.47로 약하고 종합점수는 주로 높은 intel·macro 점수에 의해 기준을 소폭 넘겼다.
- **Fix:** 사용자는 원인과 현재 매수 가능성 분석을 요청했으므로 코드나 설정은 변경하지 않았다. 후속 개선 시 소규모 포트폴리오용 최소 포지션 수/슬롯 기반 섹터 한도 또는 증분 섹터 제한을 설계하고 회귀 테스트해야 한다.
- **Regression test:** 해당 없음(진단 전용).
- **Status:** DONE_WITH_CONCERNS
