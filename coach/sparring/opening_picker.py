"""Pick the starting position / opening for a sparring game.

Three modes, blended by default:
  * "weakness" - replay one of the user's worst-scoring openings from their real
    games, so they get reps in the lines that hurt them;
  * "breadth" - deliberately take the user OUT of their pet lines: a common
    opening they rarely play, so they don't over-narrow their repertoire;
  * "standard" - the initial position, opponent chooses.

The blend leans harder on "breadth" when the profile flags a narrow repertoire.
"""

from __future__ import annotations

import io
import random

import chess
import chess.pgn
from sqlalchemy import func, select

from coach.sparring.openings_pool import BALANCED_FENS
from coach.store.db import Game, Move, session_factory


def _worst_opening_games(account: str | None, colour: str | None, limit: int = 40):
    """Openings the user plays worst, with one representative game each.

    Aggregated in SQL. An earlier version pulled 400 ``Game`` rows and then
    touched ``g.moves`` on each one, which lazy-loads a separate query per game
    and tens of thousands of move rows - all inside the websocket handler, before
    the board could appear.

    The representative game is deliberately never a sparring game. A sparring
    game's PGN begins at whatever book position it was dealt, so replaying
    ``plies_into_opening`` plies of it lands that far past where it already
    started - and the next spar in the line starts from *there*. Left alone it
    compounds: the Four Knights went from move 7 to move 13 in two games, and
    the Polish from move 4 to move 10 to move 16. An opening that begins at move
    16 is not an opening. Their real games all start at move 1, so a start
    measured from one of those means what it says.
    """
    Session = session_factory()
    with Session() as session:
        # Average opening-phase cp-loss per (opening, colour), user moves only.
        # `phase` comes from the board's own move number and material, so it is
        # right even for a game that began halfway down a line; the ply index is
        # not, and the "or ply < 24" fallback it used to carry admitted 69 moves
        # across the whole library - every one of them a move the classifier had
        # already judged not to be an opening move.
        agg = (
            select(
                Game.opening.label("opening"),
                Game.color.label("color"),
                func.avg(Move.cp_loss).label("avg_loss"),
                func.count(Move.id).label("n"),
                func.max(Game.id).filter(Game.source != "sparring").label("game_id"),
            )
            .join(Move, Move.game_id == Game.id)
            .where(
                Game.analyzed.is_(True),
                Game.opening.isnot(None),
                Move.is_user_move.is_(True),
                Move.cp_loss.isnot(None),
                Move.phase == "opening",
            )
            .group_by(Game.opening, Game.color)
            .having(func.count(Move.id) >= 6)
            .order_by(func.avg(Move.cp_loss).desc())
            .limit(limit)
        )
        if account:
            agg = agg.where(Game.account == account)
        if colour:
            agg = agg.where(Game.color == colour)
        rows = session.execute(agg).all()
        # How a line has gone when they spar it is still evidence about that
        # line, so sparring games stay in the average. They just cannot be the
        # game that supplies the moves - and a line they have only ever sparred
        # has nothing to replay, so it drops out here.
        real = [r for r in rows if r.game_id is not None]
        if not real:
            return []
        pgns = dict(
            session.execute(
                select(Game.id, Game.pgn).where(Game.id.in_([r.game_id for r in real]))
            ).all()
        )
    return [
        (float(r.avg_loss), r.opening, r.color, pgns[r.game_id], r.game_id)
        for r in real
        if pgns.get(r.game_id)
    ]


# Below this, from the user's side, the position is not a rehearsal of a line
# they play badly - it is a lost game handed to them at move six. Uncomfortable
# is the point; resignable is not.
MIN_START_CP = -200
# Tried longest first. A line that only goes wrong on move six is still worth
# playing from move four, so trimming keeps the weak opening rather than
# abandoning it for an unrelated one.
_PLY_LADDER = (12, 8, 6, 4)


def _ply_ladder(deepest: int) -> list[int]:
    return [p for p in _PLY_LADDER if p <= deepest] or [deepest]


def _eval_after_plies(game_id: int, plies: int, colour: str) -> int | None:
    """Evaluation of the position ``plies`` moves in, from ``colour``'s side.

    ``Move.eval_before_cp`` is the analysed evaluation of the position *before*
    that move, stored White-positive - so the row at ply N describes exactly the
    position a game started N plies in would begin from. None when that ply was
    never analysed.
    """
    Session = session_factory()
    with Session() as session:
        cp = session.execute(
            select(Move.eval_before_cp).where(
                Move.game_id == game_id, Move.ply == plies
            )
        ).scalar_one_or_none()
    if cp is None:
        return None
    return cp if colour == "white" else -cp


def _opening_counts(account: str | None) -> dict[tuple[str, str], int]:
    Session = session_factory()
    with Session() as session:
        q = select(Game.color, Game.opening, func.count()).where(Game.opening.isnot(None))
        if account:
            q = q.where(Game.account == account)
        rows = session.execute(q.group_by(Game.color, Game.opening)).all()
    return {(c, o): n for c, o, n in rows}


def _repertoire_concentration(account: str | None) -> dict[str, float]:
    """Herfindahl index per colour (1.0 = only ever one opening)."""
    counts = _opening_counts(account)
    by_colour: dict[str, list[int]] = {"white": [], "black": []}
    for (colour, _), n in counts.items():
        by_colour.setdefault(colour, []).append(n)
    out = {}
    for colour, ns in by_colour.items():
        total = sum(ns)
        out[colour] = sum((n / total) ** 2 for n in ns) if total else 0.0
    return out


def _breadth_probability(concentration: float) -> float:
    """How often to deliberately leave the user's repertoire.

    ``concentration`` is the Herfindahl index (1.0 = always the same opening),
    so its reciprocal is roughly the effective number of openings played.
    """
    if concentration >= 0.5:
        return 0.5      # over-narrow: force variety
    effective = (1.0 / concentration) if concentration else 0.0
    if effective >= 12:
        return 0.0      # over-broad: adding another new line teaches nothing
    return 0.25


def _breadth_start(prefer_colour: str | None, rng: random.Random) -> dict:
    fen = rng.choice(BALANCED_FENS)
    colour = prefer_colour or rng.choice(["white", "black"])
    return {
        "fen": fen,
        "opening": None,
        "user_colour": colour,
        "source": "breadth",
        "note": "a line outside your usual repertoire",
    }


def pick_start(
    account: str | None,
    prefer_colour: str | None = None,
    plies_into_opening: int = 12,
    rng: random.Random | None = None,
    mode: str = "mix",
) -> dict:
    """Return {fen, opening, user_colour, source}. ``fen`` is the position the
    sparring game starts from."""
    rng = rng or random.Random()

    if mode == "breadth":
        return _breadth_start(prefer_colour, rng)

    if mode == "mix":
        # Breadth is only a virtue for a player who has over-narrowed. A player
        # spread across ~28 effective openings has the opposite problem: they
        # never see the same position twice and so never build any opening
        # knowledge at all. For them, pushing yet another unfamiliar line is
        # actively counterproductive - consolidate instead.
        conc = _repertoire_concentration(account)
        p_breadth = _breadth_probability(max(conc.values()) if conc else 0.0)
        if p_breadth and rng.random() < p_breadth:
            return _breadth_start(prefer_colour, rng)

    candidates = _worst_opening_games(account, prefer_colour)
    if not candidates:
        return _breadth_start(prefer_colour, rng) if mode != "weakness" else {
            "fen": chess.STARTING_FEN,
            "opening": None,
            "user_colour": prefer_colour or rng.choice(["white", "black"]),
            "source": "standard",
        }

    top = candidates[: min(8, len(candidates))]
    weights = [max(1.0, avg) for avg, *_ in top]
    avg_loss, opening, colour, pgn, game_id = rng.choices(top, weights=weights, k=1)[0]

    game = chess.pgn.read_game(io.StringIO(pgn))
    mainline = list(game.mainline()) if game else []

    # Replaying a bad game blind lands the user in whatever position they had
    # already blundered into - a rep in a position no one can save teaches
    # nothing about the opening. Back the start point out until the game is still
    # a game, and only give up on the line when even move four is lost.
    start_cp = None
    plies = None
    for candidate_plies in _ply_ladder(plies_into_opening):
        depth = min(candidate_plies, len(mainline))
        cp = _eval_after_plies(game_id, depth, colour)
        if cp is None or cp >= MIN_START_CP:
            plies, start_cp = depth, cp
            break
    if plies is None:
        return _breadth_start(prefer_colour, rng)

    board = game.board() if game else chess.Board()
    for node in mainline[:plies]:
        board.push(node.move)

    return {
        "fen": board.fen(),
        "opening": opening,
        "user_colour": colour,
        "source": "weak-opening",
        "avg_cp_loss": round(avg_loss, 1),
        "plies": plies,
        "start_cp": start_cp,
    }
