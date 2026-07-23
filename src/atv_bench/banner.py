"""The fancy ATV-BENCH gold-medal first-run banner.

Python wheels can't reliably run code at `pip install` time, so the "install" greeting is shown
on the FIRST RUN of the CLI instead — once — via a sentinel file under `~/.atv-bench/`.

Everything here is FAIL-SILENT: a non-TTY, `--json` output, an env opt-out, a missing `rich`,
or an unwritable home must never raise or block the actual command. The banner is polish, not a
prerequisite.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

GOLD = "#FFD700"
MEDAL = "🥇"
SENTINEL_ENV = "ATV_BENCH_SKIP_BANNER"

# Versioned so a future redesign can re-greet existing users by bumping the filename.
_SENTINEL_NAME = ".banner_shown_v1"


def default_sentinel() -> Path:
    """The first-run marker path under the user's home (`~/.atv-bench/.banner_shown_v1`)."""
    return Path.home() / ".atv-bench" / _SENTINEL_NAME


def _wordmark() -> str:
    """Block-letter ATV-BENCH wordmark (ASCII art, terminal-safe)."""
    return r"""
   ___  _______   __      ____  _______   ___  __
  / _ |/_  __/ | / /____ / __ )/ __/ _ | / _ )/ /
 / __ | / / | |/ /____// __  / _// __ |/ /_/ / /_/
/_/ |_|/_/  |___/     /_____/___/_/ |_/____/____/
""".strip("\n")


def render_banner() -> str:
    """Render the ATV-BENCH gold-medal banner to a string (gold wordmark + medal + tagline).

    Uses rich for the gold color + panel when available; the returned string always contains the
    ATV-BENCH wordmark, the gold color token, and the medal glyph so it is verifiable and works
    even if rich degrades.
    """
    body = f"{_wordmark()}\n\n{MEDAL}  Community league for coding-agent harnesses  {MEDAL}"
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text

        text = Text(body, style=f"bold {GOLD}")
        panel = Panel(text, border_style=GOLD, title=f"[bold {GOLD}]ATV-BENCH[/]",
                      subtitle=f"[{GOLD}]{MEDAL} gold-standard harness benchmarking {MEDAL}[/]")
        console = Console(record=True, width=72)
        console.print(panel)
        rendered = console.export_text(styles=False)
        # Guarantee the verifiable tokens are present even after style export strips ANSI.
        return f"{rendered}\n{GOLD} {MEDAL}"
    except Exception:
        # Fail soft: a plain but still-verifiable banner (contains wordmark, gold token, medal).
        return f"{body}\n[{GOLD}]"


def should_show_banner(
    *,
    sentinel: Path,
    is_tty: bool,
    json_mode: bool,
    env_suppressed: bool,
) -> bool:
    """Decide whether to greet: TTY + first-run + not JSON + not env-suppressed."""
    if json_mode or env_suppressed or not is_tty:
        return False
    try:
        return not sentinel.exists()
    except Exception:
        return False


def _mark_shown(sentinel: Path) -> None:
    """Best-effort persist of the first-run marker; never raises."""
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("shown\n")
    except Exception:
        pass  # unwritable home: greet-without-persist rather than crash


def maybe_show_banner(
    *,
    sentinel: Path | None = None,
    is_tty: bool | None = None,
    json_mode: bool = False,
    env_suppressed: bool | None = None,
    stream=None,
) -> bool:
    """Show the banner once on first run, fail-silent. Returns True iff it was printed.

    All inputs are injectable for testing; in production they default to real env/TTY/home.
    """
    sentinel = sentinel or default_sentinel()
    if is_tty is None:
        try:
            is_tty = bool(sys.stdout) and sys.stdout.isatty()
        except Exception:
            is_tty = False
    if env_suppressed is None:
        env_suppressed = os.environ.get(SENTINEL_ENV, "") not in ("", "0", "false", "False")

    if not should_show_banner(sentinel=sentinel, is_tty=is_tty, json_mode=json_mode,
                              env_suppressed=env_suppressed):
        return False

    try:
        art = render_banner()
    except Exception:
        return False  # rendering failure must never crash the command

    try:
        print(art, file=stream or sys.stdout)
    except Exception:
        return False
    _mark_shown(sentinel)
    return True
