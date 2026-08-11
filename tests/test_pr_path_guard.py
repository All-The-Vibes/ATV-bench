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


def test_cli_z_tab_containing_maintainer_path_still_allowed(tmp_path):
    """A tab is LEGAL in a git pathname, and -z exists to carry it unambiguously.
    Rejecting it outright would fail a legitimate maintainer PR on the always-on gate,
    so -z records stay structured instead of being re-joined with tabs."""
    res = _run_z("maintainer", b"D\0docs/release\tnotes.md\0", tmp_path)
    assert res.exit_code == 0, res.output


def test_cli_z_tab_containing_rename_outside_league_allowed(tmp_path):
    res = _run_z("maintainer",
                 b"R100\0src/old\tname.py\0src/new\tname.py\0", tmp_path)
    assert res.exit_code == 0, res.output


def test_cli_z_tab_containing_league_delete_still_blocked(tmp_path):
    """Carrying the tab through must not weaken the gate: a tab-named league file is
    still league."""
    res = _run_z("attacker", b"D\0league/mat\tches.jsonl\0", tmp_path)
    assert res.exit_code != 0, res.output


def test_changes_accepts_structured_records():
    """validate_pr_changes takes either a raw tab-delimited line or an already-split
    (status, *paths) record — the latter is how the -z path avoids tab round-tripping."""
    assert validate_pr_changes(
        "entrant", [("A", "league/submissions/entrant/main.py")]
    )["ok"] is True
    assert validate_pr_changes(
        "attacker", [("D", "league/mat\tches.jsonl")]
    )["ok"] is False


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
    the -z framing rejects a tab outright — see test_cli_z_tab_containing_league_delete_still_blocked.)
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


# --- record arity + strict -z framing (santa round 3, reviewer B) -------------------
#
# Arity was enforced only in the CLI's -z framing, so validate_pr_changes — a PUBLIC
# function that also consumes raw --name-status text — still accepted malformed records.


def test_changes_rename_missing_destination_fails_closed():
    """`R100\tdocs/old.md` is a rename with no destination. It was accepted as ok."""
    res = validate_pr_changes("maintainer", ["R100\tdocs/old.md"])
    assert res["ok"] is False, res


def test_changes_extra_path_field_fails_closed():
    res = validate_pr_changes("maintainer", [("D", "docs/stale.md", "EXTRA")])
    assert res["ok"] is False, res


def test_changes_non_string_field_rejected_not_filtered():
    """Filtering a non-string field would silently turn a malformed record into a
    well-formed-looking one."""
    res = validate_pr_changes("maintainer", [("D", None)])
    assert res["ok"] is False, res


def test_changes_copy_missing_destination_fails_closed():
    res = validate_pr_changes("maintainer", ["C100\tdocs/old.md"])
    assert res["ok"] is False, res


def test_cli_z_stream_missing_terminator_fails_closed(tmp_path):
    """A truncated -z stream (no final NUL) must not be normalized into a valid one."""
    res = _run_z("maintainer", b"A\0docs/stale.md", tmp_path)
    assert res.exit_code != 0, res.output


def test_cli_z_stream_doubled_nul_fails_closed(tmp_path):
    res = _run_z("maintainer", b"A\0\0docs/stale.md\0", tmp_path)
    assert res.exit_code != 0, res.output


# --- empty-field arity + legacy name-only strip (santa round 3 final) ---------------


@pytest.mark.parametrize("record", [
    ("D", "docs/stale.md", ""),
    "D\tdocs/stale.md\t",
    "D\t\tdocs/stale.md",
])
def test_changes_empty_extra_field_fails_closed(record):
    """Filtering empty fields BEFORE the arity check let a malformed record collapse
    into a well-formed one-path record and pass."""
    res = validate_pr_changes("maintainer", [record])
    assert res["ok"] is False, res


def test_legacy_name_only_cli_does_not_strip_path(tmp_path):
    """The legacy --name-only interface stripped each line, so `main.py ` — a different
    file — satisfied the {main.py, submission.json} allowlist."""
    typer_testing = pytest.importorskip("typer.testing")
    from atv_bench.cli import app
    f = tmp_path / "paths.txt"
    f.write_text("league/submissions/attacker/main.py \n")
    res = typer_testing.CliRunner().invoke(
        app, ["validate-pr-paths", "--author", "attacker", "--paths-file", str(f)]
    )
    assert res.exit_code != 0, res.output


def test_legacy_name_only_cli_still_accepts_own_files(tmp_path):
    """Positive control: dropping the strip must not break the ordinary case, including
    a CRLF-terminated file."""
    typer_testing = pytest.importorskip("typer.testing")
    from atv_bench.cli import app
    f = tmp_path / "paths.txt"
    f.write_bytes(b"league/submissions/entrant/main.py\r\n"
                  b"league/submissions/entrant/submission.json\r\n")
    res = typer_testing.CliRunner().invoke(
        app, ["validate-pr-paths", "--author", "entrant", "--paths-file", str(f)]
    )
    assert res.exit_code == 0, res.output


# --- exact status tokens + documented casefold trade-off (santa round 3 final) ------


@pytest.mark.parametrize("status", ["MALFORMED", "MOVE", "AX", "DROP", "Rx", "R1000"])
def test_changes_status_matched_as_exact_token_not_first_char(status):
    """`status[:1]` read `MALFORMED` as a plain modify, so a malformed record naming a
    league path sailed through a fail-closed gate."""
    res = validate_pr_changes(
        "attacker", [f"{status}\tleague/submissions/attacker/main.py"]
    )
    assert res["ok"] is False, res


@pytest.mark.parametrize("status", ["A", "M", "T", "D", "R100", "C75", "R", "C",
                                    "R075", "C068", "R001"])
def test_changes_real_git_status_tokens_accepted(status):
    """Positive control: every token git actually emits must still parse."""
    paths = "src/a.py\tsrc/b.py" if status[:1] in ("R", "C") else "src/a.py"
    res = validate_pr_changes("maintainer", [f"{status}\t{paths}"])
    assert res["ok"] is True, res


def test_changes_non_string_record_fails_closed():
    """A non-string record was silently skipped by a bare `continue`."""
    assert validate_pr_changes("maintainer", [None])["ok"] is False
    assert validate_pr_changes("maintainer", [42])["ok"] is False


def test_case_insensitive_league_match_is_a_deliberate_tradeoff():
    """`LEAGUE/**` is gated even on a case-sensitive checkout. This is intentional: on a
    case-insensitive checkout `League/matches.jsonl` IS `league/matches.jsonl`, so a
    case-sensitive compare is a confirmed bypass (round-1 CRITICAL). The repo has only
    one `league/` tree, and the cost of this choice is a maintainer PR needing review
    rather than forged league history being silently admitted.
    """
    assert validate_pr_changes("maintainer", ["D\tLEAGUE/readme.md"])["ok"] is False
    # ...while genuinely unrelated trees are untouched.
    assert validate_pr_changes("maintainer", ["D\tleagues/readme.md"])["ok"] is True
    assert validate_pr_changes("maintainer", ["D\tdocs/league.md"])["ok"] is True


def test_legacy_name_only_whitespace_only_path_not_dropped(tmp_path):
    """A whitespace-only line IS a legal POSIX pathname. Filtering on ln.strip() dropped
    it before the validator saw it, hiding an outside-tree path and reporting ok."""
    typer_testing = pytest.importorskip("typer.testing")
    from atv_bench.cli import app
    f = tmp_path / "paths.txt"
    f.write_text("league/submissions/attacker/main.py\n   \n")
    res = typer_testing.CliRunner().invoke(
        app, ["validate-pr-paths", "--author", "attacker", "--paths-file", str(f)]
    )
    assert res.exit_code != 0, res.output


def test_legacy_name_only_blank_lines_are_still_ignored(tmp_path):
    """A truly empty line carries no path and is just formatting — it must not turn a
    valid submission into a rejection."""
    typer_testing = pytest.importorskip("typer.testing")
    from atv_bench.cli import app
    f = tmp_path / "paths.txt"
    f.write_text("league/submissions/entrant/main.py\n\n"
                 "league/submissions/entrant/submission.json\n")
    res = typer_testing.CliRunner().invoke(
        app, ["validate-pr-paths", "--author", "entrant", "--paths-file", str(f)]
    )
    assert res.exit_code == 0, res.output


def test_copy_into_own_dir_is_confinement_not_status_dependent():
    """Reviewer B (final round) argued CI must enable `-C` so the C* ban catches a PR
    byte-copying another entrant's bot. Verified against real git and REJECTED as a
    change, because it inverts into a worse failure:

    - Without `-C` (CI today) git reports the copy as `A league/submissions/<you>/main.py`
      — an add of YOUR OWN file. That is precisely what a submission IS, and submissions
      are public files in this repo, so there is nothing confidential to exfiltrate.
    - Copying into ANOTHER entrant's directory is already blocked by path confinement,
      independent of status (asserted below).
    - Enabling `-C --find-copies-harder` makes git report a legitimate newcomer who
      starts from the documented example bot as `C100 docs/example_bot.py ->
      league/submissions/<them>/main.py`. Since C* is banned against league/**, that
      would reject the primary onboarding path for every new entrant.

    The C* ban still matters for the spelling it CAN see (an explicit copy record from a
    client or config that emits one); it is simply not the control that stops content
    reuse. Attribution is a scoring/provenance concern, not a path-guard one.
    """
    # A copy into your own dir arrives as a plain add of your own file — allowed.
    assert validate_pr_changes(
        "attacker", [("A", "league/submissions/attacker/main.py")]
    )["ok"] is True
    # ...but landing it in someone else's dir is blocked no matter the status.
    for record in [("A", "league/submissions/victim/main.py"),
                   ("M", "league/submissions/victim/main.py"),
                   ("C100", "league/submissions/victim/main.py",
                    "league/submissions/attacker/main.py")]:
        assert validate_pr_changes("attacker", [record])["ok"] is False, record


@pytest.mark.parametrize("status", ["A ", " A", "R999", "C999", "M "])
def test_changes_padded_or_impossible_status_rejected(status):
    """git emits no padding and writes a similarity score of 0-100, so `A ` and `R999`
    are not tokens git wrote. Stripping the status normalized padding into a valid
    token; the score bound rejects the impossible ones."""
    paths = "src/a.py\tsrc/b.py" if status.strip()[:1] in ("R", "C") else "src/a.py"
    res = validate_pr_changes("maintainer", [f"{status}\t{paths}"])
    assert res["ok"] is False, res


def test_status_regex_accepts_every_token_real_git_emits(tmp_path):
    """Regression (santa round 3): the score bound was written as 1-2 digits or exactly
    `100`, which rejected `R075` — and git ZERO-PADS the similarity score to three
    digits. This repo's own history contains R075, so a legitimate maintainer rename
    would have failed the enforced CI path.

    Rather than hand-pick forms, drive the assertion from real `git diff --name-status`
    output produced here.
    """
    import subprocess
    from atv_bench.validate import _STATUS_RE
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "a@b.c")
    run("config", "user.name", "t")
    # A partially-modified rename yields a fractional, zero-padded score.
    (repo / "old.py").write_text("\n".join(f"line {i}" for i in range(40)))
    run("add", "-A")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").stdout.strip()
    (repo / "old.py").unlink()
    (repo / "new.py").write_text("\n".join(f"line {i}" for i in range(30)) + "\nextra\n")
    run("add", "-A")
    run("commit", "-qm", "rename")
    out = run("diff", "-M", "--name-status", f"{base}...HEAD").stdout
    tokens = [ln.split("\t")[0] for ln in out.splitlines() if ln.strip()]
    assert tokens, out
    for tok in tokens:
        assert _STATUS_RE.match(tok), f"real git token {tok!r} rejected by _STATUS_RE"
        if tok[:1] in ("R", "C"):
            res = validate_pr_changes("maintainer", [f"{tok}\tsrc/old.py\tsrc/new.py"])
            assert res["ok"] is True, (tok, res)
