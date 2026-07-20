"""Supply-chain tripwires for every executable GitHub Action."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import yaml


WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"
USE_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^#\s]+)", re.MULTILINE)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

APPROVED_PINS = {
    "actions/checkout": "34e114876b0b11c390a56381ad16ebd13914f8d5",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/upload-pages-artifact": "56afc609e74202658d3ffba0e8f6dda462b719fa",
    "actions/deploy-pages": "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
}

PRIVILEGED_WORKFLOWS = (WORKFLOWS / "league-deploy.yml",)

HASH_LOCKED_REQUIREMENTS = {
    "attrs==26.1.0": "c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309",
    "jsonschema==4.26.0": "d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce",
    "jsonschema-specifications==2025.9.1": (
        "98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe"
    ),
    "referencing==0.37.0": "381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231",
    "rpds-py==2026.6.3": "ecabd69db66de867690f9797f2f8fa27ba501bbc24540cbdbdc649cd15888ba6",
    "typing-extensions==4.16.0": (
        "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8"
    ),
}


def _walk_uses(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "uses" and isinstance(item, str):
                yield item
            yield from _walk_uses(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_uses(item)


def _external_uses() -> Iterable[tuple[Path, str]]:
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        for value in USE_RE.findall(text):
            if value.startswith("./") or value.startswith("docker://"):
                continue
            yield workflow, value


def test_uses_regex_covers_dash_prefixed_yaml_steps_and_every_uses_node():
    assert USE_RE.findall("- uses: actions/checkout@" + "a" * 40) == [
        "actions/checkout@" + "a" * 40
    ]
    for workflow in WORKFLOWS.glob("*.y*ml"):
        text_values = sorted(USE_RE.findall(workflow.read_text(encoding="utf-8")))
        yaml_values = sorted(_walk_uses(yaml.safe_load(workflow.read_text(encoding="utf-8"))))
        assert text_values == yaml_values, f"{workflow.name}: text scanner missed a uses node"


def test_every_action_is_pinned_to_an_approved_full_sha():
    found: set[str] = set()
    for workflow, value in _external_uses():
        assert "@" in value, f"{workflow.name}: action has no ref: {value}"
        action, ref = value.rsplit("@", 1)
        assert FULL_SHA_RE.fullmatch(ref), (
            f"{workflow.name}: {value} uses a movable tag or branch"
        )
        assert action in APPROVED_PINS, (
            f"{workflow.name}: {action} is executable code without an approved pin"
        )
        assert ref == APPROVED_PINS[action], (
            f"{workflow.name}: {action} changed without updating the reviewed pin"
        )
        found.add(action)
    assert found == set(APPROVED_PINS)


def test_privileged_jobs_install_only_hash_locked_binary_wheels():
    for workflow in PRIVILEGED_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert "--require-hashes" in text
        assert "--only-binary=:all:" in text
        assert "--no-deps" in text
        assert "pip install -e" not in "\n".join(
            line.split("#", 1)[0] for line in text.splitlines()
        )
        for requirement, digest in HASH_LOCKED_REQUIREMENTS.items():
            assert requirement in text
            assert f"--hash=sha256:{digest}" in text
