#!/usr/bin/env bash

set -Eeuo pipefail

APP_ROOT="${MONEY_MANI_APP_ROOT:-/srv/money-mani}"
REPO_DIR="${MONEY_MANI_REPO_DIR:-${APP_ROOT}/app}"
STATE_DIR="${MONEY_MANI_STATE_DIR:-${APP_ROOT}/shared}"
SECRETS_DIR="${MONEY_MANI_SECRETS_DIR:-${APP_ROOT}/secrets}"
DEPLOY_ENV="${MONEY_MANI_DEPLOY_ENV:-${APP_ROOT}/deploy.env}"
COMPOSE_FILE="${REPO_DIR}/compose.hermes.yml"
DB_PATH="${STATE_DIR}/data/money_mani.db"
DEPLOYMENTS_DIR="${STATE_DIR}/deployments"
LOCK_FILE="${APP_ROOT}/deploy.lock"


log() {
    printf '[money-mani] %s\n' "$*"
}


die() {
    printf '[money-mani] ERROR: %s\n' "$*" >&2
    exit 1
}


require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}


assert_exact_sha() {
    local sha="$1"
    [[ "$sha" =~ ^[0-9a-fA-F]{40}$ ]] \
        || die "A full 40-character Git SHA is required"
}


ensure_layout() {
    umask 077
    mkdir -p \
        "${STATE_DIR}/data" \
        "${STATE_DIR}/output" \
        "${STATE_DIR}/config" \
        "${STATE_DIR}/home/.money_mani" \
        "${STATE_DIR}/backups" \
        "${STATE_DIR}/restore-safety" \
        "$DEPLOYMENTS_DIR" \
        "$SECRETS_DIR"
    if [[ ! -f "${STATE_DIR}/MEMORY.md" ]]; then
        install -m 600 /dev/null "${STATE_DIR}/MEMORY.md"
    fi
    chmod 700 "$STATE_DIR" "$SECRETS_DIR" "${STATE_DIR}/backups"
    chmod 700 "${STATE_DIR}/home" "${STATE_DIR}/home/.money_mani"
    if [[ -d "${STATE_DIR}/oauth" ]]; then
        rsync -a --ignore-existing \
            "${STATE_DIR}/oauth/" "${STATE_DIR}/home/.money_mani/"
    fi
    [[ -f "${SECRETS_DIR}/app.env" ]] \
        || die "Missing production secrets file: ${SECRETS_DIR}/app.env"
    chmod 600 "${SECRETS_DIR}/app.env"
    python3 - "${SECRETS_DIR}/app.env" <<'PY'
import sys
from pathlib import Path

values = {}
for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip("'\"")
token = values.get("MONEY_MANI_INTERNAL_TOKEN", "")
if len(token) < 32:
    raise SystemExit(
        "MONEY_MANI_INTERNAL_TOKEN must be present and at least 32 characters"
    )
allowed_hosts = {
    item.strip()
    for item in values.get("MONEY_MANI_ALLOWED_HOSTS", "").split(",")
    if item.strip()
}
required_hosts = {"localhost", "127.0.0.1", "web"}
if not required_hosts.issubset(allowed_hosts):
    raise SystemExit(
        "MONEY_MANI_ALLOWED_HOSTS must include localhost,127.0.0.1,web"
    )
if any("*" in host for host in allowed_hosts):
    raise SystemExit(
        "MONEY_MANI_ALLOWED_HOSTS must use exact hostnames; wildcards are forbidden"
    )
PY

    # Persistent config is operator-owned. New tracked files are added, while
    # existing production edits are never overwritten by a code deployment.
    rsync -a --ignore-existing "${REPO_DIR}/config/" "${STATE_DIR}/config/"
}


write_deploy_env() {
    local sha="$1"
    local temporary="${DEPLOY_ENV}.tmp"
    umask 077
    {
        printf 'APP_GIT_SHA=%s\n' "$sha"
        printf 'MONEY_MANI_STATE_DIR=%s\n' "$STATE_DIR"
        printf 'MONEY_MANI_ENV_FILE=%s\n' "${SECRETS_DIR}/app.env"
        printf 'MONEY_MANI_UID=%s\n' "$(id -u)"
        printf 'MONEY_MANI_GID=%s\n' "$(id -g)"
        printf 'MONEY_MANI_DOCKER_SUBNET=%s\n' \
            "${MONEY_MANI_DOCKER_SUBNET:-172.30.77.0/24}"
        printf 'MONEY_MANI_DOCKER_GATEWAY=%s\n' \
            "${MONEY_MANI_DOCKER_GATEWAY:-172.30.77.1}"
        printf 'MONEY_MANI_FORWARDED_ALLOW_IPS=%s\n' \
            "${MONEY_MANI_FORWARDED_ALLOW_IPS:-172.30.77.1,127.0.0.1,::1}"
    } >"$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$DEPLOY_ENV"
}


compose() {
    docker compose \
        --env-file "$DEPLOY_ENV" \
        --file "$COMPOSE_FILE" \
        "$@"
}


wait_for_web_health() {
    local timeout_seconds="${1:-180}"
    local started_at
    local container_id
    local status
    started_at="$(date +%s)"
    while true; do
        container_id="$(compose ps -q web 2>/dev/null || true)"
        if [[ -n "$container_id" ]]; then
            status="$(
                docker inspect \
                    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
                    "$container_id" 2>/dev/null || true
            )"
            if [[ "$status" == "healthy" ]]; then
                return 0
            fi
            if [[ "$status" == "unhealthy" ]]; then
                return 1
            fi
        fi
        if (( $(date +%s) - started_at >= timeout_seconds )); then
            return 1
        fi
        sleep 2
    done
}


assert_single_scheduler() {
    local count
    count="$(compose ps -q scheduler | sed '/^$/d' | wc -l | tr -d ' ')"
    [[ "$count" == "1" ]] \
        || die "Expected exactly one scheduler container, found ${count}"
}


read_json_field() {
    local path="$1"
    local field="$2"
    python3 - "$path" "$field" <<'PY'
import json
import sys

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
value = payload
for part in sys.argv[2].split("."):
    value = value.get(part) if isinstance(value, dict) else None
if value is not None:
    print(value)
PY
}


write_release_metadata() {
    local path="$1"
    local deployed_sha="$2"
    local previous_sha="$3"
    local backup_path="$4"
    local status="$5"
    python3 - "$path" "$deployed_sha" "$previous_sha" "$backup_path" "$status" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "deployed_at": datetime.now(timezone.utc).isoformat(),
    "deployed_sha": sys.argv[2],
    "previous_sha": sys.argv[3] or None,
    "pre_deploy_backup": sys.argv[4] or None,
    "status": sys.argv[5],
}
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
temporary.chmod(0o600)
os.replace(temporary, path)
PY
}
