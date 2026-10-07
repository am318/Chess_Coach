"""Is the sparring bot actually the strength it claims to be?

``Strength.for_elo`` is a table of educated guesses. This turns it into a
measurement, using two things the database already holds.

**What humans at a given rating actually play.** Every game carries both players'
ratings in its PGN headers, and the analysis pass now scores the opponent's moves
as well as the user's. Several hundred games is therefore a sample of real play
across a range of ratings, with an average centipawn loss per band - the number
the bot ought to reproduce.

**What the bot would play.** Rather than staging matches, the bot's expected
cp-loss is computed directly: score every root move of a sampled position once,
then ask what the sampling distribution at each rating point would have chosen.
That expectation is exact for the distribution, needs one engine call per
position no matter how many rating points are being checked, and - because it
calls ``SparringOpponent.move_distribution`` - measures the code that really
plays rather than a copy of it.

Note that both figures are *relative*. The human ACPL depends on the analysis
budget, and the bot's on the sampling budget; what matters is that they are
computed the same way and compared to each other, not to a published number.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import chess
from sqlalchemy import select

from coach.sparring.backend import RootScorer, make_backend
from coach.sparring.opponent import SparringOpponent, Strength
from coach.store.db import Game, Move, session_factory

BAND_WIDTH = 200
MIN_BAND_MOVES = 200
# Rating points to report on: the calibration points of `Strength.for_elo` plus
# the midpoints, so an error in the interpolation shows up too.
DEFAULT_ELOS = [400, 600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400]
# Positions sampled from real games. Each costs one MultiPV search.
DEFAULT_POSITIONS = 120
# Only sample positions with at least this many legal moves: the bot's strength
# lives in which move it picks, and a position with three replies cannot show it.
MIN_LEGAL = 8


def _band(elo: int) -> str:
    lo = (elo // BAND_WIDTH) * BAND_WIDTH
    return f"{lo}-{lo + BAND_WIDTH - 1}"


@dataclass
class BandStats:
    band: str
    lo: int
    moves: int
    acpl: float


def human_bands(account: str | None = None, db_path=None) -> list[BandStats]:
    """Average centipawn loss by rating band, over everyone in the database.

    Both sides of every game count: the user's own moves against the user's own
    rating, and each opponent's moves against theirs. The opponents are the
    valuable half - they span far more of the rating range than one player ever
    will.
    """
    Session = session_factory(db_path)
    with Session() as session:
        q = (
            select(Move.cp_loss, Move.side, Game.white_elo, Game.black_elo)
            .join(Game, Move.game_id == Game.id)
            .where(Move.cp_loss.isnot(None), Game.analyzed.is_(True),
                   Game.source.in_(("lichess", "chesscom")))
        )
        if account:
            q = q.where(Game.account == account)
        rows = session.execute(q).all()

    totals: dict[str, list[int]] = {}
    for cp_loss, side, white_elo, black_elo in rows:
        elo = white_elo if side == "white" else black_elo
        if not elo:
            continue
        totals.setdefault(_band(int(elo)), []).append(int(cp_loss))

    out = [
        BandStats(band=b, lo=int(b.split("-")[0]), moves=len(v),
                  acpl=round(sum(v) / len(v), 1))
        for b, v in totals.items() if len(v) >= MIN_BAND_MOVES
    ]
    out.sort(key=lambda b: b.lo)
    return out


def sample_positions(n: int = DEFAULT_POSITIONS, db_path=None,
                     seed: int = 7) -> list[chess.Board]:
    """Real positions from real games, drawn at random across the database."""
    Session = session_factory(db_path)
    with Session() as session:
        fens = list(session.scalars(
            select(Move.fen_before)
            .join(Game, Move.game_id == Game.id)
            .where(Game.analyzed.is_(True))
        ))
    rng = random.Random(seed)
    rng.shuffle(fens)
    boards: list[chess.Board] = []
    for fen in fens:
        try:
            board = chess.Board(fen)
        except ValueError:
            continue
        if board.is_game_over() or board.legal_moves.count() < MIN_LEGAL:
            continue
        boards.append(board)
        if len(boards) >= n:
            break
    return boards


def expected_cp_loss(boards: list[chess.Board], elo: int,
                     backend: RootScorer | None = None,
                     rankings: list[list[tuple[chess.Move, int]]] | None = None) -> float:
    """The cp-loss the bot would average at this rating, over these positions.

    Exact expectation under the sampling distribution rather than a simulation,
    so it does not need thousands of games to stop being noisy. The slip
    probability is folded in as a mixture of the two windows rather than being
    rolled for.
    """
    s = Strength.for_elo(elo)
    opp = SparringOpponent(target_elo=elo, weak_pillar=None, style="solid",
                           backend=backend or make_backend())
    total = 0.0
    counted = 0
    for i, board in enumerate(boards):
        ranked = rankings[i] if rankings else opp.backend.score_roots(board, s.nodes)
        if not ranked:
            continue
        best_cp = ranked[0][1]
        loss = 0.0
        for slip, share in ((False, 1.0 - s.slip_prob), (True, s.slip_prob)):
            if share <= 0:
                continue
            pool, weights = opp.move_distribution(board, ranked, s, slip=slip)
            wsum = sum(weights) or 1.0
            loss += share * sum(
                w / wsum * max(0, best_cp - cp) for (_, cp), w in zip(pool, weights)
            )
        total += loss
        counted += 1
    return round(total / counted, 1) if counted else 0.0


def calibrate(positions: int = DEFAULT_POSITIONS, elos: list[int] | None = None,
              account: str | None = None, db_path=None,
              budget: int | None = None) -> dict:
    """Human ACPL per band against the bot's expected ACPL at each rating point.

    The engine work is done once: the root ranking of each sampled position is
    reused for every rating point, since only the sampling on top of it changes.
    """
    elos = elos or DEFAULT_ELOS
    humans = human_bands(account, db_path)
    boards = sample_positions(positions, db_path)
    if not boards:
        return {"error": "no analysed positions to sample", "bands": [], "bot": []}

    backend = make_backend()
    try:
        # One ranking budget for all rating points. Using each point's own node
        # count would confound "the bot searches less" with "the bot samples
        # more loosely"; the design intent is that only the second varies.
        nodes = budget or Strength.for_elo(1600).nodes
        rankings = [backend.score_roots(b, nodes) for b in boards]
        bot = [{"elo": e, "acpl": expected_cp_loss(boards, e, backend, rankings)}
               for e in elos]
    finally:
        backend.close()

    by_band = {b.band: b for b in humans}

    rows = []
    for entry in bot:
        band = _band(entry["elo"])
        human = by_band.get(band)
        row = {
            "elo": entry["elo"],
            "band": band,
            "bot_acpl": entry["acpl"],
            "human_acpl": human.acpl if human else None,
            "human_moves": human.moves if human else 0,
        }
        if human:
            # Bot loses less than the humans in this band => it is too strong for
            # the label it is wearing.
            row["delta"] = round(entry["acpl"] - human.acpl, 1)
            row["verdict"] = (
                "too strong" if row["delta"] < -12
                else "too weak" if row["delta"] > 12
                else "ok"
            )
        rows.append(row)

    return {
        "positions": len(boards),
        "nodes": nodes,
        "bands": [b.__dict__ for b in humans],
        "rows": rows,
        "note": (
            "bot and human ACPL are both measured with this database's analysis "
            "settings - compare them to each other, not to published figures"
        ),
    }


def render_text(result: dict) -> str:
    if result.get("error"):
        return result["error"]
    lines = [
        (f"Sparring calibration over {result['positions']} sampled positions "
         f"({result['nodes']} nodes each)"),
        "",
        f"{'elo':>5}  {'band':<10} {'humans':>14}  {'bot':>7}  {'delta':>6}  verdict",
    ]
    for r in result["rows"]:
        human = (f"{r['human_acpl']:.0f} (n={r['human_moves']})"
                 if r["human_acpl"] is not None else "-")
        delta = f"{r['delta']:+.0f}" if "delta" in r else "-"
        lines.append(
            f"{r['elo']:>5}  {r['band']:<10} {human:>14}  {r['bot_acpl']:>7.0f}  "
            f"{delta:>6}  {r.get('verdict', '')}"
        )
    lines += [
        "",
        "A negative delta means the bot loses less than humans of that rating do,",
        "so it is playing above its label: raise `temperature` / `window_cp` for",
        "that point in Strength.for_elo. A positive delta means the opposite.",
        result["note"],
    ]
    return "\n".join(lines)
