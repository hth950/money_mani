"""Validate all strategies and run backtests via web API."""
import requests
import time

base = "http://127.0.0.1:8000"

# 1. Get all strategies
strategies = requests.get(f"{base}/api/strategies").json()
print(f"Total strategies: {len(strategies)}\n")

# 2. Validate all strategies
print("=== Validating all strategies ===")
for s in strategies:
    if s["status"] != "validated":
        r = requests.put(f"{base}/api/strategies/{s['id']}", json={"status": "validated"})
        if r.status_code == 200:
            print(f"  Validated: [{s['id']:>2}] {s['name']}")
        else:
            print(f"  FAIL: [{s['id']:>2}] {s['name']} -> {r.status_code}")
    else:
        print(f"  Already OK: [{s['id']:>2}] {s['name']}")

# 3. Run backtests for all strategies
# Korean tickers: Samsung, SK Hynix, NAVER
krx_tickers = ["005930", "000660", "035420"]

print(f"\n=== Running backtests (KRX: {krx_tickers}) ===")
job_ids = []
for s in strategies:
    r = requests.post(f"{base}/api/backtest/run", json={
        "strategy_id": s["id"],
        "tickers": krx_tickers,
        "market": "KRX",
    })
    if r.status_code == 200:
        data = r.json()
        job_ids.append(data["job_id"])
        print(f"  Started: [{s['id']:>2}] {s['name']} -> job #{data['job_id']}")
    else:
        print(f"  FAIL: [{s['id']:>2}] {s['name']} -> {r.status_code}")

print(f"\n{len(job_ids)} backtest jobs started. Monitoring progress...")

# 4. Monitor job completion
completed = 0
failed = 0
while True:
    time.sleep(5)
    done = 0
    fail = 0
    running = 0
    for jid in job_ids:
        job = requests.get(f"{base}/api/backtest/jobs/{jid}").json()
        if job["status"] == "success":
            done += 1
        elif job["status"] == "failed":
            fail += 1
        else:
            running += 1

    print(f"  Progress: {done} done, {fail} failed, {running} running")

    if running == 0:
        completed = done
        failed = fail
        break

print(f"\n=== Complete ===")
print(f"  Success: {completed}")
print(f"  Failed: {failed}")

# 5. Show results summary
results = requests.get(f"{base}/api/backtest/results").json()
print(f"\n=== Backtest Results ({len(results)} total) ===")
print(f"{'Strategy':35s} {'Ticker':8s} {'Return':>10s} {'Sharpe':>8s} {'MaxDD':>10s} {'WinRate':>8s} {'Trades':>7s} {'Valid':>6s}")
print("-" * 100)
for r in results[:50]:
    ret_str = f"{r['total_return']*100:+.2f}%"
    print(f"{r['strategy_name']:35s} {r['ticker']:8s} {ret_str:>10s} {r['sharpe_ratio']:>8.2f} {r['max_drawdown']*100:>9.2f}% {r['win_rate']*100:>7.1f}% {r['num_trades']:>7d} {'Y' if r['is_valid'] else 'N':>6s}")
