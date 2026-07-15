"""SSH-only application user management.

Examples:
    .venv/bin/python -m web.auth_cli create --username admin --role owner
    .venv/bin/python -m web.auth_cli reset-password --username admin
    .venv/bin/python -m web.auth_cli disable --username guest
    .venv/bin/python -m web.auth_cli list
"""

from __future__ import annotations

import argparse
import getpass
import sys

from web.auth.service import (
    AuthError,
    create_user,
    list_users,
    reset_password,
    set_user_active,
)
from web.db.connection import init_db
from web.db.migrate import run_schema_migrations


def _confirmed_password(prompt: str) -> str:
    first = getpass.getpass(prompt)
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise AuthError("passwords do not match")
    return first


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Money Mani web users")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create an owner or viewer")
    create.add_argument("--username", required=True)
    create.add_argument("--role", required=True, choices=("owner", "viewer"))

    reset = subparsers.add_parser("reset-password", help="replace a password and revoke sessions")
    reset.add_argument("--username", required=True)

    disable = subparsers.add_parser("disable", help="disable a user and revoke sessions")
    disable.add_argument("--username", required=True)

    enable = subparsers.add_parser("enable", help="re-enable a user")
    enable.add_argument("--username", required=True)

    subparsers.add_parser("list", help="list users without credential material")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    init_db()
    run_schema_migrations()
    try:
        if args.command == "create":
            user = create_user(args.username, _confirmed_password("Password: "), args.role)
            print(f"created {user.username} ({user.role})")
        elif args.command == "reset-password":
            reset_password(args.username, _confirmed_password("New password: "))
            print(f"password reset and sessions revoked: {args.username}")
        elif args.command == "disable":
            set_user_active(args.username, False)
            print(f"disabled and sessions revoked: {args.username}")
        elif args.command == "enable":
            set_user_active(args.username, True)
            print(f"enabled: {args.username}")
        elif args.command == "list":
            print("USERNAME\tROLE\tACTIVE\tLAST_LOGIN")
            for user in list_users():
                print(
                    f"{user['username']}\t{user['role']}\t"
                    f"{'yes' if user['is_active'] else 'no'}\t"
                    f"{user['last_login_at'] or '-'}"
                )
        return 0
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
