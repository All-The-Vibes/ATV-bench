"""F2 (santa round-1, Reviewer B SECURITY): the trusted publish/build path must
re-validate merged submission fingerprints for leak-safety before their details enter a
published leaderboard row.

The probe is leak-safe at emit time, but `league/submissions/<id>/submission.json` is a
plain committed file: a contributor (or a compromised PR) can hand-edit it to carry
secret-shaped `skills`/`mcps`/`plugins`/`harness` strings. `build_leaderboard_doc`
copied those straight onto the static board (schema only requires `type: string`).

Fix: run the SAME leak-safe scanner (`fingerprint.scan.is_safe_name` / `is_secret`) the
probe uses, on every value that would enter a published row. A failing value is dropped
from details and recorded in `unknown[{field, reason:"name_failed_safety_scan"}]`. Never
published, never crashes. Fail-closed.
"""
from __future__ import annotations

from atv_bench.leaderboard import build_leaderboard_doc
from atv_bench.elo import MatchResult, Outcome


def _sub(identity, *, skills=None, mcps=None, plugins=None, harness="claude-code"):
    return {
        "identity": identity,
        "game": "battlesnake",
        "bot_sha256": "a" * 64,
        "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
        "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
        "fingerprint": {
            "harness": harness, "model": "claude-opus-4-8", "gstack": True,
            "skills": skills if skills is not None else ["gstack"],
            "mcps": mcps if mcps is not None else ["github"],
            "plugins": plugins if plugins is not None else [],
            "custom_agents_count": 0, "unknown": [], "probe_version": "1.0.0",
        },
    }


def _row(doc, identity):
    return next(r for r in doc["rows"] if r["identity"] == identity)


def test_secret_shaped_skill_never_reaches_board(tmp_path):
    """A hand-edited record with an API-key-shaped skill must NOT publish that value."""
    subs = {"mallory": _sub("mallory", skills=["gstack", "sk-ABCDEF0123456789ABCDEF0123456789"])}
    doc = build_leaderboard_doc([], subs, updated_at="2026-07-16T00:00:00Z")
    row = _row(doc, "mallory")
    published = " ".join(row["details"]["skills"])
    assert "sk-ABCDEF" not in published, "secret-shaped skill leaked onto the board"
    assert "gstack" in row["details"]["skills"], "safe skill must survive"


def test_scrubbed_value_recorded_in_unknown(tmp_path):
    """A dropped value is not silently deleted — it surfaces in unknown[] with a reason,
    so the scrub is auditable rather than invisible."""
    subs = {"mallory": _sub("mallory", mcps=["github", "ghp_0123456789abcdefABCD0123456789abcdEF"])}
    doc = build_leaderboard_doc([], subs, updated_at="2026-07-16T00:00:00Z")
    row = _row(doc, "mallory")
    assert "ghp_" not in " ".join(row["details"]["mcps"])
    reasons = [u["reason"] for u in row["details"]["unknown"]]
    assert "name_failed_safety_scan" in reasons


def test_secret_shaped_plugin_scrubbed(tmp_path):
    subs = {"m": _sub("m", plugins=["AKIAIOSFODNN7EXAMPLE"])}
    doc = build_leaderboard_doc([], subs, updated_at="2026-07-16T00:00:00Z")
    row = _row(doc, "m")
    assert row["details"]["plugins"] == []
    assert any(u["reason"] == "name_failed_safety_scan" for u in row["details"]["unknown"])


def test_secret_shaped_harness_not_published(tmp_path):
    """harness is copied to a row field; a secret-shaped harness must not publish."""
    subs = {"m": _sub("m", harness="sk-DEADBEEFDEADBEEFDEADBEEFDEADBEEF")}
    doc = build_leaderboard_doc([], subs, updated_at="2026-07-16T00:00:00Z")
    row = _row(doc, "m")
    assert not row["harness_name"].startswith("sk-")


def test_clean_fingerprint_unchanged(tmp_path):
    """No false positives: an all-safe fingerprint publishes verbatim."""
    subs = {"alice": _sub("alice", skills=["gstack", "office-hours"], mcps=["github", "grafana"])}
    doc = build_leaderboard_doc([], subs, updated_at="2026-07-16T00:00:00Z")
    row = _row(doc, "alice")
    assert row["details"]["skills"] == ["gstack", "office-hours"]
    assert row["details"]["mcps"] == ["github", "grafana"]
    assert row["details"]["unknown"] == []
    assert row["harness_name"] == "claude-code"


# --- santa round-2: gaps the round-1 F2 fix missed ---

def test_string_valued_skills_field_does_not_leak_char_by_char(tmp_path):
    """G1 (both reviewers, CRITICAL): a hand-edited record with a STRING skills field
    (not a list) was iterated character-by-character; safe chars of a secret slipped
    through. A non-list field must be rejected wholesale, never iterated."""
    subs = {"m": _sub("m", skills="ghp_0123456789abcdefABCD0123456789abcdEF")}
    doc = build_leaderboard_doc([], subs, updated_at="2026-07-16T00:00:00Z")
    row = _row(doc, "m")
    # no character fragment of the secret may appear as a published skill
    assert row["details"]["skills"] == [], f"string field leaked: {row['details']['skills']}"
    assert any(u["reason"] == "name_failed_safety_scan" for u in row["details"]["unknown"])


def test_string_valued_field_not_counted_in_summary(tmp_path):
    """The summary must not report len() of a string field as a skill count."""
    subs = {"m": _sub("m", skills="ghp_SECRET_LOOKING_VALUE_012345")}
    doc = build_leaderboard_doc([], subs, updated_at="2026-07-16T00:00:00Z")
    row = _row(doc, "m")
    # a 28-char string must not read as "28 skills"
    assert "28 skills" not in row["fingerprint_summary"]
    assert "0 skills" in row["fingerprint_summary"]


def test_secret_shaped_probe_version_not_published(tmp_path):
    """G1b (Reviewer A): fingerprint_probe_version is copied to a published row field from
    the hand-editable record and was not scanned. A secret-shaped value must not publish."""
    sub = _sub("m")
    sub["fingerprint"]["probe_version"] = "sk-DEADBEEFDEADBEEFDEADBEEFDEADBEEF"
    doc = build_leaderboard_doc([], {"m": sub}, updated_at="2026-07-16T00:00:00Z")
    row = _row(doc, "m")
    assert not row["fingerprint_probe_version"].startswith("sk-")


def test_non_list_mcps_and_plugins_rejected(tmp_path):
    subs = {"m": _sub("m", mcps="AKIAIOSFODNN7EXAMPLE", plugins={"nope": 1})}
    doc = build_leaderboard_doc([], subs, updated_at="2026-07-16T00:00:00Z")
    row = _row(doc, "m")
    assert row["details"]["mcps"] == []
    assert row["details"]["plugins"] == []
    reasons = [u["reason"] for u in row["details"]["unknown"]]
    assert reasons.count("name_failed_safety_scan") >= 2
