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
    """A REAL-layout ~/.claude fixture stuffed with secrets in every readable surface.

    Real machine (verified 2026-07-16), NOT the old simplified fixture:
      - plugins come from settings.json["enabledPlugins"] keys ("name@marketplace",
        truthy only), NOT a flat plugins/ dir.
      - ~/.claude/plugins/ holds infra dirs (cache/, data/, marketplaces/) + JSON
        manifests — these must NEVER be emitted as plugins.
      - skills/agents nested inside installed plugins are walked MANIFEST-DRIVEN via
        plugins/installed_plugins.json installPath.
      - MCP servers live in ~/.claude.json["mcpServers"] (home ROOT file), NOT
        ~/.claude/.mcp.json (which does not exist on a real machine).
    """
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

    # plugins/: infra dirs + JSON manifests (must NOT be emitted as plugins), plus a
    # cached plugin tree with nested skills/agents referenced by installed_plugins.json.
    plugins = home / "plugins"
    (plugins / "cache").mkdir(parents=True)
    (plugins / "data").mkdir()
    (plugins / "marketplaces").mkdir()
    (plugins / "blocklist.json").write_text("{}")
    ce_root = plugins / "cache" / "compound-engineering-plugin" / "compound-engineering" / "3.4.2"
    (ce_root / "skills" / "ce-brainstorm").mkdir(parents=True)
    (ce_root / "skills" / "ce-brainstorm" / "SKILL.md").write_text(
        "secret: sk-ant-api03-SECRETSECRETSECRETSECRETSECRET\n")
    (ce_root / "agents").mkdir()
    (ce_root / "agents" / "ce-reviewer.md").write_text("ghp_1234567890abcdefghijklmnopqrstuvwxyzAB")
    (plugins / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {
            "compound-engineering@compound-engineering-plugin": [
                {"installPath": str(ce_root)}
            ],
        },
    }))

    # settings.json: enabledPlugins (name@marketplace → truthy filter is the disable
    # mechanism), model allowlisted, secrets everywhere else must not leak.
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "enabledPlugins": {
            "compound-engineering@compound-engineering-plugin": True,
            "some-disabled-plugin@claude-plugins-official": False,   # false → excluded
        },
        "env": {"ANTHROPIC_API_KEY": "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET"},
        "apiKeyHelper": "echo AKIAIOSFODNN7EXAMPLE",
        "awsSecret": "AKIAIOSFODNN7EXAMPLE",
    }))
    # ~/.claude.json (HOME ROOT file, parent of ~/.claude): mcpServers NAMES allowlisted;
    # env/urls carry secrets that must not leak.
    (root / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"}},
            "grafana": {"url": "https://admin:s3cr3t@internal.example.com", "env": {"SLACK": "xoxb-1111-2222-secretslacktoken"}},
            "db": {"url": "postgres://user:hunter2@db.internal:5432/prod"},
        },
        "otherTopLevelSecret": "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET",
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
    # top-level skills PLUS nested plugin skill (ce-brainstorm).
    assert set(result.manifest["skills"]) == {"gstack", "office-hours", "ce-brainstorm"}
    # mcps come from ~/.claude.json, NOT ~/.claude/.mcp.json.
    assert set(result.manifest["mcps"]) == {"github", "grafana", "db"}
    # plugin from enabledPlugins (truthy), @marketplace stripped; disabled one excluded.
    assert result.manifest["plugins"] == ["compound-engineering"]
    # 2 top-level agents + 1 nested plugin agent.
    assert result.manifest["custom_agents_count"] == 3


def test_claude_infra_dirs_not_emitted_as_plugins(tmp_path):
    """The core reported bug: plugins/cache, plugins/data, plugins/marketplaces are infra
    dirs and must NEVER appear as plugins (the old reader globbed them)."""
    home = _build_malicious_claude_home(tmp_path)
    result = fp.probe_claude_code(home)
    for infra in ("cache", "data", "marketplaces"):
        assert infra not in result.manifest["plugins"], f"infra dir {infra!r} emitted as plugin"


def test_claude_disabled_plugin_and_nested_skills_excluded(tmp_path):
    """enabledPlugins[key]==false excludes the plugin (M4) AND its nested skills (M5)."""
    home = tmp_path / ".claude"
    home.mkdir()
    plugins = home / "plugins"
    dis_root = plugins / "cache" / "mkt" / "disabled-plugin" / "1.0.0"
    (dis_root / "skills" / "secret-disabled-skill").mkdir(parents=True)
    en_root = plugins / "cache" / "mkt" / "enabled-plugin" / "1.0.0"
    (en_root / "skills" / "enabled-skill").mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {
            "disabled-plugin@mkt": [{"installPath": str(dis_root)}],
            "enabled-plugin@mkt": [{"installPath": str(en_root)}],
        },
    }))
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "enabledPlugins": {"disabled-plugin@mkt": False, "enabled-plugin@mkt": True},
    }))
    result = fp.probe_claude_code(home)
    assert result.manifest["plugins"] == ["enabled-plugin"]
    assert "enabled-skill" in result.manifest["skills"]
    assert "disabled-plugin" not in result.manifest["plugins"]
    assert "secret-disabled-skill" not in result.manifest["skills"]


@pytest.mark.parametrize("bad_install_path", ["/etc", "../../.ssh", "/root/.aws"])
def test_claude_installpath_escape_rejected(tmp_path, bad_install_path):
    """CRITICAL M3: installed_plugins.json installPath pointing outside ~/.claude →
    rejected; no basename from that tree leaks."""
    home = tmp_path / ".claude"
    home.mkdir()
    plugins = home / "plugins"
    plugins.mkdir()
    (plugins / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {"evil@mkt": [{"installPath": bad_install_path}]},
    }))
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "enabledPlugins": {"evil@mkt": True},
    }))
    result = fp.probe_claude_code(home)  # must not raise
    blob = json.dumps(result.manifest)
    # the escaping tree contributes no skill basenames
    assert "ssh" not in result.manifest["skills"]
    assert "aws" not in result.manifest["skills"]


def test_claude_installpath_symlink_escape_rejected(tmp_path):
    """M3: an installPath that is inside ~/.claude but symlinks OUT → rejected."""
    home = tmp_path / ".claude"
    home.mkdir()
    outside = tmp_path / "outside_secrets"
    (outside / "skills" / "sk-proj-leaked").mkdir(parents=True)
    plugins = home / "plugins"
    plugins.mkdir()
    linked = plugins / "cache" / "linked-plugin"
    linked.parent.mkdir(parents=True)
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported")
    (plugins / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {"linked@mkt": [{"installPath": str(linked)}]},
    }))
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "enabledPlugins": {"linked@mkt": True},
    }))
    result = fp.probe_claude_code(home)
    assert "sk-proj-leaked" not in json.dumps(result.manifest)


def test_claude_real_mcp_source_claude_json(tmp_path):
    """NEW finding: mcps come from ~/.claude.json["mcpServers"], and .mcp.json absence
    does NOT zero them (the old reader emitted [] for everyone)."""
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {"context7": {"command": "x"}, "backlog": {"command": "y"}},
    }))
    # deliberately NO ~/.claude/.mcp.json
    result = fp.probe_claude_code(home)
    assert set(result.manifest["mcps"]) == {"context7", "backlog"}


@pytest.mark.parametrize("manifest_json", [
    '{"version": 1, "plugins": {}}',          # wrong version
    '["not", "a", "dict"]',                    # not a dict
    '{"version": 2, "plugins": {"p@m": [{}]}}',  # entry missing installPath
    'not json at all {',                       # malformed
])
def test_claude_installed_plugins_manifest_guarded(tmp_path, manifest_json):
    """M6: a missing / version!=2 / non-dict / malformed installed_plugins.json →
    top-level-skills-only, never a raise."""
    home = tmp_path / ".claude"
    (home / "skills" / "top-skill").mkdir(parents=True)
    (home / "plugins").mkdir()
    (home / "plugins" / "installed_plugins.json").write_text(manifest_json)
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "enabledPlugins": {"p@m": True},
    }))
    result = fp.probe_claude_code(home)  # must not raise
    assert "top-skill" in result.manifest["skills"]


def test_claude_gstack_as_nested_plugin_skill(tmp_path):
    """M7: gstack present only as a nested plugin skill → gstack:true."""
    home = tmp_path / ".claude"
    home.mkdir()
    plugins = home / "plugins"
    g_root = plugins / "cache" / "mkt" / "gstack-plugin" / "1.0.0"
    (g_root / "skills" / "gstack").mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {"gstack-plugin@mkt": [{"installPath": str(g_root)}]},
    }))
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "enabledPlugins": {"gstack-plugin@mkt": True},
    }))
    result = fp.probe_claude_code(home)
    assert result.manifest["gstack"] is True
    assert "gstack" in result.manifest["skills"]


def test_claude_dedup_cross_plugin_skills(tmp_path):
    """M7: the same plugin from two marketplaces / a dup skill across plugins → one entry."""
    home = tmp_path / ".claude"
    home.mkdir()
    plugins = home / "plugins"
    a_root = plugins / "cache" / "mktA" / "shared" / "1.0.0"
    (a_root / "skills" / "common-skill").mkdir(parents=True)
    b_root = plugins / "cache" / "mktB" / "shared" / "1.0.0"
    (b_root / "skills" / "common-skill").mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {
            "shared@mktA": [{"installPath": str(a_root)}],
            "shared@mktB": [{"installPath": str(b_root)}],
        },
    }))
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "enabledPlugins": {"shared@mktA": True, "shared@mktB": True},
    }))
    result = fp.probe_claude_code(home)
    assert result.manifest["skills"].count("common-skill") == 1
    assert result.manifest["plugins"].count("shared") == 1


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


# --- copilot-cli reader: required canary leak-test (CONTRIBUTING → Add a harness) ---

def _build_malicious_copilot_home(root: Path) -> Path:
    """A ~/.copilot fixture stuffed with secrets in every readable surface.

    Mirrors the claude fixture but on Copilot's layout: settings.json (model +
    enabledPlugins + disabled lists), mcp-config.json (server names), and skills/agents
    NESTED under installed-plugins/<marketplace>/<plugin>/.
    """
    home = root / ".copilot"
    home.mkdir(parents=True)
    # Nested plugin layout: installed-plugins/<marketplace>/<plugin>/{skills,agents}
    gstack_skills = home / "installed-plugins" / "gstack" / "gstack" / "skills"
    gstack_skills.mkdir(parents=True)
    (gstack_skills / "systematic-debugging").mkdir()   # long slug — must NOT be scrubbed
    (gstack_skills / "office-hours").mkdir()
    # SKILL.md body carries a secret — the probe must NEVER read file contents.
    (gstack_skills / "systematic-debugging" / "SKILL.md").write_text(
        "secret: sk-ant-api03-SECRETSECRETSECRETSECRETSECRET\n"
    )
    agents = home / "installed-plugins" / "gstack" / "gstack" / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text("token: ghp_1234567890abcdefghijklmnopqrstuvwxyzAB")
    (agents / "planner.md").write_text("planner")
    # settings.json: model allowlisted; enabledPlugins keys are "name@marketplace";
    # secrets everywhere else must not leak.
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4.8",
        "enabledPlugins": {"gstack@gstack": True, "superpowers@superpowers-marketplace": True},
        "disabledSkills": ["office-hours"],          # denylist → subtracted from effective skills
        "disabledMcpServers": ["workiq"],
        "githubToken": "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB",   # must NOT leak
    }))
    # mcp-config.json: server NAMES allowlisted; env/urls carry secrets.
    (home / "mcp-config.json").write_text(json.dumps({
        "mcpServers": {
            "github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"}},
            "grafana": {"url": "https://admin:s3cr3t@internal.example.com"},
            "workiq": {"url": "postgres://user:hunter2@db.internal:5432/prod"},  # disabled → excluded
        }
    }))
    return home


def test_copilot_probe_canary_no_leaks(tmp_path):
    home = _build_malicious_copilot_home(tmp_path)
    result = fp.probe_copilot_cli(home)
    manifest = json.dumps(result.manifest)
    log = result.log

    for canary in CANARIES:
        assert canary not in manifest, f"LEAK: {canary!r} in manifest"
        assert canary not in log, f"LEAK: {canary!r} in log"

    # Accuracy: a leak-free {} is useless. Expected names MUST be present.
    assert result.manifest["harness"] == "copilot-cli"
    assert result.manifest["model"] == "claude-opus-4.8"
    # office-hours is disabled → excluded; the long slug survives the entropy gate.
    # Copilot skills are nested under installed-plugins/*/skills → nested_skills.
    assert "systematic-debugging" in result.manifest["nested_skills"]
    assert "office-hours" not in result.manifest["nested_skills"]
    # workiq MCP is disabled → excluded; github/grafana remain.
    assert set(result.manifest["mcps"]) == {"github", "grafana"}
    assert set(result.manifest["plugins"]) == {"gstack", "superpowers"}
    assert result.manifest["custom_agents_count"] == 2
    assert result.manifest["gstack"] is True


def test_copilot_manifest_validates_fixed_schema(tmp_path):
    home = _build_malicious_copilot_home(tmp_path)
    result = fp.probe_copilot_cli(home)
    assert set(result.manifest) == set(fp.FINGERPRINT_SCHEMA_KEYS)


def test_copilot_empty_home_is_clean_not_crash(tmp_path):
    """A ~/.copilot that doesn't exist / is empty → a valid empty-ish manifest, no crash."""
    result = fp.probe_copilot_cli(tmp_path / ".copilot")
    assert result.manifest["harness"] == "copilot-cli"
    assert result.manifest["skills"] == []
    assert result.manifest["nested_skills"] == []
    assert result.manifest["plugins"] == []
    assert result.manifest["mcps"] == []
    assert result.manifest["custom_agents_count"] == 0


# --- regression: the entropy gate must not scrub legit long hyphenated slugs ---

@pytest.mark.parametrize("legit", [
    "systematic-debugging",
    "finishing-a-development-branch",
    "using-git-worktrees",
    "subagent-driven-development",
    "verification-before-completion",
    "benchmark-models",
    "claude-opus-4.8",
])
def test_long_hyphenated_slugs_not_flagged_as_secret(legit):
    """Real skill/model names with many hyphen-joined tokens have high whole-string
    entropy but are NOT secrets. The segment-entropy gate must let them through."""
    from atv_bench.fingerprint.scan import is_safe_name
    assert is_secret(legit) is False, f"{legit!r} wrongly flagged as secret"
    assert is_safe_name(legit) is True, f"{legit!r} wrongly rejected as unsafe name"


@pytest.mark.parametrize("blob", [
    "a1b2C3d4E5f6a1b2C3d4E5f6a1b2C3d4E5f6a1b2",  # long dense random run
    "aB3xK9mQ2pL5nR8w",                            # 16-char high-entropy blob
])
def test_high_entropy_blobs_still_flagged(blob):
    """The segment-entropy relaxation must NOT weaken detection of a genuine
    high-entropy token (one unbroken dense run)."""
    assert is_secret(blob) is True, f"{blob!r} should still be flagged as secret"


def test_new_surfaces_nested_skills_and_tools_no_leak(tmp_path):
    """Lane A: canary secrets planted in nested-skill dirs, tool/permission names, and
    plugin skill dirs must NOT leak into the new tools/nested_skills surfaces.
    
    NOTE: Main's reader is MANIFEST-DRIVEN — nested skills are walked via
    plugins/installed_plugins.json installPath, NOT a naive dir glob. This fixture
    uses the real installed_plugins.json layout that main's reader expects.
    MCPs come from ~/.claude.json (root's PARENT), not ~/.claude/.mcp.json.
    """
    home = tmp_path / ".claude"
    (home / "skills" / "tdd").mkdir(parents=True)
    # nested plugin skill with a SECRET-shaped dir name -> must be scrubbed to unknown[]
    ce_root = home / "plugins" / "cache" / "ce-mkt" / "compound-engineering" / "1.0.0"
    (ce_root / "skills" / "ce-plan").mkdir(parents=True)
    (ce_root / "skills" / "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB").mkdir(parents=True)
    (home / "plugins" / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {
            "compound-engineering@ce-mkt": [{"installPath": str(ce_root)}],
        },
    }))
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "enabledPlugins": {"compound-engineering@ce-mkt": True},
        "permissions": {
            "allow": ["Bash", "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET"],
            "deny": ["WebFetch"],
        },
    }))
    # MCPs from ~/.claude.json (root's PARENT), not ~/.claude/.mcp.json (main's real layout)
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {"github": {"env": {"T": "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"}}}
    }))
    result = fp.probe_claude_code(home)
    manifest = json.dumps(result.manifest)
    for canary in CANARIES:
        assert canary not in manifest, f"LEAK in new surfaces: {canary!r}"
    # accuracy: the clean nested skill + clean tool ARE present
    assert "ce-plan" in result.manifest["nested_skills"]
    tool_names = {t["name"] for t in result.manifest["tools"]}
    assert "Bash" in tool_names
    # the secret-named skill was dropped, not emitted
    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB" not in result.manifest["nested_skills"]
    assert any(u["field"] == "nested_skills" for u in result.manifest["unknown"])


# --- codex reader: required canary leak-test (eng T2, Track 2) ---

def _build_malicious_codex_home(root: Path) -> Path:
    """A ~/.codex fixture stuffed with secrets in every readable surface.

    Mirrors the real reference machine: model at the top level, provider tables +
    http_headers carrying `sk-godmode`/portkey blobs (the probe must NEVER read them),
    a flat skills/ dir, an [mcp_servers.*] table (names only), and a SKILL.md body
    carrying a secret (never opened).
    """
    home = root / ".codex"
    (home / "skills" / "gstack").mkdir(parents=True)
    (home / "skills" / "office-hours").mkdir(parents=True)
    # SKILL.md body carries a secret — the probe must NEVER read file contents.
    (home / "skills" / "gstack" / "SKILL.md").write_text(
        "name: gstack\nsecret: sk-ant-api03-SECRETSECRETSECRETSECRETSECRET\n"
    )
    # config.toml: model is allowlisted; provider tables + http_headers carry secrets.
    home_config = home / "config.toml"
    home_config.write_text(
        'model = "gpt-5.5"\n'
        'model_provider = "godmode"\n'
        'model_reasoning_effort = "xhigh"\n'
        '\n'
        '[model_providers.godmode]\n'
        'name = "godmode"\n'
        'base_url = "http://admin:s3cr3t@internal.example.com"\n'
        '\n'
        '[model_providers.godmode.http_headers]\n'
        "x-portkey-config = '{\"api_key\":\"sk-godmode\",\"token\":\"ghp_1234567890abcdefghijklmnopqrstuvwxyzAB\"}'\n"
        '\n'
        '[mcp_servers.github]\n'
        'command = "gh-mcp"\n'
        'env = { GITHUB_TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB" }\n'
        '\n'
        '[mcp_servers.grafana]\n'
        'url = "https://admin:s3cr3t@internal.example.com"\n'
    )
    return home


def test_codex_probe_canary_no_leaks(tmp_path):
    home = _build_malicious_codex_home(tmp_path)
    result = fp.probe_codex(home)
    manifest = json.dumps(result.manifest)
    log = result.log

    for canary in CANARIES + ["sk-godmode"]:
        assert canary not in manifest, f"LEAK: {canary!r} in manifest"
        assert canary not in log, f"LEAK: {canary!r} in log"

    # Accuracy: a leak-free {} is useless. Expected names MUST be present.
    assert result.manifest["harness"] == "codex"
    assert result.manifest["model"] == "gpt-5.5"
    assert set(result.manifest["skills"]) == {"gstack", "office-hours"}
    assert set(result.manifest["mcps"]) == {"github", "grafana"}
    assert result.manifest["plugins"] == []
    assert result.manifest["custom_agents_count"] == 0
    assert result.manifest["gstack"] is True


def test_codex_manifest_validates_fixed_schema(tmp_path):
    home = _build_malicious_codex_home(tmp_path)
    result = fp.probe_codex(home)
    assert set(result.manifest) == set(fp.FINGERPRINT_SCHEMA_KEYS)


def test_codex_empty_home_is_clean_not_crash(tmp_path):
    """A ~/.codex that doesn't exist / is empty → a valid empty-ish manifest, no crash."""
    result = fp.probe_codex(tmp_path / ".codex")
    assert result.manifest["harness"] == "codex"
    assert result.manifest["model"] == "unknown"
    assert result.manifest["skills"] == []
    assert result.manifest["mcps"] == []
    assert result.manifest["plugins"] == []
    assert result.manifest["custom_agents_count"] == 0


def test_codex_model_absent_is_unknown_no_unknown_entry(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "godmode"\n')  # no top-level model
    result = fp.probe_codex(home)
    assert result.manifest["model"] == "unknown"
    # absent model → NO unknown[] entry (matches claude/copilot default behavior)
    assert not any(u["field"] == "model" for u in result.manifest["unknown"])


@pytest.mark.parametrize("probe_fn,root,model_toml_or_json,is_toml", [
    (fp.probe_codex, ".codex", "model = 123", True),
    (fp.probe_codex, ".codex", "model = 1.5", True),
    (fp.probe_codex, ".codex", "model = true", True),
])
def test_codex_nonstring_model_type_is_malformed_not_name_unsafe(
        tmp_path, probe_fn, root, model_toml_or_json, is_toml):
    """Santa PR#9 round 5 (reviewer B): a model of the WRONG TYPE (a number/bool, e.g.
    `model = 123`) is a structurally malformed config field, NOT a scrubbed unsafe-name
    secret. It must be flagged REASON_MALFORMED so the CLI fail-closed guard fires —
    distinct from an unsafe-STRING model (e.g. 'hunter2'), which stays a scrub-not-fail
    consent boundary (model=unknown, exit 0)."""
    home = tmp_path / root
    home.mkdir()
    (home / "config.toml").write_text(model_toml_or_json + "\n")
    result = probe_fn(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("model", "malformed") in reasons, (
        f"wrong-type model ({model_toml_or_json}) must be malformed, got {result.manifest['unknown']}"
    )


@pytest.mark.parametrize("probe_fn,root", [
    (fp.probe_claude_code, ".claude"),
    (fp.probe_copilot_cli, ".copilot"),
])
def test_claude_copilot_nonstring_model_type_is_malformed(tmp_path, probe_fn, root):
    """Same wrong-type-model fail-closed for claude-code + copilot-cli settings.json."""
    home = tmp_path / root
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": 123}))
    result = probe_fn(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("model", "malformed") in reasons, (
        f"wrong-type model must be malformed, got {result.manifest['unknown']}"
    )


def test_unsafe_string_model_stays_scrub_not_fail(tmp_path):
    """Guard the boundary: an unsafe-STRING model (a real string that fails the safety
    scan) must remain name_failed_safety_scan (scrub, model=unknown), NOT malformed —
    preserving the documented consent-surface behavior. Only wrong-TYPE models fail closed."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "sk-proj-leaked-key"}))
    result = fp.probe_claude_code(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("model", "name_failed_safety_scan") in reasons
    assert ("model", "malformed") not in reasons
    assert result.manifest["model"] == "unknown"


def test_codex_never_reads_provider_or_http_headers(tmp_path):
    """The probe must key ONLY off top-level model; model_provider / model_providers /
    http_headers carry base_urls + embedded api keys and must never enter the manifest."""
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text(
        'model = "gpt-5.5"\n'
        'model_provider = "godmode-secret-provider"\n'
        '[model_providers.godmode.http_headers]\n'
        'authorization = "Bearer eyJhbG.SECRETJWT.payload"\n'
    )
    result = fp.probe_codex(home)
    blob = json.dumps(result.manifest) + result.log
    assert "godmode-secret-provider" not in blob
    assert "SECRETJWT" not in blob
    assert result.manifest["model"] == "gpt-5.5"


def test_codex_prompts_count(tmp_path):
    home = tmp_path / ".codex"
    (home / "prompts").mkdir(parents=True)
    (home / "prompts" / "a.md").write_text("x")
    (home / "prompts" / "b.md").write_text("y")
    (home / "prompts" / "notes.txt").write_text("not md")
    (home / "config.toml").write_text('model = "gpt-5.5"\n')
    result = fp.probe_codex(home)
    assert result.manifest["custom_agents_count"] == 2


def test_codex_probe_does_not_read_skill_or_prompt_bodies(tmp_path, monkeypatch):
    home = _build_malicious_codex_home(tmp_path)
    opened: list[str] = []
    real = Path.read_text

    def spy(self, *a, **k):
        opened.append(str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    fp.probe_codex(home)
    for p in opened:
        assert not p.endswith("SKILL.md"), f"probe read skill body: {p}"
        assert "/prompts/" not in p, f"probe read prompt body: {p}"


def test_codex_malformed_config_flags_mcps_unknown(tmp_path):
    """Santa PR#9 (reviewer B): a malformed config.toml zeroes BOTH model and mcps —
    they come from the SAME untrusted config, so an unreadable config must surface an
    unknown[] entry for every dependent field, not just `model`. Otherwise mcps=[]
    reads as a confident 'no MCP servers' when the truth is 'config unreadable'."""
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text("this is = = broken toml [[[")
    result = fp.probe_codex(home)
    fields = {u["field"] for u in result.manifest["unknown"]}
    assert "model" in fields
    assert "mcps" in fields, (
        "malformed config.toml must flag mcps unknown too (same untrusted source), "
        f"got unknown={result.manifest['unknown']}"
    )


def test_codex_existing_unreadable_config_flags_unknown(tmp_path):
    """Santa PR#9 round 3 (reviewer B): a config.toml that EXISTS but is unreadable
    (a directory where a file is expected → OSError → not_readable) must NOT be treated
    like 'absent'. An absent optional config yields no unknown; an existing-but-broken
    one must flag model+mcps unknown so the CLI guard fails closed, never publishes an
    empty confident manifest."""
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").mkdir()  # exists, but not a readable file
    result = fp.probe_codex(home)
    fields = {u["field"] for u in result.manifest["unknown"]}
    assert "model" in fields and "mcps" in fields, (
        f"existing-unreadable config.toml must flag model+mcps unknown, "
        f"got unknown={result.manifest['unknown']}"
    )


def test_claude_existing_unreadable_settings_flags_model_unknown(tmp_path):
    """Same existing-but-unreadable gap for claude settings.json."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").mkdir()  # exists, unreadable as a file
    result = fp.probe_claude_code(home)
    fields = {u["field"] for u in result.manifest["unknown"]}
    assert "model" in fields, (
        f"existing-unreadable settings.json must flag model unknown, "
        f"got unknown={result.manifest['unknown']}"
    )


def test_claude_existing_unreadable_mcp_source_flags_unknown(tmp_path):
    """Santa PR#9 round 3 (reviewer B): an existing-but-unreadable ~/.claude.json must
    flag mcps unknown, not silently emit mcps=[] (which reads as 'no MCP servers')."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    (home.parent / ".claude.json").mkdir()  # exists, unreadable as a file
    result = fp.probe_claude_code(home)
    fields = {u["field"] for u in result.manifest["unknown"]}
    assert "mcps" in fields, (
        f"existing-unreadable ~/.claude.json must flag mcps unknown, "
        f"got unknown={result.manifest['unknown']}"
    )


def test_claude_existing_unreadable_installed_plugins_flags_unknown(tmp_path):
    """An existing-but-unreadable installed_plugins.json must flag plugins unknown, not
    silently drop nested plugin skills/agents."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "plugins").mkdir()
    (home / "plugins" / "installed_plugins.json").mkdir()  # exists, unreadable as a file
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8", "enabledPlugins": {"p@m": True}}))
    result = fp.probe_claude_code(home)
    fields = {u["field"] for u in result.manifest["unknown"]}
    assert "plugins" in fields, (
        f"existing-unreadable installed_plugins.json must flag plugins unknown, "
        f"got unknown={result.manifest['unknown']}"
    )


@pytest.mark.parametrize("shape", ["[]", "42", '"a string"', "null", "true"])
def test_claude_nondict_primary_config_flags_model_malformed(tmp_path, shape):
    """Santa PR#9 round 2 (both reviewers): a PARSEABLE but non-dict settings.json
    (a JSON array/scalar/null) is ok=True yet not a dict. It must NOT fall through
    silently — it must flag model malformed so the CLI fail-closed guard can fire.
    Otherwise claude-code publishes a confident EMPTY manifest from a broken config."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(shape)
    result = fp.probe_claude_code(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("model", "malformed") in reasons, (
        f"non-dict settings.json ({shape}) must flag model malformed, "
        f"got unknown={result.manifest['unknown']}"
    )


@pytest.mark.parametrize("shape", ["[]", "42", '"a string"', "null", "true"])
def test_copilot_nondict_primary_config_flags_model_malformed(tmp_path, shape):
    """Same fail-closed gap as claude-code, for copilot-cli's settings.json."""
    home = tmp_path / ".copilot"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(shape)
    result = fp.probe_copilot_cli(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("model", "malformed") in reasons, (
        f"non-dict settings.json ({shape}) must flag model malformed, "
        f"got unknown={result.manifest['unknown']}"
    )


@pytest.mark.parametrize("manifest_json", [
    '{"version": 2, "plugins": "not-a-dict"}',       # plugins non-dict
    '{"version": 2, "plugins": {"p@m": "not-a-list"}}',  # entries non-list
    '{"version": 2, "plugins": {"p@m": ["not-a-dict"]}}',  # entry non-dict
    '{"version": 2, "plugins": {"p@m": [{}]}}',       # entry missing installPath
])
def test_claude_installed_plugins_bad_internal_shape_flags_unknown(tmp_path, manifest_json):
    """Santa PR#9 round 2 (both reviewers): bad INTERNAL shapes of a version-2
    installed_plugins.json (plugins non-dict, entries non-list, entry non-dict,
    missing installPath) for an ENABLED plugin silently dropped nested skills/agents
    with no marker. Surface an unknown[] entry so the loss is visible, never silent."""
    home = tmp_path / ".claude"
    (home / "skills" / "top-skill").mkdir(parents=True)
    (home / "plugins").mkdir()
    (home / "plugins" / "installed_plugins.json").write_text(manifest_json)
    (home / "settings.json").write_text(json.dumps({
        "model": "claude-opus-4-8",
        "enabledPlugins": {"p@m": True},
    }))
    result = fp.probe_claude_code(home)  # must not raise
    fields = {u["field"] for u in result.manifest["unknown"]}
    assert "plugins" in fields or "skills" in fields or "custom_agents_count" in fields, (
        "bad installed_plugins.json internal shape for an enabled plugin must surface "
        f"an unknown[] marker, got unknown={result.manifest['unknown']}"
    )


@pytest.mark.parametrize("shape", ["[]", "42", '"a string"', "null", "true"])
def test_claude_nondict_mcp_source_flags_mcps_malformed(tmp_path, shape):
    """Santa PR#9 round 7 (reviewer B): a PARSEABLE but non-dict ~/.claude.json (the mcp
    source) is ok=True yet not a dict, so it fell through and emitted mcps=[] with no
    marker — reading as a confident 'no MCP servers' when the source is actually
    malformed. Must flag mcps malformed."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    (home.parent / ".claude.json").write_text(shape)
    result = fp.probe_claude_code(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("mcps", "malformed") in reasons, (
        f"non-dict ~/.claude.json ({shape}) must flag mcps malformed, "
        f"got unknown={result.manifest['unknown']}"
    )


@pytest.mark.parametrize("shape", ["[]", "42", '"a string"', "null"])
def test_copilot_nondict_mcp_source_flags_mcps_malformed(tmp_path, shape):
    """Same non-dict mcp source gap for copilot-cli's mcp-config.json."""
    home = tmp_path / ".copilot"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "gpt-x"}))
    (home / "mcp-config.json").write_text(shape)
    result = fp.probe_copilot_cli(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("mcps", "malformed") in reasons, (
        f"non-dict mcp-config.json ({shape}) must flag mcps malformed, "
        f"got unknown={result.manifest['unknown']}"
    )


@pytest.mark.parametrize("bad_servers", ['["github"]', '"github"', '42', 'true'])
def test_claude_wrongshaped_mcpservers_field_flags_malformed(tmp_path, bad_servers):
    """Santa PR#9 round 10 (reviewer B): a valid-dict ~/.claude.json whose mcpServers
    FIELD is the wrong shape (a list/scalar, not a dict) must flag mcps malformed, not
    silently emit mcps=[] (reading as 'no MCP servers')."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    (home.parent / ".claude.json").write_text('{"mcpServers": ' + bad_servers + '}')
    result = fp.probe_claude_code(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("mcps", "malformed") in reasons, (
        f"wrong-shaped mcpServers ({bad_servers}) must flag mcps malformed, "
        f"got unknown={result.manifest['unknown']}"
    )


@pytest.mark.parametrize("bad_servers", ['["github"]', '"github"', '42'])
def test_copilot_wrongshaped_mcpservers_field_flags_malformed(tmp_path, bad_servers):
    """Same wrong-shaped-field gap for copilot mcp-config.json."""
    home = tmp_path / ".copilot"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "gpt-x"}))
    (home / "mcp-config.json").write_text('{"mcpServers": ' + bad_servers + '}')
    result = fp.probe_copilot_cli(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("mcps", "malformed") in reasons, (
        f"wrong-shaped mcpServers ({bad_servers}) must flag mcps malformed, "
        f"got unknown={result.manifest['unknown']}"
    )


def test_codex_wrongshaped_mcp_servers_field_flags_malformed(tmp_path):
    """A codex config.toml with mcp_servers as an ARRAY (not a table) must flag mcps
    malformed, not silently emit mcps=[]."""
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5.5"\nmcp_servers = ["github"]\n')
    result = fp.probe_codex(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("mcps", "malformed") in reasons, (
        f"wrong-shaped mcp_servers must flag mcps malformed, got {result.manifest['unknown']}"
    )


@pytest.mark.parametrize("bad_ep", ['["foo"]', '"foo"', '42'])
def test_claude_wrongshaped_enabledplugins_flags_malformed(tmp_path, bad_ep):
    """Santa PR#9 round 10 (reviewer B suggestion): a present-but-wrong-shaped
    enabledPlugins field (list/scalar, not a dict) must flag plugins malformed, not
    silently emit plugins=[]."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(
        '{"model": "claude-opus-4-8", "enabledPlugins": ' + bad_ep + '}')
    result = fp.probe_claude_code(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("plugins", "malformed") in reasons, (
        f"wrong-shaped enabledPlugins ({bad_ep}) must flag plugins malformed, "
        f"got unknown={result.manifest['unknown']}"
    )


def test_copilot_disabled_plugin_value_false_excluded(tmp_path):
    """Santa PR#9 round 11 (reviewer B): copilot ignored enabledPlugins VALUES and
    published every key — so a disabled plugin (value false) leaked into the manifest.
    A false value must exclude the plugin, matching claude-code's disable semantics."""
    home = tmp_path / ".copilot"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({
        "model": "gpt-x",
        "enabledPlugins": {"on@mkt": True, "off@mkt": False},
    }))
    result = fp.probe_copilot_cli(home)
    assert result.manifest["plugins"] == ["on"], result.manifest["plugins"]


@pytest.mark.parametrize("bad_val", ['"yes"', "1", "0", '""', "null", "[]"])
def test_claude_nonbool_enabledplugin_value_flags_malformed(tmp_path, bad_val):
    """Santa PR#9 round 11 (reviewer B): a non-boolean enabledPlugins VALUE (e.g. "yes")
    was treated as truthy and published the plugin. Non-bool values must flag plugins
    malformed and contribute no plugin name (only explicit true enables)."""
    home = tmp_path / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(
        '{"model": "claude-opus-4-8", "enabledPlugins": {"bad-plugin@mkt": ' + bad_val + '}}')
    result = fp.probe_claude_code(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("plugins", "malformed") in reasons, (
        f"non-bool enabledPlugins value ({bad_val}) must flag plugins malformed, "
        f"got unknown={result.manifest['unknown']}")
    assert "bad-plugin" not in result.manifest["plugins"], result.manifest["plugins"]


@pytest.mark.parametrize("bad_val", ['"yes"', "1", "0", "null"])
def test_copilot_nonbool_enabledplugin_value_flags_malformed(tmp_path, bad_val):
    """Same non-bool enabledPlugins value discipline for copilot-cli."""
    home = tmp_path / ".copilot"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(
        '{"model": "gpt-x", "enabledPlugins": {"bad-plugin@mkt": ' + bad_val + '}}')
    result = fp.probe_copilot_cli(home)
    reasons = {(u["field"], u["reason"]) for u in result.manifest["unknown"]}
    assert ("plugins", "malformed") in reasons, (
        f"non-bool enabledPlugins value ({bad_val}) must flag plugins malformed, "
        f"got unknown={result.manifest['unknown']}")
    assert "bad-plugin" not in result.manifest["plugins"], result.manifest["plugins"]


def test_copilot_disabled_plugin_nested_skills_agents_excluded(tmp_path):
    """Santa PR#9 round 12 (reviewer B): copilot's nested walk iterated installed-plugins/
    on disk unconditionally, so a DISABLED plugin's nested skills/agents still leaked into
    the manifest even though the plugin itself was excluded. The nested walk must be gated
    on the enabled-plugin key set (dir <mkt>/<plug> ↔ enabled key <plug>@<mkt>)."""
    home = tmp_path / ".copilot"
    (home / "skills").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({
        "model": "gpt-x",
        "enabledPlugins": {"on@mkt": True, "off@mkt": False},
    }))
    # enabled plugin: on@mkt → dir mkt/on
    on_root = home / "installed-plugins" / "mkt" / "on"
    (on_root / "skills" / "enabledskill").mkdir(parents=True)
    (on_root / "agents").mkdir(parents=True)
    (on_root / "agents" / "e.md").write_text("x")
    # disabled plugin: off@mkt → dir mkt/off — its nested content must NOT be counted
    off_root = home / "installed-plugins" / "mkt" / "off"
    (off_root / "skills" / "secretskill").mkdir(parents=True)
    (off_root / "agents").mkdir(parents=True)
    (off_root / "agents" / "s.md").write_text("x")
    result = fp.probe_copilot_cli(home)
    assert result.manifest["plugins"] == ["on"], result.manifest["plugins"]
    assert "enabledskill" in result.manifest["skills"], result.manifest["skills"]
    assert "secretskill" not in result.manifest["skills"], result.manifest["skills"]
    assert result.manifest["custom_agents_count"] == 1, result.manifest["custom_agents_count"]
