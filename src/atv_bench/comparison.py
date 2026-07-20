"""Helpers for reproducible, explicitly non-rankable local harness case studies.

This module supports exploratory Phoenix-versus-hve-core runs.  It does not produce
official benchmark evidence: execution is local and self-attested, provider credentials
enter the harness process, and the Lightcycles games are nested measurements under one
fresh harness execution rather than independent harness trials.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from atv_bench.arena.engine import Direction, TronEngine
from atv_bench.arena.referee import SubprocessMoveSource, run_match

CHECKSUM_SCHEMA = "atv.local-case-study-checksums/v1"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def write_exact_bytes(path: str | Path, payload: bytes) -> str:
    """Write bytes without newline/encoding transformations and return their digest."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return sha256_bytes(payload)


def write_exact_text(path: str | Path, value: str) -> str:
    return write_exact_bytes(path, value.encode("utf-8"))


def _checksum_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != "checksums.json"
    )


def write_checksums(root: str | Path) -> dict[str, Any]:
    """Bind every case-study file, including exact raw stream bytes."""
    directory = Path(root).resolve()
    rows = []
    for path in _checksum_files(directory):
        payload = path.read_bytes()
        rows.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "sha256": sha256_bytes(payload),
                "size_bytes": len(payload),
            }
        )
    document = {"schema": CHECKSUM_SCHEMA, "files": rows}
    write_exact_bytes(
        directory / "checksums.json",
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    return document


def verify_checksums(root: str | Path) -> tuple[bool, list[str]]:
    """Verify manifest shape, path confinement, exact coverage, size, and digest."""
    directory = Path(root).resolve()
    manifest = directory / "checksums.json"
    if not manifest.is_file():
        return False, ["checksums.json is missing"]
    try:
        document = json.loads(manifest.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, [f"checksums.json is unreadable: {exc}"]
    if not isinstance(document, dict) or document.get("schema") != CHECKSUM_SCHEMA:
        return False, ["checksum schema is invalid"]
    rows = document.get("files")
    if not isinstance(rows, list):
        return False, ["checksum file list is invalid"]

    errors: list[str] = []
    listed: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"checksum row {index} is invalid")
            continue
        relative = row.get("path")
        if not isinstance(relative, str):
            errors.append(f"checksum row {index} has no path")
            continue
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative != pure.as_posix():
            errors.append(f"checksum path is not confined: {relative!r}")
            continue
        if relative in listed:
            errors.append(f"duplicate checksum path: {relative}")
            continue
        listed.add(relative)
        target = directory.joinpath(*pure.parts)
        if not target.is_file():
            errors.append(f"checksummed file is missing: {relative}")
            continue
        payload = target.read_bytes()
        if row.get("size_bytes") != len(payload):
            errors.append(f"size mismatch: {relative}")
        if row.get("sha256") != sha256_bytes(payload):
            errors.append(f"digest mismatch: {relative}")

    actual = {
        path.relative_to(directory).as_posix() for path in _checksum_files(directory)
    }
    for relative in sorted(actual - listed):
        errors.append(f"unchecksummed file: {relative}")
    for relative in sorted(listed - actual):
        if not any(relative in error for error in errors):
            errors.append(f"manifest-only file: {relative}")
    return not errors, errors


def engine_for_seed(
    seed: int,
    *,
    board_profile: str = "standard",
    max_turns: int | None = None,
) -> TronEngine:
    """Create one deterministic asymmetric board for a paired game seed."""
    rng = random.Random(seed)
    if board_profile == "standard":
        width = 21 + 2 * (seed % 5)
        height = 17 + 2 * ((seed // 5) % 5)
    elif board_profile == "compact":
        width = 11 + 2 * (seed % 3)
        height = 9 + 2 * ((seed // 3) % 3)
    else:
        raise ValueError("board_profile must be standard or compact")
    if max_turns is not None and max_turns <= 0:
        raise ValueError("max_turns must be positive")
    y_a = rng.randrange(1, height - 1)
    y_b = rng.randrange(1, height - 1)
    if y_b == y_a:
        y_b = 1 + (y_b + max(2, height // 3)) % (height - 2)
    return TronEngine(
        width=width,
        height=height,
        start_a=(1, y_a),
        start_b=(width - 2, y_b),
        dir_a=Direction.RIGHT,
        dir_b=Direction.LEFT,
        max_turns=max_turns or width * height,
    )


def _winner_for_result(result: dict[str, Any], *, swapped: bool) -> str:
    side_winner = {
        "a_wins": "a",
        "b_wins": "b",
        "forfeit_a": "b",
        "forfeit_b": "a",
    }.get(result.get("outcome", "draw"))
    if side_winner is None:
        return "draw"
    if swapped:
        return "harness_a" if side_winner == "b" else "harness_b"
    return "harness_a" if side_winner == "a" else "harness_b"


def run_game(
    bot_a: str | Path,
    bot_b: str | Path,
    *,
    seed: int,
    swapped: bool = False,
    per_turn_timeout: float = 3.0,
    match_timeout: float | None = 60.0,
    board_profile: str = "standard",
    max_turns: int | None = None,
) -> dict[str, Any]:
    """Run one game and map side-relative outcomes to stable harness identities."""
    import sys

    left = Path(bot_b if swapped else bot_a)
    right = Path(bot_a if swapped else bot_b)
    source_a = SubprocessMoveSource(
        [sys.executable, str(left)], per_turn_timeout=per_turn_timeout
    )
    source_b = SubprocessMoveSource(
        [sys.executable, str(right)], per_turn_timeout=per_turn_timeout
    )
    try:
        result = run_match(
            engine_for_seed(
                seed,
                board_profile=board_profile,
                max_turns=max_turns,
            ),
            source_a,
            source_b,
            player_a="right" if swapped else "left",
            player_b="left" if swapped else "right",
            match_id=f"comparison-{seed}-{'swap' if swapped else 'normal'}",
            game="lightcycles",
            seed=seed,
            match_timeout_seconds=match_timeout,
        )
    finally:
        source_a.close()
        source_b.close()
    return {
        "seed": seed,
        "swapped": swapped,
        "winner": _winner_for_result(result, swapped=swapped),
        "outcome": result.get("outcome", "draw"),
        "forfeit_reason": result.get("forfeit_reason"),
        "termination_reason": result.get("termination_reason"),
    }


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> dict[str, float]:
    if trials <= 0:
        raise ValueError("trials must be positive")
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
        / denominator
    )
    return {
        "lo": round(max(0.0, center - margin), 4),
        "hi": round(min(1.0, center + margin), 4),
    }


def summarize_games(games: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(games)
    counts = Counter(row["winner"] for row in rows)
    decisive = counts["harness_a"] + counts["harness_b"]
    return {
        "games": len(rows),
        "harness_a_wins": counts["harness_a"],
        "harness_b_wins": counts["harness_b"],
        "draws": counts["draw"],
        "harness_a_decisive_win_rate": (
            round(counts["harness_a"] / decisive, 4) if decisive else None
        ),
        "harness_b_decisive_win_rate": (
            round(counts["harness_b"] / decisive, 4) if decisive else None
        ),
        "harness_a_decisive_win_rate_ci95": (
            wilson_interval(counts["harness_a"], decisive) if decisive else None
        ),
        "harness_b_decisive_win_rate_ci95": (
            wilson_interval(counts["harness_b"], decisive) if decisive else None
        ),
    }


def play_series(
    bot_a: str | Path,
    bot_b: str | Path,
    *,
    seeds: Iterable[int],
    per_turn_timeout: float = 3.0,
    match_timeout: float | None = 60.0,
    board_profile: str = "standard",
    max_turns: int | None = None,
) -> dict[str, Any]:
    """Run two side-swapped games per seed; games remain nested observations."""
    games: list[dict[str, Any]] = []
    for seed in seeds:
        seed = int(seed)
        games.append(
            run_game(
                bot_a,
                bot_b,
                seed=seed,
                swapped=False,
                per_turn_timeout=per_turn_timeout,
                match_timeout=match_timeout,
                board_profile=board_profile,
                max_turns=max_turns,
            )
        )
        games.append(
            run_game(
                bot_a,
                bot_b,
                seed=seed,
                swapped=True,
                per_turn_timeout=per_turn_timeout,
                match_timeout=match_timeout,
                board_profile=board_profile,
                max_turns=max_turns,
            )
        )
    return {"summary": summarize_games(games), "games": games}


def materialize_pointer_tree(
    source: str | Path,
    destination: str | Path,
    *,
    source_root: str | Path | None = None,
) -> int:
    """Copy a plugin and resolve Windows one-line Git-symlink pointer files."""
    src = Path(source).resolve()
    dst = Path(destination).resolve()
    root = Path(source_root).resolve() if source_root else src
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    resolved = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.stat().st_size > 512:
            continue
        try:
            pointer = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            continue
        if not pointer or "\n" in pointer or not pointer.startswith("."):
            continue
        target = (path.parent / pointer).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        output = dst / path.relative_to(src)
        if target.is_file():
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(target.read_bytes())
            resolved += 1
        elif target.is_dir():
            if output.exists():
                shutil.rmtree(output) if output.is_dir() else output.unlink()
            shutil.copytree(target, output)
            resolved += 1
    return resolved


def _frontmatter_name(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:20]:
            if line.lower().startswith("name:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    except (OSError, UnicodeDecodeError):
        pass
    for suffix in (".agent.md", ".prompt.md", ".instructions.md", ".md"):
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    return path.stem


def scan_harness_assets(root: str | Path) -> dict[str, Any]:
    """Return a names-only repository fingerprint; counts are not capability scores."""
    base = Path(root)
    skills = sorted(
        {
            path.parent.name
            for path in base.rglob("SKILL.md")
            if ".git" not in path.parts
        }
    )
    agents = sorted(
        {
            _frontmatter_name(path)
            for path in base.rglob("*.md")
            if (
                path.name.endswith(".agent.md")
                or "agents" in {part.lower() for part in path.parts}
            )
            and ".git" not in path.parts
        }
    )
    prompts = sorted(
        {
            _frontmatter_name(path)
            for path in base.rglob("*.prompt.md")
            if ".git" not in path.parts
        }
    )
    instructions = sorted(
        {
            _frontmatter_name(path)
            for path in base.rglob("*.instructions.md")
            if ".git" not in path.parts
        }
    )
    plugins: set[str] = set()
    for path in [*base.rglob("plugin.json"), *base.rglob("marketplace.json")]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("name"), str):
            plugins.add(payload["name"])
        if isinstance(payload, dict):
            for item in payload.get("plugins", []):
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    plugins.add(item["name"])
    return {
        "skills": skills,
        "agents": agents,
        "prompts": prompts,
        "instructions": instructions,
        "plugins": sorted(plugins),
        "counts": {
            "skills": len(skills),
            "agents": len(agents),
            "prompts": len(prompts),
            "instructions": len(instructions),
            "plugins": len(plugins),
        },
    }


def parse_copilot_jsonl(payload: bytes | str) -> dict[str, Any]:
    """Extract a bounded summary plus a fail-closed model-evidence receipt.

    Every model-bearing event is retained as a normalized identifier. Malformed
    lines are counted rather than silently ignored so callers can reject partial
    or mixed receipts instead of trusting whichever model happened to appear last.
    """
    utf8_decode_errors = 0
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            utf8_decode_errors = 1
            text = payload.decode("utf-8", errors="replace")
    else:
        text = payload
    final = ""
    result: dict[str, Any] = {}
    skill_sources: Counter[str] = Counter()
    mcp_servers: list[dict[str, Any]] = []
    observed_models: list[str] = []
    model_event_types: Counter[str] = Counter()
    parse_error_count = 0
    non_object_event_count = 0
    terminal_result_count = 0
    nonempty_line_count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        nonempty_line_count += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_error_count += 1
            continue
        if not isinstance(event, dict):
            non_object_event_count += 1
            continue
        raw_data = event.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        event_type = str(event.get("type", "unknown"))
        event_model = data.get("model")
        if isinstance(event_model, str) and event_model.strip():
            observed_models.append(event_model.strip())
            model_event_types[event_type] += 1
        if event.get("type") == "assistant.message":
            if isinstance(data.get("content"), str):
                final = data["content"][-16_384:]
        elif event.get("type") == "session.skills_loaded":
            for skill in data.get("skills", []):
                if isinstance(skill, dict) and skill.get("enabled", True):
                    skill_sources[str(skill.get("source", "unknown"))] += 1
        elif event.get("type") == "session.mcp_servers_loaded":
            mcp_servers = [
                {
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "source": item.get("source"),
                }
                for item in data.get("servers", [])
                if isinstance(item, dict)
            ]
        elif event.get("type") == "result":
            terminal_result_count += 1
            result = {
                "exit_code": event.get("exitCode"),
                "session_id": event.get("sessionId"),
                "usage": event.get("usage", {}),
            }
    unique_models = sorted(set(observed_models))
    terminal_success = (
        terminal_result_count == 1
        and result.get("exit_code") == 0
        and isinstance(result.get("session_id"), str)
        and bool(result["session_id"])
    )
    return {
        "model": unique_models[0] if len(unique_models) == 1 else None,
        "observed_models": unique_models,
        "model_event_count": len(observed_models),
        "model_event_types": dict(sorted(model_event_types.items())),
        "parse_error_count": parse_error_count,
        "utf8_decode_errors": utf8_decode_errors,
        "non_object_event_count": non_object_event_count,
        "nonempty_line_count": nonempty_line_count,
        "terminal_result_count": terminal_result_count,
        "terminal_result_seen": terminal_result_count > 0,
        "terminal_success": terminal_success,
        "final_message": final,
        "result": result,
        "enabled_skill_sources": dict(skill_sources),
        "mcp_servers": mcp_servers,
    }


def attest_copilot_model_receipt(
    runtime: dict[str, Any],
    *,
    requested_model: str,
) -> dict[str, Any]:
    """Evaluate a parsed Copilot JSONL receipt without trusting a collapsed model."""
    observed = runtime.get("observed_models", [])
    parse_errors = int(runtime.get("parse_error_count", 0))
    utf8_errors = int(runtime.get("utf8_decode_errors", 0))
    non_object_events = int(runtime.get("non_object_event_count", 0))
    terminal_result_count = int(runtime.get("terminal_result_count", 0))
    terminal_success = bool(runtime.get("terminal_success"))
    reasons: list[str] = []
    if parse_errors:
        reasons.append("malformed-jsonl")
    if utf8_errors:
        reasons.append("invalid-utf8")
    if non_object_events:
        reasons.append("non-object-jsonl-event")
    if not isinstance(observed, list) or not observed:
        reasons.append("model-evidence-missing")
    elif len(observed) != 1:
        reasons.append("mixed-model-evidence")
    elif observed[0] != requested_model:
        reasons.append("requested-reported-model-mismatch")
    if terminal_result_count != 1:
        reasons.append("terminal-result-count-invalid")
    if not terminal_success:
        reasons.append("terminal-result-unsuccessful")
    return {
        "status": "pass" if not reasons else "fail",
        "requested_model": requested_model,
        "selection_source": "not_attested_by_jsonl_receipt",
        "observed_models": observed if isinstance(observed, list) else [],
        "model_event_count": int(runtime.get("model_event_count", 0)),
        "model_event_types": runtime.get("model_event_types", {}),
        "parse_error_count": parse_errors,
        "utf8_decode_errors": utf8_errors,
        "non_object_event_count": non_object_events,
        "terminal_result_count": terminal_result_count,
        "terminal_success": terminal_success,
        "provider_signed": False,
        "reasons": reasons,
    }


def _git(path: str | Path, *args: str, text: bool = True) -> str | bytes:
    process = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=text,
        check=True,
    )
    return process.stdout.strip() if text else process.stdout


def git_commit(path: str | Path) -> str:
    return str(_git(path, "rev-parse", "HEAD"))


def git_tree(path: str | Path) -> str:
    return str(_git(path, "rev-parse", "HEAD^{tree}"))


def tracked_tree_listing_sha256(path: str | Path) -> str:
    payload = _git(path, "ls-tree", "-r", "-z", "--full-tree", "HEAD", text=False)
    assert isinstance(payload, bytes)
    return sha256_bytes(payload)
