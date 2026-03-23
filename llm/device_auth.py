"""OpenAI Device Authorization Flow for headless server authentication.

Implements a non-standard device auth flow compatible with OpenAI's Codex CLI OAuth.
Used when the server has no browser — displays a URL + code for the user to authenticate
on a local machine.
"""

import json
import logging
import time

import requests

logger = logging.getLogger("money_mani.llm.device_auth")

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEVICE_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
VERIFY_URL = "https://auth.openai.com/codex/device"

DEFAULT_SCOPES = "openid profile email offline_access"
POLL_TIMEOUT = 900  # 15 minutes


def request_device_code() -> dict:
    """Request a device code from OpenAI.

    Returns:
        Dict with device_auth_id, user_code, interval.

    Raises:
        RuntimeError: On HTTP error (e.g., 404 means device auth is disabled).
    """
    resp = requests.post(
        DEVICE_USERCODE_URL,
        json={"client_id": CLIENT_ID},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code == 404:
        raise RuntimeError(
            "OpenAI device code auth is not enabled. "
            "Workspace admin must enable 'device code authentication' in ChatGPT security settings."
        )
    resp.raise_for_status()
    return resp.json()


def poll_for_authorization(device_auth_id: str, user_code: str,
                           interval: int = 5, timeout: int = POLL_TIMEOUT) -> dict:
    """Poll OpenAI for authorization after user enters the code.

    Args:
        device_auth_id: From request_device_code().
        user_code: From request_device_code().
        interval: Polling interval in seconds.
        timeout: Maximum wait time in seconds.

    Returns:
        Dict with authorization_code, code_verifier.

    Raises:
        TimeoutError: If user doesn't authorize within timeout.
        RuntimeError: On unexpected error response.
    """
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(interval)
        try:
            resp = requests.post(
                DEVICE_TOKEN_URL,
                json={
                    "device_auth_id": device_auth_id,
                    "user_code": user_code,
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"Device auth poll success, keys: {list(data.keys())}, data: {json.dumps({k: v[:20] + '...' if isinstance(v, str) and len(v) > 20 else v for k, v in data.items()})}")
                return data

            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            error_raw = data.get("error", "")

            # OpenAI returns error as dict {"code": "...", "message": "..."} or as string
            if isinstance(error_raw, dict):
                error_code = error_raw.get("code", "")
                error_msg = error_raw.get("message", "")
            else:
                error_code = str(error_raw)
                error_msg = data.get("error_description", "")

            if error_code in ("authorization_pending", "deviceauth_authorization_pending"):
                continue
            elif error_code in ("slow_down", "deviceauth_slow_down"):
                interval += 5
                logger.debug(f"Slowing down polling to {interval}s")
                continue
            elif error_code in ("expired_token", "deviceauth_expired_token"):
                raise TimeoutError("Device code expired. Please restart the authentication flow.")
            elif error_code == "deviceauth_authorization_unknown":
                # User hasn't entered the code yet, keep waiting
                continue
            else:
                raise RuntimeError(f"Device auth polling error: {error_code} - {error_msg}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Polling request failed: {e}")
            continue

    raise TimeoutError(f"Device authorization timed out after {timeout}s. Please try again.")


def exchange_code_for_tokens(authorization_code: str, code_verifier: str) -> dict:
    """Exchange authorization code for access and refresh tokens via PKCE.

    Returns:
        Dict with access_token, refresh_token, expires_at.
    """
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": authorization_code,
            "code_verifier": code_verifier,
            "redirect_uri": "http://localhost:1455/auth/callback",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        logger.error(f"Token exchange failed (HTTP {resp.status_code}): {resp.text}")
        # Try without redirect_uri as fallback
        logger.info("Retrying token exchange without redirect_uri...")
        resp = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": authorization_code,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error(f"Token exchange retry failed (HTTP {resp.status_code}): {resp.text}")
            resp.raise_for_status()
    data = resp.json()

    expires_in = data.get("expires_in", 3600)
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": int(time.time()) + expires_in,
    }


def refresh_access_token(refresh_token: str) -> dict:
    """Use refresh token to obtain a new access token.

    Returns:
        Dict with access_token, refresh_token (possibly rotated), expires_at.

    Raises:
        RuntimeError: If refresh fails (token expired or revoked).
    """
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed (HTTP {resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    expires_in = data.get("expires_in", 3600)
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_at": int(time.time()) + expires_in,
    }


def run_device_flow() -> dict:
    """Run the full device authorization flow interactively.

    Prints the verification URL and user code, then polls until the user
    authorizes or the flow times out.

    Returns:
        Dict with access_token, refresh_token, expires_at.
    """
    logger.info("Starting OpenAI device authorization flow...")

    # Step 1: Request device code
    device_data = request_device_code()
    device_auth_id = device_data["device_auth_id"]
    user_code = device_data["user_code"]
    interval = int(device_data.get("interval", 5))

    # Step 2: Display instructions
    print("\n" + "=" * 60)
    print("  OpenAI OAuth 인증이 필요합니다")
    print("=" * 60)
    print(f"\n  1. 아래 URL을 브라우저에서 열어주세요:")
    print(f"     {VERIFY_URL}")
    print(f"\n  2. 다음 코드를 입력하세요:")
    print(f"     {user_code}")
    print(f"\n  인증 대기 중... (최대 15분)")
    print("=" * 60 + "\n")

    logger.info(f"Verification URL: {VERIFY_URL}")
    logger.info(f"User code: {user_code}")

    # Step 3: Poll for authorization
    auth_data = poll_for_authorization(device_auth_id, user_code, interval)

    # Step 4: Get tokens — either directly from poll or via code exchange
    if "access_token" in auth_data:
        # Poll returned tokens directly
        logger.info("Got tokens directly from device auth poll")
        expires_in = auth_data.get("expires_in", 3600)
        tokens = {
            "access_token": auth_data["access_token"],
            "refresh_token": auth_data.get("refresh_token", ""),
            "expires_at": int(time.time()) + expires_in,
        }
    elif "authorization_code" in auth_data:
        # Need to exchange authorization code for tokens
        logger.info("Exchanging authorization code for tokens")
        tokens = exchange_code_for_tokens(
            auth_data["authorization_code"],
            auth_data.get("code_verifier", ""),
        )
    else:
        raise RuntimeError(f"Unexpected auth response keys: {list(auth_data.keys())}")

    logger.info("OpenAI OAuth authentication successful!")
    print("\n  ✓ 인증 성공! 토큰이 저장되었습니다.\n")

    return tokens
