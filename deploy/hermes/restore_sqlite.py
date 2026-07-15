"""Verified and atomic SQLite restore helper.

The caller must stop every web and scheduler writer before invoking this tool.
The explicit confirmation flag is intentionally required because replacing a
live WAL database can corrupt restored state.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from deploy.hermes.sqlite_backup import (
        create_backup,
        sha256_file,
        verify_database,
    )
except ModuleNotFoundError:  # Allow direct execution from deploy/hermes/.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from deploy.hermes.sqlite_backup import (
        create_backup,
        sha256_file,
        verify_database,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _capture_auth_state(path: Path) -> dict | None:
    """Capture current identities/audit state without any live sessions."""
    if not path.is_file():
        return None
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {"app_users", "app_sessions", "auth_audit_events"}
        if not required.issubset(tables):
            return None

        def snapshot(table: str) -> dict:
            columns = [
                row[1]
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                )
            ]
            rows = [
                [row[column] for column in columns]
                for row in connection.execute(f'SELECT * FROM "{table}"')
            ]
            return {"columns": columns, "rows": rows}

        return {
            "app_users": snapshot("app_users"),
            "auth_audit_events": snapshot("auth_audit_events"),
        }
    finally:
        connection.close()


def _apply_auth_safety(
    path: Path,
    *,
    preserved_auth_state: dict | None,
) -> dict:
    """Preserve current identities when requested and revoke every session."""
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {"app_users", "app_sessions", "auth_audit_events"}
        if not required.issubset(tables):
            if preserved_auth_state is not None:
                raise RuntimeError(
                    "Rollback backup lacks the authentication schema; refusing "
                    "to discard current passwords and account status"
                )
            return {"sessions_revoked": 0, "auth_state_preserved": False}

        sessions_revoked = int(
            connection.execute("SELECT COUNT(*) FROM app_sessions").fetchone()[0]
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM app_sessions")
        if preserved_auth_state is not None:
            connection.execute("DELETE FROM auth_audit_events")
            connection.execute("DELETE FROM app_users")
            for table in ("app_users", "auth_audit_events"):
                snapshot = preserved_auth_state[table]
                columns = snapshot["columns"]
                if not columns:
                    continue
                column_sql = ", ".join(f'"{name}"' for name in columns)
                placeholders = ", ".join("?" for _ in columns)
                connection.executemany(
                    f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',
                    snapshot["rows"],
                )
        connection.commit()
        return {
            "sessions_revoked": sessions_revoked,
            "auth_state_preserved": preserved_auth_state is not None,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def restore_database(
    backup_path: Path,
    target_path: Path,
    *,
    safety_directory: Path,
    expected_sha256: str | None = None,
    services_stopped: bool = False,
    preserve_auth_state: bool | None = None,
) -> dict:
    """Verify a snapshot, preserve current state, then atomically restore it."""
    if not services_stopped:
        raise RuntimeError(
            "Refusing SQLite restore without explicit services-stopped confirmation"
        )

    backup_path = Path(backup_path).resolve()
    target_path = Path(target_path).resolve()
    safety_directory = Path(safety_directory).resolve()
    if backup_path == target_path:
        raise ValueError("Backup and target database paths must be different")

    source_verification = verify_database(backup_path)
    actual_sha256 = sha256_file(backup_path)
    sibling_manifest = backup_path.with_suffix(".manifest.json")
    if expected_sha256 is None and sibling_manifest.is_file():
        manifest = json.loads(sibling_manifest.read_text(encoding="utf-8"))
        expected_sha256 = manifest.get("sha256")
    if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
        raise RuntimeError(
            "Backup SHA-256 mismatch: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )

    preserved_auth_state = None
    if preserve_auth_state is not False:
        preserved_auth_state = _capture_auth_state(target_path)
    if preserved_auth_state is not None:
        backup_tables = set(source_verification["row_counts"])
        required_auth_tables = {
            "app_users",
            "app_sessions",
            "auth_audit_events",
        }
        if not required_auth_tables.issubset(backup_tables):
            raise RuntimeError(
                "Rollback backup lacks the authentication schema; refusing "
                "to discard current passwords and account status"
            )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    safety_directory.mkdir(parents=True, exist_ok=True)
    safety_directory.chmod(0o700)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.restore.",
        suffix=".tmp",
        dir=target_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copy2(backup_path, temporary_path)
        temporary_path.chmod(0o600)
        raw_restored_verification = verify_database(temporary_path)
        raw_restored_sha256 = sha256_file(temporary_path)
        if raw_restored_sha256 != actual_sha256:
            raise RuntimeError(
                "Restored database checksum differs from the verified backup"
            )

        # Apply account preservation and session revocation to the temporary
        # database. The live target remains untouched if any auth/schema step
        # fails or the process exits before the final atomic replace.
        auth_safety = _apply_auth_safety(
            temporary_path,
            preserved_auth_state=preserved_auth_state,
        )
        prepared_verification = verify_database(temporary_path)
        prepared_sha256 = sha256_file(temporary_path)

        safety_results = []
        if target_path.exists():
            safety_results = create_backup(
                target_path,
                safety_directory,
                kind="pre-restore",
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantined_sidecars: list[str] = []
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target_path) + suffix)
            if sidecar.exists():
                quarantined = safety_directory / (
                    f"{target_path.name}.{timestamp}{suffix}"
                )
                os.replace(sidecar, quarantined)
                quarantined_sidecars.append(str(quarantined))

        os.replace(temporary_path, target_path)
        _fsync_directory(target_path.parent)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    restored_verification = verify_database(target_path)
    restored_sha256 = sha256_file(target_path)
    if restored_sha256 != prepared_sha256:
        raise RuntimeError("Atomic restore target differs from prepared database")
    if restored_verification != prepared_verification:
        raise RuntimeError("Atomic restore verification differs from prepared database")

    return {
        "restored_from": str(backup_path),
        "target": str(target_path),
        "sha256": restored_sha256,
        "source_sha256": actual_sha256,
        "source_verification": source_verification,
        "raw_restored_verification": raw_restored_verification,
        "restored_verification": restored_verification,
        **auth_safety,
        "safety_backups": [result.path for result in safety_results],
        "quarantined_sidecars": quarantined_sidecars,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore a verified SQLite snapshot atomically."
    )
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--safety-directory", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument(
        "--confirm-services-stopped",
        action="store_true",
        help="Required assertion that web and scheduler writers are stopped.",
    )
    auth_group = parser.add_mutually_exclusive_group()
    auth_group.add_argument(
        "--preserve-auth-state",
        action="store_true",
        help="Explicitly keep current users/passwords/status (the safe default).",
    )
    auth_group.add_argument(
        "--replace-auth-state",
        action="store_true",
        help="DANGEROUS: replace current users/passwords/status from the backup.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = restore_database(
        args.backup,
        args.target,
        safety_directory=args.safety_directory,
        expected_sha256=args.expected_sha256,
        services_stopped=args.confirm_services_stopped,
        preserve_auth_state=(
            False
            if args.replace_auth_state
            else True
            if args.preserve_auth_state
            else None
        ),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
