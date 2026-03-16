"""
zee5.config — YAML config anchored to the project root.

Default location: <project_root>/zee5.yml

Only `device_name` needs changing — everything else has
sensible defaults based on the project folder structure:

  device_name:   mediatek_smart_tv_26228ff7_8131_l1.wvd
  connections:   16
  default_audio: ""
  default_subs:  ""
  profile_id:    ""

Paths that are always fixed (not in config):
  certificate/zee5_certificate.pem  — Widevine service cert
  device/{device_name}              — Widevine device file
  download/                         — output .mkv files
  tmp/                              — download temp segments
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


@dataclass
class Zee5Config:
    # Only device_name is user-facing — everything else auto-resolves
    device_name:   str = "mediatek_smart_tv_26228ff7_8131_l1.wvd"
    connections:   int = 16
    default_audio: str = ""
    default_subs:  str = ""
    profile_id:    str = ""

    # ── resolved paths (always based on project root) ──────────────────

    def resolved_device_path(self) -> Path:
        from .paths import device_dir
        return device_dir() / self.device_name

    def resolved_cert_path(self) -> Path:
        from .paths import cert_file
        return cert_file()          # always certificate/zee5_certificate.pem

    def resolved_output_dir(self) -> Path:
        from .paths import download_dir
        return download_dir()       # always download/

    def resolved_temp_dir(self) -> Path:
        from .paths import temp_dir
        return temp_dir()           # always tmp/

    # ── load / save ────────────────────────────────────────────────────

    @classmethod
    def load(cls, override_path: str | None = None) -> "Zee5Config":
        path = _resolve(override_path)
        if not path or not path.exists():
            return cls()
        if not _HAS_YAML:
            return cls()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return cls(**{k: v for k, v in raw.items()
                          if k in cls.__dataclass_fields__})
        except Exception:
            return cls()

    def save(self, override_path: str | None = None) -> Path:
        from .paths import root, device_dir, cert_file, download_dir, temp_dir
        path = _resolve(override_path) or _default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _HAS_YAML:
            raise RuntimeError("pyyaml missing — run: poetry install")

        lines = [
            "# zee5 configuration",
            f"# Project root: {root()}",
            "",
            "# Widevine device filename inside device/ folder",
            f"# Folder: {device_dir()}",
            f"device_name: {self.device_name!r}",
            "",
            "# aria2c parallel connections per server",
            f"connections: {self.connections}",
            "",
            "# Default audio languages (comma-separated, e.g. hi,en,te)",
            f"default_audio: {self.default_audio!r}",
            "",
            "# Default subtitle languages (comma-separated, e.g. en)",
            f"default_subs: {self.default_subs!r}",
            "",
            "# Your ZEE5 profile ID (leave empty to use default profile)",
            f"profile_id: {self.profile_id!r}",
            "",
            "# Fixed paths (not configurable — always relative to project root):",
            f"#   certificate: {cert_file()}",
            f"#   download:    {download_dir()}",
            f"#   temp:        {temp_dir()}",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def _default_path() -> Path:
    from .paths import config_file
    return config_file()


def _resolve(override: str | None) -> Path | None:
    if override:
        return Path(override).expanduser()
    env = os.environ.get("ZEE5_CONFIG")
    if env:
        return Path(env).expanduser()
    return _default_path()
