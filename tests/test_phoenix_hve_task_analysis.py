from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import summarize_phoenix_hve_tasks as analysis

BOOTSTRAP_SAMPLES = 400
BOOTSTRAP_SEED = 73
MODEL = "gpt-5.4"
BUDGET = {
    "max_ai_credits": 30,
    "timeout_seconds": 900,
}
CATEGORIES = (
    "greenfield",
    "repair",
    "debugging",
    "recovery",
    "context-retrieval",
)


def _seal(value: dict, field: str) -> dict:
    unsigned = {key: item for key, item in value.items() if key != field}
    return {**unsigned, field: analysis.sha256_json(unsigned)}


def _task_digest(task_id: str) -> str:
    return analysis.sha256_json({"task_id": task_id, "fixture": "analysis-v1"})


def _experiment_digest() -> str:
    return analysis.sha256_json({"experiment": "signed-analysis-fixture-v1"})


def _frozen_tasks() -> list[dict]:
    return [
        {
            "task_id": f"task-{index:03d}",
            "category": CATEGORIES[index % len(CATEGORIES)],
            "task_digest": _task_digest(f"task-{index:03d}"),
        }
        for index in range(analysis.FORMAL_TASK_COUNT)
    ]


def _preregistration(
    *,
    tasks: list[dict] | None = None,
    experiment_digest: str | None = None,
    model: str = MODEL,
    budget: dict | None = None,
) -> dict:
    unsigned = {
        "schema": analysis.PREREGISTRATION_SCHEMA,
        "experiment_digest": experiment_digest or _experiment_digest(),
        "attempts_per_task": analysis.ATTEMPTS_PER_TASK,
        "model": {"requested": model},
        "budget": dict(budget or BUDGET),
        "analysis_policy": {
            "superiority_equivalence_margin": analysis.DEFAULT_MARGIN,
            "confidence": analysis.DEFAULT_CONFIDENCE,
            "bootstrap_samples": BOOTSTRAP_SAMPLES,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "reliability_alpha": analysis.DEFAULT_RELIABILITY_ALPHA,
            "superiority_min_at_least_one_reliable_suite_rate": (
                analysis.SUPERIORITY_MIN_AT_LEAST_ONE_RELIABLE_SUITE_RATE
            ),
            "superiority_min_at_least_one_reliable_per_task": (
                analysis.SUPERIORITY_MIN_AT_LEAST_ONE_RELIABLE_PER_TASK
            ),
            "equivalence_min_both_reliable_suite_rate": (
                analysis.EQUIVALENCE_MIN_BOTH_RELIABLE_SUITE_RATE
            ),
            "equivalence_min_both_reliable_per_task": (
                analysis.EQUIVALENCE_MIN_BOTH_RELIABLE_PER_TASK
            ),
        },
        "tasks": deepcopy(tasks or _frozen_tasks()),
    }
    return _seal(unsigned, "preregistration_sha256")


def _receipt(
    *,
    model: str,
    reliable: bool,
    artifact_score: float | None,
    order_index: int,
) -> dict:
    execution_valid = True
    model_attestation_valid = True
    artifact_valid = reliable
    unsigned = {
        "order_index": order_index,
        "execution": {
            "status": "ok",
            "valid": execution_valid,
            "terminal_success": True,
        },
        "model_attestation": {
            "status": "pass",
            "requested_model": model,
            "observed_models": [model],
            "provider_signed": False,
            "reasons": [],
        },
        "artifact": {
            "valid": artifact_valid,
            "raw_score": artifact_score,
        },
        "reliability": {
            "reliable": reliable,
            "classification": (
                "reliable-completion" if reliable else "artifact-invalid"
            ),
            "execution_valid": execution_valid,
            "model_attestation_valid": model_attestation_valid,
            "artifact_valid": artifact_valid,
        },
        "reported_usage": {},
    }
    return _seal(unsigned, "receipt_sha256")


def _harness_result(
    *,
    model: str,
    score: float,
    reliable: bool,
    order_index: int,
) -> dict:
    analysis_score = score if reliable else 0.0
    artifact_score = score if reliable else None
    return {
        "score": analysis_score,
        "reliable": reliable,
        "artifact_score": artifact_score,
        "passed": reliable,
        "receipt": _receipt(
            model=model,
            reliable=reliable,
            artifact_score=artifact_score,
            order_index=order_index,
        ),
    }


def _attempt(
    task: dict,
    repetition: int,
    *,
    experiment_digest: str,
    model: str = MODEL,
    budget: dict | None = None,
    phoenix_score: float,
    hve_score: float,
    phoenix_reliable: bool = True,
    hve_reliable: bool = True,
    infrastructure_valid: bool = True,
) -> dict:
    order = ["phoenix", "hve"] if repetition % 2 == 0 else ["hve", "phoenix"]
    phoenix = _harness_result(
        model=model,
        score=phoenix_score,
        reliable=phoenix_reliable,
        order_index=order.index("phoenix"),
    )
    hve = _harness_result(
        model=model,
        score=hve_score,
        reliable=hve_reliable,
        order_index=order.index("hve"),
    )
    unsigned = {
        "attempt_id": analysis.sha256_json(
            {
                "experiment_digest": experiment_digest,
                "task_digest": task["task_digest"],
                "repetition": repetition,
            }
        ),
        "repetition": repetition,
        "infrastructure_valid": infrastructure_valid,
        "randomized_order": order,
        "randomization_key_sha256": analysis.sha256_json(
            {
                "task_digest": task["task_digest"],
                "repetition": repetition,
            }
        ),
        "model": {
            "requested": model,
            "selection_source": "explicit_cli",
        },
        "budget": dict(budget or BUDGET),
        "phoenix": phoenix,
        "hve": hve,
        "paired_score_difference_phoenix_minus_hve": (phoenix["score"] - hve["score"]),
        "evidence": {
            "relative_path": f"{task['task_id']}/attempt-{repetition}",
            "committed_before_checkpoint": True,
        },
    }
    return _seal(unsigned, "attempt_sha256")


def _task_document(
    task: dict,
    *,
    experiment_digest: str,
    model: str = MODEL,
    budget: dict | None = None,
    preregistration_sha256: str | None = None,
    phoenix_score: float = 0.8,
    hve_score: float = 0.5,
    attempts: list[dict] | None = None,
) -> dict:
    attempt_rows = attempts
    if attempt_rows is None:
        attempt_rows = [
            _attempt(
                task,
                repetition,
                experiment_digest=experiment_digest,
                model=model,
                budget=budget,
                phoenix_score=phoenix_score,
                hve_score=hve_score,
            )
            for repetition in range(analysis.ATTEMPTS_PER_TASK)
        ]
    unsigned = {
        "schema": analysis.INPUT_SCHEMA,
        "task_id": task["task_id"],
        "category": task["category"],
        "task_digest": task["task_digest"],
        "task_version": "1",
        "prompt_sha256": analysis.sha256_json(
            {"task_id": task["task_id"], "kind": "prompt"}
        ),
        "workspace_tree_digest": analysis.sha256_json(
            {"task_id": task["task_id"], "kind": "workspace"}
        ),
        "eligible": True,
        "rankable": False,
        "official": False,
        "experiment_digest": experiment_digest,
        "model": {
            "requested": model,
            "selection_source": "explicit_cli",
            "provider_signed": False,
        },
        "budget": dict(budget or BUDGET),
        "attempts": attempt_rows,
        "checkpoint": {
            "completed_attempts": len(attempt_rows),
            "required_attempts": analysis.ATTEMPTS_PER_TASK,
            "complete": len(attempt_rows) == analysis.ATTEMPTS_PER_TASK,
            "updated_at": "2026-07-21T00:00:00Z",
        },
    }
    if preregistration_sha256 is not None:
        unsigned["preregistration_sha256"] = preregistration_sha256
    return _seal(unsigned, "document_sha256")


def _write_task(root: Path, document: dict, *, name: str | None = None) -> Path:
    path = root / (name or f"{document['task_id']}.json")
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _write_suite(
    root: Path,
    preregistration: dict,
    *,
    phoenix_score: float = 0.8,
    hve_score: float = 0.5,
    task_limit: int | None = None,
    attempts_for_task=None,
) -> None:
    tasks = preregistration["tasks"]
    if task_limit is not None:
        tasks = tasks[:task_limit]
    for task in tasks:
        attempts = attempts_for_task(task) if attempts_for_task is not None else None
        _write_task(
            root,
            _task_document(
                task,
                experiment_digest=preregistration["experiment_digest"],
                model=preregistration["model"]["requested"],
                budget=preregistration["budget"],
                preregistration_sha256=preregistration["preregistration_sha256"],
                phoenix_score=phoenix_score,
                hve_score=hve_score,
                attempts=attempts,
            ),
        )


def _summarize(root: Path, preregistration: dict | Path) -> dict:
    return analysis.summarize_root(
        root,
        preregistration=preregistration,
        bootstrap_samples=BOOTSTRAP_SAMPLES,
        seed=BOOTSTRAP_SEED,
    )


def _gate(output: dict, code: str) -> dict:
    return next(item for item in output["gates"]["items"] if item["code"] == code)


def _resign_document(document: dict) -> None:
    document.pop("document_sha256", None)
    document["document_sha256"] = analysis.sha256_json(document)


def _resign_attempt(attempt: dict) -> None:
    attempt.pop("attempt_sha256", None)
    attempt["attempt_sha256"] = analysis.sha256_json(attempt)


def _resign_receipt(receipt: dict) -> None:
    receipt.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = analysis.sha256_json(receipt)


def test_tasks_are_macro_units_and_attempts_remain_nested(tmp_path):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration)

    output = _summarize(tmp_path, preregistration)

    assert output["formal_analysis"] is True
    assert output["cluster_unit"] == "task"
    assert output["attempts_are_nested"] is True
    assert output["eligible_task_count"] == 20
    assert output["paired_task_bootstrap"]["observation_count"] == 20
    assert output["paired_task_bootstrap"]["resampling_unit"] == "task"
    assert output["nested_attempt_totals_descriptive_only"] == {
        "paired_attempts": 100,
        "harness_observations": 200,
    }
    assert output["macro_average"]["estimand"] == (
        "end_to_end_completion_adjusted_quality"
    )
    assert output["macro_average"]["phoenix_minus_hve"] == pytest.approx(0.3)
    assert {row["attempt_count"] for row in output["task_effects"]} == {5}


def test_signed_frozen_suite_can_support_non_rankable_phoenix_winner(tmp_path):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration)

    output = _summarize(tmp_path, preregistration)

    assert output["schema"] == analysis.OUTPUT_SCHEMA
    assert output["rankable"] is False
    assert output["official"] is False
    assert output["global_harness_winner"] is None
    assert output["score_decision_before_gates"] == "phoenix_superior"
    assert output["gates"]["all_passed"] is True
    assert output["decision"] == "phoenix_superior"
    assert output["winner"] == "phoenix"
    assert output["paired_task_bootstrap"]["ci"]["low"] > 0.05
    assert (
        output["experiment_binding"]["experiment_digest"]
        == (preregistration["experiment_digest"])
    )


def test_conditional_both_reliable_quality_is_task_clustered_and_descriptive(
    tmp_path,
):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration)

    output = _summarize(tmp_path, preregistration)
    conditional = output["conditional_both_reliable_quality"]

    assert conditional["role"] == "descriptive_only"
    assert conditional["cluster_unit"] == "task"
    assert conditional["task_count"] == 20
    assert conditional["both_reliable_attempt_count"] == 100
    assert conditional["macro_average"]["phoenix_minus_hve"] == pytest.approx(0.3)
    assert conditional["paired_task_bootstrap"]["observation_count"] == 20
    assert conditional["paired_task_bootstrap"]["resampling_unit"] == "task"


def test_category_reversal_blocks_an_otherwise_positive_aggregate(tmp_path):
    preregistration = _preregistration()
    for task in preregistration["tasks"]:
        if task["category"] == CATEGORIES[-1]:
            phoenix_score, hve_score = 0.4, 0.6
        else:
            phoenix_score, hve_score = 0.9, 0.4
        _write_task(
            tmp_path,
            _task_document(
                task,
                experiment_digest=preregistration["experiment_digest"],
                preregistration_sha256=preregistration["preregistration_sha256"],
                phoenix_score=phoenix_score,
                hve_score=hve_score,
            ),
        )

    output = _summarize(tmp_path, preregistration)

    assert output["score_decision_before_gates"] == "phoenix_superior"
    sensitivity = output["category_sensitivity"]
    assert sensitivity["passed"] is False
    assert CATEGORIES[-1] in sensitivity["opposing_or_out_of_margin_categories"]
    assert _gate(output, "category_sensitivity")["passed"] is False
    assert output["decision"] == "inconclusive"
    assert output["winner"] is None


def test_reliability_uses_exact_task_signs_not_nested_attempts(tmp_path):
    preregistration = _preregistration()

    def attempts_for_task(task):
        index = int(task["task_id"].split("-")[-1])
        return [
            _attempt(
                task,
                repetition,
                experiment_digest=preregistration["experiment_digest"],
                phoenix_score=0.0,
                hve_score=0.0,
                phoenix_reliable=True,
                hve_reliable=not (index < 16 and repetition == 4),
            )
            for repetition in range(analysis.ATTEMPTS_PER_TASK)
        ]

    _write_suite(
        tmp_path,
        preregistration,
        attempts_for_task=attempts_for_task,
    )
    output = _summarize(tmp_path, preregistration)

    exact = output["reliability"]["exact_task_level_sign_test"]
    assert exact["unit"] == "task"
    assert exact["phoenix_better_tasks"] == 16
    assert exact["hve_better_tasks"] == 0
    assert exact["tied_tasks"] == 4
    assert exact["decisive_units"] == 16
    assert exact["two_sided_p_value"] == pytest.approx(0.000031)
    assert output["reliability"]["task_level_reliability_winner"] == "phoenix"
    nested = output["reliability"]["nested_attempt_pairs_descriptive_only"]
    assert nested["nested_descriptive_only"] is True
    assert nested["p_value_omitted"]
    assert nested["paired_outcomes"]["phoenix_only_reliable"] == 16
    assert output["score_decision_before_gates"] == "practically_equivalent"
    assert _gate(output, "informative_coverage")["passed"] is False
    assert _gate(output, "score_reliability_consistency")["passed"] is False
    assert output["winner"] is None


def test_opposite_exact_reliability_result_blocks_score_winner(tmp_path):
    preregistration = _preregistration()

    def attempts_for_task(task):
        return [
            _attempt(
                task,
                repetition,
                experiment_digest=preregistration["experiment_digest"],
                phoenix_score=0.8,
                hve_score=0.3,
                phoenix_reliable=repetition < 4,
                hve_reliable=True,
            )
            for repetition in range(analysis.ATTEMPTS_PER_TASK)
        ]

    _write_suite(
        tmp_path,
        preregistration,
        attempts_for_task=attempts_for_task,
    )
    output = _summarize(tmp_path, preregistration)

    assert output["score_decision_before_gates"] == "phoenix_superior"
    assert output["reliability"]["task_level_reliability_winner"] == "hve"
    assert _gate(output, "informative_coverage")["passed"] is True
    assert _gate(output, "score_reliability_consistency")["passed"] is False
    assert output["decision"] == "inconclusive"
    assert output["winner"] is None


def test_margin_can_support_practical_equivalence_with_strong_coverage(tmp_path):
    preregistration = _preregistration()
    _write_suite(
        tmp_path,
        preregistration,
        phoenix_score=0.51,
        hve_score=0.49,
    )

    output = _summarize(tmp_path, preregistration)

    policy = output["policy"]["informative_coverage"]
    assert policy["superiority"] == {
        "pair_definition": "at_least_one_harness_reliable",
        "minimum_suite_rate": 0.9,
        "minimum_pairs_per_task": 4,
    }
    assert policy["equivalence"] == {
        "pair_definition": "both_harnesses_reliable",
        "minimum_suite_rate": 0.9,
        "minimum_pairs_per_task": 4,
    }
    assert output["score_decision_before_gates"] == "practically_equivalent"
    assert _gate(output, "informative_coverage")["passed"] is True
    assert output["gates"]["all_passed"] is True
    assert output["decision"] == "practically_equivalent"
    assert output["winner"] is None


def test_all_unreliable_can_never_yield_equivalence(tmp_path):
    preregistration = _preregistration()

    def attempts_for_task(task):
        return [
            _attempt(
                task,
                repetition,
                experiment_digest=preregistration["experiment_digest"],
                phoenix_score=0.8,
                hve_score=0.5,
                phoenix_reliable=False,
                hve_reliable=False,
            )
            for repetition in range(analysis.ATTEMPTS_PER_TASK)
        ]

    _write_suite(
        tmp_path,
        preregistration,
        attempts_for_task=attempts_for_task,
    )
    output = _summarize(tmp_path, preregistration)

    assert output["score_decision_before_gates"] == "practically_equivalent"
    assert output["informative_coverage"]["required_coverage"] == (
        "both_harnesses_reliable"
    )
    assert output["informative_coverage"]["observed_suite_rate"] == 0.0
    assert _gate(output, "informative_coverage")["passed"] is False
    assert output["conditional_both_reliable_quality"]["task_count"] == 0
    assert output["decision"] == "inconclusive"
    assert output["winner"] is None


def test_superiority_requires_per_task_informative_coverage(tmp_path):
    preregistration = _preregistration()
    weak_task_id = preregistration["tasks"][0]["task_id"]

    def attempts_for_task(task):
        return [
            _attempt(
                task,
                repetition,
                experiment_digest=preregistration["experiment_digest"],
                phoenix_score=0.8,
                hve_score=0.5,
                phoenix_reliable=not (
                    task["task_id"] == weak_task_id and repetition >= 3
                ),
                hve_reliable=not (task["task_id"] == weak_task_id and repetition >= 3),
            )
            for repetition in range(analysis.ATTEMPTS_PER_TASK)
        ]

    _write_suite(
        tmp_path,
        preregistration,
        attempts_for_task=attempts_for_task,
    )
    output = _summarize(tmp_path, preregistration)

    assert output["score_decision_before_gates"] == "phoenix_superior"
    coverage = output["informative_coverage"]
    assert coverage["suite"]["at_least_one_reliable_rate"] == pytest.approx(0.98)
    assert coverage["tasks_below_minimum"] == [weak_task_id]
    assert _gate(output, "informative_coverage")["passed"] is False
    assert output["winner"] is None


def test_task_substitution_is_rejected_even_when_resigned(tmp_path):
    preregistration = _preregistration()
    _write_suite(
        tmp_path,
        preregistration,
        task_limit=analysis.FORMAL_TASK_COUNT - 1,
    )
    substitute = {
        "task_id": "task-substitute",
        "category": CATEGORIES[-1],
        "task_digest": _task_digest("task-substitute"),
    }
    _write_task(
        tmp_path,
        _task_document(
            substitute,
            experiment_digest=preregistration["experiment_digest"],
            preregistration_sha256=preregistration["preregistration_sha256"],
        ),
    )

    with pytest.raises(
        analysis.TaskAnalysisError,
        match="unexpected task_id|extra or substituted tasks",
    ):
        _summarize(tmp_path, preregistration)


@pytest.mark.parametrize("case", ["missing", "extra"])
def test_missing_or_extra_task_is_rejected(tmp_path, case):
    preregistration = _preregistration()
    if case == "missing":
        _write_suite(
            tmp_path,
            preregistration,
            task_limit=analysis.FORMAL_TASK_COUNT - 1,
        )
        expected = "missing preregistered tasks"
    else:
        _write_suite(tmp_path, preregistration)
        extra = {
            "task_id": "task-extra",
            "category": CATEGORIES[0],
            "task_digest": _task_digest("task-extra"),
        }
        _write_task(
            tmp_path,
            _task_document(
                extra,
                experiment_digest=preregistration["experiment_digest"],
                preregistration_sha256=preregistration["preregistration_sha256"],
            ),
        )
        expected = "extra or substituted tasks"

    with pytest.raises(analysis.TaskAnalysisError, match=expected):
        _summarize(tmp_path, preregistration)


@pytest.mark.parametrize("field", ["experiment", "model", "budget"])
def test_mixed_experiment_model_or_budget_is_rejected(tmp_path, field):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration)
    path = tmp_path / f"{preregistration['tasks'][0]['task_id']}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if field == "experiment":
        document["experiment_digest"] = analysis.sha256_json(
            {"different": "experiment"}
        )
    elif field == "model":
        document["model"]["requested"] = "different-model"
    else:
        document["budget"]["max_ai_credits"] += 1
    _resign_document(document)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(analysis.TaskAnalysisError, match=field):
        _summarize(tmp_path, preregistration)


@pytest.mark.parametrize("field", ["model", "budget"])
def test_attempt_model_and_budget_are_bound_independently(tmp_path, field):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration)
    path = tmp_path / f"{preregistration['tasks'][0]['task_id']}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    attempt = document["attempts"][0]
    if field == "model":
        attempt["model"]["requested"] = "different-model"
    else:
        attempt["budget"]["timeout_seconds"] += 1
    _resign_attempt(attempt)
    _resign_document(document)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(analysis.TaskAnalysisError, match=f"attempt 0: attempt {field}"):
        _summarize(tmp_path, preregistration)


@pytest.mark.parametrize(
    ("level", "seal_field"),
    [
        ("document", "document_sha256"),
        ("attempt", "attempt_sha256"),
        ("receipt", "receipt_sha256"),
    ],
)
def test_bad_document_attempt_or_receipt_seal_is_rejected(
    tmp_path,
    level,
    seal_field,
):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration)
    path = tmp_path / f"{preregistration['tasks'][0]['task_id']}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if level == "document":
        document["checkpoint"]["updated_at"] = "tampered"
    elif level == "attempt":
        document["attempts"][0]["tampered"] = True
        _resign_document(document)
    else:
        receipt = document["attempts"][0]["phoenix"]["receipt"]
        receipt["tampered"] = True
        _resign_attempt(document["attempts"][0])
        _resign_document(document)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(analysis.TaskAnalysisError, match=seal_field):
        _summarize(tmp_path, preregistration)


@pytest.mark.parametrize("case", ["four_attempts", "infrastructure_invalid"])
def test_exactly_five_infrastructure_valid_attempts_are_required(tmp_path, case):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration)
    path = tmp_path / f"{preregistration['tasks'][0]['task_id']}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if case == "four_attempts":
        document["attempts"].pop()
        expected = "exactly 5 paired attempts"
    else:
        document["attempts"][0]["infrastructure_valid"] = False
        _resign_attempt(document["attempts"][0])
        expected = "infrastructure_valid"
    _resign_document(document)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(analysis.TaskAnalysisError, match=expected):
        _summarize(tmp_path, preregistration)


def test_preregistration_itself_must_be_sealed(tmp_path):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration)
    preregistration["model"]["requested"] = "tampered-model"

    with pytest.raises(analysis.TaskAnalysisError, match="preregistration_sha256"):
        _summarize(tmp_path, preregistration)


def test_inference_parameters_must_match_sealed_preregistration(tmp_path):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration=preregistration)

    with pytest.raises(
        analysis.TaskAnalysisError,
        match="confidence does not match",
    ):
        analysis.summarize_root(
            tmp_path,
            preregistration=preregistration,
            bootstrap_samples=BOOTSTRAP_SAMPLES,
            confidence=0.90,
            seed=BOOTSTRAP_SEED,
        )


def test_cli_accepts_preregistration_path_and_writes_formal_outputs(
    tmp_path,
    capsys,
):
    preregistration = _preregistration()
    _write_suite(tmp_path, preregistration)
    preregistration_path = tmp_path / "preregistration.json"
    preregistration_path.write_text(
        json.dumps(preregistration, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    exit_code = analysis.main(
        [
            str(tmp_path),
            "--preregistration",
            str(preregistration_path),
            "--bootstrap-samples",
            str(BOOTSTRAP_SAMPLES),
            "--seed",
            str(BOOTSTRAP_SEED),
        ]
    )

    assert exit_code == 0
    json_path = tmp_path / analysis.DEFAULT_JSON_NAME
    markdown_path = tmp_path / analysis.DEFAULT_MARKDOWN_NAME
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["formal_analysis"] is True
    assert payload["winner"] == "phoenix"
    assert (
        payload["policy"]["informative_coverage"]["equivalence"]["minimum_suite_rate"]
        == 0.9
    )
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# NON-RANKABLE")
    assert "Conditional both-reliable quality (descriptive only)" in markdown
    assert "Experiment binding:" in markdown
    output = capsys.readouterr().out
    assert str(json_path) in output
    assert str(markdown_path) in output
