#!/usr/bin/env bash

set -Eeuo pipefail

DEPLOY_USER="${MONEY_MANI_DEPLOY_USER:-money-mani}"
DEPLOY_UID="${MONEY_MANI_DEPLOY_UID:-1000}"
APP_ROOT="${MONEY_MANI_APP_ROOT:-/srv/money-mani}"
ALLOW_DOCKER_ROOT_EQUIVALENT="${MONEY_MANI_ALLOW_DOCKER_ROOT_EQUIVALENT:-0}"
AUTHORIZED_KEY_FILE=""

usage() {
    printf 'Usage: %s --authorized-key-file /path/to/single-public-key.pub\n' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --authorized-key-file)
            [[ $# -ge 2 ]] || {
                printf '%s\n' '--authorized-key-file requires a path' >&2
                exit 2
            }
            AUTHORIZED_KEY_FILE="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[[ -n "$AUTHORIZED_KEY_FILE" ]] || {
    printf '%s\n' \
        '--authorized-key-file is required; root authorized_keys is never copied' >&2
    usage >&2
    exit 2
}
[[ -f "$AUTHORIZED_KEY_FILE" ]] || {
    printf 'Public key file not found: %s\n' "$AUTHORIZED_KEY_FILE" >&2
    exit 2
}

KEY_COUNT="$(
    awk 'NF && $1 !~ /^#/ {count += 1} END {print count + 0}' \
        "$AUTHORIZED_KEY_FILE"
)"
[[ "$KEY_COUNT" == "1" ]] || {
    printf 'Exactly one public key is required; found %s in %s\n' \
        "$KEY_COUNT" "$AUTHORIZED_KEY_FILE" >&2
    exit 2
}

KEY_TYPE="$(awk 'NF && $1 !~ /^#/ {print $1; exit}' "$AUTHORIZED_KEY_FILE")"
case "$KEY_TYPE" in
    ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)
        ;;
    *)
        printf 'Unsupported or malformed public key type: %s\n' "$KEY_TYPE" >&2
        exit 2
        ;;
esac

command -v ssh-keygen >/dev/null 2>&1 || {
    printf 'ssh-keygen is required to validate the public key.\n' >&2
    exit 1
}
ssh-keygen -l -f "$AUTHORIZED_KEY_FILE" >/dev/null 2>&1 || {
    printf 'Public key validation failed: %s\n' "$AUTHORIZED_KEY_FILE" >&2
    exit 2
}

[[ "${EUID}" -eq 0 ]] || {
    printf 'Run this one-time bootstrap as root.\n' >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || {
    printf 'Docker must be installed before host bootstrap.\n' >&2
    exit 1
}

[[ "$ALLOW_DOCKER_ROOT_EQUIVALENT" == "1" ]] || {
    printf '%s\n' \
        'Refusing to add the deploy user to the root-equivalent docker group.' \
        'Re-run with MONEY_MANI_ALLOW_DOCKER_ROOT_EQUIVALENT=1 after review.' >&2
    exit 1
}

if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    if getent passwd "$DEPLOY_UID" >/dev/null 2>&1; then
        printf 'Requested UID %s is already in use. Set MONEY_MANI_DEPLOY_UID.\n' \
            "$DEPLOY_UID" >&2
        exit 1
    fi
    useradd \
        --uid "$DEPLOY_UID" \
        --create-home \
        --shell /bin/bash \
        "$DEPLOY_USER"
fi

usermod --append --groups docker "$DEPLOY_USER"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
    "$APP_ROOT" \
    "${APP_ROOT}/shared" \
    "${APP_ROOT}/shared/data" \
    "${APP_ROOT}/shared/output" \
    "${APP_ROOT}/shared/config" \
    "${APP_ROOT}/shared/home" \
    "${APP_ROOT}/shared/home/.money_mani" \
    "${APP_ROOT}/shared/backups" \
    "${APP_ROOT}/shared/deployments" \
    "${APP_ROOT}/shared/restore-safety" \
    "${APP_ROOT}/secrets"
install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
    /dev/null "${APP_ROOT}/shared/MEMORY.md"

install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
    "/home/${DEPLOY_USER}/.ssh"
install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
    "$AUTHORIZED_KEY_FILE" \
    "/home/${DEPLOY_USER}/.ssh/authorized_keys"

printf 'Installed deploy key fingerprint: '
ssh-keygen -l -f "$AUTHORIZED_KEY_FILE"

printf 'Host layout ready. Re-login as %s so Docker group membership applies.\n' \
    "$DEPLOY_USER"
printf 'SSH hardening is intentionally separate: verify a second key-only login first.\n'
