"""Same idea, new position: test the pattern, not the memory of a board.

A puzzle mined from your own game comes back on the spaced-repetition
schedule as the *same* position. By the third review the answer is often
remembered rather than found - the card's interval keeps growing while the
idea it was meant to teach may never have transferred at all.

So once a card has been passed a couple of times, a sibling is pulled forward:
a different position that shares the idea - the same motif, or for endgame and
positional cards the same pillar - from another of your games first, and from
the imported Lichess library when your own games have nothing suitable. The
sibling carries ``variant_of`` so its result can be read as a *transfer* test:
"solved the original four times; solved a new fork position?" is the question
spaced repetition alone can't answer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from coach.puzzles.srs import GOOD, ensure_card, puzzle_motifs
from coach.store.db import Puzzle, SrsCard, get_setting, set_setting

# Reviews passed before the position is presumed memorised.
MIN_REPS = 2
RATING_WINDOW = 250
# At most this many transfer tests waiting at once. Without a cap every passed
# review of a well-known card pulled another sibling forward, and a long warm-up
# could leave the next day's queue full of them.
MAX_PENDING = 8
_STATS_KEY = "transfer_stats"
SOURCES = ("own-game", "library")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def find_variant(session: Session, puzzle: Puzzle, now: datetime | None = None) -> Puzzle | None:
    """The best sibling for ``puzzle``: shares its idea, comes from a different
    game, is near its difficulty, and is not already waiting in the queue."""
    now = now or _now()
    tags = puzzle_motifs(puzzle)
    q = (
        select(Puzzle, SrsCard)
        .outerjoin(SrsCard, SrsCard.puzzle_id == Puzzle.id)
        .where(Puzzle.id != puzzle.id, Puzzle.source.in_(SOURCES),
               Puzzle.pillar == puzzle.pillar)
        # Already due means it is coming anyway; pulling it forward adds nothing.
        .where(or_(SrsCard.id.is_(None), SrsCard.due_at > now, SrsCard.reps == 0))
    )
    if puzzle.game_id is not None:
        q = q.where(or_(Puzzle.game_id.is_(None), Puzzle.game_id != puzzle.game_id))
    if tags:
        q = q.where(or_(*[Puzzle.motif.like(f"%{t}%") for t in tags]))
    if puzzle.rating_est:
        q = q.where(or_(Puzzle.rating_est.is_(None),
                        Puzzle.rating_est.between(puzzle.rating_est - RATING_WINDOW,
                                                  puzzle.rating_est + RATING_WINDOW)))
    rows = session.execute(q.limit(200)).all()
    if not rows:
        return None

    def score(row) -> tuple:
        p, card = row
        shared = len(set(tags) & set(puzzle_motifs(p))) if tags else 0
        own = 1 if p.source == "own-game" else 0       # your own games first
        unseen = 1 if (card is None or not card.reps) else 0
        gap = abs((p.rating_est or puzzle.rating_est or 0) - (puzzle.rating_est or 0))
        return (shared, own, unseen, -gap)

    rows.sort(key=score, reverse=True)
    return rows[0][0]


def maybe_schedule(session: Session, puzzle: Puzzle, card: SrsCard, grade: int,
                   now: datetime | None = None) -> Puzzle | None:
    """After a passed review of a well-known card, bring a sibling forward."""
    if grade < GOOD or (card.reps or 0) < MIN_REPS or puzzle.source not in SOURCES:
        return None
    now = now or _now()
    from sqlalchemy import func

    pending = select(func.count(Puzzle.id)).where(Puzzle.variant_of.isnot(None))
    if (session.scalar(pending) or 0) >= MAX_PENDING:
        return None
    # One transfer test per original at a time.
    if session.scalar(select(Puzzle.id).where(Puzzle.variant_of == puzzle.id).limit(1)):
        return None
    sib = find_variant(session, puzzle, now)
    if sib is None:
        return None
    sc = ensure_card(session, sib)
    sc.due_at = now
    sib.variant_of = puzzle.id
    return sib


def record_transfer(session: Session, puzzle: Puzzle, solved: bool) -> None:
    """Filed when a sibling is answered: did the idea carry over?"""
    if not puzzle.variant_of:
        return
    data = transfer_stats(session)
    for key in ["all", *puzzle_motifs(puzzle)[:3]]:
        row = data.setdefault(key, {"n": 0, "solved": 0})
        row["n"] += 1
        row["solved"] += 1 if solved else 0
    set_setting(session, _STATS_KEY, json.dumps(data, sort_keys=True))
    # One transfer test per pull; later reviews of it are ordinary.
    puzzle.variant_of = None


def transfer_stats(session: Session) -> dict:
    raw = get_setting(session, _STATS_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def summary(session: Session) -> dict:
    return {k: {"n": v["n"], "rate": round(v["solved"] / v["n"], 3)}
            for k, v in transfer_stats(session).items() if v.get("n")}
