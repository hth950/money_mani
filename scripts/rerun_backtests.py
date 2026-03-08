"""Re-run backtests for all strategies and wait for results."""
import requests
import time

base = "http://127.0.0.1:8000"

# Get all strategies
strategies = requests.get(f"{base}/api/strategies").json()
print(f"Total strategies: {len(strategies)}")

# Run backtests
krx_tickers = ["005930", "000660", "035420"]
job_ids = []
for s in strategies:
    r = requests.post(f"{base}/api/backtest/run", json={
        "strategy_id": s["id"], "tickers": krx_tickers, "market": "KRX",
    })
    if r.status_code == 200:
        data = r.json()
        job_ids.append(data["job_id"])
        print(f"  Started: [{s['id']:>2}] {s['name']}")
    else:
        print(f"  FAIL: [{s['id']:>2}] {s['name']} -> {r.status_code}")

print(f"\n{len(job_ids)} jobs started. Waiting...")

# Wait for completion
for attempt in range(40):
    time.sleep(3)
    done = 0
    fail = 0
    running = 0
    for jid in job_ids:
        try:
            job = requests.get(f"{base}/api/backtest/jobs/{jid}").json()
            status = job.get("status", "unknown")
            if status == "success":
                done += 1
            elif status == "failed":
                fail += 1
            else:
                running += 1
        except:
            running += 1
    print(f"  [{attempt*3:>3}s] done={done}, failed={fail}, running={running}")
    if running == 0:
        break

# Show results
results = requests.get(f"{base}/api/backtest/results").json()
print(f"\n{'Strategy':35s} {'Ticker':8s} {'Return':>10s} {'Sharpe':>8s} {'MaxDD':>10s} {'Trades':>7s}")
print("-" * 85)
seen = set()
zero_count = 0
nonzero_count = 0
for r in results:
    key = f"{r['strategy_name']}|{r['ticker']}"
    if key in seen:
        continue
    seen.add(key)
    ret = r['total_return'] * 100
    trades = r['num_trades']
    if trades == 0:
        zero_count += 1
    else:
        nonzero_count += 1
    print(f"{r['strategy_name'][:35]:35s} {r['ticker']:8s} {ret:>+9.2f}% {r['sharpe_ratio']:>8.2f} {r['max_drawdown']*100:>9.2f}% {trades:>7d}")

print(f"\nSummary: {nonzero_count} with trades, {zero_count} with 0 trades")
