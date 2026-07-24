"""T2-frames: per-game frame extractors + traversal-safe round extraction."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from atv_bench.frames import (
    AntsSim,
    FrameParseError,
    LightcyclesSim,
    extract_round,
    parse_ants_sim,
    parse_lightcycles_sim,
    parse_pgn,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rounds"
ANTS = FIXTURES / "ants-0_round_0.tar.gz"
LIGHTCYCLES = FIXTURES / "lightcycles-2_round_0.tar.gz"
CHESS = FIXTURES / "chess-1_round_0.tar.gz"


def _member(tar_path: Path, name: str) -> bytes:
    with tarfile.open(tar_path) as t:
        return t.extractfile(name).read()


def test_parse_ants_sim_yields_frames():
    raw = _member(ANTS, "0/sim_0.json")
    sim = parse_ants_sim(raw)
    assert sim.game == "ants"
    assert sim.rows == 32 and sim.cols == 32
    assert sim.names == ["claude-code", "bare-claude-code"]
    assert sim.winner == 0
    assert len(sim.frames) == 501
    f0 = sim.frames[0]
    assert f0.t == 0
    assert f0.ants[0] == (10, 9, 0)
    assert f0.hills and f0.food


def test_parse_lightcycles_sim_yields_frames_with_trails():
    raw = _member(LIGHTCYCLES, "0/sim_0.json")
    sim = parse_lightcycles_sim(raw)
    assert sim.game == "lightcycles"
    assert sim.width == 48 and sim.height == 36
    assert sim.winner == 0
    assert len(sim.frames) == 323
    # heads accumulate into per-player trails over frames.
    first = sim.frames[0]
    later = sim.frames[5]
    assert len(first.heads) == 2
    # trail grows monotonically as frames advance.
    assert len(later.trails[0]) > len(first.trails[0])
    assert first.heads[0] in first.trails[0]


def test_parse_pgn_yields_games_with_fen_per_ply():
    raw = _member(CHESS, "0/match_0.pgn")
    games = parse_pgn(raw)
    # match_0.pgn holds two games (colors swap between them).
    assert len(games) == 2
    g0 = games[0]
    assert g0.white == "claude-code"
    assert g0.black == "bare-claude-code"
    assert g0.result == "1-0"
    assert g0.winner_index == 0
    assert g0.winner_name == "claude-code"
    assert all(isinstance(f, str) and " " in f for f in g0.fens)
    # Second game swaps colors: bare plays White.
    g1 = games[1]
    assert g1.white == "bare-claude-code"
    assert g1.black == "claude-code"


def test_pgn_winner_maps_through_names_when_colors_swap():
    # A White win by bare-claude-code must resolve to seat index 1, not 0.
    pgn = (
        b'[White "bare-claude-code"]\n[Black "claude-code"]\n[Result "1-0"]\n\n'
        b'1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0\n'
    )
    (game,) = parse_pgn(pgn)
    assert game.result == "1-0"
    assert game.winner_name == "bare-claude-code"
    assert game.winner_index == 1


def test_extract_round_autodetects_ants_sim():
    rnd = extract_round(ANTS)
    assert rnd.game == "ants"
    # 10 sim files per game, all parsed and ordered by member index.
    assert len(rnd.sims) == 10
    assert all(isinstance(s, AntsSim) for s in rnd.sims)
    assert len(rnd.sims[0].frames) == 501


def test_extract_round_autodetects_lightcycles_sim():
    rnd = extract_round(LIGHTCYCLES)
    assert rnd.game == "lightcycles"
    assert len(rnd.sims) == 10
    assert all(isinstance(s, LightcyclesSim) for s in rnd.sims)


def test_extract_round_autodetects_chess_pgn():
    rnd = extract_round(CHESS)
    assert rnd.game == "chess"
    # 2 PGN files x 2 games each = 4 chess games.
    assert len(rnd.games) == 4
    assert all(g.result in ("1-0", "0-1", "1/2-1/2") for g in rnd.games)
    assert all(len(g.fens) > 0 for g in rnd.games)


def test_malformed_json_raises_clean_error():
    with pytest.raises(FrameParseError):
        parse_ants_sim(b"{not valid json")


def test_empty_pgn_raises_clean_error():
    with pytest.raises(FrameParseError):
        parse_pgn(b"   \n  ")


def _make_tar_with_member(name: str, data: bytes, out_dir: Path) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    out = out_dir / "_evil.tar.gz"
    out.write_bytes(buf.getvalue())
    return out


def test_tar_member_with_dotdot_is_refused(tmp_path):
    payload = json.dumps({"rows": 1, "cols": 1, "num_players": 2,
                          "water": [], "frames": [], "names": ["a", "b"],
                          "winner": 0}).encode()
    evil = _make_tar_with_member("../escape/sim_0.json", payload, tmp_path)
    with pytest.raises(FrameParseError):
        extract_round(evil)


# ===========================================================================
# T3-harden: full edge coverage over the merged T1/T2 implementation.
# ===========================================================================

# --- T3: chess SAN edge cases (castling / promotion / en-passant / disambig) --
# Expected final FENs computed independently with python-chess; we assert the
# ply we depend on so a regression in the SAN->FEN path (not just "it parsed")
# is caught.

def _one_game(pgn: bytes):
    games = parse_pgn(pgn)
    assert len(games) == 1
    return games[0]


def test_chess_castling_kingside_updates_fen():
    pgn = (b'[White "claude-code"]\n[Black "bare-claude-code"]\n[Result "1-0"]\n\n'
           b'1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. O-O Nf6 1-0\n')
    g = _one_game(pgn)
    # After 4.O-O white king is on g1, rook on f1.
    assert g.fens[-1] == \
        "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 w kq - 6 5"


def test_chess_en_passant_captures_correctly():
    pgn = (b'[White "claude-code"]\n[Black "bare-claude-code"]\n[Result "1-0"]\n\n'
           b'1. e4 a6 2. e5 d5 3. exd6 1-0\n')
    g = _one_game(pgn)
    # 3.exd6 e.p. removes the black d5 pawn and lands white pawn on d6.
    assert g.fens[-1] == \
        "rnbqkbnr/1pp1pppp/p2P4/8/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 3"


def test_chess_promotion_to_queen_updates_fen():
    pgn = (b'[White "claude-code"]\n[Black "bare-claude-code"]\n[Result "1-0"]\n\n'
           b'1. b4 a5 2. bxa5 Nc6 3. a6 Rb8 4. axb7 Nd4 5. bxc8=Q 1-0\n')
    g = _one_game(pgn)
    # 5.bxc8=Q promotes on c8; a Q must sit on c8.
    assert g.fens[-1].startswith("1rQqkbnr/")
    assert g.fens[-1] == "1rQqkbnr/2pppppp/8/8/3n4/8/P1PPPPPP/RNBQKBNR b KQk - 0 5"


def test_chess_disambiguation_resolves_correct_knight():
    pgn = (b'[White "claude-code"]\n[Black "bare-claude-code"]\n[Result "1-0"]\n\n'
           b'1. Nf3 Nf6 2. Nc3 Nc6 3. Nd5 1-0\n')
    g = _one_game(pgn)
    # 3.Nd5 (the c3 knight, not the f3 knight) -> knight on d5, other on f3.
    assert g.fens[-1] == \
        "r1bqkb1r/pppppppp/2n2n2/3N4/8/5N2/PPPPPPPP/R1BQKB1R b KQkq - 5 3"


# --- T3: malformed chess artifacts raise a clean, caught FrameParseError -----

def test_malformed_pgn_illegal_move_raises_clean_error():
    # An illegal first move yields a game with no legal plies -> clean error.
    with pytest.raises(FrameParseError):
        parse_pgn(b'[Result "*"]\n\n1. zz9 e5 *\n')


def test_pgn_headers_only_no_moves_raises_clean_error():
    with pytest.raises(FrameParseError):
        parse_pgn(b'[White "claude-code"]\n[Black "bare-claude-code"]\n[Result "*"]\n\n*\n')


def test_binary_garbage_pgn_raises_clean_error():
    with pytest.raises(FrameParseError):
        parse_pgn(b"\x00\x01\x02not a pgn at all")


# --- T3: malformed sim artifacts raise a clean, caught FrameParseError -------

def test_malformed_ants_sim_missing_keys_raises_clean_error():
    with pytest.raises(FrameParseError):
        parse_ants_sim(json.dumps({"frames": [{"t": 0}]}).encode())  # no rows/cols


def test_malformed_lightcycles_sim_bad_frame_raises_clean_error():
    from atv_bench.frames import parse_lightcycles_sim
    bad = json.dumps({"width": 5, "height": 5, "num_players": 2,
                      "rocks": [], "names": ["a", "b"], "winner": 0,
                      "frames": [{"t": 0}]}).encode()  # frame missing 'heads'
    with pytest.raises(FrameParseError):
        parse_lightcycles_sim(bad)


def test_non_dict_json_raises_clean_error():
    with pytest.raises(FrameParseError):
        parse_ants_sim(b"[1, 2, 3]")


# --- T3: extract_round traversal / structure safety -------------------------

def test_extract_round_absolute_member_path_is_refused(tmp_path):
    payload = json.dumps({"rows": 1, "cols": 1, "num_players": 2, "water": [],
                          "frames": [], "names": ["a", "b"], "winner": 0}).encode()
    evil = _make_tar_with_member("/etc/sim_0.json", payload, tmp_path)
    with pytest.raises(FrameParseError):
        extract_round(evil)


def test_extract_round_symlink_member_is_refused(tmp_path):
    # A symlink member is a traversal vector even without '..' in the name.
    data = b"whatever"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo(name="sim_0.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../../etc/passwd"
        t.addfile(info)
    evil = tmp_path / "sym.tar.gz"
    evil.write_bytes(buf.getvalue())
    with pytest.raises(FrameParseError):
        extract_round(evil)


def test_extract_round_no_recognizable_member_raises_clean_error(tmp_path):
    evil = tmp_path / "empty.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        info = tarfile.TarInfo(name="README.txt")
        blob = b"nothing to parse here"
        info.size = len(blob)
        t.addfile(info, io.BytesIO(blob))
    with pytest.raises(FrameParseError):
        extract_round(evil)


def test_extract_round_corrupt_tar_raises_clean_error(tmp_path):
    evil = tmp_path / "corrupt.tar.gz"
    evil.write_bytes(b"this is definitely not a gzip tarball")
    with pytest.raises(FrameParseError):
        extract_round(evil)


# --- HIGH: bounded tar reads (OOM / zip-bomb guards) ------------------------


def test_extract_round_rejects_too_many_members(tmp_path):
    """A tar with more than MAX_TAR_MEMBERS members is refused before reading."""
    from atv_bench.frames import MAX_TAR_MEMBERS

    evil = tmp_path / "many.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        blob = b"{}"
        for i in range(MAX_TAR_MEMBERS + 5):
            info = tarfile.TarInfo(name=f"sim_{i}.json")
            info.size = len(blob)
            t.addfile(info, io.BytesIO(blob))
    with pytest.raises(FrameParseError):
        extract_round(evil)


def test_safe_members_streams_and_caps_without_getmembers(tmp_path, monkeypatch):
    """HIGH: the member cap must trip via streaming (tar.next()), refusing before
    the WHOLE archive is indexed. Fail loudly if _safe_members ever falls back to
    tar.getmembers() (which materializes every TarInfo in memory first)."""
    import atv_bench.frames as frames_mod

    monkeypatch.setattr(frames_mod, "MAX_TAR_MEMBERS", 8)
    evil = tmp_path / "many.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        blob = b"{}"
        for i in range(64):  # far more than the cap
            info = tarfile.TarInfo(name=f"sim_{i}.json")
            info.size = len(blob)
            t.addfile(info, io.BytesIO(blob))

    with tarfile.open(evil) as t:
        def _boom():
            raise AssertionError("getmembers() must not be called: indexes whole tar")
        monkeypatch.setattr(t, "getmembers", _boom)
        with pytest.raises(FrameParseError):
            frames_mod._safe_members(t)


def test_results_member_rejects_basename_spoof(tmp_path):
    """MEDIUM: a member whose basename is results.json but lives under a non-numeric
    dir (fake/results.json) must NOT be accepted as the round results — only the
    canonical <n>/results.json qualifies."""
    from atv_bench.frames import read_round, RoundIncomplete

    evil = tmp_path / "spoof.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        fake = json.dumps({"winner": "bare-claude-code"}).encode()
        info = tarfile.TarInfo(name="fake/results.json")
        info.size = len(fake)
        t.addfile(info, io.BytesIO(fake))
        sim = json.dumps({"rows": 1, "cols": 1, "num_players": 2, "water": [],
                          "names": ["a", "b"], "winner": 0,
                          "frames": [{"t": 0}]}).encode()
        sinfo = tarfile.TarInfo(name="0/sim_0.json")
        sinfo.size = len(sim)
        t.addfile(sinfo, io.BytesIO(sim))
    # No canonical <n>/results.json -> treated as incomplete, never publishes a
    # spoofed winner.
    with pytest.raises(RoundIncomplete):
        read_round(evil)


def test_results_member_rejects_earlier_spoof_before_real(tmp_path):
    """MEDIUM: an adversarial tar placing fake/results.json BEFORE the real
    0/results.json must not let the fake win. The real canonical member is used;
    a fake basename match is ignored entirely."""
    from atv_bench.frames import read_round

    evil = tmp_path / "spoof2.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        fake = json.dumps({"winner": "bare-claude-code"}).encode()
        finfo = tarfile.TarInfo(name="fake/results.json")  # earlier member
        finfo.size = len(fake)
        t.addfile(finfo, io.BytesIO(fake))
        real = json.dumps({"winner": "claude-code"}).encode()
        rinfo = tarfile.TarInfo(name="0/results.json")
        rinfo.size = len(real)
        t.addfile(rinfo, io.BytesIO(real))
        sim = json.dumps({"rows": 1, "cols": 1, "num_players": 2, "water": [],
                          "names": ["a", "b"], "winner": 0, "frames": []}).encode()
        sinfo = tarfile.TarInfo(name="0/sim_0.json")
        sinfo.size = len(sim)
        t.addfile(sinfo, io.BytesIO(sim))
    _, results = read_round(evil)
    assert results["winner"] == "claude-code"  # the real member, not the spoof


def test_extract_round_rejects_oversized_member(tmp_path, monkeypatch):
    """A single member whose size exceeds the per-member cap is refused. Cap is
    lowered so the test needn't materialize 64 MiB on disk."""
    import atv_bench.frames as frames_mod
    monkeypatch.setattr(frames_mod, "MAX_MEMBER_BYTES", 64)

    evil = tmp_path / "big.tar.gz"
    blob = b"x" * 256  # 256 bytes > 64-byte cap
    with tarfile.open(evil, "w:gz") as t:
        info = tarfile.TarInfo(name="sim_0.json")
        info.size = len(blob)
        t.addfile(info, io.BytesIO(blob))
    with pytest.raises(FrameParseError):
        extract_round(evil)


def test_extract_round_rejects_total_extract_budget(tmp_path, monkeypatch):
    """Many members each under the per-member cap but summing past the total
    budget are refused — guards against death-by-a-thousand-members OOM."""
    import atv_bench.frames as frames_mod
    monkeypatch.setattr(frames_mod, "MAX_MEMBER_BYTES", 256)
    monkeypatch.setattr(frames_mod, "MAX_TOTAL_BYTES", 512)

    evil = tmp_path / "sum.tar.gz"
    blob = b"x" * 200  # under per-member cap; 4 members exceed the 512 total
    with tarfile.open(evil, "w:gz") as t:
        for i in range(4):
            info = tarfile.TarInfo(name=f"sim_{i}.json")
            info.size = len(blob)
            t.addfile(info, io.BytesIO(blob))
    with pytest.raises(FrameParseError):
        extract_round(evil)
