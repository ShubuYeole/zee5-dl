"""
zee5.cli — ripx CLI for ZEE5.

  zee5 login              OTP phone login
  ripx logout             Clear session
  ripx status             Session info + token expiry
  ripx play <url>         Fetch manifest + Widevine keys
  ripx download <url>     Download with aria2c + ffmpeg
  ripx watchlist          Your watchlist
  ripx settings           Account settings
  ripx profiles           Account profiles
  ripx config             Show / init YAML config
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import click
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .auth import Zee5AuthError, OtpError, authenticated_client, send_otp, verify_otp
from .config import Zee5Config
from .log import log
from .session import SessionManager

console = Console()
sm      = SessionManager()

# ── Palette ───────────────────────────────────────────────────────────────
C_BRAND   = "bold magenta"
C_OK      = "bold green"
C_WARN    = "bold yellow"
C_ERR     = "bold red"
C_DIM     = "dim"
C_INFO    = "bold cyan"
C_URL     = "cyan"
C_KEY     = "bold magenta"
C_VAL     = "white"

_MANIFEST_PREFERENCE = [
    "manifest-connected-4k.mpd",
    "manifest-4k-connected-hevc-aac.mpd",
    "manifest-connected-hevc.mpd",
    "manifest-connected-ddplus-hevc.mpd",
    "manifest-connected-ddplus-avc.mpd",
    "manifest-high.mpd",
    "manifest-hevc.mpd",
    "manifest-mid-hevc.mpd",
    "manifest-mid.mpd",
    "manifest-phone-ddplus-hevc.mpd",
    "manifest-phone-ddplus-avc.mpd",
    "manifest-phone-hevc.mpd",
    "manifest-phone.mpd",
    "manifest-low.mpd",
]


# ── helpers ───────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)

def _die(msg: str) -> None:
    console.print(f"[{C_ERR}]✗[/{C_ERR}] {msg}")
    raise SystemExit(1)

def _session_dir() -> str:
    from .paths import root
    return str(root())

def _extract_content_id(url_or_id: str) -> str:
    if re.match(r'^[\d]+-[\d]+-\w+$', url_or_id):
        return url_or_id
    path  = url_or_id.rstrip("/").split("?")[0]
    for part in reversed(path.split("/")):
        if re.match(r'^[\d]+-[\d]+-\w+$', part):
            return part
    _die(f"Cannot extract content ID from: {url_or_id}")

def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"

def _fmt_expiry(ts: float) -> str:
    dt  = datetime.fromtimestamp(ts)
    now = datetime.now()
    diff = dt - now
    if diff.total_seconds() < 0:
        return f"[{C_ERR}]Expired[/{C_ERR}]"
    days = diff.days
    hrs  = diff.seconds // 3600
    if days > 0:
        return f"[{C_OK}]{dt.strftime('%Y-%m-%d %H:%M')}[/{C_OK}] [{C_DIM}](in {days}d {hrs}h)[/{C_DIM}]"
    return f"[{C_WARN}]{dt.strftime('%Y-%m-%d %H:%M')}[/{C_WARN}] [{C_DIM}](in {hrs}h)[/{C_DIM}]"

def _kv_table() -> Table:
    t = Table(show_header=False, box=None, padding=(0, 2), show_edge=False)
    t.add_column(style=C_KEY, width=16, no_wrap=True)
    t.add_column(style=C_VAL, overflow="fold")
    return t


# ── capabilities + MPD helpers ────────────────────────────────────────────

def _build_capabilities(range_mode: str = "DV") -> dict:
    if range_mode == "DV":
        dynamic_range = ["SDR", "DolbyVision"]
    elif range_mode == "HDR10":
        dynamic_range = ["SDR", "HDR10+", "HDR"]
    else:
        dynamic_range = ["SDR"]
    return {
        "os_name": "Android", "os_version": "9",
        "platform_name": "ctv_android", "platform_version": "28",
        "device_name": "4K Hisense Android TV", "device_type": "tv",
        "app_name": "ctv_android", "app_version": "5.42.0",
        "player_capabilities": {
            "audio_channel": ["DOLBY_ATMOS"],
            "video_codec":   ["HEVC"],
            "resolution":    ["UHD"],
            "dynamic_range": dynamic_range,
        },
        "security_capabilities": {
            "encryption":              ["WIDEVINE_AES_CTR", "WIDEVINE_AES_CBCS"],
            "widevine_security_level": ["L1"],
            "hdcp_version":            ["HDCP_V2_2"],
        },
    }

def _manifest_range_label(mpd_url: str) -> str:
    m = re.search(r"manifest-([^/?]+)\.mpd", mpd_url)
    name = m.group(1).lower() if m else ""
    if "dv" in name or "dolbyvision" in name: return "DolbyVision"
    if "hdr10+" in name or "hdr10plus" in name: return "HDR10+"
    if "hdr" in name: return "HDR"
    if "sdr" in name: return "SDR"
    return "SDR"

def _summarize_mpd(mpd_text: str, mpd_url: str) -> dict:
    from .download import parse_mpd
    base  = mpd_url.split("?")[0].rsplit("/", 1)[0] + "/"
    trks  = parse_mpd(mpd_text, base)
    max_w = max((t.width  for t in trks if t.kind == "video"), default=0)
    max_h = max((t.height for t in trks if t.kind == "video"), default=0)
    v_codecs = sorted({t.codec for t in trks if t.kind == "video" and t.codec})
    a_codecs = sorted({t.codec for t in trks if t.kind == "audio" and t.codec})
    a_labels = " ".join(
        (t.label or "") + " " + (t.track_id or "")
        for t in trks if t.kind == "audio"
    ).lower()
    dolby = any("ec-3" in c or "ac-3" in c or "eac3" in c for c in a_codecs)
    atmos = "atmos" in a_labels
    return {
        "range":        _manifest_range_label(mpd_url),
        "max_res":      f"{max_w}x{max_h}" if max_w else "—",
        "video_codecs": ", ".join(v_codecs) or "—",
        "audio_codecs": ", ".join(a_codecs) or "—",
        "dolby":        dolby,
        "atmos":        atmos,
    }

def _build_mpd_candidates(mpd_url: str) -> list[str]:
    m = re.search(r"manifest-[^/]+\.mpd", mpd_url)
    if not m:
        return [mpd_url]
    base = mpd_url[:m.start()]
    tail = mpd_url[m.end():]
    return [base + name + tail for name in _MANIFEST_PREFERENCE] + [mpd_url]

def _score_manifest(url: str) -> tuple[int, int]:
    m    = re.search(r"manifest-([^/?]+)\.mpd", url)
    name = m.group(1).lower() if m else ""
    r = 4 if ("dv" in name) else 3 if ("hdr10+" in name or "hdr10plus" in name) \
        else 2 if "hdr" in name else 1 if "sdr" in name else 0
    q = 2 if ("4k" in name or "2160" in name) else 1 if ("fhd" in name or "high" in name) else 0
    return (r, q)

async def _select_mpd_url(mpd_url: str) -> str:
    import httpx as _httpx
    candidates = _build_mpd_candidates(mpd_url)
    available: list[str] = []
    async with _httpx.AsyncClient(follow_redirects=True, timeout=10.0) as c:
        for url in candidates:
            try:
                r = await c.head(url)
                if r.status_code == 200:
                    available.append(url)
            except Exception:
                continue
    if available:
        return max(available, key=_score_manifest)
    return mpd_url


# ── Content type detection ────────────────────────────────────────────────
# ZEE5 content ID prefixes:
#   0-0-  movie
#   0-6-  show (needs episode resolution)
#   0-3-  season (needs episode resolution)
#   0-1-  episode (direct SPAPI call)

def _is_show(content_id: str) -> bool:
    return content_id.startswith("0-6-") or content_id.startswith("0-3-")

async def _fetch_show_episodes(show_id: str, session) -> list[dict]:
    """
    Fetch all episodes for a show from gwapi.zee5.com.
    Returns list of episode dicts with id, title, season, episode_number.
    """
    import httpx as _httpx

    headers = {
        "Authorization":    f"bearer {session.access_token}",
        "X-ACCESS-TOKEN":   session.platform_token or session.access_token,
        "X-User-Type":      "premium",
        "X-Z5-AppPlatform": "Android TV",
        "X-Z5-Appversion":  "5.40.0",
    }
    params = {
        "country":     "IN",
        "languages":   "hi,en,mr,te,ta,kn,ml",
        "translation": "en",
        "platform":    "con_devices",
        "version":     "14",
    }

    log.info(f"Fetching show metadata…")
    async with _httpx.AsyncClient(follow_redirects=True, timeout=20.0) as c:
        r = await c.get(
            f"https://gwapi.zee5.com/content/tvshow/{show_id}",
            params=params, headers=headers,
        )
        r.raise_for_status()
        show_data = r.json()

    show_title = show_data.get("title", show_id)
    seasons    = show_data.get("seasons", [])
    episodes: list[dict] = []

    for season in seasons:
        season_num = season.get("orderid", 0)
        season_id  = season.get("id", "")
        # Episodes may be embedded or need a separate call
        for ep in season.get("episodes", []):
            episodes.append({
                "id":             ep.get("id", ""),
                "title":          ep.get("title") or ep.get("original_title", ""),
                "show_title":     show_title,
                "season":         season_num,
                "episode_number": ep.get("episode_number", 0),
                "duration":       ep.get("duration", 0),
                "release_date":   (ep.get("release_date") or "")[:10],
            })

    # If episodes not embedded, fetch per season
    if not episodes and seasons:
        async with _httpx.AsyncClient(follow_redirects=True, timeout=20.0) as c:
            for season in seasons:
                season_num = season.get("orderid", 0)
                ep_params  = {**params, "type": "episode", "asset_subtype": "tvshow",
                              "season_id": season.get("id", ""), "limit": "100"}
                page = 0
                url  = "https://gwapi.zee5.com/content/tvshow/"
                while url:
                    ep_params["page"] = page
                    er = await c.get(url, params=ep_params, headers=headers)
                    if not er.is_success:
                        break
                    ed = er.json()
                    for ep in ed.get("episode", []):
                        episodes.append({
                            "id":             ep.get("id", ""),
                            "title":          ep.get("title") or ep.get("original_title", ""),
                            "show_title":     show_title,
                            "season":         season_num,
                            "episode_number": ep.get("episode_number", 0),
                            "duration":       ep.get("duration", 0),
                            "release_date":   (ep.get("release_date") or "")[:10],
                        })
                    nxt = ed.get("next_episode_api", "")
                    url = nxt.split("?")[0] if nxt else None
                    page += 1

    return sorted(episodes, key=lambda e: (e["season"], e["episode_number"]))


def _select_episode(episodes: list[dict],
                    batch: bool = False) -> "dict | list[dict]":
    """
    Show episode browser.
    Returns single dict normally, or list[dict] when batch=True.
    """
    if not episodes:
        _die("No episodes found for this show.")

    seasons: dict[int, list[dict]] = {}
    for ep in episodes:
        seasons.setdefault(ep["season"], []).append(ep)
    season_nums = sorted(seasons.keys())
    show_title  = episodes[0].get("show_title", "Show")

    console.print()
    console.print(Rule(f"[bold magenta]{show_title}[/bold magenta]", style="magenta"))

    # Season selection
    if len(season_nums) > 1:
        console.print(
            "\n  [bold]Seasons:[/bold]  " +
            "  ".join(f"[cyan]{s}[/cyan]" for s in season_nums) +
            "  [dim](or all)[/dim]"
        )
        raw_s = Prompt.ask("  Select season", default=str(season_nums[0]))
        sel_seasons = season_nums if raw_s.strip().lower() == "all"                       else [int(raw_s.strip())]
    else:
        sel_seasons = season_nums

    sel_eps: list[dict] = []
    for sn in sel_seasons:
        sel_eps += seasons.get(sn, [])

    # Episode table per season
    for sn in sel_seasons:
        eps = seasons.get(sn, [])
        console.print(f"\n  [bold]Season {sn} — {len(eps)} episodes:[/bold]")
        t = Table(box=None, padding=(0, 2), show_edge=False)
        t.add_column("Ep",       style="bold cyan", width=4,  no_wrap=True)
        t.add_column("Title",    style="white",     overflow="fold")
        t.add_column("Duration", style="dim",       width=10, no_wrap=True)
        t.add_column("Release",  style="dim",       width=12, no_wrap=True)
        for ep in eps:
            dur = _fmt_duration(ep["duration"]) if ep["duration"] else "—"
            t.add_row(str(ep["episode_number"]), ep["title"],
                      dur, ep["release_date"] or "—")
        console.print(t)

    if batch:
        raw_e = Prompt.ask(
            "  Episodes [dim](1,2,3  or  1-5  or  all)[/dim]",
            default="all",
        )
        chosen = _parse_episode_range(raw_e.strip(), sel_eps)
        console.print(
            f"  [{C_OK}]✓[/{C_OK}] [bold]{len(chosen)} episode(s) queued[/bold]"
        )
        console.print()
        return chosen
    else:
        raw_e = Prompt.ask("  Select episode", default="1")
        ep_num = int(raw_e.strip())
        selected = next(
            (ep for ep in sel_eps if ep["episode_number"] == ep_num),
            sel_eps[0],
        )
        console.print(
            f"  [{C_OK}]✓[/{C_OK}] "
            f"[bold]S{selected['season']:02d}E{selected['episode_number']:02d}[/bold]"
            f" — {selected['title']}"
        )
        console.print()
        return selected


def _parse_episode_range(raw: str, episodes: list[dict]) -> list[dict]:
    """Parse '1,3,5-8' or 'all' into episode dicts."""
    if raw.lower() == "all":
        return list(episodes)
    nums: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            nums.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            nums.add(int(part))
    return sorted(
        [ep for ep in episodes if ep["episode_number"] in nums],
        key=lambda e: (e["season"], e["episode_number"]),
    )


def _parse_wanted(spec: str, episodes: list[dict]) -> list[dict]:
    """
    Parse --wanted spec into episode list.

    Supports:
      all                    → every episode
      S01                    → full season 1
      S01-S03                → seasons 1 through 3
      S01E01                 → single episode
      S01E01-S01E04          → episode range within season
      S01E01-S02E03          → cross-season range
      S01E01,S02E03,S03E01   → explicit list
    """
    if spec.lower() == "all":
        return list(episodes)

    def ep_key(s: int, e: int) -> int:
        return s * 1000 + e

    def parse_token(tok: str) -> tuple[int, int] | None:
        """Return (season, episode) or (season, 0) for season-only."""
        tok = tok.strip().upper()
        m = re.match(r"S(\d+)E(\d+)$", tok)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.match(r"S(\d+)$", tok)
        if m:
            return int(m.group(1)), 0
        return None

    result: list[dict] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part and part.upper().startswith("S"):
            # Range: S01-S03 or S01E01-S02E03
            halves = part.split("-", 1)
            start_tok = parse_token(halves[0])
            end_tok   = parse_token(halves[1])
            if start_tok and end_tok:
                s_start, e_start = start_tok
                s_end,   e_end   = end_tok
                start_key = ep_key(s_start, e_start)
                end_key   = ep_key(s_end, e_end if e_end else 999)
                for ep in episodes:
                    k = ep_key(ep["season"], ep["episode_number"])
                    if start_key <= k <= end_key:
                        result.append(ep)
        else:
            tok = parse_token(part)
            if tok:
                s, e = tok
                if e == 0:
                    # Whole season
                    result += [ep for ep in episodes if ep["season"] == s]
                else:
                    result += [ep for ep in episodes
                               if ep["season"] == s and ep["episode_number"] == e]

    # Deduplicate preserving order
    seen = set()
    deduped = []
    for ep in result:
        k = (ep["season"], ep["episode_number"])
        if k not in seen:
            seen.add(k)
            deduped.append(ep)
    return sorted(deduped, key=lambda e: (e["season"], e["episode_number"]))


async def _spapi_call(content_id: str, session,
                      show_id: str = "") -> dict:
    import base64, json as _j, httpx as _httpx
    jwt   = session.access_token
    pt    = session.platform_token or jwt
    xdd   = base64.b64encode(
        _j.dumps(_build_capabilities("DV"), separators=(",", ":")).encode()
    ).decode()
    r = await _httpx.AsyncClient(follow_redirects=True,
                                  timeout=20.0).__aenter__()
    # Use a simpler direct approach
    async with _httpx.AsyncClient(follow_redirects=True, timeout=20.0) as c:
        resp = await c.post(
            "https://spapi.zee5.com/singlePlayback/v2/getDetails/secure",
            params={
                "content_id": content_id, "show_id": show_id,
                "device_id": session.device_id, "session_id": "",
                "check_parental_control": "false", "current_parental_control": "",
                "country": "IN", "platform_name": "ctv_android", "state": "IN",
                "translation": "en", "display_language": "en",
                "user_language": "hi,en", "collection_id": "",
                "app_version": "5.42.0", "gender": "", "age": "",
                "brand": "ripx", "model": "ripx", "version": "12",
            },
            json={
                "x-access-token":   pt,
                "Authorization":    f"bearer {jwt}",
                "X-Z5-Guest-Token": "",
                "x-dd-token":       xdd,
            },
            headers={
                "Authorization":    f"bearer {jwt}",
                "Content-Type":     "application/json; charset=utf-8",
                "X-ACCESS-TOKEN":   pt,
                "X-User-Type":      "premium",
                "X-Z5-AppPlatform": "Android TV",
                "X-Z5-Appversion":  "5.40.0",
                "profile-id":       "df717b00-4c56-431e-8fa7-3e885a3f47b5",
            },
        )
    log.request("POST", "https://spapi.zee5.com/…", body={"content_id": content_id})
    log.response(resp.status_code)
    resp.raise_for_status()
    return resp.json()


# ── CLI group ─────────────────────────────────────────────────────────────

@click.group(context_settings={"help_option_names": ["-h", "--help"],
                                "max_content_width": 120})
@click.option("--config", "-C", default=None, metavar="FILE",
              help="Path to zee5.yml config file")
@click.option("--debug",  "-d", is_flag=True,
              help="Enable debug output (full request/response bodies)")
@click.version_option("1.0.0", prog_name="zee5")
@click.pass_context
def cli(ctx: click.Context, config: str | None, debug: bool) -> None:
    """
    \b
    ╔══════════════════════════════╗
    ║  zee5  —  ZEE5 Downloader   ║
    ╚══════════════════════════════╝

    \b
    Download movies and shows from ZEE5 with full track
    selection, Widevine key extraction, and chapter support.

    \b
    Quick start:
      zee5 login
      zee5 play   https://www.zee5.com/movies/details/tere-naam/0-0-117369
      zee5 download 0-0-117369 --alang hi --slang en
    """
    ctx.ensure_object(dict)
    cfg = Zee5Config.load(config)
    ctx.obj["config"] = cfg
    if debug:
        import zee5.log as _lm
        _lm.VERBOSITY = 2
    # Show loaded device on every invocation (quiet, one line)
    if ctx.invoked_subcommand not in (None, "config", "status", "login"):
        dev = cfg.resolved_device_path()
        if dev.exists():
            console.print(
                f"[dim]  device  {dev.name}[/dim]",
                highlight=False,
            )
        else:
            console.print(
                f"[yellow]  ⚠  device not found: {dev}[/yellow]"
            )


# ── login ─────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--phone", "-p", default=None, metavar="NUMBER",
              help="10-digit mobile number (no +91)")
def login(phone: str | None) -> None:
    """Login with phone OTP."""
    if sm.has_session():
        s = sm.load()
        if s and not s.is_expired():
            console.print(f"[{C_OK}]✓ Already logged in.[/{C_OK}] "
                          f"[{C_DIM}]Run[/{C_DIM}] [bold]zee5 logout[/bold] "
                          f"[{C_DIM}]to re-login.[/{C_DIM}]")
            return

    console.print()
    console.print(Rule("[bold magenta]ZEE5 Login[/bold magenta]", style="magenta"))
    console.print()

    phone = phone or Prompt.ask(f"  [{C_INFO}]Mobile number[/{C_INFO}] [dim](10 digits, no +91)[/dim]")
    if not phone.isdigit() or len(phone) < 10:
        _die("Enter a valid 10-digit mobile number.")

    console.print(f"\n  [{C_DIM}]Sending OTP to[/{C_DIM}] [bold]+91 {phone}[/bold]…")

    async def _do() -> None:
        result = await send_otp(phone)
        if not result.success:
            _die(f"OTP send failed: {result.message}")
        console.print(f"  [{C_OK}]✓[/{C_OK}] OTP sent to +91 {phone}\n")
        otp = Prompt.ask(f"  [{C_INFO}]Enter OTP[/{C_INFO}]")
        if not otp.isdigit():
            _die("OTP must be digits only.")

        console.print(f"\n  [{C_DIM}]Verifying…[/{C_DIM}]")
        session, cookies = await verify_otp(phone, otp)
        sm.save(session, cookies)

        console.print()
        from .paths import root as _root
        exp = datetime.fromtimestamp(session.expires_at).strftime("%Y-%m-%d %H:%M")
        console.print(Panel(
            f"  [bold green]●[/bold green] Logged in successfully\n"
            f"  [dim]Token expires:[/dim]  [yellow]{exp}[/yellow]\n"
            f"  [dim]Session path:[/dim]   [dim]{_root()}[/dim]",
            title="[bold magenta]zee5[/bold magenta]",
            border_style="magenta",
            padding=(0, 1),
        ))
        console.print()

    try:
        _run(_do())
    except (OtpError, Zee5AuthError) as e:
        _die(str(e))


# ── logout / status ───────────────────────────────────────────────────────

@cli.command()
def logout() -> None:
    """Clear saved session."""
    sm.clear()
    console.print(f"[{C_OK}]✓[/{C_OK}] Logged out.")


@cli.command()
def status() -> None:
    """Show session status, token info, and all storage paths."""
    from .paths import describe as _paths_describe
    session = sm.load()
    if not session:
        console.print(f"[{C_ERR}]Not logged in.[/{C_ERR}] Run [bold]zee5 login[/bold].")
        raise SystemExit(1)

    expired = session.is_expired()
    state   = f"[{C_ERR}]● Expired[/{C_ERR}]" if expired else f"[{C_OK}]● Active[/{C_OK}]"

    t = _kv_table()
    t.add_row("Status",   state)
    t.add_row("Expires",  _fmt_expiry(session.expires_at))
    t.add_row("JWT",      (session.access_token or "")[:32] + "…")
    t.add_row("Platform", ((session.platform_token or "")[:32] + "…"
                           if session.platform_token
                           else f"[{C_WARN}](none)[/{C_WARN}]"))
    t.add_row("Device",   session.device_id)

    p = _paths_describe()
    t.add_row("", "")
    t.add_row("[dim]Root[/dim]",        f"[dim]{p['root']}[/dim]")
    t.add_row("[dim]Config[/dim]",      f"[dim]{p['config']}[/dim]")
    t.add_row("[dim]Session[/dim]",     f"[dim]{p['session']}[/dim]")
    t.add_row("[dim]Cookies[/dim]",     f"[dim]{p['cookies']}[/dim]")
    t.add_row("[dim]Device dir[/dim]",  f"[dim]{p['device_dir']}[/dim]")
    t.add_row("[dim]Certificate[/dim]", f"[dim]{p['certificate']}[/dim]")
    t.add_row("[dim]Download[/dim]",    f"[dim]{p['download']}[/dim]")
    t.add_row("[dim]Temp[/dim]",        f"[dim]{p['temp']}[/dim]")

    console.print()
    console.print(Panel(t, title="[bold]zee5 status[/bold]",
                        border_style="magenta", padding=(0, 1)))
    console.print()

    if expired:
        console.print(
            f"  [{C_WARN}]⚠  Token expired.[/{C_WARN}] "
            f"Run [bold]zee5 login[/bold] to refresh.\n"
        )


# ── config ────────────────────────────────────────────────────────────────

@cli.command("config")
@click.option("--init", is_flag=True, help="Create zee5.yml with defaults")
@click.pass_context
def config_cmd(ctx: click.Context, init: bool) -> None:
    """
    Show current config and resolved file paths.

    \b
    First time setup:
      1. zee5 config --init        create zee5.yml
      2. Edit zee5.yml             set device_name
      3. Copy your .wvd file to the devices/ folder shown below
    """
    from .paths import config_file, root, device_dir, cert_file, download_dir, temp_dir

    cfg  = ctx.obj["config"]
    path = config_file()

    t = _kv_table()
    t.add_row("[dim]Config file[/dim]",
              (f"[dim]{path}[/dim]" if path.exists()
               else f"[yellow]{path}[/yellow] [dim](not found — run zee5 config --init)[/dim]"))
    t.add_row("", "")
    t.add_row("output_dir",    str(cfg.resolved_output_dir()))
    t.add_row("connections",   str(cfg.connections))
    t.add_row("default_audio", cfg.default_audio or "[dim](ask interactively)[/dim]")
    t.add_row("default_subs",  cfg.default_subs  or "[dim](ask interactively)[/dim]")
    t.add_row("profile_id",    cfg.profile_id    or "[dim](default profile)[/dim]")
    t.add_row("", "")

    dev  = cfg.resolved_device_path()
    cert = cfg.resolved_cert_path()
    dev_tag  = "bold green" if dev.exists()  else "bold red"
    cert_tag = "bold green" if cert.exists() else "bold red"
    t.add_row("device",
              f"[dim]{dev}[/dim]  "
              f"[{dev_tag}]{'✓' if dev.exists() else '✗ not found'}[/{dev_tag}]")
    t.add_row("cert",
              f"[dim]{cert}[/dim]  "
              f"[{cert_tag}]{'✓' if cert.exists() else '✗ not found'}[/{cert_tag}]")

    console.print()
    console.print(Panel(t, title="[bold]zee5 config[/bold]",
                        border_style="magenta", padding=(0, 1)))
    console.print()

    if init:
        saved = cfg.save()
        console.print(f"[{C_OK}]✓[/{C_OK}] Config written → [cyan]{saved}[/cyan]")
        console.print(f"\n  [dim]Next steps:[/dim]")
        console.print(f"  1. Place device file in: [cyan]{device_dir()}[/cyan]")
        console.print(f"  2. Edit zee5.yml and set [cyan]device_name[/cyan] to your .wvd filename")
        console.print(f"  3. Place cert at:        [cyan]{cert_file()}[/cyan]\n")
    elif not path.exists():
        console.print(
            f"  [{C_WARN}]Tip:[/{C_WARN}] No config file. "
            f"Run [bold]zee5 config --init[/bold] to create one.\n"
        )



# ── play ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("url", metavar="URL_OR_ID")
@click.option("--dump", is_flag=True, help="Print raw SPAPI JSON")
@click.option("--save", is_flag=True, help="Save SPAPI response to .json file")
@click.pass_context
def play(ctx: click.Context, url: str, dump: bool, save: bool) -> None:
    """
    Show manifest info and Widevine keys for a ZEE5 title.

    \b
    Examples:
      zee5 play https://www.zee5.com/movies/details/tere-naam/0-0-117369
      ripx play 0-0-117369
      ripx play 0-0-117369 --dump
    """
    cfg        = ctx.obj["config"]
    content_id = _extract_content_id(url)
    log.debug(f"Content ID: {content_id}")

    async def _fetch() -> None:
        session = sm.load()
        if session is None:
            _die("Not logged in. Run: zee5 login")

        # Resolve show → episode
        actual_id = content_id
        show_id   = ""
        if _is_show(content_id):
            episodes  = await _fetch_show_episodes(content_id, session)
            episode   = _select_episode(episodes)
            actual_id = episode["id"]
            show_id   = content_id

        data = await _spapi_call(actual_id, session, show_id=show_id)

        if dump:
            console.print_json(data=data)
            return

        if save:
            out = Path(f"zee5_{content_id}.json")
            out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            console.print(f"[{C_OK}]✓[/{C_OK}] Saved → [cyan]{out.resolve()}[/cyan]")
            return

        asset  = data.get("assetDetails") or {}
        key_os = data.get("keyOsDetails") or {}

        _print_asset_card(actual_id, asset, key_os)

        # MPD summary
        mpd_url = (asset.get("video_url") or {}).get("mpd", "")
        if mpd_url:
            mpd_url = await _select_mpd_url(mpd_url)

        if mpd_url:
            import httpx as _httpx
            async with _httpx.AsyncClient(follow_redirects=True, timeout=30) as c2:
                mpd_resp = await c2.get(mpd_url)
                if mpd_resp.is_success:
                    mpd_text = mpd_resp.text
                    _print_mpd_card(mpd_text, mpd_url)

                    # Widevine keys
                    nl   = key_os.get("nl", "")
                    sdrm = key_os.get("sdrm", "")
                    is_drm = bool(asset.get("is_drm", 0))
                    if is_drm and nl and sdrm:
                        _acquire_and_print_keys(
                            mpd_text, mpd_url, nl, sdrm, cfg
                        )

    try:
        _run(_fetch())
    except Zee5AuthError as e:
        _die(str(e))


def _print_asset_card(content_id: str, asset: dict, key_os: dict) -> None:
    title       = asset.get("title", content_id)
    duration_s  = asset.get("duration", 0)
    release     = (asset.get("release_date") or "")[:10]
    is_drm      = bool(asset.get("is_drm", 0))
    multi_audio = asset.get("is_multi_audio", False)
    skip        = asset.get("skip_available") or {}
    sub_langs   = ", ".join(asset.get("subtitle_languages") or []) or "—"
    audio_langs = ", ".join(asset.get("audio_languages") or []) or "—"
    video_url   = asset.get("video_url") or {}
    dash        = video_url.get("mpd", "")
    hls_hevc    = video_url.get("m3u8", "")
    hls_signed  = asset.get("hls_url", "")
    nl          = key_os.get("nl", "")
    sdrm        = key_os.get("sdrm", "")
    lic_dur     = key_os.get("licenseDuration", 0)
    play_dur    = key_os.get("playbackDuration", 0)

    t = _kv_table()
    t.add_row("Title",    f"[bold]{title}[/bold]")
    t.add_row("ID",       content_id)
    t.add_row("Duration", _fmt_duration(duration_s))
    t.add_row("Release",  release)
    t.add_row("Audio",    audio_langs)
    t.add_row("Subs",     sub_langs)
    if multi_audio:
        t.add_row("Multi-audio", f"[{C_OK}]Yes[/{C_OK}]")
    drm_str = f"[{C_ERR}]Widevine L1[/{C_ERR}]" if is_drm else f"[{C_OK}]Clear[/{C_OK}]"
    t.add_row("DRM",      drm_str)
    if skip:
        t.add_row("Skip intro",
                  f"{skip.get('intro_start_s','')} → {skip.get('intro_end_s','')}")
    if lic_dur:
        t.add_row("License",
                  f"{lic_dur // 3600}h  [{C_DIM}](play: {play_dur // 3600}h)[/{C_DIM}]")

    console.print()
    console.print(Panel(t, title=f"[bold magenta]{title}[/bold magenta]",
                        border_style="magenta", padding=(0, 1)))

    # URL block — separate from the table so they're easy to copy
    if dash or hls_hevc:
        console.print()
        if dash:
            console.print(f"  [{C_DIM}]DASH[/{C_DIM}]")
            console.print(f"  [{C_URL}]{dash}[/{C_URL}]")
        if hls_hevc:
            console.print(f"\n  [{C_DIM}]HLS[/{C_DIM}]")
            console.print(f"  [{C_URL}]{hls_hevc}[/{C_URL}]")
        if nl and sdrm:
            console.print(f"\n  [{C_DIM}]DRM license → POST https://spapi.zee5.com/widevine/getLicense[/{C_DIM}]")
            console.print(f"  [{C_DIM}]nl:         {nl}[/{C_DIM}]")
            console.print(f"  [{C_DIM}]customData: {sdrm}[/{C_DIM}]")
        console.print()


def _print_mpd_card(mpd_text: str, mpd_url: str) -> None:
    try:
        summary = _summarize_mpd(mpd_text, mpd_url)
    except Exception:
        return

    t = _kv_table()
    range_val = summary["range"]
    range_col = {"DolbyVision": C_OK, "HDR10+": C_OK, "HDR": C_WARN}.get(range_val, C_DIM)
    t.add_row("Range",   f"[{range_col}]{range_val}[/{range_col}]")
    t.add_row("Max res", summary["max_res"])
    t.add_row("Video",   summary["video_codecs"])
    t.add_row("Audio",   summary["audio_codecs"])
    dolby_str = f"[{C_OK}]Yes[/{C_OK}]" if summary["dolby"] else "No"
    atmos_str = f"[{C_OK}]Yes[/{C_OK}]" if summary["atmos"] else "No"
    t.add_row("Dolby",   dolby_str)
    t.add_row("Atmos",   atmos_str)

    console.print(Panel(t, title="[bold]Stream info[/bold]",
                        border_style="blue", padding=(0, 1)))
    console.print()


def _acquire_and_print_keys(mpd_text: str, mpd_url: str,
                             nl: str, sdrm: str, cfg) -> None:
    try:
        from .download import _acquire_widevine_license

        device_path = cfg.resolved_device_path()
        cert_file   = cfg.resolved_cert_path()

        keys = _acquire_widevine_license(
            mpd_text=mpd_text,
            nl=nl,
            customdata=sdrm,
            device_path=device_path,
            license_url="https://spapi.zee5.com/widevine/getLicense",
            cert_path=cert_file,
        )
    except Exception as e:
        log.warning(f"Widevine license skipped: {e}")
        return

    if not keys:
        return

    t = Table("Type", "KID", "Key",
              box=box.SIMPLE, padding=(0, 1),
              header_style="bold magenta", show_edge=False)
    for ktype, kid_hex, key_hex in keys:
        t.add_row(ktype, kid_hex, f"[{C_URL}]{key_hex}[/{C_URL}]")

    console.print(Panel(t, title="[bold]Widevine Keys[/bold]",
                        border_style="blue", padding=(0, 1)))
    console.print()


# ── download ──────────────────────────────────────────────────────────────

@cli.command()
@click.argument("url", metavar="URL_OR_ID")
@click.option("--output",      "-o",  default=".",  metavar="DIR",
              help="Output directory")
@click.option("--vcodec",      "-v",  default="H265", metavar="CODEC",
              help="Video codec: H264 or H265 (HEVC). Default: H265")
@click.option("--range",       "-r",  "range_",  default="DV", metavar="RANGE",
              help="Color range: SDR, HDR, HDR10, DV. Default: DV")
@click.option("--wanted",      "-w",  default="all", metavar="SPEC",
              help="Episodes: S01-S05, S01E01-S02E03, S02E01,S02E03, all")
@click.option("--vlang",       "-vl", default="",   metavar="LANG",
              help="Preferred video language")
@click.option("--alang",       "-al", default="",   metavar="LANGS",
              help="Audio languages, comma-separated e.g. hi,en")
@click.option("--slang",       "-sl", default="",   metavar="LANGS",
              help="Subtitle languages, comma-separated e.g. en")
@click.option("--no-subs",     "-ns", is_flag=True,
              help="Do not download subtitle tracks")
@click.option("--no-audio",    "-na", is_flag=True,
              help="Do not download audio tracks")
@click.option("--no-video",    "-nv", is_flag=True,
              help="Do not download video tracks")
@click.option("--no-chapters", "-nc", is_flag=True,
              help="Do not add chapter markers")
@click.option("--conn",        "-c",  default=16, type=int, metavar="N",
              help="aria2c parallel connections. Default: 16")
@click.option("--keep-temp",   is_flag=True,
              help="Keep raw segment files after mux")
@click.option("--dump-spapi",  is_flag=True,
              help="Print raw SPAPI JSON and exit")
@click.pass_context
def download(ctx: click.Context, url: str, output: str,
             vcodec: str, range_: str, wanted: str,
             vlang: str, alang: str, slang: str,
             no_subs: bool, no_audio: bool, no_video: bool,
             no_chapters: bool, conn: int,
             keep_temp: bool, dump_spapi: bool) -> None:
    """
    Download a ZEE5 title with full track and episode control.

    \b
    Requires: aria2c and ffmpeg on PATH

    \b
    Examples:
      ripx download 0-0-117369
      ripx download 0-6-4z5371966 -w S01E01-S01E04
      ripx download 0-6-4z5371966 -w all --alang hi --slang en
      ripx download 0-0-117369 -v H265 -r DV --conn 32
      ripx download 0-0-117369 --no-subs --no-chapters
    """
    from .download import download_content
    cfg = ctx.obj["config"]

    content_id = _extract_content_id(url)
    out_dir    = Path(output) if output != "." else cfg.resolved_output_dir()
    conn       = conn   if conn   != 16  else cfg.connections
    alang_str  = alang  or cfg.default_audio
    slang_str  = slang  or cfg.default_subs

    # Build dynamic range from vcodec + range flags
    if vcodec.upper() == "H265":
        range_mode = range_.upper()   # DV / HDR10 / HDR / SDR
    else:
        range_mode = "SDR"

    # Parse audio/sub lang lists
    a_langs: list[str] | None = (
        None if no_audio else
        ([l.strip() for l in alang_str.split(",") if l.strip()] or None)
    )
    s_langs: list[str] | None = (
        [] if (no_subs or slang_str.lower() == "none") else
        ([l.strip() for l in slang_str.split(",") if l.strip()] if slang_str else None)
    )

    async def _run_download(ep_data: dict, ep_info: dict | None,
                            show_id: str = "") -> None:
        """Download one title (movie or single episode)."""
        if dump_spapi:
            console.print_json(data=ep_data)
            return

        asset  = ep_data.get("assetDetails") or {}
        title  = asset.get("title", content_id)

        # Filename
        if ep_info:
            sn    = ep_info.get("season", 1)
            en    = ep_info.get("episode_number", 1)
            show  = ep_info.get("show_title", title)
            stem  = f"{show} S{sn:02d}E{en:02d} {title} [{ep_info['id']}]"
            ep_id = ep_info["id"]
        else:
            stem  = f"{title} [{content_id}]"
            ep_id = content_id

        vid_url = asset.get("video_url") or {}
        mpd_url = vid_url.get("mpd", "")
        if mpd_url:
            mpd_url = await _select_mpd_url(mpd_url)
        if not mpd_url:
            log.warning(f"No MPD for {title} — skipping")
            return

        console.print(Rule(f"[bold magenta]{title}[/bold magenta]", style="magenta"))
        console.print(f"  [{C_DIM}]{mpd_url[:90]}…[/{C_DIM}]\n")

        out_path = await download_content(
            mpd_url       = mpd_url,
            spapi_data    = ep_data,
            output_dir    = Path(out_dir),
            filename_stem = stem,
            device_path   = cfg.resolved_device_path(),
            cert_path     = cfg.resolved_cert_path(),
            range_mode    = range_mode,
            video_id      = None,
            audio_langs   = a_langs,
            subs          = s_langs,
            no_video      = no_video,
            no_audio      = no_audio,
            no_chapters   = no_chapters,
            connections   = conn,
            keep_temp     = keep_temp,
        )

        size_mb = out_path.stat().st_size // 1024 // 1024
        console.print(Panel(
            f"  [{C_OK}]●[/{C_OK}] [bold]{out_path.name}[/bold]\n"
            f"  [dim]Size:[/dim]  [{C_INFO}]{size_mb} MB[/{C_INFO}]\n"
            f"  [dim]Path:[/dim]  [dim]{out_path.parent}[/dim]",
            title="[bold]Complete[/bold]",
            border_style="green", padding=(0, 1),
        ))
        console.print()

    async def _fetch() -> None:
        session = sm.load()
        if session is None:
            _die("Not logged in. Run: zee5 login")

        # ── Movie / direct episode ─────────────────────────────────────
        if not _is_show(content_id):
            data = await _spapi_call(content_id, session)
            await _run_download(data, None)
            return

        # ── Show — resolve episodes ────────────────────────────────────
        all_eps = await _fetch_show_episodes(content_id, session)

        # Parse --wanted spec into episode list
        wanted_eps = _parse_wanted(wanted, all_eps)

        if len(wanted_eps) == 0:
            _die(f"No episodes matched --wanted {wanted!r}")

        # If only one episode and not explicitly multi, show browser
        if len(wanted_eps) == 1 and wanted.lower() == "all":
            # Single episode show
            pass
        elif wanted.lower() == "all" and len(wanted_eps) > 1:
            # Show full browser for all
            selected = _select_episode(all_eps, batch=True)
            wanted_eps = selected if isinstance(selected, list) else [selected]

        total = len(wanted_eps)
        if total > 1:
            console.print(
                f"\n  [{C_INFO}]{total} episode(s) queued[/{C_INFO}]\n"
            )

        for i, ep in enumerate(wanted_eps, 1):
            if total > 1:
                console.print(Rule(
                    f"[bold magenta]{ep['show_title']} "
                    f"S{ep['season']:02d}E{ep['episode_number']:02d}[/bold magenta] "
                    f"[dim]({i}/{total})[/dim]",
                    style="magenta",
                ))
            ep_data = await _spapi_call(ep["id"], session, show_id=content_id)
            await _run_download(ep_data, ep)

    try:
        _run(_fetch())
    except Exception as e:
        _die(str(e))


# ── account commands ──────────────────────────────────────────────────────

@cli.command()
def watchlist() -> None:
    """Your ZEE5 watchlist."""
    from .urls import WATCHLIST_V2
    async def _go() -> None:
        async with authenticated_client(sm) as c:
            r = await c.get(WATCHLIST_V2)
            r.raise_for_status()
            console.print_json(data=r.json())
    try:
        _run(_go())
    except Zee5AuthError as e:
        _die(str(e))


@cli.command()
def settings() -> None:
    """Your ZEE5 account settings."""
    from .urls import SETTINGS
    async def _go() -> None:
        async with authenticated_client(sm) as c:
            r = await c.get(SETTINGS)
            r.raise_for_status()
            console.print_json(data=r.json())
    try:
        _run(_go())
    except Zee5AuthError as e:
        _die(str(e))


@cli.command()
def profiles() -> None:
    """List profiles on this account."""
    from .urls import PROFILES_V2
    async def _go() -> None:
        async with authenticated_client(sm) as c:
            r = await c.get(PROFILES_V2)
            r.raise_for_status()
            console.print_json(data=r.json())
    try:
        _run(_go())
    except Zee5AuthError as e:
        _die(str(e))


if __name__ == "__main__":
    cli()


# ── Show/episode resolution ───────────────────────────────────────────────
# Injected above _spapi_call — handles 0-6- show IDs and 0-3- season IDs

