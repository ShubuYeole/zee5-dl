"""zee5_auth — ZEE5 authentication library for ripx."""

from .auth import (
    authenticated_client,
    send_otp, verify_otp,
    login_with_password,
    generate_device_code, poll_device_code,
    refresh_token,
    fetch_platform_token,
    Zee5AuthError, OtpError, DeviceCodeExpired, DeviceCodePending,
)
from .session import SessionManager, SessionData

__all__ = [
    "authenticated_client",
    "send_otp", "verify_otp",
    "login_with_password",
    "generate_device_code", "poll_device_code",
    "refresh_token",
    "fetch_platform_token",
    "Zee5AuthError", "OtpError", "DeviceCodeExpired", "DeviceCodePending",
    "SessionManager", "SessionData",
]
