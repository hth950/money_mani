"""Prepare a verified local cutover directory for secure rsync to Hermes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from deploy.hermes.sqlite_backup import create_backup
except ModuleNotFoundError:  # Allow direct execution from deploy/hermes/.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from deploy.hermes.sqlite_backup import create_backup


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_port_closed(port: int) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            pass
    except OSError:
        return
    raise RuntimeError(
        f"Local port {port} is still accepting connections. Stop the web "
        "service before preparing the final database cutover."
    )


def _assert_scheduler_stopped() -> None:
    """Refuse a snapshot while a host or Compose scheduler is still alive."""
    pgrep = shutil.which("pgrep")
    if pgrep:
        patterns = (
            "pipeline.scheduler",
            "pipeline/scheduler",
            "start_scheduler",
            "main.py schedule",
        )
        matched_pids: set[str] = set()
        for pattern in patterns:
            result = subprocess.run(
                [pgrep, "-f", pattern],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                matched_pids.update(result.stdout.split())
            elif result.returncode not in {1}:
                raise RuntimeError(
                    f"Unable to verify scheduler state with pgrep ({pattern})"
                )
        if matched_pids:
            raise RuntimeError(
                "A local scheduler process is still running "
                f"(pid={','.join(sorted(matched_pids))}). Stop it before cutover."
            )

    docker = shutil.which("docker")
    if docker:
        result = subprocess.run(
            [
                docker,
                "ps",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=money-mani",
                "--filter",
                "label=com.docker.compose.service=scheduler",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        # An unavailable local daemon is not evidence of a running scheduler;
        # a successful query with any container ID is.
        if result.returncode == 0 and result.stdout.strip():
            raise RuntimeError(
                "The local Money Mani scheduler container is still running. "
                "Stop it before cutover."
            )


def _git_sha(project_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        destination.mkdir(parents=True, exist_ok=True)
        return
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def prepare_cutover(
    project_root: Path,
    destination: Path,
    *,
    services_stopped: bool,
    web_port: int = 31234,
    oauth_token: Path | None = None,
) -> dict:
    if not services_stopped:
        raise RuntimeError(
            "Refusing cutover without explicit services-stopped confirmation"
        )
    _assert_port_closed(web_port)
    _assert_scheduler_stopped()

    project_root = project_root.resolve()
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(f"Cutover destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)

    db_path = project_root / "data" / "money_mani.db"
    data_directory = destination / "data"
    results = create_backup(
        db_path,
        data_directory,
        kind="cutover",
        checkpoint=True,
    )
    database_result = results[0]

    _copy_tree(project_root / "config", destination / "config")
    _copy_tree(project_root / "output", destination / "output")

    memory_source = project_root / "MEMORY.md"
    memory_target = destination / "MEMORY.md"
    if memory_source.exists():
        shutil.copy2(memory_source, memory_target)
    else:
        memory_target.touch()

    env_source = project_root / ".env"
    if not env_source.is_file():
        raise FileNotFoundError(
            f"Required environment file not found: {env_source}"
        )
    secrets_directory = destination / "secrets"
    secrets_directory.mkdir(mode=0o700)
    env_target = secrets_directory / "app.env"
    shutil.copy2(env_source, env_target)
    env_target.chmod(0o600)

    if oauth_token and oauth_token.expanduser().is_file():
        oauth_directory = destination / "oauth"
        oauth_directory.mkdir(mode=0o700)
        oauth_target = oauth_directory / "openai_oauth_token.json"
        shutil.copy2(oauth_token.expanduser(), oauth_target)
        oauth_target.chmod(0o600)

    manifest_path = destination / "cutover-manifest.json"
    file_hashes: dict[str, str] = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path != manifest_path:
            file_hashes[str(path.relative_to(destination))] = _hash_file(path)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_git_sha": _git_sha(project_root),
        "database": {
            "relative_path": str(
                Path(database_result.path).relative_to(destination)
            ),
            "sha256": database_result.sha256,
            "manifest_relative_path": str(
                Path(database_result.manifest_path).relative_to(destination)
            ),
        },
        "files": file_hashes,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    return manifest


def _parse_args() -> argparse.Namespace:
    default_name = "cutover-" + datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    parser = argparse.ArgumentParser(
        description="Create a consistent Money Mani migration directory."
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--destination", type=Path, default=Path(default_name))
    parser.add_argument("--web-port", type=int, default=31234)
    parser.add_argument(
        "--oauth-token",
        type=Path,
        default=Path("~/.money_mani/openai_oauth_token.json"),
    )
    parser.add_argument(
        "--confirm-services-stopped",
        action="store_true",
        help="Required assertion that local web and scheduler are stopped.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    manifest = prepare_cutover(
        args.project_root,
        args.destination,
        services_stopped=args.confirm_services_stopped,
        web_port=args.web_port,
        oauth_token=args.oauth_token,
    )
    print(
        json.dumps(
            {
                "destination": str(args.destination.resolve()),
                "database": manifest["database"],
                "source_git_sha": manifest["source_git_sha"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
