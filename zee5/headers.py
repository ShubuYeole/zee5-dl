"""
zee5.headers — Confirmed from two sources:

  1. Chrome network sniff of zee5.com (web, CONFIRMED WORKING):
       device_id, esk, x-z5-guest-token  (= device_id UUID)
       ESK key: gBQaZLiNdGN9UsCKZaloghz9t9StWLSD

  2. CommonHeaderInterceptor.java (Android TV APK):
       X-Z5-Appversion, X-Z5-AppPlatform, X-User-Type etc.
       ESK key: HOBNPuy7H3T5meJJAfyLkJlHaX2dXeEB

Both hit the same auth.zee5.com endpoints.
We use the web-confirmed format for auth calls (sendotp/verifyotp)
and add TV headers for authenticated content calls.
"""
from __future__ import annotations

import base64
import time
import uuid

# Web ESK key — confirmed by decoding sniffed header:
# Base64("deviceId__gBQaZLiNdGN9UsCKZaloghz9t9StWLSD__timestamp")
ESK_KEY_WEB = "gBQaZLiNdGN9UsCKZaloghz9t9StWLSD"

# Android TV APK ESK key (UrlProvider.java)
ESK_KEY_TV  = "HOBNPuy7H3T5meJJAfyLkJlHaX2dXeEB"

_APP_VERSION      = "9.3.5"
_APP_VERSION_CODE = "930500"
_PLATFORM         = "androidtv"


def _make_device_id(seed: str = "") -> str:
    """Stable UUID. Pass a seed to get the same ID across restarts."""
    if seed:
        import hashlib
        return str(uuid.UUID(hashlib.md5(seed.encode()).hexdigest()))
    return str(uuid.uuid4())


def generate_esk(device_id: str, key: str = ESK_KEY_WEB,
                 timestamp_ms: int | None = None) -> str:
    """
    ESK = Base64(deviceId + "__" + key + "__" + epochMillis)
    Confirmed format from decoded sniff:
      "b15efbfd-f66c-41b2-a116-9557c2917c82__gBQaZLiNdGN9UsCKZaloghz9t9StWLSD__1773564040508"
    """
    ts  = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    raw = f"{device_id}__{key}__{ts}"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def auth_headers(device_id: str) -> dict[str, str]:
    """
    Minimal headers for auth endpoints (sendotp, verifyotp, renew).
    Confirmed exactly from Chrome sniff of zee5.com — nothing extra.

    Sniffed request had:
      device_id:         "b15efbfd-f66c-41b2-a116-9557c2917c82"
      esk:               Base64(deviceId + "__" + WEB_KEY + "__" + ts)
      x-z5-guest-token:  same UUID as device_id
      content-type:      application/json
      accept:            application/json
    """
    return {
        "device_id":          device_id,
        "esk":                generate_esk(device_id, key=ESK_KEY_WEB),
        "x-z5-guest-token":   device_id,
        "content-type":       "application/json",
        "accept":             "application/json",
    }


def tv_headers(
    device_id: str,
    access_token: str = "",
    user_type: str = "guest",
    profile_id: str | None = None,
    is_logged_in: bool = False,
) -> dict[str, str]:
    """
    Full Android TV headers for authenticated content API calls.
    From CommonHeaderInterceptor.java — used AFTER login for content APIs.
    Uses TV ESK key.
    """
    h: dict[str, str] = {
        "X-Z5-Appversion":       _APP_VERSION,
        "X-Z5-Appversionnumber": _APP_VERSION_CODE,
        "X-Z5-AppPlatform":      _PLATFORM,
        "X-User-Type":           user_type,
        "X-ACCESS-TOKEN":        access_token,
        "x-z5-device-id":        device_id,
        "device_id":             device_id,
        "esk":                   generate_esk(device_id, key=ESK_KEY_TV),
        "content-type":          "application/json",
        "accept":                "application/json",
    }
    if is_logged_in:
        h["Authorization"] = f"bearer {access_token}"
        if profile_id:
            h["profile-id"] = profile_id
    else:
        h["X-Z5-Guest-Token"] = device_id
    return h


# Keep common_headers as alias for tv_headers (used in authenticated_client)
def common_headers(
    device_id: str,
    access_token: str = "",
    user_type: str = "guest",
    profile_id: str | None = None,
    is_logged_in: bool = False,
    guest_token: str = "",
) -> dict[str, str]:
    return tv_headers(device_id, access_token, user_type, profile_id, is_logged_in)
