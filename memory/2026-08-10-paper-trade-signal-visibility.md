# DEBUG REPORT — 모의매수 후 매수 추천 카드 이동

- **Symptom:** `/signals`에서 `BUY_READY`였던 Apple(AAPL)과 Chevron(CVX)을 모의매수한 직후 매수 추천 줄에서 카드가 사라졌다.
- **Root cause:** `SignalService.get_actions()`가 전략 포지션과 `paper_positions`를 하나의 포트폴리오 위험 문맥으로 합쳤다. 모의매수 직후 동일 종목 보유 위험이 추가되어 AAPL은 위험도 45.90, CVX는 44.59로 재평가됐고 두 종목 모두 `BUY_READY`에서 `BUY_CONDITIONAL`로 이동했다. 추천 표시와 추가 모의매수 주문 위험이 같은 스냅샷을 사용한 것이 원인이다.
- **Fix:** `/signals`의 기본 추천 계산은 전략 포지션만 사용하고 모의 포지션을 제외한다. 모의 주문 검증과 `/paper-trading` 주문 후보 계산만 `include_paper_risk=True`로 모의 포지션을 포함한다. 신호 화면은 표시용 추천과 주문용 위험 스냅샷을 별도 맵으로 유지해, 카드는 기존 줄과 점수를 유지하면서 추가매수 버튼만 조건부 위험을 적용한다.
- **Evidence:** 회귀 테스트에서 모의 포지션 삽입 전후 기본 액션의 등급·위험도·해시가 동일하고, 주문 위험 액션만 `BUY_CONDITIONAL`로 변경됨을 확인했다.
- **Regression test:** `tests/test_two_axis_signal_actions.py::test_paper_position_does_not_change_dashboard_recommendation`
- **Related:** 모의보유 종목이 최신 스캔에서 사라진 경우를 위한 `_paperOnly` 보유 추적 경로는 그대로 유지한다. 점수가 실제로 하락하거나 Exit 매도 신호가 생기면 정상적으로 관망·매도 줄로 이동한다.
- **Status:** DONE
