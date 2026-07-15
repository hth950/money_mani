"""Deployment invariants for the Hermes production runtime."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from deploy.hermes.install_cutover import install_cutover
from deploy.hermes.prepare_cutover import prepare_cutover
from deploy.hermes.restore_sqlite import restore_database
from deploy.hermes.secret_scan import scan_revision
from deploy.hermes.sqlite_backup import (
    create_backup,
    rotate_backups,
    verify_database,
)


PROJECT_ROOT = Path(__file__).parent.parent


def _make_database(path: Path, value: str = "original") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO sample(value) VALUES (?)", (value,))
    connection.commit()
    connection.close()


def _add_auth_state(
    path: Path,
    *,
    password_hash: str,
    is_active: int,
    session_hash: str,
    audit_event: str,
) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE app_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            password_changed_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE TABLE app_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            csrf_token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            client_ip TEXT,
            user_agent TEXT
        );
        CREATE TABLE auth_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES app_users(id) ON DELETE SET NULL,
            username TEXT,
            event_type TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            detail_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO app_users (
            id, username, password_hash, role, is_active, created_at,
            updated_at, password_changed_at, last_login_at
        ) VALUES (1, 'admin', ?, 'owner', ?, '2026-01-01', '2026-01-01',
                  '2026-01-01', NULL)
        """,
        (password_hash, is_active),
    )
    connection.execute(
        """
        INSERT INTO app_sessions (
            user_id, token_hash, csrf_token_hash, created_at, last_seen_at,
            expires_at, revoked_at, client_ip, user_agent
        ) VALUES (1, ?, 'csrf', '2026-01-01', '2026-01-01',
                  '2099-01-01', NULL, '127.0.0.1', 'test')
        """,
        (session_hash,),
    )
    connection.execute(
        """
        INSERT INTO auth_audit_events (
            user_id, username, event_type, created_at
        ) VALUES (1, 'admin', ?, '2026-01-01')
        """,
        (audit_event,),
    )
    connection.commit()
    connection.close()


def test_database_path_can_be_overridden(monkeypatch, tmp_path):
    from web.db import connection

    configured = tmp_path / "persistent" / "money_mani.db"
    monkeypatch.setenv("MONEY_MANI_DB_PATH", str(configured))
    assert connection._resolve_db_path() == configured.resolve()


def test_scheduler_uses_internal_url_and_secret_header(monkeypatch):
    from pipeline import scheduler

    monkeypatch.setenv("MONEY_MANI_WEB_BASE_URL", "http://web:31234/")
    monkeypatch.setenv("MONEY_MANI_INTERNAL_TOKEN", "x" * 32)
    assert scheduler._web_base_url() == "http://web:31234"
    assert scheduler._internal_request_headers() == {
        "X-Money-Mani-Internal-Token": "x" * 32
    }


def test_container_settings_never_invoke_systemctl(monkeypatch):
    from web.routers import settings

    monkeypatch.setenv("MONEY_MANI_CONTAINERIZED", "1")
    monkeypatch.setattr(
        settings.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("systemctl must not run in container"),
    )
    assert settings._restart_services() is False


def test_online_backup_and_restore_are_verified(tmp_path):
    source = tmp_path / "source.db"
    _make_database(source)
    created_at = datetime(2026, 7, 19, 3, 30, tzinfo=ZoneInfo("Asia/Seoul"))

    results = create_backup(
        source,
        tmp_path / "backups",
        kind="auto",
        now=created_at,
    )
    assert [result.kind for result in results] == ["daily", "weekly"]
    manifest = json.loads(Path(results[0].manifest_path).read_text())
    assert manifest["verification"]["integrity_check"] == "ok"
    assert manifest["verification"]["row_counts"]["sample"] == 1

    target = tmp_path / "target.db"
    restored = restore_database(
        Path(results[0].path),
        target,
        safety_directory=tmp_path / "restore-safety",
        expected_sha256=results[0].sha256,
        services_stopped=True,
    )
    assert restored["sha256"] == results[0].sha256
    assert verify_database(target)["row_counts"]["sample"] == 1

    rollback_snapshot = create_backup(
        source,
        tmp_path / "backups",
        kind="pre-rollback",
        now=datetime(2026, 7, 20, 3, 30, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    assert rollback_snapshot[0].kind == "pre-rollback"


def test_restore_requires_services_stopped_confirmation(tmp_path):
    source = tmp_path / "source.db"
    _make_database(source)
    with pytest.raises(RuntimeError, match="services-stopped"):
        restore_database(
            source,
            tmp_path / "target.db",
            safety_directory=tmp_path / "safety",
        )


def test_rollback_preserves_current_auth_state_and_revokes_sessions(tmp_path):
    old_database = tmp_path / "old.db"
    _make_database(old_database, "old-business-state")
    _add_auth_state(
        old_database,
        password_hash="old-password-hash",
        is_active=1,
        session_hash="old-session",
        audit_event="old-login",
    )
    backup = create_backup(
        old_database,
        tmp_path / "backups",
        kind="pre-deploy",
    )[0]

    current_database = tmp_path / "current.db"
    _make_database(current_database, "new-business-state")
    _add_auth_state(
        current_database,
        password_hash="new-password-hash",
        is_active=0,
        session_hash="current-session",
        audit_event="account-disabled",
    )
    result = restore_database(
        Path(backup.path),
        current_database,
        safety_directory=tmp_path / "safety",
        services_stopped=True,
    )

    connection = sqlite3.connect(current_database)
    user = connection.execute(
        "SELECT password_hash, is_active FROM app_users WHERE username='admin'"
    ).fetchone()
    sessions = connection.execute("SELECT COUNT(*) FROM app_sessions").fetchone()[0]
    events = [
        row[0]
        for row in connection.execute(
            "SELECT event_type FROM auth_audit_events ORDER BY id"
        )
    ]
    business_value = connection.execute("SELECT value FROM sample").fetchone()[0]
    connection.close()

    assert user == ("new-password-hash", 0)
    assert sessions == 0
    assert events == ["account-disabled"]
    assert business_value == "old-business-state"
    assert result["auth_state_preserved"] is True


def test_rotation_keeps_14_daily_and_8_weekly(tmp_path):
    for kind, count in (("daily", 17), ("weekly", 11)):
        for index in range(count):
            database = tmp_path / f"money_mani_{kind}_{index:02d}.db"
            database.touch()
            database.with_suffix(".manifest.json").touch()

    rotate_backups(tmp_path)
    assert len(list(tmp_path.glob("money_mani_daily_*.db"))) == 14
    assert len(list(tmp_path.glob("money_mani_weekly_*.db"))) == 8


def test_cutover_bundle_verifies_and_installs(tmp_path):
    project = tmp_path / "project"
    _make_database(project / "data" / "money_mani.db")
    (project / "config").mkdir(parents=True)
    (project / "config" / "settings.yaml").write_text("web: {}\n")
    (project / "output").mkdir()
    (project / "output" / "marker.txt").write_text("state")
    (project / "MEMORY.md").write_text("memory")
    (project / ".env").write_text("MONEY_MANI_INTERNAL_TOKEN=" + "x" * 32)

    bundle = tmp_path / "cutover"
    prepare_cutover(
        project,
        bundle,
        services_stopped=True,
        web_port=65432,
        oauth_token=None,
    )
    result = install_cutover(
        bundle,
        tmp_path / "state",
        tmp_path / "secrets",
        services_stopped=True,
    )
    assert result["database_sha256"]
    assert (tmp_path / "state" / "config" / "settings.yaml").is_file()
    assert (tmp_path / "state" / "MEMORY.md").read_text() == "memory"
    assert (tmp_path / "secrets" / "app.env").stat().st_mode & 0o777 == 0o600
    assert result["bundle_removed"] is True
    assert not bundle.exists()


def test_cutover_refuses_a_running_scheduler_process(tmp_path):
    project = tmp_path / "project"
    _make_database(project / "data" / "money_mani.db")
    (project / "config").mkdir(parents=True)
    (project / "output").mkdir()
    (project / ".env").write_text("TOKEN=test\n")

    scheduler = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "start_scheduler",
        ]
    )
    try:
        time.sleep(0.1)
        with pytest.raises(RuntimeError, match="scheduler process"):
            prepare_cutover(
                project,
                tmp_path / "cutover-running",
                services_stopped=True,
                web_port=65432,
                oauth_token=None,
            )
    finally:
        scheduler.terminate()
        scheduler.wait(timeout=5)


def test_compose_keeps_public_port_on_loopback_and_one_scheduler():
    compose = yaml.safe_load((PROJECT_ROOT / "compose.hermes.yml").read_text())
    services = compose["services"]
    web = services["web"]
    scheduler = services["scheduler"]

    assert web["ports"] == ["127.0.0.1:32777:31234"]
    assert web["read_only"] is True
    assert web["cap_drop"] == ["ALL"]
    assert web["healthcheck"]["test"][1] == "python"
    assert scheduler["depends_on"]["web"]["condition"] == "service_healthy"
    assert scheduler["command"] == ["python", "main.py", "schedule"]
    assert services.keys() >= {"web", "scheduler", "backup"}
    assert services["backup"]["cap_drop"] == ["ALL"]
    assert any(
        mount.endswith("/home:/home/money-mani")
        for mount in web["volumes"]
    )
    assert web["environment"]["MONEY_MANI_DB_PATH"] == "/app/data/money_mani.db"
    assert web["environment"]["MONEY_MANI_ENV"] == "production"
    assert "MONEY_MANI_ALLOWED_HOSTS" not in web["environment"]
    assert "*" not in web["environment"]["MONEY_MANI_FORWARDED_ALLOW_IPS"]
    assert compose["networks"]["default"]["ipam"]["config"][0]["gateway"] == (
        "${MONEY_MANI_DOCKER_GATEWAY:-172.30.77.1}"
    )


def test_runtime_image_is_non_root():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    assert "FROM python:3.12-slim-bookworm AS runtime" in dockerfile
    assert "ARG APP_UID=1000" in dockerfile
    assert "USER money-mani" in dockerfile


def test_layout_rejects_wildcard_allowed_hosts(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "app.env").write_text(
        "MONEY_MANI_INTERNAL_TOKEN=" + "x" * 32 + "\n"
        "MONEY_MANI_ALLOWED_HOSTS=localhost,127.0.0.1,web,*.ts.net\n"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "MONEY_MANI_APP_ROOT": str(tmp_path),
            "MONEY_MANI_REPO_DIR": str(PROJECT_ROOT),
            "MONEY_MANI_STATE_DIR": str(tmp_path / "state"),
            "MONEY_MANI_SECRETS_DIR": str(secrets),
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {PROJECT_ROOT / 'deploy/hermes/common.sh'}; ensure_layout",
        ],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert result.returncode != 0
    assert "wildcards are forbidden" in result.stderr


def test_bootstrap_requires_one_explicit_public_key(tmp_path):
    script = PROJECT_ROOT / "deploy/hermes/bootstrap_host.sh"
    source = script.read_text()
    assert "/root/.ssh/authorized_keys" not in source

    missing = subprocess.run(
        ["bash", str(script)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing.returncode == 2
    assert "--authorized-key-file is required" in missing.stderr

    multiple_keys = tmp_path / "multiple.pub"
    multiple_keys.write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOne first\n"
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestTwo second\n"
    )
    multiple = subprocess.run(
        [
            "bash",
            str(script),
            "--authorized-key-file",
            str(multiple_keys),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert multiple.returncode == 2
    assert "Exactly one public key is required" in multiple.stderr


def test_secret_scan_rejects_a_committed_env_file(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True
    )
    (tmp_path / ".env").write_text("SECRET=value\n")
    subprocess.run(["git", "add", "-f", ".env"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True
    )
    monkeypatch.chdir(tmp_path)
    assert scan_revision("HEAD") == [
        (".env", "forbidden sensitive path")
    ]


def test_secret_scan_handles_unicode_git_paths(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True
    )
    unicode_path = tmp_path / "config" / "전략 이름.yaml"
    unicode_path.parent.mkdir()
    unicode_path.write_text("status: validated_v2\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "unicode fixture"],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    assert scan_revision("HEAD") == []
