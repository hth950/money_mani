# Money Mani - 주식 투자 리서치 & 자동 알림 파이프라인

YouTube에서 주식 투자 전략 영상을 검색하고, AI로 분석하여 구체적인 매매 전략을 추출한 뒤, 과거 데이터로 백테스트하고, 매일 아침 매매 시그널을 Discord로 알려주는 자동화 파이프라인입니다.

## 목표

1. **전략 발굴 자동화** - YouTube의 투자 전략 영상을 자동 검색하고 AI(NotebookLM + OpenRouter LLM)로 분석하여 코드화 가능한 매매 전략을 추출
2. **백테스트 검증** - 추출된 전략을 과거 주가 데이터로 백테스트하여 실제 수익성을 검증
3. **앙상블 시그널** - 다수 전략의 합의(Consensus) 기반으로 신뢰도 높은 매매 시그널 생성
4. **시장 인텔리전스** - LLM + 웹 검색으로 한국/미국 시장 이슈를 자동 탐지, 종목 영향 추적
5. **실시간 모니터링** - KIS API로 장중 실시간 체결가를 조회하여 매수/매도 시그널 즉시 알림
6. **웹 대시보드** - FastAPI 기반 웹 UI로 전략·시그널·시장 인텔리전스·포트폴리오 통합 관리

## 전체 흐름

```
[YouTube 검색] → [LLM 영상 필터링] → [NotebookLM 분석] → [LLM 전략 추출/검증]
                                           ↓ 실패 시
                                      [자막 직접 추출]
                                           ↓
                                  [Strategy YAML 저장]
                                           ↓
                            [과거 데이터 조회 (pykrx/yfinance)]
                                           ↓
                              [pandas_ta 지표 + 시그널 생성]
                                           ↓
                            [backtesting.py 백테스트 실행]
                                           ↓
                     [다중 전략 앙상블 합의 → 멀티레이어 스코어링]
                                           ↓
                     [LLM 웹검색 기반 시장 인텔리전스 (KRX/US)]
                                           ↓
                         [KIS API 실시간 모니터 (장중 자동 실행)]
                                           ↓
                   [Discord 알림] + [웹 대시보드] + [Email 백업]
```

---

## 설치 방법

### 사전 요구사항

- Python 3.12
- Python 3.14 (NotebookLM 연동용, 선택사항)

### 1. 가상환경 생성 및 패키지 설치

```bash
py -3.12 -m venv .venv
source .venv/Scripts/activate    # Windows Git Bash
# 또는
.venv\Scripts\activate           # Windows CMD

pip install -r requirements.txt
```

> pykrx 설치 시 `pkg_resources` 에러가 나면:
> ```bash
> pip install "setuptools<80" --force-reinstall
> ```

### 2. 환경변수 설정

프로젝트 루트에 `.env` 파일을 생성합니다:

```env
OPENROUTER_KEY=sk-or-v1-xxxxxxxxxxxx
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxx/xxxx

# KIS API (실시간 모니터링용)
KIS_HTS_ID=your_hts_id
KIS_API_KEY=your_api_key
KIS_API_SECRET=your_api_secret
KIS_ACCOUNT_NUMBER=12345678-01

# 네이버 뉴스 API (선택사항)
NAVER_CLIENT_ID=your_naver_client_id
NAVER_CLIENT_SECRET=your_naver_client_secret
```

| 변수 | 설명 | 필수 |
|------|------|------|
| `OPENROUTER_KEY` | [OpenRouter](https://openrouter.ai/) API 키 | O |
| `DISCORD_WEBHOOK_URL` | Discord 채널 Webhook URL | O |
| `KIS_HTS_ID` | 한국투자증권 HTS 로그인 ID | O (실시간 모니터) |
| `KIS_API_KEY` | KIS Open API 앱 키 | O (실시간 모니터) |
| `KIS_API_SECRET` | KIS Open API 시크릿 | O (실시간 모니터) |
| `KIS_ACCOUNT_NUMBER` | 계좌번호 (예: 12345678-01) | O (실시간 모니터) |
| `NAVER_CLIENT_ID` | 네이버 검색 API Client ID | X |
| `NAVER_CLIENT_SECRET` | 네이버 검색 API 시크릿 | X |
| `EMAIL_SENDER` | Gmail 발신 주소 | X |
| `EMAIL_APP_PASSWORD` | Gmail 앱 비밀번호 | X |

### 3. NotebookLM 설정 (선택사항)

NotebookLM 분석 기능을 사용하려면 Python 3.14에 `notebooklm-py`를 설치해야 합니다:

```bash
py -3.14 -m pip install notebooklm-py
py -3.14 -m notebooklm login
```

NotebookLM이 설정되지 않으면 자막 직접 추출 + LLM 분석으로 자동 fallback됩니다.

---

## 실행 방법

가상환경 활성화 후 실행합니다:

```bash
source .venv/Scripts/activate
```

### 전략 목록 확인

```bash
python main.py strategies
```

저장된 전략 목록을 출력합니다.

```
Strategies (1):
  [V] Golden Cross MA(20,60) (crossover) - validated
```

상태 아이콘: `V`=검증됨, `~`=테스트중, `o`=초안, `X`=폐기

### 백테스트 실행

특정 전략을 과거 데이터로 백테스트합니다.

```bash
# 삼성전자 (005930)에 골든크로스 전략 백테스트
python main.py backtest -s example_golden_cross -t 005930

# 여러 종목 동시 백테스트
python main.py backtest -s example_golden_cross -t 005930,000660,035420

# 미국 주식 백테스트
python main.py backtest -s example_golden_cross -t AAPL -m US
```

출력 예시:

```
==================================================
[백테스트 결과] Golden Cross MA(20,60)
종목: 005930   기간: 2020-01-02~2026-03-07
==================================================
  총 수익률    : +1.34%
  샤프 비율    : 0.84
  최대 낙폭    : -15.23%
  승률         : 41.67%
  거래 횟수    : 12회
  유효성 검증  : 통과
==================================================
```

### 일일 스캔 (앙상블 + 멀티레이어 스코어링)

검증된 전략의 앙상블 합의 기반으로 오늘의 매매 시그널을 확인합니다.

```bash
python main.py scan
```

동작 순서:
1. 오늘이 거래일인지 확인 (KRX: 월~금 + 공휴일 제외, NYSE: 동일)
2. `validated` 상태인 전략만 로드
3. 각 종목에 대해 전략별 시그널 계산 → 앙상블 합의 비율 산출
4. 멀티레이어 스코어링 (기술적 + 펀더멘털 + 투자자 흐름 + 인텔 스코어)
5. 합의 확신도에 따라 `EXECUTE` / `WATCH` / `SKIP` 분류
6. Discord로 시그널 알림 전송

### 실시간 모니터링

KIS API로 장중 실시간 체결가를 모니터링합니다. 장 시작 시 자동으로 켜지고 장 마감 시 종료됩니다.

```bash
# 전체 시장 모니터링
python main.py monitor

# KRX만 모니터링
python main.py monitor --market KRX

# US만 모니터링
python main.py monitor --market US
```

- 60초(기본) 간격으로 실시간 체결가 조회
- 보유 종목(HOLD 모드)은 매도 시그널, 감시 종목(WATCH 모드)은 매수 시그널 체크
- 쿨다운(기본 30분) 내 중복 알림 방지

### 포트폴리오 조회

```bash
python main.py portfolio
```

KIS API를 통해 현재 보유 종목 목록과 평가손익을 출력합니다.

### 전략 자동 발굴

가격 패턴 기반으로 수익성 높은 전략을 자동 탐색합니다.

```bash
python main.py discover
```

### YouTube 리서치

YouTube에서 주식 전략 영상을 검색합니다.

```bash
# 특정 키워드로 검색
python main.py research -q "주식 골든크로스 전략"

# 최대 영상 수 지정
python main.py research -q "주식 단타 매매법" -n 20

# 기본 검색어 사용 (config/search_queries.yaml에 정의된 쿼리)
python main.py research
```

LLM이 영상의 품질을 1~10점으로 채점하고, 어그로/클릭베이트를 자동 필터링합니다.

### NotebookLM 분석

```bash
python main.py analyze -q "주식 이동평균선 전략"
```

### 스케줄러 (자동 실행)

```bash
python main.py schedule
```

| 작업 | 실행 시간 | 설명 |
|------|-----------|------|
| 일일 스캔 | 월~금 08:30 KST | 장 시작 전 앙상블 시그널 체크 |
| 장전 인텔 | 월~금 08:00 KST | KRX 장전 시장 인텔리전스 |
| 장중 인텔 | 월~금 12:00 KST | 장중 이슈 업데이트 |
| 장후 인텔 | 월~금 16:00 KST | 장후 분석 |
| US 인텔 | 월~금 (US 시간대) | US 프리마켓/장중/장후 인텔리전스 |
| 인텔 가격 추적 | 매일 17:00 KST | 이슈 예측 정확도 사후 검증 |
| 상관관계 로그 | 월~금 18:00 KST | 인텔 예측 vs 앙상블 시그널 상관관계 기록 |
| 리서치 갱신 | 매주 일 22:00 KST | 새 YouTube 영상 검색 + 전략 추출 |

`Ctrl+C`로 종료합니다. 서버에서 백그라운드로 실행하려면:

```bash
nohup python main.py schedule > output/logs/scheduler.log 2>&1 &
```

### 웹 대시보드

FastAPI 기반 웹 서버를 실행합니다:

```bash
python run_web.py
```

브라우저에서 `http://localhost:8000`으로 접속합니다.

**주요 기능:**
- 전략 목록 / 백테스트 결과 조회
- 일일 시그널 히스토리
- 시장 인텔리전스 이슈 목록
- 실시간 모니터 상태 오버레이
- 포트폴리오 현황
- 종목 발굴 대시보드

### 전체 파이프라인

```bash
python main.py full -q "주식 단타 매매 전략"
```

---

## 서버 배포 (Oracle Cloud)

본 서비스는 **Oracle Cloud Infrastructure(OCI) ARM 인스턴스**에서 24시간 상시 운영 중입니다.

| 항목 | 값 |
|------|----|
| 서버 IP | `168.107.42.41` |
| OS | Ubuntu (ARM) |
| SSH 접속 유저 | `ubuntu` |
| 인증키 | `~/.ssh/oracle_cloud2` |

### SSH 접속

`~/.ssh/config`에 아래 설정을 추가하면 `ssh oracle`로 간편하게 접속할 수 있습니다:

```
Host oracle
    HostName 168.107.42.41
    User ubuntu
    IdentityFile ~/.ssh/oracle_cloud2
    StrictHostKeyChecking no
```

```bash
ssh oracle
```

### 서버에서 서비스 실행

```bash
# 프로젝트 디렉토리 이동
cd ~/money_mani

# 가상환경 활성화
source .venv/bin/activate

# 스케줄러 백그라운드 실행
nohup python main.py schedule > output/logs/scheduler.log 2>&1 &

# 웹 대시보드 백그라운드 실행
nohup python run_web.py > output/logs/web.log 2>&1 &
```

### 프로세스 확인 및 종료

```bash
# 실행 중인 프로세스 확인
ps aux | grep "main.py\|run_web"

# 로그 실시간 확인
tail -f ~/money_mani/output/logs/scheduler.log
tail -f ~/money_mani/output/logs/web.log

# 프로세스 종료
pkill -f "main.py schedule"
pkill -f "run_web.py"
```

---

## 프로젝트 구조

```
money_mani/
├── main.py                          # CLI 진입점 (10개 서브커맨드)
├── run_web.py                       # FastAPI 웹 서버 시작
├── requirements.txt                 # Python 패키지 목록
├── .env                             # 환경변수 (API 키, Webhook URL)
│
├── config/
│   ├── settings.yaml                # 마스터 설정 파일
│   ├── scoring.yaml                 # 멀티레이어 스코어링 가중치/임계값
│   ├── search_queries.yaml          # YouTube 검색 쿼리 목록
│   └── strategies/
│       └── example_golden_cross.yaml  # 전략 정의 파일 (YAML)
│
├── youtube_scraper/                 # YouTube 검색 & 자막 추출
│   ├── scraper.py                   #   yt-dlp 기반 영상 검색
│   ├── subtitles.py                 #   자막 다운로드 & 텍스트 추출
│   └── exporter.py                  #   결과 내보내기
│
├── market_data/                     # 주식 시세 데이터 수집
│   ├── krx_fetcher.py               #   한국 주식 (pykrx)
│   ├── us_fetcher.py                #   미국 주식 (yfinance)
│   ├── fdr_fetcher.py               #   종목 목록, 지수, 환율 (FinanceDataReader)
│   ├── calendar.py                  #   KRX/NYSE 거래일 판별
│   └── cache.py                     #   CSV 파일 기반 데이터 캐시
│
├── strategy/                        # 전략 관리
│   ├── models.py                    #   Strategy 데이터 모델
│   ├── registry.py                  #   YAML 전략 저장/로드/목록
│   └── extractor.py                 #   분석 텍스트 → Strategy 변환
│
├── llm/                             # LLM (OpenRouter) 연동
│   ├── client.py                    #   OpenRouter API 클라이언트
│   ├── prompts.py                   #   프롬프트 템플릿 (영상필터, 전략, 인텔 등)
│   ├── video_filter.py              #   영상 품질 필터 (LLM 채점)
│   ├── strategy_refiner.py          #   전략 정제 & 코드화 검증
│   └── backtest_interpreter.py      #   백테스트 결과 한국어 해석
│
├── backtester/                      # 백테스트 엔진
│   ├── engine.py                    #   backtesting.py 래퍼
│   ├── signals.py                   #   기술 지표 계산 & 시그널 생성
│   ├── metrics.py                   #   BacktestResult 데이터 모델
│   └── report.py                    #   한국어 결과 보고서
│
├── scoring/                         # 멀티레이어 스코어링
│   ├── multi_layer_scorer.py        #   4축 복합 스코어 (기술/펀더/흐름/인텔)
│   ├── data_collectors.py           #   펀더멘털·투자자 흐름·매크로 데이터 수집
│   ├── intel_scorer.py              #   시장 인텔리전스 스코어 계산
│   ├── exit_scorer.py               #   매도 시그널 스코어
│   ├── diversity_scorer.py          #   전략 다양성 스코어
│   └── risk_manager.py              #   리스크 관리 (포지션 사이징)
│
├── broker/                          # KIS API 연동
│   ├── kis_client.py                #   python-kis 래퍼 (실시간 체결가, 포트폴리오)
│   └── portfolio.py                 #   보유 종목 조회 및 HoldingInfo 모델
│
├── monitor/                         # 실시간 모니터링
│   ├── realtime_monitor.py          #   장중 실시간 체결가 조회 + 시그널 알림
│   ├── market_session.py            #   KRX/US 장 시간 판별 (자동 시작/종료)
│   ├── rolling_buffer.py            #   실시간 OHLCV 롤링 버퍼
│   └── signal_tracker.py           #   시그널 쿨다운 & 중복 방지
│
├── notebooklm_analyzer/             # NotebookLM 연동
│   ├── client.py                    #   Python 3.14 서브프로세스 브릿지
│   ├── analyzer.py                  #   노트북 생성/분석/전략 추출
│   └── prompts.py                   #   NotebookLM 전용 프롬프트
│
├── alerts/                          # 알림 시스템
│   ├── discord_webhook.py           #   Discord Webhook 전송
│   ├── email_sender.py              #   Gmail SMTP 전송
│   └── formatter.py                 #   Discord Embed 포맷터 (한국어)
│
├── pipeline/                        # 파이프라인 오케스트레이션
│   ├── runner.py                    #   4단계 전체 파이프라인 실행기
│   ├── daily_scan.py                #   앙상블 시그널 스캔 + 멀티레이어 스코어링
│   ├── market_intel.py              #   LLM+웹검색 기반 시장 인텔리전스 (KRX/US)
│   ├── web_search.py                #   DuckDuckGo + 네이버 뉴스 듀얼 검색
│   ├── intel_price_tracker.py       #   인텔 예측 사후 정확도 추적
│   ├── correlation_logger.py        #   인텔 예측 vs 앙상블 시그널 상관관계 기록
│   ├── discovery.py                 #   전략 자동 발굴 (패턴 탐색)
│   ├── ranking.py                   #   전략 순위 산출
│   ├── trend_scanner.py             #   트렌드 스캔
│   ├── decision_score.py            #   최종 의사결정 스코어
│   ├── evening_report.py            #   저녁 리포트 생성
│   ├── nightly.py                   #   야간 배치 작업
│   └── scheduler.py                 #   APScheduler 자동 실행
│
├── web/                             # FastAPI 웹 대시보드
│   ├── app.py                       #   FastAPI 앱 + 라우터 등록
│   ├── db/                          #   SQLite DB 연결 & 마이그레이션
│   ├── models/                      #   Pydantic 스키마
│   ├── routers/                     #   API 엔드포인트 (전략/시그널/인텔/모니터 등)
│   ├── services/                    #   비즈니스 로직 서비스 레이어
│   ├── templates/                   #   Jinja2 HTML 템플릿
│   └── static/                      #   CSS/JS 정적 파일
│
├── scripts/                         # 유틸리티 스크립트
│   ├── generate_strategies.py       #   전략 일괄 생성
│   ├── validate_and_backtest.py     #   전략 일괄 검증+백테스트
│   ├── ensemble_backtest.py         #   앙상블 백테스트
│   └── walk_forward_validate.py     #   워크포워드 검증
│
└── utils/                           # 공통 유틸리티
    ├── config_loader.py             #   YAML 설정 로드 + 환경변수 치환
    └── logging_config.py            #   로깅 설정 (파일 + 콘솔)
```

---

## 모듈별 상세 설명

### 1. YouTube 검색 (`youtube_scraper/`)

yt-dlp를 사용하여 YouTube에서 주식 전략 영상을 검색합니다. 영상의 제목, 설명, 조회수, URL 등 메타데이터를 수집하고, 필요 시 한국어 자막을 텍스트로 추출합니다.

### 2. 시세 데이터 수집 (`market_data/`)

| 클래스 | 라이브러리 | 대상 | 주요 기능 |
|--------|-----------|------|----------|
| `KRXFetcher` | pykrx | 한국 주식 | OHLCV, 재무제표, 투자자별 매매동향, 시총 상위 종목 |
| `USFetcher` | yfinance | 미국 주식 | OHLCV, 종목 정보 |
| `FDRFetcher` | FinanceDataReader | 한국+미국 | 전체 상장 종목, 지수, 환율 |

- `KRXCalendar` / `NYSECalendar`: 공휴일 + 주말을 고려한 거래일 판별
- `DataCache`: 동일 데이터 반복 조회 방지를 위한 CSV 파일 캐시

### 3. 전략 관리 (`strategy/`)

전략은 YAML 파일로 정의됩니다. 각 전략에는 사용할 기술적 지표(indicators)와 매수/매도 규칙(rules)이 포함됩니다.

**전략 상태 흐름:**
```
draft → testing → validated → retired
(초안)   (테스트)   (검증됨)    (폐기)
```

`validated` 상태인 전략만 일일 스캔에서 사용됩니다.

**전략 정의 예시** (`config/strategies/example_golden_cross.yaml`):

```yaml
name: "Golden Cross MA(20,60)"
description: "MA20이 MA60을 상향 돌파하면 매수, 하향 돌파하면 매도"
status: "validated"

indicators:
  - type: "sma"
    period: 20
    column: "close"
    output_name: "SMA_20"
  - type: "sma"
    period: 60
    column: "close"
    output_name: "SMA_60"

rules:
  entry:
    - condition: "crossover"
      indicator_a: "SMA_20"
      indicator_b: "SMA_60"
      direction: "above"
  exit:
    - condition: "crossover"
      indicator_a: "SMA_20"
      indicator_b: "SMA_60"
      direction: "below"
```

**지원 지표:** SMA, EMA, RSI, MACD, Bollinger Bands, Stochastic

**지원 조건:**
- `crossover`: 지표 A가 지표 B를 위/아래로 돌파
- `threshold`: 지표값이 특정 수치 이상/이하 (예: RSI > 70)
- `band`: 지표값이 밴드 안/밖

### 4. LLM 연동 (`llm/`)

[OpenRouter](https://openrouter.ai/) API를 통해 LLM을 활용합니다:

| 단계 | 모델 | 역할 |
|------|------|------|
| 영상 필터링 | fast (Haiku) | 영상 품질 1~10점 채점, 클릭베이트 판별 |
| 전략 추출 | default (Sonnet) | 분석 텍스트를 구조화된 JSON 전략으로 변환 |
| 전략 검증 | default (Sonnet) | pandas-ta로 코드화 가능한지 검증 |
| 결과 해석 | fast (Haiku) | 백테스트 수치를 한국어 인사이트로 변환 |
| 시장 인텔리전스 | default (Sonnet) | 뉴스 분석 → 이슈 탐지 → 영향 종목 추출 |

### 5. 백테스트 엔진 (`backtester/`)

`backtesting.py` 라이브러리를 래핑하여 YAML로 정의된 전략을 자동으로 실행합니다.

**유효성 판정 기준:**
- 거래 횟수 5회 이상
- 총 수익률 > 0%
- 샤프 비율 >= 0.5
- 최대 낙폭(MDD) >= -30%

### 6. 멀티레이어 스코어링 (`scoring/`)

4개 축을 가중 합산하여 최종 매매 확신도를 산출합니다.

| 축 | KRX 가중치 | US 가중치 | 데이터 소스 |
|----|-----------|----------|-----------|
| 기술적 (Technical) | 35% | 50% | 앙상블 전략 합의 비율 |
| 펀더멘털 (Fundamental) | 25% | 20% | pykrx 재무 데이터 |
| 투자자 흐름 (Flow) | 25% | 0% | 외국인·기관 매매동향 |
| 인텔리전스 (Intel) | 15% | 30% | 시장 인텔리전스 DB |

**의사결정 임계값:**
- `EXECUTE` (실행): 복합 스코어 >= 0.60
- `WATCH` (관찰): 복합 스코어 >= 0.40
- `SKIP` (건너뜀): 복합 스코어 < 0.40

### 7. KIS API 연동 (`broker/`)

한국투자증권 Open API(python-kis)를 통해 실시간 체결가와 포트폴리오 정보를 조회합니다.

- `KISClient`: 국내/해외 종목 실시간 체결가, 매수/매도 주문
- `PortfolioManager`: 보유 종목 조회, HoldingInfo 모델 관리

### 8. 실시간 모니터링 (`monitor/`)

KIS API로 60초 간격 실시간 데이터를 조회하며 장중 시그널을 탐지합니다.

- **자동 시작/종료**: 장 개시 시 자동 시작, 장 마감 시 자동 종료 (`MarketSession`)
- **KRX/US 분리**: `--market KRX|US` 또는 두 시장 동시 모니터링
- **WATCH/HOLD 모드**: 감시 종목(매수 탐지) / 보유 종목(매도 탐지) 구분
- **쿨다운**: 동일 종목 중복 알림 방지 (기본 30분)
- **롤링 버퍼**: 실시간 OHLCV 축적으로 기술 지표 계산

### 9. 시장 인텔리전스 (`pipeline/market_intel.py`)

LLM + 웹 검색으로 시장 이슈를 자동 탐지하여 영향 종목을 추적합니다.

**스캔 타입 (KRX/US 분리):**

| 타입 | 시간 | 대상 |
|------|------|------|
| `pre_market` | 08:00 KST | KRX 장전 브리핑 |
| `midday` | 12:00 KST | KRX 장중 업데이트 |
| `post_market` | 16:00 KST | KRX 장후 분석 |
| `us_pre_market` | US 프리마켓 시간 | US 프리마켓 |
| `us_midday` | US 장중 시간 | US 장중 |
| `us_post_market` | US 장후 시간 | US 애프터아워스 |

**웹 검색 (`pipeline/web_search.py`):**
- DuckDuckGo 검색 + 뉴스 검색 (기본)
- 네이버 뉴스 API 검색 (NAVER_CLIENT_ID 설정 시 활성화)
- 두 소스 결과를 병합하여 중복 제거

**인텔 가격 추적 (`pipeline/intel_price_tracker.py`):**
- 이슈 탐지 후 1일/3일/5일 뒤 주가 변화 추적
- 예측 정확도(accuracy_score) 사후 검증

**상관관계 로거 (`pipeline/correlation_logger.py`):**
- 인텔 예측 종목 vs 앙상블 시그널 종목 일치율 매일 기록
- DB-only 읽기로 순환 참조 없이 독립 실행

### 10. NotebookLM 분석 (`notebooklm_analyzer/`)

Google NotebookLM을 무료 RAG 시스템으로 활용합니다. notebooklm-py는 Python 3.14 전용이므로 서브프로세스 브릿지로 호출합니다.

### 11. 알림 시스템 (`alerts/`)

- **Discord**: Webhook으로 매수/매도 시그널, 앙상블 확신도, 스코어 상세, 시장 인텔리전스 알림 전송
- **Email**: Gmail SMTP 백업 (선택사항)

### 12. 웹 대시보드 (`web/`)

FastAPI + SQLite + Jinja2 기반 웹 인터페이스:

| 라우터 | 경로 | 기능 |
|--------|------|------|
| pages | `/` | 메인 대시보드 |
| strategies | `/api/strategies` | 전략 CRUD |
| signals | `/api/signals` | 시그널 히스토리 |
| market_intel | `/api/intel` | 인텔리전스 이슈 |
| monitor | `/api/monitor` | 실시간 모니터 상태 |
| portfolio | `/api/portfolio` | 포트폴리오 현황 |
| scoring | `/api/scoring` | 스코어링 결과 |
| backtest | `/api/backtest` | 백테스트 실행/결과 |
| discovery | `/api/discovery` | 전략 자동 발굴 |

---

## 설정 커스터마이징

### 감시 종목 변경

`config/settings.yaml`에서 수정:

```yaml
pipeline:
  targets:
    custom_tickers: ["005930", "000660", "035420"]  # 한국 주식
  us_targets:
    custom_tickers: ["AAPL", "MSFT", "NVDA"]       # 미국 주식
```

### 스코어링 가중치 변경

`config/scoring.yaml`에서 수정:

```yaml
weights:
  KRX:
    technical: 0.35
    fundamental: 0.25
    flow: 0.25
    intel: 0.15
  US:
    technical: 0.50
    fundamental: 0.20
    flow: 0.0
    intel: 0.30
thresholds:
  execute: 0.60   # 이 이상이면 EXECUTE
  watch: 0.40     # 이 이상이면 WATCH
```

### 실시간 모니터 설정

`config/settings.yaml`:

```yaml
realtime:
  interval_seconds: 60     # 체결가 조회 간격
  warmup_bars: 60          # 지표 계산 워밍업 캔들 수
  max_buffer_size: 200     # 롤링 버퍼 최대 크기
  cooldown_minutes: 30     # 중복 알림 방지 쿨다운
```

### 새 전략 추가

`config/strategies/` 폴더에 YAML 파일을 추가합니다:

```yaml
name: "RSI Oversold Bounce"
description: "RSI가 30 이하로 떨어졌다가 반등하면 매수"
source: "manual"
category: "momentum"
status: "validated"

indicators:
  - type: "rsi"
    period: 14
    column: "close"
    output_name: "RSI_14"

rules:
  entry:
    - condition: "crossover"
      indicator_a: "RSI_14"
      indicator_b: "30"
      direction: "above"
  exit:
    - condition: "threshold"
      indicator: "RSI_14"
      value: 70
      direction: "above"
```

### LLM 모델 변경

```yaml
llm:
  fast_model: "anthropic/claude-3-haiku"       # 빠른 작업 (필터링, 해석)
  default_model: "anthropic/claude-3.5-sonnet"  # 일반 작업 (전략 추출, 인텔)
  deep_model: "anthropic/claude-3.5-sonnet"     # 심층 분석
```

---

## 사용 라이브러리

| 라이브러리 | 용도 |
|-----------|------|
| yt-dlp | YouTube 검색 및 자막 추출 |
| pykrx | 한국 주식 시세 (KRX) |
| yfinance | 미국 주식 시세 (Yahoo Finance) |
| FinanceDataReader | 종목 목록, 지수, 환율 |
| python-kis | KIS Open API (실시간 체결가, 포트폴리오) |
| pandas-ta | 150+ 기술적 지표 계산 |
| backtesting.py | 전략 백테스트 엔진 |
| FastAPI | 웹 대시보드 API 서버 |
| uvicorn | ASGI 웹 서버 |
| Jinja2 | HTML 템플릿 렌더링 |
| duckduckgo-search | DuckDuckGo 웹/뉴스 검색 |
| requests | OpenRouter API, Discord Webhook, 네이버 뉴스 API |
| APScheduler | 스케줄러 (크론 기반 자동 실행) |
| PyYAML | 전략/설정 YAML 파싱 |
| python-dotenv | .env 환경변수 로드 |
| notebooklm-py | NotebookLM 비공식 API (Python 3.14 전용) |

---

## 주의사항

- 이 프로그램은 **투자 참고용**이며, 매매 시그널은 투자 결정의 보조 수단으로만 사용하세요
- 과거 백테스트 성과가 미래 수익을 보장하지 않습니다
- `market_data/calendar.py`의 공휴일 목록은 2026년 기준이므로 매년 업데이트가 필요합니다
- OpenRouter API 사용 시 요금이 발생할 수 있습니다
- KIS API는 실전 계좌에 연결되므로 주문 기능 사용 시 각별히 주의하세요
