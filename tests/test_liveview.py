"""T1-liveview: background daemon poll-server + round watcher.

Hermetic (no Docker, no browser): drives the watcher against a temp dir with
real round tarball fixtures, stepping `poll_once()` deterministically instead of
relying on the background poll cadence.
"""
from __future__ import annotations

import json
import shutil
import tarfile
import urllib.request
from pathlib import Path

import pytest

from atv_bench.liveview import LiveView, confidence_bucket

FIXTURES = Path(__file__).parent / "fixtures" / "rounds"
ANTS = FIXTURES / "ants-0_round_0.tar.gz"
CHESS = FIXTURES / "chess-1_round_0.tar.gz"
# Captured real-arena decisive tars (UNMODIFIED): results at <n>/results.json
# (2/ and 3/, NOT 0/ — proves the watcher does not hardcode 0/), bare-claude-code
# (seat 1) wins outright, so the seat->color mapping resolves red with no
# repackaging/fabrication.
ANTS_DECISIVE_BARE = FIXTURES / "ants-0_round_2_decisive_bare.tar.gz"
CHESS_DECISIVE_BARE = FIXTURES / "chess-5_round_3_decisive_bare.tar.gz"


def _drop_round(match_dir: Path, tar_src: Path, round_index: int) -> Path:
    """Drop a complete round: a real arena `round_N.tar.gz` (results.json lives
    INSIDE the tar at 0/results.json — no fabricated on-disk sibling)."""
    match_dir.mkdir(parents=True, exist_ok=True)
    dest = match_dir / f"round_{round_index}.tar.gz"
    shutil.copy(tar_src, dest)
    return dest


def _retar_with_results(tar_src: Path, dest: Path, results: dict) -> Path:
    """Repackage a fixture tar, overwriting its in-tar `results.json` member.

    Preserves every other member (the sim_*/pgn artifacts extract_round parses)
    so the only change is the winner recorded inside 0/results.json — exactly the
    real arena layout, with no on-disk sibling.
    """
    import io
    blob = json.dumps(results).encode()
    with tarfile.open(tar_src) as src:
        members = src.getmembers()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dest, "w:gz") as out:
            for m in members:
                if Path(m.name).name == "results.json":
                    info = tarfile.TarInfo(name=m.name)
                    info.size = len(blob)
                    out.addfile(info, io.BytesIO(blob))
                else:
                    f = src.extractfile(m)
                    out.addfile(m, f) if f is not None else out.addfile(m)
    return dest


@pytest.fixture
def live(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants", "chess"],
                  harness="claude-code", baseline="bare-claude-code")
    yield lv
    lv.close()


def _status(live: LiveView) -> dict:
    return json.loads((live.live_dir / "status.json").read_text())


def test_complete_round_drop_writes_per_round_json_and_updates_status(live, tmp_path):
    match_dir = tmp_path / "store" / "ants-0" / "rounds"
    live.match_start("ants", 0, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    _drop_round(match_dir, ANTS, 0)

    # Mid-write gate: first poll records size, does not publish yet.
    published = live.poll_once()
    assert published == []
    # Second poll: size stable + sibling results.json present -> publish.
    published = live.poll_once()
    assert len(published) == 1

    round_json = live.live_dir / published[0]
    assert round_json.exists()
    payload = json.loads(round_json.read_text())
    assert payload["game"] == "ants"
    assert payload["round"] == 0


def test_non_dict_results_json_does_not_crash_poll_loop(live, tmp_path, monkeypatch):
    """A valid-JSON-but-non-object results.json (list/str) must not raise out of
    the poll loop — it is treated as a permanent, cached parse failure so the
    watcher survives and does not reparse it every tick (correctness + DoS)."""
    import io
    from atv_bench import liveview as _lv
    match_dir = tmp_path / "store" / "ants-0" / "rounds"
    live.match_start("ants", 0, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    # Repackage the fixture with a results.json that is a JSON list, not an object.
    dest = match_dir / "round_0.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(["not", "a", "dict"]).encode()
    with tarfile.open(ANTS) as src, tarfile.open(dest, "w:gz") as out:
        for m in src.getmembers():
            if Path(m.name).name == "results.json":
                info = tarfile.TarInfo(name=m.name)
                info.size = len(blob)
                out.addfile(info, io.BytesIO(blob))
            else:
                f = src.extractfile(m)
                out.addfile(m, f) if f is not None else out.addfile(m)

    # Count real parse attempts: a broken impl that reparses the stable non-dict
    # tar every poll would bump this past 1 (the cache defeats that).
    calls = {"n": 0}
    real_read_round = _lv.read_round
    def _counting_read_round(tar):
        calls["n"] += 1
        return real_read_round(tar)
    monkeypatch.setattr(_lv, "read_round", _counting_read_round)

    # Never raises; publishes nothing; parsed exactly once then cached.
    assert live.poll_once() == []          # poll 1: records size, no parse yet
    assert live.poll_once() == []          # poll 2: size stable -> parse -> non-dict -> cached fail
    assert live.poll_once() == []          # poll 3: cached, NOT reparsed
    assert live.poll_once() == []
    assert calls["n"] == 1                  # proves the failure cache holds

    # Nothing published, and status.json has no round for this match.
    status = _status(live)
    assert status["matches"][0]["rounds"] == []


def test_winner_color_encodes_seat(live, tmp_path):
    match_dir = tmp_path / "store" / "chess-1" / "rounds"
    match_dir.mkdir(parents=True, exist_ok=True)
    live.match_start("chess", 1, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    # In-tar results.json where claude-code (seat 0 = blue) wins outright.
    _retar_with_results(CHESS, match_dir / "round_0.tar.gz", {
        "round_num": 0, "winner": "claude-code",
        "scores": {"claude-code": 2, "bare-claude-code": 0},
    })
    live.poll_once()
    live.poll_once()
    rnd = _status(live)["matches"][0]["rounds"][0]
    assert rnd["winner"] == "claude-code"
    assert rnd["color"] == "blue"


def test_bare_control_winner_maps_to_red(live, tmp_path):
    """HIGH: a bare-control win (seat 1) must color red(--b), whether results.json
    records the on-disk name 'bare-claude-code' or the seat index 1."""
    match_dir = tmp_path / "store" / "chess-1" / "rounds"
    match_dir.mkdir(parents=True, exist_ok=True)
    live.match_start("chess", 1, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    _retar_with_results(CHESS, match_dir / "round_0.tar.gz", {
        "round_num": 0, "winner": "bare-claude-code",
        "scores": {"claude-code": 0, "bare-claude-code": 2},
    })
    live.poll_once()
    live.poll_once()
    rnd = _status(live)["matches"][0]["rounds"][0]
    assert rnd["winner"] == "bare-claude-code"
    assert rnd["color"] == "red"


def test_bare_control_winner_by_index_maps_to_red(live, tmp_path):
    """A winner recorded as the integer seat index 1 also maps to red."""
    match_dir = tmp_path / "store" / "chess-1" / "rounds"
    match_dir.mkdir(parents=True, exist_ok=True)
    live.match_start("chess", 1, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    _retar_with_results(CHESS, match_dir / "round_0.tar.gz", {
        "round_num": 0, "winner": 1,
        "scores": {"claude-code": 0, "bare-claude-code": 2},
    })
    live.poll_once()
    live.poll_once()
    assert _status(live)["matches"][0]["rounds"][0]["color"] == "red"


def test_real_decisive_ants_tar_colors_bare_winner_red(live, tmp_path):
    """MEDIUM: an UNMODIFIED real-arena decisive tar (results at 2/results.json,
    winner=bare-claude-code) must publish and color the bare-control winner red.
    No repackaging/fabrication — proves the winner->color mapping on real bytes
    AND that the watcher does not hardcode the round dir to 0/."""
    match_dir = tmp_path / "store" / "ants-0" / "rounds"
    live.match_start("ants", 0, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    match_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ANTS_DECISIVE_BARE, match_dir / "round_0.tar.gz")  # verbatim
    live.poll_once()
    live.poll_once()
    rnd = _status(live)["matches"][0]["rounds"][0]
    assert rnd["winner"] == "bare-claude-code"
    assert rnd["color"] == "red"


def test_real_decisive_chess_tar_bare_prefix_seat_colors_red(live, tmp_path):
    """MEDIUM: a second UNMODIFIED real-arena decisive tar (chess, results at
    3/results.json, winner=bare-claude-code), with seat 1 bound in DISPLAY form
    'bare:claude-code'. The winner name must normalize across the 'bare:'/'bare-'
    prefix and resolve to seat 1 = red — proving the mapping on real bytes for a
    non-zero round dir AND through the prefix-normalization path."""
    match_dir = tmp_path / "store" / "chess-5" / "rounds"
    live.match_start("chess", 5, str(match_dir),
                     seats=("claude-code", "bare:claude-code"))
    match_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(CHESS_DECISIVE_BARE, match_dir / "round_0.tar.gz")  # verbatim
    live.poll_once()
    live.poll_once()
    rnd = _status(live)["matches"][0]["rounds"][0]
    assert rnd["winner"] == "bare-claude-code"
    assert rnd["color"] == "red"


def test_stable_malformed_tar_is_parsed_once_not_every_poll(live, tmp_path, monkeypatch):
    """HIGH: a stable-but-malformed tar must be parsed ONCE and then cached as a
    failure by (size, mtime_ns) — never reparsed on every 250ms poll under the
    lock. Retry only happens once the file actually changes."""
    import atv_bench.liveview as lv_mod

    match_dir = tmp_path / "store" / "ants-0" / "rounds"
    match_dir.mkdir(parents=True, exist_ok=True)
    live.match_start("ants", 0, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    tar = match_dir / "round_0.tar.gz"
    tar.write_bytes(b"not a tarball at all")  # stable size, cannot open

    calls = {"n": 0}
    real_read_round = lv_mod.read_round

    def _counting_read_round(path):
        calls["n"] += 1
        return real_read_round(path)

    monkeypatch.setattr(lv_mod, "read_round", _counting_read_round)

    # First poll records size (no parse). Then many polls over a STABLE file:
    # exactly one parse attempt total, the rest short-circuit on the failure cache.
    for _ in range(6):
        assert live.poll_once() == []
    assert calls["n"] == 1, f"stable malformed tar reparsed {calls['n']} times"

    # File changes (arena rewrites it) -> the cache invalidates and it retries.
    import time
    time.sleep(0.01)
    tar.write_bytes(b"still not a tarball, but different bytes now!!")
    live.poll_once()  # records new size
    live.poll_once()  # stable at new size -> one fresh parse attempt
    assert calls["n"] == 2


def test_real_arena_tar_publishes_without_sibling_results(live, tmp_path):
    """REAL-PUBLISH regression: an UNMODIFIED real-arena fixture tar (results.json
    lives INSIDE at 0/results.json, NO on-disk sibling) must publish through the
    gate — per-round JSON written AND status.json updated. FAILS against the old
    sibling-results gate; PASSES after the in-tar gate."""
    match_dir = tmp_path / "store" / "ants-0" / "rounds"
    live.match_start("ants", 0, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    match_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ANTS, match_dir / "round_0.tar.gz")  # verbatim, no sibling
    assert not (match_dir / "results.json").exists()  # prove: no sibling

    assert live.poll_once() == []          # first poll only records size
    published = live.poll_once()           # stable + in-tar results -> publish
    assert len(published) == 1

    round_json = live.live_dir / published[0]
    assert round_json.exists()
    payload = json.loads(round_json.read_text())
    assert payload["game"] == "ants" and payload["sims"]

    status = _status(live)
    assert status["matches"][0]["rounds"][0]["round"] == 0
    assert status["rounds"][0]["status"] == "landed"


def test_tie_round_has_no_seat_color(live, tmp_path):
    match_dir = tmp_path / "store" / "ants-0" / "rounds"
    live.match_start("ants", 0, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    _drop_round(match_dir, ANTS, 0)  # fixture results.json winner == "Tie"
    live.poll_once()
    live.poll_once()
    rnd = _status(live)["matches"][0]["rounds"][0]
    assert rnd["winner"] in ("Tie", "tie", None)
    assert rnd["color"] is None


def test_truncated_tar_is_skipped_and_retried_never_crashes(live, tmp_path):
    match_dir = tmp_path / "store" / "ants-0" / "rounds"
    match_dir.mkdir(parents=True, exist_ok=True)
    live.match_start("ants", 0, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))

    # Mid-write: a truncated (half-written) tar — cannot open / no results member.
    full = ANTS.read_bytes()
    tar_dest = match_dir / "round_0.tar.gz"
    tar_dest.write_bytes(full[: len(full) // 2])

    # Truncated tar -> gate never opens; no publish, no crash.
    assert live.poll_once() == []
    assert live.poll_once() == []
    assert _status(live)["matches"][0]["rounds"] == []

    # Arena finishes the write (results.json is INSIDE the tar) -> now it publishes.
    tar_dest.write_bytes(full)
    live.poll_once()  # first stable observation
    published = live.poll_once()
    assert len(published) == 1
    assert len(_status(live)["matches"][0]["rounds"]) == 1


def test_corrupt_stable_tar_does_not_crash_watcher(live, tmp_path):
    match_dir = tmp_path / "store" / "ants-0" / "rounds"
    match_dir.mkdir(parents=True, exist_ok=True)
    live.match_start("ants", 0, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    # Stable size, but the tar is garbage -> cannot open -> gate stays shut.
    (match_dir / "round_0.tar.gz").write_bytes(b"not a tarball at all")
    assert live.poll_once() == []
    assert live.poll_once() == []  # stable + corrupt: skipped, watcher survives
    assert _status(live)["matches"][0]["rounds"] == []


def test_finish_flips_status_to_complete_and_thread_joins(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    assert json.loads((lv.live_dir / "status.json").read_text())["status"] == "running"
    lv.finish()
    status = json.loads((lv.live_dir / "status.json").read_text())
    assert status["status"] == "complete"
    # Daemon thread still serving after finish; close() joins it cleanly.
    assert lv._thread.is_alive()
    lv.close()
    assert not lv._thread.is_alive()


def test_status_carries_lift_and_confidence_fields(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    try:
        lv.finish(lift=0.69, ci_lo=-4.6, ci_hi=8.2)
        status = json.loads((lv.live_dir / "status.json").read_text())
        assert status["lift"]["value"] == 0.69
        assert status["lift"]["ci_lo"] == -4.6
        assert status["lift"]["ci_hi"] == 8.2
        # confidence bucket (D2): a CI straddling 0 is "low".
        assert status["lift"]["confidence"] == "low"
    finally:
        lv.close()


def test_server_serves_status_over_http(live, tmp_path):
    match_dir = tmp_path / "store" / "ants-0" / "rounds"
    live.match_start("ants", 0, str(match_dir),
                     seats=("claude-code", "bare-claude-code"))
    with urllib.request.urlopen(f"{live.url_base}/status.json", timeout=5) as resp:
        body = json.loads(resp.read())
    assert body["harness"] == "claude-code"
    assert body["status"] == "running"


def test_confidence_bucket_thresholds():
    assert confidence_bucket(-1.0, 1.0) == "low"      # straddles 0
    assert confidence_bucket(0.5, 1.0) == "high"      # tight, excludes 0
    assert confidence_bucket(0.1, 9.0) == "medium"    # excludes 0 but wide


# --- remediation: status.json is the live.html VIEW contract ----------------


def test_status_json_carries_live_html_view_contract(tmp_path):
    """The served status.json must be readable by live.html's poller: it needs
    state/game/seats[{name,color}]/score/rounds[{status,...}]/current — not only
    the engine-facing keys. Regression for 'browser renders nothing'."""
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants", "chess"],
                  harness="claude-code", baseline="bare-claude-code",
                  rounds_per_match=3)
    try:
        match_dir = store / "ants-0" / "rounds"
        lv.match_start("ants", 0, str(match_dir),
                       seats=("claude-code", "bare-claude-code"))
        status = _status(lv)
        # view keys present + shaped for live.html.
        assert status["state"] in ("empty", "running")
        assert status["game"] == "ants"
        seats = status["seats"]
        assert seats[0] == {"name": "claude-code", "color": "a"}
        assert seats[1] == {"name": "bare-claude-code", "color": "b"}
        assert "score" in status and {"a", "b"} <= set(status["score"])
        # a fresh match strips to all-pending until round 0 lands.
        assert [r["status"] for r in status["rounds"]] == ["current", "pending", "pending"]

        _drop_round(match_dir, ANTS, 0)
        lv.poll_once(); lv.poll_once()
        status = _status(lv)
        assert status["rounds"][0]["status"] == "landed"
        assert "turn" in status["rounds"][0]
        # browser-shaped round file exists with flat frames the canvas consumes.
        rj = json.loads((lv.live_dir / "round_0.json").read_text())
        assert rj["game"] == "ants" and rj["frames"] and "ants" in rj["frames"][0]
    finally:
        lv.close()


def test_match_end_publishes_last_round_needing_two_observations(tmp_path):
    """A round tar landing right before match_end must still publish: match_end
    polls to quiescence, not once (one poll can't satisfy the 2-observation gate)."""
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    try:
        match_dir = store / "ants-0" / "rounds"
        lv.match_start("ants", 0, str(match_dir),
                       seats=("claude-code", "bare-claude-code"))
        _drop_round(match_dir, ANTS, 0)  # first-ever observation for this tar
        # A single poll_once would only record the size, publishing nothing.
        # match_end must keep polling until the gate opens.
        lv.match_end("ants", 0)
        assert len(_status(lv)["matches"][0]["rounds"]) == 1
    finally:
        lv.close()


def test_status_json_is_written_atomically(tmp_path):
    """status.json is published via os.replace of a temp file, so a reader never
    sees a truncated document even under concurrent writes."""
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    try:
        path = lv.live_dir / "status.json"
        import threading as _t
        stop = _t.Event()
        errors: list[BaseException] = []

        def _hammer():
            # every read must parse — never a partial file. Collect any failure
            # so a reader-thread exception is ASSERTED, not swallowed as a thread
            # warning that lets the test pass despite a truncated read.
            try:
                while not stop.is_set():
                    json.loads(path.read_text())
            except BaseException as exc:  # noqa: BLE001 - surfaced via assert below
                errors.append(exc)

        readers = [_t.Thread(target=_hammer) for _ in range(4)]
        for r in readers:
            r.start()
        try:
            for _ in range(200):
                lv._write_status()
        finally:
            stop.set()
            for r in readers:
                r.join(timeout=5)
        assert errors == [], f"reader threads saw partial/failed reads: {errors!r}"
        assert not any(r.is_alive() for r in readers)
        # no leftover temp files in the served dir.
        assert not list(lv.live_dir.glob(".status.json.*.tmp"))
    finally:
        lv.close()



# ===========================================================================
# T3-harden: watcher shutdown, malformed-drop survival, background loop,
# headless guard. Full edge coverage over the merged T1/T2/T5 result.
# ===========================================================================

LIGHTCYCLES = FIXTURES / "lightcycles-2_round_0.tar.gz"


def test_start_watching_spawns_joinable_background_thread(tmp_path):
    """cli.py calls live_view.start_watching(); it must exist, spawn a daemon
    watch thread, be idempotent, and join cleanly on close() (no hang)."""
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    try:
        lv.start_watching()
        watch = lv._watch_thread
        assert watch is not None and watch.is_alive() and watch.daemon
        # idempotent: a second call does not spawn a second thread.
        lv.start_watching()
        assert lv._watch_thread is watch
    finally:
        lv.close()
    assert not watch.is_alive()
    assert not lv._thread.is_alive()


def test_background_watch_loop_publishes_a_dropped_round(tmp_path):
    """The real (non-test) path: with start_watching() running, dropping a
    complete round eventually publishes it without any manual poll_once()."""
    import time
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    try:
        match_dir = store / "ants-0" / "rounds"
        lv.match_start("ants", 0, str(match_dir),
                       seats=("claude-code", "bare-claude-code"))
        lv.start_watching()
        _drop_round(match_dir, ANTS, 0)
        deadline = time.time() + 10
        while time.time() < deadline:
            if _status(lv)["matches"][0]["rounds"]:
                break
            time.sleep(0.1)
        assert len(_status(lv)["matches"][0]["rounds"]) == 1
    finally:
        lv.close()


def test_watch_loop_survives_a_corrupt_drop(tmp_path):
    """A corrupt tar dropped while the background loop runs must not kill the
    loop — a later good round still publishes."""
    import time
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    try:
        match_dir = store / "ants-0" / "rounds"
        match_dir.mkdir(parents=True, exist_ok=True)
        lv.match_start("ants", 0, str(match_dir),
                       seats=("claude-code", "bare-claude-code"))
        lv.start_watching()
        # corrupt round 0 (stable size, can't open) -> caught, loop lives.
        (match_dir / "round_0.tar.gz").write_bytes(b"not a tarball")
        time.sleep(1.0)
        assert lv._watch_thread.is_alive()
        # a good round 1 still lands.
        _drop_round(match_dir, ANTS, 1)
        deadline = time.time() + 10
        while time.time() < deadline:
            rounds = _status(lv)["matches"][0]["rounds"]
            if any(r["round"] == 1 for r in rounds):
                break
            time.sleep(0.1)
        rounds = _status(lv)["matches"][0]["rounds"]
        assert any(r["round"] == 1 for r in rounds)
    finally:
        lv.close()


def test_close_is_idempotent_and_never_hangs(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    lv.start_watching()
    lv.close()
    # second close must not raise or hang.
    lv.close()
    assert not lv._thread.is_alive()


def test_close_without_start_watching_joins_cleanly(tmp_path):
    """finish()/close() must work even if start_watching() was never called
    (headless-adjacent path where the server exists but no watch loop ran)."""
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    lv.finish()
    lv.close()
    assert not lv._thread.is_alive()


def test_malformed_round_leaves_watcher_alive_and_status_intact(tmp_path):
    """A malformed sim inside an otherwise-well-formed tar drops that round to
    the text panel (no per-round JSON), and the watcher keeps running."""
    import io as _io, tarfile as _tf
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["ants"],
                  harness="claude-code", baseline="bare-claude-code")
    try:
        match_dir = store / "ants-0" / "rounds"
        match_dir.mkdir(parents=True, exist_ok=True)
        lv.match_start("ants", 0, str(match_dir),
                       seats=("claude-code", "bare-claude-code"))
        # tar whose sim_0.json is malformed JSON -> extract_round raises, caught.
        # A valid in-tar results.json opens the gate so the parse is actually tried.
        buf = _io.BytesIO()
        with _tf.open(fileobj=buf, mode="w:gz") as t:
            blob = b"{not json"
            info = _tf.TarInfo(name="0/sim_0.json")
            info.size = len(blob)
            t.addfile(info, _io.BytesIO(blob))
            rblob = json.dumps({"winner": "Tie"}).encode()
            rinfo = _tf.TarInfo(name="0/results.json")
            rinfo.size = len(rblob)
            t.addfile(rinfo, _io.BytesIO(rblob))
        (match_dir / "round_0.tar.gz").write_bytes(buf.getvalue())
        assert lv.poll_once() == []
        assert lv.poll_once() == []  # stable + malformed -> never published
        assert _status(lv)["matches"][0]["rounds"] == []
        # a subsequent good round still publishes (watcher survived).
        _drop_round(match_dir, ANTS, 1)
        lv.poll_once(); lv.poll_once()
        assert any(r["round"] == 1
                   for r in _status(lv)["matches"][0]["rounds"])
    finally:
        lv.close()


def test_lightcycles_round_publishes_browser_frames(tmp_path):
    """Coverage over the lightcycles adapter through the watcher: trails present
    in the browser-shaped round file."""
    store = tmp_path / "store"
    store.mkdir()
    lv = LiveView(str(store), games=["lightcycles"],
                  harness="claude-code", baseline="bare-claude-code")
    try:
        match_dir = store / "lightcycles-0" / "rounds"
        lv.match_start("lightcycles", 0, str(match_dir),
                       seats=("claude-code", "bare-claude-code"))
        _drop_round(match_dir, LIGHTCYCLES, 0)
        lv.poll_once(); lv.poll_once()
        rj = json.loads((lv.live_dir / "round_0.json").read_text())
        assert rj["game"] == "lightcycles"
        assert rj["frames"] and "heads" in rj["frames"][0]
        assert "trails" in rj["frames"][0]
    finally:
        lv.close()


# --- T3: headless guard (no server, live_url=None under --yes/--json) -------

def _stub_executor(*, plays):
    """Minimal executor mirroring test_quickstart_engine: canned scores."""
    def _ex(*, harness_a, harness_b, game, model, seed, index):
        sa = plays.get(game, 0.5)
        return {"game": game, "harness_a": harness_a, "harness_b": harness_b,
                "model_a": model, "model_b": model, "score_a": sa, "seed": seed}
    return _ex


def test_headless_engine_run_binds_no_port_and_live_url_none(tmp_path):
    """The engine itself never constructs a LiveView: a plain run_quickstart_eval
    (the headless / --yes / --json path) binds no port and leaves live_url=None."""
    from atv_bench.quickstart import run_quickstart_eval
    events = []
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet",
        games=["lightcycles", "ants"], repeats=1,
        store=tmp_path / "league",
        execute=_stub_executor(plays={"lightcycles": 1.0, "ants": 0.0}),
        progress=events.append, build_board=False,
    )
    assert res.live_url is None
    assert res.to_dict()["live_url"] is None
    # per-round / per-match textual progress still emitted for the headless story.
    assert any(e.get("phase") == "match" for e in events)


def test_headless_progress_events_carry_match_out_and_seats(tmp_path):
    """T5 seam the watcher binds to: the match event carries match_out + seats
    even on the headless path (the CLI decides whether to attach a LiveView)."""
    from atv_bench.quickstart import run_quickstart_eval, live_match_executor
    events = []
    ex = _stub_executor(plays={"lightcycles": 1.0})
    # attach a base_out so the event can compute the exact per-match dir.
    ex.base_out = str(tmp_path / "out")
    run_quickstart_eval(
        harness="claude-code", model="sonnet", games=["lightcycles"], repeats=1,
        store=tmp_path / "league", execute=ex, progress=events.append,
        build_board=False,
    )
    match_evs = [e for e in events if e.get("phase") == "match"]
    assert match_evs
    ev = match_evs[0]
    assert ev["match_out"] is not None and "lightcycles-0" in ev["match_out"]
    assert ev["seats"]["a"] == "claude-code"
    # baseline seat carries the schedule's bare: prefix (BARE_PREFIX).
    assert ev["seats"]["b"] == "bare:claude-code"
