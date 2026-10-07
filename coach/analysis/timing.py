"""How long each move actually took, and whether it was long enough.

The database already stores ``clock_ms`` - the clock *reading* after each move -
for every game that carried ``[%clk]`` comments. The interesting quantity is the
difference: seconds spent thinking, which is the previous reading for the same
side minus this one, plus whatever the increment gave back.

This matters because average centipawn loss cannot see it. A player who spends
two seconds in the one position where the evaluation could swing two pawns, and
fourteen seconds in a position where every move is fine, has a specific and
fixable habit - and it shows up in the profile as an ordinary tactical weakness,
which sends them to do more tactics puzzles rather than to slow down.

Nothing here needs an engine, so it backfills over existing games for free.
"""

from __future__ import annotations

import re

# Positions this forcing (or with this much at stake) are "critical": the move
# choice carries real consequences and deserves time on the clock.
CRITICAL_FORCINGNESS = 0.5
# ... or the top two engine moves differ by at least this much, which is the same
# statement made from the other direction.
CRITICAL_GAP_CP = 120
# Spending less than this on a critical position is the habit worth naming.
RUSHED_SECONDS = 5.0
# Above this, a "think" is long enough that calling it rushed would be silly, no
# matter how forcing the position is.
CONSIDERED_SECONDS = 15.0

_TC = re.compile(r"^\s*(\d+)\s*(?:\+\s*(\d+))?\s*$")


def parse_time_control(tc: str | None) -> tuple[int | None, int]:
    """``"600+5"`` -> ``(600, 5)``. Returns ``(None, 0)`` for daily/unknown."""
    if not tc:
        return None, 0
    tc = tc.strip()
    if "/" in tc or tc == "-":       # correspondence ("1/259200") or untimed
        return None, 0
    m = _TC.match(tc)
    if not m:
        return None, 0
    base = int(m.group(1))
    inc = int(m.group(2) or 0)
    return base, inc


def compute_seconds_spent(moves, time_control: str | None) -> int:
    """Fill ``seconds_spent`` on an ordered list of Move rows. Returns how many
    were filled.

    The first move of each side has no previous reading to difference against,
    so it is left as ``None`` rather than guessed from the base time - an opening
    move played from memory is not a measurement of thinking time.
    """
    base, inc = parse_time_control(time_control)
    prev: dict[str, int] = {}
    filled = 0
    for m in sorted(moves, key=lambda x: x.ply):
        clock = m.clock_ms
        if clock is None:
            continue
        before = prev.get(m.side)
        prev[m.side] = clock
        if before is None:
            continue
        spent = (before - clock) / 1000.0 + inc
        # A negative or absurd value means the clock comments are unreliable for
        # this game (a Berserk, a takeback, a server adjustment). Skip rather
        # than pollute the average.
        if spent < 0 or (base is not None and spent > base):
            continue
        m.seconds_spent = round(spent, 2)
        filled += 1
    return filled


def is_critical(forcingness: float | None, best_cp: int | None,
                second_cp: int | None) -> bool:
    """Did this position actually deserve time on the clock?

    Either it is forcing, or the two best moves are far enough apart that
    choosing between them is where the game is decided.
    """
    if forcingness is not None and forcingness >= CRITICAL_FORCINGNESS:
        return True
    if best_cp is not None and second_cp is not None:
        return abs(best_cp - second_cp) >= CRITICAL_GAP_CP
    return False
