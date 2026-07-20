#!/usr/bin/env python3
"""Audit ATV-Bench launch credibility requirements without network mutation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from atv_bench.launch_audit import (  # noqa: E402
    audit_launch,
    render_json,
    render_markdown,
)
from atv_bench.verification_manifest import (  # noqa: E402
    validate_evidence_manifest,
)


def _load_json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} is unreadable: {path}: {exc}") from exc
    try:
        document = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return document


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed local audit of every credibility launch gate and "
            "official release-checklist item."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to inspect (default: current directory).",
    )
    parser.add_argument(
        "--governance-json",
        type=Path,
        help="Fresh output from scripts/audit_github_governance.py.",
    )
    parser.add_argument(
        "--evidence-manifest",
        type=Path,
        help="Content-addressed test/build/evidence proof manifest.",
    )
    parser.add_argument(
        "--audit-date",
        default=date.today().isoformat(),
        help="Audit date or ISO-8601 timestamp (default: today).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Write the deterministic JSON report to this path.",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        help="Write the deterministic Markdown summary to this path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(os.path.abspath(os.fspath(args.repo_root)))
    if not repo_root.is_dir():
        print(f"launch audit input error: repository root is not a directory: {repo_root}", file=sys.stderr)
        return 2
    if (
        args.json_out is not None
        and args.markdown_out is not None
        and os.path.abspath(args.json_out) == os.path.abspath(args.markdown_out)
    ):
        print("launch audit input error: JSON and Markdown outputs must differ", file=sys.stderr)
        return 2

    try:
        governance = (
            _load_json_object(args.governance_json, "governance JSON")
            if args.governance_json is not None
            else None
        )
        evidence_manifest = (
            validate_evidence_manifest(repo_root, args.evidence_manifest)
            if args.evidence_manifest is not None
            else None
        )
        report = audit_launch(
            repo_root,
            audit_date=args.audit_date,
            governance=governance,
            evidence_manifest=evidence_manifest,
        )
    except (TypeError, ValueError) as exc:
        print(f"launch audit input error: {exc}", file=sys.stderr)
        return 2

    json_report = render_json(report)
    markdown_report = render_markdown(report)
    if args.json_out is not None:
        _write_text(args.json_out, json_report)
    if args.markdown_out is not None:
        _write_text(args.markdown_out, markdown_report)
    if args.json_out is None and args.markdown_out is None:
        sys.stdout.write(json_report)

    print(
        "launch audit: "
        f"ready={str(report.launch_ready).lower()} "
        f"blockers={report.blocker_count} "
        f"achieved={report.status_counts['achieved']} "
        f"blocked={report.status_counts['blocked']} "
        f"failed={report.status_counts['failed']} "
        f"unverified={report.status_counts['unverified']}",
        file=sys.stderr,
    )
    return 0 if report.launch_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
