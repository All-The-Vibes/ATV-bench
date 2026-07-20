#!/usr/bin/env python3
"""Run a reproducible, non-rankable Phoenix-versus-hve-core local case study.

This script is intentionally outside the official benchmark and leaderboard paths.
It performs one fresh paired harness trial, preserves exact process-stream bytes and
checksums, and treats its side-swapped games as nested descriptive measurements.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atv_bench.adapters.contract import build_child_environment, capture_repo_diff, git_base
from atv_bench.comparison import (
    attest_copilot_model_receipt,
    git_commit,
    git_tree,
    materialize_pointer_tree,
    parse_copilot_jsonl,
    play_series,
    scan_harness_assets,
    sha256_bytes,
    sha256_file,
    summarize_games,
    tracked_tree_listing_sha256,
    write_checksums,
    write_exact_bytes,
    write_exact_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTER_BOT = (
    REPO_ROOT
    / "src"
    / "atv_bench"
    / "arena"
    / "sample_bots"
    / "greedy_survivor.py"
)

GOAL = """Build the strongest correct ATV-bench Lightcycles bot you can.

The scored artifact is main.py. It is a long-lived Python process: for every JSON
observation line on stdin, print exactly one move (up, down, left, or right) on stdout
and flush. Preserve that protocol. You may edit main.py only; do not edit baseline.py,
smoke_test.py, benchmark.py, or README.md.

Use your harness's normal workflow. Run `python smoke_test.py` and
`python benchmark.py`. Do not use network access. Finish only after smoke_test.py passes.
The final comparison uses held-out seeds that benchmark.py does not reveal.
"""


@dataclass(frozen=True)
class HarnessExecution:
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout: bytes
    stderr: bytes
    diff: str


def _run_text(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    process = subprocess.run(
        list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return process.stdout.strip()


def _agent_tool_compatibility_shim(path: Path) -> dict[str, Any]:
    """Apply the same narrow current-Copilot tool shim to either selected agent."""
    before = path.read_bytes()
    lines = before.decode("utf-8").splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise RuntimeError(f"agent has no YAML frontmatter: {path}")
    try:
        end = next(
            index for index in range(1, len(lines)) if lines[index].strip() == "---"
        )
    except StopIteration as exc:
        raise RuntimeError(f"agent frontmatter is unterminated: {path}") from exc
    replacement = "tools: ['*']"
    tool_rows = [
        index for index in range(1, end) if lines[index].startswith("tools:")
    ]
    if tool_rows:
        lines[tool_rows[0]] = replacement
        for index in reversed(tool_rows[1:]):
            del lines[index]
    else:
        lines.insert(end, replacement)
    after = ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(after)
    return {
        "path": path.name,
        "before_sha256": sha256_bytes(before),
        "after_sha256": sha256_bytes(after),
        "change": "frontmatter tools allowlist only",
    }


def _copilot_argv() -> tuple[str, str]:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is required for GitHub Copilot CLI")
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is required to locate the Copilot npm loader")
    loader = (
        Path(appdata)
        / "npm"
        / "node_modules"
        / "@github"
        / "copilot"
        / "npm-loader.js"
    )
    if not loader.is_file():
        raise RuntimeError(f"Copilot npm loader not found: {loader}")
    return node, str(loader)


def _github_token() -> str:
    token = _run_text("gh", "auth", "token")
    if not token:
        raise RuntimeError("gh auth token returned no token")
    return token


def _ambient_skill_names() -> list[str]:
    names: set[str] = set()
    home = Path.home()
    for root in (
        home / ".agents" / "skills",
        home / ".claude" / "skills",
        home / ".codex" / "skills",
    ):
        if root.is_dir():
            names.update(path.parent.name for path in root.rglob("SKILL.md"))
    return sorted(names)


def _write_isolated_config(copilot_home: Path, disabled_skills: list[str]) -> None:
    copilot_home.mkdir(parents=True, exist_ok=True)
    write_exact_text(
        copilot_home / "config.json",
        json.dumps({"disabled_skills": disabled_skills}, indent=2) + "\n",
    )


def _prepare_phoenix(
    phoenix_repo: Path,
    root: Path,
    disabled: list[str],
    *,
    tool_compat_shim: bool,
) -> dict[str, Any]:
    binary = phoenix_repo / "target" / "release" / (
        "phoenix-mcp.exe" if os.name == "nt" else "phoenix-mcp"
    )
    if not binary.is_file():
        subprocess.run(
            ["cargo", "build", "--release", "--bin", "phoenix-mcp"],
            cwd=phoenix_repo,
            check=True,
        )
    copilot_home = root / "copilot-home"
    user_home = root / "user-home"
    (copilot_home / "agents").mkdir(parents=True, exist_ok=True)
    (copilot_home / "skills").mkdir(parents=True, exist_ok=True)
    user_home.mkdir(parents=True, exist_ok=True)
    phoenix_skill_names = {
        path.parent.name for path in (phoenix_repo / "skills").rglob("SKILL.md")
    }
    _write_isolated_config(
        copilot_home, [name for name in disabled if name not in phoenix_skill_names]
    )

    agent = (phoenix_repo / "dist" / "phoenix.agent.md").read_text(encoding="utf-8")
    agent = agent.replace("__PHOENIX_BIN__", binary.as_posix())
    agent_path = copilot_home / "agents" / "phoenix.agent.md"
    write_exact_text(agent_path, agent)
    shim = _agent_tool_compatibility_shim(agent_path) if tool_compat_shim else None
    for skill in sorted((phoenix_repo / "skills").iterdir()):
        if skill.is_dir() and (skill / "SKILL.md").is_file():
            shutil.copytree(skill, copilot_home / "skills" / skill.name)
    write_exact_text(
        copilot_home / "mcp-config.json",
        json.dumps(
            {"mcpServers": {"phoenix": {"type": "stdio", "command": binary.as_posix()}}},
            indent=2,
        )
        + "\n",
    )
    return {
        "copilot_home": copilot_home,
        "user_home": user_home,
        "binary": binary,
        "tool_compatibility_shim": shim,
    }


def _prepare_hve(
    hve_repo: Path,
    root: Path,
    disabled: list[str],
    *,
    tool_compat_shim: bool,
) -> dict[str, Any]:
    plugin = root / "plugin"
    resolved = materialize_pointer_tree(
        hve_repo / "plugins" / "hve-core",
        plugin,
        source_root=hve_repo,
    )
    if resolved == 0 and os.name == "nt":
        raise RuntimeError("hve-core plugin pointer files were not materialized")
    copilot_home = root / "copilot-home"
    user_home = root / "user-home"
    user_home.mkdir(parents=True, exist_ok=True)
    hve_skill_names = {path.parent.name for path in plugin.rglob("SKILL.md")}
    _write_isolated_config(
        copilot_home, [name for name in disabled if name not in hve_skill_names]
    )

    shim = None
    if tool_compat_shim:
        candidates: list[Path] = []
        for path in plugin.rglob("*.md"):
            try:
                header = path.read_text(encoding="utf-8").splitlines()[:20]
            except (OSError, UnicodeDecodeError):
                continue
            if any(line.strip().casefold() == "name: rpi agent" for line in header):
                candidates.append(path)
        if len(candidates) != 1:
            raise RuntimeError(
                f"expected one materialized hve-core RPI agent, found {len(candidates)}"
            )
        shim = _agent_tool_compatibility_shim(candidates[0])
    return {
        "copilot_home": copilot_home,
        "user_home": user_home,
        "plugin": plugin,
        "resolved_pointers": resolved,
        "tool_compatibility_shim": shim,
    }


def _write_seed_repo(path: Path) -> None:
    path.mkdir(parents=True)
    shutil.copy2(STARTER_BOT, path / "main.py")
    shutil.copy2(STARTER_BOT, path / "baseline.py")
    write_exact_text(
        path / "README.md",
        "# ATV-bench Lightcycles seed\n\n"
        "Edit only `main.py`. One JSON observation line in; one move word out.\n",
    )
    write_exact_text(
        path / "smoke_test.py",
        """import sys
from atv_bench.arena.referee import SubprocessMoveSource

obs = {
    "width": 11, "height": 9, "turn": 0,
    "you": {"pos": [1, 1], "dir": "right", "trail": [[1, 1]]},
    "opponent": {"pos": [9, 7], "dir": "left", "trail": [[9, 7]]},
}
source = SubprocessMoveSource([sys.executable, "main.py"], per_turn_timeout=3.0)
try:
    move = source.next_move(obs)
finally:
    source.close()
if move is None:
    raise SystemExit("main.py did not return a valid move")
print(move.value)
""",
    )
    write_exact_text(
        path / "benchmark.py",
        """import json
from atv_bench.comparison import play_series

result = play_series("main.py", "baseline.py", seeds=range(10, 12))
print(json.dumps(result["summary"], indent=2))
""",
    )
    write_exact_text(path / ".gitignore", "__pycache__/\n*.pyc\n")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=atv@bench.local",
            "-c",
            "user.name=ATV Bench",
            "commit",
            "-qm",
            "seed",
        ],
        cwd=path,
        check=True,
    )


def _clone_seed(seed: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(seed), str(destination)],
        check=True,
    )


def _common_env(
    *,
    copilot_home: Path,
    user_home: Path,
    token: str,
) -> dict[str, str]:
    pythonpath = str(REPO_ROOT / "src")
    existing = os.environ.get("PYTHONPATH")
    if existing:
        pythonpath += os.pathsep + existing
    env = build_child_environment()
    env.update(
        {
            "COPILOT_HOME": str(copilot_home),
            "COPILOT_GITHUB_TOKEN": token,
            "COPILOT_AUTO_UPDATE": "false",
            "HOME": str(user_home),
            "USERPROFILE": str(user_home),
            "PYTHONPATH": pythonpath,
            "NO_COLOR": "1",
        }
    )
    return env


def _command(
    *,
    node: str,
    loader: str,
    model: str,
    credits: int,
    agent: str,
    plugin: Path | None = None,
) -> list[str]:
    argv = [node, loader]
    if plugin is not None:
        argv += ["--plugin-dir", str(plugin)]
    argv += [
        "-C",
        "{repo}",
        "-p",
        GOAL,
        "--agent",
        agent,
        "--allow-all-tools",
        "--no-ask-user",
        "--output-format",
        "json",
        "--stream",
        "off",
        "--model",
        model,
        "--max-ai-credits",
        str(credits),
        "--disable-builtin-mcps",
        "--no-remote",
        "--no-remote-export",
        "--no-auto-update",
        "--no-color",
        "--plain-diff",
        "--log-level",
        "error",
        "--secret-env-vars=COPILOT_GITHUB_TOKEN",
    ]
    return argv


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_harness(
    command: list[str],
    *,
    workspace: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> HarnessExecution:
    """Capture stdout/stderr as exact bytes while preserving repository changes."""
    base = git_base(str(workspace))
    if base is None:
        raise RuntimeError(f"workspace has no readable HEAD: {workspace}")
    expanded = [part.replace("{repo}", str(workspace)) for part in command]
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    started = time.monotonic()
    process = subprocess.Popen(
        expanded,
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
    duration = time.monotonic() - started
    diff = capture_repo_diff(str(workspace), base)
    if timed_out:
        status = "timeout"
    elif process.returncode != 0:
        status = "error"
    elif diff.strip():
        status = "ok"
    else:
        status = "no_edit"
    return HarnessExecution(
        status=status,
        exit_code=process.returncode,
        duration_seconds=duration,
        stdout=stdout,
        stderr=stderr,
        diff=diff,
    )


def _run_validation(
    workspace: Path,
    env: dict[str, str],
    out: Path,
    name: str,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    commands = {
        "compile": [sys.executable, "-m", "py_compile", "main.py"],
        "smoke": [sys.executable, "smoke_test.py"],
    }
    for label, command in commands.items():
        try:
            process = subprocess.run(
                command,
                cwd=workspace,
                env=env,
                capture_output=True,
                text=False,
                timeout=30,
                check=False,
            )
            stdout = process.stdout
            stderr = process.stderr
            exit_code: int | None = process.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            exit_code = None
            timed_out = True
        stdout_path = out / "validation" / name / f"{label}.stdout.bin"
        stderr_path = out / "validation" / name / f"{label}.stderr.bin"
        results[label] = {
            "ok": exit_code == 0 and not timed_out,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout_sha256": write_exact_bytes(stdout_path, stdout),
            "stderr_sha256": write_exact_bytes(stderr_path, stderr),
        }
    return {
        "compile_ok": results["compile"]["ok"],
        "smoke_ok": results["smoke"]["ok"],
        "commands": results,
    }


def _empty_series(reason: str = "not_run_invalid_or_noncomparable_trial") -> dict[str, Any]:
    return {
        "status": reason,
        "summary": summarize_games([]),
        "games": [],
    }


def _render_readme(document: dict[str, Any]) -> str:
    summary = document["series"]["phoenix_vs_hve"]["summary"]
    classification = document["trial_outcome"]["classification"]
    requested_model = document["methodology"]["requested_model"]
    phoenix_attestation = document["builds"]["phoenix"]["model_attestation"]
    hve_attestation = document["builds"]["hve"]["model_attestation"]
    if document["trial_outcome"]["comparable"]:
        model_statement = (
            f"Both Copilot JSONL receipts consistently reported `{requested_model}` "
            "and contained one successful terminal result. This remains CLI-self-"
            "reported rather than provider-signed evidence."
        )
    else:
        model_statement = (
            f"Requested model: `{requested_model}`. Phoenix observed "
            f"`{phoenix_attestation['observed_models']}` with attestation "
            f"`{phoenix_attestation['status']}`; hve-core observed "
            f"`{hve_attestation['observed_models']}` with attestation "
            f"`{hve_attestation['status']}`. This trial is noncomparable."
        )
    return f"""# NON-RANKABLE local case study: ATV-Phoenix vs hve-core

Run: `{document["run_id"]}`

**This is not an official ATV-Bench result, is never leaderboard-rankable, and does
not establish a global harness winner.** It is one local, self-attested, fresh paired
harness trial on a synthetic Lightcycles task.

{model_statement}

Both harnesses were assigned the same Copilot CLI, goal, seed repository, budget, and
compatibility-shim policy. Every held-out seed was played twice with sides swapped,
but those {summary["games"]} games are nested descriptive measurements—not
{summary["games"]} independent harness trials.

Artifact classification: **{classification}**.

| Nested result | Count |
|---|---:|
| ATV-Phoenix wins | {summary["harness_a_wins"]} |
| hve-core wins | {summary["harness_b_wins"]} |
| Draws | {summary["draws"]} |
| Total nested games | {summary["games"]} |

Exact stdout/stderr bytes, candidate artifacts (including invalid ones), diffs, source
Git tree identities, validation streams, raw games, and checksums are preserved. The
aggregate tool requires at least five comparable both-valid fresh trials before it can
make even a task-contract-specific decision.
"""


def _candidate_is_valid(
    *,
    bot_path: Path,
    validation: dict[str, Any],
) -> bool:
    """Validate the candidate artifact independently from execution provenance."""
    return bool(
        bot_path.is_file()
        and validation["compile_ok"]
        and validation["smoke_ok"]
    )


def _model_identifier(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or any(character.isspace() for character in normalized)
        or normalized.casefold() in {"unknown", "default", "auto"}
    ):
        raise argparse.ArgumentTypeError(
            "model must be an explicit nonblank identifier; auto/default/unknown "
            "and whitespace are not allowed"
        )
    return normalized


def _model_attestation(
    runtime: dict[str, Any],
    *,
    requested_model: str,
) -> dict[str, Any]:
    receipt = attest_copilot_model_receipt(
        runtime,
        requested_model=requested_model,
    )
    receipt["selection_source"] = "explicit_cli"
    return receipt


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phoenix-repo", required=True)
    parser.add_argument("--hve-repo", required=True)
    parser.add_argument("--out")
    parser.add_argument(
        "--model",
        required=True,
        type=_model_identifier,
        help=(
            "Exact Copilot CLI model identifier for this preregistered case-study "
            "cell. No implicit model default is allowed."
        ),
    )
    parser.add_argument("--held-out-seeds", type=int, default=4)
    parser.add_argument("--seed-start", type=int, default=100)
    parser.add_argument(
        "--held-out-seed",
        action="append",
        type=int,
        default=[],
        help=(
            "Explicit held-out seed; repeat for a preregistered balanced seed set. "
            "When supplied, --held-out-seeds and --seed-start are ignored."
        ),
    )
    parser.add_argument(
        "--per-turn-timeout",
        type=float,
        default=3.0,
        help="Held-out Lightcycles move deadline in seconds for both candidates.",
    )
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-ai-credits", type=int, default=30)
    parser.add_argument(
        "--tool-compat-shim",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Apply the identical tools:['*'] frontmatter-only compatibility policy "
            "to both selected agents and record before/after hashes."
        ),
    )
    return parser


def main() -> None:
    args = _argument_parser().parse_args()
    if args.held_out_seeds <= 0:
        raise SystemExit("--held-out-seeds must be positive")
    if args.held_out_seed and (
        any(seed < 0 for seed in args.held_out_seed)
        or len(set(args.held_out_seed)) != len(args.held_out_seed)
    ):
        raise SystemExit("--held-out-seed values must be unique non-negative integers")
    if args.per_turn_timeout <= 0:
        raise SystemExit("--per-turn-timeout must be positive")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")

    phoenix_repo = Path(args.phoenix_repo).resolve()
    hve_repo = Path(args.hve_repo).resolve()
    if not (phoenix_repo / "Cargo.toml").is_file():
        raise SystemExit(f"Not an ATV-Phoenix checkout: {phoenix_repo}")
    if not (hve_repo / "plugins" / "hve-core").is_dir():
        raise SystemExit(f"Not an hve-core checkout: {hve_repo}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = (
        Path(args.out).resolve()
        if args.out
        else REPO_ROOT / "reports" / "comparison" / "phoenix-hve" / timestamp
    )
    out.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="atv-phoenix-hve-"))

    try:
        disabled = _ambient_skill_names()
        phoenix = _prepare_phoenix(
            phoenix_repo,
            work / "phoenix-runtime",
            disabled,
            tool_compat_shim=args.tool_compat_shim,
        )
        hve = _prepare_hve(
            hve_repo,
            work / "hve-runtime",
            disabled,
            tool_compat_shim=args.tool_compat_shim,
        )
        shim_applied = {
            "phoenix": phoenix["tool_compatibility_shim"] is not None,
            "hve": hve["tool_compatibility_shim"] is not None,
        }
        if len(set(shim_applied.values())) != 1:
            raise RuntimeError("tool compatibility shim was not applied equally")
        if args.tool_compat_shim and not all(shim_applied.values()):
            raise RuntimeError("requested tool compatibility shim was not applied to both")

        seed = work / "seed"
        _write_seed_repo(seed)
        phoenix_workspace = work / "phoenix-workspace"
        hve_workspace = work / "hve-workspace"
        _clone_seed(seed, phoenix_workspace)
        _clone_seed(seed, hve_workspace)

        node, loader = _copilot_argv()
        token = _github_token()
        environments = {
            "phoenix": _common_env(
                copilot_home=phoenix["copilot_home"],
                user_home=phoenix["user_home"],
                token=token,
            ),
            "hve": _common_env(
                copilot_home=hve["copilot_home"],
                user_home=hve["user_home"],
                token=token,
            ),
        }
        commands = {
            "phoenix": _command(
                node=node,
                loader=loader,
                model=args.model,
                credits=args.max_ai_credits,
                agent="phoenix",
            ),
            "hve": _command(
                node=node,
                loader=loader,
                model=args.model,
                credits=args.max_ai_credits,
                agent="hve-core:rpi-agent",
                plugin=hve["plugin"],
            ),
        }
        workspaces = {"phoenix": phoenix_workspace, "hve": hve_workspace}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                name: pool.submit(
                    _run_harness,
                    commands[name],
                    workspace=workspaces[name],
                    env=environments[name],
                    timeout_seconds=args.timeout,
                )
                for name in ("phoenix", "hve")
            }
            executions = {name: future.result() for name, future in futures.items()}

        write_exact_text(out / "prompt.txt", GOAL)
        builds: dict[str, Any] = {}
        for name, execution in executions.items():
            runtime = parse_copilot_jsonl(execution.stdout)
            model_attestation = _model_attestation(
                runtime,
                requested_model=args.model,
            )
            runtime["model_attestation"] = model_attestation
            reported_model = runtime["model"]
            raw_stdout_sha256 = write_exact_bytes(
                out / "raw" / f"{name}.stdout.bin", execution.stdout
            )
            raw_stderr_sha256 = write_exact_bytes(
                out / "raw" / f"{name}.stderr.bin", execution.stderr
            )
            diff_sha256 = write_exact_text(
                out / "diffs" / f"{name}.patch", execution.diff
            )
            runtime_sha256 = write_exact_text(
                out / "runtime" / f"{name}.json",
                json.dumps(runtime, indent=2, sort_keys=True) + "\n",
            )

            bot_path = workspaces[name] / "main.py"
            candidate_path = out / "artifacts" / name / "main.py"
            bot_sha256 = None
            if bot_path.is_file():
                bot_sha256 = write_exact_bytes(candidate_path, bot_path.read_bytes())
            validation = _run_validation(
                workspaces[name], environments[name], out, name
            )
            valid_artifact = _candidate_is_valid(
                bot_path=bot_path,
                validation=validation,
            )
            execution_valid = bool(
                execution.status == "ok"
                and execution.exit_code == 0
                and runtime["terminal_success"]
            )
            builds[name] = {
                "status": execution.status,
                "exit_code": execution.exit_code,
                "duration_seconds": round(execution.duration_seconds, 6),
                "requested_model": args.model,
                "reported_model": reported_model,
                "model_matches_request": model_attestation["status"] == "pass",
                "model_attestation": model_attestation,
                "reported_usage": runtime["result"].get("usage", {}),
                "enabled_skill_sources": runtime["enabled_skill_sources"],
                "mcp_servers": runtime["mcp_servers"],
                "final_message": runtime["final_message"],
                "bot_present": bot_path.is_file(),
                "bot_sha256": bot_sha256,
                "candidate_persisted_even_if_invalid": bot_path.is_file(),
                "valid_artifact": valid_artifact,
                "execution_valid": execution_valid,
                "validation": validation,
                "diff_sha256": diff_sha256,
                "raw_stdout_sha256": raw_stdout_sha256,
                "raw_stderr_sha256": raw_stderr_sha256,
                "runtime_summary_sha256": runtime_sha256,
            }

        held_out = (
            tuple(args.held_out_seed)
            if args.held_out_seed
            else tuple(range(args.seed_start, args.seed_start + args.held_out_seeds))
        )
        phoenix_bot = phoenix_workspace / "main.py"
        hve_bot = hve_workspace / "main.py"
        baseline = seed / "baseline.py"
        build_comparability = {
            name: bool(
                builds[name]["valid_artifact"]
                and builds[name]["execution_valid"]
                and builds[name]["model_attestation"]["status"] == "pass"
            )
            for name in ("phoenix", "hve")
        }
        trial_comparable = all(build_comparability.values())
        series = {
            "phoenix_vs_hve": (
                play_series(
                    phoenix_bot,
                    hve_bot,
                    seeds=held_out,
                    per_turn_timeout=args.per_turn_timeout,
                )
                if trial_comparable
                else _empty_series("not_run_noncomparable_trial")
            ),
            "phoenix_vs_baseline": (
                play_series(
                    phoenix_bot,
                    baseline,
                    seeds=held_out,
                    per_turn_timeout=args.per_turn_timeout,
                )
                if build_comparability["phoenix"]
                else _empty_series("not_run_noncomparable_phoenix_build")
            ),
            "hve_vs_baseline": (
                play_series(
                    hve_bot,
                    baseline,
                    seeds=held_out,
                    per_turn_timeout=args.per_turn_timeout,
                )
                if build_comparability["hve"]
                else _empty_series("not_run_noncomparable_hve_build")
            ),
            "baseline_control": play_series(
                baseline,
                baseline,
                seeds=held_out,
                per_turn_timeout=args.per_turn_timeout,
            ),
        }

        artifact_validity = {
            "phoenix": bool(builds["phoenix"]["valid_artifact"]),
            "hve": bool(builds["hve"]["valid_artifact"]),
        }
        execution_validity = {
            "phoenix": bool(builds["phoenix"]["execution_valid"]),
            "hve": bool(builds["hve"]["execution_valid"]),
        }
        attestation_validity = {
            "phoenix": builds["phoenix"]["model_attestation"]["status"] == "pass",
            "hve": builds["hve"]["model_attestation"]["status"] == "pass",
        }
        if not all(attestation_validity.values()):
            classification = "model-attestation-failed"
        elif not all(execution_validity.values()):
            classification = "harness-execution-invalid"
        elif all(artifact_validity.values()):
            classification = "comparable-both-valid"
        elif artifact_validity["phoenix"]:
            classification = "phoenix-valid-hve-invalid"
        elif artifact_validity["hve"]:
            classification = "phoenix-invalid-hve-valid"
        else:
            classification = "both-invalid-artifacts"

        document = {
            "schema_version": 2,
            "schema": "atv.phoenix-hve-local-trial/v2",
            "run_id": timestamp,
            "trust_tier": "local-self-attested",
            "rankable": False,
            "official": False,
            "methodology": {
                "runner": {
                    "script_sha256": sha256_file(Path(__file__)),
                    "comparison_module_sha256": sha256_file(
                        REPO_ROOT / "src" / "atv_bench" / "comparison.py"
                    ),
                    "arena_engine_sha256": sha256_file(
                        REPO_ROOT / "src" / "atv_bench" / "arena" / "engine.py"
                    ),
                    "arena_referee_sha256": sha256_file(
                        REPO_ROOT / "src" / "atv_bench" / "arena" / "referee.py"
                    ),
                },
                "game": "lightcycles",
                "model": args.model,
                "requested_model": args.model,
                "model_selection_source": "explicit_cli",
                "model_identity_policy": (
                    "complete_jsonl_single_observed_model_exact_match_and_"
                    "successful_terminal_result"
                ),
                "model_identity_attestation": "copilot_cli_jsonl_self_reported",
                "copilot_cli": _run_text(node, loader, "--version").splitlines()[0],
                "node": _run_text(node, "--version"),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "prompt_sha256": hashlib.sha256(GOAL.encode("utf-8")).hexdigest(),
                "held_out_seeds": len(held_out),
                "held_out_seed_values": list(held_out),
                "seed_start": None if args.held_out_seed else args.seed_start,
                "per_turn_timeout_seconds": args.per_turn_timeout,
                "harness_timeout_seconds": args.timeout,
                "max_ai_credits": args.max_ai_credits,
                "side_swapped": True,
                "same_seed_repository": True,
                "parallel_builds": True,
                "self_attested": True,
                "independent_unit": "fresh_paired_harness_trial",
                "nested_games_are_not_independent_trials": True,
                "provider_secret_entered_harness_runtime": True,
                "network_policy_enforced": False,
                "tool_compatibility_shim": bool(args.tool_compat_shim),
                "tool_compatibility_shim_equal": len(set(shim_applied.values())) == 1,
                "tool_compatibility_shim_scope": (
                    "selected-agent frontmatter tools allowlist only"
                    if args.tool_compat_shim
                    else None
                ),
            },
            "sources": {
                "atv_phoenix": {
                    "repository": "All-The-Vibes/ATV-Phoenix",
                    "commit": git_commit(phoenix_repo),
                    "git_tree": git_tree(phoenix_repo),
                    "tracked_tree_listing_sha256": tracked_tree_listing_sha256(
                        phoenix_repo
                    ),
                    "fingerprint": scan_harness_assets(phoenix["copilot_home"]),
                    "runtime_tool_compatibility_shim": phoenix[
                        "tool_compatibility_shim"
                    ],
                },
                "hve_core": {
                    "repository": "microsoft/hve-core",
                    "commit": git_commit(hve_repo),
                    "git_tree": git_tree(hve_repo),
                    "tracked_tree_listing_sha256": tracked_tree_listing_sha256(
                        hve_repo
                    ),
                    "fingerprint": scan_harness_assets(hve["plugin"]),
                    "materialized_pointer_files": hve["resolved_pointers"],
                    "runtime_tool_compatibility_shim": hve[
                        "tool_compatibility_shim"
                    ],
                },
            },
            "builds": builds,
            "trial_outcome": {
                "independent_unit": "fresh_paired_harness_trial",
                "artifact_validity": artifact_validity,
                "execution_validity": execution_validity,
                "model_attestation_validity": attestation_validity,
                "build_comparability": build_comparability,
                "comparable": trial_comparable,
                "classification": classification,
                "quality_winner_claimed": False,
                "invalid_artifacts_persisted": True,
            },
            "series": series,
            "limitations": [
                "One model, one synthetic Lightcycles task contract, and local execution.",
                "Local self-attested execution, not trusted protocol-v1 OCI evidence.",
                "Requested model identity is only self-reported by Copilot CLI, not provider-signed.",
                "Provider credentials entered the harness process.",
                "Network isolation was requested in the prompt but not technically enforced.",
                "Games are nested under one fresh paired harness trial.",
                "This evidence cannot establish overall harness richness or sophistication.",
            ],
        }
        write_exact_text(
            out / "comparison.json",
            json.dumps(document, indent=2, sort_keys=True) + "\n",
        )
        write_exact_text(out / "README.md", _render_readme(document))
        write_checksums(out)
        print(
            json.dumps(
                {
                    "out": str(out),
                    "rankable": False,
                    "official": False,
                    "classification": classification,
                    "comparable": trial_comparable,
                    "nested_result": series["phoenix_vs_hve"]["summary"],
                },
                indent=2,
            )
        )
        if not trial_comparable:
            raise SystemExit(2)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
