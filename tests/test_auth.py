"""
Tests for ZEE5 auth — all endpoints confirmed from APK smali.
Run: poetry run pytest -v

Uses pytest-httpx to mock all network — no real API calls.
"""
from __future__ import annotations
import time
import pytest
import httpx
from pytest_httpx import HTTPXMock

from zee5.auth import send_otp, verify_otp, login_with_password, Zee5AuthError, OtpError
from zee5.session import SessionData, SessionManager
from zee5.headers import generate_esk, common_headers
from zee5.urls import SEND_OTP, VERIFY_OTP, LOGIN_EMAIL_PASS, TOKEN_REFRESH


# ── send_otp ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_otp_success(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST", url=SEND_OTP,
        json={"code": 200, "message": "OTP sent successfully"},
    )
    result = await send_otp("9876543210")
    assert result.success
    assert result.code == 200


@pytest.mark.asyncio
async def test_send_otp_uses_phoneno_field(httpx_mock: HTTPXMock):
    """Confirmed field name is 'phoneno' not 'mobile' or 'phone'."""
    captured = {}
    def capture(request: httpx.Request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 200})
    httpx_mock.add_callback(capture, method="POST", url=SEND_OTP)
    await send_otp("9876543210")
    assert "phoneno" in captured["body"]
    assert captured["body"]["phoneno"] == "9876543210"
    assert captured["body"].get("platformName") == "androidtv"


@pytest.mark.asyncio
async def test_send_otp_failure(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST", url=SEND_OTP, status_code=400,
        json={"code": "invalid_phone", "message": "Invalid phone"},
    )
    with pytest.raises(Zee5AuthError):
        await send_otp("0000000000")


# ── verify_otp ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_otp_success(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST", url=VERIFY_OTP,
        json={
            "access_token": "tok_abc",
            "refresh_token": "ref_xyz",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    session, cookies = await verify_otp("9876543210", "123456")
    assert session.access_token == "tok_abc"
    assert session.refresh_token == "ref_xyz"
    assert not session.is_expired()


@pytest.mark.asyncio
async def test_verify_otp_uses_correct_fields(httpx_mock: HTTPXMock):
    """Confirmed fields: phoneno, otp, platform, device, version."""
    captured = {}
    def capture(request: httpx.Request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "access_token": "t", "refresh_token": "r",
            "expires_in": 3600, "token_type": "Bearer",
        })
    httpx_mock.add_callback(capture, method="POST", url=VERIFY_OTP)
    await verify_otp("9876543210", "654321")
    b = captured["body"]
    assert b["phoneno"] == "9876543210"
    assert b["otp"] == "654321"
    assert b["platform"] == "androidtv"
    assert b["device"] == "tv"
    assert b["version"] == "9.3.5"


@pytest.mark.asyncio
async def test_verify_otp_expired(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST", url=VERIFY_OTP, status_code=400,
        json={"code": "otp_expired", "message": "OTP expired"},
    )
    with pytest.raises(OtpError):
        await verify_otp("9876543210", "000000")


# ── login_with_password ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_password_success(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST", url=LOGIN_EMAIL_PASS,
        json={"access_token": "tok", "refresh_token": "ref", "expires_in": 3600},
    )
    session, _ = await login_with_password("user@test.com", "pass123")
    assert session.access_token == "tok"


@pytest.mark.asyncio
async def test_login_password_uses_guest_token(httpx_mock: HTTPXMock):
    """Confirmed hardcoded guest_token from VerifyPasswordRequest.java."""
    captured = {}
    def capture(request: httpx.Request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "access_token": "t", "refresh_token": "r", "expires_in": 3600,
        })
    httpx_mock.add_callback(capture, method="POST", url=LOGIN_EMAIL_PASS)
    await login_with_password("u@t.com", "pw")
    assert captured["body"]["guest_token"] == "8ac71050855811eb9365c7b9492c1290"
    assert captured["body"]["platform"] == "androidtv"
    assert captured["body"]["device"] == "tv"


# ── headers ───────────────────────────────────────────────────────────────

def test_esk_format():
    """ESK = Base64(deviceId + '__' + ESK_KEY + '__' + epochMs)"""
    import base64
    did = "test-device-id"
    ts  = 1700000000000
    esk = generate_esk(did, timestamp_ms=ts)
    decoded = base64.b64decode(esk).decode()
    assert decoded.startswith(did + "__")
    assert str(ts) in decoded


def test_common_headers_guest():
    h = common_headers(device_id="dev-1")
    assert h["X-Z5-AppPlatform"] == "androidtv"
    assert h["x-z5-device-id"] == "dev-1"
    assert "X-Z5-Appversion" in h
    assert "Authorization" not in h


def test_common_headers_logged_in():
    h = common_headers(device_id="dev-1", access_token="mytoken", is_logged_in=True)
    assert h["Authorization"] == "bearer mytoken"   # lowercase 'bearer' confirmed


# ── SessionData ───────────────────────────────────────────────────────────

def test_session_not_expired():
    s = SessionData("tok", "ref", "uid", "dev", time.time() + 3600)
    assert not s.is_expired()

def test_session_expired():
    s = SessionData("tok", "ref", "uid", "dev", time.time() - 10)
    assert s.is_expired()

def test_session_buffer():
    """Considered expired 2 minutes before actual expiry."""
    s = SessionData("tok", "ref", "uid", "dev", time.time() + 60)
    assert s.is_expired(buffer_secs=120)


# ── SessionManager round-trip ─────────────────────────────────────────────

def test_session_manager_save_load(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    sm = SessionManager()
    s  = SessionData("access", "refresh", "uid99", "dev-abc", time.time() + 1800)
    sm.save(s, httpx.Cookies())
    loaded = sm.load()
    assert loaded is not None
    assert loaded.access_token  == "access"
    assert loaded.refresh_token == "refresh"
    assert loaded.uid           == "uid99"
    assert loaded.device_id     == "dev-abc"

def test_session_manager_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    sm = SessionManager()
    sm.save(SessionData("a", "b", "c", "d", time.time() + 100), httpx.Cookies())
    sm.clear()
    assert sm.load() is None
