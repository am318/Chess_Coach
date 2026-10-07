"""Replicate the harness contract against a built agent.

Plays the engine against itself from the standard start and from a few curated
near-level FENs, simulating the 120 s + 0.5 s clock. Asserts:
  * every returned move is legal
  * the agent never exceeds its remaining time (would be a flag loss)
  * every response is <= 4096 bytes

Run AFTER building, ideally in a fresh 3.12 venv that has ONLY the competition
packages installed, to catch stray imports.
"""

from __future__ import annotations

import os
import time

import chess

from submission.agent import get_move

# Real control is 120 s + 0.5 s. For a fast CI pass, scale the clock down with
# SMOKE_CLOCK_MS; time-discipline is still exercised (the agent only ever sees
# time_left_ms). Set SMOKE_CLOCK_MS=120000 for a full-length rehearsal.
_START_CLOCK_MS = int(os.environ.get("SMOKE_CLOCK_MS", "6000"))
_INCREMENT_MS = 500
_MAX_PLIES = int(os.environ.get("SMOKE_MAX_PLIES", "40"))

_OPENING_FENS = [
    chess.STARTING_FEN,
    "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2",
    "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
]


def _play_one(start_fen: str) -> None:
    board = chess.Board(start_fen)
    clocks = {chess.WHITE: _START_CLOCK_MS, chess.BLACK: _START_CLOCK_MS}

    for _ in range(_MAX_PLIES):
        if board.is_game_over(claim_draw=True):
            break
        stm = board.turn
        t0 = time.monotonic()
        uci = get_move(board.fen(), clocks[stm])
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert len(uci.encode()) <= 4096, "response exceeds 4096 bytes"
        assert elapsed_ms <= clocks[stm], (
            f"flagged: used {elapsed_ms:.0f} ms of {clocks[stm]} ms"
        )
        move = chess.Move.from_uci(uci)
        assert move in board.legal_moves, f"illegal move {uci} in {board.fen()}"

        clocks[stm] -= elapsed_ms
        clocks[stm] += _INCREMENT_MS
        board.push(move)

    print(f"ok: {start_fen[:32]}... {board.fullmove_number} moves, "
          f"clocks W={clocks[chess.WHITE]/1000:.1f}s B={clocks[chess.BLACK]/1000:.1f}s")


def main() -> None:
    for fen in _OPENING_FENS:
        _play_one(fen)
    print("smoke test passed")


if __name__ == "__main__":
    main()
