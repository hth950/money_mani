# money_mani — AI 주식 스코어링 시스템 (Agent 가이드)

> **컨텍스트 압축 후에도 이 파일을 읽으면 프로젝트 전체 상태를 파악할 수 있습니다.**
> 세션 시작 시 항상 이 파일을 먼저 읽으세요.

---

## 프로젝트 개요

- **목적**: KRX(국내) + US(미국) 주식 대상 5축 스코어링 → 매수/관망/스킵 결정 → Discord 알림
- **서버**: `168.107.42.41:8000` (SSH alias: `money-mani`, key: `~/.ssh/ssh-key-2026-03-08.key`)
- **배포**: git 미사용. `rsync -avz ... money-mani:~/money_mani/` 로 동기화
- **Python**: `.venv/bin/python` (서버에서 반드시 이 venv 사용)

---

## 아키텍처 개요

```
YouTube / Web 검색
       ↓
[pipeline/runner.py] PipelineRunner — 전략 발굴 파이프라인
[pipeline/discovery.py] StrategyDiscovery
       ↓
[strategy/] Strategy YAML (config/strategies/*.yaml, 147개)
       ↓
[backtester/] BacktestEngine → BacktestResult
       ↓
[pipeline/daily_scan.py] DailyScan (매일 8:00 KST)
 ↳ validated_v2 전략 × KRX/US 종목 → 신호 생성
       ↓
[scoring/multi_layer_scorer.py] MultiLayerScorer
 ├── TechnicalScorer (50%)
 ├── FundamentalCollector (10%)
 ├── FlowCollector (20% KRX / 0% US)
 ├── IntelScorer (10% KRX / 25% US)
 └── MacroCollector (10% KRX / 15% US)
       ↓
[web/] FastAPI + HTMX UI (168.107.42.41:8000)
[alerts/discord_webhook.py] Discord 알림
```

---

## 5축 스코어링 (config/scoring.yaml)

| 축 | KRX 비중 | US 비중 | 데이터 소스 |
|---|---|---|---|
| **technical** | 50% | 50% | OHLCV 지표 (RSI/MACD/BB/ADX 등) |
| **flow** | 20% | 0% | 외국인/기관 순매수 (Naver 스크래퍼 → KIS REST 전환 예정) |
| **fundamental** | 10% | 10% | yfinance PER/PBR/ROE vs 섹터 벤치마크 |
| **intel** | 10% | 25% | market_intel_issues DB (LLM 웹검색 결과) |
| **macro** | 10% | 15% | VIX 기반 (VIX≤15 → 0.80, VIX≥35 → 0.15) |

**결정 임계값**:
- EXECUTE: composite ≥ 0.60
- WATCH: composite ≥ 0.40
- SKIP: composite < 0.40

---

## 핵심 파일 맵

### 데이터 수집 (market_data/)
| 파일 | 역할 |
|---|---|
| `krx_fetcher.py` | KRX OHLCV, 수급, 펀더멘털 (yfinance primary, pykrx fallback) |
| `us_fetcher.py` | US OHLCV + 펀더멘털 (yfinance) |
| `fdr_fetcher.py` | KRX 종목 목록 + 섹터 (FinanceDataReader) |
| `naver_flow_fetcher.py` | 외국인/기관 수급 스크래퍼 (pykrx 차단 시 fallback) |

### 스코어링 (scoring/)
| 파일 | 역할 |
|---|---|
| `multi_layer_scorer.py` | 5축 → composite 점수 + 결정 |
| `data_collectors.py` | FundamentalCollector / FlowCollector / MacroCollector |
| `technical_scorer.py` | 지표 기반 기술적 점수 |
| `intel_scorer.py` | DB에서 최근 인텔 이슈 → 감성 점수 |
| `dart_fundamental.py` | DART API 재무데이터 (한국 주식) |
| `risk_manager.py` | 포트폴리오 리스크 체크 (config/risk.yaml) |

### 파이프라인 (pipeline/)
| 파일 | 역할 |
|---|---|
| `scheduler.py` | APScheduler — 모든 스케줄 정의 |
| `daily_scan.py` | 매일 8:00 KST 전략×종목 스캔 |
| `rescore.py` | 최신 캐시로 전 종목 재스코어링 (수급/인텔 업데이트 반영) |
| `market_intel.py` | LLM 웹검색 → 인텔 이슈 DB 저장 |
| `nightly.py` | 19:00 야간 리포트 (P&L, 지식 업데이트) |

### 브로커 (broker/)
| 파일 | 역할 |
|---|---|
| `kis_client.py` | KIS pykis 래퍼 (현재가 + 잔고 조회만, 매매 없음) |
| `portfolio.py` | 실시간 포트폴리오 상태 |

### 백테스터 (backtester/)
| 파일 | 역할 |
|---|---|
| `engine.py` | backtesting.py 래퍼. **주의: size≥1.0은 주 수로 처리됨 → 0.9999 사용** |
| `signals.py` | YAML 전략 규칙 → 매수/매도 신호 생성 |
| `metrics.py` | BacktestResult (Sharpe, MDD, win_rate) |

### 전략 (strategy/)
| 파일 | 역할 |
|---|---|
| `registry.py` | YAML 파일 로드/저장/목록. **버그**: get_validated()가 "validated"만 인식 (validated_v2 미포함) → Phase 1에서 수정 예정 |
| `models.py` | Strategy 데이터클래스 |

### 웹 (web/)
| 파일 | 역할 |
|---|---|
| `app.py` | FastAPI 앱 (시작 시 DB 초기화 + YAML 마이그레이션) |
| `db/schema.sql` | SQLite 스키마 (14개 테이블) |
| `db/migrate.py` | 스키마 마이그레이션 |
| `routers/` | 14개 라우터 (pages, strategies, backtest, signals, scoring 등) |
| `services/` | 비즈니스 로직 레이어 |

---

## 데이터베이스 주요 테이블

| 테이블 | 역할 |
|---|---|
| `strategies` | 전략 정의 (status CHECK 제약: draft/testing/validated/retired) |
| `backtest_results` | 종목별 백테스트 결과 |
| `signals` | 감지된 매수/매도 신호 |
| `scoring_results` | 5축 점수 + composite + decision |
| `positions` | 오픈/클로즈 포지션 |
| `market_intel_issues` | LLM 감지 시장 이슈 (direction/confidence/accuracy_score) |
| `knowledge_entries` | 지속적 지식 베이스 |

**현재 DB 버그**: `strategies.status` CHECK 제약에 `validated_v2`, `rejected_v2` 미포함 → Phase 1에서 수정

---

## 스케줄 요약 (KST 기준, 평일)

| 시간 | 작업 |
|---|---|
| 08:00 | daily_scan (전략×종목 스캔) |
| 08:50 | 실시간 모니터 시작 (KRX) |
| 09:00–15:00 | 매시 인텔 스캔 + 즉시 재스코어링 |
| 09:30/11:30/13:30/15:30 | 전체 재스코어링 |
| 15:35 | 실시간 모니터 종료 (KRX) |
| 16:00 | 인텔 가격 추적 |
| 16:10 | 수급 캐시 초기화 + 재스코어링 |
| 18:00 | 인텔-신호 상관관계 로깅 |
| 19:00 | 야간 리포트 |
| 22:50 | 실시간 모니터 시작 (US) |

---

## 전략 현황 (config/strategies/, 147개)

| 상태 | 수량 | 설명 |
|---|---|---|
| `validated_v2` | **21** | 10년 백테스트 통과 (Sharpe≥0.1, MDD≤55%, WR≥30%, PassRate≥20%) |
| `rejected_v2` | 80 | 10년 재검증 실패 |
| `rejected` | 46 | 초기 검증 실패 |
| `draft` / `archived` | 0 | (아직 없음) |

**validated_v2 전략 목록** (21개):
ADX+MACD 추세, ADX+MACD+EMA(15) 트리플, ADX+MACD+EMA(35) 트리플,
BB 수축 돌파 (15), BB 수축 돌파 (25), BB+MACD+ADX 트리플,
Keltner Channel 돌파, MA 수렴 후 확산, MACD + RSI 복합, MACD 크로스오버,
PSAR 추세 (af=0.01), PSAR+ADX 강추세, ROC 모멘텀 (20-3%), ROC(20)+SMA 추세,
SMA 삼중 정렬 (5-15-45), SMA 크로스 (10-30), SMA 크로스 (5-20),
Stochastic RSI 반전, Williams %R 반전, 볼린저 밴드 수축 돌파, 볼린저 스퀴즈 (KC 안쪽)

---

## 진행 중인 작업 계획 (Phase 0~6)

계획 파일: `~/.claude/plans/bubbly-brewing-zebra.md`

| Phase | 내용 | 상태 |
|---|---|---|
| **Phase 0** | AGENTS.md 작성 (이 파일) | ✅ 완료 |
| **Phase 1** | DB status 제약 확장 + get_validated() 수정 + UI 배지 | ✅ 완료 |
| **Phase 2** | rejected_v2 → archived 소프트 딜리트 (80개, 파일 유지) | ✅ 완료 |
| **Phase 3A** | ATR 추세추종 신고가 전략 + atr_stop 인디케이터 (KR+US) | ✅ 완료 |
| **Phase 3B** | 듀얼 모멘텀 KR/US 전략 (ROC_252 > 0) | ✅ 완료 |
| **Phase 3C** | 섹터 로테이션 KR/US 전략 (ROC_252 > 5% + SMA_20) | ✅ 완료 |
| **Phase 3D** | 외국인/기관 수급 팔로잉 (KR only, KIS flow 데이터 필요) | ⬜ 대기 |
| **Phase 3E~F** | 팩터 전략 (저변동성, F-Score) — PortfolioRanker 신규 구현 | ⬜ 대기 |
| **Phase 4** | KIS REST API 데이터 클라이언트 (15 req/sec, OHLCV+수급) | ✅ 완료 |
| **Phase 5** | 신규 전략 백테스트 + 서버 검증 | ⬜ 대기 |
| **Phase 6** | 서버 배포 (Phase마다 즉시 rsync) | ✅ 진행 중 |

### 신규 추가 예정 전략 (웹 검색 검증, 10년+ 성과)
| 전략 | Sharpe | 적용 | Phase |
|---|---|---|---|
| ATR 추세추종 신고가+ATR청산 | 1.24 | KR+US | 3A |
| 듀얼 모멘텀 (Gary Antonacci) | 0.8~1.0 | KR+US | 3B |
| 섹터 로테이션 모멘텀 | 0.54~1.16 | KR+US | 3C |
| 외국인/기관 수급 팔로잉 | - | KR only | 3D |
| 저변동성 팩터 | 0.72 | KR+US | 3E |
| 피오트로스키 F-Score | 1.38 | KR+US | 3F |

---

## KIS API 정보

- **라이브러리**: `pykis` (python-kis) — 현재가 + 잔고만 지원
- **인증**: `.env` → KIS_API_KEY, KIS_API_SECRET, KIS_HTS_ID, KIS_ACCOUNT_NUMBER
- **REST API**: `https://openapi.koreainvestment.com:9443`
- **속도 제한**: 20 req/sec → **안전 마진 15 req/sec** 로 구현
- **신규 클라이언트 예정**: `market_data/kis_data_client.py` (pykis와 별도)
  - 일봉 OHLCV: `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice` (FHKST03010100, 100건/회)
  - 수급: `/uapi/domestic-stock/v1/quotations/inquire-investor` (FHKST01010900)

---

## 알려진 버그 및 수정 내역

| 버그 | 수정 상태 | 파일 |
|---|---|---|
| engine.py: `buy(size=1.0)` = 1주 (백테스트 수익률 0.8% → 실제 44.9%) | ✅ 수정 (0.9999 사용) | `backtester/engine.py` |
| pykrx OCI 서버 IP 차단 → OHLCV 빈 DataFrame | ✅ yfinance .KS fallback 추가 | `market_data/krx_fetcher.py` |
| KRX 펀더멘털 점수 50 고정 (pykrx 차단) | ✅ DART fallback + yfinance 섹터 추가 | `scoring/data_collectors.py` |
| rescore 오늘 데이터 없으면 0 업데이트 | ✅ MAX(scan_date) fallback 추가 | `pipeline/rescore.py` |
| rescore decision/block_reason 재평가 안됨 | ✅ PortfolioRiskManager 재평가 추가 | `pipeline/rescore.py` |
| CVX BLOCKED (max_positions 초과) | ✅ risk.yaml → 9999, rescore로 해결 | `config/risk.yaml` |
| strategies.status CHECK에 validated_v2 없음 | ✅ 수정 (자동 마이그레이션) | `web/db/schema.sql`, `web/db/migrate.py` |
| get_validated()이 validated_v2 미인식 | ✅ 수정 (validated_v2 포함) | `strategy/registry.py` |

---

## 서버 배포 방법

```bash
# 로컬 → 서버 파일 동기화
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='output/' --exclude='.venv/' \
  /Users/hwangtaehwan/Desktop/project/money_mani/ \
  money-mani:~/money_mani/

# 단일 파일 업로드
scp 파일경로 money-mani:~/money_mani/같은경로

# 서버에서 Python 실행
ssh money-mani "cd ~/money_mani && .venv/bin/python -c '...'"

# 서비스 재시작 (필요 시)
ssh money-mani "pkill -f uvicorn; cd ~/money_mani && nohup .venv/bin/python run_web.py &"
```

---

## 환경 변수 (.env 필요 키)

```
KIS_API_KEY=...
KIS_API_SECRET=...
KIS_HTS_ID=...
KIS_ACCOUNT_NUMBER=...
DART_API_KEY=...
OPENROUTER_API_KEY=...
DISCORD_WEBHOOK_URL=...
```
