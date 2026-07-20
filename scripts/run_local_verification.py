#!/usr/bin/env python3
"""Run the fixed local ATV-Bench launch-verification evidence plan."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from atv_bench.verification_manifest import (  # noqa: E402
    LocalVerificationRunner,
    VerificationError,
    format_diagnostic,
)


def _configure_stdio() -> None:
    """Keep diagnostics printable on legacy Windows/CP1252 consoles."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Execute the fixed shell-free local verification plan and emit "
            "content-addressed launch evidence. This never runs a benchmark "
            "evaluation on GitHub Actions and never creates an official result."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--quick",
        action="store_true",
        help="Run focused local verification lanes only (default).",
    )
    mode.add_argument(
        "--full",
        action="store_true",
        help=(
            "Also run the full non-live suite, real Docker integrations, locked "
            "uv sync, and clean wheel/sdist installation checks."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="Repository root (default: this checkout).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("reports/local-verification"),
        help=(
            "Evidence directory under reports/local-verification "
            "(default: reports/local-verification)."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse only fresh, digest-valid, same-repository passing command "
            "evidence. Docker commands are always rerun."
        ),
    )
    parser.add_argument(
        "--governance-json",
        type=Path,
        help=(
            "Optional fresh output from audit_github_governance.py. It is "
            "validated, copied, and hashed; it is never executed."
        ),
    )
    return parser


def _error_diagnostic(exc: Exception) -> dict[str, object]:
    return {
        "Problem": "Local verification evidence could not be completed.",
        "Cause": str(exc),
        "Fix": (
            "Correct the input, prerequisite, or evidence-integrity problem and "
            "rerun the same fixed plan."
        ),
        "Evidence": [exc.__class__.__name__],
    }


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    mode = "full" if args.full else "quick"
    repo_root = Path(os.path.abspath(os.fspath(args.repo_root)))
    out_dir = args.out_dir
    if not out_dir.is_absolute():
        out_dir = repo_root / out_dir
    try:
        outcome = LocalVerificationRunner(
            repo_root,
            output_root=out_dir,
            mode=mode,
            governance_json=args.governance_json,
            resume=args.resume,
        ).run()
    except (OSError, VerificationError, ValueError) as exc:
        print(format_diagnostic(_error_diagnostic(exc)), file=sys.stderr)
        return 2

    diagnostic = {
        "Problem": (
            "No fixed-plan command failed."
            if outcome.plan_succeeded
            else "One or more fixed-plan commands failed, skipped, or were blocked."
        ),
        "Cause": (
            "All requested local checks produced passing bound evidence."
            if outcome.plan_succeeded
            else (
                "The content-addressed manifest records the exact failing or "
                "missing prerequisite. Local evidence was not promoted."
            )
        ),
        "Fix": (
            "No local verification fix is indicated."
            if outcome.plan_succeeded
            else "Inspect the command diagnostics, fix the cause, and rerun."
        ),
        "Evidence": [
            f"manifest={outcome.manifest_path}",
            f"manifest_file_sha256={outcome.manifest_sha256}",
            f"manifest_canonical_sha256={outcome.manifest_canonical_sha256}",
            f"proof={outcome.proof_path}",
            f"proof_file_sha256={outcome.proof_sha256}",
            f"proof_canonical_sha256={outcome.proof_canonical_sha256}",
            f"audit={outcome.audit_path}",
            f"audit_sha256={outcome.audit_sha256}",
            f"commands={dict(outcome.command_counts)}",
            f"gates={dict(outcome.gate_counts)}",
            "official_run_claimed=false",
            f"launch_ready={str(outcome.launch_ready).lower()}",
        ],
    }
    print(format_diagnostic(diagnostic))
    return 0 if outcome.plan_succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
