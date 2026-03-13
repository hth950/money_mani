# Money Mani — 주식 자동 스코어링 & 매매 신호 시스템

OCI 클라우드에 상시 운영 중인 한국/미국 주식 자동화 파이프라인입니다.
5축 복합 스코어로 종목을 평가하고, 실시간 모니터가 매매 신호를 Discord로 알림합니다.

---

## 목차

1. [시스템 개요](#1-시스템-개요)
2. [5축 복합 스코어링](#2-5축-복합-스코어링)
3. [매수 · 매도 판단 기준](#3-매수--매도-판단-기준)
4. [실시간 모니터 (Realtime Monitor)](#4-실시간-모니터-realtime-monitor)
5. [인텔리전스 스코어 (Intel Score)](#5-인텔리전스-스코어-intel-score)
6. [자동 스케줄 (전체 타임라인)](#6-자동-스케줄-전체-타임라인)
7. [재스코어링 (Rescore) 시스템](#7-재스코어링-rescore-시스템)
8. [웹 대시보드 페이지 안내](#8-웹-대시보드-페이지-안내)
9. [Paper Trading & 성과 검증](#9-paper-trading--성과-검증)
10. [데이터 소스 & 캐시 전략](#10-데이터-소스--캐시-전략)
11. [프로젝트 구조](#11-프로젝트-구조)
12. [서버 배포 & 운영](#12-서버-배포--운영)
13. [설정 커스터마이징](#13-설정-커스터마이징)
14. [주의사항](#14-주의사항)

---

## 1. 시스템 개요

```
[일일 스캔 08:00]          [실시간 모니터 08:50~15:35]
    ↓                              ↓
기술적 전략 합의             60초마다 가격 폴링
(Multi-Strategy Consensus)   → 기술적 시그널 감지
    ↓                              ↓
[5축 복합 스코어 계산]       [합의 전환 시 즉시 재스코어링]
 Technical  30%                    ↓
 Fundamental 25%          [Discord 매수/매도 알림]
 Flow        20%
 Intel       15%          [인텔 스캔 09:00~15:00, 매시]
 Macro       10%           AI가 뉴스/공시 분석 → Intel Score 갱신
    ↓                              ↓
[scoring_results DB 저장]   [재스코어링 09:30/11:30/13:30/15:30]
    ↓
[웹 대시보드 실시간 표시]
http://168.107.42.41:8000
```

**핵심 원칙**
- 단일 전략이 아닌 **여러 전략의 합의(Consensus)** 를 기술적 점수로 환산
- 펀더멘탈·수급·뉴스·매크로를 더해 **5개 축 가중 합산** → 복합 점수 0~1
- 복합 점수가 **0.65 이상이면 매수 추천**, 0.40 이하면 매도 추천
- 시장이 열려있는 동안 실시간 모니터가 **60초 주기**로 조건 체크

---

## 2. 5축 복합 스코어링

### 가중치 (config/scoring.yaml)

| 축 | KRX (한국) | US (미국) | 의미 |
|----|-----------|----------|------|
| **Technical** | 30% | 45% | 기술적 전략 합의 비율 |
| **Fundamental** | 25% | 20% | 재무 건전성 (PER/PBR/ROE/배당) |
| **Flow** | 20% | 0% | 외국인·기관 수급 |
| **Intel** | 15% | 25% | AI 뉴스·공시 감성 분석 |
| **Macro** | 10% | 10% | VIX 기반 시장 공포지수 |

> US 주식은 수급(Flow) 데이터가 없어 Technical·Intel 비중이 높습니다.

### 각 축 상세

#### Technical Score (기술적 점수)
- **계산**: 매수 신호를 낸 전략 수 ÷ 전체 전략 수
- **예시**: 검증된 전략 8개 중 6개가 BUY 신호 → Technical Score = 0.75
- **전략 종류**: 골든크로스, RSI 반등, MACD 상향돌파, 볼린저밴드 등 `config/strategies/` 하위 YAML 정의
- **특징**: 일일 스캔 결과이므로 장중에는 실시간 모니터가 합의 전환 시 즉시 갱신

#### Fundamental Score (펀더멘탈 점수)
- **데이터 소스**:
  - 한국: **DART 전자공시 API** (연결재무제표 우선, 별도재무제표 폴백)
  - 미국: **yfinance** (P/E, P/B, ROE, 배당수익률)
- **평가 항목**: PER, PBR, ROE, 배당수익률
- **섹터 상대 평가**: 업종 평균 대비 우열 판단 (Technology PER 기준 35배, 금융 12배 등 섹터별 상이)
- **캐시**: 4시간 TTL (API 호출 최소화)

#### Flow Score (수급 점수, KRX 전용)
- **데이터 소스**: 네이버 금융 스크래퍼 (pykrx가 OCI에서 차단될 경우 자동 fallback)
- **평가 항목**:
  - 연속 순매수 일수 (Streak): 20%
  - 순매수 금액 절대값 (Amount): 35%
  - 외국인/기관 합산 비율 (Ratio): 25%
  - 외국인-기관 동반 매수 시너지 (Synergy): 20%
- **조회 기간**: 14일
- **캐시**: 4시간 TTL (장 마감 후 16:10에 강제 무효화)

#### Intel Score (인텔리전스 점수)
- **역할**: AI가 뉴스·공시를 읽고 감성 점수 산출 → 스코어링에 반영
- **자세한 내용**: [5장 인텔리전스 스코어](#5-인텔리전스-스코어-intel-score) 참고
- **캐시**: 1시간 TTL

#### Macro Score (매크로 점수)
- **데이터**: **VIX** (미국 변동성 지수, CBOE Volatility Index)
- **계산 방식**: Piecewise-linear 보간 (급격한 점프 없이 연속적으로 변화)

| VIX 수준 | 점수 | 상태 |
|---------|------|------|
| ≤ 15 | 0.80 | Calm (안정) |
| 20 | 0.70 | Caution (주의) |
| 25 | 0.50 | Elevated (경계) |
| ≥ 35 | 0.15 | Fear (공포) |

- **캐시**: 2시간 TTL (전 종목 공통값)

### 복합 점수 계산

```
composite_score = Technical × 0.30
               + Fundamental × 0.25
               + Flow × 0.20
               + Intel × 0.15
               + Macro × 0.10
```

결과는 0.0 ~ 1.0 사이 값으로 DB(`scoring_results`)에 저장됩니다.

---

## 3. 매수 · 매도 판단 기준

### 매수 추천 (BUY)
- 복합 점수 **≥ 0.65** (65% 이상)
- 포트폴리오 리스크 한도 내 (최대 20 종목, 단일 종목 20%, 섹터 30%)

### 관망 (WATCH)
- 복합 점수 0.40 ~ 0.65 사이
- 조건을 기다리는 후보군

### 매도 추천 (SELL)
- 복합 점수 **≤ 0.40** (40% 이하)
- 또는 매도 타이밍 스코어가 기준 이하일 때

### 매도 타이밍 스코어 (Exit Scorer)
보유 종목에 대해 별도로 매도 적정성을 평가합니다.

| 구성 요소 | 가중치 |
|----------|--------|
| 추세 (Trend) | 35% |
| 모멘텀 (Momentum) | 30% |
| 트레일링 스탑 | 35% |

- 매도 신호: Exit Score ≤ 0.25
- 매도 경고: Exit Score 0.25 ~ 0.40
- 손절 기준: -5% (stop_loss_pct)
- 목표 수익: +15% (take_profit_pct)
- 최소 보유일: 2일

---

## 4. 실시간 모니터 (Realtime Monitor)

### 역할
일일 스캔이 "아침에 찍어둔 스냅샷"이라면, 실시간 모니터는 **장중 연속 감시자**입니다.
여러 기술적 전략을 60초마다 평가해 합의(Consensus)가 전환되면 즉시 Discord 알림을 보냅니다.

### 운영 시간 (자동 시작/종료)

| 시장 | 시작 | 종료 |
|------|------|------|
| KRX (한국) | 평일 **08:50 KST** | 평일 **15:35 KST** |
| US (미국) | 평일 **22:50 KST** | 익일 **06:05 KST** |

스케줄러가 자동으로 시작/종료합니다. 수동으로 조작하려면:

```
POST http://168.107.42.41:8000/api/monitor/start
POST http://168.107.42.41:8000/api/monitor/stop
```

### 동작 흐름

```
[60초마다]
  현재가 수집 (KIS API → yfinance fallback)
      ↓
  RollingBuffer에 OHLCV 추가 (최대 200봉)
      ↓
  검증된 전략 N개 각각 시그널 계산
      ↓
  합의 비율 변화 감지
      ↓ (합의 전환: SELL→BUY, BUY→SELL, 임계값 돌파)
  Discord 알림 전송
  + 즉시 재스코어링 (composite_score 갱신)
```

### 합의(Consensus)란?
- 8개 전략 중 6개 이상 BUY 신호 → **BUY 합의**
- BUY 합의에서 5개 이하로 떨어지면 → **합의 전환(Flip)** 발생, 알림 발송
- 합의 전환 시 `technical_score`를 BUY=0.75, SELL=0.25로 근사하여 즉시 재스코어링

### 쿨다운
동일 종목에 대해 **30분** 내 중복 알림 억제 (SignalTracker)

---

## 5. 인텔리전스 스코어 (Intel Score)

### 인텔리전스란?
AI(LLM)가 뉴스·공시·시황 텍스트를 분석하여 **종목별 감성 점수**를 자동 산출하는 기능입니다.
인간이 뉴스를 읽고 "이 재료가 호재냐 악재냐"를 판단하는 과정을 자동화합니다.

### 인텔 스캔 주기

| 시장 | 스캔 시간 | 횟수 |
|------|----------|------|
| KRX | 평일 09:00~15:00 매시 정각 | 하루 7회 |
| US | 평일 23:00~익일 06:00 매시 정각 | 하루 8회 |

매 스캔 후 **전 종목 재스코어링**이 자동 실행됩니다.

### 스캔 → 스코어 흐름

```
[MarketIntelScanner 실행]
    ↓
Naver 검색 API로 최신 뉴스 수집
    ↓
LLM(Gemini)이 각 뉴스를 읽고:
  - 관련 종목 추출
  - 감성 판단 (POSITIVE/NEGATIVE/NEUTRAL)
  - impact_score (0~1), confidence (0~1)
  - category (earnings / regulation / macro / technical ...)
    ↓
DB(market_intel_issues) 저장
    ↓
[IntelScorer.score(ticker)]
  최근 7일 해당 종목 이슈 조회
  시간 감쇠 적용: 0.85^(경과일수)  ← 오래된 뉴스는 가중치 감소
  카테고리별 과거 정확도 보정
    ↓
  Intel Score 0.0~1.0 반환
```

### 정확도 피드백 루프
- 인텔 시그널 후 실제 주가 변동을 `IntelPriceTracker`가 추적 (매일 16:00)
- 예측 방향 일치 여부를 `accuracy_score`로 DB에 기록
- IntelScorer가 카테고리별 과거 정확도를 조회해 신뢰도 낮은 카테고리 가중치 감소
- 매주 일요일 09:00 `CorrelationReport`가 스코어-수익률 Spearman 상관분석 결과를 Discord로 전송

---

## 6. 자동 스케줄 (전체 타임라인)

### 평일 (한국 장 기준)

| 시간 (KST) | 작업 | 설명 |
|-----------|------|------|
| 00:05 | DART 카운터 초기화 | DART 일일 API 한도(10,000건) 카운터 리셋 |
| 06:00 | DART 이벤트 캐시 갱신 | 실적 발표 일정 등 사전 캐시 |
| 08:00 | **일일 스캔** | 전 감시 종목 기술적 전략 합의 평가 + 5축 스코어 계산 |
| 08:50 | 실시간 모니터 시작 | KRX 장 시작 10분 전 자동 시작 |
| 09:00~15:00 | **인텔 스캔** (매시) | KRX 뉴스·공시 AI 분석 → Intel Score 갱신 |
| 09:30 | 재스코어링 | 최신 캐시로 전 종목 복합 점수 재계산 |
| 11:30 | 재스코어링 | |
| 13:30 | 재스코어링 | |
| 15:30 | 재스코어링 | |
| 15:35 | 실시간 모니터 종료 | KRX 장 마감 후 자동 종료 |
| 16:00 | 인텔 가격 추적 | 시그널 발생 종목의 당일 종가 기록 |
| 16:10 | **수급 재스코어링** | Flow 캐시 강제 만료 → 당일 수급 데이터로 재계산 |
| 18:00 | 상관관계 로깅 | 인텔 시그널 vs 수익률 상관계수 기록 |
| 19:00 | **저녁 성과 리포트** | P&L, 포지션 현황, 분석 결과 Discord 전송 |
| 22:50 | US 실시간 모니터 시작 | 미국 장 전 자동 시작 |
| 23:00~익일 06:00 | **US 인텔 스캔** (매시) | 미국 뉴스 AI 분석 |

### 익일 새벽 (미국 장)

| 시간 (KST) | 작업 |
|-----------|------|
| 00:00~06:00 | US 인텔 스캔 계속 |
| 06:05 | US 실시간 모니터 종료 |

### 주간/월간

| 주기 | 시간 | 작업 |
|------|------|------|
| 매주 일요일 09:00 | | 스코어-수익률 상관분석 리포트 → Discord |
| 매주 일요일 22:00 | | YouTube 리서치 갱신 (신규 전략 발굴) |
| 매월 1일 09:00 | | 가중치 자동 최적화 (성과 기반) |

---

## 7. 재스코어링 (Rescore) 시스템

### 왜 필요한가?
일일 스캔은 아침에 한 번만 실행됩니다. 하지만 수급·뉴스·VIX는 장중에도 바뀝니다.
재스코어링은 최신 캐시 데이터를 이용해 **Technical을 제외한 4개 축**을 재계산하고 복합 점수를 갱신합니다.

### 트리거 종류

| 트리거 | 시점 | 대상 |
|--------|------|------|
| 스케줄 재스코어링 | 09:30 / 11:30 / 13:30 / 15:30 | 오늘 스캔된 전 종목 |
| 인텔 스캔 후 | 매시 인텔 스캔 완료 직후 | 전 종목 |
| 수급 재스코어링 | 16:10 (Flow 캐시 만료 후) | 전 종목 |
| 합의 전환 시 | 실시간 모니터에서 Consensus Flip 감지 즉시 | 해당 종목만 |

### 합의 전환 시 재스코어링 특이사항
실시간 모니터가 BUY ↔ SELL 합의 전환을 감지하면 `rescore_ticker_by_signal()`이 호출됩니다.
- Technical Score: BUY 신호 → 0.75, SELL → 0.25, HOLD → 0.50 (근사값 사용)
- 나머지 4축: 최신 캐시에서 즉시 재계산

---

## 8. 웹 대시보드 페이지 안내

서버 주소: **http://168.107.42.41:8000**

| 경로 | 페이지 | 내용 |
|------|--------|------|
| `/` | 홈 | 시스템 상태 요약 |
| `/scoring` | **스코어링 현황** | 전 종목 5축 점수 + 복합 점수 테이블 |
| `/signals` | **매매 대시보드** | 매수 추천 / 관망 / 매도 추천 종목 목록 |
| `/monitor` | **실시간 모니터** | 모니터 ON/OFF, 실시간 시그널 스트림 |
| `/market-intel` | **인텔리전스** | AI 분석 뉴스 이슈 목록, 종목별 감성 |
| `/performance` | **성과 분석** | Paper Trading P&L, Spearman 상관계수 |
| `/portfolio` | **포트폴리오** | 가상 보유 포지션 |
| `/backtest` | **백테스트** | 전략별 과거 수익률 검증 |
| `/strategies` | **전략 목록** | 등록된 기술적 전략 관리 |
| `/risk` | **리스크 관리** | 포트폴리오 한도 설정 현황 |
| `/discovery` | **종목 발굴** | 신규 유망 종목 스캔 결과 |

---

## 9. Paper Trading & 성과 검증

### Paper Trading이란?
실제 돈을 쓰지 않고 **가상 매매**를 시뮬레이션하는 기능입니다.
시스템이 BUY 신호를 내면 가상으로 매수하고, SELL 신호에 가상 매도합니다.

- 보유 종목이 `/portfolio` 및 `/signals` 페이지에 "보유중"으로 표시됩니다
- 실제 계좌 잔고가 아니며, DB의 `paper_positions` 테이블에 기록됩니다
- KIS API를 연결하면 실제 계좌 연동도 가능 (현재는 Paper Trading 모드)

### 성과 검증 흐름

```
[BUY 신호 발생]
    ↓
signal_price 기록 (시그널 당시 가격)
    ↓
[16:00 IntelPriceTracker 실행]
    ↓
당일 종가 vs signal_price 비교 → pnl_pct 계산
    ↓
[scoring_results ↔ signal_performance JOIN]
    ↓
[일요일 09:00 CorrelationReport]
각 축별 Spearman 순위상관계수 계산
→ |r| < 0.1이면 가중치 재조정 필요 경고
→ Discord로 주간 리포트 전송
```

### 상관계수 해석

| Spearman r | 의미 |
|-----------|------|
| 0.3 이상 | 해당 축이 수익률과 양의 상관 (가중치 유지 또는 확대) |
| 0.1 ~ 0.3 | 약한 상관 (모니터링 필요) |
| 0.1 미만 | 상관 없음 (가중치 축소 검토) |
| 음수 | 역효과 (즉시 검토) |

---

## 10. 데이터 소스 & 캐시 전략

### 데이터 소스

| 데이터 | 소스 | 비고 |
|--------|------|------|
| 한국 주가 (OHLCV) | KIS API → yfinance fallback | 실시간 |
| 한국 재무제표 | **DART 전자공시 API** | 연결재무제표 우선 |
| 한국 수급 (외국인/기관) | **네이버 금융 스크래퍼** → pykrx fallback | 14일 |
| 미국 주가·재무 | yfinance | 실시간/분기 |
| VIX (매크로) | yfinance (`^VIX`) | 일별 |
| 뉴스 (인텔) | **네이버 검색 API** | 매시 |

> OCI 클라우드에서 pykrx가 KRX 서버 IP 차단을 당하는 문제를 우회하기 위해
> DART API(재무)와 네이버 스크래퍼(수급)를 primary 소스로 사용합니다.

### TTL 캐시 전략

| 캐시 | TTL | 비고 |
|------|-----|------|
| Fundamental | 4시간 | DART API 호출 최소화 |
| Flow (수급) | 4시간 | 장 마감 후 16:10에 강제 만료 |
| Macro (VIX) | 2시간 | 전 종목 공통값 |
| Intel | 1시간 | DB 조회 캐시 |
| 섹터 맵 | 1일 | FDR 전체 종목 리스트 |
| DART corp_code | 24시간 | corpCode.xml 매핑 |

캐시는 module-level `TTLCache` 인스턴스로 관리되어 스케줄러가 인스턴스를 재생성해도 캐시가 유지됩니다.

---

## 11. 프로젝트 구조

```
money_mani/
├── config/
│   ├── scoring.yaml          # 5축 가중치, 매수/매도 임계값, 섹터 벤치마크
│   ├── risk.yaml             # 최대 포지션 수, 섹터 한도, 일일 손실 한도
│   ├── settings.yaml         # 전체 시스템 설정 (스케줄, LLM, 알림 등)
│   └── strategies/           # 기술적 전략 YAML 파일들
│
├── scoring/
│   ├── multi_layer_scorer.py # 5축 복합 스코어 계산 메인 클래스
│   ├── data_collectors.py    # FundamentalCollector, FlowCollector, MacroCollector
│   ├── intel_scorer.py       # IntelScorer (DB에서 감성 점수 집계)
│   ├── exit_scorer.py        # 매도 타이밍 스코어
│   ├── risk_manager.py       # 포트폴리오 리스크 한도 체크
│   ├── diversity_scorer.py   # 앙상블 다양성 평가
│   ├── dart_fundamental.py   # DART API 재무데이터 수집
│   └── dart_event_scorer.py  # DART 공시 이벤트 스코어링
│
├── pipeline/
│   ├── scheduler.py          # APScheduler 전체 스케줄 등록
│   ├── daily_scan.py         # 일일 스캔 (08:00 실행)
│   ├── rescore.py            # 재스코어링 함수 (run_rescore, rescore_ticker_by_signal)
│   ├── market_intel.py       # MarketIntelScanner (뉴스 수집 + LLM 분석)
│   ├── intel_price_tracker.py# 인텔 시그널 가격 추적
│   ├── correlation_logger.py # 스코어-수익률 상관계수 기록
│   ├── correlation_report.py # 주간 상관분석 리포트
│   ├── evening_report.py     # 저녁 성과 리포트 (19:00)
│   └── nightly.py            # 야간 오케스트레이터
│
├── monitor/
│   ├── realtime_monitor.py   # 실시간 모니터 메인 (60초 루프)
│   ├── rolling_buffer.py     # OHLCV 롤링 버퍼 (최대 200봉)
│   ├── signal_tracker.py     # 알림 쿨다운 관리 (30분)
│   └── market_session.py     # 장 운영 시간 판별
│
├── market_data/
│   ├── krx_fetcher.py        # KRX 주가 (pykrx + KIS API)
│   ├── us_fetcher.py         # 미국 주가 (yfinance)
│   ├── naver_flow_fetcher.py # 네이버 금융 수급 스크래퍼
│   └── fdr_fetcher.py        # 전체 상장 종목 목록
│
├── web/
│   ├── app.py                # FastAPI 애플리케이션
│   ├── routers/              # 페이지별 라우터
│   ├── services/             # 비즈니스 로직 (scoring_service, signal_service 등)
│   └── db/                   # SQLite 연결, 마이그레이션
│
├── broker/
│   ├── kis_client.py         # 한국투자증권 KIS API 클라이언트
│   └── portfolio.py          # 포트폴리오 관리
│
├── utils/
│   ├── cache.py              # TTLCache (thread-safe, monotonic clock)
│   └── config_loader.py      # YAML 설정 + 환경변수 로드
│
└── scripts/
    ├── verify_flow_scale.py  # 수급 Amount Scale 검증
    └── correlation_analysis.py # 스코어-수익률 수동 분석
```

---

## 12. 서버 배포 & 운영

### 서버 정보
- **주소**: `168.107.42.41` (OCI 클라우드, Ubuntu)
- **SSH 접속**: `ssh money-mani`
- **경로**: `/home/ubuntu/money_mani`
- **Python**: `/home/ubuntu/money_mani/.venv/bin/python`

### systemd 서비스

| 서비스 | 역할 |
|--------|------|
| `money-mani` | FastAPI 웹 서버 (포트 8000) |
| `money-mani-scheduler` | APScheduler 스케줄러 |

```bash
# 서비스 상태 확인
ssh money-mani "systemctl status money-mani money-mani-scheduler"

# 서비스 재시작
ssh money-mani "sudo systemctl restart money-mani money-mani-scheduler"

# 로그 확인
ssh money-mani "journalctl -u money-mani-scheduler -n 50 --no-pager"
```

### 코드 배포
git을 사용하지 않고 rsync로 배포합니다.

```bash
# 단일 파일 배포
rsync -av scoring/data_collectors.py money-mani:/home/ubuntu/money_mani/scoring/

# 전체 소스 배포 (캐시·DB 제외)
rsync -av --exclude='.git' --exclude='__pycache__' --exclude='*.db' \
  . money-mani:/home/ubuntu/money_mani/
```

### 환경변수 (.env)

```env
OPENROUTER_KEY=sk-or-v1-xxxx       # LLM (OpenRouter / Gemini)
DISCORD_WEBHOOK_URL=https://...     # Discord 알림
DART_API_KEY=xxxx                   # DART 전자공시 API
NAVER_CLIENT_ID=xxxx                # 네이버 검색 API
NAVER_CLIENT_SECRET=xxxx
KIS_API_KEY=xxxx                    # 한국투자증권 KIS (선택)
KIS_API_SECRET=xxxx
KIS_ACCOUNT_NUMBER=xxxx
```

---

## 13. 설정 커스터마이징

### 가중치 변경 (config/scoring.yaml)

```yaml
weights:
  KRX:
    technical: 0.30    # 기술적 전략 합의
    fundamental: 0.25  # 재무 건전성
    flow: 0.20         # 수급
    intel: 0.15        # AI 뉴스 분석
    macro: 0.10        # VIX 매크로
```

### 매수 임계값 변경

`web/services/signal_service.py` 에서 `if score >= 0.65` 값을 수정합니다.

### 포트폴리오 한도 변경 (config/risk.yaml)

```yaml
max_positions: 20        # 최대 보유 종목 수
max_single_weight: 0.20  # 단일 종목 최대 비중 20%
max_sector_weight: 0.30  # 단일 섹터 최대 비중 30%
max_daily_loss: -0.03    # 일일 최대 손실 -3%
```

### 감시 종목 변경 (config/settings.yaml)

```yaml
realtime:
  watchlist:
    krx: ["005930", "000660", "035420"]  # 삼성전자, SK하이닉스, NAVER
    us: ["AAPL", "MSFT", "NVDA"]
```

---

## 14. 주의사항

- 이 시스템의 매매 신호는 **투자 참고용**입니다. 실제 투자 결정은 본인 판단으로 하세요.
- 과거 백테스트 성과가 미래 수익을 보장하지 않습니다.
- 네이버 금융 스크래퍼는 HTML 구조 변경 시 파싱 실패할 수 있습니다. 로그를 모니터링하세요.
- DART API 무료 티어는 일 10,000건 제한입니다. 대량 스캔 시 한도에 주의하세요.
- KIS API 연결 없이는 Paper Trading 모드로만 동작합니다 (실제 매매 없음).
