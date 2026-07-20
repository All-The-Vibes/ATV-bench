from __future__ import annotations

import json
import subprocess

import pytest

from atv_bench.comparison import (
    engine_for_seed,
    git_commit,
    git_tree,
    materialize_pointer_tree,
    parse_copilot_jsonl,
    run_game,
    scan_harness_assets,
    summarize_games,
    tracked_tree_listing_sha256,
    verify_checksums,
    wilson_interval,
    write_checksums,
    write_exact_bytes,
)


def test_engine_for_seed_is_deterministic_and_asymmetric():
    first = engine_for_seed(123)
    second = engine_for_seed(123)
    assert first == second
    assert first.start_a != first.start_b
    assert 0 < first.start_a[0] < first.width - 1
    assert 0 < first.start_b[0] < first.width - 1


def test_compact_engine_profile_and_explicit_turn_cap_are_deterministic():
    engine = engine_for_seed(123, board_profile="compact", max_turns=40)
    assert 11 <= engine.width <= 15
    assert 9 <= engine.height <= 13
    assert engine.max_turns == 40
    assert engine == engine_for_seed(
        123,
        board_profile="compact",
        max_turns=40,
    )


def test_engine_profile_and_turn_cap_fail_closed():
    with pytest.raises(ValueError, match="board_profile"):
        engine_for_seed(1, board_profile="unknown")
    with pytest.raises(ValueError, match="max_turns"):
        engine_for_seed(1, max_turns=0)


def test_summarize_games_counts_side_swapped_results():
    summary = summarize_games(
        [
            {"winner": "harness_a"},
            {"winner": "harness_b"},
            {"winner": "harness_a"},
            {"winner": "draw"},
        ]
    )
    assert summary["games"] == 4
    assert summary["harness_a_wins"] == 2
    assert summary["harness_b_wins"] == 1
    assert summary["draws"] == 1
    assert summary["harness_a_decisive_win_rate"] == 0.6667
    assert summary["harness_a_decisive_win_rate_ci95"] == wilson_interval(2, 3)


def test_run_game_propagates_hard_match_timeout(tmp_path):
    bot = tmp_path / "bot.py"
    bot.write_text(
        "import sys\nfor _ in sys.stdin:\n print('right', flush=True)\n",
        encoding="utf-8",
    )

    result = run_game(
        bot,
        bot,
        seed=123,
        per_turn_timeout=1.0,
        match_timeout=0.001,
        board_profile="compact",
        max_turns=40,
    )

    assert result["outcome"] == "draw"
    assert result["termination_reason"] == "MATCH_TIMEOUT"


def test_materialize_pointer_tree_resolves_file_and_directory_pointers(tmp_path):
    root = tmp_path / "repo"
    plugin = root / "plugins" / "demo"
    target = root / ".github" / "agents" / "worker.agent.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nname: Worker\ndescription: test\n---\n", encoding="utf-8")
    pointer = plugin / "agents" / "worker.md"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("../../../.github/agents/worker.agent.md\n", encoding="utf-8")
    (plugin / ".github" / "plugin").mkdir(parents=True)
    (plugin / ".github" / "plugin" / "plugin.json").write_text(
        json.dumps({"name": "demo", "agents": ["agents/"]}),
        encoding="utf-8",
    )
    target_directory = root / ".github" / "hooks" / "telemetry"
    target_directory.mkdir(parents=True)
    (target_directory / "collector.ps1").write_text(
        "Write-Output ok\n", encoding="utf-8"
    )
    directory_pointer = plugin / "hooks" / "telemetry"
    directory_pointer.parent.mkdir(parents=True)
    directory_pointer.write_text(
        "../../../.github/hooks/telemetry\n", encoding="utf-8"
    )

    output = tmp_path / "out"
    assert materialize_pointer_tree(plugin, output, source_root=root) == 2
    assert "name: Worker" in (output / "agents" / "worker.md").read_text(
        encoding="utf-8"
    )
    assert (output / "hooks" / "telemetry" / "collector.ps1").is_file()


def test_scan_harness_assets_is_names_only(tmp_path):
    (tmp_path / "skills" / "build").mkdir(parents=True)
    (tmp_path / "skills" / "build" / "SKILL.md").write_text(
        "# Build", encoding="utf-8"
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "worker.agent.md").write_text(
        "---\nname: Worker\ndescription: x\n---\n", encoding="utf-8"
    )
    fingerprint = scan_harness_assets(tmp_path)
    assert fingerprint["skills"] == ["build"]
    assert fingerprint["agents"] == ["Worker"]
    assert fingerprint["counts"]["skills"] == 1


def test_parse_copilot_jsonl_ignores_encrypted_payloads():
    lines = [
        {
            "type": "session.skills_loaded",
            "data": {
                "skills": [
                    {"name": "x", "source": "plugin", "enabled": True},
                    {"name": "y", "source": "inherited", "enabled": False},
                ]
            },
        },
        {
            "type": "assistant.message",
            "data": {
                "model": "gpt-5.4",
                "content": "done",
                "encryptedContent": "secret",
                "reasoningOpaque": "secret",
            },
        },
        {
            "type": "result",
            "exitCode": 0,
            "sessionId": "s",
            "usage": {"premiumRequests": 0},
        },
    ]
    parsed = parse_copilot_jsonl(
        "\n".join(json.dumps(item) for item in lines).encode("utf-8")
    )
    assert parsed["model"] == "gpt-5.4"
    assert parsed["observed_models"] == ["gpt-5.4"]
    assert parsed["parse_error_count"] == 0
    assert parsed["terminal_result_count"] == 1
    assert parsed["terminal_success"] is True
    assert parsed["final_message"] == "done"
    assert parsed["enabled_skill_sources"] == {"plugin": 1}
    assert "secret" not in json.dumps(parsed)


def test_parse_copilot_jsonl_exposes_mixed_missing_and_malformed_model_evidence():
    mixed = [
        {"type": "assistant.turn_start", "data": {"model": "wrong-model"}},
        {"type": "assistant.message", "data": {"model": "requested-model"}},
        {
            "type": "result",
            "exitCode": 0,
            "sessionId": "session",
            "usage": {},
        },
    ]
    payload = (
        "\n".join(json.dumps(item) for item in mixed)
        + "\nnot-json\n"
    ).encode("utf-8")

    parsed = parse_copilot_jsonl(payload)

    assert parsed["model"] is None
    assert parsed["observed_models"] == ["requested-model", "wrong-model"]
    assert parsed["model_event_count"] == 2
    assert parsed["parse_error_count"] == 1
    assert parsed["terminal_success"] is True

    missing = parse_copilot_jsonl(
        json.dumps(
            {
                "type": "result",
                "exitCode": 0,
                "sessionId": "session",
                "usage": {},
            }
        )
    )
    assert missing["model"] is None
    assert missing["observed_models"] == []
    assert missing["model_event_count"] == 0


def test_exact_byte_checksums_cover_binary_logs_and_detect_tamper(tmp_path):
    payload = b"line-1\r\nline-2\x00\xff\n"
    digest = write_exact_bytes(tmp_path / "raw" / "phoenix.stdout.bin", payload)
    assert (tmp_path / "raw" / "phoenix.stdout.bin").read_bytes() == payload
    document = write_checksums(tmp_path)
    assert document["files"][0]["sha256"] == digest
    assert verify_checksums(tmp_path) == (True, [])

    (tmp_path / "raw" / "phoenix.stdout.bin").write_bytes(payload + b"tampered")
    ok, errors = verify_checksums(tmp_path)
    assert ok is False
    assert any("mismatch" in error for error in errors)


def test_git_identities_bind_commit_tree_and_tracked_listing(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=ATV Test",
            "-c",
            "user.email=atv@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        cwd=tmp_path,
        check=True,
    )
    commit = git_commit(tmp_path)
    tree = git_tree(tmp_path)
    listing_digest = tracked_tree_listing_sha256(tmp_path)
    assert len(commit) == 40
    assert len(tree) == 40
    assert len(listing_digest) == 64

    (tmp_path / "untracked.txt").write_text("ignored by identity\n", encoding="utf-8")
    assert tracked_tree_listing_sha256(tmp_path) == listing_digest
