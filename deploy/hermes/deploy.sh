#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

usage() {
    printf 'Usage: %s <full-40-character-git-sha>\n' "$0"
}

[[ $# -eq 1 ]] || { usage >&2; exit 2; }
TARGET_SHA="${1,,}"
assert_exact_sha "$TARGET_SHA"

require_command docker
require_command git
require_command python3
require_command rsync
require_command flock

mkdir -p "$APP_ROOT"
exec 9>"$LOCK_FILE"
flock -n 9 || die "Another deployment or rollback is already running"

[[ -d "${REPO_DIR}/.git" ]] || die "Git checkout not found: ${REPO_DIR}"
cd "$REPO_DIR"
[[ -z "$(git status --porcelain)" ]] \
    || die "Deployment checkout has uncommitted or untracked files"

log "Fetching remote refs"
git fetch --prune origin
git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null \
    || die "Git commit is unavailable after fetch: ${TARGET_SHA}"
git branch --remotes --contains "$TARGET_SHA" | grep -q . \
    || die "Commit is not present on a fetched remote branch; push it first"
python3 deploy/hermes/secret_scan.py --revision "$TARGET_SHA"

CURRENT_METADATA="${DEPLOYMENTS_DIR}/current.json"
PREVIOUS_SHA=""
if [[ -f "$CURRENT_METADATA" ]]; then
    PREVIOUS_SHA="$(read_json_field "$CURRENT_METADATA" deployed_sha)"
else
    PREVIOUS_SHA="$(git rev-parse HEAD)"
fi

BACKUP_PATH=""
if [[ -f "$DB_PATH" ]]; then
    log "Creating verified pre-deploy SQLite backup"
    BACKUP_PATH="$(
        python3 deploy/hermes/sqlite_backup.py \
            --db "$DB_PATH" \
            --destination "${STATE_DIR}/backups" \
            --kind pre-deploy \
            --json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["path"])'
    )"
fi

log "Checking out exact commit ${TARGET_SHA}"
git switch --detach "$TARGET_SHA"
ensure_layout
write_deploy_env "$TARGET_SHA"

PENDING_METADATA="${DEPLOYMENTS_DIR}/pending.json"
write_release_metadata \
    "$PENDING_METADATA" "$TARGET_SHA" "$PREVIOUS_SHA" "$BACKUP_PATH" pending

log "Validating Compose configuration"
compose config --quiet
log "Building immutable image money-mani:${TARGET_SHA}"
compose build --pull web scheduler

log "Starting web and the single scheduler"
compose up --detach --no-build web scheduler
if ! wait_for_web_health 180; then
    compose logs --tail 100 web >&2 || true
    die "New web container did not become healthy; run rollback.sh"
fi
assert_single_scheduler

write_release_metadata \
    "$PENDING_METADATA" "$TARGET_SHA" "$PREVIOUS_SHA" "$BACKUP_PATH" healthy
HISTORY_FILE="${DEPLOYMENTS_DIR}/$(date -u +%Y%m%dT%H%M%SZ)-${TARGET_SHA}.json"
cp "$PENDING_METADATA" "$HISTORY_FILE"
chmod 600 "$HISTORY_FILE"
mv "$PENDING_METADATA" "$CURRENT_METADATA"

log "Deployment healthy at ${TARGET_SHA}"
compose ps
