"""Check which strategies still have 0 trades."""
import requests
from collections import defaultdict

base = "http://127.0.0.1:8000"
results = requests.get(f"{base}/api/backtest/results").json()

# Keep only latest per strategy|ticker
latest = {}
for r in results:
    key = f"{r['strategy_name']}|{r['ticker']}"
    if key not in latest:
        latest[key] = r

strat_trades = defaultdict(list)
for key, r in latest.items():
    strat_trades[r['strategy_name']].append((r['ticker'], r['num_trades'], r['total_return']))

print("=== Strategies with 0 trades on ALL tickers ===")
for sname, entries in sorted(strat_trades.items()):
    all_zero = all(t[1] == 0 and t[2] == 0.0 for t in entries)
    if all_zero:
        print(f"  {sname}: {[(t[0], t[1]) for t in entries]}")

print()
print("=== Strategies with 0 trades but non-zero return (always in position) ===")
for sname, entries in sorted(strat_trades.items()):
    has_weird = any(t[1] == 0 and t[2] != 0.0 for t in entries)
    if has_weird:
        print(f"  {sname}: {[(t[0], t[1], f'{t[2]*100:+.1f}%') for t in entries]}")
