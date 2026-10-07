"""Tactical sanity: the search must find well-known mates/wins quickly.

This is a tiny smoke suite. The full WAC/ERET suites live in M2 as a tracked
solve-rate metric, not a hard gate.
"""

import chess
import pytest

from engine.search import Searcher, SearchLimits

_CASES = [
    # (fen, best_move_uci, note)
    ("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1", "a1a8", "back-rank mate in 1"),
    ("r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4",
     None, "black is mated (scholar's) - just needs a legal reply search"),
    ("2rr3k/pp3pp1/1nnqbN1p/3pN3/2pP4/2P3Q1/PPB4P/4RRK1 w - - 0 1", "g3g6", "Qxg6 crush"),
    ("r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4", "h5f7",
     "scholar's mate in 1"),
]


@pytest.mark.parametrize("fen,best,note", _CASES)
def test_finds_tactic(fen, best, note):
    board = chess.Board(fen)
    if not list(board.legal_moves):
        pytest.skip(f"{note}: terminal position")
    searcher = Searcher()
    result = searcher.search_position(
        board, SearchLimits(soft_time_ms=1500, hard_time_ms=2500, max_depth=12)
    )
    assert result.best_move is not None
    if best is not None:
        assert result.best_move.uci() == best, f"{note}: got {result.best_move}"
