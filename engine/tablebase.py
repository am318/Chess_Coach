"""Syzygy tablebase probing (optional).

Used by the coaching bot's analysis for perfect endgame verdicts, and by the
competition engine only if tablebase files fit inside the 50 MB submission
budget (they usually do not for 3-4-5, so this is off by default in agent.zip).
"""

from __future__ import annotations

from pathlib import Path

import chess
import chess.syzygy


class Tablebase:
    def __init__(self, path: str | Path | None) -> None:
        self._tb: chess.syzygy.Tablebase | None = None
        if path and Path(path).is_dir():
            try:
                self._tb = chess.syzygy.open_tablebase(str(path))
            except (OSError, ValueError):
                self._tb = None

    @property
    def available(self) -> bool:
        return self._tb is not None

    def probe_wdl(self, board: chess.Board) -> int | None:
        """Win/draw/loss from side-to-move view: +2/+1/0/-1/-2, or None."""
        if self._tb is None or chess.popcount(board.occupied) > 5:
            return None
        try:
            return self._tb.probe_wdl(board)
        except (KeyError, chess.syzygy.MissingTableError, ValueError):
            return None

    def best_move(self, board: chess.Board) -> chess.Move | None:
        """Pick a move that preserves the best WDL, minimising DTZ on wins."""
        if self._tb is None or chess.popcount(board.occupied) > 5:
            return None
        best: tuple[int, int] | None = None
        best_move: chess.Move | None = None
        for move in board.legal_moves:
            board.push(move)
            try:
                wdl = self._tb.probe_wdl(board)
                dtz = self._tb.probe_dtz(board)
            except (KeyError, chess.syzygy.MissingTableError, ValueError):
                board.pop()
                return None
            board.pop()
            # Opponent's WDL is negated to our perspective; smaller |dtz| better on wins.
            key = (-wdl, abs(dtz))
            if best is None or key < best:
                best = key
                best_move = move
        return best_move

    def close(self) -> None:
        if self._tb is not None:
            self._tb.close()
            self._tb = None
