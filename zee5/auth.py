"""
zee5.auth — ZEE5 authentication.

Token architecture (confirmed from Chrome network sniff):
  platform_token  → fetched from launchapi.zee5.com at app start
                    goes into: body["x-access-token"]
                    short-lived (~24h), cached in session

  user_jwt        → fetched via OTP/password login
                    goes into: headers["Authorization"] = "bearer {jwt}"
                    expires per ZEE5 server setting, refreshed via /v1/user/renew
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

from .headers import tv_headers, _make_device_id
from .log import log
from .models import (
    SendOtpResult, LoginResponse,
    VerifyPasswordRequest, GenerateDeviceAuthenticationDto,
)
from .session import SessionData, SessionManager
from .urls import (
    SEND_OTP, VERIFY_OTP, LOGIN_EMAIL_PASS,
    DEVICE_CODE_GEN, DEVICE_CODE_LOGIN, TOKEN_REFRESH,
)


# ── Exceptions ────────────────────────────────────────────────────────────

class Zee5AuthError(RuntimeError):
    pass

class OtpError(Zee5AuthError):
    pass

class DeviceCodeExpired(Zee5AuthError):
    pass

class DeviceCodePending(Zee5AuthError):
    pass


# ── HTTP client ───────────────────────────────────────────────────────────

def _client(cookies: httpx.Cookies | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(15.0, connect=8.0),
        cookies=cookies or httpx.Cookies(),
    )

def _raise_for_error(r: httpx.Response) -> None:
    if r.is_success:
        return
    try:
        body = r.json()
        msg  = body.get("message") or body.get("error") or body.get("error_msg") or str(body)
        code = body.get("code", r.status_code)
        raise Zee5AuthError(f"ZEE5 {r.status_code}: {msg} (code={code})")
    except Zee5AuthError:
        raise
    except Exception:
        r.raise_for_status()


# ── Platform token (fetched at app launch) ────────────────────────────────

async def fetch_platform_token(device_id: str, user_jwt: str = "") -> str:
    """
    Fetch ZEE5 guest/platform token.

    CONFIRMED from reference code get_guest_token():
      URL:   https://launchapi.zee5.com/launch
      param: platform_name=androidtv_app
      field: response["platform_token"]["token"]
    """
    url = "https://launchapi.zee5.com/launch"
    params = {
        "ccode":         "IN",
        "lang":          "en",
        "version":       "4",
        "country":       "IN",
        "state":         "MH",
        "translation":   "en",
        "platform_name": "androidtv_app",
        "partner_name":  "",
    }
    log.info("Fetching platform token from launchapi…")
    log.request("GET", url, body=params)
    try:
        async with _client() as c:
            r = await c.get(url, params=params,
                            headers={"Accept": "application/json"})
            log.response(r.status_code,
                         r.json() if r.content else None)
            if r.is_success:
                data = r.json()
                token = (
                    (data.get("platform_token") or {}).get("token") or
                    data.get("token") or
                    data.get("access_token") or ""
                )
                if token:
                    log.success(f"Platform token: {token[:16]}…")
                    return token
                log.warning(f"launchapi OK but no token. Keys: {list(data.keys())}")
    except Exception as e:
        log.warning(f"launchapi error: {e}")

    if user_jwt:
        log.warning("Using user JWT as x-access-token fallback")
        return user_jwt
    return ""

async def send_otp(phone: str, device_id: str | None = None) -> SendOtpResult:
    """POST https://auth.zee5.com/v1/user/sendotp"""
    did = device_id or _make_device_id()

    stripped = phone.lstrip("+")
    phoneno  = stripped if (stripped.startswith("91") and len(stripped) == 12) \
               else "91" + stripped

    log.request("POST", SEND_OTP, body={"phoneno": phoneno})
    async with _client() as c:
        r = await c.post(SEND_OTP, json={"phoneno": phoneno},
                         headers=tv_headers(did))
        log.response(r.status_code, r.json() if r.content else None)
        _raise_for_error(r)
        result = SendOtpResult.model_validate(r.json())
        if result.success:
            log.success(f"OTP dispatched → {result.message}")
        else:
            log.error(f"OTP failed → {result.message}")
        return result


async def verify_otp(
    phone: str,
    otp: str,
    device_id: str | None = None,
) -> tuple[SessionData, httpx.Cookies]:
    """POST https://auth.zee5.com/v1/user/verifyotp"""
    did = device_id or _make_device_id()

    stripped = phone.lstrip("+")
    phoneno  = stripped if (stripped.startswith("91") and len(stripped) == 12) \
               else "91" + stripped

    body = {
        "phoneno":  phoneno,
        "otp":      otp,
        "platform": "androidtv",
        "device":   "tv",
        "version":  "9.3.5",
    }
    log.request("POST", VERIFY_OTP, body=body)
    async with _client() as c:
        r = await c.post(VERIFY_OTP, json=body, headers=tv_headers(did))
        try:
            resp_body = r.json()
        except Exception:
            resp_body = None
        log.response(r.status_code, resp_body)

        if r.status_code in (400, 401) and resp_body:
            code = str(resp_body.get("code", ""))
            if any(k in code.lower() for k in ("otp", "invalid", "expire")):
                raise OtpError("OTP invalid or expired.")

        _raise_for_error(r)
        parsed  = LoginResponse.model_validate(r.json())
        session = _session_from_login(parsed, did)

        # Fetch platform token, falling back to JWT if launchapi fails
        platform_token = await fetch_platform_token(
            did, user_jwt=parsed.token or ""
        )
        session.platform_token = platform_token

        log.success(f"Verified — JWT: {(parsed.token or '')[:16]}…")
        return session, c.cookies


# ── Flow B: Email + Password ──────────────────────────────────────────────

async def login_with_password(
    email: str,
    password: str,
    device_id: str | None = None,
) -> tuple[SessionData, httpx.Cookies]:
    """POST https://auth.zee5.com/v2/user/loginemail"""
    did  = device_id or _make_device_id()
    body = VerifyPasswordRequest(
        email=email, password=password,
        platform="androidtv", device="tv", version="9.3.5",
    ).to_api_dict()

    log.request("POST", LOGIN_EMAIL_PASS,
                body={**body, "password": "***"})
    async with _client() as c:
        r = await c.post(LOGIN_EMAIL_PASS, json=body,
                         headers=tv_headers(did))
        log.response(r.status_code, r.json() if r.content else None)
        _raise_for_error(r)
        parsed  = LoginResponse.model_validate(r.json())
        session = _session_from_login(parsed, did)
        session.platform_token = await fetch_platform_token(
            did, user_jwt=parsed.token or ""
        )
        log.success("Email login successful")
        return session, c.cookies


# ── Flow C: Device Code ───────────────────────────────────────────────────

async def generate_device_code(
    device_name: str = "Ripx TV",
    device_id: str | None = None,
) -> GenerateDeviceAuthenticationDto:
    """POST https://auth.zee5.com/useraction/device/getcode"""
    did = device_id or _make_device_id()
    log.request("POST", DEVICE_CODE_GEN,
                body={"device_name": device_name})
    async with _client() as c:
        r = await c.post(DEVICE_CODE_GEN,
                         params={"device_name": device_name},
                         headers=tv_headers(did))
        log.response(r.status_code, r.json() if r.content else None)
        _raise_for_error(r)
        result = GenerateDeviceAuthenticationDto.model_validate(r.json())
        log.success(f"Device code: {result.device_code}")
        return result


async def poll_device_code(
    device_code: str,
    device_name: str = "Ripx TV",
    device_id: str | None = None,
    interval_secs: int = 7,
    max_retries:   int = 20,
) -> tuple[SessionData, httpx.Cookies]:
    """Poll https://auth.zee5.com/useraction/device/getdeviceuser"""
    import asyncio
    did = device_id or _make_device_id()

    for attempt in range(1, max_retries + 1):
        log.poll(attempt, max_retries, interval_secs)
        async with _client() as c:
            r = await c.post(
                DEVICE_CODE_LOGIN,
                params  = {"device_code": device_code,
                           "device_name": device_name},
                headers = tv_headers(did),
            )
            log.response(r.status_code,
                         r.json() if r.content else None)
            if r.status_code == 202:
                await asyncio.sleep(interval_secs)
                continue
            if r.status_code == 410:
                raise DeviceCodeExpired("Device code expired.")
            _raise_for_error(r)
            parsed  = LoginResponse.model_validate(r.json())
            session = _session_from_login(parsed, did)
            session.platform_token = await fetch_platform_token(did)
            log.success("Device activated!")
            return session, c.cookies

    raise DeviceCodeExpired(f"No activation after {max_retries} attempts.")


# ── Token refresh ─────────────────────────────────────────────────────────

async def refresh_token(session: SessionData) -> SessionData:
    """POST https://auth.zee5.com/v1/user/renew"""
    headers = tv_headers(
        device_id    = session.device_id,
        access_token = session.access_token,
        is_logged_in = True,
    )
    log.info("Refreshing access token…")
    log.request("POST", TOKEN_REFRESH + "?refresh_token=***",
                headers=headers)
    async with _client() as c:
        r = await c.post(TOKEN_REFRESH,
                         params  = {"refresh_token": session.refresh_token},
                         headers = headers)
        log.response(r.status_code,
                     r.json() if r.content else None)
        _raise_for_error(r)
        parsed = LoginResponse.model_validate(r.json())

    log.success("Token refreshed")

    new_session = SessionData(
        access_token   = parsed.token or session.access_token,
        refresh_token  = session.refresh_token,
        uid            = session.uid,
        device_id      = session.device_id,
        expires_at     = _parse_expiry(parsed.expires_in),
        platform_token = session.platform_token,
    )
    return new_session


# ── Authenticated client ──────────────────────────────────────────────────

@asynccontextmanager
async def authenticated_client(
    sm: SessionManager | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """
    Yield a ready-to-use client with TV headers + auto token refresh.
    Refreshes platform_token if missing too.
    """
    sm      = sm or SessionManager()
    session = sm.load()
    if session is None:
        raise Zee5AuthError("Not logged in. Run: ripx login")

    if session.is_expired():
        log.warning(
            f"Token expired at "
            f"{time.strftime('%H:%M:%S', time.localtime(session.expires_at))}"
            " — refreshing…"
        )
        try:
            session = await refresh_token(session)
            sm.save(session, sm.load_cookies())
        except Exception as e:
            raise Zee5AuthError(
                f"Token expired and refresh failed: {e}. "
                "Run: ripx login"
            ) from e

    # Refresh platform token if missing
    if not session.platform_token:
        session.platform_token = await fetch_platform_token(
            session.device_id,
            user_jwt=session.access_token,
        )
        sm.save(session, sm.load_cookies())

    log.debug("Using session", {
        "device_id":      session.device_id[:16] + "…",
        "token_prefix":   (session.access_token or "")[:16] + "…",
        "platform_token": (session.platform_token or "")[:16] + "…",
        "expires_at":     time.strftime(
            "%Y-%m-%d %H:%M",
            time.localtime(session.expires_at)
        ),
    })

    async with httpx.AsyncClient(
        follow_redirects = True,
        timeout          = httpx.Timeout(20.0, connect=8.0),
        cookies          = sm.load_cookies(),
        headers          = tv_headers(
            device_id    = session.device_id,
            access_token = session.access_token,
            is_logged_in = True,
        ),
    ) as client:
        yield client
        sm.save(session, client.cookies)


# ── helpers ───────────────────────────────────────────────────────────────

def _parse_expiry(expires_in: int | None) -> float:
    """
    ZEE5 may return expires_in as:
      - seconds from now (e.g. 3600)
      - Unix timestamp (e.g. 1742389721)
    Detect which by magnitude.
    """
    if not expires_in:
        return time.time() + 3600
    # If > 1_000_000_000 it's already a Unix timestamp
    if expires_in > 1_000_000_000:
        return float(expires_in)
    return time.time() + expires_in


def _session_from_login(
    parsed: LoginResponse,
    device_id: str,
) -> SessionData:
    return SessionData(
        access_token   = parsed.token or "",
        refresh_token  = parsed.refresh_token or "",
        uid            = "",
        device_id      = device_id,
        expires_at     = _parse_expiry(parsed.expires_in),
        platform_token = "",   # filled after login
    )


async def fetch_platform_token_from_web(device_id: str) -> str:
    """
    Try to get the platform token the same way the ZEE5 web app does.
    
    The web token is HS256 signed: {"platform_code":"Web@@!t38712","product_code":"zee5@975"}
    The signing secret is embedded in ZEE5's web JS bundle.
    
    This fetches zee5.com's main JS and tries to extract the secret.
    """
    import re
    log.info("Searching for platform token secret in ZEE5 web JS…")
    try:
        async with _client() as c:
            # Get zee5.com to find the main JS bundle URL
            r = await c.get("https://www.zee5.com/",
                            headers={"User-Agent": "Mozilla/5.0"})
            # Find main JS chunk
            js_urls = re.findall(
                r'src="(https://[^"]*\.js)"', r.text
            )
            # Look for the chunk that likely has auth config
            for url in js_urls:
                if any(k in url for k in ("main", "app", "vendor", "chunk")):
                    js_r = await c.get(url)
                    # Look for the platform token secret pattern
                    # Common patterns: product_code, zee5@, platform_code
                    matches = re.findall(
                        r'product_code["\s:]+["\']([^"\']+)["\']', js_r.text
                    )
                    if matches:
                        log.debug("Found in JS", {"matches": matches[:3]})
    except Exception as e:
        log.warning(f"JS scrape failed: {e}")
    return ""
