"""Build puzzles from the user's own analysed games.

The highest-value training material: the exact positions where the user went
wrong. A puzzle = the position just before a mistake/blunder/missed-mate, with
the engine's best line as the solution, tagged with the pillar + motif that
`classify.py` assigned.
"""

from __future__ import annotations

import chess
from sqlalchemy import select
from sqlalchemy.orm import Session

from coach.analysis.annotate import EVAL_CLAMP_CP, MATE_CP
from coach.puzzles.srs import ensure_card
from coach.store.db import Game, Move, Puzzle, session_factory

_MIN_CP_LOSS = 120           # mistake threshold
_MIN_SOLUTION_PLIES = 1
_MAX_SOLUTION_PLIES = 8

# A puzzle is only fair if the best move is clearly best. When the second-best
# move is within this many centipawns the solver can find a move that is just as
# good and still be told "wrong", which teaches the opposite of the lesson.
#
# This gate is deliberately looser than it looks, because it is only half the
# defence: `solve.py` also accepts any move Stockfish rates within
# ``_ACCEPT_ALT_CP`` of the best at solve time. Measured on real games, a 150cp
# gate alone discarded ~80% of the material; 80cp plus alternative-acceptance
# keeps the volume and still never marks an equal move wrong.
_MIN_UNIQUENESS_CP = 80


# Bounds on a mined puzzle's estimated rating. The floor used to be 400, which
# is at or above where a genuine beginner plays: for a ~480-rated player every
# blunder puzzle landed exactly on it, so 1,206 of 1,257 puzzles in one real
# library shared a single rating and the queue had nothing to sort by. The
# selection target sat below the entire library and difficulty targeting was
# inert - measured success rate 24% against a stated 80%.
RATING_MIN, RATING_MAX = 200, 2800
# Bumped when `_rating_estimate` or its inputs change, so `repair.rerate_puzzles`
# knows to walk the library again. v2: floor lowered from 400, and the user's Elo
# taken from the game the puzzle came from rather than from whoever happened to
# call the miner.
PUZZLE_RATING_VERSION = 2


def _rating_estimate(cp_loss: int, phase: str | None, user_elo: int | None) -> int:
    base = user_elo or 1500
    # Bigger blunders were "easier to see" -> slightly lower rating; quiet-phase
    # misses rate a touch higher.
    adj = 0
    if cp_loss >= 400:
        adj -= 120
    elif cp_loss >= 250:
        adj -= 40
    if phase == "endgame":
        adj += 40
    return max(RATING_MIN, min(RATING_MAX, base + adj))


def user_elo_for(game: Game) -> int | None:
    """The user's own rating in the game this puzzle came from.

    The miner used to take ``user_elo`` from its caller, and the callers
    disagreed: the CLI passed ``--elo`` while the web app passed nothing at all,
    so ``base`` fell back to 1500. The same mistake in the same game was rated
    400 when mined from the command line and 1380 when mined on app open. The
    game's own header is the honest answer and needs no caller to supply it.
    """
    elo = game.white_elo if game.color == "white" else game.black_elo
    return int(elo) if elo else None


def _mover_pov(m: Move, cp: int | None) -> int | None:
    """A stored White-POV score, seen from the side that moved."""
    if cp is None:
        return None
    return cp if m.side == "white" else -cp


def _is_mate_position(m: Move) -> bool:
    """The engine had a forced mate here, whatever the centipawns say."""
    best = _mover_pov(m, m.eval_before_cp)
    return best is not None and best >= MATE_CP


def _mate_is_unique(m: Move) -> bool:
    """Only one move mates, so marking the others wrong is fair.

    Centipawns cannot answer this. At +20 pawns a mate and a merely-crushing move
    both clamp to +1500 and the gap collapses to zero, so the cp gate rejected
    every mate puzzle from a winning position - which is most of them, and the
    most instructive ones at that. The raw scores answer it directly: a second
    line that also mates scores in the mate range too.

    When the second line is unknown (a single-PV pass) the puzzle is kept and the
    decision handed to `solve.py`, which re-checks alternatives against the engine
    at solve time - the same second line of defence the cp gate already relies on.
    """
    second = _mover_pov(m, m.second_best_cp)
    return second is None or second < MATE_CP


def _uniqueness_cp(m: Move) -> int | None:
    """How much better the best move is than the second best, in cp.

    Both are stored in White's POV, so flip for a Black move. Evaluations are
    clamped the same way cp-loss is: without it, "mate in 2 vs mate in 4" reads
    as a ~50000cp gap and sails through the gate, when in truth *both* moves win
    and marking one wrong would be unfair. Clamped, two mating moves collapse to
    a gap of 0 and the puzzle is correctly rejected, while mate-versus-merely-
    winning still shows a large gap and is kept.
    """
    if m.second_best_cp is None or m.eval_before_cp is None:
        return None
    sign = 1 if m.side == "white" else -1
    best = max(-EVAL_CLAMP_CP, min(EVAL_CLAMP_CP, sign * m.eval_before_cp))
    second = max(-EVAL_CLAMP_CP, min(EVAL_CLAMP_CP, sign * m.second_best_cp))
    return int(best - second)


def _candidate_moves(session: Session, game: Game) -> list[Move]:
    out = []
    for m in sorted(game.moves, key=lambda x: x.ply):
        if not m.is_user_move or m.cp_loss is None:
            continue
        if not ((m.cp_loss >= _MIN_CP_LOSS or m.mate_missed) and m.best_line and m.best_move_uci):
            continue
        # Mate positions are judged on whether a second move also mates; every
        # other position on the centipawn gap. Both ask the same question - "is
        # there a single right answer here?" - but only one of them can be asked
        # in centipawns.
        if _is_mate_position(m):
            if not _mate_is_unique(m):
                continue
        else:
            uq = _uniqueness_cp(m)
            if uq is None or uq < _MIN_UNIQUENESS_CP:
                continue
        out.append(m)
    return out


def mine_game(session: Session, game: Game, user_elo: int | None) -> int:
    added = 0
    user_elo = user_elo or user_elo_for(game)
    for m in _candidate_moves(session, game):
        exists = session.scalar(
            select(Puzzle.id).where(Puzzle.game_id == game.id, Puzzle.ply == m.ply)
        )
        if exists:
            continue
        # Sanity: solution line must be legal from the stored position.
        board = chess.Board(m.fen_before)
        line = []
        for tok in m.best_line.split():
            try:
                mv = chess.Move.from_uci(tok)
            except ValueError:
                break
            if mv not in board.legal_moves:
                break
            line.append(tok)
            board.push(mv)
            if len(line) >= _MAX_SOLUTION_PLIES:
                break
        if len(line) < _MIN_SOLUTION_PLIES:
            continue

        if m.punish_miss:
            kind = "punish"
        elif m.context == "defending":
            kind = "defend"
        else:
            kind = "find-best"

        uq = _uniqueness_cp(m)
        puzzle = Puzzle(
            game_id=game.id,
            ply=m.ply,
            source="own-game",
            fen=m.fen_before,
            solution=" ".join(line),
            pillar=m.pillar or "tactics",
            motif=m.motif,
            kind=kind,
            rating_est=_rating_estimate(int(m.cp_loss), m.phase, user_elo),
            uniqueness_cp=uq,
            # Carried onto the puzzle so a failed attempt can be shown what the
            # move they played actually ran into, without a second lookup.
            refutation=m.refutation_line,
            speed=game.speed,
        )
        session.add(puzzle)
        session.flush()
        ensure_card(session, puzzle)
        added += 1
    return added


def mine_all(account: str | None = None, user_elo: int | None = None, db_path=None) -> int:
    Session = session_factory(db_path)
    total = 0
    with Session() as session:
        q = select(Game).where(Game.analyzed.is_(True))
        if account:
            q = q.where(Game.account == account)
        for game in session.scalars(q):
            total += mine_game(session, game, user_elo)
        session.commit()
    return total
