"""Section 9 / G-build-once DoD gate: PROVE build-once across a MULTI-ROUND match.

test_players.py::test_build_once_across_multiple_triggers already asserts the cache
holds when the SAME HarnessPlayerCore instance runs edit_turn() N times. That is NOT
the real risk. In production (vendored CodeClash PvpTournament), the flow is:

    PvpTournament.__init__  -> constructs each agent (Player) EXACTLY ONCE
    PvpTournament.run()     -> for round_num in 1..N:
                                   run_edit_phase(round_num)
                                     -> run_agent(agent, round_num)
                                          -> agent.run()          # per ROUND
    HarnessPlayer.run()     -> run_isolated_edit_turn(...)
                                 -> HarnessPlayerCore(...)         # FRESH core each round
                                      .edit_turn()

So a NEW HarnessPlayerCore is built every round; build-once survives ONLY because
_ARTIFACT_CACHE lives at MODULE scope keyed by (player_id, game, prompt_version).
These tests drive a fake multi-round referee that reproduces that exact shape
(construct player once, invoke per round through the production seam
run_isolated_edit_turn) and prove the live adapter is invoked EXACTLY ONCE across
all N rounds.

FAKE adapters + a fake referee only. No Docker, no credentials, no live CLI — that
is test_e2e_live.py's nightly job.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    HarnessAdapter,
    Usage,
)
from atv_bench.integration import run_isolated_edit_turn
from atv_bench.players import _ARTIFACT_CACHE, clear_artifact_cache

ROUNDS = 5


class DirContainer:
    """Fake TreeContainerLike backed by a host dir (stand-in for the Docker workdir)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def read_tree(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                out[p.relative_to(self.root).as_posix()] = p.read_text()
        return out

    def write_tree(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)


class CountingAdapter(HarnessAdapter):
    """A fake CLI that COUNTS how often it is invoked and edits the bot file once."""

    name = "counting"

    def __init__(self, *, new_content: str = "def get_move(o):\n    return 'S'\n",
                 model: str = "fake-model-1"):
        self.new_content = new_content
        self.model = model
        self.calls = 0

    def run(self, req: AdapterRequest) -> AdapterResult:
        self.calls += 1
        (Path(req.repo_path) / req.bot_file).write_text(self.new_content)
        return AdapterResult(status=AdapterStatus.OK, diff="", log="",
                             usage=Usage(), model=self.model)


class FakePlayer:
    """Stand-in for CodeClash's Player: constructed ONCE, run() invoked per round.

    Mirrors integration.HarnessPlayer.run() — it goes through the SAME production seam
    (run_isolated_edit_turn), which builds a FRESH HarnessPlayerCore every call. So this
    faithfully reproduces the per-round reconstruction that build-once must survive.
    """

    def __init__(self, *, adapter, container, player_id, game="lightcycles",
                 prompt_version="edit@1"):
        self.adapter = adapter
        self.container = container
        self.player_id = player_id
        self.game = game
        self.prompt_version = prompt_version
        self.results: list[AdapterResult] = []

    def run(self, round_num: int) -> AdapterResult:
        result = run_isolated_edit_turn(
            adapter=self.adapter,
            container=self.container,
            home=None,
            goal="win",
            model="m",
            player_id=self.player_id,
            game=self.game,
            prompt_version=self.prompt_version,
            bot_file="main.py",
        )
        self.results.append(result)
        return result


class FakeReferee:
    """Fake multi-round tournament: constructs players ONCE, calls run() per round.

    This is the deterministic analogue of PvpTournament.run(): agents are built in
    __init__ and agent.run() is invoked once per round (1..N).
    """

    def __init__(self, players: list[FakePlayer], rounds: int):
        self.players = players  # constructed by the caller exactly once
        self.rounds = rounds

    def run(self) -> None:
        for round_num in range(1, self.rounds + 1):
            for player in self.players:
                player.run(round_num)


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_artifact_cache()
    yield
    clear_artifact_cache()


def _seed(tmp_path: Path, name: str) -> DirContainer:
    c = DirContainer(tmp_path / name)
    c.write_tree({"main.py": "def get_move(o):\n    return 'N'\n"})
    return c


def test_single_build_across_rounds_fake(tmp_path):
    """DoD GATE: one player, N rounds, adapter invoked EXACTLY ONCE across all rounds.

    Fails if the cache is missing/misconfigured or if per-round invocation (fresh core
    each round) re-triggers a build.
    """
    container = _seed(tmp_path, "ctr")
    adapter = CountingAdapter()
    player = FakePlayer(adapter=adapter, container=container, player_id="p1")
    referee = FakeReferee([player], rounds=ROUNDS)

    referee.run()

    assert len(player.results) == ROUNDS, "referee did not drive all rounds"
    assert adapter.calls == 1, (
        f"build-once VIOLATED: adapter invoked {adapter.calls}x across {ROUNDS} rounds"
    )
    assert "return 'S'" in container.read_tree()["main.py"]


def test_frozen_artifact_reused_bytewise(tmp_path):
    """Rounds 2..N replay the SAME frozen tree object as round 1 (cache identity)."""
    container = _seed(tmp_path, "ctr")
    adapter = CountingAdapter()
    player = FakePlayer(adapter=adapter, container=container, player_id="pfrozen")
    referee = FakeReferee([player], rounds=ROUNDS)

    referee.run()

    key = ("pfrozen", "lightcycles", "edit@1")
    assert key in _ARTIFACT_CACHE
    frozen_tree, _result, _diff = _ARTIFACT_CACHE[key]
    # Container ends bytewise-identical to the one frozen artifact on every round.
    assert container.read_tree() == frozen_tree
    assert frozen_tree["main.py"] == "def get_move(o):\n    return 'S'\n"


def test_build_once_cache_key_isolation(tmp_path):
    """Different players / game / prompt_version each build ONCE, independently — no
    cross-contamination in the shared module-level cache."""
    ca = CountingAdapter(new_content="def get_move(o):\n    return 'A'\n", model="ma")
    cb = CountingAdapter(new_content="def get_move(o):\n    return 'B'\n", model="mb")
    # cc: same player_id as ca but a DIFFERENT game -> distinct cache key.
    cc = CountingAdapter(new_content="def get_move(o):\n    return 'C'\n", model="mc")

    pa = FakePlayer(adapter=ca, container=_seed(tmp_path, "a"), player_id="pa")
    pb = FakePlayer(adapter=cb, container=_seed(tmp_path, "b"), player_id="pb")
    pc = FakePlayer(adapter=cc, container=_seed(tmp_path, "c"), player_id="pa",
                    game="othello")

    FakeReferee([pa, pb, pc], rounds=ROUNDS).run()

    assert ca.calls == 1, "player A rebuilt"
    assert cb.calls == 1, "player B rebuilt"
    assert cc.calls == 1, "different-game key collided / rebuilt"
    assert pa.container.read_tree()["main.py"].endswith("return 'A'\n")
    assert pb.container.read_tree()["main.py"].endswith("return 'B'\n")
    assert pc.container.read_tree()["main.py"].endswith("return 'C'\n")
    assert len(_ARTIFACT_CACHE) == 3, "expected 3 independent build-once entries"


def test_no_model_calls_after_build(tmp_path):
    """After the single build (round 1), rounds 2..N make ZERO adapter/model calls."""
    container = _seed(tmp_path, "ctr")
    adapter = CountingAdapter()
    player = FakePlayer(adapter=adapter, container=container, player_id="pquiet")
    referee = FakeReferee([player], rounds=ROUNDS)

    # Round 1: the one build.
    player.run(1)
    assert adapter.calls == 1

    # Rounds 2..N: replay only, no further invocations.
    for round_num in range(2, ROUNDS + 1):
        player.run(round_num)
        assert adapter.calls == 1, (
            f"model called again on round {round_num} (calls={adapter.calls})"
        )
