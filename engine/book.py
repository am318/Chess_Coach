"""Polyglot opening-book probing.

The book file is optional. If ``data/book.bin`` (or the path in EngineConfig)
is missing, ``probe`` returns None and the engine just searches.
"""

from __future__ import annotations

import random
from pathlib import Path

import chess
import chess.polyglot


class OpeningBook:
    def __init__(self, path: str | Path | None) -> None:
        self._reader: chess.polyglot.MemoryMappedReader | None = None
        if path and Path(path).is_file():
            try:
                self._reader = chess.polyglot.open_reader(str(path))
            except (OSError, ValueError):
                self._reader = None

    @property
    def available(self) -> bool:
        return self._reader is not None

    def probe(self, board: chess.Board, max_ply: int = 16) -> chess.Move | None:
        """Weighted-random book move, or None to fall through to search."""
        if self._reader is None or board.ply() >= max_ply:
            return None
        try:
            entries = list(self._reader.find_all(board))
        except (IndexError, KeyError):
            return None
        if not entries:
            return None
        total = sum(e.weight for e in entries) or 1
        pick = random.randint(1, total)
        acc = 0
        for e in entries:
            acc += e.weight
            if acc >= pick:
                return e.move
        return entries[0].move

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
