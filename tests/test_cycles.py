"""Two-week focus cycles."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from coach.session import cycles
from coach.store.db import FocusCycle, Game, Move, Puzzle, SrsCard, session_factory, set_setting

FEN = "8/8/8/8/8/8/8/8 w - - 0 1"
NOW = datetime(2026, 6, 1, 12, 0)


def _puzzles(s, n, pillar="endgame"):
    for i in range(n):
        s.add(Puzzle(source="own-game", fen=FEN, solution="e2e4", pillar=pillar,
                     rating_est=1000 + 10 * i))
    s.flush()


def _games(s, when: datetime, n_moves: int, cp: int, pillar="endgame", gid=[0]):  # noqa: B006
    gid[0] += 1
    g = Game(source="lichess", source_id=f"g{gid[0]}", account="me", color="white",
             result="loss", pgn="", analyzed=True, played_at=when)
    s.add(g)
    s.flush()
    for i in range(n_moves):
        s.add(Move(game_id=g.id, ply=2 * i, side="white", is_user_move=True, san="e4",
                   uci="e2e4", fen_before=FEN, cp_loss=cp, pillar=pillar,
                   error_bucket="blunder" if cp >= 250 else "ok", decision_w=1.0))
    s.flush()


@pytest.fixture
def s(tmp_path):
    with session_factory(tmp_path / "c.db")() as session:
        set_setting(session, "puzzle_rating", "1200")
        yield session


def test_start_builds_matched_parallel_tests_and_holds_the_second_back(s):
    _puzzles(s, 20)
    cyc = cycles.start(s, "endgame", now=NOW)
    import json
    t = json.loads(cyc.tests)
    a, b = t["pre"]["ids"], t["post"]["ids"]
    assert len(a) == len(b) == cycles.TEST_SIZE and not set(a) & set(b)
    ra = sorted(s.get(Puzzle, i).rating_est for i in a)
    rb = sorted(s.get(Puzzle, i).rating_est for i in b)
    assert abs(sum(ra) - sum(rb)) / len(ra) <= 10          # matched difficulty
    for pid in b:                                          # kept out of the daily queue
        assert s.query(SrsCard).filter_by(puzzle_id=pid).one().due_at >= cyc.ends_at


def test_tests_are_due_at_the_right_ends(s):
    _puzzles(s, 20)
    cyc = cycles.start(s, "endgame", now=NOW)
    assert cycles.pending_test(cyc, NOW) == "pre"
    assert cycles.pending_test(cyc, NOW + timedelta(days=5)) is None   # too late for a pre-test
    import json
    for pid in json.loads(cyc.tests)["pre"]["ids"]:
        cycles.answer(s, cyc, "pre", pid, pid % 2 == 0, 9000)
    assert cycles.pending_test(cyc, NOW + timedelta(days=1)) is None
    assert cycles.pending_test(cyc, NOW + timedelta(days=14)) == "post"
    with pytest.raises(ValueError):
        cycles.answer(s, cyc, "post", 999_999, True, 1)


def test_no_test_when_the_pillar_has_too_few_positions(s):
    _puzzles(s, 6)
    cyc = cycles.start(s, "endgame", now=NOW)
    assert cycles.pending_test(cyc, NOW) is None
    assert cycles.maybe_finish(s, cyc, NOW + timedelta(days=13)) is None
    rep = cycles.maybe_finish(s, cyc, NOW + timedelta(days=14))
    assert rep is not None and cyc.finished_at is not None
    assert "Too few real games" in rep["summary"][0]


def test_report_compares_tests_and_real_games(s):
    _puzzles(s, 20)
    _games(s, NOW - timedelta(days=10), 50, 200)
    cyc = cycles.start(s, "endgame", now=NOW)
    _games(s, NOW + timedelta(days=3), 50, 80)
    import json
    t = json.loads(cyc.tests)
    for i, pid in enumerate(t["pre"]["ids"]):
        cycles.answer(s, cyc, "pre", pid, i < 3, 9000)
    end = NOW + timedelta(days=14)
    assert cycles.maybe_finish(s, cyc, end) is None          # post-test still owed
    for pid in t["post"]["ids"]:
        cycles.answer(s, cyc, "post", pid, True, 6000)
    rep = cycles.maybe_finish(s, cyc, end)
    assert rep["pre_test"]["solved"] == 3 and rep["post_test"]["solved"] == cycles.TEST_SIZE
    assert rep["games_comparable"]
    assert rep["during"]["avg_cp_loss"] < rep["baseline"]["avg_cp_loss"]
    assert any(line.startswith("Test: 3/8") for line in rep["summary"])


def test_ensure_rotates_after_two_stuck_cycles(s):
    for _ in range(2):
        c = FocusCycle(pillar="endgame", started_at=NOW, ends_at=NOW, finished_at=NOW)
        s.add(c)
    s.flush()
    cyc = cycles.ensure(s, ["endgame", "quiet"], now=NOW)
    assert cyc.pillar == "quiet"
    assert cycles.ensure(s, ["endgame", "quiet"], now=NOW) is cyc      # one at a time


def test_plan_follows_the_cycle_and_opens_with_its_test(monkeypatch):
    from coach.session import plan as sp

    monkeypatch.setattr(sp, "latest_profile", lambda *_a, **_k: {
        "ranked_weaknesses": [{"pillar": "tactics", "label": "Tactics", "avg_cp_loss": 90,
                               "blunder_rate": 0.1}]})
    monkeypatch.setattr(sp, "_rotation", lambda: 0)
    cyc = {"id": 7, "pillar": "endgame", "label": "Endgames", "day": 1, "days": 14,
           "pending_test": "pre",
           "test_block": {"kind": "test", "title": "Cycle test", "minutes": 6, "detail": "",
                          "pillar": "endgame",
                          "params": {"cycle_id": 7, "phase": "pre", "puzzle_ids": [1, 2]}}}
    plan = sp.build_plan(None, 60, cycle=cyc)
    assert plan["focus_pillar"] == "endgame"
    assert plan["blocks"][0]["kind"] == "test"
    assert plan["cycle"]["day"] == 1
    assert any("Focus cycle" in n for n in plan["notes"])
    plan = sp.build_plan(None, 60)
    assert plan["focus_pillar"] == "tactics" and plan["blocks"][0]["kind"] == "warmup"


def test_a_skipped_end_test_cannot_hold_the_cycle_open_forever(s):
    _puzzles(s, 20)
    cyc = cycles.start(s, "endgame", now=NOW)
    end = NOW + timedelta(days=cycles.CYCLE_DAYS)
    assert cycles.pending_test(cyc, end) == "post"
    assert cycles.maybe_finish(s, cyc, end + timedelta(days=1)) is None      # still owed
    rep = cycles.maybe_finish(s, cyc, end + timedelta(days=cycles.POST_TEST_GRACE_DAYS))
    assert rep is not None and rep["post_test"] is None and cyc.finished_at is not None


def test_preview_announces_the_cycle_the_start_button_will_open(monkeypatch):
    from coach.session import plan as sp

    monkeypatch.setattr(sp, "latest_profile", lambda *_a, **_k: {
        "ranked_weaknesses": [{"pillar": "endgame", "label": "Endgames", "avg_cp_loss": 90,
                               "blunder_rate": 0.1}]})
    monkeypatch.setattr(sp, "_rotation", lambda: 0)
    monkeypatch.setattr(cycles, "active", lambda *_a, **_k: None)
    cyc = sp.cycle_preview(None)
    assert cyc["proposed"] and cyc["pillar"] == "endgame"
    plan = sp.build_plan(None, 60, cycle=cyc)
    assert any(n.startswith("Starting today") for n in plan["notes"])
    assert plan["blocks"][0]["kind"] == "warmup"
