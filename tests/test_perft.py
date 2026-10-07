"""Move-generation correctness via perft node counts.

We rely on python-chess for generation in v1, so this mostly guards against a
broken environment / wrong python-chess version. When the numba movegen lands
(M6) these same numbers gate it.
"""

import chess
import pytest

_CASES = [
    # (fen, depth, expected nodes) - canonical perft positions.
    (chess.STARTING_FEN, 1, 20),
    (chess.STARTING_FEN, 2, 400),
    (chess.STARTING_FEN, 3, 8_902),
    (chess.STARTING_FEN, 4, 197_281),
    # Kiwipete
    ("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", 2, 2_039),
    ("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", 3, 97_862),
    ("8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", 4, 43_238),
    ("r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1", 3, 9_467),
    ("rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8", 3, 62_379),
]


def _perft(board: chess.Board, depth: int) -> int:
    if depth == 0:
        return 1
    total = 0
    for move in board.legal_moves:
        board.push(move)
        total += _perft(board, depth - 1)
        board.pop()
    return total


@pytest.mark.parametrize("fen,depth,expected", _CASES)
def test_perft(fen, depth, expected):
    assert _perft(chess.Board(fen), depth) == expected
