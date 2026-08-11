"""PR-path governance (santa round-4, Reviewer B CRITICAL): a community PR must be
confined to its OWN submission tree. The runtime scoring path is workflow-pinned to the
PR author, but the durable store is rebuilt from committed files — so a merged PR that
edits league/matches.jsonl directly, or writes into another entrant's directory, could
forge history or poison another row. This guard fails closed on any changed path outside
league/submissions/<author>/{main.py,submission.json}, so CI can block the PR before merge.
"""
from __future__ import annotations

import pytest

from atv_bench.validate import validate_pr_paths


def test_own_submission_files_allowed():
    res = validate_pr_paths("octocat", [
        "league/submissions/octocat/main.py",
        "league/submissions/octocat/submission.json",
    ])
    assert res["ok"] is True and res["errors"] == []


def test_direct_matches_edit_rejected():
    res = validate_pr_paths("octocat", ["league/matches.jsonl"])
    assert res["ok"] is False
    assert any("matches.jsonl" in e for e in res["errors"])


def test_other_entrant_dir_rejected():
    res = validate_pr_paths("octocat", ["league/submissions/victim/main.py"])
    assert res["ok"] is False


def test_stray_file_in_own_dir_rejected():
    res = validate_pr_paths("octocat", ["league/submissions/octocat/evil.sh"])
    assert res["ok"] is False


def test_path_traversal_in_changed_path_rejected():
    res = validate_pr_paths("octocat", ["league/submissions/octocat/../victim/main.py"])
    assert res["ok"] is False


def test_edits_outside_league_rejected():
    res = validate_pr_paths("octocat", ["src/atv_bench/store.py"])
    assert res["ok"] is False


def test_empty_or_invalid_author_rejected():
    for bad in ("", "has space", "a/b"):
        res = validate_pr_paths(bad, ["league/submissions/x/main.py"])
        assert res["ok"] is False


# --- name-status handling + always-on submission-PR confinement (santa round-7, Reviewer
#     B): the gate must reject renames/deletes (not just added/modified paths) and treat a
#     PR as a "submission PR" (subject to confinement) iff it touches league/submissions/**.
#     A submission PR that ALSO touches workflows / matches.jsonl / other dirs is the attack.

from atv_bench.validate import validate_pr_changes


def test_changes_add_modify_own_files_allowed():
    res = validate_pr_changes("octocat", [
        "A\tleague/submissions/octocat/main.py",
        "M\tleague/submissions/octocat/submission.json",
    ])
    assert res["ok"] is True


def test_changes_rename_rejected():
    # a rename of another entrant's bot into your dir must be rejected (R status, 2 paths)
    res = validate_pr_changes("octocat", [
        "R100\tleague/submissions/victim/main.py\tleague/submissions/octocat/main.py",
    ])
    assert res["ok"] is False


def test_changes_delete_rejected():
    res = validate_pr_changes("octocat", ["D\tleague/matches.jsonl"])
    assert res["ok"] is False


def test_changes_workflow_edit_on_submission_pr_rejected():
    # a submission PR that also edits a workflow file is the pwn-request vector
    res = validate_pr_changes("octocat", [
        "M\tleague/submissions/octocat/main.py",
        "M\t.github/workflows/league.yml",
    ])
    assert res["ok"] is False


def test_changes_non_submission_pr_is_not_confined():
    # a pure maintainer/plumbing PR (touches no league/submissions/**) is NOT a submission
    # PR and is not confined by this gate (it goes through normal review, not the league).
    res = validate_pr_changes("maintainer", [
        "M\tsrc/atv_bench/store.py",
        "M\t.github/workflows/ci.yml",
    ])
    assert res["ok"] is True
    assert res["is_submission_pr"] is False


def test_changes_submission_pr_flag_set():
    res = validate_pr_changes("octocat", ["A\tleague/submissions/octocat/main.py"])
    assert res["is_submission_pr"] is True


def test_submissions_root_scaffolding_is_not_a_submission():
    # league/submissions/.gitkeep (directory scaffolding at the submissions ROOT, no
    # per-entrant subdir) must NOT flip is_submission_pr — otherwise the foundational
    # maintainer PR that creates the tree gets confined to submission-only paths and its
    # own .github/** and src/** files are rejected. A real submission lives one level
    # deeper: league/submissions/<identity>/{main.py,submission.json}.
    res = validate_pr_changes("maintainer", [
        "A\tleague/submissions/.gitkeep",
        "M\tsrc/atv_bench/store.py",
        "A\t.github/workflows/ci.yml",
    ])
    assert res["is_submission_pr"] is False
    assert res["ok"] is True


def test_changes_maintainer_delete_outside_league_allowed():
    """A pure maintainer PR that DELETES a non-league file must pass.

    Regression (PR #34): the R/C/D status rejection ran BEFORE the is_submission_pr
    check, so any maintainer refactor that deleted a stale doc was rejected with
    "disallowed change status 'D'" even though is_submission_pr was False. The
    rename/delete gate exists to stop a submission PR dragging another entrant's
    bytes around or dropping league history — it must not police src/ and root docs.
    """
    res = validate_pr_changes("maintainer", [
        "D\tIMPLEMENTATION_PLAN.md",
        "M\tsrc/atv_bench/cli.py",
    ])
    assert res["is_submission_pr"] is False
    assert res["ok"] is True, res["errors"]


def test_changes_delete_of_league_state_still_rejected_for_maintainer():
    """Deleting league/** state stays rejected regardless of who opens the PR."""
    res = validate_pr_changes("maintainer", ["D\tleague/matches.jsonl"])
    assert res["ok"] is False


def test_changes_rename_within_league_still_rejected():
    res = validate_pr_changes("maintainer", [
        "R100\tleague/submissions/victim/main.py\tleague/submissions/other/main.py",
    ])
    assert res["ok"] is False


# --- league/ path normalization + fail-closed statuses (santa round 2) --------------
#
# Scoping the R/C/D gate to league/** made it a RAW prefix test against unnormalized
# `git diff --name-status` output, so three spellings of a league path slipped a gate
# that main rejects. Each vector below was verified against real git output (not a
# hand-built string): all return ok=False on origin/main and returned ok=True on the
# unfixed scoping commit 8fe1485.


def test_changes_c_quoted_league_delete_rejected():
    """git C-quotes any non-ASCII path by default (core.quotepath on), so the literal
    line ci.yml fed the guard was `D\t"league/submissions/caf\\303\\251/main.py"`.

    The leading quote defeated both the league/ prefix test AND _is_submission_path, so
    the PR was not even classified as a submission PR. An entrant whose GitHub login
    carries an accent is enough to reach this.
    """
    res = validate_pr_changes(
        "attacker", ['D\t"league/submissions/caf\\303\\251/main.py"']
    )
    assert res["ok"] is False, res


def test_changes_dot_slash_prefixed_league_delete_rejected():
    """A './' prefix defeated the raw league/ prefix test, allowing deletion of the
    durable league store while the guard still reported ok."""
    res = validate_pr_changes("attacker", ["D\t./league/matches.jsonl"])
    assert res["ok"] is False, res


def test_changes_case_variant_league_delete_rejected():
    """'League/matches.jsonl' resolves to the same file on a case-insensitive checkout,
    so the league/ comparison must not be case-sensitive."""
    res = validate_pr_changes("attacker", ["D\tLeague/matches.jsonl"])
    assert res["ok"] is False, res


def test_c_quoted_path_is_decoded_not_merely_unquoted():
    """Stripping the quotes alone would leave the literal text 'caf\\303\\251'; the
    real path must be recovered so downstream checks see the bytes git sees."""
    from atv_bench.validate import _decode_status_path
    assert _decode_status_path(
        '"league/submissions/caf\\303\\251/main.py"'
    ) == "league/submissions/café/main.py"


@pytest.mark.parametrize("status", ["U", "X", "B", "Z"])
def test_changes_unrecognized_status_fails_closed(status):
    """Scoping the gate to R/C/D let every OTHER status fall through to ALLOW, where
    main rejected them. An unrecognized code is the exact case a gate must not guess
    about."""
    res = validate_pr_changes("attacker", [f"{status}\tleague/matches.jsonl"])
    assert res["ok"] is False, res


def test_changes_malformed_record_without_path_fails_closed():
    """A status field with no path at all must not be silently skipped."""
    res = validate_pr_changes("attacker", ["D"])
    assert res["ok"] is False, res


# --- POSITIVE: the fix must not over-reject ----------------------------------------


def test_changes_normalized_own_submission_still_accepted():
    """The whole point of decoding is that a LEGITIMATE own-path submission still
    passes. A non-ASCII entrant must be able to add/modify their own two files."""
    res = validate_pr_changes("entrant", [
        "A\tleague/submissions/entrant/main.py",
        "M\tleague/submissions/entrant/submission.json",
    ])
    assert res["ok"] is True, res
    assert res["is_submission_pr"] is True


def test_changes_c_quoted_other_entrant_path_now_classified_and_rejected():
    """The C-quoted form previously defeated _is_submission_path too, so a PR touching
    an accented entrant's directory was not even classified as a submission PR and the
    confining validator never ran. After decoding it is classified, and confinement
    rejects it as another entrant's tree.

    (An accented *login* is impossible — _AUTHOR_RE is ASCII-only — but an accented
    *path* is not: it is created by the filesystem, and any ASCII attacker can name it.)
    """
    res = validate_pr_changes(
        "attacker", ['A\t"league/submissions/caf\\303\\251dev/main.py"']
    )
    assert res["is_submission_pr"] is True, res
    assert res["ok"] is False, res


def test_changes_maintainer_delete_outside_league_still_allowed_after_fix():
    """PR #35's intent is preserved: the fail-closed status check must not undo the
    scoping. A maintainer deleting a stale doc / renaming a src module still passes."""
    res = validate_pr_changes("maintainer", [
        "D\tdocs/stale.md",
        "R100\tsrc/atv_bench/old.py\tsrc/atv_bench/new.py",
    ])
    assert res["ok"] is True, res
    assert res["is_submission_pr"] is False


# --- CLI -z framing ----------------------------------------------------------------
#
# ci.yml now runs `git diff -z --name-status`, whose NUL-separated FIELD stream has no
# C-quoting at all — there is simply no quoted-text format left to mis-parse. The CLI
# re-frames that stream into records; these lock both directions of that framing.


def _run_z(author, payload, tmp_path):
    typer_testing = pytest.importorskip("typer.testing")
    from atv_bench.cli import app
    f = tmp_path / "changes.txt"
    f.write_bytes(payload)
    return typer_testing.CliRunner().invoke(
        app, ["validate-pr-paths", "--author", author,
              "--name-status", "--paths-file", str(f)]
    )


def test_cli_z_framing_blocks_accented_league_delete(tmp_path):
    """The exact bytes `git diff -z --name-status` emits for deleting an accented
    entrant's bot must exit non-zero."""
    res = _run_z("attacker",
                 "D\0league/submissions/café/main.py\0".encode(), tmp_path)
    assert res.exit_code != 0, res.output


def test_cli_z_framing_accepts_own_submission(tmp_path):
    """Positive control: a normalized own-path submission in -z form still passes, so
    the framing did not simply reject everything."""
    res = _run_z("entrant", (
        "A\0league/submissions/entrant/main.py\0"
        "M\0league/submissions/entrant/submission.json\0"
    ).encode(), tmp_path)
    assert res.exit_code == 0, res.output


def test_cli_z_framing_consumes_both_sides_of_a_rename(tmp_path):
    """R/C records carry TWO path fields; mis-counting them would desynchronize the
    whole stream and turn a following path into a bogus status."""
    res = _run_z("attacker", (
        "R100\0league/submissions/victim/main.py\0"
        "league/submissions/attacker/main.py\0"
    ).encode(), tmp_path)
    assert res.exit_code != 0, res.output


def test_changes_copy_status_into_league_rejected():
    """C* (copy) had zero coverage. A copy can duplicate another entrant's bytes into
    your directory just as a rename can, so it must be blocked against league/**."""
    res = validate_pr_changes("attacker", [
        "C100\tleague/submissions/victim/main.py\tleague/submissions/attacker/main.py",
    ])
    assert res["ok"] is False, res


def test_changes_copy_status_c_quoted_league_rejected():
    """Copy + C-quoting combined — the two bypasses must not compose into an allow."""
    res = validate_pr_changes("attacker", [
        'C100\t"league/submissions/caf\\303\\251/main.py"'
        '\tleague/submissions/attacker/main.py',
    ])
    assert res["ok"] is False, res


# --- classifier normalization (santa round 2, reviewer B CRITICAL) ------------------
#
# Normalizing ONLY the R/C/D gate left the submission-PR CLASSIFIER fail-open: a PR
# spelling its own submission `League/...` or `./league/...` was not recognized as a
# submission PR, so validate_pr_paths never ran and it could edit anything — including
# .github/workflows/**, the pwn-request vector. Both must share one normalization.


@pytest.mark.parametrize("spelling", [
    "League/submissions/attacker/main.py",
    "./league/submissions/attacker/main.py",
    "LEAGUE/SUBMISSIONS/attacker/main.py",
    '"league/submissions/caf\\303\\251/main.py"',
])
def test_changes_noncanonical_submission_is_still_classified(spelling):
    """A non-canonically spelled submission path must still set is_submission_pr, so the
    PR gets confined rather than sailing past as 'plumbing'."""
    res = validate_pr_changes("attacker", [f"A\t{spelling}"])
    assert res["is_submission_pr"] is True, res


def test_changes_case_variant_submission_pr_cannot_edit_workflow():
    """The pwn-request vector: spelling your own submission `League/` used to skip
    classification entirely, leaving the PR free to rewrite the workflow that scores it."""
    res = validate_pr_changes("attacker", [
        "A\tLeague/submissions/attacker/main.py",
        "M\t.github/workflows/ci.yml",
    ])
    assert res["is_submission_pr"] is True, res
    assert res["ok"] is False, res


def test_changes_dot_slash_submission_pr_cannot_edit_matches():
    res = validate_pr_changes("attacker", [
        "M\t./league/submissions/attacker/submission.json",
        "M\tleague/matches.jsonl",
    ])
    assert res["ok"] is False, res


def test_changes_mixed_case_author_own_files_still_allowed():
    """Normalization is for CLASSIFICATION only — confinement still compares the real
    path, so an author whose login has capitals is not wrongly rejected."""
    res = validate_pr_changes("MixedCase", [
        "A\tleague/submissions/MixedCase/main.py",
        "M\tleague/submissions/MixedCase/submission.json",
    ])
    assert res["ok"] is True, res


def test_cli_z_truncated_rename_record_fails_closed(tmp_path):
    """A truncated R/C record (one path where two are required) must fail closed; it
    was previously reframed as a one-path rename outside league/** and accepted."""
    res = _run_z("attacker", "R100\0docs/stale.md\0".encode(), tmp_path)
    assert res.exit_code != 0, res.output


def test_cli_z_tab_containing_path_fails_closed(tmp_path):
    """-z exists to carry paths a tab-delimited format cannot represent; rather than
    silently corrupt one, the gate rejects it."""
    res = _run_z("attacker", "A\0league/submissions/a/ma\tin.py\0".encode(), tmp_path)
    assert res.exit_code != 0, res.output


# --- normalization completeness (santa round 3) ------------------------------------
#
# Peeling a literal "./" prefix handled ./league/… but NOT these, each of which names
# the same tree. posixpath.normpath + lstrip("/") collapses the whole family at once.


@pytest.mark.parametrize("spelling", [
    ".//league/matches.jsonl",
    "././league/matches.jsonl",
    "/league/matches.jsonl",
    "league/./matches.jsonl",
    "league/submissions/x/../../matches.jsonl",
])
def test_changes_noncanonical_league_delete_rejected(spelling):
    res = validate_pr_changes("attacker", [f"D\t{spelling}"])
    assert res["ok"] is False, res


def test_changes_double_slash_dot_submission_pr_cannot_edit_workflow():
    """`.//league/...` defeated the ./-peeling classifier, so the PR skipped confinement
    and could rewrite the workflow that scores it."""
    res = validate_pr_changes("attacker", [
        "A\t.//league/submissions/attacker/main.py",
        "M\t.github/workflows/ci.yml",
    ])
    assert res["is_submission_pr"] is True, res
    assert res["ok"] is False, res


@pytest.mark.parametrize("filename", ["main.py ", " main.py"])
def test_changes_whitespace_padded_filename_rejected(filename):
    """A leading/trailing space is a LEGAL filename byte that git emits verbatim.
    Stripping it let `main.py ` — a different file — satisfy the allowlist.

    (A TAB cannot be tested here: it is the field separator of this format, so a
    tab-bearing path is unrepresentable in it. That is exactly why CI uses -z, and why
    the -z framing rejects a tab outright — see test_cli_z_tab_containing_path_fails_closed.)
    """
    res = validate_pr_changes("attacker", [
        f"A\tleague/submissions/attacker/{filename}",
    ])
    assert res["ok"] is False, res


def test_decode_does_not_strip_meaningful_whitespace():
    from atv_bench.validate import _decode_status_path
    assert _decode_status_path("league/submissions/a/main.py ") == \
        "league/submissions/a/main.py "


def test_cli_invalid_utf8_z_payload_does_not_crash(tmp_path):
    """git pathnames are arbitrary bytes. A strict-UTF-8 read raised UnicodeDecodeError —
    a traceback, not a verdict. The gate must return a controlled rejection instead."""
    res = _run_z("attacker", b"D\0league/submissions/\xff\xfe/main.py\0", tmp_path)
    assert res.exit_code != 0, res.output
    assert not isinstance(res.exception, UnicodeDecodeError), res.exception
