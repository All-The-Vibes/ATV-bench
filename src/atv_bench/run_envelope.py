"""Run-CLI machine-readable envelope + stable exit codes (DX-1/DX-2/DX-3).

An AI agent is a named primary user of this CLI, so `run`/`doctor`/`--demo` emit a
stable JSON envelope and distinct per-failure-mode exit codes — matching CLAUDE.md's
API-envelope convention. Human-mode output is rendered separately by the CLI.

Envelope:
    {success, data:{...} | null, error:{code, exit_code, message, fix} | null}
"""
from __future__ import annotations

from typing import Any

# Stable, documented exit-code map (DX-2). Agents/CI distinguish retryable
# (timeout) from human-action (unauthenticated) failures by code.
EXIT_CODES: dict[str, int] = {
    "ok": 0,
    "usage": 2,
    "missing_cli": 3,
    "unauthenticated": 4,
    "docker_unavailable": 5,
    "policy_denied": 6,
    "timeout": 7,
    "model_unparseable": 8,
    "codeclash_dep": 9,
}


class RunError(Exception):
    """A run failure with a stable code, human message, and actionable fix hint."""

    def __init__(self, code: str, message: str, *, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    @property
    def exit_code(self) -> int:
        # Unknown code = a programming mistake; fail as usage (2), never crash.
        return EXIT_CODES.get(self.code, EXIT_CODES["usage"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "exit_code": self.exit_code,
            "message": self.message,
            "fix": self.fix,
        }


def ok_envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None}


def error_envelope(err: RunError) -> dict[str, Any]:
    return {"success": False, "data": None, "error": err.to_dict()}
