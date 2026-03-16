"""
zee5.download — DASH downloader with track selection, aria2c, and chapters.

Pipeline:
  1. Fetch + parse MPD manifest (xml.etree.ElementTree — no extra deps)
  2. Show available video / audio / subtitle tracks
  3. User selects tracks
  4. Build aria2c input file for all segments
  5. Run aria2c with 16 connections
  6. Concatenate segments → .mp4 / .m4a per track
  7. Mux with ffmpeg → .mkv with all tracks
  8. Write chapters from SPAPI skip_available + end_credits_start_s

Requirements (external):
  aria2c  — https://aria2.github.io/
  ffmpeg  — https://ffmpeg.org/

Both must be on PATH.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .log import log, VERBOSITY

console = Console()

# ── Namespace map for DASH MPD ────────────────────────────────────────────
_NS = {
    "mpd":   "urn:mpeg:dash:schema:mpd:2011",
    "cenc":  "urn:mpeg:cenc:2013",
}


# ── Data classes ──────────────────────────────────────────────────────────

@dataclass
class Segment:
    url:        str
    index:      int  = 0
    init_range: str  = ""   # e.g. "0-707" for SegmentBase init
    full_range: str  = ""   # e.g. "708-1234567" for byte-range segment


@dataclass
class Track:
    track_id:    str
    kind:        str          # "video" | "audio" | "subtitle"
    codec:       str
    lang:        str
    bandwidth:   int
    width:       int = 0
    height:      int = 0
    frame_rate:  str = ""
    channels:    int = 0
    label:       str = ""
    segments:    list[Segment] = field(default_factory=list)
    init_url:    str = ""

    @property
    def display_res(self) -> str:
        if self.width:
            return f"{self.width}x{self.height}"
        return ""

    @property
    def kbps(self) -> int:
        return self.bandwidth // 1000


@dataclass
class Chapter:
    start_ms: int
    title:    str


# ── MPD Parser ────────────────────────────────────────────────────────────

def parse_mpd(mpd_text: str, base_url: str) -> list[Track]:
    """Parse DASH MPD and return all tracks with segment URLs."""
    tracks: list[Track] = []

    # Register all namespaces before parsing to avoid "unbound prefix" errors
    # Extract all xmlns declarations and register them
    for prefix, uri in re.findall(r'xmlns:?(\w*)=["\']([^"\']+)["\']', mpd_text):
        try:
            ET.register_namespace(prefix or "mpd", uri)
        except Exception:
            pass

    # Build a namespace map from the MPD for find() calls
    ns_map: dict[str, str] = {}
    for prefix, uri in re.findall(r'xmlns:(\w+)=["\']([^"\']+)["\']', mpd_text):
        ns_map[prefix] = uri
    # Default namespace
    default_ns_m = re.search(r'xmlns=["\']([^"\']+)["\']', mpd_text)
    if default_ns_m:
        ns_map["mpd"] = default_ns_m.group(1)

    def _tag(name: str) -> str:
        """Return Clark notation {uri}name if default namespace exists."""
        if "mpd" in ns_map:
            return f"{{{ns_map['mpd']}}}{name}"
        return name

    def _find(el: ET.Element, tag: str) -> ET.Element | None:
        # Try Clark notation first, then plain
        result = el.find(_tag(tag))
        if result is None:
            result = el.find(tag)
        return result

    def _findall(el: ET.Element, tag: str) -> list[ET.Element]:
        result = el.findall(_tag(tag))
        if not result:
            result = el.findall(tag)
        return result

    root   = ET.fromstring(mpd_text)
    period = _find(root, "Period") or root

    def _parse_channels(val: str) -> int:
        m = re.search(r"\d+", val or "")
        return int(m.group(0)) if m else 0

    for adapt in _findall(period, "AdaptationSet"):
        mime  = adapt.get("mimeType", "")
        lang  = adapt.get("lang", "und")
        label = adapt.get("label", "") or adapt.get("contentType", "")
        adapt_codec = adapt.get("codecs", "")

        if "video" in mime:
            kind = "video"
        elif "audio" in mime:
            kind = "audio"
        elif "text" in mime or "ttml" in mime or "vtt" in mime:
            kind = "subtitle"
        else:
            # Try to infer from contentType attribute
            ct = adapt.get("contentType", "")
            if ct == "video":   kind = "video"
            elif ct == "audio": kind = "audio"
            elif ct == "text":  kind = "subtitle"
            else: continue

        seg_tmpl = _find(adapt, "SegmentTemplate")

        adapt_channels = 0
        ach_adapt = adapt.find(".//{urn:mpeg:dash:schema:mpd:2011}AudioChannelConfiguration") or \
                    adapt.find(".//AudioChannelConfiguration")
        if ach_adapt is not None:
            adapt_channels = _parse_channels(ach_adapt.get("value", ""))

        for rep in _findall(adapt, "Representation"):
            rid       = rep.get("id", "")
            codec     = rep.get("codecs", "") or adapt_codec
            bandwidth = int(rep.get("bandwidth", 0))
            width     = int(rep.get("width", 0))
            height    = int(rep.get("height", 0))
            fps       = rep.get("frameRate", "")
            channels  = adapt_channels
            rep_lang  = rep.get("lang", "") or lang

            ach = rep.find(".//{urn:mpeg:dash:schema:mpd:2011}AudioChannelConfiguration") or \
                  rep.find(".//AudioChannelConfiguration")
            if ach is not None:
                channels = _parse_channels(ach.get("value", "")) or channels

            rt = _find(rep, "SegmentTemplate") or seg_tmpl
            if rt is not None:
                # ── SegmentTemplate (new-style, most ZEE5 content) ────────
                init_tmpl  = rt.get("initialization", "")
                media_tmpl = rt.get("media", "")
                timescale  = int(rt.get("timescale", 1))
                start_num  = int(rt.get("startNumber", 1))

                def resolve(tmpl: str) -> str:
                    t = tmpl.replace("$RepresentationID$", rid)
                    t = t.replace("$Bandwidth$", str(bandwidth))
                    if not t.startswith("http"):
                        t = urljoin(base_url, t)
                    return t

                init_url   = resolve(init_tmpl)
                segments: list[Segment] = []

                timeline = _find(rt, "SegmentTimeline")
                if timeline is not None:
                    seg_num = start_num
                    t_val   = 0
                    for s in _findall(timeline, "S"):
                        if s.get("t"):
                            t_val = int(s.get("t"))
                        d_val = int(s.get("d", 0))
                        r_val = int(s.get("r", 0))
                        for _ in range(r_val + 1):
                            seg_url = resolve(
                                media_tmpl
                                .replace("$Number$", str(seg_num))
                                .replace("$Time$", str(t_val))
                            )
                            segments.append(Segment(url=seg_url, index=seg_num))
                            seg_num += 1
                            t_val   += d_val
                else:
                    duration   = int(rt.get("duration", 0))
                    # Period duration may be on the Period element or on the root MPD
                    # as mediaPresentationDuration — check both
                    period_dur = (
                        _parse_duration(period.get("duration", "")) or
                        _parse_duration(root.get("mediaPresentationDuration", ""))
                    )
                    if duration and period_dur:
                        count = int(period_dur * timescale / duration) + 2
                        for i in range(start_num, start_num + count):
                            seg_url = resolve(
                                media_tmpl.replace("$Number$", str(i))
                            )
                            segments.append(Segment(url=seg_url, index=i))

            else:
                # ── SegmentBase / SegmentList (old-style single-file MP4s) ─
                # The whole track is a single URL with byte-range requests.
                # Build a BaseURL for the representation.
                base_url_el = (_find(rep, "BaseURL") or
                               _find(adapt, "BaseURL") or
                               _find(period, "BaseURL") or
                               _find(root, "BaseURL"))
                file_url = base_url_el.text.strip() if base_url_el is not None else ""
                if not file_url:
                    continue
                if not file_url.startswith("http"):
                    file_url = urljoin(base_url, file_url)

                seg_base = (_find(rep, "SegmentBase") or
                            _find(adapt, "SegmentBase"))

                if seg_base is not None:
                    # Single-segment: init range + one media segment (full file)
                    init_range = seg_base.get("initialization", "")
                    # init_range may be on a child Initialization element
                    init_el = _find(seg_base, "Initialization")
                    if init_el is not None:
                        init_range = init_el.get("range", init_range)
                    init_url  = file_url   # whole file; downloader uses range header
                    # Wrap the whole file as a single "segment" with range hint
                    index_range = seg_base.get("indexRange", "")
                    segments    = [Segment(
                        url   = file_url,
                        index = 0,
                        # Store init_range and index_range in the URL as hints
                        # download handles byte-range via headers
                    )]
                    # Attach range info so _download_segbase can read it
                    segments[0] = Segment(
                        url        = file_url,
                        index      = 0,
                        init_range = init_range,
                        full_range = "",   # no range = full file
                    )
                    init_url = file_url

                else:
                    # SegmentList — explicit list of URLs / ranges
                    seg_list = _find(rep, "SegmentList") or _find(adapt, "SegmentList")
                    if seg_list is None:
                        continue
                    init_el  = _find(seg_list, "Initialization")
                    init_url = file_url
                    if init_el is not None:
                        src = init_el.get("sourceURL", "")
                        init_url = urljoin(base_url, src) if src else file_url
                    segments = []
                    for idx, su in enumerate(_findall(seg_list, "SegmentURL")):
                        media_src = su.get("media", file_url)
                        seg_url   = urljoin(base_url, media_src) \
                                    if not media_src.startswith("http") else media_src
                        media_range = su.get("mediaRange", "")
                        segments.append(Segment(
                            url        = seg_url,
                            index      = idx,
                            init_range = "",
                            full_range = media_range,
                        ))

            tracks.append(Track(
                track_id   = rid,
                kind       = kind,
                codec      = codec,
                lang       = rep_lang,
                bandwidth  = bandwidth,
                width      = width,
                height     = height,
                frame_rate = fps,
                channels   = channels,
                label      = label,
                segments   = segments,
                init_url   = init_url,
            ))

    return tracks


def _parse_duration(dur: str) -> float:
    """Parse ISO 8601 duration like PT1H46M15S → seconds."""
    if not dur:
        return 0.0
    m = re.match(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", dur
    )
    if not m:
        return 0.0
    h = float(m.group(1) or 0)
    mi = float(m.group(2) or 0)
    s = float(m.group(3) or 0)
    return h * 3600 + mi * 60 + s


def _extract_pssh(mpd_text: str) -> str | None:
    """Extract the first Widevine PSSH (base64) from MPD text."""
    try:
        root = ET.fromstring(mpd_text)
    except Exception:
        return None

    wv_scheme = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
    for cp in root.findall(".//{urn:mpeg:dash:schema:mpd:2011}ContentProtection"):
        if (cp.get("schemeIdUri") or "").lower() == wv_scheme:
            node = cp.find(".//{urn:mpeg:cenc:2013}pssh") or cp.find(".//pssh")
            if node is not None and node.text:
                return node.text.strip()

    pssh_nodes = root.findall(".//{urn:mpeg:cenc:2013}pssh") or root.findall(".//pssh")
    try:
        from pywidevine.pssh import PSSH
    except Exception:
        return None
    for node in pssh_nodes:
        if not node.text:
            continue
        try:
            pssh = PSSH(node.text.strip())
            if str(pssh.system_id).lower() == wv_scheme.replace("urn:uuid:", ""):
                return node.text.strip()
        except Exception:
            continue
    return None


def _load_service_certificate(path: Path) -> bytes | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if "BEGIN CERTIFICATE" in raw:
        lines = [ln.strip() for ln in raw.splitlines()
                 if ln and "BEGIN CERTIFICATE" not in ln and "END CERTIFICATE" not in ln]
        cleaned = "".join(lines)
    else:
        cleaned = raw.strip().strip("(").strip(")").strip().strip("\"").strip("'")
    try:
        return base64.b64decode(cleaned)
    except Exception:
        return None


def _save_service_certificate(path: Path, cert: bytes) -> None:
    encoded = base64.b64encode(cert).decode("utf-8")
    path.write_text(encoded, encoding="utf-8")


def _acquire_widevine_license(
    mpd_text: str,
    nl: str,
    customdata: str,
    device_path: Path,
    license_url: str,
    cert_path: Path,
) -> list[tuple[str, str, str]]:
    """Acquire Widevine license using pywidevine; return (type, kid_hex, key_hex)."""
    pssh_b64 = _extract_pssh(mpd_text)
    if not pssh_b64:
        log.warning("Widevine PSSH not found in MPD; skipping license")
        return []
    if not device_path.exists():
        log.warning(f"Widevine device file not found: {device_path}")
        return []

    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import PSSH

    device = Device.load(str(device_path))
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    headers = {
        "origin": "https://www.zee5.com",
        "referer": "https://www.zee5.com/",
        "customdata": customdata if customdata.startswith("Nagra_") else f"Nagra_{nl}",
        "nl": nl,
        "content-type": "application/octet-stream",
    }

    keys: list[tuple[str, str, str]] = []
    try:
        cert = _load_service_certificate(cert_path)
        if cert is None:
            log.warning("Service certificate missing; skipping certificate request")

        if cert:
            cdm.set_service_certificate(session_id, cert)

        challenge = cdm.get_license_challenge(
            session_id,
            PSSH(pssh_b64),
            license_type="STREAMING",
            privacy_mode=True,
        )
        lic_resp = httpx.post(
            license_url,
            content=challenge,
            headers=headers,
            timeout=20.0,
        )
        try:
            lic_resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:1000] if exc.response is not None else ""
            log.error(f"License request failed: {exc.response.status_code} {body}")
            raise
        cdm.parse_license(session_id, lic_resp.content)
        for key in cdm.get_keys(session_id):
            kid = key.kid
            if hasattr(kid, "hex"):
                kid_hex = kid.hex
            elif isinstance(kid, bytes):
                kid_hex = kid.hex()
            else:
                kid_hex = str(kid).replace("-", "")

            k = key.key
            key_hex = k.hex() if isinstance(k, bytes) else str(k)
            if kid_hex and key_hex:
                ktype = getattr(key, "type", "UNKNOWN")
                keys.append((str(ktype), kid_hex.lower(), key_hex.lower()))
        log.success("Widevine license acquired")
        return keys
    finally:
        cdm.close(session_id)


def _mp4decrypt(input_path: Path, output_path: Path,
                keys: list[tuple[str, str, str]]) -> Path:
    """Decrypt an MP4 file using mp4decrypt and provided keys."""
    mp4decrypt = _check_tool("mp4decrypt")
    cmd = [mp4decrypt]
    for ktype, kid_hex, key_hex in keys:
        if ktype.upper() != "CONTENT":
            continue
        cmd += ["--key", f"{kid_hex}:{key_hex}"]
    cmd += [str(input_path), str(output_path)]
    subprocess.run(cmd, check=True)
    return output_path


# ── Track selection ───────────────────────────────────────────────────────

def select_tracks(
    tracks: list[Track],
    video_id:    str | None = None,
    audio_langs: list[str] | None = None,
    subs:        list[str] | None = None,
    spapi_data:  dict | None = None,
) -> tuple[Track, list[Track], list[Track]]:
    """
    Interactive track selection.
    Returns (video_track, audio_tracks, subtitle_tracks).
    subtitle_tracks is a list of dicts for external VTT subs (not Track objects).
    """
    videos    = [t for t in tracks if t.kind == "video"]
    audios    = [t for t in tracks if t.kind == "audio"]
    # ZEE5 uses external VTT — subtitles are rarely in the MPD
    mpd_subs  = [t for t in tracks if t.kind == "subtitle"]

    # External subtitles from SPAPI subtitle_url field
    asset    = (spapi_data or {}).get("assetDetails") or {}
    ext_subs = asset.get("subtitle_url") or []
    # Normalise: [{url, language}] or [{url, lang}]
    ext_subs = [
        {
            "url":  s.get("url", ""),
            "lang": s.get("language") or s.get("lang", "und"),
            "forced": s.get("forced", False),
        }
        for s in ext_subs if s.get("url")
    ]
    # Also note subtitle_languages even if URLs are empty
    sub_langs_avail = asset.get("subtitle_languages") or []

    # ── Video ──────────────────────────────────────────────────────────
    # Check if requested codec/range is available and warn if not
    has_hevc = any("hvc" in (t.codec or "").lower() or "hevc" in (t.codec or "").lower()
                   for t in videos)
    has_avc  = any("avc" in (t.codec or "").lower() for t in videos)

    console.print("\n[bold]Video tracks:[/bold]")
    vtable = Table("№", "ID", "Resolution", "Codec", "Bitrate", "FPS",
                   box=None, padding=(0, 2))
    for i, v in enumerate(videos):
        vtable.add_row(
            str(i + 1), v.track_id, v.display_res,
            v.codec, f"{v.kbps} kbps", v.frame_rate or "—",
        )
    console.print(vtable)

    if video_id:
        video = next((v for v in videos if v.track_id == video_id), videos[-1])
    elif len(videos) == 1:
        video = videos[0]
    else:
        while True:
            choice = Prompt.ask(
                "Select video track",
                default=str(len(videos)),
            )
            try:
                idx = int(choice.strip()) - 1
                if 0 <= idx < len(videos):
                    video = videos[idx]
                    break
                console.print(f"  [yellow]Enter a number between 1 and {len(videos)}[/yellow]")
            except ValueError:
                console.print(f"  [yellow]Enter a number (1–{len(videos)})[/yellow]")

    console.print(f"[green]✓ Video:[/green] {video.display_res} {video.codec} @ {video.kbps} kbps")

    # ── Audio ──────────────────────────────────────────────────────────
    console.print("\n[bold]Audio tracks:[/bold]")
    atable = Table("№", "ID", "Lang", "Codec", "Channels", "Bitrate",
                   box=None, padding=(0, 2))
    for i, a in enumerate(audios):
        atable.add_row(
            str(i + 1), a.track_id, a.lang,
            a.codec, str(a.channels) + "ch" if a.channels else "—",
            f"{a.kbps} kbps",
        )
    console.print(atable)

    if audio_langs == []:
        sel_audio = []
    elif audio_langs:
        matched = [a for a in audios if a.lang in audio_langs]
        if not matched:
            console.print(
                f"  [yellow]⚠  Audio language(s) {audio_langs} not available. "
                f"Available: {list({a.lang for a in audios})}[/yellow]"
            )
            sel_audio = audios   # fall back to all
        else:
            sel_audio = matched
    elif len(audios) == 1:
        sel_audio = audios
    else:
        while True:
            raw = Prompt.ask(
                "Select audio (comma-separated, e.g. 1,2 or 'all')",
                default="all",
            )
            raw = raw.strip().lower()
            if raw == "all":
                sel_audio = audios
                break
            try:
                idxs      = [int(x.strip()) - 1 for x in raw.split(",")]
                sel_audio = [audios[i] for i in idxs if 0 <= i < len(audios)]
                if sel_audio:
                    break
                console.print(f"  [yellow]No valid tracks selected[/yellow]")
            except ValueError:
                console.print(f"  [yellow]Enter numbers like 1,2 or 'all'[/yellow]")
    for a in sel_audio:
        console.print(f"[green]✓ Audio:[/green] [{a.lang}] {a.codec} {a.channels}ch")

    # ── Subtitles ──────────────────────────────────────────────────────
    # Combine MPD subs + external VTT subs into one list for display
    all_subs: list[dict] = []
    for t in mpd_subs:
        all_subs.append({"kind": "mpd", "lang": t.lang, "codec": t.codec,
                         "track": t, "url": "", "forced": False})
    for s in ext_subs:
        all_subs.append({"kind": "vtt", "lang": s["lang"], "codec": "WebVTT",
                         "track": None, "url": s["url"], "forced": s["forced"]})
    # If SPAPI lists subtitle_languages but no URLs yet, show as info
    extra_langs = [l for l in sub_langs_avail
                   if l not in {s["lang"] for s in all_subs}]

    sel_subs: list[dict] = []

    if all_subs:
        console.print("\n[bold]Subtitle tracks:[/bold]")
        stable = Table("№", "Lang", "Format", "Type", "URL",
                       box=None, padding=(0, 2))
        for i, s in enumerate(all_subs):
            forced = "[yellow]FORCED[/yellow]" if s["forced"] else "optional"
            url_short = s["url"][:60] + "…" if len(s["url"]) > 60 else s["url"]
            stable.add_row(str(i + 1), s["lang"], s["codec"], forced, url_short)
        console.print(stable)
        if extra_langs:
            console.print(f"[dim]  Note: {', '.join(extra_langs)} listed but no URL in response[/dim]")

        if subs is not None and subs == []:
            sel_subs = []
            console.print("[dim]  Subtitles: none[/dim]")
        elif subs is not None:
            sel_subs = [s for s in all_subs if s["lang"] in subs]
        else:
            # Auto-include forced subs, ask for rest
            forced_subs = [s for s in all_subs if s["forced"]]
            if forced_subs:
                console.print(f"[yellow]⚠  {len(forced_subs)} forced subtitle(s) — always included[/yellow]")
                sel_subs = list(forced_subs)

            optional = [s for s in all_subs if not s["forced"]]
            if optional:
                raw = Prompt.ask(
                    "Select optional subtitle tracks (comma-separated, 'all', or 'none')",
                    default="all",
                )
                if raw.strip().lower() == "none":
                    pass
                elif raw.strip().lower() == "all":
                    sel_subs += optional
                else:
                    idxs     = [int(x.strip()) - 1 for x in raw.split(",")]
                    sel_subs += [all_subs[i] for i in idxs]

        for s in sel_subs:
            console.print(f"[green]✓ Sub:[/green] [{s['lang']}] {s['codec']}"
                          f"{' [yellow](forced)[/yellow]' if s['forced'] else ''}")
    elif extra_langs:
        console.print(f"\n[dim]Subtitles listed ({', '.join(extra_langs)}) but no download URLs available.[/dim]")

    return video, sel_audio, sel_subs


# ── aria2c downloader ─────────────────────────────────────────────────────

def _check_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(
            f"{name} not found on PATH.\n"
            f"  aria2c → https://aria2.github.io/\n"
            f"  ffmpeg → https://ffmpeg.org/"
        )
    return path


def _write_aria2_input(urls: list[str], out_dir: Path,
                       segments: list[Segment] | None = None) -> Path:
    """Write aria2c input file. Supports byte-range headers for SegmentBase MPDs."""
    inp   = out_dir / "aria2_input.txt"
    lines = []
    for i, url in enumerate(urls):
        seg   = segments[i] if segments else None
        fname = f"seg_{i:06d}{Path(urlparse(url).path).suffix or '.mp4'}"
        lines.append(url)
        lines.append(f"  out={fname}")
        lines.append(f"  dir={out_dir}")
        if seg and seg.full_range:
            lines.append(f"  header=Range: bytes={seg.full_range}")
        lines.append("")
    inp.write_text("\n".join(lines), encoding="utf-8")
    return inp


def _aria2c_download(urls: list[str], out_dir: Path,
                     connections: int = 16,
                     label: str = "",
                     segments: list[Segment] | None = None) -> list[Path]:
    """Download URLs with aria2c, showing a Rich progress bar."""
    import threading
    import time as _time

    from rich.progress import (BarColumn, Progress, ProgressColumn, TextColumn,
                               TimeRemainingColumn)
    from rich.text import Text

    aria2c = _check_tool("aria2c")
    inp    = _write_aria2_input(urls, out_dir, segments=segments)

    cmd = [
        aria2c,
        f"--input-file={inp}",
        f"--max-connection-per-server={connections}",
        f"--split={connections}",
        "--min-split-size=1M",
        "--continue=true",
        "--auto-file-renaming=false",
        "--console-log-level=warn",
        "--summary-interval=0",
        "--download-result=hide",
        "--retry-wait=3",
        "--max-tries=5",
    ]

    log.info(f"{label or 'Downloading'}: {len(urls)} segments "
             f"({connections} parallel connections)…")

    total_segs   = len(urls)
    done_segs    = 0
    done_bytes   = 0
    done_lock    = threading.Lock()
    finished_evt = threading.Event()

    def _count_done() -> tuple[int, int]:
        segs  = list(out_dir.glob("seg_*"))
        count = len(segs)
        size  = sum(p.stat().st_size for p in segs if p.is_file())
        return count, size

    def _watch_progress() -> None:
        nonlocal done_segs, done_bytes
        while not finished_evt.is_set():
            count, size = _count_done()
            with done_lock:
                done_segs  = count
                done_bytes = size
            _time.sleep(0.5)

    watcher = threading.Thread(target=_watch_progress, daemon=True)
    watcher.start()

    class MbSpeedColumn(ProgressColumn):
        def render(self, task) -> Text:
            bytes_done = task.fields.get("bytes_done", 0)
            start_time = task.fields.get("start_time", None)
            if not start_time:
                return Text("— MB/s")
            elapsed = _time.monotonic() - start_time
            if elapsed <= 0:
                return Text("— MB/s")
            mbps = bytes_done / 1024 / 1024 / elapsed
            return Text(f"{mbps:>6.2f} MB/s")

    # Capture stderr so we can show it on failure
    import tempfile as _tf
    err_file = _tf.NamedTemporaryFile(mode="w+", suffix=".log",
                                      delete=False, encoding="utf-8")

    with Progress(
        TextColumn(f"[bold cyan]{label or 'DL'}[/bold cyan]"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("•"),
        TextColumn("{task.completed}/{task.total} segs"),
        TextColumn("•"),
        MbSpeedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        refresh_per_second=4,
    ) as progress:
        task = progress.add_task(
            "", total=total_segs,
            bytes_done=0,
            start_time=_time.monotonic(),
        )

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=err_file)
        while proc.poll() is None:
            with done_lock:
                current       = done_segs
                current_bytes = done_bytes
            progress.update(task, completed=current, bytes_done=current_bytes)
            _time.sleep(0.3)

        finished_evt.set()
        final_count, final_bytes = _count_done()
        progress.update(task, completed=final_count, bytes_done=final_bytes)

    err_file.flush()
    err_file.seek(0)
    err_text = err_file.read().strip()
    err_file.close()
    Path(err_file.name).unlink(missing_ok=True)

    if proc.returncode not in (0, 7):
        if err_text:
            log.error(f"aria2c output:\n{err_text[-2000:]}")
        raise RuntimeError(f"aria2c failed (exit {proc.returncode})")

    # Show warnings even on success so we know if any segments failed
    if err_text and VERBOSITY >= 1:
        for line in err_text.splitlines():
            if any(w in line.lower() for w in ("error", "failed", "warn")):
                log.warning(f"aria2c: {line.strip()}")

    # Exit code 7 = network error on some segments — retry missing ones
    if proc.returncode == 7:
        downloaded = {p.stem for p in out_dir.glob("seg_*")}
        missing_segs  = []
        missing_urls  = []
        for i, url in enumerate(urls):
            stem = f"seg_{i:06d}"
            if stem not in downloaded:
                missing_urls.append(url)
                missing_segs.append(segments[i] if segments else None)

        if missing_urls:
            log.warning(f"Retrying {len(missing_urls)} failed segments…")
            retry_inp = _write_aria2_input(missing_urls, out_dir,
                                           segments=missing_segs if segments else None)
            retry_cmd = [
                aria2c,
                f"--input-file={retry_inp}",
                f"--max-connection-per-server={connections}",
                f"--split={connections}",
                "--min-split-size=1M",
                "--continue=true",
                "--auto-file-renaming=false",
                "--retry-wait=5",
                "--max-tries=8",
            ]
            result = subprocess.run(retry_cmd, check=False)
            if result.returncode not in (0, 7):
                raise RuntimeError(
                    f"aria2c retry failed (exit {result.returncode}). "
                    "Try --conn 4 to reduce parallel connections."
                )

    return sorted(out_dir.glob("seg_*"))


# ── Segment concatenation ─────────────────────────────────────────────────

def _concat_segments(init_path: Path, seg_paths: list[Path],
                     out_path: Path) -> Path:
    """Concatenate init + segments into a single file (binary cat)."""
    with out_path.open("wb") as fout:
        fout.write(init_path.read_bytes())
        for seg in seg_paths:
            fout.write(seg.read_bytes())
    return out_path


# ── Chapter generation ────────────────────────────────────────────────────

def build_chapters(spapi_data: dict, duration_s: int) -> list[Chapter]:
    """
    Build chapter list from SPAPI response fields.
    Confirmed fields:
      assetDetails.skip_available.intro_start_s  "00:00:14"
      assetDetails.skip_available.intro_end_s    "00:01:24"
      assetDetails.end_credits_start_s           "00:02:59"
    """
    asset   = spapi_data.get("assetDetails") or {}
    skip    = asset.get("skip_available") or {}
    credits = asset.get("end_credits_start_s", "")

    def ts_to_ms(ts: str) -> int:
        parts = [int(p) for p in ts.split(":")]
        if len(parts) == 3:
            return (parts[0] * 3600 + parts[1] * 60 + parts[2]) * 1000
        return (parts[0] * 60 + parts[1]) * 1000

    chapters: list[Chapter] = []

    intro_start = skip.get("intro_start_s", "")
    intro_end   = skip.get("intro_end_s", "")

    if intro_start and intro_end:
        # Has intro credits at start
        if ts_to_ms(intro_start) > 0:
            chapters.append(Chapter(start_ms=0, title="Cold Open"))
        chapters.append(Chapter(start_ms=ts_to_ms(intro_start), title="Intro Credits"))
        chapters.append(Chapter(start_ms=ts_to_ms(intro_end),   title="Main Feature"))
    else:
        chapters.append(Chapter(start_ms=0, title="Main Feature"))

    if credits:
        chapters.append(Chapter(start_ms=ts_to_ms(credits), title="End Credits"))

    chapters.sort(key=lambda c: c.start_ms)
    return chapters


def write_ffmpeg_chapters(chapters: list[Chapter],
                          path: Path,
                          total_duration_ms: int = 0) -> Path:
    """Write FFmpeg metadata file with chapters."""
    lines = [";FFMETADATA1"]
    for i, ch in enumerate(chapters):
        if i + 1 < len(chapters):
            end_ms = chapters[i + 1].start_ms - 1
        elif total_duration_ms > 0:
            end_ms = total_duration_ms
        else:
            end_ms = ch.start_ms + 1  # at least 1ms duration
        # Ensure end > start
        if end_ms <= ch.start_ms:
            end_ms = ch.start_ms + 1
        lines += [
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={ch.start_ms}",
            f"END={end_ms}",
            f"title={ch.title}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── ffmpeg mux ────────────────────────────────────────────────────────────

def mux(
    video_path:   Path | None,
    audio_paths:  list[Path],
    sub_paths:    list[Path],
    chapter_meta: Path | None,
    output_path:  Path,
    title:        str = "",
) -> Path:
    """Mux all tracks into an MKV container using ffmpeg."""
    ffmpeg = _check_tool("ffmpeg")

    cmd = [ffmpeg, "-y", "-loglevel", "error"]

    # Inputs
    if video_path:
        cmd += ["-i", str(video_path)]
    for p in audio_paths:
        cmd += ["-i", str(p)]
    for p in sub_paths:
        cmd += ["-i", str(p)]
    if chapter_meta:
        cmd += ["-i", str(chapter_meta)]

    # Map streams
    idx = 0
    if video_path:
        cmd += ["-map", f"{idx}:v"]
        idx += 1
    for _ in audio_paths:
        cmd += ["-map", f"{idx}:a"]
        idx += 1
    for _ in sub_paths:
        cmd += ["-map", f"{idx}:s"]
        idx += 1
    if chapter_meta:
        cmd += ["-map_metadata", str(idx)]

    # Copy all streams without re-encoding
    cmd += ["-c", "copy"]

    # Metadata
    if title:
        cmd += ["-metadata", f"title={title}"]

    cmd += [str(output_path)]

    log.info(f"ffmpeg mux → {output_path.name}")
    subprocess.run(cmd, check=True)
    return output_path


# ── Main download orchestrator ────────────────────────────────────────────

async def download_content(
    mpd_url:       str,
    spapi_data:    dict,
    output_dir:    Path,
    filename_stem: str,
    device_path:   Path,        # resolved .wvd path
    cert_path:     Path,        # resolved cert.pem path
    range_mode:    str = "DV",
    video_id:      str | None = None,
    audio_langs:   list[str] | None = None,
    subs:          list[str] | None = None,
    no_video:      bool = False,
    no_audio:      bool = False,
    no_chapters:   bool = False,
    connections:   int = 16,
    keep_temp:     bool = False,
) -> Path:
    """
    Full download pipeline:
      fetch MPD → select tracks → download segments → concat → mux → chapters
    """
    _check_tool("aria2c")
    _check_tool("ffmpeg")

    from .paths import temp_dir, downloads_dir

    if not output_dir or output_dir == Path("."):
        output_dir = downloads_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch MPD
    base_url = mpd_url.split("?")[0].rsplit("/", 1)[0] + "/"
    log.info(f"Fetching MPD…")
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as c:
        r = await c.get(mpd_url)
        r.raise_for_status()
        mpd_text = r.text

    asset = spapi_data.get("assetDetails") or {}
    key_os = spapi_data.get("keyOsDetails") or {}
    if bool(asset.get("is_drm", 0)):
        nl         = key_os.get("nl", "")
        customdata = key_os.get("sdrm", "")
        if nl and customdata:
            keys = _acquire_widevine_license(
                mpd_text    = mpd_text,
                nl          = nl,
                customdata  = customdata,
                device_path = device_path,
                license_url = "https://spapi.zee5.com/widevine/getLicense",
                cert_path   = cert_path,
            )
            if keys:
                table = Table("Type", "KID", "Key", box=None, padding=(0, 1))
                for ktype, kid_hex, key_hex in keys:
                    table.add_row(ktype, kid_hex, key_hex)
                console.print(table)
        else:
            log.warning("DRM content but missing nl/sdrm; skipping license")
            keys = []
    else:
        keys = []

    # 2. Parse tracks
    tracks = parse_mpd(mpd_text, base_url)
    v_tracks = [t for t in tracks if t.kind == "video"]
    a_tracks = [t for t in tracks if t.kind == "audio"]
    log.success(f"Parsed MPD: {len(v_tracks)}V "
                f"{len(a_tracks)}A "
                f"{len([t for t in tracks if t.kind=='subtitle'])}S tracks")

    # Warn if any tracks have 0 segments — usually means bad duration
    zero_seg = [t for t in tracks if len(t.segments) == 0]
    if zero_seg:
        log.warning(
            f"{len(zero_seg)} track(s) have 0 segments — "
            f"MPD may be missing mediaPresentationDuration"
        )

    # Quality availability check
    avail_codecs  = {(t.codec or "").lower() for t in v_tracks}
    wants_hevc    = range_mode in ("DV", "HDR10", "HDR")
    has_hevc      = any("hvc" in c or "hevc" in c for c in avail_codecs)
    has_avc       = any("avc" in c for c in avail_codecs)

    if wants_hevc and not has_hevc:
        log.warning(
            f"Requested codec H265/HEVC not available for this title "
            f"(only {sorted(avail_codecs)}). Falling back to best available."
        )
    if range_mode == "DV" and not any(
        "dolby" in u.lower() or "dv" in u.lower() or "dolbyvision" in u.lower()
        for u in [mpd_url]
    ):
        log.warning(f"DolbyVision not available — stream is SDR/HDR only.")

    # 3. Select tracks — respect no_video / no_audio flags
    if no_video and no_audio:
        _exit_no_tracks = True
    else:
        _exit_no_tracks = False

    video, sel_audio, sel_subs = select_tracks(
        tracks, video_id,
        audio_langs = [] if no_audio else audio_langs,
        subs        = [] if True else subs,   # always go through sub selection
        spapi_data  = spapi_data,
    )
    # Override subs if no_audio / no_video
    if no_audio:
        sel_audio = []
    if no_video:
        # Still need a video track object for mux; skip downloading it
        pass

    # 4. Create temp dir inside app temp folder (not next to project)
    tmp = Path(tempfile.mkdtemp(prefix="zee5_dl_", dir=str(temp_dir())))
    log.debug(f"Temp dir", {"path": str(tmp)})

    try:
        asset = spapi_data.get("assetDetails") or {}
        title = asset.get("title", filename_stem)
        dur_s = asset.get("duration", 0)

        # 5. Download each track
        def dl_track(track: Track, label: str) -> Path:
            track_dir = tmp / label
            track_dir.mkdir()
            import urllib.request as _ur
            init_path = track_dir / "init.mp4"
            log.info(f"[{label}] init segment…")
            # SegmentBase: init is a byte range of the same file
            init_range = track.segments[0].init_range if track.segments else ""
            if init_range:
                req = _ur.Request(track.init_url,
                                  headers={"Range": f"bytes={init_range}"})
                with _ur.urlopen(req) as resp:
                    init_path.write_bytes(resp.read())
            else:
                _ur.urlretrieve(track.init_url, init_path)
            seg_urls = [s.url for s in track.segments]
            _aria2c_download(seg_urls, track_dir, connections,
                             label=label, segments=track.segments)
            segs    = sorted(track_dir.glob("seg_*"))
            out_raw = tmp / f"{label}.mp4"
            _concat_segments(init_path, segs, out_raw)
            mb = out_raw.stat().st_size // 1024 // 1024
            log.success(f"[{label}] {mb} MB")
            return out_raw

        video_file  = dl_track(video, "video") if not no_video else None
        audio_files = [dl_track(a, f"audio_{a.lang}_{i}")
                       for i, a in enumerate(sel_audio)]
        sub_files: list[Path] = []

        if keys and video_file:
            log.info("Decrypting tracks with mp4decrypt…")
            video_file = _mp4decrypt(video_file, tmp / "video.dec.mp4", keys)
            dec_audio: list[Path] = []
            for i, af in enumerate(audio_files):
                dec_audio.append(_mp4decrypt(af, tmp / f"audio_{i}.dec.m4a", keys))
            audio_files = dec_audio

        # Subtitles
        for i, sub_info in enumerate(sel_subs):
            if sub_info["kind"] == "mpd" and sub_info["track"]:
                sub_files.append(dl_track(sub_info["track"],
                                          f"sub_{sub_info['lang']}_{i}"))
            elif sub_info["kind"] == "vtt" and sub_info["url"]:
                lang     = sub_info["lang"]
                vtt_path = tmp / f"sub_{lang}_{i}.vtt"
                log.info(f"Downloading subtitle [{lang}]…")
                import urllib.request as _ur2
                _ur2.urlretrieve(sub_info["url"], vtt_path)
                sub_files.append(vtt_path)
                log.success(f"Sub [{lang}] → {vtt_path.name}")

        # Chapters
        chapter_meta: Path | None = None
        if not no_chapters:
            chapters = build_chapters(spapi_data, dur_s)
            if chapters:
                chapter_meta = write_ffmpeg_chapters(
                    chapters, tmp / "chapters.txt",
                    total_duration_ms=dur_s * 1000,
                )
                log.info(f"Chapters: {[c.title for c in chapters]}")

        # 7. Mux
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_name   = re.sub(r'[<>:"/\\|?*]', "", filename_stem)
        output_path = output_dir / f"{safe_name}.mkv"

        if video_file is None and not audio_files:
            log.warning("Nothing to mux — both --no-video and --no-audio were set.")
            return output_path

        mux(
            video_path   = video_file,
            audio_paths  = audio_files,
            sub_paths    = sub_files,
            chapter_meta = chapter_meta,
            output_path  = output_path,
            title        = title,
        )

        log.success(f"Done → {output_path}")
        return output_path

    finally:
        if not keep_temp:
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)
        else:
            log.debug(f"Temp files kept", {"path": str(tmp)})
