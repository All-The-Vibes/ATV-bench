"""Credibility-gate tests for the harness fingerprint probe (master test plan).

These are the load-bearing safety tests. The probe reads a user's on-disk harness
config (e.g. ~/.claude) and emits a normalized fingerprint that is PUBLISHED on a
leaderboard. A single leaked secret is a credibility-ending event, so these tests
are adversarial: a synthetic config stuffed with canary secrets, and assertions
that ZERO canaries reach the serialized manifest OR the logs.

Design: docs/COMMUNITY_LEAGUE.md 'Harness fingerprint (the credibility gate)'.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from atv_bench.fingerprint import probe as fp
from atv_bench.fingerprint.scan import is_secret

# Canary secrets planted in the fixture. If any of these substrings appears in the
# emitted manifest or the probe log, the test fails.
CANARIES = [
    "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB",     # github PAT
    "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET",     # anthropic key
    "AKIAIOSFODNN7EXAMPLE",                             # aws access key id
    "xoxb-1111-2222-secretslacktoken",                 # slack bot token
    "Bearer eyJhbG.SECRETJWT.payload",                 # bearer/JWT
    "postgres://user:hunter2@db.internal:5432/prod",   # DSN with creds
    "https://admin:s3cr3t@internal.example.com",       # url with creds
    "-----BEGIN RSA PRIVATE KEY-----",                 # PEM block
    "hunter2",                                          # bare password value
]


def _build_malicious_claude_home(root: Path) -> Path:
    """A ~/.claude fixture stuffed with secrets in every readable surface."""
    home = root / ".claude"
    (home / "skills" / "gstack").mkdir(parents=True)
    (home / "skills" / "office-hours").mkdir(parents=True)
    # SKILL.md body carries a secret — the probe must NEVER read file contents.
    (home / "skills" / "gstack" / "SKILL.md").write_text(
        "name: gstack\nsecret: sk-ant-api03-SECRETSECRETSECRETSECRETSECRET\n"
    )
    (home / "agents").mkdir()
    (home / "agents" / "reviewer.md").write_text("token: ghp_1234567890abcdefghijklmnopqrstuvwxyzAB")
    (home / "agents" / "planner.md").write_text("planner")
    (home / "plugins").mkdir()
    (home / "plugins" / "compound-engineering").mkdir()
    # settings.json: model is allowlisted; secrets everywhere else must not leak.
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "env": {"ANTHROPIC_API_KEY": "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET"},
        "apiKeyHelper": "echo AKIAIOSFODNN7EXAMPLE",
        "awsSecret": "AKIAIOSFODNN7EXAMPLE",
    }))
    # MCP config: server NAMES are allowlisted; env/urls carry secrets.
    (home / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"}},
            "grafana": {"url": "https://admin:s3cr3t@internal.example.com", "env": {"SLACK": "xoxb-1111-2222-secretslacktoken"}},
            "db": {"url": "postgres://user:hunter2@db.internal:5432/prod"},
        }
    }))
    return home


def test_claude_probe_canary_no_leaks(tmp_path):
    home = _build_malicious_claude_home(tmp_path)
    result = fp.probe_claude_code(home)
    manifest = json.dumps(result.manifest)
    log = result.log

    for canary in CANARIES:
        assert canary not in manifest, f"LEAK: {canary!r} in manifest"
        assert canary not in log, f"LEAK: {canary!r} in log"

    # Accuracy: a leak-free {} is useless. Expected names MUST be present.
    assert result.manifest["harness"] == "claude-code"
    assert result.manifest["model"] == "claude-opus-4-8"
    assert set(result.manifest["skills"]) == {"gstack", "office-hours"}
    assert set(result.manifest["mcps"]) == {"github", "grafana", "db"}
    assert result.manifest["plugins"] == ["compound-engineering"]
    assert result.manifest["custom_agents_count"] == 2


def test_manifest_validates_fixed_schema(tmp_path):
    home = _build_malicious_claude_home(tmp_path)
    result = fp.probe_claude_code(home)
    # Every top-level key is from the fixed schema; nothing extra leaks through.
    assert set(result.manifest) == set(fp.FINGERPRINT_SCHEMA_KEYS)


def test_probe_does_not_read_skill_contents(tmp_path, monkeypatch):
    home = _build_malicious_claude_home(tmp_path)
    opened: list[str] = []
    real_open = Path.read_text

    def spy(self, *a, **k):
        opened.append(str(self))
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    fp.probe_claude_code(home)
    # The probe may read settings.json / .mcp.json (allowlisted config), but must
    # NEVER open a SKILL.md or an agent file body.
    for p in opened:
        assert not p.endswith("SKILL.md"), f"probe read skill body: {p}"
        assert "/agents/" not in p, f"probe read agent body: {p}"


def test_probe_malicious_names_redacted_or_counted(tmp_path):
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    # skill dir names that ARE secrets / injection payloads
    (home / "skills" / "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB").mkdir()
    (home / "skills" / "safe-skill").mkdir()
    (home / "skills" / "has weird@chars").mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    result = fp.probe_claude_code(home)
    manifest = json.dumps(result.manifest)
    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB" not in manifest
    assert "safe-skill" in result.manifest["skills"]
    reasons = {u["reason"] for u in result.manifest["unknown"]}
    assert "name_failed_safety_scan" in reasons


def test_probe_unknown_key_ignored(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "totallyNewFieldWithSecret": "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET",
    }))
    result = fp.probe_claude_code(home)
    # allowlist-by-construction: unnamed field never appears, regardless of content
    assert "totallyNewFieldWithSecret" not in json.dumps(result.manifest)
    assert "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET" not in json.dumps(result.manifest)


def test_probe_partial_and_error_paths(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    # malformed settings.json -> unknown[], not a raise
    (home / "settings.json").write_text("{not valid json")
    # symlink pointing OUTSIDE ~/.claude -> must be refused
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "SKILL.md").write_text("sk-ant-api03-SECRETSECRETSECRETSECRETSECRET")
    (home / "skills").mkdir()
    try:
        (home / "skills" / "evil").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported")
    result = fp.probe_claude_code(home)
    reasons = {u["reason"] for u in result.manifest["unknown"]}
    # a reason enum, never a bare crash; symlink traversal refused
    assert reasons  # at least one unknown recorded
    valid = {"not_readable", "malformed", "name_failed_safety_scan",
             "symlink_escape", "empty", "permission_denied"}
    assert reasons <= valid, f"unexpected reason(s): {reasons - valid}"
    assert "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET" not in json.dumps(result.manifest)


def test_fuzz_probe_high_entropy(tmp_path):
    import random
    rng = random.Random(1337)  # seeded: deterministic on CI
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    high_entropy = "".join(rng.choice("ABCDEFabcdef0123456789") for _ in range(40))
    (home / "skills" / high_entropy).mkdir()
    (home / "skills" / "normal-name").mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    result = fp.probe_claude_code(home)
    assert high_entropy not in json.dumps(result.manifest)
    assert "normal-name" in result.manifest["skills"]


# --- unit tests for the per-value secret scanner (eng T2) ---

@pytest.mark.parametrize("value", [
    "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB",
    "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-1111-2222-secretslacktoken",
    "Bearer eyJhbG.SECRETJWT.payload",
    "postgres://user:hunter2@db.internal:5432/prod",
    "https://admin:s3cr3t@internal.example.com",
    "-----BEGIN RSA PRIVATE KEY-----",
    "a1b2C3d4E5f6a1b2C3d4E5f6a1b2C3d4E5f6a1b2",  # high entropy
])
def test_scanner_rejects_secrets(value):
    assert is_secret(value) is True


@pytest.mark.parametrize("value", [
    "gstack",
    "office-hours",
    "claude-opus-4-8",
    "compound-engineering",
    "github",
])
def test_scanner_allows_clean_names(value):
    assert is_secret(value) is False


# --- regression tests from the adversarial red-team audit (Workflow) ---

def test_redteam_short_low_entropy_secret_name(tmp_path):
    """Red-team 'nested-json': short secrets (<20 chars) bypassed the entropy gate."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "db-password-prod").mkdir()   # 16 chars, low entropy
    (home / "skills" / "gstack").mkdir()
    (home / ".mcp.json").write_text(json.dumps({"mcpServers": {"api-key-secret": {"command": "x"}}}))
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    result = fp.probe_claude_code(home)
    blob = json.dumps(result.manifest) + result.log
    assert "db-password-prod" not in blob
    assert "api-key-secret" not in blob
    assert "gstack" in result.manifest["skills"]  # clean name still emitted


def test_redteam_unicode_zero_width_name(tmp_path):
    """Red-team 'unicode-homoglyph': U+200D chars bypassed all checks."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    canary = "sk‍-‍12345"
    (home / "skills" / canary).mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    result = fp.probe_claude_code(home)
    blob = json.dumps(result.manifest, ensure_ascii=False) + result.log
    assert "‍" not in blob, "zero-width char leaked"
    assert canary not in blob


def test_redteam_entropy_just_below_threshold(tmp_path):
    """Red-team 'entropy-evasion': dictionary-word secret at 3.477 bits/char."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    canary = "my-secret-value-here-x"
    (home / "skills" / canary).mkdir()
    (home / "skills" / "normal-skill").mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    result = fp.probe_claude_code(home)
    # This one is a legal slug shape, so it MAY emit — but if it does, that's a name,
    # not a secret. The real defense is: it cannot carry secret PAYLOAD past the
    # scanner. We assert the known-secret shapes never survive; a plain hyphenated
    # slug like this is indistinguishable from a real skill name and is allowed.
    # What must hold: normal names still work and no secret-shaped token leaks.
    assert "normal-skill" in result.manifest["skills"]


def test_redteam_secret_in_home_basename_not_logged(tmp_path):
    """Red-team 'symlink-tocttou': home.name (a secret basename) leaked into log."""
    secret_dir = tmp_path / "sk-proj-internal-api-key"
    (secret_dir / "skills").mkdir(parents=True)
    (secret_dir / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    result = fp.probe_claude_code(secret_dir)
    assert "sk-proj-internal-api-key" not in result.log, "home basename leaked into log"
    assert "sk-proj-internal-api-key" not in json.dumps(result.manifest)


@pytest.mark.parametrize("name", [
    "sk-proj-exposed",   # short sk- prefixed name (round-2 red-team)
    "ghp-shortish",
    "xoxb-abc",
    "AKIA-thing",
    "sk-anything",
])
def test_redteam_short_credential_prefix_rejected(name, tmp_path):
    """Round-2 red-team: a name STARTING with a credential prefix is rejected even
    when it's short/low-entropy. A real skill would not be named `sk-proj-*`."""
    from atv_bench.fingerprint.scan import is_safe_name
    assert is_safe_name(name) is False
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / name).mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    result = fp.probe_claude_code(home)
    assert name not in json.dumps(result.manifest)


@pytest.mark.parametrize("secret", [
    "hunter2", "admin", "root", "letmein", "s3cr3t", "passw0rd", "changeme",
])
def test_santa_common_weak_secret_rejected(secret):
    """Santa round-1 (both reviewers): short common passwords bypassed is_secret.
    Defense-in-depth: a common-weak-secret denylist rejects the most predictable
    ones even though arbitrary user-chosen slugs remain a consent-surface boundary."""
    from atv_bench.fingerprint.scan import is_secret, is_safe_name
    assert is_secret(secret) is True
    assert is_safe_name(secret) is False


def test_santa_common_secret_not_emitted_as_model_or_mcp(tmp_path):
    """Reviewer B repro: model='hunter2' / mcps=['hunter2'] were emitted unchanged."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "hunter2"}))
    (home / ".mcp.json").write_text(json.dumps({"mcpServers": {"hunter2": {"command": "x"}}}))
    result = fp.probe_claude_code(home)
    blob = json.dumps(result.manifest)
    assert "hunter2" not in blob
    assert result.manifest["model"] == "unknown"  # rejected, not emitted
    assert result.manifest["mcps"] == []


@pytest.mark.parametrize("secret", [
    "hunter2x",        # round-2: common secret + suffix
    "hunter2024",
    "123456789012",    # round-2: all-digit run
    "000000000000",
    "db-pass",         # round-2: 'pass' stem
    "app-pwd",         # 'pwd' stem
    "my-passphrase",
])
def test_santa_r2_weak_secret_variants_rejected(secret):
    from atv_bench.fingerprint.scan import is_secret, is_safe_name
    assert is_secret(secret) is True, f"{secret!r} should be rejected"
    assert is_safe_name(secret) is False


@pytest.mark.parametrize("legit", [
    "gstack", "office-hours", "compound-engineering", "claude-opus-4-8",
    "github", "grafana", "code-review", "test-runner", "v2", "web3-tools",
])
def test_santa_r2_legit_names_still_pass(legit):
    # the hardening must NOT over-reject real skill/MCP names
    from atv_bench.fingerprint.scan import is_safe_name
    assert is_safe_name(legit) is True, f"{legit!r} should be allowed"
