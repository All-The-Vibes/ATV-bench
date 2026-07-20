#!/usr/bin/env python3
"""Run a local, non-rankable model-backed ATV evaluation.

The provider implementation is intentionally dependency-injected. The symbol
passed with ``--backend-factory`` must be a callable accepting the validated
``ModelBackedEvalPolicy`` and returning ``ProviderBindings``.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from atv_bench.operator import (
    ModelBackedEvalPlan,
    ModelBackedEvalPolicy,
    ModelBackedOperator,
    ModelBackedOperatorError,
    ProviderBindings,
)
from atv_bench.sandbox import CliOciEngine, OciTrialRunner


def _load_symbol(value: str) -> Callable[[ModelBackedEvalPolicy], ProviderBindings]:
    if ":" not in value:
        raise ModelBackedOperatorError(
            "backend_factory_invalid",
            "--backend-factory must use module.path:callable",
        )
    module_name, attribute = value.rsplit(":", 1)
    if not module_name or not attribute:
        raise ModelBackedOperatorError(
            "backend_factory_invalid",
            "--backend-factory must use module.path:callable",
        )
    try:
        module = importlib.import_module(module_name)
        factory: Any = getattr(module, attribute)
    except (ImportError, AttributeError):
        raise ModelBackedOperatorError(
            "backend_factory_unavailable",
            "the requested backend factory could not be imported",
        ) from None
    if not callable(factory):
        raise ModelBackedOperatorError(
            "backend_factory_invalid",
            "the backend factory symbol is not callable",
        )
    return factory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Execute a preregistered paired model-backed evaluation locally. "
            "All outputs are explicitly non-rankable."
        )
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--task", type=Path, action="append", required=True)
    parser.add_argument("--harness", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--backend-factory",
        required=True,
        help=(
            "Trusted Python symbol module.path:callable. It receives the "
            "validated policy and returns atv_bench.operator.ProviderBindings."
        ),
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "docker", "podman"),
        default="auto",
        help="OCI CLI to use for TrialController execution.",
    )
    return parser


def _engine(name: str) -> CliOciEngine:
    engine = CliOciEngine.auto() if name == "auto" else CliOciEngine(name)
    ok, detail = engine.daemon_status()
    if not ok:
        raise ModelBackedOperatorError(
            "oci_engine_unavailable",
            f"{engine.executable}: {detail}",
        )
    return engine


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = ModelBackedEvalPlan.load(
            policy_path=args.policy,
            task_paths=args.task,
            harness_paths=args.harness,
        )
        bindings = _load_symbol(args.backend_factory)(plan.policy)
        if not isinstance(bindings, ProviderBindings):
            raise ModelBackedOperatorError(
                "backend_factory_result_invalid",
                "backend factory must return ProviderBindings",
            )
        engine = _engine(args.engine)
        operator = ModelBackedOperator(
            providers=bindings,
            oci_runner_factory=lambda work_root, _server: OciTrialRunner(
                engine,
                work_root=work_root,
            ),
        )
        result = operator.run(plan, args.out)
        print(
            json.dumps(
                {
                    "status": "completed" if result.succeeded else "failed",
                    "output": str(result.root),
                    "plan_digest": result.plan_digest,
                    "schedule_id": result.schedule_id,
                    "attempts": len(result.attempts),
                    "trust_tier": result.trust_tier,
                    "rankable": result.rankable,
                    "official_verified": result.official_verified,
                },
                sort_keys=True,
            )
        )
        return 0 if result.succeeded else 2
    except ModelBackedOperatorError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": exc.code,
                    "message": exc.safe_message,
                    "rankable": False,
                    "official_verified": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
