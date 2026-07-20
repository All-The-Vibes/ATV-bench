"""Deterministic paired, blocked, and order-balanced trial scheduling."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Sequence

from ._canonical import sha256_json
from .trial import (
    BudgetProfile,
    HarnessRef,
    ModelPolicyRef,
    TaskRef,
    TrialAttempt,
    TrialSpec,
)


@dataclass(frozen=True, slots=True)
class ScheduledTrial:
    attempt: TrialAttempt
    block_id: str
    order_index: int
    sequence_index: int
    worker_id: str

    @property
    def spec(self) -> TrialSpec:
        return self.attempt.spec

    def retry(self, *, attempt_number: int, fresh_nonce: str) -> "ScheduledTrial":
        """Preserve the pairing assignment while requiring a new workspace nonce."""

        if attempt_number <= self.attempt.attempt_number:
            raise ValueError("retry attempt_number must increase")
        if fresh_nonce == self.attempt.fresh_nonce:
            raise ValueError("retry must use a new fresh_nonce")
        return ScheduledTrial(
            attempt=TrialAttempt(
                spec=self.spec,
                attempt_number=attempt_number,
                fresh_nonce=fresh_nonce,
            ),
            block_id=self.block_id,
            order_index=self.order_index,
            sequence_index=self.sequence_index,
            worker_id=self.worker_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "atv.scheduled-trial/v1",
            "block_id": self.block_id,
            "order_index": self.order_index,
            "sequence_index": self.sequence_index,
            "worker_id": self.worker_id,
            "trial": self.spec.to_dict(),
            "attempt": self.attempt.to_dict(),
        }


def _unique(items: Sequence[Any], *, label: str, key) -> tuple[Any, ...]:
    result = tuple(items)
    if not result:
        raise ValueError(f"{label} must not be empty")
    seen: set[Any] = set()
    for item in result:
        identity = key(item)
        if identity in seen:
            raise ValueError(f"duplicate {label} identity: {identity}")
        seen.add(identity)
    return result


def _derived_seed(seed: int, *parts: str) -> int:
    digest = sha256_json({"seed": seed, "parts": list(parts)})
    return int(digest[:16], 16)


def build_paired_schedule(
    *,
    benchmark_release: str,
    protocol_version: str,
    tasks: Sequence[TaskRef],
    harnesses: Sequence[HarnessRef],
    model_policies: Sequence[ModelPolicyRef],
    budget_profiles: Sequence[BudgetProfile],
    repetitions: int,
    seed: int,
    workers: Sequence[str],
) -> tuple[ScheduledTrial, ...]:
    """Build a fully crossed schedule with fresh paired trials.

    A block is task x model-policy x budget x repetition. Every harness appears
    exactly once in every block. Harness order is randomized once per logical
    task/model/budget group and rotated across repetitions, which provides exact
    balance whenever repetitions is a multiple of the harness count.
    """

    task_refs = _unique(tasks, label="tasks", key=lambda item: (item.id, item.version))
    harness_refs = _unique(
        harnesses, label="harnesses", key=lambda item: (item.id, item.version)
    )
    model_refs = _unique(
        model_policies,
        label="model policies",
        key=lambda item: (item.id, item.version),
    )
    budgets = _unique(budget_profiles, label="budget profiles", key=lambda item: item.id)
    worker_ids = _unique(
        tuple(str(worker) for worker in workers),
        label="workers",
        key=lambda item: item,
    )
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions <= 0:
        raise ValueError("repetitions must be a positive integer")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("seed must be an integer")

    schedule_definition = {
        "schema": "atv.schedule/v1",
        "benchmark_release": benchmark_release,
        "protocol_version": protocol_version,
        "tasks": [item.to_dict() for item in task_refs],
        "harnesses": [item.to_dict() for item in harness_refs],
        "model_policies": [item.to_dict() for item in model_refs],
        "budget_profiles": [item.to_dict() for item in budgets],
        "repetitions": repetitions,
        "seed": seed,
        "workers": list(worker_ids),
    }
    schedule_id = sha256_json(schedule_definition)

    blocks: list[tuple[TaskRef, ModelPolicyRef, BudgetProfile, int]] = [
        (task, model, budget, repetition)
        for task in task_refs
        for model in model_refs
        for budget in budgets
        for repetition in range(repetitions)
    ]
    random.Random(seed).shuffle(blocks)

    scheduled: list[ScheduledTrial] = []
    sequence_index = 0
    for task, model, budget, repetition in blocks:
        group_parts = (task.id, task.version, model.id, model.version, budget.id)
        base_order = list(harness_refs)
        random.Random(_derived_seed(seed, *group_parts)).shuffle(base_order)
        rotation = repetition % len(base_order)
        ordered_harnesses = base_order[rotation:] + base_order[:rotation]
        block_id = sha256_json(
            {
                "schema": "atv.schedule-block/v1",
                "schedule_id": schedule_id,
                "task": task.to_dict(),
                "model_policy": model.to_dict(),
                "budget_profile": budget.to_dict(),
                "repetition": repetition,
            }
        )

        for order_index, harness in enumerate(ordered_harnesses):
            spec = TrialSpec(
                benchmark_release=benchmark_release,
                protocol_version=protocol_version,
                schedule_id=schedule_id,
                task=task,
                harness=harness,
                model_policy=model,
                budget_profile=budget,
                repetition=repetition,
                schedule_seed=seed,
            )
            fresh_nonce = sha256_json(
                {
                    "schema": "atv.fresh-nonce/v1",
                    "schedule_id": schedule_id,
                    "block_id": block_id,
                    "harness": harness.to_dict(),
                    "repetition": repetition,
                }
            )
            scheduled.append(
                ScheduledTrial(
                    attempt=TrialAttempt(
                        spec=spec,
                        attempt_number=1,
                        fresh_nonce=fresh_nonce,
                    ),
                    block_id=block_id,
                    order_index=order_index,
                    sequence_index=sequence_index,
                    worker_id=worker_ids[sequence_index % len(worker_ids)],
                )
            )
            sequence_index += 1

    return tuple(scheduled)
