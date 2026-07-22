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
from atv_bench.games import GAMES, DEFAULT_GAME, assert_playable
from atv_bench.harnesses import HARNESSES, DEFAULT_HARNESS, detect_harness, harness_config_present
from atv_bench.submit import run_preflight, submission_status_trail

app = typer.Typer(
    name="atv-bench",
    help="Community league for coding-agent harnesses: fingerprint your harness and submit a bot.",
    no_args_is_help=True,
    add_completion=False,
)


def _probe_or_exit(home: Path | None, harness: str | None) -> fp.ProbeResult:
    """Probe the resolved harness, or print an actionable message and exit(2).

    Centralizes the fail-closed handling so every probing command (fingerprint / submit /
    validate-harness) rejects an unknown or planned harness the same way instead of
    emitting an empty/placeholder fingerprint.

    Detect-guard (M10): when the harness is being AUTO-detected (no explicit --harness)
    against the real $HOME (no --home override) and more than one live harness config is
    present, refuse to silently pick the first — require an explicit --harness so the user
    controls which harness is published.
    """
    from atv_bench import harnesses as hz

    if harness is None and home is None:
        detected = [h.key for h in HARNESSES if h.live
                    and hz.harness_config_present(h.key)]
        if len(detected) > 1:
            typer.echo(
                "Multiple coding-agent harnesses detected on this machine: "
                f"{', '.join(detected)}.\n"
                "Auto-detect won't guess which one to publish. Re-run with an explicit "
                "harness, e.g. `atv-bench fingerprint --harness "
                f"{detected[0]}` (see `atv-bench harnesses`)."
            )
            raise typer.Exit(2)

    try:
        result = fp.probe(home=home, harness=harness)
    except ValueError as e:
        typer.echo(f"Cannot fingerprint: {e}")
        raise typer.Exit(2)

    # M9: an explicitly-probed harness whose config is absent/empty must not present as a
    # confident published fingerprint. Surface an actionable problem/cause/fix message.
    # The manifest's harness is the source of truth — probe() already resolved it from
    # --harness, else the --home root basename, else $HOME auto-detect.
    resolved = (result.manifest.get("harness")
                or harness or hz.detect_harness() or hz.DEFAULT_HARNESS)
    _warn_if_config_absent(resolved, home, result)
    return result


def _warn_if_config_absent(harness_key: str, home: Path | None, result: fp.ProbeResult) -> None:
    """Fail loudly (exit 2) when the harness's primary config file is missing, empty, or
    malformed, so an empty manifest never passes silently as a real fingerprint (M9).

    Missing is caught by a file-existence check; empty/malformed is caught by inspecting
    the probe result — the readers surface an unknown[{field:"model", reason:"empty"|
    "malformed"}] entry when the primary config parsed to nothing usable."""
    from atv_bench import harnesses as hz
    from atv_bench.fingerprint import reader as _reader

    root = Path(home) if home is not None else hz.config_root_for(harness_key)
    primary = hz.PRIMARY_CONFIG.get(harness_key)
    if primary is None:
        return
    primary_path = root / primary
    # A dangling symlink is NOT "missing" — the file is present as a link, just unreadable.
    # Treat it as present here so the accurate empty/malformed/unreadable branch below fires
    # (the probe already flagged it not_readable) rather than the "missing file" message.
    if not primary_path.exists() and not primary_path.is_symlink():
        typer.echo(
            f"Cannot fingerprint {harness_key}: no {primary} found in {root}.\n"
            f"  problem: the harness config file is missing, so the fingerprint would be empty.\n"
            f"  cause:   {harness_key} is not set up at {root}, or the wrong --home was passed.\n"
            f"  fix:     run {harness_key} at least once to create {primary}, or pass the "
            f"correct --home / --harness (see `atv-bench harnesses`)."
        )
        raise typer.Exit(2)

    # File exists but is unusable (empty / malformed / unreadable / symlink-escaped): the
    # readers flag the model field as unknown with one of these reasons. Fail closed there
    # too — the published fingerprint would be an empty shell reading as a confident one.
    unusable = {
        _reader.REASON_EMPTY, _reader.REASON_MALFORMED,
        _reader.REASON_PERMISSION, _reader.REASON_SYMLINK_ESCAPE,
        _reader.REASON_NOT_READABLE,
    }
    model_bad = any(
        u.get("field") == "model" and u.get("reason") in unusable
        for u in result.manifest.get("unknown", [])
    )
    if model_bad:
        typer.echo(
            f"Cannot fingerprint {harness_key}: {primary} in {root} is empty, malformed, "
            f"or unreadable.\n"
            f"  problem: the harness config file has no usable content, so the fingerprint "
            f"would be an empty shell.\n"
            f"  cause:   {primary} is blank, not valid "
            f"{'TOML' if primary.endswith('.toml') else 'JSON'}, or not readable "
            f"(permissions / symlink).\n"
            f"  fix:     repair or re-generate {primary} (run {harness_key} so it rewrites a "
            f"valid config), then re-run (see `atv-bench harnesses`)."
        )
        raise typer.Exit(2)


def _render_consent(manifest: dict) -> str:
    m = manifest
    lines = []
    lines.append(
        "Will publish:  "
        f"harness {m['harness']} · model {m['model']} · gstack {str(m['gstack']).lower()} · "
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


def _render_full_assessment(manifest: dict, harness_key: str | None = None) -> str:
    """A COMPLETE, untruncated read-back of what the fingerprint found in the harness.

    Unlike the one-line consent view (which truncates lists for a quick glance), this
    prints the model plus every skill / MCP / plugin name and the agent count — the full
    inventory of what makes this harness what it is.
    """
    m = manifest
    hb = "═" * 64

    def _block(label: str, items: list[str]) -> list[str]:
        out = [f"\n  {label} ({len(items)}):"]
        if not items:
            out.append("    (none)")
        else:
            for name in items:
                out.append(f"    • {name}")
        return out

    cli = m.get("cli_version") or {}
    cli_line = "unknown"
    if isinstance(cli, dict) and cli.get("version", "unknown") != "unknown":
        sha = cli.get("sha256", "unknown")
        sha_disp = sha[:12] if isinstance(sha, str) and sha != "unknown" else "unknown"
        cli_line = f"{cli.get('version')}  (sha256:{sha_disp})"

    tools = m.get("tools", [])
    tool_lines = [f"{t['name']} [{t['source']}{'' if t['enabled'] else ' · off'}]"
                  for t in tools]

    lines = [
        hb,
        f"  HARNESS ASSESSMENT — {harness_key or m['harness']}",
        hb,
        f"  Harness type : {m['harness']}",
        f"  Model        : {m['model']}",
        f"  CLI runtime  : {cli_line}",
        f"  gstack       : {str(m['gstack']).lower()}",
        f"  Custom agents: {m['custom_agents_count']}",
        f"  Totals       : {len(m['skills'])} skills · {len(m.get('nested_skills', []))} "
        f"nested · {len(tools)} tools · {len(m['mcps'])} MCP servers · "
        f"{len(m['plugins'])} plugins",
    ]
    lines += _block("Skills", m["skills"])
    lines += _block("Nested skills (plugin-provided)", m.get("nested_skills", []))
    lines += _block("Tools", tool_lines)
    lines += _block("MCP servers", m["mcps"])
    lines += _block("Plugins", m["plugins"])
    # Surface anything the scanner withheld or couldn't read, honestly.
    scrubbed = [u for u in m["unknown"] if u["reason"] == "name_failed_safety_scan"]
    other = [u for u in m["unknown"] if u["reason"] != "name_failed_safety_scan"]
    lines.append("")
    if scrubbed:
        fields = ", ".join(sorted({u["field"] for u in scrubbed}))
        lines.append(f"  Scrubbed     : {len(scrubbed)} secret-like value(s) withheld "
                     f"(fields: {fields})")
    else:
        lines.append("  Scrubbed     : 0 (scanner ran, nothing looked secret-like)")
    if other:
        parts = "; ".join(f"{u['field']}: {u['reason']}" for u in other)
        lines.append(f"  Unread       : {parts}")
    else:
        lines.append("  Unread       : (all surfaces read cleanly)")
    # Runtime honesty: names of config dirs are not the whole harness.
    runtime_unknown = m.get("unknown_runtime", [])
    if runtime_unknown:
        parts = "; ".join(f"{u['field']}: {u['reason']}" for u in runtime_unknown)
        lines.append(f"  Runtime gaps : {parts}")
    lines.append(hb)
    return "\n".join(lines)


@app.command()
def fingerprint(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the consent view (default human)."),
    full: bool = typer.Option(False, "--full", help="Full read-back: model + EVERY skill/MCP/plugin/agent (untruncated)."),
    json_out: bool = typer.Option(False, "--json", help="Emit the raw manifest as JSON."),
    harness: str = typer.Option(None, "--harness", help="Harness to probe (default: auto-detect; see `atv-bench harnesses`)."),
    home: Path = typer.Option(None, "--home", help="Harness config root (default: harness's standard dir under $HOME)."),
) -> None:
    """Probe your coding-agent harness and show what a submission would publish."""
    result = _probe_or_exit(home, harness)
    if json_out:
        typer.echo(json.dumps(result.manifest, indent=2))
        return
    if full:
        typer.echo(_render_full_assessment(result.manifest, harness))
        return
    # default + --dry-run both show the consent view (dry-run is the documented verb)
    typer.echo(_render_consent(result.manifest))


@app.command()
def submit(
    bot: Path = typer.Argument(None, help="Path to the harness-built bot file (e.g. main.py)."),
    game: str = typer.Option(DEFAULT_GAME, "--game", help="Arena the bot targets (see `atv-bench games`)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run preflight + emit the submission JSON; no PR."),
    live: bool = typer.Option(False, "--live", help="Open the PR live via gh (fork, branch, push, PR)."),
    harness: str = typer.Option(None, "--harness", help="Harness to fingerprint (default: auto-detect; see `atv-bench harnesses`)."),
    home: Path = typer.Option(None, "--home", help="Harness config root (default: harness's standard dir under $HOME)."),
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

    # Fail closed on a game with no trusted arena (santa-league integrity): a bot for a
    # planned/unknown game can never be adjudicated, so reject it here before any PR work
    # rather than accepting a dead submission the match job will only forfeit.
    try:
        assert_playable(game)
    except ValueError as e:
        typer.echo(f"Cannot submit: {e}")
        raise typer.Exit(2)

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
        manifest = _probe_or_exit(home, harness).manifest
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

        # UC1 provenance: the record binds harness+bot+fingerprint into a token. Report the
        # tier the CURRENT (Phase-1, keyless) board will assign — NOT the tier a key-holding
        # local verify would grant. A contributor who set ATV_PROVENANCE_KEY built an
        # HMAC-signed token, but the Phase-1 board holds no key and publishes the row as
        # self-attested until a trusted sandbox re-signs (Phase 2). Verify keyless here so
        # the reported tier matches what the board will actually show — never over-claim.
        from atv_bench.submit import verify_submission_provenance
        board_res = verify_submission_provenance(record, bot_path=str(bot), key=None)
        prov = record["provenance"]
        keyed_build = bool(prov.get("signed"))
        if board_res.ok:
            # board tier is self-attested in Phase 1 (keyless); board_res.signed is False.
            typer.echo(f"Provenance: bound to harness={prov['harness']} "
                       f"bot+fingerprint — self-attested (unkeyed) on the current board.")
            if keyed_build:
                typer.echo("  Your token is HMAC-signed (ATV_PROVENANCE_KEY set), but the "
                           "Phase-1 board is keyless, so the row publishes as self-attested "
                           "until a trusted sandbox re-fingerprints and re-signs "
                           "(COMMUNITY_LEAGUE.md#provenance).")
            else:
                typer.echo("  Set ATV_PROVENANCE_KEY before building for an HMAC token; rows "
                           "stay self-attested until a trusted sandbox re-fingerprints "
                           "(COMMUNITY_LEAGUE.md#provenance).")
        else:
            typer.echo("Provenance: ✗ does not verify — "
                       + "; ".join(board_res.reasons))

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
    harness: str = typer.Option(None, "--harness", help="Harness to probe (default: auto-detect; see `atv-bench harnesses`)."),
    home: Path = typer.Option(None, "--home", help="Harness config root (default: harness's standard dir under $HOME)."),
) -> None:
    """Probe the local harness and validate its fingerprint is schema-complete + leak-safe."""
    from atv_bench import validate as _validate
    from atv_bench import harnesses as hz

    result = _probe_or_exit(home, harness)
    manifest = result.manifest
    resolved = manifest.get("harness") or harness or hz.detect_harness() or hz.DEFAULT_HARNESS
    report = _validate.validate_harness_fingerprint(manifest)
    if report["ok"]:
        typer.echo(f"✓ {resolved} harness fingerprint is schema-complete and leak-safe")
    else:
        typer.echo(f"✗ {resolved} harness fingerprint has issues — fix before submitting:")
        for e in report["errors"]:
            typer.echo(f"  - {e}")
        typer.echo(
            "  fix: adjust your reader / config so every published name passes the safety "
            "scan and the schema is complete, then re-run `atv-bench validate-harness "
            f"--harness {resolved}`. See CONTRIBUTING.md → Add a harness adapter."
        )
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
        help="File with changed paths (default: read stdin).",
    ),
    name_status: bool = typer.Option(
        False, "--name-status",
        help="Input is `git diff --name-status` output (rejects renames/deletes and "
             "confines only submission PRs). Preferred for the always-on CI gate.",
    ),
) -> None:
    """Fail closed if a community submission PR touches anything outside its own tree.

    Wire into CI as an ALWAYS-ON required check on every PR:
      git diff --name-status <base>...<head> | atv-bench validate-pr-paths --author <login> --name-status
    With --name-status: a PR touching league/submissions/** is a submission PR and is
    confined to its own league/submissions/<author>/{main.py,submission.json}; renames,
    deletes, and any other path (incl .github/workflows/**, league/matches.jsonl) fail
    closed. A pure plumbing PR (no submissions/**) passes for normal review.
    Legacy --name-only mode (no flag) confines against a plain path list.
    """
    from atv_bench.validate import validate_pr_paths, validate_pr_changes
    if paths_file is not None:
        text = paths_file.read_text()
    else:
        text = sys.stdin.read()
    lines = [ln.rstrip("\n") for ln in text.splitlines() if ln.strip()]
    if name_status:
        report = validate_pr_changes(author, lines)
        if report["ok"]:
            kind = "submission PR (confined to own files)" if report["is_submission_pr"] \
                else "non-submission PR (not confined)"
            typer.echo(f"✓ PR by {author}: {kind}")
        else:
            typer.echo("✗ PR is not confined to its own submission tree:")
            for e in report["errors"]:
                typer.echo(f"  - {e}")
            raise typer.Exit(1)
        return
    report = validate_pr_paths(author, [ln.strip() for ln in lines])
    if report["ok"]:
        typer.echo(f"✓ PR by {author} touches only its own submission files")
    else:
        typer.echo("✗ PR touches paths outside its own submission tree:")
        for e in report["errors"]:
            typer.echo(f"  - {e}")
        raise typer.Exit(1)


@app.command()
def harnesses(
    json_out: bool = typer.Option(False, "--json", help="Emit the harnesses list as JSON."),
) -> None:
    """List the coding-agent harnesses you can fingerprint (which are live vs. planned)."""
    detected = detect_harness()
    # Mirror the M10 detect-guard: if >1 live harness config is present, auto-detect is
    # ambiguous and the probing commands refuse to guess — so this listing must NOT claim
    # a single confident default either. Both surfaces tell the same story.
    live_present = [h.key for h in HARNESSES if h.live
                    and harness_config_present(h.key)]
    ambiguous = len(live_present) > 1
    # When ambiguous, no single harness is "the detected one" — the probing commands
    # refuse to guess, so neither surface may stamp a winner.
    marked = None if ambiguous else detected
    if json_out:
        payload = [
            {"key": h.key, "title": h.title, "live": h.live,
             "config_root": h.config_root, "summary": h.summary,
             "detected": h.key == marked}
            for h in HARNESSES
        ]
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo("Harnesses you can fingerprint with `atv-bench fingerprint [--harness <key>]`:\n")
    for h in HARNESSES:
        status = "live" if h.live else "planned"
        mark = "✓" if h.live else "·"
        here = "  ← detected on this machine" if h.key == marked else ""
        typer.echo(f"  {mark} {h.key}  [{status}]  — {h.title}{here}")
        typer.echo(f"      {h.summary}")
    if ambiguous:
        typer.echo(
            f"\nMultiple harnesses detected ({', '.join(live_present)}): auto-detect is "
            "ambiguous. Name one explicitly with `--harness <key>` — the probing commands "
            "won't guess which to publish."
        )
    else:
        default_note = detected or DEFAULT_HARNESS
        typer.echo(f"\nDefault (auto-detected): {default_note}. "
                   f"Override with `--harness <key>`.")


@app.command()
def games(
    json_out: bool = typer.Option(False, "--json", help="Emit the games list as JSON."),
) -> None:
    """List the arenas you can submit a bot to (which are live vs. planned)."""
    if json_out:
        payload = [
            {"key": g.key, "title": g.title, "live": g.live,
             "entrypoint": g.entrypoint, "summary": g.summary}
            for g in GAMES
        ]
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo("Games you can target with `atv-bench submit --game <key>`:\n")
    for g in GAMES:
        status = "live" if g.live else "planned"
        mark = "✓" if g.live else "·"
        typer.echo(f"  {mark} {g.key}  [{status}]  — {g.title}")
        typer.echo(f"      {g.summary}")
    typer.echo(f"\nDefault: {DEFAULT_GAME}. Bot entrypoint: main.py.")


@app.command()
def bots(
    json_out: bool = typer.Option(False, "--json", help="Emit the bots list as JSON."),
) -> None:
    """List the local opponents you can play the visualization against (`atv-bench play`)."""
    from atv_bench.bots import BOTS, DEFAULT_OPPONENT

    if json_out:
        payload = [{"key": b.key, "title": b.title, "summary": b.summary} for b in BOTS]
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo("Local opponents for `atv-bench play --opponent <key>`:\n")
    for b in BOTS:
        typer.echo(f"  • {b.key}  — {b.title}")
        typer.echo(f"      {b.summary}")
    typer.echo(f"\nDefault opponent: {DEFAULT_OPPONENT}. "
               f"Play your own bot with `--player-bot path/to/main.py`.")


@app.command()
def play(
    game: str = typer.Option(DEFAULT_GAME, "--game", help="Arena to play (see `atv-bench games`)."),
    player: str = typer.Option(None, "--player", help="Named bot to play as (see `atv-bench bots`)."),
    player_bot: Path = typer.Option(None, "--player-bot", help="Path to YOUR harness-built bot file (main.py) to play as."),
    opponent: str = typer.Option(None, "--opponent", help="Named opponent bot (default: greedy anchor)."),
    opponent_bot: Path = typer.Option(None, "--opponent-bot", help="Path to a harness-built bot file to play AS the opponent (harness-vs-harness)."),
    seed: int = typer.Option(0, "--seed", help="Match label/id (matches are already fully deterministic; seed only labels the replay)."),
    out: Path = typer.Option(None, "--out", help="Where to write the replay (default: ./_replay)."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the animated replay in a browser."),
) -> None:
    """Run a REAL refereed match locally and watch it — your bot vs the opponent series.

    This is the honest, un-mocked visualization: the same trusted engine + referee the
    sandboxed arena uses adjudicates the match from real gameplay. Pick a named bot with
    `--player` or your own harness-built bot with `--player-bot main.py`, choose an
    `--opponent` from `atv-bench bots`, and it prints an ASCII board + writes an animated
    HTML replay you can scrub through.

        atv-bench play --player bare --opponent greedy
        atv-bench play --player-bot main.py --opponent wall_hugger
        atv-bench play --player-bot mine.py --opponent-bot theirs.py   # harness vs harness
    """
    from atv_bench.bots import DEFAULT_OPPONENT
    from atv_bench.play import Contestant, build_replay_html, render_ascii, run_local_match

    if player_bot is not None and player is not None:
        typer.echo("Pick one of --player <bot> or --player-bot <file>, not both.")
        raise typer.Exit(2)
    if opponent_bot is not None and opponent is not None:
        typer.echo("Pick one of --opponent <bot> or --opponent-bot <file>, not both.")
        raise typer.Exit(2)
    if player_bot is not None:
        if not player_bot.is_file():
            typer.echo(f"No bot file at {player_bot}.")
            raise typer.Exit(2)
        me = Contestant(bot_path=str(player_bot), label=player_bot.stem)
    else:
        me = Contestant(key=player or "bare")
    if opponent_bot is not None:
        if not opponent_bot.is_file():
            typer.echo(f"No bot file at {opponent_bot}.")
            raise typer.Exit(2)
        opp = Contestant(bot_path=str(opponent_bot), label=opponent_bot.stem)
    else:
        opp = Contestant(key=opponent or DEFAULT_OPPONENT)

    try:
        result = run_local_match(game=game, player=me, opponent=opp, seed=seed)
    except ValueError as e:
        typer.echo(f"Cannot play: {e}")
        raise typer.Exit(2)

    typer.echo(render_ascii(result))
    out_dir = out or Path("_replay")
    replay = build_replay_html(result, out_dir, game=game, seed=seed)
    typer.echo(f"\n✓ Wrote animated replay: {replay}")
    if open_browser:
        _serve_and_open(replay.parent, index=replay.name)
    else:
        typer.echo(f"  Open it: open {replay}  (or serve: python -m http.server --directory {replay.parent})")


@app.command()
def board(
    store: Path = typer.Option(None, "--store", help="League store dir (default: ./league)."),
    out: Path = typer.Option(None, "--out", help="Where to write the static board (default: ./_board)."),
    demo: bool = typer.Option(False, "--demo", help="Build a populated sample board (no store needed)."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the board in a browser."),
) -> None:
    """Build the leaderboard locally and open it — see where every harness ranks.

    Renders the same static site the GitHub Action publishes, from your local league
    store (submissions + match history). With --demo it fabricates a populated sample
    board so you can see the ranking UI before you've submitted anything. The viewer
    HTML is bundled in the package, so this works from an installed tool with no clone.
    """
    from atv_bench.publish import build_site

    out_dir = out or Path("_board")

    tmp_store: Path | None = None
    if demo:
        import tempfile
        from atv_bench.demo import build_demo_store
        tmp_store = Path(tempfile.mkdtemp(prefix="atv-demo-store-"))
        build_demo_store(str(tmp_store))
        store_dir = str(tmp_store)
    else:
        store_dir = str(store or Path("league"))
        if not Path(store_dir).exists():
            typer.echo(
                f"No league store at {store_dir}. Options:\n"
                f"  • `atv-bench board --demo` to see a populated sample board, or\n"
                f"  • point --store at a checkout's league/ dir, or\n"
                f"  • view the live board at https://all-the-vibes.github.io/ATV-bench/"
            )
            raise typer.Exit(1)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        site = build_site(str(out_dir), store_dir=store_dir, updated_at=now)
    finally:
        # The demo store is throwaway: build_site has already read it and written the
        # self-contained site (json + html), so drop the temp dir now rather than leak it.
        if tmp_store is not None:
            import shutil
            shutil.rmtree(tmp_store, ignore_errors=True)
    index = site / "index.html"
    doc_path = site / "leaderboard.json"
    rows = json.loads(doc_path.read_text()).get("rows", [])
    typer.echo(f"✓ Built board with {len(rows)} row(s): {index}")
    if not rows and not demo:
        typer.echo("  (empty — no submissions in this store yet. Try `atv-bench board --demo`.)")

    # The board is a static file; fetch() needs http (file:// blocks it). Serve it
    # locally and open that, unless --no-open (tests + CI use --no-open).
    if open_browser:
        _serve_and_open(site)
    else:
        typer.echo(f"  Open it with: python -m http.server --directory {site}")


def _serve_and_open(site: Path, index: str = "index.html") -> None:
    """Serve `site` on a local port and open a browser at it (fetch needs http, not file://)."""
    import functools
    import http.server
    import threading
    import webbrowser

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/{index}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    typer.echo(f"  Serving at {url} (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        thread.join()
    except KeyboardInterrupt:
        httpd.shutdown()
        typer.echo("\nStopped.")


def _default_demo_bots() -> tuple[str, str]:
    """Paths to the two DISTINCT bundled sample bots for the zero-setup demo.

    Player A defaults to greedy_survivor (keeps heading, else first safe neighbor);
    player B defaults to wall_hugger (steers to the nearest wall and traces it). They
    must be different files — defaulting both players to the same bot made the demo a
    deterministic mirror self-play that always drew, producing a flat 1500/1500 board.
    """
    from atv_bench.arena import sample_bots

    base = Path(sample_bots.__file__).parent
    return str(base / "greedy_survivor.py"), str(base / "wall_hugger.py")


def _record_demo_match(
    store_dir: str, result: dict, a_name: str, b_name: str,
    a_bot_label: str, b_bot_label: str,
) -> None:
    """Seed the two demo players + the match they just played into the demo store.

    The demo's whole point is "play a match, then see IT on the board". Without this,
    Act 3 shows only the canned build_demo_store roster and the two bots the user just
    watched (a_name vs b_name) never appear. We add a submission for each and record the
    just-played outcome.

    IMPORTANT — the winner is decided by PLAY, never by the fingerprint. The fingerprint
    is only metadata describing which bot/harness produced the entry; it never touches
    adjudication. The bundled sample bots are plain scripts, NOT built by a real harness,
    so we label each entrant by the STRATEGY it actually ran (harness = "sample-bot")
    rather than fabricating a "claude-code beat copilot-cli" story the match never earned.
    Whichever bot survives longer wins; the ELO simply follows that result.
    """
    from atv_bench.store import LeagueStore
    from atv_bench.elo import MIN_RATED_MATCHES

    store = LeagueStore(store_dir)

    def _fingerprint(skill: str) -> dict:
        # Harness-neutral: these are bundled sample scripts, not harness-authored bots.
        return {
            "harness": "sample-bot", "model": "demo", "gstack": False,
            "skills": [skill], "mcps": [], "plugins": [], "custom_agents_count": 0,
            "probe_version": "1.0.0", "unknown": [],
        }

    entrants = (
        (a_name, a_bot_label),
        (b_name, b_bot_label),
    )
    for identity, skill in entrants:
        store.add_submission({
            "identity": identity,
            "game": "lightcycles",
            "bot_sha256": (identity.encode().hex() * 8)[:64].ljust(64, "0"),
            "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
            "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
            "fingerprint": _fingerprint(skill),
        })

    outcome = result.get("outcome", "draw")
    # Replay the identical adjudicated outcome across distinct match_ids so the pairing
    # clears MIN_RATED_MATCHES and shows as a real rated row, not "waiting for opponent".
    # This repeats the match the bots ACTUALLY played — it does not invent a result.
    for i in range(MIN_RATED_MATCHES + 2):
        store.append_match({
            "player_a": a_name,
            "player_b": b_name,
            "outcome": outcome,
            "match_id": f"demo-match-{i}",
            "game": "lightcycles",
            "seed": i,
        })



@app.command(name="demo-match")
def demo_match_cmd(
    a_bot: Path = typer.Option(None, "--a-bot", help="Bot file for player A (default: bundled sample)."),
    b_bot: Path = typer.Option(None, "--b-bot", help="Bot file for player B (default: bundled sample)."),
    a_name: str = typer.Option("ATV-StarterKit", "--a-name", help="Display name for player A."),
    b_name: str = typer.Option("ATV-Phoenix", "--b-name", help="Display name for player B."),
    terminal: bool = typer.Option(False, "--terminal",
                                  help="Render the feed in the terminal instead of the browser."),
    open_browser: bool = typer.Option(True, "--open/--no-open",
                                      help="Browser mode: open a browser. --no-open serves the URL without launching/blocking."),
    turn_delay: float = typer.Option(0.12, "--turn-delay",
                                     help="Browser mode: seconds between streamed turns (watchability)."),
    live: bool = typer.Option(True, "--live/--no-live",
                              help="Terminal mode: animate the feed with a per-turn delay (--no-live for CI/scripts)."),
    board: bool = typer.Option(True, "--board/--no-board",
                               help="Terminal mode: after the match, build the leaderboard + insights."),
    seed: int = typer.Option(0, "--seed", help="Trusted engine seed (reproducible match)."),
) -> None:
    """Full head-to-head demo experience (prefer `run --demo` for a quick look).

    Use `run --demo` for the fast, agent-friendly one-shot demo envelope. Use this
    `demo-match` when you want the rich three-act experience with a live feed;
    use `play` to pit your own two bots, and `board` to view the leaderboard alone.

    Play two harness bots head-to-head in Tron with a live feed, then show the board.

    The demo in three acts: (1) two named harnesses enter, (2) a live turn-by-turn Tron
    feed, (3) the leaderboard + gstack insights. With no bot paths it uses two distinct
    bundled sample bots (greedy-survivor vs wall-hugger) so the demo runs with zero setup
    and produces a real, decisive head-to-head.

    Default surface is the BROWSER: a canvas Tron feed streamed live over SSE, then the
    leaderboard + insights reveal on the same page. Use --terminal for the in-terminal
    ASCII feed (what CI/scripts use), or --no-open to serve the browser URL without
    launching a browser or blocking.
    """
    default_a, default_b = _default_demo_bots()
    a_path = str(a_bot) if a_bot is not None else default_a
    b_path = str(b_bot) if b_bot is not None else default_b
    for _label, _p in ((a_name, a_path), (b_name, b_path)):
        if not Path(_p).is_file():
            typer.echo(f"Bot for {_label} not found: {_p}")
            raise typer.Exit(2)

    # Default surface: browser SSE live stream (Act 2 live feed + Act 3 board, one page).
    # --no-live / --no-board are terminal-only knobs; using either implies the terminal
    # path (backward compatible with pre-browser scripts + CI invocations).
    use_terminal = terminal or (not live) or (not board)
    if not use_terminal:
        from atv_bench.arena.live_server import serve_live_match
        typer.echo(f"\n  {a_name}  ⚔  {b_name}   —  Lightcycles (Tron), live in your browser\n")
        serve_live_match(
            a_bot=a_path, b_bot=b_path, a_name=a_name, b_name=b_name,
            seed=seed, turn_delay=turn_delay, open_browser=open_browser,
            echo=typer.echo,
        )
        return

    import time

    from atv_bench.arena.engine import Direction, TronEngine
    from atv_bench.arena.referee import SubprocessMoveSource, run_match
    from atv_bench.arena.render import render_frame

    default_a, default_b = _default_demo_bots()
    a_path = str(a_bot) if a_bot is not None else default_a
    b_path = str(b_bot) if b_bot is not None else default_b

    # Strategy label = the bot file's stem (e.g. "greedy_survivor"), so the board row
    # describes the bot that actually played rather than a fabricated harness identity.
    a_bot_label = Path(a_path).stem
    b_bot_label = Path(b_path).stem

    for label, p in ((a_name, a_path), (b_name, b_path)):
        if not Path(p).is_file():
            typer.echo(f"Bot for {label} not found: {p}")
            raise typer.Exit(2)

    board_w = board_h = 25
    # Deliberately ASYMMETRIC starts. A point-symmetric arena (corner vs opposite corner,
    # mirrored directions) forces the two bots to reach a fatal cell on the SAME turn →
    # mutual crash → draw, no matter how differently they play. Offsetting player B's row
    # breaks that mirror so a real skill difference decides the match (and the board shows
    # a genuine ELO spread instead of a flat 1500/1500).
    engine = TronEngine(
        width=board_w, height=board_h,
        start_a=(1, 1), start_b=(board_w - 2, board_h - 5),
        dir_a=Direction.RIGHT, dir_b=Direction.LEFT, max_turns=400,
    )

    typer.echo(f"\n  {a_name}  ⚔  {b_name}   —  Lightcycles (Tron)\n")

    def _observe(state):
        frame = render_frame(state, engine, label_a=a_name, label_b=b_name)
        if live:
            # Clear + redraw for an in-place animation in a real terminal.
            typer.echo("\x1b[2J\x1b[H" + frame)
            time.sleep(0.06)
        else:
            typer.echo(frame)

    source_a = SubprocessMoveSource([sys.executable, a_path], per_turn_timeout=2.0)
    source_b = SubprocessMoveSource([sys.executable, b_path], per_turn_timeout=2.0)
    try:
        result = run_match(
            engine, source_a, source_b,
            player_a=a_name, player_b=b_name, match_id="demo-local",
            game="lightcycles", seed=seed, observer=_observe,
        )
    finally:
        source_a.close()
        source_b.close()

    outcome = result.get("outcome")
    winner = {"a_wins": a_name, "b_wins": b_name}.get(outcome)
    if winner:
        typer.echo(f"\n★ Result: {winner} wins ({outcome}).")
    else:
        typer.echo(f"\n— Result: draw between {a_name} and {b_name}.")

    if not board:
        return

    # Act 3: record the match into a throwaway store, build the board, show insights.
    import tempfile
    import shutil
    from datetime import datetime, timezone
    from atv_bench.demo import build_demo_store
    from atv_bench.store import LeagueStore
    from atv_bench.publish import build_site
    from atv_bench.leaderboard import build_insights

    tmp_store = Path(tempfile.mkdtemp(prefix="atv-demo-match-"))
    out_dir = Path(tempfile.mkdtemp(prefix="atv-demo-board-"))
    try:
        build_demo_store(str(tmp_store))
        # Record the match that JUST played so Act 3's board reflects it — otherwise the
        # user watches ATV-StarterKit vs ATV-Phoenix, then sees an unrelated canned roster.
        _record_demo_match(str(tmp_store), result, a_name, b_name, a_bot_label, b_bot_label)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        site = build_site(str(out_dir), store_dir=str(tmp_store), updated_at=now)
        doc = json.loads((site / "leaderboard.json").read_text())
        rows = doc.get("rows", [])

        # Section 6 typed-rank guard: an unverified board never prints a rank. Route the
        # gate through the single renderer so a free-text rank leak is impossible here.
        from atv_bench.render import render_ranking, UnrankedView

        gate = render_ranking({"ratings": {"verified": doc.get("verified", True)}},
                              verified=doc.get("verified", True))
        if isinstance(gate, UnrankedView):
            typer.echo("\n=== Leaderboard ===")
            typer.echo(str(gate))
        else:
            typer.echo("\n=== Leaderboard ===")
            for r in rows:
                typer.echo(
                    f"  #{r.get('rank')}  {round(float(r.get('elo', 0)))} ELO  "
                    f"@{r.get('identity')} ({r.get('harness_name')})  "
                    f"— {r.get('fingerprint_summary', '')}"
                )
            typer.echo("\n=== Insights ===")
            for line in build_insights(rows):
                typer.echo(f"  • {line}")
        typer.echo(f"\n  Static board written to: {site / 'index.html'}")
    finally:
        shutil.rmtree(tmp_store, ignore_errors=True)
        # Leave the built board on disk for the user to open; only clean the store.


@app.command()
def doctor(
    harness: str = typer.Option(None, "--harness", help="Harness to check for (default: auto-detect; see `atv-bench harnesses`)."),
    home: Path = typer.Option(None, "--home", help="Harness config root (default: harness's standard dir under $HOME)."),
) -> None:
    """Preflight: is your environment ready to fingerprint, submit, and run matches?

    Reports readiness for each capability with an actionable fix for anything missing.
    Never fails the process — it's a diagnostic, so it always exits 0 and lets you read
    the full report.
    """
    import shutil
    import subprocess

    from atv_bench import harnesses as hz

    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok_py = sys.version_info >= (3, 11)
    lines: list[str] = []
    lines.append(f"  {'✓' if ok_py else '✗'} Python {py}" + ("" if ok_py else " (need >= 3.11)"))

    # Resolve which harness we're reporting on: explicit --harness, else auto-detect.
    detected = detect_harness()
    key = harness or detected or DEFAULT_HARNESS
    h = hz.get_harness(key)
    root = Path(home) if home is not None else hz.config_root_for(key)
    found = root.exists()
    if found:
        title = h.title if h is not None else key
        lines.append(f"  ✓ Harness config for {title} at {root} detected")
    else:
        live = ", ".join(hz.live_keys())
        lines.append(
            f"  ✗ No supported harness config found (looked for {key} at {root}). "
            f"Supported now: {live} — see `atv-bench harnesses`."
        )

    gh = shutil.which("gh")
    if gh:
        try:
            auth = subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=10)
            authed = auth.returncode == 0
        except Exception:
            authed = False
        lines.append(
            f"  {'✓' if authed else '·'} GitHub CLI (gh) installed"
            + ("" if authed else " but not logged in — run `gh auth login` for `submit --live`")
        )
    else:
        lines.append("  · GitHub CLI (gh) not installed — needed only for `submit --live` "
                     "(https://cli.github.com)")

    docker = shutil.which("docker")
    lines.append(
        f"  {'✓' if docker else '·'} Docker "
        + ("installed" if docker else "not installed — needed only to run matches locally")
    )

    # `run` prerequisites (DX-4): reuse the shared preflight checks so doctor and run
    # report the SAME readiness. Docker daemon + CodeClash + each harness CLI on PATH.
    from atv_bench import preflight as pf
    dchk = pf.check_docker()
    lines.append(f"  {'✓' if dchk.ok else '·'} Docker daemon (for `run`): {dchk.detail}")
    cchk = pf.check_codeclash()
    lines.append(f"  {'✓' if cchk.ok else '·'} CodeClash arena dep (for `run`): {cchk.detail}"
                 + ("" if cchk.ok else f" — {cchk.fix}"))
    for binary in ("claude", "copilot"):
        bc = pf.check_cli_on_path(binary)
        lines.append(f"  {'✓' if bc.ok else '·'} Harness CLI `{binary}` (for `run`): {bc.detail}")

    typer.echo("atv-bench doctor — environment readiness:\n")
    for ln in lines:
        typer.echo(ln)
    typer.echo("\nNext: `atv-bench fingerprint --dry-run` to preview your harness, "
               "then `atv-bench run --demo` for a real recorded match, then a live `run`.")


def _emit_run_error(err, json_out: bool) -> None:
    """Print a RunError as JSON envelope or a human problem+fix, then exit with its code."""
    from atv_bench.run_envelope import error_envelope

    if json_out:
        typer.echo(json.dumps(error_envelope(err), indent=2))
    else:
        typer.echo(f"✗ {err.message}")
        if err.fix:
            typer.echo(f"  fix: {err.fix}")
        typer.echo(f"  (exit {err.exit_code}, code={err.code})")
    raise typer.Exit(err.exit_code)


@app.command()
def run(
    game: str = typer.Option("lightcycles", "--game", help="Arena game (see --list-games)."),
    a: str = typer.Option(None, "--a", "--player-a", help="Harness A (see --list-harnesses)."),
    b: str = typer.Option(None, "--b", "--player-b", help="Harness B (see --list-harnesses)."),
    model: str = typer.Option(None, "--model", help="Model BOTH harnesses run for parity."),
    rounds: int = typer.Option(3, "--rounds", help="Number of edit+compete rounds."),
    demo: bool = typer.Option(False, "--demo", help="Replay a canned REAL match — zero Docker/auth/network."),
    json_out: bool = typer.Option(False, "--json", help="Emit the stable machine-readable envelope."),
    list_games: bool = typer.Option(False, "--list-games", help="List valid --game values and exit."),
    list_harnesses: bool = typer.Option(False, "--list-harnesses", help="List valid --a/--b harness values and exit."),
    out: Path = typer.Option(None, "--out", help="Output dir for match logs + replay."),
    a_home: Path = typer.Option(None, "--a-home", help="Config root for harness A (e.g. a cloned repo) to fingerprint."),
    b_home: Path = typer.Option(None, "--b-home", help="Config root for harness B to fingerprint."),
    persist: Path = typer.Option(None, "--persist", help="Append this match as a rating row to a JSONL lift corpus (feeds `atv-bench lift`)."),
) -> None:
    """Run a REAL harness-vs-harness match: each harness CLI builds its own bot headless,
    the two bots compete in a CodeClash arena (Docker), and a schema-v2 record is written.

    Start with `--demo` (no prerequisites) to see a real recorded match, then `atv-bench
    doctor`, then a live `run`. Phase 1 results are labeled unverified/local-debug and do
    NOT publish a ranked number (that needs the Phase 2 gateway).

    Exit codes: 0 ok · 2 usage · 3 missing-cli · 4 unauth · 5 docker · 6 policy ·
    7 timeout · 8 model-unparseable · 9 codeclash-dep.
    """
    from atv_bench.config import GAME_SPECS
    from atv_bench.runner import _HARNESS_BINARY

    if list_games:
        for g in sorted(GAME_SPECS):
            typer.echo(g)
        return
    if list_harnesses:
        for h in sorted(_HARNESS_BINARY):
            typer.echo(h)
        return

    if demo:
        _run_demo(json_out=json_out, out=out)
        return

    from atv_bench.run_envelope import RunError, ok_envelope
    from atv_bench.runner import RunConfig

    if not a or not b or not model:
        err = RunError("usage", "run needs --a <harness> --b <harness> --model <M> "
                       "(or use --demo for a no-setup real recording).",
                       fix="example: atv-bench run --game lightcycles --a copilot-cli "
                           "--b claude-code --model claude-opus-4.8")
        _emit_run_error(err, json_out)

    cfg = RunConfig(game=game, a=a, b=b, model=model, rounds=rounds)
    try:
        cfg.validate()
    except RunError as err:
        _emit_run_error(err, json_out)

    out_dir = out or Path("./_run")
    try:
        env = _run_live(cfg, out_dir, a_home, b_home, json_out, persist=persist)
    except RunError as err:
        _emit_run_error(err, json_out)
        return
    if json_out:
        typer.echo(json.dumps(env, indent=2))


def _run_demo(*, json_out: bool, out: Path | None) -> None:
    from atv_bench.demo_run import demo_envelope, demo_match_result
    from atv_bench.play import build_replay_html

    out_dir = out or Path("./_demo_replay")
    match = demo_match_result()
    try:
        replay = build_replay_html(match, out_dir, game=match.get("game"))
        replay_path = str(replay)
    except Exception:
        replay_path = ""
    env = demo_envelope(replay_path=replay_path)
    if json_out:
        typer.echo(json.dumps(env, indent=2))
        return
    d = env["data"]
    typer.echo("▶ atv-bench run --demo — a canned but REAL recorded match")
    typer.echo(f"  game    : {d['game']}")
    for p in d["players"]:
        typer.echo(f"  player  : {p['harness']} · model {p['model']} "
                   f"(source={p['model_source']}, verified={p['verified']})")
    typer.echo(f"  winner  : {d['outcome'].get('winner')}  "
               f"(turns={d['outcome'].get('turns')})")
    if replay_path:
        typer.echo(f"  replay  : {replay_path}")
    typer.echo(f"\n{d['next']}")


def _run_live(cfg, out_dir, a_home, b_home, json_out, persist=None):  # pragma: no cover - Docker + live CLIs
    from atv_bench.run_envelope import ok_envelope
    from atv_bench.runner import (
        build_match_record, fingerprint_harness_repo, preflight_or_raise, run_live_match,
    )

    preflight_or_raise(cfg)
    typer.echo(f"▶ building bots: {cfg.a} vs {cfg.b} on {cfg.model} ({cfg.rounds} rounds)…")
    homes = {cfg.a: a_home, cfg.b: b_home}
    raw = run_live_match(cfg, output_dir=Path(out_dir), homes=homes)

    # Fingerprint each harness (leak-safe) for the record identity + moat surface.
    fps: dict[str, str] = {}
    manifests: dict[str, dict] = {}
    for h, home in homes.items():
        try:
            sha, manifest = fingerprint_harness_repo(h, home)
            fps[h] = sha
            manifests[h] = manifest
        except Exception:
            fps[h] = "0" * 64

    from atv_bench.runner import summarize_budgets
    outcome, models = _summarize_tournament(raw, cfg)
    budgets = summarize_budgets(raw, cfg)
    rec = build_match_record(
        cfg, outcome=outcome, player_models=models, player_fingerprints=fps,
        player_manifests=manifests, player_budgets=budgets,
        replay_path=str(Path(out_dir)), verified=False,
    )
    if persist is not None:
        from atv_bench.run_envelope import RunError
        from atv_bench.runner import persist_rating_row_from_record
        try:
            persist_rating_row_from_record(rec, persist)
        except ValueError as exc:
            # a malformed outcome (missing/blank/foreign winner) must surface as a stable
            # RunError, not a raw ValueError leaking out of the command.
            raise RunError("policy_denied", f"cannot persist rating row: {exc}",
                           fix="the match outcome is malformed; re-run the match") from exc
        typer.echo(f"↳ appended rating row to {persist}")
    return ok_envelope(rec.to_dict())


def _summarize_tournament(raw, cfg):  # pragma: no cover - shape depends on live run
    from atv_bench.runner import summarize_tournament
    return summarize_tournament(raw, cfg)


@app.command(name="plan-schedule")
def plan_schedule(
    harness: list[str] = typer.Option(..., "--harness", help="Harness key (repeatable). Use 'bare:<inner>' for the bare control."),
    game: list[str] = typer.Option(..., "--game", help="Game key (repeatable)."),
    repeats: int = typer.Option(1, "--repeats", help="Paired repeats per (pair, game) cell."),
    seed: int = typer.Option(0, "--seed", help="Deterministic ordering seed."),
    json_out: bool = typer.Option(False, "--json", help="Emit the plan as JSON."),
) -> None:
    """Build a side-balanced round-robin match plan (G1 scheduler, wired live).

    Every unordered harness pair plays every game `--repeats` times with alternating seats.
    Deterministic under `--seed`. This is the planning half of the live pipeline — it emits
    the matches to run; execution + gating happen downstream (`run`, `rate --enforce-gates`).
    """
    import dataclasses as _dc

    from atv_bench.scheduler import build_paired_schedule

    matches = build_paired_schedule(harness, game, seed=seed, repeats=repeats)
    plan = [_dc.asdict(m) for m in matches]
    if json_out:
        typer.echo(json.dumps(plan, indent=2))
    else:
        typer.echo(f"{len(plan)} matches planned "
                   f"({len(harness)} harnesses x {len(game)} games x {repeats} repeats):")
        for m in plan:
            typer.echo(f"  {m['game']}: {m['harness_a']} vs {m['harness_b']} "
                       f"(side={m['side_index']}, repeat={m['repeat_index']})")


@app.command()
def rate(
    store: Path = typer.Option(..., "--store", help="Corpus dir containing matches.jsonl."),
    out: Path = typer.Option(None, "--out", help="Write ratings.json here (default: <store>/ratings.json)."),
    json_out: bool = typer.Option(False, "--json", help="Also print the ratings doc to stdout."),
    enforce_gates: bool = typer.Option(False, "--enforce-gates", help="Refuse to publish if the corpus fails the G5/G6 quality gates."),
) -> None:
    """Fit Tier-1 harness ratings from a match corpus and emit ratings.json.

    Loads the corpus, fits the penalized hierarchical Bradley-Terry model (base model
    factored out where the design identifies it, an honest harness+model BUNDLE where it
    is model-locked), and writes theta, theta_model_adjusted (or bundle_unit), clustered +
    FDR-corrected pairwise CIs, data_sufficiency, attributed, verified, and the unknown[]
    list of non-publishable model tags.
    """
    from atv_bench.rating import build_ratings_doc

    matches_file = store / "matches.jsonl"
    if not matches_file.exists():
        typer.echo(f"Cannot rate: no matches.jsonl in {store}")
        raise typer.Exit(2)

    rows = []
    total_records = 0
    infra_failures = 0
    for line in matches_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError as e:
            typer.echo(f"Cannot rate: malformed matches.jsonl line: {e}")
            raise typer.Exit(2)
        total_records += 1
        row = _rating_row_from_match(m)
        if row is not None:
            rows.append(row)
        elif _is_infrastructure_failure(m):
            # ONLY genuine crashes/forfeits/malformed records count toward the infra rate —
            # NOT a valid-but-not-yet-scored record, which would poison the gate on a mixed
            # corpus. A record that is neither rateable nor an explicit failure is ignored.
            infra_failures += 1

    if not rows:
        typer.echo("No rateable matches in corpus (need >=1 scored head-to-head record).")
        raise typer.Exit(2)

    if enforce_gates:
        from atv_bench.pipeline import corpus_stats, gate_corpus
        # Measure the infra-error rate over ALL records, counting only genuine failures. The
        # referee-nondeterminism rate is NOT measurable from a single corpus pass (it needs a
        # re-run-agreement probe), so it is deliberately NOT supplied here — corpus_stats
        # omits it and evaluate_quality_gates FAILS CLOSED on the missing signal. Publishing a
        # gated board therefore requires a real nondeterminism measurement, not a fabricated
        # 0.0 (a fabricated clean value would silently disable that gate).
        infra_rate = (infra_failures / total_records) if total_records else 1.0
        stats = corpus_stats(rows, infrastructure_error_rate=infra_rate)
        report = gate_corpus(stats)
        if not report.passed:
            typer.echo("Refusing to publish: corpus failed quality gates (G5/G6):")
            for f in report.failures:
                typer.echo(f"  - gate={f['gate']} observed={f.get('observed')} "
                           f"threshold={f.get('threshold')}")
            raise typer.Exit(6)

    doc = build_ratings_doc(rows, verified=True, model_overdispersion=True)
    out_path = out or (store / "ratings.json")
    out_path.write_text(json.dumps(doc, indent=2))
    typer.echo(f"Wrote {out_path} — {len(doc['harnesses'])} harnesses, "
               f"attributed={doc['attributed']}, model_locked={doc.get('model_locked')}, "
               f"unknown={doc['unknown']}")
    if json_out:
        typer.echo(json.dumps(doc, indent=2))


def _cluster_key_from_match(m: dict) -> str:
    """Cluster key = game x (unordered build-artifact pair).

    Nested games sharing a harness build-artifact are intra-cluster correlated, so the lift
    bootstrap must resample the CLUSTER, not the row (gap G2). We key on the two players'
    fingerprint_sha256 (the artifact identity) when present, falling back to harness names,
    scoped by game so cross-game rows never share a cluster.
    """
    game = m.get("game") or m.get("game_version") or "game"
    players = m.get("players") or []
    ids = []
    for p in players[:2]:
        ids.append(str(p.get("fingerprint_sha256") or p.get("harness") or "?"))
    if len(ids) < 2:
        ids = [str(m.get("harness_a", "?")), str(m.get("harness_b", "?"))]
    return f"{game}::" + "~".join(sorted(ids))


def _load_rating_matches(store: Path, *, with_clusters: bool = False):
    """Load matches.jsonl from a corpus dir into RatingMatch rows (shared by rate/lift).

    When ``with_clusters`` is True, returns ``(matches, cluster_ids)`` with ``cluster_ids``
    aligned to ``matches`` (one key per row). Otherwise returns just ``matches``.
    """
    from atv_bench.rating import RatingMatch

    matches_file = store / "matches.jsonl"
    if not matches_file.exists():
        typer.echo(f"Cannot rate: no matches.jsonl in {store}")
        raise typer.Exit(2)
    out = []
    clusters: list[str] = []
    for line in matches_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError as e:
            typer.echo(f"Cannot rate: malformed matches.jsonl line: {e}")
            raise typer.Exit(2)
        row = _rating_row_from_match(m)
        if row is not None:
            out.append(RatingMatch(
                harness_a=row["harness_a"], harness_b=row["harness_b"],
                model_a=row["model_a"], model_b=row["model_b"],
                score_a=float(row["score_a"]),
            ))
            if with_clusters:
                clusters.append(_cluster_key_from_match(m))
    if with_clusters:
        return out, clusters
    return out


@app.command(name="lift")
def lift(
    store: Path = typer.Option(..., "--store", help="Corpus dir containing matches.jsonl."),
    baseline: list[str] = typer.Option(
        ..., "--baseline",
        help="Bare baseline mapping HARNESS=BARE_HARNESS (repeatable). The bare control "
             "must have matches on the same base model as HARNESS."),
    out: Path = typer.Option(None, "--out", help="Write lift.json here (default: <store>/lift.json)."),
    seed: int = typer.Option(0, "--seed", help="Bootstrap seed."),
    n_boot: int = typer.Option(1000, "--n-boot", help="Bootstrap replicates for the CI."),
    json_out: bool = typer.Option(False, "--json", help="Also print the lift doc to stdout."),
    cluster: bool = typer.Option(
        True, "--cluster/--no-cluster",
        help="Resample whole build-artifact clusters (game x artifact pair) instead of "
             "individual matches, so nested games do not inflate CI precision (gap G2)."),
) -> None:
    """Emit harness LIFT over the bare model — the headline product metric (Section 5.5).

    For each --baseline HARNESS=BARE mapping, computes lift = theta(M+HARNESS) - theta(M BARE)
    on the shared base model M, with a percentile-bootstrap CI. Lifts are comparable across
    harnesses on DIFFERENT base models because each subtracts its own bare baseline. Refuses
    (exit 2) when a declared bare baseline was never run on the harness's base model.
    """
    from atv_bench.lift import LiftError, compute_lift

    if cluster:
        matches, cluster_ids = _load_rating_matches(store, with_clusters=True)
    else:
        matches, cluster_ids = _load_rating_matches(store), None
    if not matches:
        typer.echo("No rateable matches in corpus (need >=1 scored head-to-head record).")
        raise typer.Exit(2)

    baselines: dict[str, str] = {}
    for spec in baseline:
        if "=" not in spec:
            typer.echo(f"Bad --baseline {spec!r}: expected HARNESS=BARE_HARNESS")
            raise typer.Exit(2)
        h, bare = spec.split("=", 1)
        baselines[h.strip()] = bare.strip()

    try:
        results = compute_lift(matches, baselines, seed=seed, n_boot=n_boot,
                               cluster_ids=cluster_ids)
    except LiftError as e:
        typer.echo(f"Cannot compute lift: {e}")
        raise typer.Exit(2)

    doc = {
        "seed": seed,
        "n_boot": n_boot,
        "resampling_unit": "cluster" if cluster else "match",
        "lifts": [
            {
                "harness": r.harness,
                "bare_harness": r.bare_harness,
                "base_model": r.base_model,
                "lift": r.lift,
                "ci": {"lo": r.lo, "hi": r.hi},
            }
            for r in results.values()
        ],
    }
    out_path = out or (store / "lift.json")
    out_path.write_text(json.dumps(doc, indent=2))
    # Section 6: emit lift through the single typed renderer so LIFT is the headline
    # metric and no rank leaks. Lift is inherently verified (each harness is its own control).
    from atv_bench.render import render_ranking
    ratings_stub = {
        "verified": True,
        "harnesses": [{"harness": r.harness, "theta": None, "bundle_unit": True}
                      for r in results.values()],
        "unknown": [],
    }
    typer.echo(str(render_ranking({"ratings": ratings_stub, "lifts": doc}, verified=True)))
    typer.echo(f"Wrote {out_path} — {len(doc['lifts'])} lift(s)")
    if json_out:
        typer.echo(json.dumps(doc, indent=2))


def _is_infrastructure_failure(m: dict) -> bool:
    """True if a match record is a genuine infrastructure failure (crash/forfeit/malformed).

    Used by `rate --enforce-gates` to measure the infra-error rate HONESTLY: a record that is
    simply not-yet-scored is NOT an infra failure (counting it would poison the gate on a
    mixed corpus). We recognize the explicit failure shapes the pipeline emits: a CRASH
    record (`loser`/`opponent` keys, no players), or a schema-v2 outcome whose winner is a
    crash/forfeit token.
    """
    if m.get("crashed") or m.get("infrastructure_error"):
        return True
    # CRASH-artifact shape (publish.py): {loser, opponent, match_id}, no scored head-to-head.
    if "loser" in m and "opponent" in m and "players" not in m:
        return True
    outcome = m.get("outcome")
    if isinstance(outcome, dict):
        winner = str(outcome.get("winner", "")).lower()
        if any(tok in winner for tok in ("crash", "forfeit", "error")):
            return True
    return False


def _rating_row_from_match(m: dict) -> dict | None:
    """Extract (harness_a, harness_b, model_a, model_b, score_a) from a match record.

    Returns None when the record cannot supply a two-player scored head-to-head. Supports
    both the flat rating-row shape and the schema-v2 match record (players[] + outcome).

    Fails closed like runner.match_record_to_rating_row: a MISSING or None winner is NOT a
    silent draw (returns None — unrateable), and two players sharing a harness key are
    rejected (ambiguous attribution). Only an explicit ``draw``/``tie`` scores 0.5.
    """
    if all(k in m for k in ("harness_a", "harness_b", "model_a", "model_b", "score_a")):
        # flat rating row: reject a missing/blank harness id and identical-harness self-play
        # (both make attribution meaningless), consistent with the schema-v2 path below.
        ha, hb = str(m.get("harness_a") or "").strip(), str(m.get("harness_b") or "").strip()
        if not ha or not hb or ha == hb:
            return None
        return m
    players = m.get("players")
    if not players or len(players) < 2:
        return None
    pa, pb = players[0], players[1]
    ha, hb = str(pa.get("harness") or "").strip(), str(pb.get("harness") or "").strip()
    if not ha or not hb or ha == hb:
        return None  # missing/blank harness id or identical-harness self-play: unrateable
    outcome = m.get("outcome") or {}
    if "winner" not in outcome or outcome.get("winner") is None:
        return None  # unscored/malformed — never a fabricated draw
    winner = outcome.get("winner")
    if winner in ("a", "A", 0, pa.get("harness")):
        score_a = 1.0
    elif winner in ("b", "B", 1, pb.get("harness")):
        score_a = 0.0
    elif winner in ("draw", "tie"):
        score_a = 0.5
    else:
        return None
    return {
        "harness_a": pa.get("harness"), "harness_b": pb.get("harness"),
        "model_a": pa.get("model", "unknown"), "model_b": pb.get("model", "unknown"),
        "score_a": score_a,
    }


if __name__ == "__main__":
    app()
