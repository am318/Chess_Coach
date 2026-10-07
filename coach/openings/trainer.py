"""Learning a named opening: study its lines, then drill them on the SRS.

Drills are ordinary puzzle rows with ``source="opening"`` and ``kind="line"``,
exactly as repertoire lines are, so the solver, the scheduler and the Today
tab's drill block all work on them unchanged. Two things differ, and both are
handled where the attempt is graded (``coach/server/app.py``): an opening drill
is about *this* line, so an engine-approved alternative is not accepted in its
place, and memorising a line is not evidence of tactical strength, so it does
not move the puzzle rating.

Rows exist only for openings the player has chosen to learn. Shipping all of
them as cards would put a few dozen strangers' lines into the puzzle queue of
someone who never asked for them.
"""

from __future__ import annotations

import io
from collections import Counter, defaultdict
from datetime import UTC, datetime
from functools import cache

import chess
import chess.pgn
from sqlalchemy import select
from sqlalchemy.orm import Session

from coach.openings.catalog import BY_KEY, CATALOG, Line, Opening
from coach.puzzles.srs import ensure_card
from coach.store.db import Game, Move, Puzzle, SrsCard, session_factory

SOURCE = "opening"
# A card whose stability has reached this many days is one the player knows:
# it has survived a fortnight-long gap at least once.
SOLID_DAYS = 14.0
# How deep into a stored game to look for the opening. The longest catalogue
# line is 24 plies; a transposition can arrive a little later than that.
_GAME_PLIES = 32


def _fen_key(fen: str) -> str:
    """Position identity ignoring the clocks, so transpositions merge."""
    return " ".join(fen.split()[:4])


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def external_id(opening: Opening, line: Line) -> str:
    return f"{SOURCE}:{opening.key}:{line.key}"


def _check_square(board: chess.Board) -> str | None:
    if not board.is_check():
        return None
    sq = board.king(board.turn)
    return chess.square_name(sq) if sq is not None else None


def _move_label(board: chess.Board) -> str:
    """'6.' before a White move, '6…' before a Black one."""
    return f"{board.fullmove_number}{'.' if board.turn == chess.WHITE else '…'}"


def _parse_movetext(text: str) -> list[tuple[chess.Move, str]]:
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None or game.errors:
        raise ValueError(f"unparseable movetext: {text.strip()[:60]}…"
                         + (f" ({game.errors[0]})" if game and game.errors else ""))
    out = []
    node = game
    while node.variations:
        node = node.variation(0)
        out.append((node.move, " ".join(node.comment.split())))
    return out


@cache
def line_plies(opening_key: str, line_key: str) -> tuple[dict, ...]:
    """Every ply of one line: the move, the position it reaches, and its note.

    The browser has no chess rules, so stepping through a line in study mode
    needs the positions handed to it rather than worked out.
    """
    opening = BY_KEY[opening_key]
    line = next(x for x in opening.lines if x.key == line_key)
    learner = chess.WHITE if opening.side == "white" else chess.BLACK
    board = chess.Board()
    plies = []
    for mv, note in _parse_movetext(line.pgn):
        label = _move_label(board)
        yours = board.turn == learner
        san = board.san(mv)
        board.push(mv)
        plies.append({
            "san": san,
            "uci": mv.uci(),
            "label": label,
            "fen": board.fen(),
            "check": _check_square(board),
            "note": note or None,
            "yours": yours,
        })
    return tuple(plies)


def line_uci(opening: Opening, line: Line) -> list[str]:
    return [p["uci"] for p in line_plies(opening.key, line.key)]


@cache
def _learner_positions(opening_key: str) -> dict[str, tuple[str, str]]:
    """Position key -> (the move this opening plays there, the full FEN)."""
    opening = BY_KEY[opening_key]
    out: dict[str, tuple[str, str]] = {}
    for line in opening.lines:
        board = chess.Board()
        for p in line_plies(opening.key, line.key):
            if p["yours"]:
                out.setdefault(_fen_key(board.fen()), (p["uci"], board.fen()))
            board.push(chess.Move.from_uci(p["uci"]))
    return out


def learner_moves(opening_key: str) -> dict[str, str]:
    """Position -> the move this opening plays there, for the learner's side."""
    return {k: uci for k, (uci, _) in _learner_positions(opening_key).items()}


def inconsistencies(opening: Opening) -> list[str]:
    """Positions where two lines of one opening disagree about the learner's move.

    A drill marks every move but the stored one wrong, so a disagreement would
    mark the player wrong whichever line they had learned. The catalogue test
    asserts this is empty.
    """
    seen: dict[str, tuple[str, str]] = {}
    bad = []
    for line in opening.lines:
        board = chess.Board()
        for p in line_plies(opening.key, line.key):
            if p["yours"]:
                key = _fen_key(board.fen())
                prev = seen.setdefault(key, (p["uci"], line.key))
                if prev[0] != p["uci"]:
                    bad.append(f"{line.key} plays {p['san']} where {prev[1]} plays {prev[0]}")
            board.push(chess.Move.from_uci(p["uci"]))
    return bad


@cache
def _defining_key(opening_key: str) -> str:
    board = chess.Board()
    for mv, _ in _parse_movetext(BY_KEY[opening_key].defining):
        board.push(mv)
    return _fen_key(board.fen())


def conflicts(opening: Opening, others: list[Opening]) -> list[dict]:
    """Openings being learned that answer one of this opening's positions differently.

    Learning the Italian and the Scotch together is perfectly reasonable - each
    drill names its line, so the drills don't contradict each other - but over
    the board the third move has to be one or the other, and that choice is
    better made knowingly.
    """
    if not others:
        return []
    # 1.e4 openings and 1.d4 openings are not in each other's way: choosing a
    # first move is what having a White repertoire means.
    start = _fen_key(chess.STARTING_FEN)
    mine = learner_moves(opening.key)
    out = []
    for other in others:
        if other.key == opening.key or other.side != opening.side:
            continue
        theirs = learner_moves(other.key)
        clash = next(
            (k for k in mine if k != start and k in theirs and theirs[k] != mine[k]),
            None,
        )
        if clash is None:
            continue
        # The full FEN, so the move number in the label is the real one.
        board = chess.Board(_learner_positions(opening.key)[clash][1])
        out.append({
            "key": other.key,
            "name": other.name,
            "label": _move_label(board),
            "mine": board.san(chess.Move.from_uci(mine[clash])),
            "theirs": board.san(chess.Move.from_uci(theirs[clash])),
        })
    return out


# ----------------------------------------------------------- storage

def _opening_rows(session: Session, opening: Opening) -> dict[str, Puzzle]:
    prefix = f"{SOURCE}:{opening.key}:"
    rows = session.scalars(
        select(Puzzle).where(Puzzle.source == SOURCE, Puzzle.external_id.startswith(prefix))
    )
    return {p.external_id: p for p in rows}


def learning_keys(session: Session) -> set[str]:
    ids = session.scalars(select(Puzzle.external_id).where(Puzzle.source == SOURCE))
    return {eid.split(":")[1] for eid in ids if eid}


def learn(opening_key: str, db_path=None) -> int:
    """Put an opening's lines into the drill queue. Idempotent; returns lines added.

    A line whose moves have changed in the catalogue since it was added is
    rewritten in place, keeping its card - the player still knows most of it.
    """
    opening = BY_KEY[opening_key]
    Session = session_factory(db_path)
    added = 0
    with Session() as session:
        existing = _opening_rows(session, opening)
        for line in opening.lines:
            eid = external_id(opening, line)
            solution = " ".join(line_uci(opening, line))
            name = f"{opening.name}: {line.name}"
            p = existing.get(eid)
            if p is not None:
                p.solution, p.name = solution, name
                continue
            p = Puzzle(
                source=SOURCE,
                external_id=eid,
                fen=chess.STARTING_FEN,
                solution=solution,
                pillar="opening",
                motif=opening.name,
                kind="line",
                name=name,
                # Every line starts from the initial position, so a Black line's
                # first stored move is White's and the solver plays it first.
                opponent_first=(opening.side == "black"),
            )
            session.add(p)
            session.flush()
            ensure_card(session, p)
            added += 1
        # Lines dropped from the catalogue stop being drilled.
        keep = {external_id(opening, line) for line in opening.lines}
        for eid, p in existing.items():
            if eid not in keep:
                session.delete(p)
        session.commit()
    return added


def forget(opening_key: str, db_path=None) -> int:
    """Take an opening out of the drill queue, cards and history with it."""
    opening = BY_KEY[opening_key]
    Session = session_factory(db_path)
    with Session() as session:
        rows = _opening_rows(session, opening)
        for p in rows.values():
            session.delete(p)
        session.commit()
    return len(rows)


def _card_state(card: SrsCard | None, now: datetime) -> dict | None:
    if card is None:
        return None
    if not card.reps:
        state = "new"
    elif (card.stability or 0) >= SOLID_DAYS:
        state = "solid"
    else:
        state = "learning"
    return {
        "state": state,
        "due": card.due_at <= now,
        "due_at": card.due_at.isoformat(),
        "reps": card.reps,
        "lapses": card.lapses,
        "stability_days": round(card.stability or 0.0, 1),
    }


def _progress(rows: dict[str, Puzzle], now: datetime) -> dict:
    states = Counter()
    due = 0
    for p in rows.values():
        st = _card_state(p.srs, now)
        if st is None:
            continue
        states[st["state"]] += 1
        due += st["due"]
    return {"new": states["new"], "learning": states["learning"],
            "solid": states["solid"], "due": due}


# ----------------------------------------------------- your own games

def your_games(session: Session, openings: list[Opening] | None = None) -> dict[str, dict]:
    """How each opening has gone in the player's own games, and where they left it.

    Sparring games are excluded: their openings were dealt by the trainer, so
    counting them would be the trainer reporting on its own choices.

    "Where they left it" is the useful half. For every game that reached the
    opening, the player's moves are walked against the book until the first one
    that differs, and the commonest of those departures are reported - "at move
    6 you play h3, the line plays O-O" is a thing to fix; a win rate is not.
    """
    openings = list(openings or CATALOG)
    by_side: dict[str, list[Opening]] = defaultdict(list)
    for o in openings:
        by_side[o.side].append(o)

    out: dict[str, dict] = {}
    for side, group in by_side.items():
        rows = session.execute(
            select(Move.game_id, Move.fen_before, Move.uci, Move.is_user_move, Game.result)
            .join(Game, Move.game_id == Game.id)
            .where(Game.color == side, Game.source != "sparring", Move.ply < _GAME_PLIES)
            .order_by(Move.game_id, Move.ply)
        ).all()
        games: dict[int, list] = defaultdict(list)
        results: dict[int, str] = {}
        for gid, fen, uci, is_user, result in rows:
            games[gid].append((fen, uci, is_user))
            results[gid] = result

        for o in group:
            target = _defining_key(o.key)
            book = learner_moves(o.key)
            tally = Counter()
            departures: Counter = Counter()
            fens: dict[str, str] = {}
            kept = 0
            for gid, moves in games.items():
                if not any(_fen_key(f) == target for f, _, _ in moves):
                    continue
                tally[results[gid]] += 1
                left = False
                for fen, uci, is_user in moves:
                    if not is_user:
                        continue
                    key = _fen_key(fen)
                    want = book.get(key)
                    if want is None:
                        continue
                    if uci[:4] != want[:4]:
                        departures[(key, uci, want)] += 1
                        fens[key] = fen
                        left = True
                        break
                if not left:
                    kept += 1
            n = sum(tally.values())
            top = []
            for (key, played, want), count in departures.most_common(3):
                board = chess.Board(fens[key])
                try:
                    top.append({
                        "label": _move_label(board),
                        "played": board.san(chess.Move.from_uci(played)),
                        "book": board.san(chess.Move.from_uci(want)),
                        "games": count,
                    })
                except (ValueError, AssertionError):
                    continue
            out[o.key] = {
                "games": n,
                "wins": tally["win"], "draws": tally["draw"], "losses": tally["loss"],
                "score_pct": round(100 * (tally["win"] + 0.5 * tally["draw"]) / n) if n else None,
                "stayed_in_book": kept,
                "departures": top,
            }
    return out


# ----------------------------------------------------------- read side

def _summary(o: Opening) -> dict:
    return {
        "key": o.key, "name": o.name, "side": o.side, "eco": o.eco,
        "summary": o.summary, "lines": len(o.lines),
    }


def catalog_overview(db_path=None) -> list[dict]:
    """Every opening with how far the player has got with it."""
    Session = session_factory(db_path)
    now = _now()
    with Session() as session:
        mine = your_games(session)
        out = []
        for o in CATALOG:
            rows = _opening_rows(session, o)
            out.append({
                **_summary(o),
                "learning": bool(rows),
                "progress": _progress(rows, now) if rows else None,
                "your_games": mine.get(o.key),
            })
    return out


def opening_detail(opening_key: str, db_path=None) -> dict:
    opening = BY_KEY[opening_key]
    Session = session_factory(db_path)
    now = _now()
    with Session() as session:
        rows = _opening_rows(session, opening)
        others = [BY_KEY[k] for k in learning_keys(session) if k in BY_KEY]
        lines = []
        for line in opening.lines:
            p = rows.get(external_id(opening, line))
            lines.append({
                "key": line.key,
                "name": line.name,
                "plies": list(line_plies(opening.key, line.key)),
                "card": _card_state(p.srs, now) if p else None,
                "puzzle_id": p.id if p else None,
            })
        return {
            **_summary(opening),
            "plans": list(opening.plans),
            "learning": bool(rows),
            "progress": _progress(rows, now) if rows else None,
            "lines": lines,
            "your_games": your_games(session, [opening]).get(opening.key),
            "conflicts": conflicts(opening, others),
        }


def next_drill(opening_key: str, line_key: str | None = None,
               db_path=None) -> tuple[Puzzle | None, bool, datetime | None]:
    """The line to drill next: ``(puzzle, practice, next_due)``.

    Reviews come before new lines, and new lines in catalogue order, so an
    opening is learned main line first. The day's new-card intake is *not*
    applied here - choosing an opening and pressing "drill" is the intake.

    With nothing due, the shakiest line is offered as practice (unscored), so
    the button always does something.
    """
    opening = BY_KEY[opening_key]
    Session = session_factory(db_path)
    now = _now()
    with Session() as session:
        rows = _opening_rows(session, opening)
        if not rows:
            return None, False, None
        if line_key:
            p = rows.get(f"{SOURCE}:{opening.key}:{line_key}")
            if p is None:
                return None, False, None
            card = ensure_card(session, p)
            session.commit()
            session.expunge(p)
            return p, card.due_at > now, card.due_at
        cards = [(p, ensure_card(session, p)) for p in rows.values()]
        session.commit()
        due = [(p, c) for p, c in cards if c.due_at <= now]
        later = min((c.due_at for _, c in cards if c.due_at > now), default=None)
        if due:
            due.sort(key=lambda pc: (pc[1].reps == 0, pc[1].due_at, pc[0].id))
            p = due[0][0]
            session.expunge(p)
            return p, False, later
        cards.sort(key=lambda pc: (pc[1].stability or 0.0, pc[1].due_at))
        p = cards[0][0]
        session.expunge(p)
        return p, True, later


def due_in_opening(opening_key: str, db_path=None) -> int:
    Session = session_factory(db_path)
    now = _now()
    with Session() as session:
        rows = _opening_rows(session, BY_KEY[opening_key])
        return sum(1 for p in rows.values() if p.srs is not None and p.srs.due_at <= now)
