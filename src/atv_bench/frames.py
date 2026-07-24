"""T2-frames: pure per-game frame extractors.

Parses round tarballs (`ants` / `lightcycles` sim JSON, `chess` PGN) into
immutable frame structures. Extraction is traversal-safe: any tar member whose
resolved path escapes the archive root is refused before it is read.

Implemented against tests/fixtures/CONTRACT.md (the plan's prose guessed the
schema wrong — this follows the captured reality).

A round tarball contains *many* artifacts (10 `sim_N.json` for ants/lightcycles,
multiple `match_N.pgn` each holding multiple games for chess). `extract_round`
parses all of them, sorted by numeric member index, into `Round.sims` /
`Round.games` — never just the first artifact.
"""
from __future__ import annotations

import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


class FrameParseError(Exception):
    """A round artifact could not be parsed or is unsafe to extract."""


class RoundIncomplete(FrameParseError):
    """The tar opened cleanly but carries no results.json member yet.

    Distinct from a malformed/corrupt tar: a round still being written opens but
    lacks its results member, and must be *retried* (not cached as a permanent
    failure) once the arena finishes writing.
    """


# --- extraction bounds (OOM / zip-bomb guards) ------------------------------
# A malicious round tarball could carry millions of members or a single member
# that inflates to gigabytes. Cap member count, per-member size, and cumulative
# extracted bytes so a hostile tar is rejected before it exhausts memory.
MAX_TAR_MEMBERS = 4096
MAX_MEMBER_BYTES = 64 * 1024 * 1024        # 64 MiB per member
MAX_TOTAL_BYTES = 256 * 1024 * 1024        # 256 MiB extracted per round


# --- seat convention --------------------------------------------------------
# Contract: index 0 = claude-code (HARNESS / blue / --a);
#           index 1 = bare-claude-code (CONTROL / red / --b).
_CANONICAL_SEATS = ("claude-code", "bare-claude-code")


def canonical_seat_index(name: str) -> int:
    """Map a player name to its canonical seat index (0 or 1).

    Raises FrameParseError for a name outside the known seat convention.
    """
    try:
        return _CANONICAL_SEATS.index(name)
    except ValueError as exc:
        raise FrameParseError(
            f"unknown player name for seat convention: {name!r}"
        ) from exc


# --- ants -------------------------------------------------------------------

Ant = Tuple[int, int, int]  # x, y, player
Hill = Tuple[int, int, int]
Food = Tuple[int, int]


@dataclass(frozen=True)
class AntsFrame:
    t: int
    ants: tuple[Ant, ...]
    hills: tuple[Hill, ...]
    food: tuple[Food, ...]


@dataclass(frozen=True)
class AntsSim:
    game: str
    rows: int
    cols: int
    num_players: int
    water: tuple[tuple[int, int], ...]
    names: list[str]
    winner: int | None
    frames: tuple[AntsFrame, ...]


def parse_ants_sim(raw: bytes) -> AntsSim:
    """Parse an ants `sim_N.json` payload into immutable frames."""
    data = _load_json(raw)
    try:
        frames = tuple(
            AntsFrame(
                t=int(f["t"]),
                ants=tuple(tuple(a) for a in f.get("ants", [])),
                hills=tuple(tuple(h) for h in f.get("hills", [])),
                food=tuple(tuple(x) for x in f.get("food", [])),
            )
            for f in data["frames"]
        )
        return AntsSim(
            game="ants",
            rows=int(data["rows"]),
            cols=int(data["cols"]),
            num_players=int(data["num_players"]),
            water=tuple(tuple(w) for w in data.get("water", [])),
            names=list(data["names"]),
            winner=_opt_int(data["winner"]),
            frames=frames,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FrameParseError(f"malformed ants sim: {exc}") from exc


# --- lightcycles ------------------------------------------------------------

Head = Tuple[int, int, int]


@dataclass(frozen=True)
class LightcyclesFrame:
    t: int
    heads: tuple[Head, ...]
    # per-player accumulated trail of visited head positions up to this frame.
    trails: tuple[tuple[Head, ...], ...]


@dataclass(frozen=True)
class LightcyclesSim:
    game: str
    width: int
    height: int
    num_players: int
    rocks: tuple[tuple[int, int], ...]
    names: list[str]
    winner: int | None
    frames: tuple[LightcyclesFrame, ...]


def parse_lightcycles_sim(raw: bytes) -> LightcyclesSim:
    """Parse a lightcycles `sim_N.json`; derive trails by accumulating heads."""
    data = _load_json(raw)
    try:
        num_players = int(data["num_players"])
        accum: list[list[Head]] = [[] for _ in range(num_players)]
        frames: list[LightcyclesFrame] = []
        for f in data["frames"]:
            heads = tuple(tuple(h) for h in f["heads"])
            for player, head in enumerate(heads):
                if player < num_players:
                    accum[player].append(head)
            frames.append(
                LightcyclesFrame(
                    t=int(f["t"]),
                    heads=heads,
                    trails=tuple(tuple(p) for p in accum),
                )
            )
        return LightcyclesSim(
            game="lightcycles",
            width=int(data["width"]),
            height=int(data["height"]),
            num_players=num_players,
            rocks=tuple(tuple(r) for r in data.get("rocks", [])),
            names=list(data["names"]),
            winner=_opt_int(data["winner"]),
            frames=tuple(frames),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FrameParseError(f"malformed lightcycles sim: {exc}") from exc


# --- chess ------------------------------------------------------------------

# PGN result -> which color scored the win ("white"/"black"/None for a draw).
_RESULT_COLOR = {"1-0": "white", "0-1": "black", "1/2-1/2": None}


@dataclass(frozen=True)
class ChessGame:
    game: str
    white: str
    black: str
    result: str
    # Canonical seat index of the winner (0 or 1), independent of color.
    winner_index: int | None
    # Canonical seat name of the winner, None for a draw.
    winner_name: str | None
    fens: tuple[str, ...]


def _chess_game_from_node(node) -> ChessGame:
    board = node.board()
    fens: list[str] = []
    for move in node.mainline_moves():
        board.push(move)
        fens.append(board.fen())
    if not fens:
        raise FrameParseError("PGN game has no moves")
    result = node.headers.get("Result", "*")
    white = node.headers.get("White", "")
    black = node.headers.get("Black", "")
    win_color = _RESULT_COLOR.get(result)
    if win_color is None:
        winner_name: str | None = None
        winner_index: int | None = None
    else:
        winner_name = white if win_color == "white" else black
        winner_index = canonical_seat_index(winner_name)
    return ChessGame(
        game="chess",
        white=white,
        black=black,
        result=result,
        winner_index=winner_index,
        winner_name=winner_name,
        fens=tuple(fens),
    )


def parse_pgn(raw: bytes) -> tuple[ChessGame, ...]:
    """Parse a PGN (possibly multi-game) into a tuple of ChessGame.

    Winner is mapped through the White/Black player names to a canonical seat
    index, so a color swap (bare-claude-code playing White) is scored correctly.

    python-chess is an optional dependency; import it lazily with a clear error.
    """
    try:
        import chess.pgn
    except ImportError as exc:  # pragma: no cover - environment guard
        raise FrameParseError(
            "parsing chess PGN requires the optional 'chess' dependency "
            "(pip install 'chess>=1.9')"
        ) from exc

    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise FrameParseError("empty PGN")
    stream = _io_from(text)
    games: list[ChessGame] = []
    while True:
        node = chess.pgn.read_game(stream)
        if node is None:
            break
        games.append(_chess_game_from_node(node))
    if not games:
        raise FrameParseError("PGN contained no game")
    return tuple(games)


def _io_from(text: str):
    import io

    return io.StringIO(text)


# --- round extraction -------------------------------------------------------


@dataclass(frozen=True)
class Round:
    game: str
    # All sims (ants/lightcycles) parsed from the tarball, ordered by member index.
    sims: tuple[AntsSim | LightcyclesSim, ...] = ()
    # All chess games parsed from the tarball, ordered by (file index, game order).
    games: tuple[ChessGame, ...] = ()


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Return members, refusing any that is unsafe to extract.

    Streams members via `tar.next()` (never `tar.getmembers()`, which would index
    the *entire* archive into memory before any cap could trip). The member cap is
    enforced as we go, so a tar declaring millions of members is refused after
    reading only MAX_TAR_MEMBERS+1 headers — not after fully indexing it.

    Rejects path traversal / links, an oversized member, or a member count or
    cumulative declared size that would risk OOM on a malicious tarball.
    """
    safe: list[tarfile.TarInfo] = []
    total = 0
    while True:
        m = tar.next()
        if m is None:
            break
        if len(safe) >= MAX_TAR_MEMBERS:
            raise FrameParseError(
                f"refusing tar with too many members (> {MAX_TAR_MEMBERS})"
            )
        name = m.name
        if name.startswith("/") or ".." in Path(name).parts or m.islnk() or m.issym():
            raise FrameParseError(f"refusing unsafe tar member: {name!r}")
        if m.size > MAX_MEMBER_BYTES:
            raise FrameParseError(
                f"refusing oversized tar member: {name!r} ({m.size} bytes)"
            )
        total += m.size
        if total > MAX_TOTAL_BYTES:
            raise FrameParseError(
                f"refusing tar exceeding total extract budget (> {MAX_TOTAL_BYTES} bytes)"
            )
        safe.append(m)
    return safe


def _read(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    f = tar.extractfile(member)
    if f is None:
        raise FrameParseError(f"cannot read tar member: {member.name!r}")
    # Bound the actual read: a member whose declared size lies about its true
    # (inflated) content is truncated at the cap rather than read unbounded.
    data = f.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise FrameParseError(
            f"tar member exceeds size cap: {member.name!r} (> {MAX_MEMBER_BYTES} bytes)"
        )
    return data


def _member_index(name: str) -> int:
    """Numeric index embedded in a member basename (e.g. sim_7.json -> 7)."""
    match = re.search(r"(\d+)", Path(name).stem)
    return int(match.group(1)) if match else 0


def _parse_members(tar: tarfile.TarFile, members: list[tarfile.TarInfo]) -> Round:
    """Autodetect the game from `members` and parse every artifact once."""
    sim_members = sorted(
        (m for m in members
         if Path(m.name).name.startswith("sim_") and m.name.endswith(".json")),
        key=lambda m: _member_index(m.name),
    )
    pgn_members = sorted(
        (m for m in members if m.name.endswith(".pgn")),
        key=lambda m: _member_index(m.name),
    )

    if sim_members:
        probe = _load_json(_read(tar, sim_members[0]))
        if "rows" in probe and "cols" in probe:
            parser, game = parse_ants_sim, "ants"
        elif "width" in probe and "height" in probe:
            parser, game = parse_lightcycles_sim, "lightcycles"
        else:
            raise FrameParseError("unrecognized sim schema")
        sims = tuple(parser(_read(tar, m)) for m in sim_members)
        return Round(game=game, sims=sims)

    if pgn_members:
        games: list[ChessGame] = []
        for m in pgn_members:
            games.extend(parse_pgn(_read(tar, m)))
        return Round(game="chess", games=tuple(games))

    raise FrameParseError("no sim_*.json or *.pgn member found in round")


def extract_round(tar_path: str | Path) -> Round:
    """Open a round tarball, autodetect the game, and parse every artifact.

    Traversal-safe: every member is path-checked before any read.
    """
    try:
        with tarfile.open(tar_path) as tar:
            return _parse_members(tar, _safe_members(tar))
    except tarfile.TarError as exc:
        raise FrameParseError(f"corrupt round tarball: {exc}") from exc


def read_round(tar_path: str | Path) -> Tuple[Round, dict]:
    """Open the tar ONCE and return `(Round, results_dict)` together.

    The watcher's per-poll cost was three separate opens (a has-results probe
    plus a parse open plus a results-read open). This does the whole gate-and-parse
    in a single pass over one open handle.

    Raises `RoundIncomplete` (a FrameParseError subclass) when the tar opens but
    has no `<n>/results.json` member yet — the caller may retry that case when the
    file changes. Any other FrameParseError is a permanent, cache-able failure.
    """
    try:
        with tarfile.open(tar_path) as tar:
            members = _safe_members(tar)
            member = _results_member(members)
            if member is None:
                raise RoundIncomplete("round tar has no results.json member yet")
            results = _load_json(_read(tar, member))
            rnd = _parse_members(tar, members)
            return rnd, results
    except tarfile.TarError as exc:
        raise FrameParseError(f"corrupt round tarball: {exc}") from exc


# The real arena writes results at `<round_dir>/results.json`, where the round
# dir is the numeric round index (e.g. `0/results.json`, `2/results.json`). A
# member whose basename merely *ends* in results.json (e.g. `fake/results.json`)
# is a spoof and must not be accepted.
_RESULTS_RE = re.compile(r"^\d+/results\.json$")


def _results_member(members: list[tarfile.TarInfo]) -> tarfile.TarInfo | None:
    """Return the canonical `<n>/results.json` member, or None if absent.

    Only members matching `^\\d+/results.json$` qualify. If several qualify
    (ambiguous archive), refuse rather than silently pick the first — a crafted
    tar with two results members must not be able to publish an arbitrary winner.
    """
    matches = [m for m in members if _RESULTS_RE.match(m.name)]
    if not matches:
        return None
    if len(matches) > 1:
        raise FrameParseError(
            f"ambiguous round tar: multiple results.json members {[m.name for m in matches]!r}"
        )
    return matches[0]


def _load_json(raw: bytes) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FrameParseError(f"invalid JSON: {exc}") from exc


def _opt_int(value) -> int | None:
    """Coerce a winner field to int, preserving None for draws."""
    return None if value is None else int(value)
