"""Play a single game between two move functions and return the result.

A move function has the signature ``fn(board: chess.Board, time_left_ms: int) -> str``
and returns a UCI move. Each side gets its own simulated clock so time losses are
real losses, mirroring the competition harness.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import chess

MoveFn = Callable[[chess.Board, int], str]

WHITE_WIN, DRAW, BLACK_WIN = 1.0, 0.5, 0.0

_ADJUDICATE_PLIES = 300
_PIECE_VAL = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


@dataclass
class GameOutcome:
    result: float          # from White's perspective: 1.0 / 0.5 / 0.0
    reason: str
    plies: int
    flagged: bool = False


def _material(board: chess.Board) -> int:
    total = 0
    for pt, v in _PIECE_VAL.items():
        total += v * (len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK)))
    return total


def play_game(
    white: MoveFn,
    black: MoveFn,
    start_fen: str = chess.STARTING_FEN,
    start_clock_ms: int = 10_000,
    increment_ms: int = 500,
) -> GameOutcome:
    board = chess.Board(start_fen)
    clocks = {chess.WHITE: start_clock_ms, chess.BLACK: start_clock_ms}
    fns = {chess.WHITE: white, chess.BLACK: black}

    import time as _time

    while True:
        if board.is_checkmate():
            return GameOutcome(
                BLACK_WIN if board.turn == chess.WHITE else WHITE_WIN,
                "checkmate", board.ply(),
            )
        if (
            board.is_stalemate()
            or board.is_insufficient_material()
            or board.is_seventyfive_moves()
            or board.is_fivefold_repetition()
            or board.can_claim_draw()
        ):
            return GameOutcome(DRAW, "draw", board.ply())
        if board.ply() >= _ADJUDICATE_PLIES:
            m = _material(board)
            if m >= 4:
                return GameOutcome(WHITE_WIN, "adjudicated-material", board.ply())
            if m <= -4:
                return GameOutcome(BLACK_WIN, "adjudicated-material", board.ply())
            return GameOutcome(DRAW, "adjudicated-drawn", board.ply())

        stm = board.turn
        t0 = _time.monotonic()
        try:
            uci = fns[stm](board, int(clocks[stm]))
        except Exception:  # noqa: BLE001 - an engine that crashes loses
            return GameOutcome(
                BLACK_WIN if stm == chess.WHITE else WHITE_WIN, "engine-crash", board.ply()
            )
        elapsed_ms = (_time.monotonic() - t0) * 1000
        clocks[stm] -= elapsed_ms
        if clocks[stm] < 0:
            return GameOutcome(
                BLACK_WIN if stm == chess.WHITE else WHITE_WIN,
                "flag", board.ply(), flagged=True,
            )
        clocks[stm] += increment_ms

        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            move = None
        if move is None or move not in board.legal_moves:
            return GameOutcome(
                BLACK_WIN if stm == chess.WHITE else WHITE_WIN, "illegal-move", board.ply()
            )
        board.push(move)
