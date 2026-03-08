# Money Mani - 주식 투자 리서치 & 자동 알림 파이프라인

YouTube에서 주식 투자 전략 영상을 검색하고, AI로 분석하여 구체적인 매매 전략을 추출한 뒤, 과거 데이터로 백테스트하고, 매일 아침 매매 시그널을 Discord로 알려주는 자동화 파이프라인입니다.

## 목표

1. **전략 발굴 자동화** - YouTube의 투자 전략 영상을 자동 검색하고 AI(NotebookLM + OpenRouter LLM)로 분석하여 코드화 가능한 매매 전략을 추출
2. **백테스트 검증** - 추출된 전략을 과거 주가 데이터로 백테스트하여 실제 수익성을 검증
3. **일일 시그널 알림** - 검증된 전략을 기반으로 매일 아침 한국/미국 주식의 매매 시그널을 Discord로 자동 전송

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
                              [LLM 한국어 결과 해석]
                                           ↓
                        [Discord 알림] + [Email 백업] + [콘솔 출력]
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
```

| 변수 | 설명 | 필수 |
|------|------|------|
| `OPENROUTER_KEY` | [OpenRouter](https://openrouter.ai/) API 키 | O |
| `DISCORD_WEBHOOK_URL` | Discord 채널 Webhook URL | O |
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

저장된 전략 목록을 출력합니다. 기본으로 `Golden Cross MA(20,60)` 전략이 포함되어 있습니다.

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

검색된 영상을 NotebookLM으로 심층 분석합니다.

```bash
python main.py analyze -q "주식 이동평균선 전략"
```

NotebookLM에 영상 URL을 소스로 추가하고, AI가 투자 전략을 요약/추출합니다. NotebookLM 연결 실패 시 자막을 직접 추출하여 LLM으로 분석합니다.

### 일일 스캔

검증된 전략을 기반으로 오늘의 매매 시그널을 확인합니다.

```bash
python main.py scan
```

동작 순서:
1. 오늘이 거래일인지 확인 (KRX: 월~금 + 공휴일 제외, NYSE: 동일)
2. `validated` 상태인 전략만 로드
3. 설정된 감시 종목의 최신 데이터로 시그널 계산
4. 시그널 발생 시 Discord로 알림 전송

### 스케줄러 (자동 실행)

매일 자동으로 스캔을 실행합니다.

```bash
python main.py schedule
```

| 작업 | 실행 시간 | 설명 |
|------|-----------|------|
| 일일 스캔 | 월~금 08:00 KST | 장 시작 전 매매 시그널 체크 |
| 리서치 갱신 | 매주 일 22:00 KST | 새 YouTube 영상 검색 + 전략 추출 |

`Ctrl+C`로 종료합니다. 서버에서 백그라운드로 실행하려면:

```bash
nohup python main.py schedule > output/logs/scheduler.log 2>&1 &
```

### 전체 파이프라인

리서치 → 분석 → 전략 추출 → 백테스트를 한 번에 실행합니다.

```bash
# 특정 검색어로 전체 파이프라인
python main.py full -q "주식 단타 매매 전략"

# 기본 검색어로 전체 파이프라인
python main.py full
```

---

## 프로젝트 구조

```
money_mani/
├── main.py                          # CLI 진입점 (7개 서브커맨드)
├── requirements.txt                 # Python 패키지 목록
├── .env                             # 환경변수 (API 키, Webhook URL)
│
├── config/
│   ├── settings.yaml                # 마스터 설정 파일
│   ├── search_queries.yaml          # YouTube 검색 쿼리 목록
│   └── strategies/
│       └── example_golden_cross.yaml  # 전략 정의 파일 (YAML)
│
├── youtube_scraper/                 # YouTube 검색 & 자막 추출
│   ├── scraper.py                   #   yt-dlp 기반 영상 검색
│   ├── subtitles.py                 #   자막 다운로드 & 텍스트 추출
│   ├── downloader.py                #   영상 다운로드
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
│   ├── prompts.py                   #   프롬프트 템플릿 4종
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
│   ├── daily_scan.py                #   일일 시그널 스캔 + 알림
│   └── scheduler.py                 #   APScheduler 자동 실행
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

[OpenRouter](https://openrouter.ai/) API를 통해 4곳에서 LLM을 활용합니다:

| 단계 | 프롬프트 | 모델 | 역할 |
|------|----------|------|------|
| 영상 필터링 | `VIDEO_FILTER_PROMPT` | fast (Haiku) | 영상 품질 1~10점 채점, 클릭베이트 판별 |
| 전략 추출 | `STRATEGY_REFINE_PROMPT` | default (Sonnet) | 분석 텍스트를 구조화된 JSON 전략으로 변환 |
| 전략 검증 | `STRATEGY_VALIDATE_PROMPT` | default (Sonnet) | pandas-ta로 코드화 가능한지 검증 |
| 결과 해석 | `BACKTEST_INTERPRET_PROMPT` | fast (Haiku) | 백테스트 수치를 한국어 인사이트로 변환 |

### 5. 백테스트 엔진 (`backtester/`)

`backtesting.py` 라이브러리를 래핑하여, YAML로 정의된 전략을 자동으로 실행합니다.

**실행 흐름:**
```
Strategy YAML → 지표 계산 (pandas_ta) → 시그널 생성 (1/0/-1)
    → backtesting.py Strategy 클래스 동적 생성 → 백테스트 실행
    → BacktestResult (수익률, 샤프, MDD, 승률, 거래내역)
```

**유효성 판정 기준:**
- 거래 횟수 5회 이상
- 총 수익률 > 0%
- 샤프 비율 >= 0.5
- 최대 낙폭(MDD) >= -30%

### 6. NotebookLM 분석 (`notebooklm_analyzer/`)

Google NotebookLM을 무료 RAG 시스템으로 활용합니다. YouTube 영상 URL을 소스로 추가하면 NotebookLM이 내용을 분석하고, 투자 전략을 추출해줍니다.

**Python 버전 브릿지:** notebooklm-py는 Python 3.14 전용이므로, `py -3.14 -c` 서브프로세스로 호출합니다. NotebookLM 연결 실패 시 자막 직접 추출 + LLM 분석으로 자동 fallback됩니다.

### 7. 알림 시스템 (`alerts/`)

- **Discord**: Webhook으로 매수/매도 시그널 알림, 일일 요약, 백테스트 결과 전송. 한국어 embed 포맷 (매수=녹색, 매도=빨간색)
- **Email**: Gmail SMTP 백업 (선택사항, 기본 비활성화)

### 8. 파이프라인 (`pipeline/`)

- **PipelineRunner**: 리서치→분석→추출→백테스트 4단계를 순서대로 실행
- **DailyScan**: 매일 아침 검증된 전략으로 감시 종목 스캔, 시그널 발생 시 알림
- **Scheduler**: APScheduler로 일일 스캔(평일 08:00)과 리서치 갱신(일요일 22:00)을 자동 실행

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

### 스케줄 변경

```yaml
schedule:
  daily_scan:
    cron: "0 8 * * 1-5"      # 분 시 일 월 요일 (월~금 08:00)
    timezone: "Asia/Seoul"
  research_refresh:
    cron: "0 22 * * 0"        # 매주 일요일 22:00
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

parameters:
  position_size: 1.0
  stop_loss: null
  take_profit: null
```

### LLM 모델 변경

```yaml
llm:
  fast_model: "anthropic/claude-3-haiku"       # 빠른 작업 (필터링, 해석)
  default_model: "anthropic/claude-3.5-sonnet"  # 일반 작업 (전략 추출)
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
| pandas-ta | 150+ 기술적 지표 계산 |
| backtesting.py | 전략 백테스트 엔진 |
| requests | OpenRouter API, Discord Webhook HTTP 호출 |
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
