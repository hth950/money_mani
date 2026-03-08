"""Add 20 more strategies to reach ~30 total."""
import requests

base = "http://127.0.0.1:8000/api/strategies"

strategies = [
    {
        "name": "데드크로스 공매도",
        "description": "MA20이 MA60을 하향 돌파시 매도 신호, 상향 복귀시 청산",
        "source": "classic_ta",
        "category": "crossover",
        "rules": {
            "entry": [{"condition": "crossover", "indicator_a": "SMA_20", "indicator_b": "SMA_60", "direction": "below"}],
            "exit": [{"condition": "crossover", "indicator_a": "SMA_20", "indicator_b": "SMA_60", "direction": "above"}],
        },
        "indicators": [
            {"type": "sma", "period": 20, "column": "close", "output_name": "SMA_20"},
            {"type": "sma", "period": 60, "column": "close", "output_name": "SMA_60"},
        ],
        "parameters": {"position_size": 1.0, "stop_loss": 0.06},
    },
    {
        "name": "Williams %R 반전",
        "description": "Williams %R -80 이하 과매도 진입, -20 이상 과매수 청산",
        "source": "classic_ta",
        "category": "momentum",
        "rules": {
            "entry": [{"condition": "threshold", "indicator": "WILLR_14", "direction": "below", "value": -80}],
            "exit": [{"condition": "threshold", "indicator": "WILLR_14", "direction": "above", "value": -20}],
        },
        "indicators": [{"type": "willr", "period": 14, "column": "close", "output_name": "WILLR_14"}],
        "parameters": {"position_size": 0.8, "stop_loss": 0.05, "take_profit": 0.10},
    },
    {
        "name": "CCI 과매도 매수",
        "description": "CCI -100 이하 진입, +100 이상 청산. 상품채널지수 역추세 전략",
        "source": "classic_ta",
        "category": "momentum",
        "rules": {
            "entry": [{"condition": "threshold", "indicator": "CCI_20", "direction": "below", "value": -100}],
            "exit": [{"condition": "threshold", "indicator": "CCI_20", "direction": "above", "value": 100}],
        },
        "indicators": [{"type": "cci", "period": 20, "column": "close", "output_name": "CCI_20"}],
        "parameters": {"position_size": 0.7, "stop_loss": 0.06},
    },
    {
        "name": "이중 이동평균 + RSI 필터",
        "description": "MA5 > MA20 골든크로스 + RSI 40~70 구간 필터로 과매수 진입 방지",
        "source": "composite",
        "category": "composite",
        "rules": {
            "entry": [
                {"condition": "crossover", "indicator_a": "SMA_5", "indicator_b": "SMA_20", "direction": "above"},
                {"condition": "threshold", "indicator": "RSI_14", "direction": "above", "value": 40},
                {"condition": "threshold", "indicator": "RSI_14", "direction": "below", "value": 70},
            ],
            "exit": [{"condition": "crossover", "indicator_a": "SMA_5", "indicator_b": "SMA_20", "direction": "below"}],
        },
        "indicators": [
            {"type": "sma", "period": 5, "column": "close", "output_name": "SMA_5"},
            {"type": "sma", "period": 20, "column": "close", "output_name": "SMA_20"},
            {"type": "rsi", "period": 14, "column": "close", "output_name": "RSI_14"},
        ],
        "parameters": {"position_size": 1.0, "stop_loss": 0.05},
    },
    {
        "name": "Parabolic SAR 추세",
        "description": "Parabolic SAR이 가격 아래로 전환시 매수, 위로 전환시 매도",
        "source": "classic_ta",
        "category": "trend",
        "rules": {
            "entry": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "PSAR", "direction": "above"}],
            "exit": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "PSAR", "direction": "below"}],
        },
        "indicators": [{"type": "psar", "af": 0.02, "max_af": 0.2, "output_name": "PSAR"}],
        "parameters": {"position_size": 1.0, "stop_loss": 0.07},
    },
    {
        "name": "OBV 다이버전스",
        "description": "OBV 상승 추세 + 가격 횡보시 매수 (숨겨진 매집 탐지)",
        "source": "volume_analysis",
        "category": "volume",
        "rules": {
            "entry": [
                {"condition": "threshold", "indicator": "OBV_SLOPE", "direction": "above", "value": 0},
                {"condition": "threshold", "indicator": "ROC_5", "direction": "below", "value": 2},
            ],
            "exit": [{"condition": "threshold", "indicator": "OBV_SLOPE", "direction": "below", "value": -0.5}],
        },
        "indicators": [
            {"type": "obv", "column": "close", "output_name": "OBV"},
            {"type": "roc", "period": 5, "column": "close", "output_name": "ROC_5"},
        ],
        "parameters": {"position_size": 0.6, "stop_loss": 0.05},
    },
    {
        "name": "Ichimoku 구름 돌파",
        "description": "가격이 구름(Kumo) 상단 돌파 + 전환선 > 기준선시 매수",
        "source": "japanese_ta",
        "category": "trend",
        "rules": {
            "entry": [
                {"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "ISA_9", "direction": "above"},
                {"condition": "threshold_compare", "indicator_a": "ITS_9", "indicator_b": "IKS_26", "direction": "above"},
            ],
            "exit": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "IKS_26", "direction": "below"}],
        },
        "indicators": [{"type": "ichimoku", "tenkan": 9, "kijun": 26, "senkou": 52, "output_name": "ICHIMOKU"}],
        "parameters": {"position_size": 1.0, "stop_loss": 0.08},
    },
    {
        "name": "Donchian Channel 돌파",
        "description": "20일 최고가 돌파시 매수 (터틀 트레이딩), 10일 최저가 이탈시 매도",
        "source": "turtle_trading",
        "category": "breakout",
        "rules": {
            "entry": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "DCU_20", "direction": "above"}],
            "exit": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "DCL_10", "direction": "below"}],
        },
        "indicators": [
            {"type": "donchian", "period": 20, "output_name": "DC_20"},
            {"type": "donchian", "period": 10, "output_name": "DC_10"},
        ],
        "parameters": {"position_size": 0.5, "stop_loss": 0.10},
    },
    {
        "name": "ATR 변동성 돌파",
        "description": "전일 종가 + ATR*0.5 이상 돌파시 매수 (래리 윌리엄스 변형)",
        "source": "larry_williams",
        "category": "breakout",
        "rules": {
            "entry": [{"condition": "threshold", "indicator": "ATR_BREAKOUT", "direction": "above", "value": 0}],
            "exit": [{"condition": "threshold", "indicator": "ROC_1", "direction": "below", "value": -1}],
        },
        "indicators": [
            {"type": "atr", "period": 14, "column": "close", "output_name": "ATR_14"},
            {"type": "roc", "period": 1, "column": "close", "output_name": "ROC_1"},
        ],
        "parameters": {"position_size": 0.5, "stop_loss": 0.03, "take_profit": 0.05},
    },
    {
        "name": "VWAP 회귀",
        "description": "가격이 VWAP 아래로 2% 이상 이탈시 매수, VWAP 복귀시 청산",
        "source": "institutional",
        "category": "mean_reversion",
        "rules": {
            "entry": [{"condition": "threshold", "indicator": "VWAP_DIST", "direction": "below", "value": -2.0}],
            "exit": [{"condition": "threshold", "indicator": "VWAP_DIST", "direction": "above", "value": 0}],
        },
        "indicators": [{"type": "vwap", "column": "close", "output_name": "VWAP"}],
        "parameters": {"position_size": 0.7, "stop_loss": 0.04, "take_profit": 0.03},
    },
    {
        "name": "52주 신고가 모멘텀",
        "description": "52주 신고가 갱신 종목 매수, 20일 이동평균 이탈시 청산",
        "source": "momentum_investing",
        "category": "momentum",
        "rules": {
            "entry": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "HIGH_252", "direction": "above"}],
            "exit": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "SMA_20", "direction": "below"}],
        },
        "indicators": [
            {"type": "highest", "period": 252, "column": "high", "output_name": "HIGH_252"},
            {"type": "sma", "period": 20, "column": "close", "output_name": "SMA_20"},
        ],
        "parameters": {"position_size": 0.8, "stop_loss": 0.08},
    },
    {
        "name": "MFI 자금흐름 반전",
        "description": "MFI 20 이하 자금 유출 과다시 매수, 80 이상 자금 유입 과다시 매도",
        "source": "classic_ta",
        "category": "volume",
        "rules": {
            "entry": [{"condition": "threshold", "indicator": "MFI_14", "direction": "below", "value": 20}],
            "exit": [{"condition": "threshold", "indicator": "MFI_14", "direction": "above", "value": 80}],
        },
        "indicators": [{"type": "mfi", "period": 14, "output_name": "MFI_14"}],
        "parameters": {"position_size": 0.7, "stop_loss": 0.05, "take_profit": 0.12},
    },
    {
        "name": "Keltner Channel 돌파",
        "description": "Keltner 상단 돌파시 추세 진입, 중심선 이탈시 청산",
        "source": "classic_ta",
        "category": "volatility",
        "rules": {
            "entry": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "KCU_20", "direction": "above"}],
            "exit": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "KCM_20", "direction": "below"}],
        },
        "indicators": [{"type": "kc", "period": 20, "scalar": 1.5, "output_name": "KC_20"}],
        "parameters": {"position_size": 0.8, "stop_loss": 0.06},
    },
    {
        "name": "EMA 5/20 + MACD 히스토그램",
        "description": "EMA 5>20 + MACD 히스토그램 양전환 복합 조건 매수",
        "source": "composite",
        "category": "composite",
        "rules": {
            "entry": [
                {"condition": "threshold_compare", "indicator_a": "EMA_5", "indicator_b": "EMA_20", "direction": "above"},
                {"condition": "threshold", "indicator": "MACDh_12_26_9", "direction": "above", "value": 0},
            ],
            "exit": [{"condition": "threshold_compare", "indicator_a": "EMA_5", "indicator_b": "EMA_20", "direction": "below"}],
        },
        "indicators": [
            {"type": "ema", "period": 5, "column": "close", "output_name": "EMA_5"},
            {"type": "ema", "period": 20, "column": "close", "output_name": "EMA_20"},
            {"type": "macd", "fast": 12, "slow": 26, "signal": 9, "column": "close", "output_name": "MACD_12_26_9"},
        ],
        "parameters": {"position_size": 1.0, "stop_loss": 0.05},
    },
    {
        "name": "RSI + 볼린저 밴드 하단",
        "description": "RSI 35 이하 + 볼린저 하단 터치시 매수 (이중 과매도 확인)",
        "source": "composite",
        "category": "mean_reversion",
        "rules": {
            "entry": [
                {"condition": "threshold", "indicator": "RSI_14", "direction": "below", "value": 35},
                {"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "BBL_20_2.0", "direction": "below"},
            ],
            "exit": [{"condition": "threshold", "indicator": "RSI_14", "direction": "above", "value": 60}],
        },
        "indicators": [
            {"type": "rsi", "period": 14, "column": "close", "output_name": "RSI_14"},
            {"type": "bbands", "period": 20, "std": 2.0, "column": "close", "output_name": "BB_20"},
        ],
        "parameters": {"position_size": 0.8, "stop_loss": 0.04, "take_profit": 0.08},
    },
    {
        "name": "200일 이동평균 지지",
        "description": "200일 MA 위에서만 매수, 가격이 200일 MA 아래로 이탈시 전량 청산",
        "source": "long_term",
        "category": "trend",
        "rules": {
            "entry": [
                {"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "SMA_200", "direction": "above"},
                {"condition": "crossover", "indicator_a": "SMA_10", "indicator_b": "SMA_50", "direction": "above"},
            ],
            "exit": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "SMA_200", "direction": "below"}],
        },
        "indicators": [
            {"type": "sma", "period": 200, "column": "close", "output_name": "SMA_200"},
            {"type": "sma", "period": 50, "column": "close", "output_name": "SMA_50"},
            {"type": "sma", "period": 10, "column": "close", "output_name": "SMA_10"},
        ],
        "parameters": {"position_size": 1.0, "stop_loss": 0.10},
    },
    {
        "name": "스윙 트레이딩 (RSI+MACD+ADX)",
        "description": "ADX>20 추세 확인 + RSI 30~50 + MACD 상향 전환시 스윙 매수",
        "source": "swing_trading",
        "category": "composite",
        "rules": {
            "entry": [
                {"condition": "threshold", "indicator": "ADX_14", "direction": "above", "value": 20},
                {"condition": "threshold", "indicator": "RSI_14", "direction": "above", "value": 30},
                {"condition": "threshold", "indicator": "RSI_14", "direction": "below", "value": 50},
                {"condition": "threshold", "indicator": "MACDh_12_26_9", "direction": "above", "value": 0},
            ],
            "exit": [{"condition": "threshold", "indicator": "RSI_14", "direction": "above", "value": 70}],
        },
        "indicators": [
            {"type": "adx", "period": 14, "column": "close", "output_name": "ADX_14"},
            {"type": "rsi", "period": 14, "column": "close", "output_name": "RSI_14"},
            {"type": "macd", "fast": 12, "slow": 26, "signal": 9, "column": "close", "output_name": "MACD_12_26_9"},
        ],
        "parameters": {"position_size": 0.8, "stop_loss": 0.05, "take_profit": 0.15},
    },
    {
        "name": "갭 상승 매수",
        "description": "전일 대비 2% 이상 갭 상승 시작시 모멘텀 매수, 당일 음봉 전환시 청산",
        "source": "day_trading",
        "category": "breakout",
        "rules": {
            "entry": [{"condition": "threshold", "indicator": "GAP_PCT", "direction": "above", "value": 2.0}],
            "exit": [{"condition": "threshold", "indicator": "ROC_1", "direction": "below", "value": -1.0}],
        },
        "indicators": [
            {"type": "roc", "period": 1, "column": "open", "output_name": "GAP_PCT"},
            {"type": "roc", "period": 1, "column": "close", "output_name": "ROC_1"},
        ],
        "parameters": {"position_size": 0.3, "stop_loss": 0.03, "take_profit": 0.05},
    },
    {
        "name": "MA 수렴 후 확산",
        "description": "MA5/10/20이 수렴(밴드 좁아짐) 후 상방 확산시 매수",
        "source": "classic_ta",
        "category": "volatility",
        "rules": {
            "entry": [
                {"condition": "threshold", "indicator": "MA_SPREAD", "direction": "above", "value": 0},
                {"condition": "threshold_compare", "indicator_a": "SMA_5", "indicator_b": "SMA_10", "direction": "above"},
                {"condition": "threshold_compare", "indicator_a": "SMA_10", "indicator_b": "SMA_20", "direction": "above"},
            ],
            "exit": [{"condition": "crossover", "indicator_a": "SMA_5", "indicator_b": "SMA_10", "direction": "below"}],
        },
        "indicators": [
            {"type": "sma", "period": 5, "column": "close", "output_name": "SMA_5"},
            {"type": "sma", "period": 10, "column": "close", "output_name": "SMA_10"},
            {"type": "sma", "period": 20, "column": "close", "output_name": "SMA_20"},
        ],
        "parameters": {"position_size": 0.8, "stop_loss": 0.04},
    },
    {
        "name": "듀얼 모멘텀 (절대+상대)",
        "description": "12개월 수익률 양수(절대 모멘텀) + 시장 대비 우위(상대 모멘텀) 충족시 매수",
        "source": "gary_antonacci",
        "category": "momentum",
        "rules": {
            "entry": [
                {"condition": "threshold", "indicator": "ROC_252", "direction": "above", "value": 0},
                {"condition": "threshold", "indicator": "RSI_14", "direction": "above", "value": 50},
            ],
            "exit": [{"condition": "threshold", "indicator": "ROC_252", "direction": "below", "value": 0}],
        },
        "indicators": [
            {"type": "roc", "period": 252, "column": "close", "output_name": "ROC_252"},
            {"type": "rsi", "period": 14, "column": "close", "output_name": "RSI_14"},
        ],
        "parameters": {"position_size": 1.0, "stop_loss": 0.12},
    },
]

ok = 0
for s in strategies:
    r = requests.post(base, json=s)
    if r.status_code in (200, 201):
        data = r.json()
        print(f"OK: [{data['id']:>2}] {data['name']}")
        ok += 1
    else:
        print(f"FAIL: {s['name']} -> {r.status_code} {r.text[:100]}")

print(f"\n{ok}/{len(strategies)} strategies added successfully")
