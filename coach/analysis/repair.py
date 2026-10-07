"""Recompute derived move fields from the evaluations already stored.

Some fields are pure functions of what Stockfish already told us -
``eval_before_cp``, ``eval_after_cp``, ``fen_before`` - which means changing how
they are derived does not require re-running the engine, by far the most
expensive part of the pipeline. That covers cp-loss, the whole classification
(pillar, motif, forcing-ness, decision weight) and per-move time spent.

What it does *not* cover is anything needing a fresh search: the refutation line,
and the same-budget re-evaluation of the played move. Those need
``coach analyze --refresh``.

Each repair is idempotent and reports how many rows it touched, so it is safe to
run at the end of every analysis pass.
"""

from __future__ import annotations

import chess
from sqlalchemy import select

from coach.analysis.annotate import clamped_cp_loss
from coach.analysis.classify import CLASSIFIER_VERSION, classify_move
from coach.analysis.timing import compute_seconds_spent
from coach.config import CoachConfig
from coach.store.db import Game, Move, Puzzle, get_setting, session_factory, set_setting

_VERSION_KEY = "classifier_version"
_PUZZLE_RATING_KEY = "puzzle_rating_version"


def recompute_cp_loss(db_path=None, batch: int = 5000, cfg: CoachConfig | None = None) -> int:
    """Re-derive cp_loss (and its error bucket) for every scored move.

    Covers the opponent's moves as well as the user's. Both evaluations were
    always stored for them - only the subtraction was skipped - and they are the
    sample of real play at a known rating that the sparring calibration needs.
    """
    prof = (cfg or CoachConfig()).profile
    Session = session_factory(db_path)
    changed = 0
    with Session() as session:
        ids = list(session.scalars(
            select(Move.id).where(Move.eval_before_cp.isnot(None),
                                  Move.eval_after_cp.isnot(None))
        ))
        for start in range(0, len(ids), batch):
            chunk = ids[start:start + batch]
            for m in session.scalars(select(Move).where(Move.id.in_(chunk))):
                new = clamped_cp_loss(m.eval_before_cp, m.eval_after_cp, m.side == "white")
                if new is None:
                    continue
                bucket = (
                    "blunder" if new >= prof.blunder_cp
                    else "mistake" if new >= prof.mistake_cp
                    else "inaccuracy" if new >= prof.inaccuracy_cp
                    else "ok"
                ) if m.is_user_move else m.error_bucket
                if new == m.cp_loss and bucket == m.error_bucket:
                    continue
                m.cp_loss = new
                m.error_bucket = bucket
                changed += 1
            session.commit()
    return changed


def reclassify(db_path=None, batch: int = 2000, cfg: CoachConfig | None = None) -> int:
    """Re-run the classifier over stored positions.

    Needed whenever the pillar routing or the tag set changes. Rebuilding the
    board from ``fen_before`` costs a few tens of seconds over a whole database
    and saves an hour of Stockfish.
    """
    prof = (cfg or CoachConfig()).profile
    Session = session_factory(db_path)
    changed = 0
    with Session() as session:
        ids = list(session.scalars(
            select(Move.id).where(Move.is_user_move.is_(True), Move.cp_loss.isnot(None))
        ))
        for start in range(0, len(ids), batch):
            chunk = ids[start:start + batch]
            for m in session.scalars(select(Move).where(Move.id.in_(chunk))):
                try:
                    board = chess.Board(m.fen_before)
                    played = chess.Move.from_uci(m.uci)
                except ValueError:
                    continue
                if played not in board.legal_moves:
                    continue
                best = None
                if m.best_move_uci:
                    try:
                        best = chess.Move.from_uci(m.best_move_uci)
                    except ValueError:
                        best = None
                tags = classify_move(board, played, best, m.cp_loss,
                                     m.eval_before_cp, m.second_best_cp, prof)
                motif = tags.motif or None
                # `punish_miss` is added to the motif string by the analysis pass
                # and is not re-derivable here, so carry it across rather than
                # dropping it on every repair.
                if m.punish_miss:
                    motif = ((motif + ",") if motif else "") + "missed-punish"
                before = (m.pillar, m.motif, m.forcingness, m.decision_w, m.phase)
                m.phase = tags.phase
                m.forcingness = tags.forcingness
                m.pillar = tags.pillar
                m.error_bucket = tags.error_bucket
                m.motif = motif
                m.endgame_type = tags.endgame_type
                m.context = tags.context
                m.decision_w = tags.decision_w
                if before != (m.pillar, m.motif, m.forcingness, m.decision_w, m.phase):
                    changed += 1
            session.commit()
    return changed


def rerate_puzzles(db_path=None, batch: int = 2000) -> int:
    """Re-estimate ``rating_est`` for every puzzle mined from a real game.

    Derived purely from things already stored - the move's cp-loss and phase, and
    the user's own rating in that game - so this costs a few seconds and no
    engine time. It exists because the estimate had a floor of 400 that a genuine
    beginner sat below, and because the two callers of the miner disagreed about
    what to pass for the user's rating. Both are fixed in `miner`; this brings
    the library that was written under the old rules into line with the new ones.
    """
    from coach.puzzles.miner import _rating_estimate, user_elo_for
    from coach.puzzles.srs import _OLD_RATING_FLOOR, _RATING_KEY

    Session = session_factory(db_path)
    changed = 0
    with Session() as session:
        # The ability estimate had a floor of 600 and a real beginner sat below
        # it, so a rating resting exactly there is the clamp talking, not a
        # measurement - and `selection_rating` anchors the whole queue to it.
        # Re-seed it from the user's own game ratings and let it move from there.
        stored = get_setting(session, _RATING_KEY)
        if stored and int(float(stored)) == _OLD_RATING_FLOOR:
            from coach.profile.rating import estimate_rating

            seed = estimate_rating(None, db_path)
            if seed:
                set_setting(session, _RATING_KEY, str(int(seed)))
                session.commit()
    with Session() as session:
        ids = list(session.scalars(
            select(Puzzle.id).where(Puzzle.source == "own-game",
                                    Puzzle.game_id.isnot(None),
                                    Puzzle.ply.isnot(None))
        ))
        for start in range(0, len(ids), batch):
            chunk = ids[start:start + batch]
            for p in session.scalars(select(Puzzle).where(Puzzle.id.in_(chunk))):
                game = session.get(Game, p.game_id)
                if game is None:
                    continue
                move = session.scalar(
                    select(Move).where(Move.game_id == p.game_id, Move.ply == p.ply)
                )
                if move is None or move.cp_loss is None:
                    continue
                new = _rating_estimate(int(move.cp_loss), move.phase, user_elo_for(game))
                if new != p.rating_est:
                    p.rating_est = new
                    changed += 1
            session.commit()
    return changed


def backfill_timing(db_path=None) -> int:
    """Fill ``seconds_spent`` from the clock readings already ingested."""
    Session = session_factory(db_path)
    filled = 0
    with Session() as session:
        gids = list(session.scalars(select(Game.id)))
        for gid in gids:
            game = session.get(Game, gid)
            if game is None:
                continue
            if all(m.seconds_spent is not None or m.clock_ms is None for m in game.moves):
                continue
            filled += compute_seconds_spent(game.moves, game.time_control)
        session.commit()
    return filled


def repair_all(db_path=None, cfg: CoachConfig | None = None, force: bool = False) -> dict:
    """Run every cheap repair, skipping the classifier pass when nothing changed.

    ``reclassify`` walks every stored position, so running it after each nightly
    delta of three games would be pure waste. A version marker in ``settings``
    makes it run exactly once per classifier change.
    """
    out = {"cp_loss": recompute_cp_loss(db_path, cfg=cfg),
           "seconds_spent": backfill_timing(db_path)}

    Session = session_factory(db_path)
    with Session() as session:
        stored = get_setting(session, _VERSION_KEY, "0") or "0"
    if force or stored != str(CLASSIFIER_VERSION):
        out["reclassified"] = reclassify(db_path, cfg=cfg)
        with Session() as session:
            set_setting(session, _VERSION_KEY, str(CLASSIFIER_VERSION))
            session.commit()

    from coach.puzzles.miner import PUZZLE_RATING_VERSION

    with Session() as session:
        stored = get_setting(session, _PUZZLE_RATING_KEY, "0") or "0"
    if force or stored != str(PUZZLE_RATING_VERSION):
        out["puzzles_rerated"] = rerate_puzzles(db_path)
        with Session() as session:
            set_setting(session, _PUZZLE_RATING_KEY, str(PUZZLE_RATING_VERSION))
            session.commit()
    return out
