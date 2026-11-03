"""Candidate moves first, on quiet and positional puzzles."""

from __future__ import annotations

import chess
import pytest
from fastapi.testclient import TestClient

from coach.puzzles import candidates as cand
from coach.store.db import Puzzle, session_factory

FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"


def fixed_scorer(table: dict[str, int]):
    """Ranks the listed moves by the given cp; anything else trails far behind."""
    def score(board, budget, limit):
        out = [(chess.Move.from_uci(u), cp) for u, cp in table.items()]
        out.sort(key=lambda t: -t[1])
        return out
    return score


SCORER = fixed_scorer({"f1b5": 40, "f1c4": 30, "d2d4": 25, "b1c3": -40, "h2h4": -150})


def test_required_only_where_options_go_unconsidered():
    quiet = Puzzle(source="own-game", fen=FEN, solution="f1b5", pillar="quiet", kind="find-best")
    tactic = Puzzle(source="own-game", fen=FEN, solution="f1b5", pillar="tactics", kind="find-best")
    drill = Puzzle(source="endgame", fen=FEN, solution="f1b5", pillar="quiet", kind="find-best")
    line = Puzzle(source="repertoire", fen=FEN, solution="f1b5", pillar="opening", kind="line")
    assert cand.required(quiet)
    assert not cand.required(tactic)
    assert cand.required(tactic, everywhere=True)
    assert not cand.required(drill, everywhere=True)
    assert not cand.required(line, everywhere=True)


def test_a_list_with_the_best_move_scores_full_marks():
    g = cand.grade(chess.Board(FEN), ["f1b5", "f1c4"], scorer=SCORER)
    assert g["graded"] and g["found_best"]
    assert g["score"] == 1.0
    assert [r["verdict"] for r in g["listed"]] == ["best", "good"]
    assert [m["san"] for m in g["missed"]] == ["d4"]


def test_a_list_that_missed_the_good_moves_says_which_one():
    g = cand.grade(chess.Board(FEN), ["b1c3", "h2h4"], scorer=SCORER)
    assert not g["found_best"]
    assert g["score"] == 0.0
    assert g["listed"][0]["verdict"] == "playable"
    assert g["listed"][1]["verdict"] == "weak"
    assert "Bb5" in g["note"]


def test_illegal_and_duplicate_candidates_are_ignored():
    g = cand.grade(chess.Board(FEN), ["e1e8", "f1b5", "f1b5", "zz"], scorer=SCORER)
    assert [r["uci"] for r in g["listed"]] == ["f1b5"]
    assert cand.grade(chess.Board(FEN), [], scorer=SCORER)["graded"] is False


def test_engine_failure_does_not_break_the_puzzle():
    def broken(*_):
        raise RuntimeError("no engine")
    assert cand.grade(chess.Board(FEN), ["f1b5"], scorer=broken)["graded"] is False


def test_tally_accumulates(tmp_path):
    with session_factory(tmp_path / "c.db")() as s:
        cand.record(s, {"graded": True, "found_best": True, "score": 1.0}, "quiet")
        cand.record(s, {"graded": True, "found_best": False, "score": 0.5}, "quiet")
        cand.record(s, {"graded": False}, "quiet")
        summ = cand.summary(s)
    assert summ["all"]["n"] == 2
    assert summ["quiet"]["found_best_rate"] == 0.5
    assert summ["all"]["avg_score"] == 0.75


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("coach.server.app._REFRESH_ON_START", False, raising=False)
    from coach.server.app import app

    return TestClient(app)


def test_the_list_is_asked_for_first_and_graded_only_at_the_end(client, monkeypatch):
    monkeypatch.setattr(cand, "_default_scorer", SCORER)
    monkeypatch.setattr("coach.puzzles.solve.stockfish_verifier", lambda b, m: None)
    monkeypatch.setattr("coach.puzzles.solve.stockfish_refuter", lambda b, m: None)
    with session_factory()() as s:
        p = Puzzle(source="own-game", fen=FEN, solution="f1b5", pillar="quiet",
                   kind="find-best", rating_est=1400)
        s.add(p)
        s.commit()
        pid = p.id

    first = client.post(f"/api/puzzles/{pid}/attempt", json={"moves": []}).json()
    assert first["status"] == "continue"
    assert first["candidates_required"] == {"min": 2, "max": 3}
    assert "candidates" not in first          # nothing graded, nothing revealed

    done = client.post(f"/api/puzzles/{pid}/attempt",
                       json={"moves": ["f1b5"], "candidates": ["f1c4", "f1b5"]}).json()
    assert done["status"] == "solved"
    assert done["candidates"]["found_best"] is True

    # Practice retries never file a grade, and never re-grade the list either.
    again = client.post(f"/api/puzzles/{pid}/attempt",
                        json={"moves": ["f1b5"], "candidates": ["f1c4"], "practice": True}).json()
    assert "candidates" not in again


def test_the_everywhere_toggle_round_trips(client):
    assert client.post("/api/settings/training",
                       json={"candidates_everywhere": True}).json()["candidates_everywhere"]
    assert client.get("/api/settings/training").json()["candidates_everywhere"] is True
    client.post("/api/settings/training", json={"candidates_everywhere": False})
    assert client.get("/api/settings/training").json()["candidates_everywhere"] is False


def test_a_promotion_candidate_from_the_board_is_read_as_a_queen():
    board = chess.Board("8/4P3/8/8/8/8/k7/4K3 w - - 0 1")
    g = cand.grade(board, ["e7e8", "e1d2"], scorer=fixed_scorer({"e7e8q": 900, "e1d2": 0}))
    assert [r["uci"] for r in g["listed"]] == ["e7e8q", "e1d2"]
    assert g["found_best"]
