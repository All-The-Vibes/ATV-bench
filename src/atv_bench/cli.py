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


@app.command()
def fingerprint(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the consent view (default human)."),
    json_out: bool = typer.Option(False, "--json", help="Emit the raw manifest as JSON."),
    harness: str = typer.Option(None, "--harness", help="Harness to probe (default: auto-detect; see `atv-bench harnesses`)."),
    home: Path = typer.Option(None, "--home", help="Harness config root (default: harness's standard dir under $HOME)."),
) -> None:
    """Probe your coding-agent harness and show what a submission would publish."""
    result = _probe_or_exit(home, harness)
    if json_out:
        typer.echo(json.dumps(result.manifest, indent=2))
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


def _serve_and_open(site: Path) -> None:
    """Serve `site` on a local port and open a browser at it (fetch needs http, not file://)."""
    import functools
    import http.server
    import threading
    import webbrowser

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/index.html"
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


def _record_demo_match(store_dir: str, result: dict, a_name: str, b_name: str) -> None:
    """Seed the two demo players + the match they just played into the demo store.

    The demo's whole point is "play a match, then see IT on the board". Without this,
    Act 3 shows only the canned build_demo_store roster and the two harnesses the user
    just watched (a_name vs b_name) never appear. We add a submission for each (so they
    get a leaderboard row with a fingerprint chip) and replay the just-played, fully
    deterministic outcome across enough seeded match_ids to clear the rated gate — the
    result is honest (same adjudicated outcome every time), just repeated so the row is
    rated rather than "waiting for opponent".
    """
    from atv_bench.store import LeagueStore
    from atv_bench.elo import MIN_RATED_MATCHES

    store = LeagueStore(store_dir)

    def _fingerprint(harness: str, gstack: bool, skills: list[str]) -> dict:
        return {
            "harness": harness, "model": "demo", "gstack": gstack,
            "skills": skills, "mcps": [], "plugins": [], "custom_agents_count": 0,
            "probe_version": "1.0.0", "unknown": [],
        }

    entrants = (
        (a_name, "claude-code", True, ["gstack"]),
        (b_name, "copilot-cli", False, []),
    )
    for identity, harness, gstack, skills in entrants:
        store.add_submission({
            "identity": identity,
            "game": "lightcycles",
            "bot_sha256": (identity.encode().hex() * 8)[:64].ljust(64, "0"),
            "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
            "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
            "fingerprint": _fingerprint(harness, gstack, skills),
        })

    outcome = result.get("outcome", "draw")
    # Replay the identical adjudicated outcome across distinct match_ids so the pairing
    # clears MIN_RATED_MATCHES and shows as a real rated row, not "waiting for opponent".
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
    live: bool = typer.Option(True, "--live/--no-live",
                              help="Animate the feed with a per-turn delay (--no-live for CI/scripts)."),
    board: bool = typer.Option(True, "--board/--no-board",
                               help="After the match, build the leaderboard + insights."),
    seed: int = typer.Option(0, "--seed", help="Trusted engine seed (reproducible match)."),
) -> None:
    """Play two harness bots head-to-head in Tron with a live feed, then show the board.

    The demo in three acts: (1) two named harnesses enter, (2) a live turn-by-turn Tron
    feed renders each frame, (3) the leaderboard + gstack insights are shown. With no bot
    paths it uses two bundled greedy-survivor sample bots so the demo runs with zero setup.
    """
    import time

    from atv_bench.arena.engine import Direction, TronEngine
    from atv_bench.arena.referee import SubprocessMoveSource, run_match
    from atv_bench.arena.render import render_frame
    from atv_bench.arena import sample_bots

    sample = Path(sample_bots.__file__).parent / "greedy_survivor.py"
    a_path = str(a_bot) if a_bot is not None else str(sample)
    b_path = str(b_bot) if b_bot is not None else str(sample)

    for label, p in ((a_name, a_path), (b_name, b_path)):
        if not Path(p).is_file():
            typer.echo(f"Bot for {label} not found: {p}")
            raise typer.Exit(2)

    board_w = board_h = 25
    engine = TronEngine(
        width=board_w, height=board_h,
        start_a=(1, 1), start_b=(board_w - 2, board_h - 2),
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
        _record_demo_match(str(tmp_store), result, a_name, b_name)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        site = build_site(str(out_dir), store_dir=str(tmp_store), updated_at=now)
        doc = json.loads((site / "leaderboard.json").read_text())
        rows = doc.get("rows", [])

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

    typer.echo("atv-bench doctor — environment readiness:\n")
    for ln in lines:
        typer.echo(ln)
    typer.echo("\nNext: `atv-bench fingerprint --dry-run` to preview your harness, "
               "then `atv-bench games` to pick an arena.")


if __name__ == "__main__":
    app()
