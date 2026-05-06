"""Remove allowed_signal_types: [BUY] from all strategy YAMLs."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    strat_dir = Path("config/strategies")
    removed = 0
    for yml in strat_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(yml.read_text())
        except Exception as e:
            print(f"  yaml load failed for {yml.name}: {e}")
            continue
        if not isinstance(data, dict):
            continue
        if "allowed_signal_types" not in data:
            continue
        data.pop("allowed_signal_types")
        yml.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False))
        print(f"  [rolled back] {yml.name}")
        removed += 1
    print(f"\nDone. Removed allowed_signal_types from {removed} YAMLs.")


if __name__ == "__main__":
    main()
