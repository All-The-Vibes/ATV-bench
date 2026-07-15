"""Contributor validation tools (devex T6).

`validate-harness` and `validate-game` are the ecosystem on-ramp: a contributor runs
them locally before opening a PR, so a broken harness reader or an unsafe bot fails on
their machine, not in review. Both reuse the same leak-safe scanner + shape guards the
production path uses — no second, weaker code path.
"""
from __future__ import annotations

from typing import Any

from atv_bench.fingerprint import reader
from atv_bench.fingerprint.probe import FINGERPRINT_SCHEMA_KEYS
from atv_bench.fingerprint.scan import is_safe_name, is_secret
from atv_bench.submit import validate_bot_shape
from atv_bench.errors import AtvError


def validate_harness_fingerprint(manifest: dict[str, Any]) -> dict[str, Any]:
    """Check a harness reader's output is schema-complete and leak-safe.

    A new harness adapter (copilot, codex, …) implements a reader that returns this
    manifest shape; this validates it before it can enter the league.
    """
    errors: list[str] = []
    # 1. schema completeness (allowlist keys exactly)
    missing = set(FINGERPRINT_SCHEMA_KEYS) - set(manifest)
    for k in sorted(missing):
        errors.append(f"missing required schema key: {k}")
    extra = set(manifest) - set(FINGERPRINT_SCHEMA_KEYS)
    for k in sorted(extra):
        errors.append(f"unexpected key not in fixed schema: {k}")
    # 2. leak-safety: every emitted name must pass the scanner
    for field in ("skills", "mcps", "plugins"):
        for name in manifest.get(field, []) or []:
            if not is_safe_name(name):
                errors.append(f"leak risk: {field} entry failed safety scan")
    model = manifest.get("model")
    if isinstance(model, str) and model != "unknown" and is_secret(model):
        errors.append("leak risk: model value looks secret-like")
    # 3. unknown[] entries carry a field + a reason from the locked schema enum
    for u in manifest.get("unknown", []) or []:
        if not isinstance(u, dict) or "field" not in u or "reason" not in u:
            errors.append("unknown[] entry missing field/reason")
        elif u["reason"] not in reader.VALID_REASONS:
            errors.append(
                f"unknown[] reason {u['reason']!r} not in schema enum "
                f"{sorted(reader.VALID_REASONS)}"
            )
    return {"ok": not errors, "errors": errors}


def validate_game_bot(bot_path: str) -> dict[str, Any]:
    """Check a submitted bot's shape/size before it is ever executed."""
    errors: list[str] = []
    try:
        validate_bot_shape(bot_path)
    except AtvError as e:
        errors.append(f"{e.problem} ({e.cause})")
    return {"ok": not errors, "errors": errors}
