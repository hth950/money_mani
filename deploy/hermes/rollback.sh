#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

TARGET_SHA=""
DATABASE_BACKUP=""
CUSTOM_TARGET=0
CUSTOM_BACKUP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --to)
            TARGET_SHA="${2:-}"
            CUSTOM_TARGET=1
            shift 2
            ;;
        --database-backup)
            DATABASE_BACKUP="${2:-}"
            CUSTOM_BACKUP=1
            shift 2
            ;;
        *)
            die "Unknown rollback option: $1"
            ;;
    esac
done

require_command docker
require_command git
require_command python3
require_command flock

exec 9>"$LOCK_FILE"
flock -n 9 || die "Another deployment or rollback is already running"

METADATA="${DEPLOYMENTS_DIR}/current.json"
if [[ -f "${DEPLOYMENTS_DIR}/pending.json" ]]; then
    METADATA="${DEPLOYMENTS_DIR}/pending.json"
fi
[[ -f "$METADATA" ]] || die "No deployment metadata is available"

RECORDED_PREVIOUS="$(read_json_field "$METADATA" previous_sha)"
RECORDED_BACKUP="$(read_json_field "$METADATA" pre_deploy_backup)"
ROLLED_BACK_FROM="$(read_json_field "$METADATA" deployed_sha)"
TARGET_SHA="${TARGET_SHA:-$RECORDED_PREVIOUS}"
DATABASE_BACKUP="${DATABASE_BACKUP:-$RECORDED_BACKUP}"
TARGET_SHA="${TARGET_SHA,,}"
assert_exact_sha "$TARGET_SHA"
if [[ "$CUSTOM_TARGET" == "1" && "$CUSTOM_BACKUP" != "1" ]]; then
    die "A custom rollback SHA requires --database-backup"
fi
[[ -f "$DATABASE_BACKUP" ]] \
    || die "Verified pre-deploy database backup is required: ${DATABASE_BACKUP}"

cd "$REPO_DIR"
[[ -z "$(git status --porcelain)" ]] \
    || die "Deployment checkout has uncommitted or untracked files"
git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null \
    || die "Rollback commit is unavailable: ${TARGET_SHA}"

CURRENT_SAFETY_BACKUP=""
if [[ -f "$DB_PATH" ]]; then
    CURRENT_SAFETY_BACKUP="$(
        python3 deploy/hermes/sqlite_backup.py \
            --db "$DB_PATH" \
            --destination "${STATE_DIR}/backups" \
            --kind pre-rollback \
            --json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["path"])'
    )"
fi

log "Stopping scheduler and web before database restore"
compose stop scheduler web
python3 deploy/hermes/restore_sqlite.py \
    --backup "$DATABASE_BACKUP" \
    --target "$DB_PATH" \
    --safety-directory "${STATE_DIR}/restore-safety" \
    --preserve-auth-state \
    --confirm-services-stopped >/dev/null

log "Switching code to ${TARGET_SHA}"
git switch --detach "$TARGET_SHA"
ensure_layout
write_deploy_env "$TARGET_SHA"
if ! docker image inspect "money-mani:${TARGET_SHA}" >/dev/null 2>&1; then
    compose build web scheduler
fi
compose up --detach --no-build web scheduler
if ! wait_for_web_health 180; then
    compose logs --tail 100 web >&2 || true
    die "Rolled-back web container did not become healthy"
fi
assert_single_scheduler

write_release_metadata \
    "$METADATA" "$TARGET_SHA" "$ROLLED_BACK_FROM" "$CURRENT_SAFETY_BACKUP" healthy
HISTORY_FILE="${DEPLOYMENTS_DIR}/$(date -u +%Y%m%dT%H%M%SZ)-rollback-${TARGET_SHA}.json"
cp "$METADATA" "$HISTORY_FILE"
chmod 600 "$HISTORY_FILE"
if [[ "$METADATA" != "${DEPLOYMENTS_DIR}/current.json" ]]; then
    mv "$METADATA" "${DEPLOYMENTS_DIR}/current.json"
fi

log "Rollback healthy at ${TARGET_SHA}"
compose ps
