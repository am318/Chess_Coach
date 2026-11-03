"""Calculation mode: type the whole line, checked ply by ply."""

from __future__ import annotations

import random

import chess

from coach.puzzles import calculation as calc
from coach.store.db import Puzzle, session_factory

# White: Qxf7+ Kd8 Qf8#? Use a real forcing line from a known position.
# Scholar's-mate-style: after 1.e4 e5 2.Bc4 Nc6 3.Qh5 Nf6?? Qxf7# is mate in one.
FEN = "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4"
# A deliberately longer stored line to test depth: Bxf7+ Ke7 Qxe5+ Nxe5? is not
# forced, but the grader only needs a legal stored line.
LINE = "c4f7 e8e7 h5e5 c6e5 d2d4"


def _puzzle(solution=LINE, source="own-game", pillar="tactics"):
    return Puzzle(id=1, source=source, fen=FEN, solution=solution, pillar=pillar,
                  kind="find-best", rating_est=1400)


def test_notation_is_read_forgivingly():
    b = chess.Board(FEN)
    moves, bad = calc.parse_san_line(b, "1.Bxf7+ Ke7, 2. Qxe5+!? Nxe5")
    assert bad is None
    assert [m.uci() for m in moves] == ["c4f7", "e8e7", "h5e5", "c6e5"]
    moves, bad = calc.parse_san_line(b, "Bxf7+ Kxx9")
    assert len(moves) == 1 and bad == "Kxx9"
    moves, _ = calc.parse_san_line(b, "c4f7 e8e7")       # UCI is accepted too
    assert len(moves) == 2


def test_the_stored_line_solves_strictly():
    r = calc.check_line(_puzzle(), "Bxf7+ Ke7 Qxe5+", depth=2)
    assert r["status"] == "solved"
    assert r["needed"] == 3 and r["reached"] == 3
    assert r["solution_san"] == ["Bxf7+", "Ke7", "Qxe5+"]


def test_stopping_early_or_guessing_their_reply_wrong_fails():
    r = calc.check_line(_puzzle(), "Bxf7+ Ke7", depth=2)
    assert r["status"] == "wrong" and r["plies"][-1]["note"] == "the line stops here"
    r = calc.check_line(_puzzle(), "Bxf7+ Kd8?? Qxe5", depth=2)
    assert r["status"] == "wrong"
    assert not r["plies"][1]["ok"] and r["plies"][1]["expected"] == "Ke7"


def test_engine_accepted_alternatives_pass_and_bad_replies_do_not():
    # Treat Qxf7# as equally good; any other deviation as far behind.
    def verifier(board, mv):
        return 0 if mv.uci() == "h5f7" else 400
    r = calc.check_line(_puzzle(), "Qxf7#", depth=2, verifier=verifier)
    # The alternative is accepted, but the line still has to be the full length.
    assert r["plies"][0]["ok"] and "just as good" in r["plies"][0]["note"]


def test_depth_adapts_to_results(tmp_path):
    with session_factory(tmp_path / "c.db")() as s:
        p = _puzzle()
        s.add(p)
        s.flush()
        win = {"status": "solved", "depth": 2}
        for _ in range(5):
            out = calc.record(s, p, win)
        assert out["depth_next"] == 3
        lose = {"status": "wrong", "depth": 3}
        for _ in range(5):
            out = calc.record(s, p, lose)
        assert out["depth_next"] == 2
        assert calc.summary(s)["by_depth"]["3"]["rate"] == 0.0


def test_next_task_needs_a_long_enough_line(tmp_path):
    with session_factory(tmp_path / "n.db")() as s:
        s.add(Puzzle(source="own-game", fen=FEN, solution="c4f7", pillar="tactics",
                     kind="find-best", rating_est=1400))
        s.commit()
        assert calc.next_task(s, depth=2) is None
        s.add(_puzzle().__class__(source="own-game", fen=FEN, solution=LINE, pillar="tactics",
                                  kind="find-best", rating_est=1400))
        s.commit()
        t = calc.next_task(s, depth=3, rng=random.Random(1))
        assert t is not None and t["plies"] == 5 and t["depth"] == 3
        assert t["fen"] == FEN


def test_calc_api_round_trip(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr("coach.server.app._REFRESH_ON_START", False, raising=False)
    monkeypatch.setattr("coach.puzzles.solve.stockfish_verifier", lambda b, m: None)
    from coach.server.app import app

    with session_factory()() as s:
        s.add(Puzzle(source="own-game", fen=FEN, solution=LINE, pillar="tactics",
                     kind="find-best", rating_est=1400))
        s.commit()
    c = TestClient(app)
    t = c.get("/api/calc/next?depth=2").json()["task"]
    assert t["depth"] == 2
    r = c.post(f"/api/calc/{t['id']}/check", json={"line": "Bxf7+ Ke7 Qxe5+", "depth": 2}).json()
    assert r["status"] == "solved" and r["filed"]["rating"]
