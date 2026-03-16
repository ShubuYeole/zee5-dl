"""
SessionManager — persists ZEE5 auth state between CLI runs.

Storage layout  (~/.config/ripx/zee5/):
  session.json   — tokens, uid, device_id, expiry  (AES-encrypted)
  cookies.pkl    — httpx CookieJar binary blob
  .key           — Fernet key (chmod 600 on first write)

Why encrypt?  access_token is effectively a password — plain-text on disk
is a bad habit even for personal tools.
"""
from __future__ import annotations

import json
import os
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from cryptography.fernet import Fernet

from .paths import session_file, key_file, cookies_file


# ── path shims (all delegate to paths.py) ─────────────────────────────────

def _key_path()     -> Path: return key_file()
def _session_path() -> Path: return session_file()
def _cookies_path() -> Path: return cookies_file()


# ── encryption helpers ──────────────────────────────────────────────────────

def _get_or_create_key() -> bytes:
    """Load existing Fernet key or generate + save a new one."""
    kp = _key_path()
    if kp.exists():
        return kp.read_bytes()
    key = Fernet.generate_key()
    kp.write_bytes(key)
    kp.chmod(0o600)          # owner read/write only
    return key


def _fernet() -> Fernet:
    return Fernet(_get_or_create_key())


# ── session data ────────────────────────────────────────────────────────────

@dataclass
class SessionData:
    access_token:   str
    refresh_token:  str
    uid:            str
    device_id:      str
    expires_at:     float          # Unix timestamp
    platform_token: str = ""       # ZEE5 app/platform token (x-access-token in SPAPI)

    def is_expired(self, buffer_secs: int = 120) -> bool:
        return time.time() >= (self.expires_at - buffer_secs)

    def to_dict(self) -> dict:
        return {
            "access_token":   self.access_token,
            "refresh_token":  self.refresh_token,
            "uid":            self.uid,
            "device_id":      self.device_id,
            "expires_at":     self.expires_at,
            "platform_token": self.platform_token,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionData":
        return cls(
            access_token   = d["access_token"],
            refresh_token  = d["refresh_token"],
            uid            = d["uid"],
            device_id      = d["device_id"],
            expires_at     = d["expires_at"],
            platform_token = d.get("platform_token", ""),
        )


# ── manager ─────────────────────────────────────────────────────────────────

class SessionManager:
    """
    Thread-safe (enough for a CLI) session store.

    Usage:
        sm = SessionManager()
        if sm.has_session():
            session = sm.load()
        sm.save(session, jar)
        sm.clear()
    """

    def has_session(self) -> bool:
        return _session_path().exists() and _cookies_path().exists()

    def load(self) -> Optional[SessionData]:
        """Decrypt and return saved session, or None if missing/corrupt."""
        sp = _session_path()
        if not sp.exists():
            return None
        try:
            raw     = sp.read_bytes()
            plaintext = _fernet().decrypt(raw)
            return SessionData.from_dict(json.loads(plaintext))
        except Exception:
            # Corrupt or key mismatch — treat as logged out
            return None

    def save(self, session: SessionData, jar: httpx.Cookies) -> None:
        """Encrypt and persist session + cookie jar."""
        # session.json (encrypted)
        plaintext = json.dumps(session.to_dict()).encode()
        _session_path().write_bytes(_fernet().encrypt(plaintext))
        _session_path().chmod(0o600)

        # cookies.pkl (pickle of dict — httpx Cookies is dict-compatible)
        with open(_cookies_path(), "wb") as f:
            pickle.dump(dict(jar), f)
        _cookies_path().chmod(0o600)

    def load_cookies(self) -> httpx.Cookies:
        """Return a populated httpx.Cookies jar from disk, or empty."""
        cp = _cookies_path()
        if not cp.exists():
            return httpx.Cookies()
        with open(cp, "rb") as f:
            raw: dict = pickle.load(f)
        jar = httpx.Cookies()
        for k, v in raw.items():
            jar.set(k, v)
        return jar

    def clear(self) -> None:
        """Delete all saved auth state (logout)."""
        for p in (_session_path(), _cookies_path()):
            if p.exists():
                p.unlink()
