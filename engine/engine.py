"""Top-level engine object: the single thing ``agent.py`` talks to.

Persists across moves within a game (the harness keeps the process alive), so the
transposition table, history heuristic and opening book stay warm.
"""

from __future__ import annotations

import chess

from engine.book import OpeningBook
from engine.config import EngineConfig
from engine.search import Searcher, SearchLimits
from engine.tablebase import Tablebase
from engine.timeman import allocate, hard_limit


class Engine:
    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig.load()
        self.searcher = Searcher(tt_budget_mb=self.config.tt_budget_mb)
        self.book = OpeningBook(self.config.book_path)
        self.tb = Tablebase(self.config.syzygy_path)
        self._last_result = None

    def warmup(self) -> None:
        """Do first-move-ish work during the 60 s init budget (JIT, table build)."""
        board = chess.Board()
        self.searcher.search_position(
            board, SearchLimits(soft_time_ms=200, hard_time_ms=400, max_depth=6)
        )
        self.searcher.tt.clear()

    def get_move(self, fen: str, time_left_ms: int) -> str:
        board = chess.Board(fen)

        legal = list(board.legal_moves)
        if not legal:
            return "0000"
        if len(legal) == 1:
            return legal[0].uci()

        book_move = self.book.probe(board, self.config.book_max_ply)
        if book_move is not None and book_move in board.legal_moves:
            return book_move.uci()

        if self.config.use_tablebase_in_search and self.tb.available:
            tb_move = self.tb.best_move(board)
            if tb_move is not None:
                return tb_move.uci()

        soft = allocate(time_left_ms, self.config.default_moves_to_go)
        hard = hard_limit(time_left_ms)
        limits = SearchLimits(soft_time_ms=soft, hard_time_ms=max(soft, hard))
        result = self.searcher.search_position(board, limits)
        self._last_result = result

        move = result.best_move or legal[0]
        if move not in board.legal_moves:
            move = legal[0]
        return move.uci()

    def close(self) -> None:
        self.book.close()
        self.tb.close()
