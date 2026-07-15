# Money Mani — 주식 자동 스코어링 & 매매 신호 시스템

현재 로컬 PC의 `http://localhost:31234`에서 운영 중인 한국/미국 주식 자동화 파이프라인입니다.
5축 복합 스코어로 종목을 평가하고, 일일 스캔과 인텔 갱신 결과를 대시보드와 Discord로 알립니다.
실시간 모니터는 일봉/장중 시계열 재설계가 끝날 때까지 안전 잠금 상태입니다.

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
http://localhost:31234
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

로컬 주소: **http://localhost:31234**

| 경로 | 페이지 | 내용 |
|------|--------|------|
| `/` | 홈 | 시스템 상태 요약 |
| `/scoring` | **스코어링 현황** | 전 종목 5축 점수 + 복합 점수 테이블 |
| `/signals` | **매매 대시보드** | 매수 추천 / 관망 / 매도 추천 종목 목록 |
| `/monitor` | **실시간 모니터** | 현재 안전 잠금 상태와 비활성 사유 확인 |
| `/intel` | **인텔리전스** | AI 분석 뉴스 이슈 목록, 종목별 감성 |
| `/paper-trading` | **모의투자** | 가상 주문, 포지션, 손익, 30일 가격·점수 추적 |
| `/portfolio` | **실계좌 포트폴리오** | KIS 실계좌 잔고와 보유 종목 조회 |
| `/performance` | **신호 성과 추적** | 신호 이후 수익률, Spearman 상관계수 |
| `/backtest` | **백테스트** | 전략별 과거 수익률 검증 |
| `/strategies` | **전략 목록** | 등록된 기술적 전략 관리 |
| `/risk` | **리스크 관리** | 포트폴리오 한도 설정 현황 |
| `/discovery` | **종목 발굴** | 신규 유망 종목 스캔 결과 |

---

## 9. Paper Trading & 성과 검증

### Paper Trading이란?
실제 돈이나 주문 가능 현금 한도를 사용하지 않고 **독립된 가상 원장**에서 매매를 연습하는 기능입니다.
`/paper-trading` 또는 `/signals`에서 수량을 선택하고 예상 가격·수수료·시세 지연 여부를 확인한 뒤 사용자가 직접 모의 체결을 확정합니다.

- `/paper-trading`: 모의 포지션, 보유 원가, 평가액, 실현·미실현손익, 수수료와 수정 불가능한 체결 이력
- `/portfolio`: KIS에서 조회한 **실계좌 포트폴리오**. 모의 주문으로 잔고가 바뀌지 않습니다
- `/signals`: legacy 전략 포지션은 “전략 추적 보유”, 새 가상 원장은 “모의 보유”로 분리합니다. KIS 실계좌 보유 여부는 `/portfolio`에서만 확인합니다
- 주문 확정 시 서버가 가격을 다시 조회합니다. 최근 종가 fallback은 `지연 시세`로 명시해 체결할 수 있고, 모든 시세 소스가 실패한 경우에만 오류로 중단합니다

### 성과 검증 흐름

```
[추천에서 모의 주문 미리보기]
    ↓ 가격·수수료·추천 스냅샷 확인
[모의 체결 확정]
    ↓ 포지션 + 불변 체결 이력 기록
[시세·5축·Exit 점수 갱신]
    ↓ 실현/미실현손익 + 매수 당시 대비 점수 변화
[30일 가격·복합 점수·Exit 점수 차트]
```

모의 원장 손익은 `/paper-trading`에서 확인합니다. `/performance`는 신호 이후 실제 수익률과 스코어 상관관계를 분석하는 별도 검증 화면입니다.

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

운영 대상은 `hermes-vps`이며, 애플리케이션은 Docker에서 실행합니다. 호스트에는
`127.0.0.1:32777`만 바인딩하고 Tailscale Funnel이 공개 HTTPS를 제공합니다.
기존 Traefik의 80/443 및 Hostinger 관리 포트 32781은 사용하거나 변경하지 않습니다.

```text
외부 브라우저
  → https://<tailscale-host>.<tailnet>.ts.net:8443
  → Tailscale Funnel
  → 127.0.0.1:32777
  → web:31234 ↔ SQLite ↔ scheduler
```

### 배포 파일과 영속 상태

| 경로 | 역할 |
|------|------|
| `/srv/money-mani/app` | 공개 Git 저장소의 정확한 commit SHA checkout |
| `/srv/money-mani/shared` | DB, 설정, 전략, output, MEMORY, OAuth, 백업 |
| `/srv/money-mani/secrets/app.env` | API 키와 내부 토큰; 권한 600 |
| `compose.hermes.yml` | 단일 web worker와 단일 scheduler |

컨테이너 루트 파일시스템은 읽기 전용이며, 위 영속 경로만 쓰기 가능합니다.
DB 경로는 `MONEY_MANI_DB_PATH`, scheduler 내부 주소는
`MONEY_MANI_WEB_BASE_URL`로 주입합니다. Docker 네트워크는 고정 gateway
`172.30.77.1`만 신뢰 프록시로 허용합니다. 해당 subnet이 VPS와 충돌할 때만
deploy env의 `MONEY_MANI_DOCKER_SUBNET`, `MONEY_MANI_DOCKER_GATEWAY`,
`MONEY_MANI_FORWARDED_ALLOW_IPS`를 같은 값으로 함께 변경합니다.

### 최초 호스트 준비

먼저 코드와 테스트를 커밋하고 공개 원격 저장소에 push합니다. `.env`, DB,
백업, WAL/SHM, `MEMORY.md`가 commit에 포함되지 않았는지 반드시 확인합니다.

```bash
.venv/bin/python -m pytest -q
python3 -m deploy.hermes.secret_scan --revision HEAD
git status --short
git rev-parse HEAD  # 이후 모든 명령에서 이 40자 SHA를 사용
```

root로 `deploy/hermes/bootstrap_host.sh`를 한 번 실행하면 기본 UID 1000의
`money-mani` 사용자, Docker 그룹과 `/srv/money-mani` 디렉터리를 준비합니다.
root의 `authorized_keys` 전체는 절대 복사하지 않으며, 명시한 단일 공개키만
fingerprint 검증 후 설치합니다. Docker 그룹은 root에 준하는 권한이므로 이
계정은 배포 전용으로만 사용합니다.

```bash
scp deploy/hermes/bootstrap_host.sh hermes-vps:/tmp/
scp ~/.ssh/ssh-key-hwang.pub hermes-vps:/tmp/ssh-key-hwang.pub
ssh hermes-vps \
  'MONEY_MANI_ALLOW_DOCKER_ROOT_EQUIVALENT=1 bash /tmp/bootstrap_host.sh \
    --authorized-key-file /tmp/ssh-key-hwang.pub && \
    rm -f /tmp/ssh-key-hwang.pub'
ssh -i ~/.ssh/ssh-key-hwang money-mani@187.127.121.97
```

두 번째 터미널에서 `money-mani` 키 로그인이 성공하고 Hostinger 콘솔 접근을
확보한 뒤에만 `PermitRootLogin no`, `PasswordAuthentication no`를 SSH drop-in에
설정하고 `sshd -t` 성공 후 SSH를 reload합니다. 로컬 SSH alias에는 다음을
사용하며 기존 `LocalForward`는 비상 접속용으로 유지합니다.

```sshconfig
Host hermes-vps
    HostName 187.127.121.97
    User money-mani
    IdentityFile ~/.ssh/ssh-key-hwang
    IdentitiesOnly yes
    LocalForward 9119 127.0.0.1:32777
```

### Tailscale과 공개 HTTPS

VPS에 Tailscale을 설치하고 로그인한 뒤 hostname을 고정합니다. Funnel은 앱과
인증 검증이 끝난 마지막 단계에서만 켭니다.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=money-mani-hermes
sudo tailscale funnel --bg --https=8443 http://127.0.0.1:32777
tailscale funnel status
```

Funnel이 출력한 정확한 origin을
`MONEY_MANI_ALLOWED_ORIGINS=https://...:8443`으로 `app.env`에 저장하고,
`MONEY_MANI_ALLOWED_HOSTS`에는 `localhost,127.0.0.1,web`과 scheme/port 없는
정확한 Funnel hostname을 나열합니다. `*.ts.net` wildcard는 허용하지 않습니다.
환경 파일을 바꾼 뒤에는 다음 명령으로 적용합니다.

```bash
docker compose --env-file /srv/money-mani/deploy.env \
  -f compose.hermes.yml up -d --force-recreate web scheduler
```

Funnel은 일반 브라우저 방문자에게 공개되므로 owner/viewer 로그인이 필수입니다.

### 로컬 데이터 컷오버

최종 컷오버 때 로컬 web과 scheduler를 먼저 완전히 중지합니다. 아래 도구는
31234 포트가 열려 있으면 거부하고, WAL checkpoint 후 SQLite Backup API로
스냅샷을 만들며 모든 파일의 SHA-256 매니페스트를 기록합니다.

```bash
.venv/bin/python -m deploy.hermes.prepare_cutover \
  --confirm-services-stopped \
  --destination "cutover-$(date -u +%Y%m%dT%H%M%SZ)"

rsync -az --protect-args cutover-<timestamp>/ \
  hermes-vps:/srv/money-mani/incoming/cutover-<timestamp>/
```

VPS에서 정확한 SHA를 checkout한 뒤 번들을 검증·설치합니다. `app.env`에는 기존
API 키 외에 32자 이상의 무작위 `MONEY_MANI_INTERNAL_TOKEN`을 추가합니다.

```bash
git clone https://github.com/hth950/money_mani.git /srv/money-mani/app
cd /srv/money-mani/app
git switch --detach <40-char-sha>
python3 -m deploy.hermes.install_cutover \
  --bundle /srv/money-mani/incoming/cutover-<timestamp> \
  --confirm-services-stopped

python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
chmod 600 /srv/money-mani/secrets/app.env
```

원격 설치기는 검증 성공 후 전송 번들의 비밀 파일을 덮어쓰고 번들을 제거합니다.
외부 로그인과 row count까지 확인한 뒤 로컬 번들도 정리합니다.

```bash
.venv/bin/python -m deploy.hermes.cleanup_cutover \
  --bundle cutover-<timestamp> \
  --confirm-remote-verified
```

### 배포, 사용자, 검증

`deploy.sh`는 remote branch에 존재하는 정확한 40자 SHA만 허용합니다. 시작 전
온라인 DB 백업을 만들고 이미지를 해당 SHA로 태깅한 뒤 web healthcheck가
성공해야 scheduler를 하나만 시작합니다.

```bash
cd /srv/money-mani/app
./deploy/hermes/deploy.sh <40-char-sha>

# SSH 터미널에서 비밀번호를 안전하게 입력해 사용자 생성
docker compose --env-file /srv/money-mani/deploy.env \
  -f compose.hermes.yml exec web \
  python -m web.auth_cli create --username admin --role owner
docker compose --env-file /srv/money-mani/deploy.env \
  -f compose.hermes.yml exec web \
  python -m web.auth_cli create --username guest --role viewer

curl -fsS http://127.0.0.1:32777/healthz
docker compose --env-file /srv/money-mani/deploy.env \
  -f compose.hermes.yml ps
```

외부에서는 Funnel URL의 로그인, owner 변경 기능과 viewer 조회 제한을 각각
검증합니다. 공인 IP의 31234/32777은 계속 닫혀 있어야 하며,
`ssh hermes-vps` 연결 중 `http://localhost:9119`는 비상 경로로 사용할 수 있습니다.

### 백업과 롤백

다음 설치 스크립트는 매일 03:30 KST에 온라인 백업과 무결성/FK 검사를 실행해
일간 14개와 일요일 주간본 8개를 보관합니다.

```bash
sudo /srv/money-mani/app/deploy/hermes/install_backup_timer.sh
systemctl list-timers money-mani-backup.timer --no-pager
```

직전 배포로 되돌릴 때는 기록된 이전 SHA와 그 배포 직전 DB가 한 쌍으로
복구됩니다. 이때 현재 `app_users`의 비밀번호 해시·역할·활성 상태와 인증 감사
기록은 승계하고 모든 세션은 폐기하므로, 과거 세션이나 변경 전 비밀번호가
부활하지 않습니다. 임의 SHA는 그 시점의 검증된 DB 백업도 명시해야 합니다.

```bash
./deploy/hermes/rollback.sh
./deploy/hermes/rollback.sh --to <40-char-sha> \
  --database-backup /srv/money-mani/shared/backups/<verified>.db
```

컷오버 후 SQLite 쓰기 주체는 VPS 하나뿐입니다. 로컬 web/scheduler를 다시
시작하려면 먼저 최신 원격 DB를 동일한 검증 절차로 내려받아야 합니다.

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
- 모의 주문은 KIS 실계좌 주문을 호출하지 않으며 실제 잔고를 변경하지 않습니다.
