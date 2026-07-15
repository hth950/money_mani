"""Verify and install a transferred cutover directory on Hermes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from deploy.hermes.restore_sqlite import restore_database
except ModuleNotFoundError:  # Allow direct execution from deploy/hermes/.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from deploy.hermes.restore_sqlite import restore_database


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_bundle(bundle: Path) -> dict:
    manifest_path = bundle / "cutover-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_files = set(manifest["files"])
    actual_files = {
        str(path.relative_to(bundle))
        for path in bundle.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual_files != expected_files:
        raise RuntimeError(
            "Cutover file set differs from manifest: "
            f"missing={sorted(expected_files - actual_files)}, "
            f"unexpected={sorted(actual_files - expected_files)}"
        )
    for relative_path, expected_hash in manifest["files"].items():
        actual_hash = _hash_file(bundle / relative_path)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Cutover checksum mismatch for {relative_path}"
            )
    return manifest


def _replace_tree(source: Path, target: Path, safety_directory: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.install.", dir=target.parent)
    )
    try:
        if source.exists():
            shutil.copytree(source, temporary, dirs_exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if target.exists():
            safety_directory.mkdir(parents=True, exist_ok=True)
            os.replace(target, safety_directory / f"{target.name}.{timestamp}")
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _replace_file(source: Path, target: Path, mode: int = 0o600) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        temporary.chmod(mode)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def remove_cutover_bundle(bundle: Path) -> None:
    """Best-effort overwrite secret files, then remove a verified bundle."""
    bundle = bundle.resolve()
    manifest_path = bundle / "cutover-manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(
            f"Refusing to remove a directory without a cutover manifest: {bundle}"
        )
    for relative_path in (
        Path("secrets/app.env"),
        Path("oauth/openai_oauth_token.json"),
    ):
        sensitive = bundle / relative_path
        if not sensitive.is_file() or sensitive.is_symlink():
            continue
        size = sensitive.stat().st_size
        with sensitive.open("r+b", buffering=0) as handle:
            remaining = size
            zeroes = b"\0" * min(1024 * 1024, max(1, size))
            while remaining:
                chunk_size = min(remaining, len(zeroes))
                handle.write(zeroes[:chunk_size])
                remaining -= chunk_size
            handle.flush()
            os.fsync(handle.fileno())
    shutil.rmtree(bundle)


def install_cutover(
    bundle: Path,
    state_directory: Path,
    secrets_directory: Path,
    *,
    services_stopped: bool,
    remove_bundle: bool = True,
) -> dict:
    if not services_stopped:
        raise RuntimeError(
            "Refusing cutover install without explicit services-stopped confirmation"
        )
    bundle = bundle.resolve()
    state_directory = state_directory.resolve()
    secrets_directory = secrets_directory.resolve()
    for target in (state_directory, secrets_directory):
        if bundle == target or bundle in target.parents or target in bundle.parents:
            raise RuntimeError(
                "Cutover bundle and installation targets must not overlap"
            )
    manifest = _verify_bundle(bundle)

    state_directory.mkdir(parents=True, exist_ok=True)
    state_directory.chmod(0o700)
    secrets_directory.mkdir(parents=True, exist_ok=True)
    secrets_directory.chmod(0o700)
    safety_directory = state_directory / "restore-safety"

    database_info = manifest["database"]
    database_source = bundle / database_info["relative_path"]
    restore_result = restore_database(
        database_source,
        state_directory / "data" / "money_mani.db",
        safety_directory=safety_directory,
        expected_sha256=database_info["sha256"],
        services_stopped=True,
    )

    _replace_tree(bundle / "config", state_directory / "config", safety_directory)
    _replace_tree(bundle / "output", state_directory / "output", safety_directory)
    _replace_file(bundle / "MEMORY.md", state_directory / "MEMORY.md", 0o600)
    _replace_file(bundle / "secrets" / "app.env", secrets_directory / "app.env", 0o600)

    home_directory = state_directory / "home"
    home_directory.mkdir(parents=True, exist_ok=True)
    home_directory.chmod(0o700)
    oauth_source = bundle / "oauth"
    if oauth_source.exists():
        _replace_tree(
            oauth_source,
            home_directory / ".money_mani",
            safety_directory,
        )
    else:
        (home_directory / ".money_mani").mkdir(parents=True, exist_ok=True)
    (home_directory / ".money_mani").chmod(0o700)

    for directory in (
        state_directory / "data",
        state_directory / "config",
        state_directory / "output",
        state_directory / "home",
        state_directory / "home" / ".money_mani",
        state_directory / "backups",
        state_directory / "deployments",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    installed_manifest = state_directory / "cutover-installed.json"
    installed_payload = {
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "source_git_sha": manifest.get("source_git_sha"),
        "database_sha256": restore_result["sha256"],
        "bundle": str(bundle),
    }
    installed_manifest.write_text(
        json.dumps(installed_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    installed_manifest.chmod(0o600)
    result = {
        **installed_payload,
        "restore": restore_result,
        "bundle_removed": remove_bundle,
    }
    if remove_bundle:
        remove_cutover_bundle(bundle)
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and install a Money Mani cutover directory."
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--state-directory",
        type=Path,
        default=Path("/srv/money-mani/shared"),
    )
    parser.add_argument(
        "--secrets-directory",
        type=Path,
        default=Path("/srv/money-mani/secrets"),
    )
    parser.add_argument(
        "--confirm-services-stopped",
        action="store_true",
        help="Required assertion that web and scheduler writers are stopped.",
    )
    parser.add_argument(
        "--keep-bundle",
        action="store_true",
        help="Keep the transferred bundle after a successful verified install.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = install_cutover(
        args.bundle,
        args.state_directory,
        args.secrets_directory,
        services_stopped=args.confirm_services_stopped,
        remove_bundle=not args.keep_bundle,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
