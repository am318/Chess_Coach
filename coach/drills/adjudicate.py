"""Judge an endgame drill by its result rather than by a stored line.

A Lucena has several winning moves and a Philidor several holding ones. What
matters is whether the player kept the win (or kept the draw), not whether they
found the move printed in the book - so drills are scored by asking the engine
what the position is worth now, from the drilling side's point of view.

The verdict is deliberately coarse. Endgame technique is about not throwing the
result away; a drill should say "you still win this" or "that threw it away", and
never quibble about half a pawn.
"""

from __future__ import annotations

from dataclasses import dataclass

import chess

WIN, DRAW, LOSS = "win", "draw", "loss"

# Above this the side to move is winning; below the negative of it, losing.
# Deliberately wide: an endgame drill is about the result, not the margin.
WIN_THRESHOLD_CP = 250
ADJUDICATE_NODES = 250_000
MATE_CP = 100_000


@dataclass
class Verdict:
    outcome: str          # win | draw | loss, from `side`'s point of view
    cp: int               # evaluation from `side`'s point of view
    mate_in: int | None
    held: bool            # did this preserve the drill's goal?


def _outcome(cp: int, mate_in: int | None) -> str:
    if mate_in is not None:
        return WIN if mate_in > 0 else LOSS
    if cp >= WIN_THRESHOLD_CP:
        return WIN
    if cp <= -WIN_THRESHOLD_CP:
        return LOSS
    return DRAW


def evaluate(board: chess.Board, side: chess.Color, goal: str,
             scorer=None, nodes: int = ADJUDICATE_NODES) -> Verdict | None:
    """What is this position worth to ``side``, and does that meet ``goal``?

    Returns None when no engine is available, so callers fall back rather than
    guessing. Terminal positions are decided without an engine at all.
    """
    if board.is_checkmate():
        # The side to move is mated.
        loser_is_side = board.turn == side
        cp = -MATE_CP if loser_is_side else MATE_CP
        return _verdict(cp, None, goal)
    if board.is_stalemate() or board.is_insufficient_material() or \
            board.is_seventyfive_moves() or board.is_fivefold_repetition():
        return _verdict(0, None, goal)

    if scorer is None:
        from coach.sparring.backend import shared_backend

        try:
            scorer = shared_backend()
        except Exception:  # noqa: BLE001 - adjudication is best-effort
            return None
    try:
        top = scorer.best(board, nodes)
    except Exception:  # noqa: BLE001
        return None
    if not top:
        return None

    # The score is from the point of view of the side to move.
    best_cp = top[1]
    cp = best_cp if board.turn == side else -best_cp
    mate_in = None
    if abs(cp) >= MATE_CP - 1000:
        mate_in = 1 if cp > 0 else -1
    return _verdict(cp, mate_in, goal)


def _verdict(cp: int, mate_in: int | None, goal: str) -> Verdict:
    outcome = _outcome(cp, mate_in)
    # A draw goal is also met by winning - you were asked not to lose it.
    held = outcome == goal or (goal == DRAW and outcome == WIN)
    return Verdict(outcome=outcome, cp=cp, mate_in=mate_in, held=held)


def describe(goal: str, verdict: Verdict) -> str:
    """One line the drill can show the player."""
    if verdict.held:
        return "Still winning — that holds it." if goal == WIN else "Still drawn — that holds."
    if goal == WIN and verdict.outcome == DRAW:
        return "That throws the win away — it's only a draw now."
    if goal == WIN:
        return "That loses a won position."
    return "That gives up the draw — you're losing now."
