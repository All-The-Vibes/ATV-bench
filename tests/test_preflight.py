"""TDD for the shared preflight checks (DX-4): doctor and runner both reuse these."""
from __future__ import annotations

from atv_bench.preflight import (
    CheckResult,
    check_cli_on_path,
    check_docker,
    check_codeclash,
    aggregate,
)


def test_check_result_shape():
    r = CheckResult(name="docker", ok=True, detail="running")
    assert r.ok is True
    assert r.name == "docker"


def test_check_cli_on_path_reports_missing():
    r = check_cli_on_path("definitely-not-a-real-cli-xyz")
    assert r.ok is False
    assert "not found" in r.detail.lower()


def test_check_cli_on_path_reports_present():
    r = check_cli_on_path("git")  # git is always present in CI
    assert r.ok is True


def test_aggregate_reports_all_failures_at_once():
    # DX-4: aggregate ALL failures, not one-at-a-time.
    checks = [
        CheckResult("a", True, "ok"),
        CheckResult("b", False, "missing b"),
        CheckResult("c", False, "missing c"),
    ]
    ok, failures = aggregate(checks)
    assert ok is False
    assert len(failures) == 2
    assert {f.name for f in failures} == {"b", "c"}


def test_aggregate_all_ok():
    ok, failures = aggregate([CheckResult("a", True, "ok")])
    assert ok is True
    assert failures == []


def test_check_docker_returns_a_result():
    r = check_docker()
    assert r.name == "docker"
    assert isinstance(r.ok, bool)


def test_check_codeclash_returns_a_result():
    r = check_codeclash()
    assert r.name == "codeclash"
    assert isinstance(r.ok, bool)
