"""Phase 6 — the fancy ATV-BENCH gold-medal first-run banner.

TDD contract for `atv_bench.banner`:
  * `render_banner()` returns a string containing the ATV-BENCH wordmark, gold styling, and a
    gold-medal glyph.
  * `should_show_banner(...)` gates the banner: TTY + first-run + not JSON + not env-suppressed.
  * `maybe_show_banner(...)` is fail-silent — a read-only home, missing rich, or non-TTY must
    NEVER raise or block the command.
  * The first-run sentinel is written once so the banner shows on first invocation only.
"""
from __future__ import annotations

import atv_bench.banner as banner


def test_render_banner_contains_wordmark_and_medal():
    out = banner.render_banner()
    assert "ATV" in out and "BENCH" in out          # the wordmark
    assert "🥇" in out                                # gold medal glyph
    # gold color surfaced (hex or rich style token)
    assert "FFD700" in out or "gold" in out.lower()


def test_should_show_true_on_first_run_tty(tmp_path):
    sentinel = tmp_path / ".banner_shown_v1"
    assert banner.should_show_banner(
        sentinel=sentinel, is_tty=True, json_mode=False, env_suppressed=False
    ) is True


def test_should_show_false_when_not_tty(tmp_path):
    sentinel = tmp_path / ".banner_shown_v1"
    assert banner.should_show_banner(
        sentinel=sentinel, is_tty=False, json_mode=False, env_suppressed=False
    ) is False


def test_should_show_false_in_json_mode(tmp_path):
    sentinel = tmp_path / ".banner_shown_v1"
    assert banner.should_show_banner(
        sentinel=sentinel, is_tty=True, json_mode=True, env_suppressed=False
    ) is False


def test_should_show_false_when_env_suppressed(tmp_path):
    sentinel = tmp_path / ".banner_shown_v1"
    assert banner.should_show_banner(
        sentinel=sentinel, is_tty=True, json_mode=False, env_suppressed=True
    ) is False


def test_should_show_false_when_sentinel_exists(tmp_path):
    sentinel = tmp_path / ".banner_shown_v1"
    sentinel.write_text("shown")
    assert banner.should_show_banner(
        sentinel=sentinel, is_tty=True, json_mode=False, env_suppressed=False
    ) is False


def test_maybe_show_writes_sentinel_and_shows_once(tmp_path, capsys):
    sentinel = tmp_path / ".banner_shown_v1"
    shown1 = banner.maybe_show_banner(
        sentinel=sentinel, is_tty=True, json_mode=False, env_suppressed=False
    )
    assert shown1 is True
    assert sentinel.exists()
    out1 = capsys.readouterr().out
    assert "ATV" in out1 and "BENCH" in out1

    # second invocation: sentinel present → no banner
    shown2 = banner.maybe_show_banner(
        sentinel=sentinel, is_tty=True, json_mode=False, env_suppressed=False
    )
    assert shown2 is False
    assert "ATV" not in capsys.readouterr().out


def test_maybe_show_fail_silent_on_unwritable_home(tmp_path, capsys):
    """An unwritable sentinel dir must not raise and must not crash the command."""
    unwritable = tmp_path / "nope" / "deep" / ".banner_shown_v1"
    # parent does not exist and we simulate mkdir failure by pointing at a file-as-dir
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    sentinel = blocker / ".banner_shown_v1"  # parent is a file → mkdir/write fails
    # must not raise
    result = banner.maybe_show_banner(
        sentinel=sentinel, is_tty=True, json_mode=False, env_suppressed=False
    )
    assert result in (True, False)  # either shows-without-persist or silently skips; never raises


def test_maybe_show_fail_silent_on_rich_import_error(tmp_path, monkeypatch, capsys):
    """If rendering blows up, the command continues (fail silent)."""
    monkeypatch.setattr(banner, "render_banner", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    sentinel = tmp_path / ".banner_shown_v1"
    result = banner.maybe_show_banner(
        sentinel=sentinel, is_tty=True, json_mode=False, env_suppressed=False
    )
    assert result is False  # render failed → treated as not-shown, no raise
