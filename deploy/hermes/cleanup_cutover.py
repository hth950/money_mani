"""Remove a local cutover bundle after remote verification."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from deploy.hermes.install_cutover import remove_cutover_bundle
except ModuleNotFoundError:  # Allow direct execution from deploy/hermes/.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from deploy.hermes.install_cutover import remove_cutover_bundle


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Best-effort wipe secrets and remove a cutover directory."
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--confirm-remote-verified", action="store_true")
    args = parser.parse_args()
    if not args.confirm_remote_verified:
        raise SystemExit(
            "Refusing cleanup without --confirm-remote-verified"
        )
    remove_cutover_bundle(args.bundle)
    print(f"Removed cutover bundle: {args.bundle}")


if __name__ == "__main__":
    main()
