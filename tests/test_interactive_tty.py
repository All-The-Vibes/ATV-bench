"""Unit C (live-ish e2e): the interactive model picker driven in a REAL pseudo-terminal.

The hermetic tests stub ``_questionary_select``. These drive the ACTUAL questionary arrow-key
picker under a real pty (stdlib ``pty`` + a child python process), sending real arrow-key +
Enter escape sequences, to prove the interactive path works in a genuine terminal — not just
with the selector monkeypatched.

Marked ``live`` (needs a real tty + questionary rendering); skips if questionary is unavailable.
Kept hermetic-safe: no network, no external CLI — just a child python under a pty.
"""
from __future__ import annotations

import os
import pty
import select
import sys
import time

import pytest

pytestmark = pytest.mark.live

questionary = pytest.importorskip("questionary")

# A tiny child program that builds the SAME picker select_model uses (via atv_bench.interactive
# on real ModelChoice objects) and prints the chosen id on a marker line we can grep.
_CHILD = r"""
import sys
from atv_bench.interactive import select_model
from atv_bench.models import ModelChoice
choices = [
    ModelChoice("model-default", "Default model", is_current=True),
    ModelChoice("model-second", "Second model"),
    ModelChoice("model-third", "Third model"),
]
# force the interactive path (a real tty is attached under pty), no preselected value.
picked = select_model(choices, preselected=None, non_interactive=False)
sys.stdout.write("PICKED:" + picked + "\n")
sys.stdout.flush()
"""


def _drive_pty(keys: bytes, timeout: float = 20.0) -> str:
    """Run the child under a pty, send `keys` once the picker has rendered, return all output."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.execv(sys.executable, [sys.executable, "-c", _CHILD])
        os._exit(127)  # unreachable

    # parent: wait for the picker to render, then send the keystrokes.
    buf = b""
    sent = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        # once we've seen the first choice render, send the navigation keys once.
        if not sent and (b"Default model" in buf or b"Select a model" in buf):
            time.sleep(0.3)
            os.write(fd, keys)
            sent = True
        if b"PICKED:" in buf:
            break
    try:
        os.close(fd)
    except OSError:
        pass
    return buf.decode(errors="replace")


DOWN = b"\x1b[B"   # arrow-down escape sequence
ENTER = b"\r"


def test_picker_arrow_down_selects_second_model():
    """Arrow-down once + Enter selects the SECOND choice (not the default) — proving real
    keyboard navigation through the questionary TUI in a live pty."""
    out = _drive_pty(DOWN + ENTER)
    assert "PICKED:" in out, f"picker never returned a choice; raw:\n{out[-800:]}"
    picked = out.split("PICKED:", 1)[1].splitlines()[0].strip()
    assert picked == "model-second", f"expected arrow-down to land on the 2nd model, got {picked!r}"


def test_picker_enter_selects_default():
    """Enter with no navigation selects the highlighted default (the is_current model)."""
    out = _drive_pty(ENTER)
    assert "PICKED:" in out, f"picker never returned a choice; raw:\n{out[-800:]}"
    picked = out.split("PICKED:", 1)[1].splitlines()[0].strip()
    assert picked == "model-default", f"expected Enter to keep the default, got {picked!r}"
