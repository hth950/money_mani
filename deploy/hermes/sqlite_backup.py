"""Consistent SQLite backups, verification, manifests, and retention.

This module uses SQLite's online Backup API. Copying the database file with a
regular filesystem copy is unsafe while WAL mode is active.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
DAILY_RETENTION = 14
WEEKLY_RETENTION = 8
VALID_KINDS = {
    "auto",
    "daily",
    "weekly",
    "pre-deploy",
    "pre-rollback",
    "pre-restore",
    "cutover",
    "manual",
}


@dataclass(frozen=True)
class BackupResult:
    path: str
    manifest_path: str
    kind: str
    sha256: str
    size_bytes: int
    created_at: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()))}?mode=ro"


def verify_database(path: Path) -> dict[str, Any]:
    """Return integrity, foreign-key, and row-count evidence for a database."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")

    connection = sqlite3.connect(
        _readonly_uri(path), uri=True, timeout=30, isolation_level=None
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        integrity_rows = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        if integrity_rows != ["ok"]:
            raise RuntimeError(
                f"SQLite integrity_check failed for {path}: {integrity_rows[:5]}"
            )

        foreign_key_rows = [
            list(row) for row in connection.execute("PRAGMA foreign_key_check")
        ]
        if foreign_key_rows:
            raise RuntimeError(
                "SQLite foreign_key_check failed for "
                f"{path}: {foreign_key_rows[:5]}"
            )

        table_names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        row_counts: dict[str, int] = {}
        for table_name in table_names:
            quoted_name = '"' + table_name.replace('"', '""') + '"'
            row_counts[table_name] = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {quoted_name}"
                ).fetchone()[0]
            )

        return {
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "row_counts": row_counts,
            "sqlite_version": sqlite3.sqlite_version,
        }
    finally:
        connection.close()


def checkpoint_database(path: Path) -> tuple[int, int, int]:
    """Checkpoint and truncate WAL, refusing a busy cutover."""
    connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        result = tuple(
            int(value)
            for value in connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
        )
        if result[0] != 0:
            raise RuntimeError(
                "SQLite WAL checkpoint is busy; a writer may still be running "
                f"(result={result})"
            )
        return result
    finally:
        connection.close()


def _write_manifest(
    backup_path: Path,
    *,
    source_path: Path,
    kind: str,
    created_at: datetime,
) -> BackupResult:
    verification = verify_database(backup_path)
    digest = sha256_file(backup_path)
    result = BackupResult(
        path=str(backup_path.resolve()),
        manifest_path=str(backup_path.with_suffix(".manifest.json").resolve()),
        kind=kind,
        sha256=digest,
        size_bytes=backup_path.stat().st_size,
        created_at=created_at.astimezone(timezone.utc).isoformat(),
    )
    manifest = {
        **asdict(result),
        "source_path": str(source_path.resolve()),
        "verification": verification,
    }
    manifest_path = Path(result.manifest_path)
    temporary_manifest = manifest_path.with_suffix(
        manifest_path.suffix + ".tmp"
    )
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_manifest.chmod(0o600)
    os.replace(temporary_manifest, manifest_path)
    return result


def _snapshot_name(kind: str, created_at: datetime) -> str:
    timestamp = created_at.astimezone(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    return f"money_mani_{kind}_{timestamp}.db"


def _online_backup(
    source_path: Path,
    destination_path: Path,
) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        source = sqlite3.connect(
            _readonly_uri(source_path), uri=True, timeout=30
        )
        target = sqlite3.connect(str(temporary_path), timeout=30)
        try:
            source.execute("PRAGMA busy_timeout=30000")
            source.backup(target, pages=1000, sleep=0.05)
            target.commit()
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target.commit()
        finally:
            target.close()
            source.close()

        verify_database(temporary_path)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, destination_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _copy_verified_snapshot(
    source_snapshot: Path,
    destination_path: Path,
) -> None:
    temporary_path = destination_path.with_suffix(
        destination_path.suffix + ".tmp"
    )
    shutil.copy2(source_snapshot, temporary_path)
    try:
        verify_database(temporary_path)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, destination_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def rotate_backups(
    destination: Path,
    *,
    daily_retention: int = DAILY_RETENTION,
    weekly_retention: int = WEEKLY_RETENTION,
) -> dict[str, list[str]]:
    """Remove only managed daily/weekly snapshots beyond retention."""
    removed: dict[str, list[str]] = {"daily": [], "weekly": []}
    for kind, retention in (
        ("daily", daily_retention),
        ("weekly", weekly_retention),
    ):
        if retention < 1:
            raise ValueError(f"{kind} retention must be at least 1")
        snapshots = sorted(
            destination.glob(f"money_mani_{kind}_*.db"), reverse=True
        )
        for snapshot in snapshots[retention:]:
            manifest = snapshot.with_suffix(".manifest.json")
            snapshot.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            removed[kind].append(snapshot.name)
    return removed


def create_backup(
    source_path: Path,
    destination: Path,
    *,
    kind: str = "manual",
    now: datetime | None = None,
    checkpoint: bool = False,
    daily_retention: int = DAILY_RETENTION,
    weekly_retention: int = WEEKLY_RETENTION,
) -> list[BackupResult]:
    """Create one backup, or daily plus Sunday weekly snapshots for auto."""
    source_path = Path(source_path)
    destination = Path(destination)
    if kind not in VALID_KINDS:
        raise ValueError(f"Unsupported backup kind: {kind}")
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")
    if checkpoint:
        checkpoint_database(source_path)

    created_at = now or datetime.now(timezone.utc)
    effective_kind = "daily" if kind == "auto" else kind
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)

    backup_path = destination / _snapshot_name(effective_kind, created_at)
    _online_backup(source_path, backup_path)
    results = [
        _write_manifest(
            backup_path,
            source_path=source_path,
            kind=effective_kind,
            created_at=created_at,
        )
    ]

    if kind == "auto" and created_at.astimezone(KST).weekday() == 6:
        weekly_path = destination / _snapshot_name("weekly", created_at)
        _copy_verified_snapshot(backup_path, weekly_path)
        results.append(
            _write_manifest(
                weekly_path,
                source_path=source_path,
                kind="weekly",
                created_at=created_at,
            )
        )

    rotate_backups(
        destination,
        daily_retention=daily_retention,
        weekly_retention=weekly_retention,
    )
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and verify a consistent Money Mani SQLite backup."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--kind", choices=sorted(VALID_KINDS), default="manual")
    parser.add_argument("--checkpoint", action="store_true")
    parser.add_argument("--daily-retention", type=int, default=DAILY_RETENTION)
    parser.add_argument("--weekly-retention", type=int, default=WEEKLY_RETENTION)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    results = create_backup(
        args.db,
        args.destination,
        kind=args.kind,
        checkpoint=args.checkpoint,
        daily_retention=args.daily_retention,
        weekly_retention=args.weekly_retention,
    )
    payload = [asdict(result) for result in results]
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        for result in results:
            print(result.path)


if __name__ == "__main__":
    main()
