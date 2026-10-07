"""Load the drill sets into the puzzle table so they share the SRS queue.

Endgame positions and repertoire lines are both "a position plus something to
do", which is what a ``Puzzle`` already is - so they reuse the scheduler, the
rating targeting and the review flow rather than growing a parallel system.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from coach.drills.endgames import ENDGAMES
from coach.drills.repertoire import mine_repertoire
from coach.puzzles.srs import ensure_card
from coach.store.db import Puzzle, session_factory


def seed_endgames(db_path=None) -> tuple[int, int]:
    """Insert or refresh the canonical endgame drills. Idempotent.

    Returns ``(created, reset)``: how many drills were newly inserted, and how
    many had their scheduling reset because the position itself changed.
    """
    Session = session_factory(db_path)
    written = 0
    reset = 0
    with Session() as session:
        for e in ENDGAMES:
            ext = f"endgame:{e.key}"
            puzzle = session.scalar(select(Puzzle).where(Puzzle.external_id == ext))
            if puzzle is None:
                puzzle = Puzzle(external_id=ext, source="endgame")
                session.add(puzzle)
                written += 1
            # Refresh the content in place so edits to the set reach existing
            # databases without discarding the SRS history for that drill - but
            # only while it is still the same drill. A changed FEN is a different
            # position, and carrying "you have held this five times" across from
            # the old one would schedule the new one as if it were already known.
            moved = puzzle.fen is not None and puzzle.fen != e.fen
            puzzle.fen = e.fen
            puzzle.solution = ""            # judged by result, not by a stored line
            puzzle.pillar = "endgame"
            puzzle.motif = e.category
            puzzle.kind = e.mode            # "key-move" | "play-out"
            puzzle.name = e.name
            puzzle.goal = e.goal
            puzzle.rating_est = e.rating
            session.flush()
            card = ensure_card(session, puzzle)
            if moved:
                card.stability = 0.0
                card.difficulty = 5.0
                card.reps = 0
                card.lapses = 0
                card.wins = 0
                card.last_grade = None
                card.last_review = None
                card.last_ms = None
                card.avg_ms = None
                card.due_at = datetime.now(UTC).replace(tzinfo=None)
                reset += 1
        session.commit()
    return written, reset


def seed_all(account: str | None = None, db_path=None) -> dict:
    endgames, reset = seed_endgames(db_path)
    lines = mine_repertoire(account, db_path)
    out = {"endgames": endgames, "repertoire_lines": lines}
    if reset:
        out["rescheduled"] = reset
    return out
