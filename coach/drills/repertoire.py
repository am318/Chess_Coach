"""Build a repertoire trainer out of the games you have actually played.

Shipping a generic opening book would be the obvious move and the wrong one. A
book teaches someone else's repertoire; what a club player needs is for the first
ten moves of *their own* openings to become automatic, and for the handful of
places where their habitual move is genuinely bad to be corrected.

So the tree is mined from the user's own games:

* at a position **the user has reached**, the recommended move is their own
  most-played move when it holds up (average cp-loss under
  ``KEEP_OWN_MOVE_CP``), and the engine's best move when it doesn't - that way a
  drill reinforces what they already do except where it is actually costing them;
* at a position **the opponent moves in**, the branch taken is the reply they
  have most often faced, so the drill rehearses real problems rather than
  theoretical ones.

Lines are stored as ordinary puzzles with ``source="repertoire"``: the solver
already walks a line taking the user's moves at even indices and replying at odd
ones, which is exactly the shape of an opening drill.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import chess
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coach.puzzles.srs import ensure_card
from coach.store.db import Game, Move, Puzzle, session_factory

# How deep a drill line runs. Long enough to leave the book, short enough to
# rehearse in a few seconds.
MAX_PLIES = 12
MIN_PLIES = 6
# A position must have been reached this often to be worth drilling.
MIN_VISITS = 3
# Keep the user's own habitual move when it costs less than this on average.
KEEP_OWN_MOVE_CP = 50.0
# Cap so one dominant opening can't fill the whole queue.
MAX_LINES_PER_COLOUR = 20


@dataclass
class _PositionStats:
    visits: int = 0
    # uci -> [count, total_cp_loss]
    user_moves: dict[str, list[float]] = field(default_factory=lambda: defaultdict(lambda: [0, 0.0]))
    opponent_moves: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    engine_best: str | None = None
    openings: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def opening(self) -> str | None:
        if not self.openings:
            return None
        return max(self.openings.items(), key=lambda kv: kv[1])[0]


def _fen_key(fen: str) -> str:
    """Position identity ignoring clocks, so transpositions merge."""
    return " ".join(fen.split()[:4])


# The deepest real move number the tree can consult: `build_lines` walks
# MAX_PLIES plies from the initial position and names the line from the position
# after the last of them.
MAX_MOVE_NUMBER = MAX_PLIES // 2 + 1


def _within_tree(fen: str) -> bool:
    """Is this position shallow enough for the drill tree to ever reach it?

    ``Move.ply`` counts from the start of the *stored game*, which is only the
    start of the opening for a game that began at move one. Anything dealt from
    a book position - every sparring game - has its first dozen plies land
    wherever it was dealt, so a ply filter let a move-20 position in as opening
    play. The FEN's own move number is always honest, and it is the last field
    of the string, so this costs a split rather than a board.
    """
    try:
        return int(fen.rsplit(" ", 1)[-1]) <= MAX_MOVE_NUMBER
    except (ValueError, AttributeError):
        return False


def _collect(session: Session, colour: str, account: str | None) -> dict[str, _PositionStats]:
    q = (
        select(
            Move.fen_before, Move.uci, Move.is_user_move, Move.cp_loss,
            Move.best_move_uci, Game.opening,
        )
        .join(Game, Move.game_id == Game.id)
        .where(
            Game.analyzed.is_(True),
            Game.color == colour,
            # Sparring games are the training, not the evidence. Their opening
            # moves were dealt by the opening picker rather than chosen, and the
            # replies came from a deliberately weak bot - so counting them as
            # "your most-played move" or "the reply you most often face" would
            # have the trainer marking its own homework, and would drift the
            # tree towards whatever it happened to deal last.
            Game.source != "sparring",
            # An index-friendly superset: no position at move MAX_MOVE_NUMBER or
            # less can sit deeper than this in any game. `_within_tree` is the
            # bound that actually means what it says.
            Move.ply < MAX_PLIES + 2,
        )
    )
    if account:
        q = q.where(Game.account == account)

    stats: dict[str, _PositionStats] = defaultdict(_PositionStats)
    for fen, uci, is_user, cp_loss, best, opening in session.execute(q):
        if not _within_tree(fen):
            continue
        st = stats[_fen_key(fen)]
        st.visits += 1
        if opening:
            st.openings[opening] += 1
        if is_user:
            entry = st.user_moves[uci]
            entry[0] += 1
            entry[1] += float(cp_loss or 0)
            if best and not st.engine_best:
                st.engine_best = best
        else:
            st.opponent_moves[uci] += 1
    return stats


def _recommend(st: _PositionStats) -> tuple[str | None, str]:
    """The move to drill at this position, and why."""
    if not st.user_moves:
        return (st.engine_best, "engine") if st.engine_best else (None, "")
    # Most-played move first; ties broken by lower average cost.
    ranked = sorted(
        st.user_moves.items(),
        key=lambda kv: (-kv[1][0], kv[1][1] / max(1, kv[1][0])),
    )
    uci, (count, total) = ranked[0]
    avg = total / max(1, count)
    if avg <= KEEP_OWN_MOVE_CP:
        return uci, "yours"
    if st.engine_best:
        return st.engine_best, "corrected"
    return uci, "yours"


def build_lines(session: Session, colour: str, account: str | None = None) -> list[dict]:
    """Walk the tree breadth-first, emitting one line per popular branch."""
    stats = _collect(session, colour, account)
    user_is_white = colour == "white"
    lines: list[dict] = []

    def walk(board: chess.Board, moves: list[str], corrections: int) -> None:
        if len(lines) >= MAX_LINES_PER_COLOUR:
            return
        if len(moves) >= MAX_PLIES:
            _emit(board, moves, corrections)
            return
        st = stats.get(_fen_key(board.fen()))
        if st is None or st.visits < MIN_VISITS:
            _emit(board, moves, corrections)
            return

        our_turn = (board.turn == chess.WHITE) == user_is_white
        if our_turn:
            uci, why = _recommend(st)
            if not uci:
                return _emit(board, moves, corrections)
            try:
                mv = chess.Move.from_uci(uci)
            except ValueError:
                return _emit(board, moves, corrections)
            if mv not in board.legal_moves:
                return _emit(board, moves, corrections)
            board.push(mv)
            walk(board, [*moves, uci], corrections + (why == "corrected"))
            board.pop()
            return

        # Opponent to move: branch on the replies actually faced, commonest first.
        replies = sorted(st.opponent_moves.items(), key=lambda kv: -kv[1])
        branched = 0
        for uci, count in replies:
            if count < MIN_VISITS or branched >= 2:
                break
            try:
                mv = chess.Move.from_uci(uci)
            except ValueError:
                continue
            if mv not in board.legal_moves:
                continue
            board.push(mv)
            walk(board, [*moves, uci], corrections)
            board.pop()
            branched += 1
        if not branched:
            _emit(board, moves, corrections)

    def _emit(board: chess.Board, moves: list[str], corrections: int) -> None:
        # A line must end on the opponent's reply so the user's last move is answered,
        # and must be long enough to be worth drilling.
        trimmed = moves[:-1] if len(moves) % 2 == (0 if user_is_white else 1) else list(moves)
        if len(trimmed) < MIN_PLIES:
            return
        name = _name_for(trimmed, stats)
        sig = " ".join(trimmed)
        if any(existing["solution"] == sig for existing in lines):
            return
        lines.append({
            "fen": chess.STARTING_FEN,
            "solution": sig,
            "opening": name,
            "colour": colour,
            "corrections": corrections,
            "plies": len(trimmed),
        })

    def _name_for(moves: list[str], stats: dict[str, _PositionStats]) -> str:
        """Name from the *deepest* position that has one - the start position's
        label is whatever game happened to pass through it first, and is useless."""
        board = chess.Board()
        name = None
        for uci in moves:
            board.push(chess.Move.from_uci(uci))
            st = stats.get(_fen_key(board.fen()))
            if st and st.opening:
                name = st.opening
        return name or "your opening"

    walk(chess.Board(), [], 0)
    return lines


def mine_repertoire(account: str | None = None, db_path=None) -> int:
    """Refresh the stored repertoire drills. Returns how many were written."""
    Session = session_factory(db_path)
    added = 0
    with Session() as session:
        # Repertoire lines are derived, so rebuild rather than accumulate. Cards
        # for lines that survive are keyed on the line itself and kept.
        existing = {
            p.solution: p
            for p in session.scalars(select(Puzzle).where(Puzzle.source == "repertoire"))
        }
        seen: set[str] = set()
        for colour in ("white", "black"):
            for line in build_lines(session, colour, account):
                seen.add(line["solution"])
                if line["solution"] in existing:
                    continue
                puzzle = Puzzle(
                    source="repertoire",
                    fen=line["fen"],
                    solution=line["solution"],
                    pillar="opening",
                    motif=line["opening"],
                    kind="line",
                    name=f"{line['opening']} as {colour}",
                    # A black line starts from the initial position, so its first
                    # stored move is White's and the solver must play it first.
                    opponent_first=(colour == "black"),
                    goal=None,
                    rating_est=None,
                    speed=None,
                )
                session.add(puzzle)
                session.flush()
                ensure_card(session, puzzle)
                added += 1
        # Drop lines the user no longer plays.
        for sig, puzzle in existing.items():
            if sig not in seen:
                session.delete(puzzle)
        session.commit()
    return added


def repertoire_summary(account: str | None = None, db_path=None) -> dict:
    """What the drill set currently covers, for the dashboard."""
    Session = session_factory(db_path)
    with Session() as session:
        rows = session.execute(
            select(Puzzle.name, func.count())
            .where(Puzzle.source == "repertoire")
            .group_by(Puzzle.name)
        ).all()
    return {"lines": sum(n for _, n in rows), "by_opening": dict(rows)}
