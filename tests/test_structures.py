"""Pawn structures: detection on the textbook positions, and the quiz."""

from __future__ import annotations

import random

import chess
import pytest
from fastapi.testclient import TestClient

from coach.openings import structures as st


def _keys(fen):
    return dict(st.detect(chess.Board(fen)))


# Each a middlegame position with pieces still on, move number past the opening.
@pytest.mark.parametrize("fen,key,owner", [
    # IQP for White (Panov / QGA type): d4, no c- or e-pawns.
    ("r1bq1rk1/pp2bppp/2n1pn2/8/3P4/2NB1N2/PP3PPP/R1BQ1RK1 w - - 0 10", "iqp", "white"),
    # Carlsbad: White d4 e3, no c; Black c6 d5, no e.
    ("r1bq1rk1/pp1nbppp/2p2n2/3p4/3P4/2NBPN2/PPQ2PPP/R1B2RK1 w - - 0 10", "carlsbad", "white"),
    # Maroczy: White c4 e4 no d; Black d6 no c.
    ("r1bq1rk1/pp2ppbp/2np1np1/8/2P1P3/2N1B3/PP2BPPP/R2QK1NR w KQ - 0 10", "maroczy", "white"),
    # Advance French chains.
    ("r1b1kbnr/pp3ppp/1qn1p3/2ppP3/3P4/2P2N2/PP3PPP/RNBQKB1R w KQkq - 0 8",
     "french-advance", "white"),
    # King's Indian chain.
    ("r1bq1rk1/pppn1pbp/3p1np1/3Pp3/2P1P3/2N2N2/PP2BPPP/R1BQ1RK1 w - - 0 9", "kid-chain", "white"),
    # Black hedgehog: a6 b6 d6 e6, no c; White c4 e4.
    ("r2q1rk1/1b1nbppp/pp1ppn2/8/2P1P3/1PN2N2/P2QBPPP/R1B2RK1 w - - 0 12", "hedgehog", "black"),
    # Black Stonewall: c6 d5 e6 f5.
    ("rnbq1rk1/pp2b1pp/2p1pn2/3p1p2/2PP4/5NP1/PP2PPBP/RNBQ1RK1 w - - 0 8", "stonewall", "black"),
    # Najdorf d6/e5 vs e4: the Boleslavsky hole is Black's.
    ("r1bqkb1r/1p2pppp/p1np1n2/4p3/4P3/1NN5/PPP1BPPP/R1BQK2R w KQkq - 0 8",
     "boleslavsky-hole", "black"),
])
def test_textbook_structures_are_recognised(fen, key, owner):
    board = chess.Board(fen)
    assert board.is_valid(), board.status()
    found = _keys(fen)
    assert key in found, f"expected {key}, got {found}"
    assert found[key] == owner


def test_a_lone_pawn_still_on_d6_is_not_an_iqp():
    fen = "r1bq1rk1/pp3ppp/2np1n2/8/8/2N2N2/PP2BPPP/R1BQ1RK1 w - - 0 10"
    assert chess.Board(fen).is_valid()
    assert "iqp" not in _keys(fen)


def test_nothing_in_the_opening_or_a_bare_ending():
    assert st.detect(chess.Board()) == []
    assert st.detect(chess.Board("8/5k2/8/3p4/3P4/8/5K2/8 w - - 0 40")) == []


def test_every_structure_has_both_plans():
    assert len(st.STRUCTURES) >= 15
    for s in st.STRUCTURES:
        assert s.owner_plan and s.against_plan and s.breaks and s.pieces and s.recognise
    assert len(st.BY_KEY) == len(st.STRUCTURES)


def test_describe_for_takes_the_viewer_side():
    me = st.describe_for("iqp", "white", "black")
    assert not me["is_owner"] and me["plan"] == st.BY_KEY["iqp"].against_plan
    me = st.describe_for("iqp", "white", "white")
    assert me["is_owner"] and me["plan"] == st.BY_KEY["iqp"].owner_plan


def test_quiz_has_one_right_answer_among_three():
    q = st.quiz("carlsbad", True, random.Random(3))
    assert len(q["options"]) == 3 and len(set(q["options"])) == 3
    assert q["options"][q["answer"]] == st.BY_KEY["carlsbad"].owner_plan


def test_answer_endpoint_checks_on_the_server(monkeypatch):
    monkeypatch.setattr("coach.server.app._REFRESH_ON_START", False, raising=False)
    from coach.server.app import app

    c = TestClient(app)
    q = c.get("/api/structures/maroczy/quiz").json()
    assert "answer" not in q and len(q["options"]) == 3
    right = st.BY_KEY["maroczy"].owner_plan if q["is_owner"] else st.BY_KEY["maroczy"].against_plan
    r = c.post("/api/structures/maroczy/answer",
               json={"is_owner": q["is_owner"], "choice": right}).json()
    assert r["correct"] is True
    wrong = next(o for o in q["options"] if o != right)
    assert c.post("/api/structures/maroczy/answer",
                  json={"is_owner": q["is_owner"], "choice": wrong}).json()["correct"] is False
    assert c.get("/api/structures/nope/quiz").status_code == 404
    listing = c.get("/api/structures").json()
    assert listing["structures"][0]["key"] and "profile" in listing
