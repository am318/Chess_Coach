"""Think-time allocation.

Competition time control: 120 s per side + 0.5 s increment per move, delivered
to the agent only as ``time_left_ms`` (our own remaining clock). We never see
the opponent clock or the move number, so we estimate moves-to-go from a decaying
horizon and always keep a safety buffer so we cannot flag.
"""

from __future__ import annotations

INCREMENT_MS = 500
# Hard floor of wall-clock we refuse to dip below before returning a move.
SAFETY_BUFFER_MS = 300
# Assume this many more moves need to be made from the current one.
DEFAULT_MOVES_TO_GO = 28
MIN_THINK_MS = 20
MAX_FRACTION_OF_REMAINING = 0.30


def allocate(time_left_ms: int, moves_to_go: int = DEFAULT_MOVES_TO_GO) -> int:
    """Return a soft time budget in milliseconds for this move.

    The search treats this as a target: it finishes the current iterative-deepening
    ply and then stops. A separate hard cap (``hard_limit``) bounds pathological
    single-ply blowups.
    """
    usable = max(0, time_left_ms - SAFETY_BUFFER_MS)
    if usable <= 0:
        return MIN_THINK_MS

    budget = usable / max(4, moves_to_go) + INCREMENT_MS * 0.8
    budget = min(budget, usable * MAX_FRACTION_OF_REMAINING)
    return int(max(MIN_THINK_MS, budget))


def hard_limit(time_left_ms: int) -> int:
    """Absolute wall-clock cap for a single move; the search must return by this."""
    return int(max(MIN_THINK_MS, min(time_left_ms - SAFETY_BUFFER_MS,
                                     time_left_ms * 0.5)))
