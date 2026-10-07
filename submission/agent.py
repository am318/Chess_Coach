"""AI Chessathon entry point.

The harness imports this module once per game and calls ``get_move`` for every
move, keeping the process alive between calls. Must sit at the root of agent.zip.
Only the sandbox's preinstalled packages may be imported (transitively).
"""

from __future__ import annotations

_ENGINE = None


def _get_engine():
    global _ENGINE
    if _ENGINE is None:
        from engine.engine import Engine

        _ENGINE = Engine()
        try:
            _ENGINE.warmup()
        except Exception:  # noqa: BLE001, S110 - warmup must never break move generation
            pass
    return _ENGINE


def get_move(fen: str, time_left_ms: int) -> str:
    """Return a legal move in UCI notation for ``fen``."""
    try:
        return _get_engine().get_move(fen, time_left_ms)
    except Exception:  # noqa: BLE001 - last-ditch: return *some* legal move
        import chess

        board = chess.Board(fen)
        move = next(iter(board.legal_moves), None)
        return move.uci() if move else "0000"
