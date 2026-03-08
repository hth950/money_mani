"""Add 8 sample strategies via the web API."""
import requests
import json

base = "http://127.0.0.1:8000/api/strategies"

strategies = [
    {
        "name": "RSI 과매도 반등",
        "description": "RSI 30 이하 과매도 구간 진입, 70 이상 과매수 구간 청산",
        "source": "classic_ta",
        "category": "momentum",
        "rules": {
            "entry": [{"condition": "threshold", "indicator": "RSI_14", "direction": "below", "value": 30}],
            "exit": [{"condition": "threshold", "indicator": "RSI_14", "direction": "above", "value": 70}],
        },
        "indicators": [{"type": "rsi", "period": 14, "column": "close", "output_name": "RSI_14"}],
        "parameters": {"position_size": 1.0, "stop_loss": 0.05, "take_profit": 0.15},
    },
    {
        "name": "MACD 크로스오버",
        "description": "MACD 라인이 시그널 라인을 상향 돌파시 매수, 하향 돌파시 매도",
        "source": "classic_ta",
        "category": "crossover",
        "rules": {
            "entry": [{"condition": "crossover", "indicator_a": "MACD_12_26_9", "indicator_b": "MACDs_12_26_9", "direction": "above"}],
            "exit": [{"condition": "crossover", "indicator_a": "MACD_12_26_9", "indicator_b": "MACDs_12_26_9", "direction": "below"}],
        },
        "indicators": [{"type": "macd", "fast": 12, "slow": 26, "signal": 9, "column": "close", "output_name": "MACD_12_26_9"}],
        "parameters": {"position_size": 1.0, "stop_loss": 0.07},
    },
    {
        "name": "볼린저 밴드 수축 돌파",
        "description": "볼린저 밴드 폭이 좁아진 후 상단 돌파시 매수, 중심선 이탈시 매도",
        "source": "classic_ta",
        "category": "volatility",
        "rules": {
            "entry": [{"condition": "threshold", "indicator": "BBU_20_2.0", "direction": "above", "value": 0}],
            "exit": [{"condition": "threshold", "indicator": "BBM_20_2.0", "direction": "below", "value": 0}],
        },
        "indicators": [{"type": "bbands", "period": 20, "std": 2.0, "column": "close", "output_name": "BB_20"}],
        "parameters": {"position_size": 0.8, "stop_loss": 0.05},
    },
    {
        "name": "삼중 EMA 추세",
        "description": "EMA 5/20/60 정배열시 매수, 역배열시 매도. 단기-중기-장기 이동평균 정렬 전략",
        "source": "trend_following",
        "category": "crossover",
        "rules": {
            "entry": [
                {"condition": "crossover", "indicator_a": "EMA_5", "indicator_b": "EMA_20", "direction": "above"},
                {"condition": "threshold_compare", "indicator_a": "EMA_20", "indicator_b": "EMA_60", "direction": "above"},
            ],
            "exit": [{"condition": "crossover", "indicator_a": "EMA_5", "indicator_b": "EMA_20", "direction": "below"}],
        },
        "indicators": [
            {"type": "ema", "period": 5, "column": "close", "output_name": "EMA_5"},
            {"type": "ema", "period": 20, "column": "close", "output_name": "EMA_20"},
            {"type": "ema", "period": 60, "column": "close", "output_name": "EMA_60"},
        ],
        "parameters": {"position_size": 1.0, "stop_loss": 0.06},
    },
    {
        "name": "Stochastic RSI 반전",
        "description": "Stochastic RSI K가 D를 상향 돌파 + 과매도 구간에서 매수",
        "source": "classic_ta",
        "category": "momentum",
        "rules": {
            "entry": [
                {"condition": "threshold", "indicator": "STOCHRSIk_14_14_3_3", "direction": "below", "value": 20},
                {"condition": "crossover", "indicator_a": "STOCHRSIk_14_14_3_3", "indicator_b": "STOCHRSId_14_14_3_3", "direction": "above"},
            ],
            "exit": [{"condition": "threshold", "indicator": "STOCHRSIk_14_14_3_3", "direction": "above", "value": 80}],
        },
        "indicators": [{"type": "stochrsi", "period": 14, "smooth_k": 3, "smooth_d": 3, "column": "close", "output_name": "STOCHRSI_14"}],
        "parameters": {"position_size": 0.7, "stop_loss": 0.05, "take_profit": 0.12},
    },
    {
        "name": "거래량 급증 돌파",
        "description": "20일 평균 거래량 대비 2배 이상 급증 + 가격 상승시 매수",
        "source": "volume_analysis",
        "category": "volume",
        "rules": {
            "entry": [
                {"condition": "threshold", "indicator": "VOL_RATIO", "direction": "above", "value": 2.0},
                {"condition": "threshold", "indicator": "ROC_1", "direction": "above", "value": 0},
            ],
            "exit": [{"condition": "threshold", "indicator": "RSI_14", "direction": "above", "value": 75}],
        },
        "indicators": [
            {"type": "sma", "period": 20, "column": "volume", "output_name": "VOL_SMA_20"},
            {"type": "rsi", "period": 14, "column": "close", "output_name": "RSI_14"},
            {"type": "roc", "period": 1, "column": "close", "output_name": "ROC_1"},
        ],
        "parameters": {"position_size": 0.5, "stop_loss": 0.04, "take_profit": 0.10},
    },
    {
        "name": "ADX 강한 추세 추종",
        "description": "ADX 25 이상 강한 추세 + DI+ > DI-면 매수, ADX 하락시 청산",
        "source": "trend_following",
        "category": "trend",
        "rules": {
            "entry": [
                {"condition": "threshold", "indicator": "ADX_14", "direction": "above", "value": 25},
                {"condition": "threshold_compare", "indicator_a": "DMP_14", "indicator_b": "DMN_14", "direction": "above"},
            ],
            "exit": [{"condition": "threshold", "indicator": "ADX_14", "direction": "below", "value": 20}],
        },
        "indicators": [{"type": "adx", "period": 14, "column": "close", "output_name": "ADX_14"}],
        "parameters": {"position_size": 1.0, "stop_loss": 0.08},
    },
    {
        "name": "MACD + RSI 복합",
        "description": "MACD 히스토그램 양전환 + RSI 50 이상 동시 충족시 매수. 보수적 복합 전략",
        "source": "composite",
        "category": "composite",
        "rules": {
            "entry": [
                {"condition": "threshold", "indicator": "MACDh_12_26_9", "direction": "above", "value": 0},
                {"condition": "threshold", "indicator": "RSI_14", "direction": "above", "value": 50},
            ],
            "exit": [
                {"condition": "threshold", "indicator": "MACDh_12_26_9", "direction": "below", "value": 0},
            ],
        },
        "indicators": [
            {"type": "macd", "fast": 12, "slow": 26, "signal": 9, "column": "close", "output_name": "MACD_12_26_9"},
            {"type": "rsi", "period": 14, "column": "close", "output_name": "RSI_14"},
        ],
        "parameters": {"position_size": 0.8, "stop_loss": 0.06, "take_profit": 0.12},
    },
]

for s in strategies:
    r = requests.post(base, json=s)
    if r.status_code in (200, 201):
        data = r.json()
        print(f"OK: {data['name']} (id={data['id']})")
    else:
        print(f"FAIL: {s['name']} -> {r.status_code} {r.text}")
