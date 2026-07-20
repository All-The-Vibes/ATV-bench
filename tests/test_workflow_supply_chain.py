"""Supply-chain tripwires for every executable third-party GitHub Action."""
from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"
USE_RE = re.compile(r"^\s*uses:\s*([^#\s]+)", re.MULTILINE)
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

APPROVED_PINS = {
    "actions/checkout": "34e114876b0b11c390a56381ad16ebd13914f8d5",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/upload-pages-artifact": "56afc609e74202658d3ffba0e8f6dda462b719fa",
    "actions/deploy-pages": "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
}

PRIVILEGED_WORKFLOWS = (
    WORKFLOWS / "league-publish.yml",
    WORKFLOWS / "league-deploy.yml",
)

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


def _external_uses():
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        for value in USE_RE.findall(text):
            if value.startswith("./") or value.startswith("docker://"):
                continue
            yield workflow, value


def test_every_third_party_action_is_pinned_to_an_approved_full_sha():
    found = set()
    for workflow, value in _external_uses():
        assert "@" in value, f"{workflow.name}: action has no ref: {value}"
        action, ref = value.rsplit("@", 1)
        assert FULL_SHA_RE.fullmatch(ref), (
            f"{workflow.name}: {value} uses a movable tag or branch, not a full commit SHA"
        )
        assert action in APPROVED_PINS, (
            f"{workflow.name}: {action} is executable third-party code without an approved pin"
        )
        assert ref == APPROVED_PINS[action], (
            f"{workflow.name}: {action} changed without updating the reviewed pin"
        )
        found.add(action)

    assert found == set(APPROVED_PINS)


def test_privileged_jobs_install_only_exact_hash_locked_binary_wheels():
    for workflow in PRIVILEGED_WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        assert 'jsonschema>=4.0' not in text
        assert "--require-hashes" in text
        assert "--only-binary=:all:" in text
        assert "--no-deps" in text
        assert "pip install -e" not in "\n".join(
            line.split("#", 1)[0] for line in text.splitlines()
        )
        for requirement, digest in HASH_LOCKED_REQUIREMENTS.items():
            assert requirement in text, f"{workflow.name} is missing {requirement}"
            assert f"--hash=sha256:{digest}" in text, (
                f"{workflow.name} is missing the reviewed hash for {requirement}"
            )


def test_privileged_dependency_lock_is_identical_in_publish_and_deploy():
    def lock_lines(path: Path) -> set[str]:
        text = path.read_text(encoding="utf-8")
        start = text.index("cat > \"$RUNNER_TEMP/league-requirements.txt\"")
        end = text.index("\n          EOF", start)
        block = text[start:end]
        return {
            line.strip().removesuffix("\\").strip()
            for line in block.splitlines()
            if "==" in line or "--hash=sha256:" in line
        }

    publish, deploy = (lock_lines(path) for path in PRIVILEGED_WORKFLOWS)
    assert publish == deploy
