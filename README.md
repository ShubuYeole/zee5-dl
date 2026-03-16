# zee5

A production-ready CLI tool for downloading movies and shows from ZEE5, with full track selection, Widevine key extraction, and chapter support.

> All API endpoints confirmed from decompiled Android TV APK smali + live network analysis.

---

## Requirements

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11+ | Runtime |
| [Poetry](https://python.poetry.org) | Latest | Dependency management |
| [aria2c](https://aria2.github.io) | Latest | Segment downloading |
| [ffmpeg](https://ffmpeg.org) | Latest | Muxing tracks |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/ShubuYeole/zee5-dl.git
cd zee5-dl
```

### 2. Create a virtual environment and install dependencies

```bash
# Poetry will create .venv automatically in the project folder
poetry config virtualenvs.in-project true
poetry install
```

This creates `.venv/` inside the project and installs the `zee5` command.

### 3. Activate the virtual environment

**Windows:**
```bat
.venv\Scripts\activate
```

**Linux / macOS:**
```bash
source .venv/bin/activate
```

After activation, the `zee5` command is available directly:
```bash
zee5 --help
```

### 4. Install aria2c and ffmpeg

**Windows (winget):**
```bat
winget install aria2.aria2
winget install ffmpeg
```

**Windows (scoop):**
```bat
scoop install aria2 ffmpeg
```

**Linux:**
```bash
sudo apt install aria2 ffmpeg        # Debian / Ubuntu
sudo pacman -S aria2 ffmpeg          # Arch
```

**macOS:**
```bash
brew install aria2 ffmpeg
```

---

## Project layout

After setup, your project folder should look like this:

```
zee5/
├── .venv/                          ← virtual environment (auto-created)
├── zee5/                           ← source package
│   ├── __init__.py
│   ├── auth.py                     ← OTP login, token refresh
│   ├── cli.py                      ← CLI commands
│   ├── config.py                   ← YAML config loader
│   ├── download.py                 ← MPD parser, aria2c, ffmpeg mux
│   ├── headers.py                  ← ESK signing, TV headers
│   ├── log.py                      ← Rich logger
│   ├── models.py                   ← Pydantic DTOs (confirmed from APK)
│   ├── paths.py                    ← all file paths (project-root anchored)
│   ├── session.py                  ← encrypted session storage
│   └── urls.py                     ← all confirmed API endpoints
├── certificate/
│   └── zee5_certificate.pem        ← ← place your Widevine service cert here
├── device/
│   └── your_device.wvd             ← ← place your Widevine device file here
├── download/                       ← finished .mkv files (auto-created)
├── tmp/                            ← download temp segments (auto-created)
├── session.json                    ← encrypted JWT (auto-created on login)
├── cookies.pkl                     ← session cookies (auto-created on login)
├── .key                            ← Fernet encryption key (auto-created)
├── zee5.yml                        ← your config (create with zee5 config --init)
├── pyproject.toml
└── README.md
```

> **Everything stays in the project folder.** No files are written to `%APPDATA%` or `~/.config`.

---

## First-time setup

### Step 1 — Create the config file

```bash
zee5 config --init
```

This writes `zee5.yml` to the project root with all defaults and comments.

### Step 2 — Edit zee5.yml

Open `zee5.yml` and set your device filename:

```yaml
# Widevine device filename inside device/ folder
device_name: "mediatek_smart_tv_26228ff7_8131_l1.wvd"

# aria2c parallel connections per server
connections: 16

# Default audio languages (comma-separated)
default_audio: "hi"

# Default subtitle languages (comma-separated)
default_subs: "en"
```

### Step 3 — Place your Widevine files

```
device/
  └── mediatek_smart_tv_26228ff7_8131_l1.wvd   ← required for DRM
certificate/
  └── zee5_certificate.pem                      ← optional service cert
```

### Step 4 — Verify everything is found

```bash
zee5 config
```

Output:
```
╭─────────────── zee5 config ───────────────╮
│  Config file  zee5.yml
│  device_name  mediatek_smart_tv_...wvd
│  connections  16
│  ...
│  device       device/mediatek...wvd  ✓
│  cert         certificate/zee5_certificate.pem  ✓
╰───────────────────────────────────────────╯
```

### Step 5 — Login

```bash
zee5 login
```

```
  Mobile number (10 digits, no +91): 9876543210
  ✓ OTP sent to +91 9876543210

  Enter OTP: 1234
  ✓ Logged in — expires 2026-03-19 23:35
```

---

## Usage

### Play — inspect a title

```bash
# Full URL
zee5 play https://www.zee5.com/movies/details/tere-naam/0-0-117369

# Content ID only
zee5 play 0-0-117369
```

Shows manifest URLs, stream info (codec, resolution, Dolby/Atmos), and decrypted Widevine keys.

### Download — movies

```bash
# Interactive track selection
zee5 download 0-0-117369

# Specify tracks
zee5 download 0-0-117369 --alang hi --slang en

# HEVC + DolbyVision (falls back gracefully if unavailable)
zee5 download 0-0-117369 -v H265 -r DV --alang hi

# Custom output directory
zee5 download 0-0-117369 -o ~/Downloads

# More connections = faster download
zee5 download 0-0-117369 --conn 32
```

### Download — TV shows

```bash
# Browse episodes interactively
zee5 download https://www.zee5.com/web-series/details/gyaarah-gyaarah/0-6-4z5371966

# Specific episodes using --wanted
zee5 download 0-6-4z5371966 -w S01E01
zee5 download 0-6-4z5371966 -w S01E01-S01E04
zee5 download 0-6-4z5371966 -w S01
zee5 download 0-6-4z5371966 -w S01-S02
zee5 download 0-6-4z5371966 -w all --alang hi --slang en
```

### Download flags

```
  -v, --vcodec   H264|H265         Video codec (default: H265)
  -r, --range    SDR|HDR|HDR10|DV  Color range (default: DV)
  -w, --wanted   SPEC              Episode spec (default: all)
  -al, --alang   LANGS             Audio languages e.g. hi,en,te
  -sl, --slang   LANGS             Subtitle languages e.g. en
  -ns, --no-subs                   Skip subtitle tracks
  -na, --no-audio                  Skip audio tracks
  -nv, --no-video                  Skip video track
  -nc, --no-chapters               Skip chapter markers
  -c,  --conn    N                 aria2c connections (default: 16)
       --keep-temp                 Keep raw segment files
       --dump-spapi                Print raw API response and exit
  -o,  --output  DIR               Output directory
```

### Account commands

```bash
zee5 status        # session info, token expiry, all file paths
zee5 watchlist     # your ZEE5 watchlist
zee5 settings      # account settings
zee5 profiles      # account profiles
zee5 logout        # clear session + cookies
```

### Debug mode

```bash
zee5 -d play 0-0-117369        # full request/response bodies
zee5 -d download 0-0-117369    # verbose download logging
```

Or set the env var permanently:
```bash
set ZEE5_LOG=2     # Windows
export ZEE5_LOG=2  # Linux/macOS
```

---

## Output filenames

| Type | Filename |
|------|----------|
| Movie | `Tere Naam [0-0-117369].mkv` |
| Episode | `Gyaarah Gyaarah S01E01 Episode 1 [0-1-...].mkv` |

All files are MKV containers with:
- Selected video track (HEVC or AVC)
- All selected audio tracks with language tags
- Subtitle tracks (WebVTT, remuxed)
- Chapter markers from skip_available + end_credits data

---

## Confirmed API endpoints

All endpoints confirmed from decompiled Android TV APK smali + live network capture.

| Endpoint | URL |
|----------|-----|
| Send OTP | `auth.zee5.com/v1/user/sendotp` |
| Verify OTP | `auth.zee5.com/v1/user/verifyotp` |
| Platform token | `launchapi.zee5.com/launch` |
| Token refresh | `auth.zee5.com/v1/user/renew` |
| SPAPI manifest | `spapi.zee5.com/singlePlayback/v2/getDetails/secure` |
| Widevine license | `spapi.zee5.com/widevine/getLicense` |
| Watchlist | `user.zee5.com/v2/watchlist` |
| Settings | `user.zee5.com/v1/settings` |
| Profiles | `profiles.zee5.com/v2/profiles` |
| Show metadata | `gwapi.zee5.com/content/tvshow/{id}` |

---

## Token architecture

ZEE5 uses two separate tokens per request:

| Token | How obtained | Where used |
|-------|-------------|------------|
| **User JWT** (RS256) | OTP login | `Authorization: bearer {jwt}` header |
| **Platform token** (HS256) | `launchapi.zee5.com/launch` | `x-access-token` in POST body |

The platform token identifies the Android TV app. The user JWT identifies your account. Both are required for the SPAPI playback endpoint.

---

## Session storage

All files stay in the project root — nothing goes to `%APPDATA%` or `~/.config`.

| File | Contents | Encrypted |
|------|----------|-----------|
| `session.json` | JWT + platform token + expiry | AES (Fernet) |
| `cookies.pkl` | httpx cookie jar | No |
| `.key` | Fernet encryption key | — |

Tokens auto-refresh via `auth.zee5.com/v1/user/renew` — you only need to login again if the refresh token expires (~30 days).

---

## Using as a library

```python
from zee5.auth import authenticated_client, send_otp, verify_otp

# OTP login
result = await send_otp("9876543210")
session, cookies = await verify_otp("9876543210", "1234")

# Authenticated API calls
async with authenticated_client() as client:
    r = await client.get("https://user.zee5.com/v2/watchlist")
    print(r.json())
```

---

## Legal

This tool is for personal use only. Downloading DRM-protected content may violate ZEE5's terms of service. You are responsible for how you use this software.
