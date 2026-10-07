"""Play an endgame drill out against the engine.

Stateless on purpose: each request carries the position, so the drill survives a
reload and needs no server-side session. The client cannot cheat itself into a
better position anyway - the adjudicator re-evaluates from whatever it is sent.

Success and failure are defined by the goal, not by a line:

* **win** - deliver mate. Any move that lets the position slip to a draw or a
  loss ends the drill as a failure, with the evaluation shown.
* **draw** - reach a drawn ending (stalemate, insufficient material, repetition,
  fifty moves), or simply survive ``HOLD_MOVES`` of the engine's best efforts
  with the draw intact. Losing it ends the drill.
"""

from __future__ import annotations

import chess

from coach.drills.adjudicate import DRAW, WIN, describe, evaluate

# The defender plays properly - a drill you beat by hoping teaches nothing.
DEFENCE_NODES = 200_000
# Surviving this many of your own moves with the draw intact counts as holding it.
HOLD_MOVES = 16
# Safety valve so a drill can't run forever.
MAX_MOVES = 60


def _terminal(board: chess.Board, side: chess.Color, goal: str) -> tuple[str, str] | None:
    """(status, message) when the drill has reached its natural end."""
    if board.is_checkmate():
        winner_is_side = board.turn != side
        if goal == WIN and winner_is_side:
            return "success", "Mate. That's the technique."
        if winner_is_side:
            return "success", "Mate — better than the draw you needed."
        return "failed", "You got mated."
    if board.is_stalemate():
        return (("failed", "Stalemate — the draw you had to avoid.") if goal == WIN
                else ("success", "Stalemate. Held."))
    if board.is_insufficient_material():
        return (("failed", "No mating material left — the win is gone.") if goal == WIN
                else ("success", "Insufficient material. Held."))
    if board.is_fivefold_repetition() or board.is_seventyfive_moves() or board.can_claim_draw():
        return (("failed", "Drawn by repetition or the fifty-move rule.") if goal == WIN
                else ("success", "Drawn by repetition. Held."))
    return None


def _dests(board: chess.Board) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for mv in board.legal_moves:
        out.setdefault(chess.square_name(mv.from_square), []).append(
            chess.square_name(mv.to_square)
        )
    return out


def _ply_index(board: chess.Board) -> int:
    """Plies elapsed since the start of the game this FEN describes."""
    return 2 * (board.fullmove_number - 1) + (0 if board.turn == chess.WHITE else 1)


def moves_played_between(start_fen: str, fen: str) -> int | None:
    """How many moves the drilling side has made, read off the two positions.

    The drill is stateless, so the move count used to travel in the request -
    and it decides real outcomes: ``HOLD_MOVES`` ends a draw drill in success
    and ``MAX_MOVES`` ends a win drill in failure. A client sending 999, whether
    from a bug or a replayed request, could finish a drill without playing it.
    Both FENs carry an honest fullmove counter, so the number can simply be
    derived. Returns None if either position will not parse.
    """
    try:
        a, b = chess.Board(start_fen), chess.Board(fen)
    except ValueError:
        return None
    elapsed = _ply_index(b) - _ply_index(a)
    if elapsed < 0:
        return None
    # The drilling side moves on the first ply and every second one after it.
    return (elapsed + 1) // 2


def start(fen: str) -> dict:
    board = chess.Board(fen)
    return {
        "status": "continue",
        "fen": board.fen(),
        "user_colour": "white" if board.turn == chess.WHITE else "black",
        "dests": _dests(board),
        "moves_played": 0,
        "message": "",
    }


def step(fen: str, uci: str, goal: str, mode: str, user_colour: str,
         moves_played: int = 0, scorer=None) -> dict:
    """Apply the user's move, adjudicate, and (in play-out) reply.

    ``fen`` is the position before the user's move; ``user_colour`` fixes whose
    result the goal refers to, since the side to move alternates.
    """
    board = chess.Board(fen)
    side = chess.WHITE if user_colour == "white" else chess.BLACK

    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return {"status": "error", "message": f"bad move {uci}", "fen": board.fen()}
    if move not in board.legal_moves:
        # Retry an under-specified promotion as a queen promotion.
        promoted = chess.Move(move.from_square, move.to_square, promotion=chess.QUEEN)
        if promoted in board.legal_moves:
            move = promoted
        else:
            return {"status": "error", "message": "illegal move", "fen": board.fen()}

    played_san = board.san(move)
    board.push(move)
    moves_played = max(0, moves_played) + 1

    term = _terminal(board, side, goal)
    if term:
        status, message = term
        return _result(status, board, side, message, played_san, moves_played)

    verdict = evaluate(board, side, goal, scorer=scorer)
    if verdict is not None and not verdict.held:
        return _result("failed", board, side, describe(goal, verdict), played_san,
                       moves_played, verdict=verdict)

    if mode == "key-move":
        msg = describe(goal, verdict) if verdict else "Move accepted."
        return _result("success", board, side, msg, played_san, moves_played, verdict=verdict)

    # play-out: the engine defends (or presses) with its best move.
    if goal == DRAW and moves_played >= HOLD_MOVES:
        return _result("success", board, side,
                       f"Held it for {moves_played} moves. That's the technique.",
                       played_san, moves_played, verdict=verdict)
    if moves_played >= MAX_MOVES:
        return _result("failed", board, side,
                       "Ran out of moves — the win needs to be converted faster.",
                       played_san, moves_played, verdict=verdict)

    reply = _engine_move(board, scorer)
    if reply is not None:
        board.push(reply)
        term = _terminal(board, side, goal)
        if term:
            status, message = term
            return _result(status, board, side, message, played_san, moves_played)

    return _result("continue", board, side, "", played_san, moves_played, verdict=verdict)


def _engine_move(board: chess.Board, scorer=None) -> chess.Move | None:
    if scorer is None:
        from coach.sparring.backend import shared_backend

        try:
            scorer = shared_backend()
        except Exception:  # noqa: BLE001
            return None
    try:
        top = scorer.best(board, DEFENCE_NODES)
    except Exception:  # noqa: BLE001
        return None
    return top[0] if top else None


def _result(status: str, board: chess.Board, side: chess.Color, message: str,
            played_san: str, moves_played: int, verdict=None) -> dict:
    return {
        "status": status,
        "fen": board.fen(),
        "dests": _dests(board) if status == "continue" else {},
        "user_colour": "white" if side == chess.WHITE else "black",
        "last_move": board.move_stack[-1].uci() if board.move_stack else None,
        "played_san": played_san,
        "moves_played": moves_played,
        "message": message,
        "eval_cp": verdict.cp if verdict else None,
        "outcome": verdict.outcome if verdict else None,
    }
