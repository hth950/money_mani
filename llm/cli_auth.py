"""OpenAI OAuth CLI authentication tool.

Usage:
    python -m llm.cli_auth           # Run device auth flow (new or re-auth)
    python -m llm.cli_auth --status  # Check current token status
    python -m llm.cli_auth --revoke  # Delete stored token
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from utils.config_loader import load_config


def get_token_path() -> Path:
    cfg = load_config()
    oauth_cfg = cfg.get("llm", {}).get("openai_oauth", {})
    return Path(
        oauth_cfg.get("token_path", "~/.money_mani/openai_oauth_token.json")
    ).expanduser()


def cmd_auth():
    """Run the device authorization flow."""
    from llm.device_auth import run_device_flow

    token_path = get_token_path()
    if token_path.exists():
        print(f"\n기존 토큰이 존재합니다: {token_path}")
        answer = input("새로 인증하시겠습니까? (y/N): ").strip().lower()
        if answer != "y":
            print("취소되었습니다.")
            return

    tokens = run_device_flow()

    # Save token
    token_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens, indent=2))
    tmp.chmod(0o600)
    tmp.rename(token_path)
    print(f"토큰이 저장되었습니다: {token_path}")


def cmd_status():
    """Check token status."""
    token_path = get_token_path()
    if not token_path.exists():
        print(f"토큰 파일 없음: {token_path}")
        print("인증이 필요합니다: python -m llm.cli_auth")
        return

    try:
        data = json.loads(token_path.read_text())
    except json.JSONDecodeError:
        print("토큰 파일이 손상되었습니다. 재인증이 필요합니다.")
        return

    expires_at = data.get("expires_at", 0)
    now = time.time()
    has_refresh = bool(data.get("refresh_token"))

    print(f"\n토큰 파일: {token_path}")
    print(f"Access token: {'유효' if now < expires_at else '만료됨'}")
    if expires_at > now:
        remaining = int(expires_at - now)
        mins, secs = divmod(remaining, 60)
        print(f"남은 시간: {mins}분 {secs}초")
    else:
        expired_ago = int(now - expires_at)
        mins, secs = divmod(expired_ago, 60)
        print(f"만료된 지: {mins}분 {secs}초")
    print(f"Refresh token: {'있음' if has_refresh else '없음'}")
    if has_refresh:
        print("→ 자동 갱신 가능")
    else:
        print("→ 재인증 필요: python -m llm.cli_auth")


def cmd_revoke():
    """Delete stored token."""
    token_path = get_token_path()
    if not token_path.exists():
        print(f"토큰 파일 없음: {token_path}")
        return

    answer = input(f"토큰을 삭제하시겠습니까? {token_path} (y/N): ").strip().lower()
    if answer == "y":
        token_path.unlink()
        print("토큰이 삭제되었습니다.")
    else:
        print("취소되었습니다.")


def main():
    parser = argparse.ArgumentParser(
        description="OpenAI OAuth 인증 관리",
        prog="python -m llm.cli_auth",
    )
    parser.add_argument("--status", action="store_true", help="토큰 상태 확인")
    parser.add_argument("--revoke", action="store_true", help="저장된 토큰 삭제")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.status:
        cmd_status()
    elif args.revoke:
        cmd_revoke()
    else:
        cmd_auth()


if __name__ == "__main__":
    main()
