"""Tactical motif detection: what the player missed, and what they walked into.

The old detector knew six motifs. Anything outside them - a skewer, a
deflection, a zwischenzug - fell through to "no motif", which made the tactics
pillar look like one undifferentiated lump and left nothing for the recurrence
chart or the puzzle queue to target. This module names about thirty, using the
same vocabulary as the Lichess puzzle themes (see ``LICHESS_THEME``) so imported
library puzzles and puzzles mined from the player's own games can be matched
against each other.

Two kinds of tag come out:

* **walked-into** tags describe the move that was played: it hung a piece, it
  moved a pinned piece, it allowed a mate.
* **missed** tags describe the engine's best line: what the player could have
  had. Many of them are only visible over several plies - a deflection is a
  check, the defender moving away, *then* the capture it was guarding against -
  so the detector reads the whole principal variation when it is given one, and
  uses it to confirm rather than guess. Without a line it falls back to what the
  first move alone shows, and simply does not claim the multi-move themes.

Every rule here is a heuristic over the board, not a solver. The thresholds err
towards silence: a missing tag costs a little resolution, a wrong one sends the
player to drill a pattern they do not have.
"""

from __future__ import annotations

from collections.abc import Sequence

import chess

VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5,
         chess.QUEEN: 9, chess.KING: 100}

# At most this many tags per move. The column is a short comma list and, more to
# the point, a move tagged with eight themes says nothing about any of them.
MAX_TAGS = 6

# Our tag -> the Lichess puzzle-theme token for the same idea (None: no direct
# equivalent). Kept kebab-case internally so tags already stored keep meaning.
LICHESS_THEME: dict[str, str | None] = {
    "hangs-piece": "hangingPiece",
    "wins-exchange": None,
    "moved-pinned-piece": "pin",
    "allowed-mate": None,
    "back-rank-weak": "backRankMate",
    "missed-punish": None,
    "free-piece": "hangingPiece",
    "fork": "fork",
    "pawn-fork": "fork",
    "pin": "pin",
    "skewer": "skewer",
    "discovered-attack": "discoveredAttack",
    "discovered-check": "discoveredAttack",
    "double-check": "doubleCheck",
    "capturing-defender": "capturingDefender",
    "deflection": "deflection",
    "attraction": "attraction",
    "intermezzo": "intermezzo",
    "trapped-piece": "trappedPiece",
    "clearance": "clearance",
    "interference": "interference",
    "sacrifice": "sacrifice",
    "x-ray": "xRayAttack",
    "mate-in-1": "mateIn1",
    "mate-in-2": "mateIn2",
    "mate-in-3": "mateIn3",
    "mate-long": "mateIn4",
    "back-rank-mate": "backRankMate",
    "smothered-mate": "smotheredMate",
    "promotion": "promotion",
    "under-promotion": "underPromotion",
    "advanced-pawn": "advancedPawn",
    "quiet-move": "quietMove",
    "defensive-move": "defensiveMove",
}

# Lichess token -> our tag. Several of ours share a token; the reverse picks the
# general one.
FROM_LICHESS: dict[str, str] = {
    "hangingPiece": "free-piece", "fork": "fork", "pin": "pin", "skewer": "skewer",
    "discoveredAttack": "discovered-attack", "doubleCheck": "double-check",
    "capturingDefender": "capturing-defender", "deflection": "deflection",
    "attraction": "attraction", "intermezzo": "intermezzo",
    "trappedPiece": "trapped-piece", "clearance": "clearance",
    "interference": "interference", "sacrifice": "sacrifice", "xRayAttack": "x-ray",
    "mateIn1": "mate-in-1", "mateIn2": "mate-in-2", "mateIn3": "mate-in-3",
    "mateIn4": "mate-long", "mateIn5": "mate-long", "backRankMate": "back-rank-mate",
    "smotheredMate": "smothered-mate", "promotion": "promotion",
    "underPromotion": "under-promotion", "advancedPawn": "advanced-pawn",
    "quietMove": "quiet-move", "defensiveMove": "defensive-move",
}

LABEL = {
    "hangs-piece": "leaves a piece hanging",
    "wins-exchange": "loses the exchange",
    "moved-pinned-piece": "moves a pinned piece",
    "allowed-mate": "allows a forced mate",
    "back-rank-weak": "back rank is weak",
    "missed-punish": "didn't punish the opponent's mistake",
    "free-piece": "a free piece was there to take",
    "fork": "forks two pieces",
    "pawn-fork": "pawn fork",
    "pin": "pin",
    "skewer": "skewer",
    "discovered-attack": "discovered attack",
    "discovered-check": "discovered check",
    "double-check": "double check",
    "capturing-defender": "removes the defender",
    "deflection": "deflection",
    "attraction": "attraction (lures a piece onto a bad square)",
    "intermezzo": "in-between move (zwischenzug)",
    "trapped-piece": "traps a piece",
    "clearance": "clearance (vacates a square or line)",
    "interference": "interference (cuts a defender's line)",
    "sacrifice": "sacrifice",
    "x-ray": "x-ray attack",
    "mate-in-1": "mate in 1",
    "mate-in-2": "mate in 2",
    "mate-in-3": "mate in 3",
    "mate-long": "a longer forced mate",
    "back-rank-mate": "back-rank mate",
    "smothered-mate": "smothered mate",
    "promotion": "promotion",
    "under-promotion": "under-promotion",
    "advanced-pawn": "advanced passed pawn",
    "quiet-move": "a quiet move that wins",
    "defensive-move": "a defensive move was needed",
}

ALL_MOTIFS = tuple(LABEL)

_ROOK_DIRS = ((0, 1), (0, -1), (1, 0), (-1, 0))
_BISHOP_DIRS = ((1, 1), (1, -1), (-1, 1), (-1, -1))


def label(tag: str) -> str:
    return LABEL.get(tag, tag)


def from_lichess_themes(themes: str) -> list[str]:
    """Lichess's space-separated theme string as our tags, in their order."""
    out = [FROM_LICHESS[t] for t in (themes or "").split() if t in FROM_LICHESS]
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Small board helpers.

def _val(board: chess.Board, sq: chess.Square) -> int:
    pt = board.piece_type_at(sq)
    return VALUE.get(pt, 0) if pt else 0


def _defended(board: chess.Board, sq: chess.Square) -> bool:
    color = board.color_at(sq)
    return color is not None and bool(board.attackers(color, sq))


def _wins_material(board: chess.Board, mv: chess.Move) -> bool:
    """A capture that takes more than it can lose: a free piece, or a cheap
    piece taking a dear one."""
    if not board.is_capture(mv):
        return False
    if board.is_en_passant(mv):
        return False
    victim = _val(board, mv.to_square)
    attacker = _val(board, mv.from_square)
    if victim < VALUE[chess.KNIGHT] and attacker > victim:
        return False
    return victim > attacker or not _defended(board, mv.to_square)


def _ray(board: chess.Board, sq: chess.Square, df: int, dr: int) -> list[chess.Square]:
    """Occupied squares along one direction from ``sq``, nearest first (max 2)."""
    f, r = chess.square_file(sq), chess.square_rank(sq)
    hits: list[chess.Square] = []
    while True:
        f, r = f + df, r + dr
        if not (0 <= f < 8 and 0 <= r < 8):
            return hits
        s = chess.square(f, r)
        if board.piece_at(s):
            hits.append(s)
            if len(hits) == 2:
                return hits


def _dirs(pt: int) -> tuple:
    if pt == chess.ROOK:
        return _ROOK_DIRS
    if pt == chess.BISHOP:
        return _BISHOP_DIRS
    if pt == chess.QUEEN:
        return _ROOK_DIRS + _BISHOP_DIRS
    return ()


def _line_boards(board: chess.Board, line: Sequence[chess.Move]) -> list[chess.Board]:
    """Board before each move of ``line`` and after the last legal one."""
    out = [board.copy(stack=False)]
    b = board.copy(stack=False)
    for mv in line:
        if mv not in b.legal_moves:
            break
        b.push(mv)
        out.append(b.copy(stack=False))
    return out


def _legal_prefix(board: chess.Board, line: Sequence[chess.Move] | None) -> list[chess.Move]:
    out: list[chess.Move] = []
    b = board.copy(stack=False)
    for mv in line or ():
        if mv not in b.legal_moves:
            break
        out.append(mv)
        b.push(mv)
    return out


# ---------------------------------------------------------------------------
# Walked into: properties of the move that was played.

def _walked_into(board: chess.Board, played: chess.Move,
                 refutation: Sequence[chess.Move] | None) -> list[str]:
    tags: list[str] = []
    stm = board.turn
    after = board.copy(stack=False)
    after.push(played)

    for mv in after.legal_moves:
        if not after.is_capture(mv):
            continue
        victim = after.piece_type_at(mv.to_square)
        if victim is None:
            continue
        attackers = after.attackers(after.turn, mv.to_square)
        defenders = after.attackers(not after.turn, mv.to_square)
        if victim >= chess.KNIGHT and len(defenders) == 0:
            tags.append("hangs-piece")
            break
        if victim >= chess.ROOK and len(attackers) > len(defenders):
            tags.append("wins-exchange")
            break

    if board.is_pinned(stm, played.from_square):
        tags.append("moved-pinned-piece")

    ref = _legal_prefix(after, refutation)
    if ref:
        boards = _line_boards(after, ref)
        last = boards[-1]
        if last.is_checkmate() and last.turn == stm:
            tags.append("allowed-mate")
    return tags


def _back_rank_weak(board: chess.Board) -> bool:
    stm = board.turn
    king_sq = board.king(stm)
    if king_sq is None:
        return False
    back = 0 if stm == chess.WHITE else 7
    if chess.square_rank(king_sq) != back:
        return False
    fwd = 8 if stm == chess.WHITE else -8
    return all(
        (0 <= king_sq + fwd + d < 64)
        and board.piece_type_at(king_sq + fwd + d) == chess.PAWN
        and board.color_at(king_sq + fwd + d) == stm
        for d in (-1, 0, 1)
        if 0 <= chess.square_file(king_sq) + d <= 7
    )


# ---------------------------------------------------------------------------
# Missed: properties of the engine's best line.

def _survives(after: chess.Board, sq: chess.Square, us: bool) -> bool:
    """Is the piece that just moved to ``sq`` safe from being taken at a profit?

    A "fork" by a queen the king simply captures is not a fork, it is a queen
    sacrifice - and a sacrifice is tagged as one, separately.
    """
    hitters = after.attackers(not us, sq)
    if not hitters:
        return True
    if not after.attackers(us, sq):
        return False
    return min(_val(after, h) for h in hitters) >= _val(after, sq)


def _fork(after: chess.Board, sq: chess.Square, us: bool) -> tuple[bool, bool]:
    """(is_fork, by_pawn) for the piece standing on ``sq`` after it moved."""
    pt = after.piece_type_at(sq)
    if not pt:
        return False, False
    mine = VALUE[pt]
    if not _survives(after, sq, us):
        return False, False
    targets = 0
    for tsq in after.attacks(sq):
        if after.color_at(tsq) == us:
            continue
        tp = after.piece_type_at(tsq)
        if tp is None or tp == chess.PAWN:
            continue
        if tp == chess.KING or VALUE[tp] > mine or not _defended(after, tsq):
            targets += 1
    return targets >= 2, pt == chess.PAWN and targets >= 2


def _pin_skewer_xray(after: chess.Board, sq: chess.Square, us: bool) -> set[str]:
    """Lines from the slider that just moved to ``sq``."""
    out: set[str] = set()
    pt = after.piece_type_at(sq)
    if not pt or not _survives(after, sq, us):
        return out
    mine = VALUE[pt]
    for df, dr in _dirs(pt):
        hits = _ray(after, sq, df, dr)
        if len(hits) < 1:
            continue
        front = hits[0]
        if after.color_at(front) == us:
            # Our own piece in front, an enemy piece behind: x-ray through it.
            # A battery: our own slider of the same kind in front, aimed through.
            if len(hits) == 2 and after.color_at(hits[1]) != us:
                bt = after.piece_type_at(hits[1])
                if (bt and bt != chess.PAWN
                        and (VALUE[bt] > mine or not _defended(after, hits[1]))
                        and after.piece_type_at(front) in (pt, chess.QUEEN)):
                    out.add("x-ray")
            continue
        if len(hits) < 2 or after.color_at(hits[1]) == us:
            continue
        back = hits[1]
        fv, bv = _val(after, front), _val(after, back)
        ft = after.piece_type_at(front)
        if ft == chess.PAWN:
            continue
        if ft != chess.KING and bv > fv:
            # The front piece cannot move without exposing something dearer.
            out.add("pin")
        elif ((ft == chess.KING or fv > bv) and bv >= VALUE[chess.KNIGHT]
              and (bv > mine or not _defended(after, back))):
            out.add("skewer")
    return out


def _discovery(board: chess.Board, after: chess.Board, mv: chess.Move, us: bool) -> set[str]:
    out: set[str] = set()
    if after.is_check() and len(after.checkers()) >= 2:
        out.add("double-check")
    for s in board.pieces(chess.BISHOP, us) | board.pieces(chess.ROOK, us) | board.pieces(chess.QUEEN, us):
        if s == mv.from_square:
            continue
        if mv.from_square not in chess.SquareSet(chess.between(s, mv.to_square)) and \
                mv.from_square not in board.attacks(s):
            continue
        newly = after.attacks(s) & ~board.attacks(s)
        for t in newly:
            if after.color_at(t) == us:
                continue
            tp = after.piece_type_at(t)
            if tp == chess.KING:
                out.add("discovered-check")
            elif tp and tp != chess.PAWN and (VALUE[tp] > _val(after, s) or not _defended(after, t)):
                out.add("discovered-attack")
    return out


def _trapped(after: chess.Board, us: bool) -> bool:
    """An enemy piece is attacked and every square it can reach is covered."""
    if after.is_check() or after.turn == us:
        return False
    them = not us
    for sq in chess.SquareSet(after.occupied_co[them]):
        pt = after.piece_type_at(sq)
        if pt in (None, chess.PAWN, chess.KING):
            continue
        v = VALUE[pt]
        attackers = after.attackers(us, sq)
        if not attackers:
            continue
        cheapest = min(_val(after, a) for a in attackers)
        if not (cheapest < v or not _defended(after, sq)):
            continue
        exits = [m for m in after.legal_moves if m.from_square == sq]
        if not exits:
            continue
        safe = False
        for m in exits:
            if after.is_capture(m) and _val(after, m.to_square) >= v:
                safe = True
                break
            b = after.copy(stack=False)
            b.push(m)
            hitters = b.attackers(us, m.to_square)
            if not hitters:
                safe = True
                break
            if min(_val(b, a) for a in hitters) >= v and b.attackers(them, m.to_square):
                safe = True
                break
        if not safe:
            return True
    return False


def _mate_tags(boards: list[chess.Board], line: list[chess.Move], us: bool) -> list[str]:
    last = boards[-1]
    if not (last.is_checkmate() and last.turn != us):
        return []
    n = (len(boards) - 1 + 1) // 2
    tags = [f"mate-in-{n}" if n <= 3 else "mate-long"]
    k = last.king(not us)
    if k is not None:
        checkers = last.checkers()
        back = 7 if us == chess.WHITE else 0
        if chess.square_rank(k) == back and any(
            last.piece_type_at(c) in (chess.ROOK, chess.QUEEN) and chess.square_rank(c) == back
            for c in checkers
        ):
            tags.append("back-rank-mate")
        if len(checkers) == 1 and last.piece_type_at(next(iter(checkers))) == chess.KNIGHT:
            around = chess.SquareSet(chess.BB_KING_ATTACKS[k])
            if all(last.color_at(s) == (not us) for s in around):
                tags.append("smothered-mate")
    return tags


def _sacrifice(board: chess.Board, after: chess.Board, mv: chess.Move, us: bool,
               line: list[chess.Move]) -> bool:
    moved = _val(after, mv.to_square)
    gained = _val(board, mv.to_square) if board.is_capture(mv) else 0
    if moved - gained < 2 or moved >= VALUE[chess.KING]:
        return False
    if not after.attackers(not us, mv.to_square):
        return False
    if len(line) >= 2:
        return line[1].to_square == mv.to_square
    return not after.attackers(us, mv.to_square)


def _threat_against_us(board: chess.Board) -> list[chess.Move]:
    """Captures the opponent would have if it were their move now."""
    if board.is_check():
        return []
    nb = board.copy(stack=False)
    nb.push(chess.Move.null())
    return [m for m in nb.legal_moves if _wins_material(nb, m)]


def _missed(board: chess.Board, best: chess.Move,
            best_line: Sequence[chess.Move] | None) -> list[str]:
    us = board.turn
    line = _legal_prefix(board, best_line) if best_line else []
    if not line or line[0] != best:
        line = [best] if best in board.legal_moves else []
    if not line:
        return []
    boards = _line_boards(board, line)
    after = boards[1]
    tags: list[str] = []

    tags += _mate_tags(boards, line, us)

    if _wins_material(board, best) and not _defended(board, best.to_square):
        tags.append("free-piece")

    fork, pawn_fork = _fork(after, best.to_square, us)
    if fork:
        tags.append("fork")
    if pawn_fork:
        tags.append("pawn-fork")

    tags += sorted(_pin_skewer_xray(after, best.to_square, us))
    tags += sorted(_discovery(board, after, best, us))

    if best.promotion:
        tags.append("promotion" if best.promotion == chess.QUEEN else "under-promotion")
    elif board.piece_type_at(best.from_square) == chess.PAWN:
        rel = chess.square_rank(best.to_square) if us == chess.WHITE else 7 - chess.square_rank(best.to_square)
        if rel >= 5:
            tags.append("advanced-pawn")

    if _sacrifice(board, after, best, us, line):
        tags.append("sacrifice")

    if _trapped(after, us):
        tags.append("trapped-piece")

    # The multi-move themes need the reply and our follow-up.
    if len(boards) >= 4:
        b1, b2 = boards[1], boards[2]
        reply, follow = line[1], line[2]
        follow_hits = b2.is_capture(follow) or boards[3].is_checkmate() or boards[3].is_check()
        target = follow.to_square

        # Removing the defender: we capture D, which was the only guard of T.
        if board.is_capture(best) and b2.is_capture(follow):
            them = not us
            guards = board.attackers(them, target)
            if best.to_square in guards and len(guards) == 1 and reply.to_square != target:
                tags.append("capturing-defender")

        # Deflection: a forcing first move makes defender D leave its post.
        forcing = after.is_check() or board.is_capture(best)
        if forcing and follow_hits and reply.from_square != target:
            mover = reply.from_square
            if b1.piece_at(mover) and target in b1.attacks(mover) and target not in b2.attacks(reply.to_square):
                if board.is_capture(best) and best.to_square in board.attackers(not us, target):
                    pass  # that is removing the defender, already handled
                else:
                    tags.append("deflection")

        # Attraction: they take the bait, and the piece that took it is hit.
        if reply.to_square == best.to_square and b1.is_capture(reply) and _sacrifice(board, after, best, us, line):
            b3 = boards[3]
            lured = reply.to_square
            if b3.is_check() and b3.king(not us) == lured or lured in b3.attacks(follow.to_square):
                tags.append("attraction")

        # Zwischenzug: a recapture was on offer, we played something forcing
        # first and recaptured afterwards.
        if not board.is_capture(best) and reply.to_square != best.to_square and (
                after.is_check() or _threatens(after, best.to_square, us)):
            recaptures = {m.to_square for m in board.legal_moves if board.is_capture(m)}
            if b2.is_capture(follow) and target in recaptures:
                tags.append("intermezzo")

        # Clearance: the first move vacates a square or line another piece uses.
        if follow.from_square != best.to_square and follow_hits:
            path = chess.SquareSet(chess.between(follow.from_square, follow.to_square))
            if best.from_square == follow.to_square or best.from_square in path:
                tags.append("clearance")

        # Interference: the first move lands between an enemy guard and what it
        # guarded, and the follow-up takes the now-unguarded thing.
        if b2.is_capture(follow):
            for g in board.attackers(not us, target):
                if best.to_square in chess.SquareSet(chess.between(g, target)) and \
                        target not in after.attacks(g):
                    tags.append("interference")
                    break

    if not (board.is_capture(best) or after.is_check() or best.promotion):
        threats = _threat_against_us(board)
        if threats and not _threat_against_us_after(after, threats):
            tags.append("defensive-move")
        elif not any(t in tags for t in ("fork", "pin", "skewer", "discovered-attack",
                                         "discovered-check", "trapped-piece", "advanced-pawn",
                                         "mate-in-1", "mate-in-2", "mate-in-3", "mate-long")):
            tags.append("quiet-move")
    return tags


def _threatens(after: chess.Board, sq: chess.Square, us: bool) -> bool:
    pt = after.piece_type_at(sq)
    if not pt:
        return False
    for t in after.attacks(sq):
        tp = after.piece_type_at(t)
        if after.color_at(t) != us and tp and tp != chess.PAWN and (
            tp == chess.KING or VALUE[tp] > VALUE[pt] or not _defended(after, t)
        ):
            return True
    return False


def _threat_against_us_after(after: chess.Board, threats: list[chess.Move]) -> bool:
    """Does any of the threats still work once our move is on the board?"""
    for m in threats:
        if m in after.legal_moves and _wins_material(after, m):
            return True
    return False


# ---------------------------------------------------------------------------

def detect_motifs(
    board: chess.Board,
    played: chess.Move,
    best: chess.Move | None,
    best_line: Sequence[chess.Move] | None = None,
    refutation: Sequence[chess.Move] | None = None,
) -> list[str]:
    """Motif tags for a position where the player chose ``played`` over ``best``.

    ``best_line`` is the engine's PV from ``board`` (starting with ``best``) and
    ``refutation`` the PV from the position after ``played``. Both are optional.
    """
    tags = _walked_into(board, played, refutation)
    if best is not None and best != played:
        tags += _missed(board, best, best_line)
    elif best is not None:
        # The move under discussion is the best move itself (candidate lists):
        # describe what it does.
        tags += [t for t in _missed(board, best, best_line)
                 if t not in ("quiet-move", "defensive-move")]
    if _back_rank_weak(board):
        tags.append("back-rank-weak")
    return list(dict.fromkeys(tags))[:MAX_TAGS]


def parse_line(uci: str | None) -> list[chess.Move]:
    out: list[chess.Move] = []
    for tok in (uci or "").split():
        try:
            out.append(chess.Move.from_uci(tok))
        except ValueError:
            break
    return out
