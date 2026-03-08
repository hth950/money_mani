"""Fix strategy definitions with incorrect rules."""
import requests

base = "http://127.0.0.1:8000/api/strategies"

# Fix [6] 볼린저 밴드 수축 돌파: Close > BBU (breakout), Close < BBM (exit)
r = requests.put(f"{base}/6", json={
    "rules": {
        "entry": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "BBU_20_2.0", "direction": "above"}],
        "exit": [{"condition": "threshold_compare", "indicator_a": "Close", "indicator_b": "BBM_20_2.0", "direction": "below"}],
    }
})
print(f"Fix 볼린저 밴드 수축 돌파: {r.status_code}")

# Fix [2] Low Volatility ETF: BBB values are in % (e.g. 12%), threshold 5 not 0.05
r = requests.put(f"{base}/2", json={
    "rules": {
        "entry": [{"condition": "threshold", "indicator": "BBB_20_2.0", "direction": "below", "value": 5.0}],
        "exit": [
            {"condition": "threshold", "indicator": "BBB_20_2.0", "direction": "above", "value": 15.0},
            {"condition": "threshold", "indicator": "RSI_14", "direction": "above", "value": 70},
        ],
    }
})
print(f"Fix Low Volatility ETF: {r.status_code}")
