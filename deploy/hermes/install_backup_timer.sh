#!/usr/bin/env bash

set -Eeuo pipefail

[[ "${EUID}" -eq 0 ]] || {
    printf 'Run this installer as root.\n' >&2
    exit 1
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install -m 644 \
    "${SCRIPT_DIR}/systemd/money-mani-backup.service" \
    /etc/systemd/system/money-mani-backup.service
install -m 644 \
    "${SCRIPT_DIR}/systemd/money-mani-backup.timer" \
    /etc/systemd/system/money-mani-backup.timer
systemctl daemon-reload
systemctl enable --now money-mani-backup.timer
systemctl list-timers money-mani-backup.timer --no-pager
