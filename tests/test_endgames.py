"""Every shipped endgame drill must be a legal position whose stated goal is true.

A drill that claims "you can hold this" about a lost position teaches the
opposite of the lesson, so the whole set is checked against Stockfish. Marked
slow: it needs the binary and a few seconds per position.
"""

import chess
import pytest

from coach.drills.adjudicate import DRAW, WIN, Verdict, _outcome, _verdict, describe, evaluate
from coach.drills.endgames import ENDGAMES

MATE_CP = 100_000


def _stockfish():
    import shutil

    if not shutil.which("stockfish"):
        pytest.skip("stockfish not installed")
    from coach.sparring.backend import StockfishBackend

    return StockfishBackend()


# ------------------------------------------------------------- offline checks

def test_every_position_is_legal_and_well_formed():
    keys = set()
    for e in ENDGAMES:
        board = chess.Board(e.fen)
        assert board.is_valid(), f"{e.key}: illegal position ({board.status()})"
        assert not board.is_game_over(), f"{e.key}: already over"
        assert e.goal in (WIN, DRAW)
        assert e.mode in ("key-move", "play-out")
        assert e.idea.strip(), f"{e.key}: a drill with no idea teaches nothing"
        assert e.key not in keys, f"duplicate key {e.key}"
        keys.add(e.key)


def test_outcome_thresholds():
    assert _outcome(900, None) == "win"
    assert _outcome(-900, None) == "loss"
    assert _outcome(30, None) == "draw"
    assert _outcome(0, 5) == "win"
    assert _outcome(0, -5) == "loss"


def test_a_draw_goal_is_also_met_by_winning():
    assert _verdict(900, None, DRAW).held is True
    assert _verdict(0, None, DRAW).held is True
    assert _verdict(-900, None, DRAW).held is False


def test_a_win_goal_is_not_met_by_drawing():
    assert _verdict(900, None, WIN).held is True
    assert _verdict(0, None, WIN).held is False


def test_describe_is_specific_about_what_went_wrong():
    assert "draw" in describe(WIN, _verdict(0, None, WIN)).lower()
    assert "losing" in describe(DRAW, _verdict(-900, None, DRAW)).lower()


def test_terminal_positions_need_no_engine():
    # Black is mated; from White's point of view that is a win.
    board = chess.Board("7k/5QQ1/8/8/8/8/8/7K b - - 0 1")
    v = evaluate(board, chess.WHITE, WIN, scorer=None)
    assert isinstance(v, Verdict) and v.outcome == WIN and v.held

    stalemate = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    v2 = evaluate(stalemate, chess.WHITE, WIN, scorer=None)
    assert v2.outcome == DRAW and not v2.held


def test_missing_engine_returns_none_rather_than_guessing():
    class Broken:
        def score_roots(self, board, budget):
            raise RuntimeError("no engine")

    board = chess.Board("8/8/8/4k3/8/8/8/3QK3 w - - 0 1")
    assert evaluate(board, chess.WHITE, WIN, scorer=Broken()) is None


# --------------------------------------------------------- engine-backed check

@pytest.mark.slow
def test_stated_goal_matches_the_engine():
    engine = _stockfish()
    try:
        wrong = []
        for e in ENDGAMES:
            board = chess.Board(e.fen)
            v = evaluate(board, board.turn, e.goal, scorer=engine, nodes=1_500_000)
            assert v is not None, f"{e.key}: could not adjudicate"
            if v.outcome != e.goal:
                wrong.append(f"{e.key}: claims {e.goal}, engine says {v.outcome} ({v.cp}cp)")
        assert not wrong, "endgame set makes false claims:\n  " + "\n  ".join(wrong)
    finally:
        engine.close()
