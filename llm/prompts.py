"""LLM prompt templates for money_mani pipeline."""

VIDEO_FILTER_PROMPT = """You are a financial content quality evaluator.

Given a YouTube video's title and description, evaluate its quality for extracting real investment strategies.

Respond with ONLY valid JSON (no markdown, no extra text):
{{
  "quality_score": <integer 1-10>,
  "is_clickbait": <true|false>,
  "reason": "<brief explanation>"
}}

Scoring guide:
- 9-10: Concrete strategy with specific indicators, entry/exit rules, backtested results
- 7-8: Educational content with actionable techniques
- 5-6: General market commentary, some useful info
- 3-4: Mostly opinion, vague advice
- 1-2: Clickbait, get-rich-quick, misleading title

Video title: {title}
Video description: {description}
View count: {view_count}
"""

STRATEGY_REFINE_PROMPT = """You are an algorithmic trading strategy analyst.

Extract ALL distinct investment strategies from the following analysis text. Each strategy must be concrete enough to implement in code.

Respond with ONLY valid JSON array (no markdown, no extra text):
[
  {{
    "name": "<strategy name>",
    "entry_rules": [
      {{"condition": "<condition description>", "indicator": "<indicator name>", "params": {{}}}}
    ],
    "exit_rules": [
      {{"condition": "<condition description>", "indicator": "<indicator name>", "params": {{}}}}
    ],
    "indicators": [
      {{"type": "<indicator type>", "period": <int or null>, "column": "close", "output_name": "<name>"}}
    ],
    "timeframe": "<daily|weekly|hourly>",
    "risk_management": {{
      "stop_loss": <float or null>,
      "take_profit": <float or null>,
      "position_size": <float>
    }}
  }}
]

If no codeable strategies are found, return an empty array: []

Analysis text:
{raw_analysis}
"""

STRATEGY_VALIDATE_PROMPT = """You are an algorithmic trading engineer reviewing a strategy specification.

Determine if this strategy can be implemented in Python code using pandas and standard technical indicators (SMA, EMA, RSI, MACD, Bollinger Bands, etc.).

Respond with ONLY valid JSON (no markdown, no extra text):
{{
  "is_codeable": <true|false>,
  "issues": ["<issue 1>", "<issue 2>"],
  "refined_rules": {{
    "entry_rules": [],
    "exit_rules": []
  }}
}}

Strategy specification:
{strategy_dict}
"""

BACKTEST_INTERPRET_PROMPT = """당신은 퀀트 투자 전문가입니다. 다음 백테스트 결과를 분석하여 한국어로 통찰을 제공하세요.

다음 항목을 포함하여 분석하세요:
1. 전략의 강점 (수익률, 샤프 비율, 승률 등)
2. 전략의 약점 (최대 낙폭, 연속 손실 등)
3. 적합한 시장 환경 (추세장, 횡보장, 변동성 등)
4. 개선 권고사항

백테스트 결과:
{metrics}

분석 결과를 명확하고 간결하게 한국어로 작성하세요.
"""

TREND_EXTRACT_PROMPT = """당신은 한국 주식시장 트렌드 분석가입니다.

아래 YouTube 영상들의 제목과 설명을 분석하여, 현재 주식시장에서 주목받는 핫한 섹터/테마/이슈를 추출하세요.

응답은 반드시 유효한 JSON 배열만 출력하세요 (마크다운, 추가 텍스트 없이):
[
  {{
    "sector": "<섹터/테마명 (예: 반도체, AI, 2차전지, 방산, 바이오)>",
    "keywords": ["<관련 키워드1>", "<관련 키워드2>", "<관련 종목명>"],
    "confidence": <0.0-1.0 사이 확신도>,
    "reason": "<왜 이 섹터가 핫한지 간단한 이유>"
  }}
]

규칙:
- 최소 3개, 최대 7개 섹터를 추출하세요
- 여러 영상에서 반복적으로 언급되는 섹터일수록 confidence가 높습니다
- 단순 개별 종목이 아닌 섹터/테마 단위로 묶어주세요
- 투자 관련이 아닌 내용은 무시하세요

영상 목록:
{video_list}
"""

QUERY_GENERATE_PROMPT = """당신은 주식 투자 전략 검색 전문가입니다.

아래 핫 섹터/테마 목록을 보고, 각 섹터에 대해 구체적인 투자 전략을 찾을 수 있는 YouTube 검색어를 생성하세요.

응답은 반드시 유효한 JSON 배열만 출력하세요 (마크다운, 추가 텍스트 없이):
[
  "<검색어1>",
  "<검색어2>",
  ...
]

규칙:
- 각 섹터당 2개의 검색어를 생성하세요
- 검색어는 구체적인 매매 전략이나 기법을 찾을 수 있도록 작성하세요
- 예: "반도체 관련주 매매 전략", "AI 주식 기술적 분석 매매법"
- 일반적인 뉴스가 아닌 실제 매매 전략/기법 중심의 검색어를 만드세요
- 총 검색어 수는 최대 10개로 제한하세요

핫 섹터 목록:
{trends}
"""

MARKET_INTEL_PROMPT = """당신은 한국 주식시장 전문 애널리스트입니다. 아래의 최신 뉴스/검색 결과를 분석하여 한국 주식시장에 영향을 줄 수 있는 핵심 이슈를 추출하세요.

현재 시간: {current_time} KST
스캔 유형: {scan_type_label}

=== 최신 뉴스/검색 결과 ===
{search_results}
=== 끝 ===

위 뉴스를 분석하여 다음을 추출하세요:
1. 정책/규제 변화 (금리, 세금, 산업 정책)
2. 기업 실적/공시 (어닝 서프라이즈, 대규모 계약)
3. 글로벌 이슈 (미국 시장, 중국, 환율, 원자재)
4. 섹터/테마 모멘텀 (AI, 반도체, 2차전지, 방산, 바이오 등)
5. 수급 동향 (외국인/기관 매매, 공매도)

응답은 반드시 유효한 JSON 배열만 출력하세요 (마크다운, 추가 텍스트 없이):
[
  {{
    "title": "<이슈 제목>",
    "summary": "<이슈 설명 (2-3문장)>",
    "category": "<policy|earnings|sector|global|event|supply_demand>",
    "sentiment": "<positive|negative|neutral|mixed>",
    "confidence": <0.0-1.0>,
    "affected_tickers": [
      {{
        "ticker": "<KRX 종목코드 6자리 (예: 005930)>",
        "name": "<종목명>",
        "direction": "<up|down>",
        "reason": "<이 종목이 영향받는 이유>"
      }}
    ],
    "source_info": "<참고한 뉴스 출처 요약>"
  }}
]

규칙:
- 최소 3개, 최대 8개 이슈를 분석하세요
- 각 이슈에 최소 1개, 최대 5개 관련 종목을 지정하세요
- 종목코드는 반드시 KRX 6자리 코드를 사용하세요 (예: 삼성전자=005930, SK하이닉스=000660, NAVER=035420, 카카오=035720, LG화학=051910, 현대차=005380, 기아=000270, 삼성SDI=006400, LG에너지솔루션=373220, 포스코홀딩스=005490, 셀트리온=068270, KB금융=105560, 삼성바이오로직스=207940, 한화에어로스페이스=012450)
- 모르는 종목코드는 종목명만 적고 코드는 빈 문자열로 두세요
- 실제 뉴스에 기반한 이슈만 포함하세요 (추측 금지)
"""
