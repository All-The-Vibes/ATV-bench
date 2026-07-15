"""atv-bench CLI (devex T2, eng T4).

`atv-bench fingerprint --dry-run` is the consent surface: a 3-section human view
(Will publish / Scrubbed / Unknown) that lets a developer see exactly what would be
published — and, load-bearingly, that the scanner FIRED (the Scrubbed section shows
counts even when zero). `--json` emits the raw manifest for machines.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from atv_bench.fingerprint import probe as fp
from atv_bench.submit import run_preflight, submission_status_trail

app = typer.Typer(
    name="atv-bench",
    help="Community league for coding-agent harnesses: fingerprint your harness and submit a bot.",
    no_args_is_help=True,
    add_completion=False,
)


def _default_home() -> Path:
    return Path.home() / ".claude"


def _render_consent(manifest: dict) -> str:
    m = manifest
    lines = []
    lines.append(
        "Will publish:  "
        f"harness {m['harness']} · gstack {str(m['gstack']).lower()} · "
        f"{len(m['skills'])} skills · {len(m['mcps'])} MCPs · "
        f"{len(m['plugins'])} plugins · {m['custom_agents_count']} agents"
    )
    def _names(label, items, limit=6):
        shown = ", ".join(items[:limit])
        more = " …" if len(items) > limit else ""
        return f"{label}: {shown}{more}" if items else f"{label}: (none)"
    lines.append(
        "Public names:  "
        + "   ".join([
            _names("skills", m["skills"]),
            _names("mcps", m["mcps"]),
            _names("plugins", m["plugins"]),
        ])
    )
    # Scrubbed section — load-bearing. Count values the scanner withheld (names that
    # failed the safety scan). Always shown, even at 0, so consent is reassurable.
    scrubbed = [u for u in m["unknown"] if u["reason"] == "name_failed_safety_scan"]
    if scrubbed:
        fields = ", ".join(sorted({u["field"] for u in scrubbed}))
        lines.append(
            f"Scrubbed:      {len(scrubbed)} value(s) looked secret-like and were "
            f"withheld (fields: {fields}; values never shown)"
        )
    else:
        lines.append("Scrubbed:      0 values withheld (scanner ran, nothing looked secret-like)")
    # Unknown section — surfaces that couldn't be read (non-scrub reasons).
    other = [u for u in m["unknown"] if u["reason"] != "name_failed_safety_scan"]
    if other:
        parts = " · ".join(f"{u['field']}: {u['reason']}" for u in other)
        lines.append(f"Unknown:       {parts}")
    else:
        lines.append("Unknown:       (all surfaces read cleanly)")
    return "\n".join(lines)


@app.command()
def fingerprint(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the consent view (default human)."),
    json_out: bool = typer.Option(False, "--json", help="Emit the raw manifest as JSON."),
    home: Path = typer.Option(None, "--home", help="Harness config root (default ~/.claude)."),
) -> None:
    """Probe your claude-code harness and show what a submission would publish."""
    root = home or _default_home()
    result = fp.probe_claude_code(root)
    if json_out:
        typer.echo(json.dumps(result.manifest, indent=2))
        return
    # default + --dry-run both show the consent view (dry-run is the documented verb)
    typer.echo(_render_consent(result.manifest))


@app.command()
def submit(
    bot: Path = typer.Argument(None, help="Path to the harness-built bot file (e.g. main.py)."),
    game: str = typer.Option("battlesnake", "--game", help="Arena the bot targets."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run preflight + emit the submission JSON; no PR."),
    home: Path = typer.Option(None, "--home", help="Harness config root (default ~/.claude)."),
    identity: str = typer.Option("", "--identity", help="Your GitHub login (submission attribution)."),
    out: Path = typer.Option(None, "--out", help="Write the submission JSON here (default ./submission.json)."),
) -> None:
    """Open a PR carrying your bot + harness fingerprint to the league repo.

    On --dry-run this runs preflight AND writes the store-ingestable submission record
    (identity, game, bot_sha256, bot_filename, pr_url, logs_url, fingerprint) so the
    manual-PR fallback documented in CONTRIBUTING is real, not aspirational.
    """
    from atv_bench.submit import build_submission

    # preflight uses a stub runner (the gh-touching live path is a separate, honestly
    # unwired step); the contract + reporting are exercised now.
    def _stub_runner(check):
        return False, "not wired in this build (dry-run stub)"

    report = run_preflight(runner=_stub_runner)
    typer.echo("Preflight:")
    for r in report["results"]:
        mark = "✓" if r["ok"] else "✗"
        typer.echo(f"  {mark} {r['id']}: {r['description']}")
        if not r["ok"] and "fix" in r:
            typer.echo(f"      Fix: {r['fix']}")

    # Build the submission record from the real bot + probed fingerprint.
    if bot is not None:
        manifest = fp.probe_claude_code(home or _default_home()).manifest
        who = identity or "your-github-login"
        try:
            record = build_submission(
                bot_path=str(bot), fingerprint=manifest, identity=who, game=game,
            )
        except Exception as e:  # AtvError (leak/shape) — surface, don't crash
            typer.echo(f"\nCannot build submission: {e}")
            raise typer.Exit(1)
        out_path = out or Path("submission.json")
        out_path.write_text(json.dumps(record, indent=2, sort_keys=True))
        typer.echo(f"\nWrote submission record: {out_path}")

    typer.echo("\nSubmission status trail:")
    for step in submission_status_trail(is_first_time=True):
        typer.echo(f"  {step}")
    if dry_run:
        typer.echo("\n(--dry-run: no PR opened. Commit the bot + submission.json under "
                   "league/submissions/ and open a PR — see CONTRIBUTING.md#manual-pr-fallback.)")
        return
    typer.echo("\nLive PR automation is not wired in this build; use --dry-run then open a "
               "PR manually (see CONTRIBUTING.md#manual-pr-fallback).")


@app.command(name="validate-harness")
def validate_harness_cmd(
    home: Path = typer.Option(None, "--home", help="Harness config root (default ~/.claude)."),
) -> None:
    """Probe the local harness and validate its fingerprint is schema-complete + leak-safe."""
    from atv_bench.validate import validate_harness_fingerprint
    root = home or _default_home()
    manifest = fp.probe_claude_code(root).manifest
    report = validate_harness_fingerprint(manifest)
    if report["ok"]:
        typer.echo("✓ harness fingerprint is schema-complete and leak-safe")
    else:
        typer.echo("✗ harness fingerprint has issues:")
        for e in report["errors"]:
            typer.echo(f"  - {e}")
        raise typer.Exit(1)


@app.command(name="validate-game")
def validate_game_cmd(
    bot: Path = typer.Argument(..., help="Path to the bot file to validate."),
) -> None:
    """Validate a game bot's shape/size before submission."""
    from atv_bench.validate import validate_game_bot
    report = validate_game_bot(str(bot))
    if report["ok"]:
        typer.echo(f"✓ bot {bot.name} passes shape validation")
    else:
        typer.echo("✗ bot failed validation:")
        for e in report["errors"]:
            typer.echo(f"  - {e}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
