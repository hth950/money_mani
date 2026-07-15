"""Application authentication and authorization primitives."""

from web.auth.config import AuthSettings, get_auth_settings
from web.auth.service import AuthenticatedUser

__all__ = ["AuthSettings", "AuthenticatedUser", "get_auth_settings"]
