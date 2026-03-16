"""
zee5.log — Rich-based CLI logger.

Levels controlled by ZEE5_LOG env var or --debug flag:
  0  silent
  1  normal (default)
  2  debug (full request/response bodies)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from rich.console import Console
from rich.text import Text

VERBOSITY: int = int(os.environ.get("ZEE5_LOG", "1"))

# stderr so output doesn't pollute piped stdout
_con = Console(stderr=True, highlight=False)


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _mask(obj: Any) -> str:
    """JSON-encode a dict, masking long tokens."""
    if not isinstance(obj, dict):
        return str(obj)[:400]
    out = {}
    for k, v in obj.items():
        if k == "esk":
            out[k] = "***"
        elif k in ("access_token", "token", "refresh_token", "x-access-token") \
                and isinstance(v, str) and len(v) > 20:
            out[k] = v[:16] + "…"
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _line(icon: str, icon_style: str, msg: str) -> None:
    """Print a plain-text log line (no markup in msg)."""
    t = Text()
    t.append(f"{_ts()} ", style="dim")
    t.append(f"{icon} ", style=icon_style)
    t.append(msg)
    _con.print(t)


class _Log:
    def __init__(self) -> None:
        self._req_start: float = 0.0

    def info(self, msg: str) -> None:
        if VERBOSITY >= 1:
            _line("●", "bold cyan", msg)

    def success(self, msg: str) -> None:
        if VERBOSITY >= 1:
            _line("✓", "bold green", msg)

    def warning(self, msg: str) -> None:
        if VERBOSITY >= 1:
            _line("⚠", "bold yellow", msg)

    def error(self, msg: str) -> None:
        _line("✗", "bold red", msg)

    def debug(self, msg: str, value: Any = None) -> None:
        if VERBOSITY >= 2:
            _line("·", "dim", msg)
            if value is not None:
                _con.print(f"           [dim]{_mask(value)[:300]}[/dim]")

    def request(self, method: str, url: str,
                headers: dict | None = None,
                body: dict | None = None) -> None:
        self._req_start = time.monotonic()
        if VERBOSITY >= 1:
            # Use _con.print with markup for the URL styling
            _con.print(
                f"[dim]{_ts()}[/dim] [bold blue]↑[/bold blue] "
                f"[bold]{method}[/bold] [dim]{url}[/dim]"
            )
        if VERBOSITY >= 2:
            if headers:
                safe = {k: "***" if k == "esk" else v
                        for k, v in headers.items()}
                _con.print(f"           [dim]headers: {_mask(safe)[:300]}[/dim]")
            if body:
                _con.print(f"           [dim]body:    {_mask(body)[:300]}[/dim]")

    def response(self, status: int, body: Any = None) -> None:
        elapsed = time.monotonic() - self._req_start
        if VERBOSITY >= 1:
            if 200 <= status < 300:
                _con.print(
                    f"[dim]{_ts()}[/dim] [bold green]↓[/bold green] "
                    f"[bold green]{status} OK[/bold green]"
                    f"[dim]  ({elapsed:.2f}s)[/dim]"
                )
            else:
                _con.print(
                    f"[dim]{_ts()}[/dim] [bold red]↓[/bold red] "
                    f"[bold red]{status} ERROR[/bold red]"
                    f"[dim]  ({elapsed:.2f}s)[/dim]"
                )
        if VERBOSITY >= 2 and body is not None:
            raw = _mask(body) if isinstance(body, dict) else str(body)
            _con.print(f"           [dim]{raw[:400]}[/dim]")

    def poll(self, attempt: int, max_attempts: int, interval: int) -> None:
        if VERBOSITY >= 1:
            _line("⟳", "dim",
                  f"Waiting… {attempt}/{max_attempts}  "
                  f"(retry in {interval}s)")

    def step(self, n: int, total: int, msg: str) -> None:
        if VERBOSITY >= 1:
            _line(f"[{n}/{total}]", "cyan", msg)


log = _Log()
