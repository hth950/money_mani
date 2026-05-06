"""Add allowed_signal_types: [BUY] to strategies whose SELL signals underperform.

Identifies strategies with BUY avg_ret > 0 AND SELL avg_ret < -1.5% (n>=20)
from the latest forward-eval JSON, and updates their YAML files.
"""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from web.db.connection import get_db


def latest_eval_json() -> Path:
    candidates = sorted(Path("output").glob("strategy_forward_eval_*.json"))
    if not candidates:
        raise RuntimeError("No strategy_forward_eval_*.json found in output/")
    return candidates[-1]


def find_buy_only_targets(eval_data: dict) -> list[str]:
    """Strategies where BUY works but SELL is bad."""
    by_strategy = {}
    for s in eval_data["stats"]:
        by_strategy.setdefault(s["strategy_name"], {})[s["signal_type"]] = s
    targets = []
    for name, sigs in by_strategy.items():
        buy = sigs.get("BUY")
        sell = sigs.get("SELL")
        if not buy or not sell:
            continue
        if (buy.get("count", 0) >= 20 and sell.get("count", 0) >= 20
                and (buy.get("avg_ret_10d") or 0) > 0
                and (sell.get("avg_ret_10d") or 0) < -1.5):
            targets.append(name)
    return targets


def filter_active(targets: list[str]) -> list[str]:
    """Keep only currently-validated strategies."""
    with get_db() as db:
        rows = db.execute(
            "SELECT name FROM strategies WHERE status IN ('validated','validated_v2')"
        ).fetchall()
    active = {r["name"] for r in rows}
    return [t for t in targets if t in active]


def update_yaml(name: str, strategies_dir: Path) -> bool:
    """Find YAML by exact name match and add allowed_signal_types."""
    for yml in strategies_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(yml.read_text())
        except Exception as e:
            print(f"  yaml load failed for {yml.name}: {e}")
            continue
        if not isinstance(data, dict) or data.get("name") != name:
            continue
        if data.get("allowed_signal_types") == ["BUY"]:
            print(f"  [skip already-set] {yml.name}")
            return True
        data["allowed_signal_types"] = ["BUY"]
        yml.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False))
        print(f"  [updated] {yml.name}")
        return True
    print(f"  [NOT FOUND] {name}")
    return False


def update_db(names: list[str]):
    """Persist allowed_signal_types into strategies.parameters_json (informational)."""
    import json as _json
    with get_db() as db:
        for name in names:
            row = db.execute(
                "SELECT parameters_json FROM strategies WHERE name=?", (name,)
            ).fetchone()
            if not row:
                continue
            try:
                params = _json.loads(row["parameters_json"] or "{}")
            except Exception:
                params = {}
            params["allowed_signal_types"] = ["BUY"]
            db.execute(
                "UPDATE strategies SET parameters_json=?, updated_at=datetime('now') WHERE name=?",
                (_json.dumps(params, ensure_ascii=False), name),
            )


def main():
    eval_path = latest_eval_json()
    print(f"Reading: {eval_path}")
    eval_data = json.loads(eval_path.read_text())

    raw_targets = find_buy_only_targets(eval_data)
    targets = filter_active(raw_targets)
    print(f"\nTargeting {len(targets)} active strategies for BUY-only:")
    for t in targets:
        print(f"  - {t}")

    strat_dir = Path("config/strategies")
    print(f"\nUpdating YAML files in {strat_dir}...")
    updated = []
    for name in targets:
        if update_yaml(name, strat_dir):
            updated.append(name)

    print(f"\nUpdating DB strategies.parameters_json...")
    update_db(updated)

    print(f"\nDone. {len(updated)}/{len(targets)} strategies updated.")


if __name__ == "__main__":
    main()
