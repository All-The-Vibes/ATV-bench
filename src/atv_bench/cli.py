"""atv-bench CLI (devex T2, eng T4).

`atv-bench fingerprint --dry-run` is the consent surface: a 3-section human view
(Will publish / Scrubbed / Unknown) that lets a developer see exactly what would be
published — and, load-bearingly, that the scanner FIRED (the Scrubbed section shows
counts even when zero). `--json` emits the raw manifest for machines.
"""
from __future__ import annotations

import json
import sys
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
    live: bool = typer.Option(False, "--live", help="Open the PR live via gh (fork, branch, push, PR)."),
    home: Path = typer.Option(None, "--home", help="Harness config root (default ~/.claude)."),
    identity: str = typer.Option("", "--identity", help="Your GitHub login (submission attribution)."),
    out: Path = typer.Option(None, "--out", help="Write the submission JSON here (default ./submission.json)."),
    workdir: Path = typer.Option(None, "--workdir", help="Git worktree root for --live (default cwd)."),
) -> None:
    """Open a PR carrying your bot + harness fingerprint to the league repo.

    On --dry-run this runs preflight AND writes the store-ingestable submission record
    (identity, game, bot_sha256, bot_filename, pr_url, logs_url, fingerprint) so the
    manual-PR fallback documented in CONTRIBUTING is real, not aspirational.

    With --live it runs the gh-backed preflight and, if it passes, opens the PR end-to-end
    (fork → branch → stage under league/submissions/<identity>/ → commit → push → PR).
    """
    from atv_bench.submit import (
        build_submission,
        default_command_runner,
        gh_preflight_runner,
        open_submission_pr,
    )

    # --live uses the real gh/git-backed preflight; otherwise a stub exercises the contract
    # + reporting without touching gh. --live requires a bot and an identity.
    if live:
        if bot is None:
            typer.echo("--live requires a bot file argument.")
            raise typer.Exit(2)
        who = identity or ""
        if not who:
            typer.echo("--live requires --identity <your-github-login>.")
            raise typer.Exit(2)

        live_workdir = str(workdir or Path.cwd())

        def _live_runner(check):
            return gh_preflight_runner(check, runner=default_command_runner,
                                       bot_path=str(bot), identity=who,
                                       workdir=live_workdir)
        runner_fn = _live_runner
    else:
        def _stub_runner(check):
            return False, "not wired in this build (dry-run stub)"
        runner_fn = _stub_runner

    report = run_preflight(runner=runner_fn)
    typer.echo("Preflight:")
    for r in report["results"]:
        mark = "✓" if r["ok"] else "✗"
        typer.echo(f"  {mark} {r['id']}: {r['description']}")
        if not r["ok"] and "fix" in r:
            typer.echo(f"      Fix: {r['fix']}")

    # Build the submission record from the real bot + probed fingerprint.
    record = None
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

    if live:
        # Fail closed: only open the PR if preflight passed.
        if not report["passed"]:
            typer.echo("\nPreflight failed; not opening a PR. Fix the ✗ items above and retry.")
            raise typer.Exit(1)
        try:
            result = open_submission_pr(
                record=record, bot_path=str(bot), identity=identity,
                workdir=str(workdir or Path.cwd()),
            )
        except Exception as e:  # AtvError (SUBMIT_PR_FAILED) — surface, don't crash
            typer.echo(f"\nLive submission failed: {e}")
            raise typer.Exit(1)
        typer.echo(f"\n✓ Opened submission PR: {result['pr_url']}")
        return

    if dry_run:
        typer.echo("\n(--dry-run: no PR opened. Commit the bot + submission.json under "
                   "league/submissions/ and open a PR — see CONTRIBUTING.md#manual-pr-fallback.)")
        return
    typer.echo("\nNo --live flag: PR not opened. Re-run with --live to open it via gh, or "
               "use --dry-run then open a PR manually (see CONTRIBUTING.md#manual-pr-fallback).")


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


@app.command(name="validate-pr-paths")
def validate_pr_paths_cmd(
    author: str = typer.Option(..., "--author", help="PR author GitHub login."),
    paths_file: Path = typer.Option(
        None, "--paths-file",
        help="File with one changed path per line (default: read stdin).",
    ),
) -> None:
    """Fail closed if a community PR touches anything outside its own submission tree.

    Wire into CI as a gate on community submission PRs: `git diff --name-only base...head`
    -> this command. Rejects direct league/matches.jsonl edits, other-entrant directories,
    and stray files, so a merged PR cannot forge history or poison another row.
    """
    from atv_bench.validate import validate_pr_paths
    if paths_file is not None:
        text = paths_file.read_text()
    else:
        text = sys.stdin.read()
    changed = [ln.strip() for ln in text.splitlines() if ln.strip()]
    report = validate_pr_paths(author, changed)
    if report["ok"]:
        typer.echo(f"✓ PR by {author} touches only its own submission files")
    else:
        typer.echo("✗ PR touches paths outside its own submission tree:")
        for e in report["errors"]:
            typer.echo(f"  - {e}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
