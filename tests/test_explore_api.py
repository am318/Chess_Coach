"""The explore endpoints, over HTTP.

The unit tests in ``test_explore.py`` cover when a demonstration has made its
point. These cover the wiring: that the routes exist, that a hostile or
malformed body is answered rather than raising, and - the one that matters most
- that exploring a position never writes to the SRS. The grade is fixed by the
attempt, and a trainer that quietly re-graded you for looking would teach you
not to look.
"""

from __future__ import annotations

import chess
import pytest
from fastapi.testclient import TestClient

from coach.puzzles import explore

QUIET = "r1bq1rk1/pp2bppp/2n1pn2/2pp4/3P1B2/2PBPN2/PP1N1PPP/R2Q1RK1 w - - 0 9"


class StubScorer:
    """Plays the first legal move and calls every position level."""

    def best(self, board, budget):
        moves = list(board.legal_moves)
        return (moves[0], 0) if moves else None

    def score_roots(self, board, budget, limit=None):
        out = [(m, -i) for i, m in enumerate(board.legal_moves)]
        return out if limit is None else out[:limit]

    def line(self, board, budget, limit=8):
        return list(board.legal_moves)[:1]


@pytest.fixture
def client(monkeypatch):
    # No Stockfish in the test run, and no server lifespan either: the app's
    # startup hook syncs and analyses, which is not what is under test here.
    monkeypatch.setattr(explore, "_backend", StubScorer)
    monkeypatch.setattr("coach.server.app._REFRESH_ON_START", False, raising=False)
    from coach.server.app import app

    return TestClient(app)


def test_start_returns_a_playable_position(client):
    r = client.post("/api/explore/start", json={"fen": QUIET, "mode": "convert"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "continue"
    assert body["dests"]
    assert body["user_colour"] == "white"


def test_step_applies_the_move_and_answers_it(client):
    r = client.post("/api/explore/step", json={
        "fen": QUIET, "uci": "d1c2", "mode": "convert", "user_colour": "white",
        "hold_cp": 0, "moves_played": 0, "start_fen": QUIET,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["played_san"] == "Qc2"
    assert body["reply_san"]
    assert chess.Board(body["fen"]).turn == chess.WHITE     # back to the user


def test_candidates_rank_the_position(client):
    r = client.post("/api/explore/candidates", json={"fen": QUIET, "top_n": 3})
    assert r.status_code == 200
    moves = r.json()["moves"]
    assert len(moves) == 3
    assert moves[0]["behind"] == 0
    assert all(m["san"] and m["fen_after"] for m in moves)


def test_an_illegal_move_is_answered_not_raised(client):
    r = client.post("/api/explore/step", json={
        "fen": QUIET, "uci": "a1a8", "mode": "punish", "user_colour": "white",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "error"


def test_a_malformed_position_is_rejected_cleanly(client):
    """A bad FEN must come back as a refusal, not a 500 with a stack trace."""
    for bad in ("not a fen", "", "8/8/8/8/8/8/8/8 w - - 0 1 extra junk"):
        r = client.post("/api/explore/candidates", json={"fen": bad})
        assert r.status_code in (200, 400, 422), bad
        if r.status_code == 200:
            assert r.json()["moves"] == []


def test_exploring_does_not_touch_the_review_schedule(client):
    """The whole feature rests on this: looking is free."""
    from coach.puzzles import srs
    from coach.store.db import Puzzle, session_factory

    Session = session_factory()
    with Session() as session:
        p = Puzzle(fen=QUIET, solution="d1c2", pillar="tactics", source="test",
                   rating_est=1000)
        session.add(p)
        session.commit()
        card = srs.ensure_card(session, p)
        session.commit()
        before = (card.due_at, card.stability, card.reps, card.lapses)

    for _ in range(3):
        client.post("/api/explore/step", json={
            "fen": QUIET, "uci": "d1c2", "mode": "convert", "user_colour": "white",
            "hold_cp": 0, "moves_played": 0,
        })
    client.post("/api/explore/candidates", json={"fen": QUIET})

    with Session() as session:
        card = srs.ensure_card(session, session.get(Puzzle, p.id))
        assert (card.due_at, card.stability, card.reps, card.lapses) == before


def test_a_take_back_retry_does_not_overwrite_the_miss(client, monkeypatch):
    """"Take back" re-sends the attempt as practice. Finding the move on the
    second go must not file a success over the miss it is retrying."""
    from coach.puzzles import solve, srs
    from coach.store.db import Puzzle, session_factory

    # No engine: a wrong move is simply wrong, with no refutation to show.
    monkeypatch.setattr(solve, "stockfish_verifier", lambda board, mv: None)
    monkeypatch.setattr(solve, "stockfish_refuter", lambda board, mv: None)

    Session = session_factory()
    with Session() as session:
        p = Puzzle(fen=QUIET, solution="d1c2", pillar="tactics", source="test",
                   rating_est=1000)
        session.add(p)
        session.commit()
        pid = p.id

    miss = client.post(f"/api/puzzles/{pid}/attempt",
                       json={"moves": ["a2a3"], "auto_first": False}).json()
    assert miss["status"] == "wrong"
    assert miss["graded"]["label"] == "again"

    with Session() as session:
        card = srs.ensure_card(session, session.get(Puzzle, pid))
        after_miss = (card.due_at, card.stability, card.reps, card.lapses)
        rating = session.get(Puzzle, pid).rating_est

    retry = client.post(f"/api/puzzles/{pid}/attempt",
                        json={"moves": ["d1c2"], "auto_first": False,
                              "practice": True}).json()
    assert retry["status"] == "solved"
    assert retry["practice"] is True
    assert retry["graded"] is None

    with Session() as session:
        card = srs.ensure_card(session, session.get(Puzzle, pid))
        assert (card.due_at, card.stability, card.reps, card.lapses) == after_miss
        assert session.get(Puzzle, pid).rating_est == rating
