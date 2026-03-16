"""
zee5.paths — All paths anchored to the project root folder.

Project root = the directory containing the `zee5/` package.
If you have:
    D:\\tools\\zee5_auth-github\\
        zee5\\            ← package
        certificate\\     ← certs live here
        device\\          ← .wvd files live here
        download\\        ← output goes here
        tmp\\             ← segment temp files
        session.json      ← encrypted session
        cookies.pkl       ← cookie jar
        .key              ← fernet key
        zee5.yml          ← config

Everything stays in the project root, not %APPDATA%.
"""
from __future__ import annotations

from pathlib import Path


def _root() -> Path:
    """Project root = parent of the zee5 package directory."""
    return Path(__file__).resolve().parent.parent


def root()          -> Path: return _root()
def session_file()  -> Path: return _root() / "session.json"
def key_file()      -> Path: return _root() / ".key"
def cookies_file()  -> Path: return _root() / "cookies.pkl"
def config_file()   -> Path: return _root() / "zee5.yml"

def device_dir()    -> Path:
    d = _root() / "device"
    d.mkdir(exist_ok=True)
    return d

def cert_file()     -> Path:
    """Fixed path: <root>/certificate/zee5_certificate.pem"""
    d = _root() / "certificate"
    d.mkdir(exist_ok=True)
    return d / "zee5_certificate.pem"

def download_dir()  -> Path:
    d = _root() / "download"
    d.mkdir(exist_ok=True)
    return d

# alias — some imports use the plural form
downloads_dir = download_dir

def temp_dir()      -> Path:
    d = _root() / "tmp"
    d.mkdir(exist_ok=True)
    return d


def describe() -> dict[str, str]:
    """Human-readable map of all paths for zee5 status / config."""
    return {
        "root":        str(root()),
        "config":      str(config_file()),
        "session":     str(session_file()),
        "cookies":     str(cookies_file()),
        "device_dir":  str(device_dir()),
        "certificate": str(cert_file()),
        "download":    str(download_dir()),
        "temp":        str(temp_dir()),
    }
