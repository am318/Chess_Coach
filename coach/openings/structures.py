"""Pawn structures: the plans that come with the pawns.

The opening trainer teaches move orders; the repertoire drill rehearses what
you already play. Neither says what to *do* once the moves run out - and at
club level that is where opening knowledge actually pays: a player who knows
that the Carlsbad structure means a minority attack, or that an isolated
queen's pawn wants pieces rather than trades, has a plan on move fifteen that
no memorised line would have given them.

So fifteen common structures, hand-written: how to recognise them, what each
side is trying to do, the pawn breaks, where the pieces go. Detection reads
only the pawns, so it works on any position from your games, and every rule
is written once with the "owner" (the side the structure is named for, or the
side with the feature) as White and mirrored for Black.

Three uses:
* lessons, with a quiz on positions from your own games that have the
  structure ("you are the side against the IQP - which plan?");
* a profile: how you play in each structure, as owner and against it;
* a tag on review moments, so a mistake in a Carlsbad comes with its plan.
"""

from __future__ import annotations

import json
import random
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

import chess
from sqlalchemy import select
from sqlalchemy.orm import Session

from coach.store.db import Game, Move, get_setting, session_factory, set_setting

Pawns = set[tuple[int, int]]       # (file 0-7, rank 1-8), owner's point of view


@dataclass(frozen=True)
class Structure:
    key: str
    name: str
    recognise: str
    owner_label: str       # how to name the owning side, e.g. "the IQP side"
    against_label: str
    owner_plan: str
    against_plan: str
    breaks: str
    pieces: str
    rule: Callable[[Pawns, Pawns], bool]
    symmetric: bool = False


def _sq(name: str) -> tuple[int, int]:
    return ord(name[0]) - 97, int(name[1])


def _has(p: Pawns, *names: str) -> bool:
    return all(_sq(n) in p for n in names)


def _file(p: Pawns, f: str) -> list[int]:
    fi = ord(f) - 97
    return [r for (ff, r) in p if ff == fi]


def _none(p: Pawns, *files: str) -> bool:
    return all(not _file(p, f) for f in files)


def _qs_count(p: Pawns) -> int:
    return sum(1 for f, _ in p if f <= 2)


STRUCTURES: tuple[Structure, ...] = (
    Structure(
        "iqp", "Isolated queen's pawn",
        "A d-pawn with no pawns of its own colour on the c- or e-files.",
        "the side with the isolani", "the side playing against it",
        "Play with your pieces while they are on the board: the pawn gives you space and "
        "outposts on e5 and c5. Knight to e5, a rook lift, a kingside attack - and the d5 "
        "break when it opens lines with tempo. Avoid trades; each one weakens the pawn.",
        "Blockade the square in front of the pawn with a knight, then trade pieces. Every "
        "exchange takes away the pawn's dynamism and leaves a weakness; in the endgame it "
        "usually falls.",
        "The owner's break is d4-d5. The defender aims to keep d5 under control.",
        "Owner: knights on e5 and c3, bishops aimed at the king, rooks on d1 and e1. "
        "Defender: a knight on d5, rooks on the d-file against the pawn.",
        # d4/d5 from the owner's side only: a lone pawn still on d3 (d6) has none of
        # the space or outposts the plans below are about.
        lambda o, x: bool([r for r in _file(o, "d") if 4 <= r <= 5]) and _none(o, "c", "e"),
    ),
    Structure(
        "hanging-pawns", "Hanging pawns",
        "Two pawns side by side on c4 and d4 (c5/d5), with no pawns of their colour on the "
        "b- or e-files.",
        "the side with the hanging pawns", "the side playing against them",
        "Keep them abreast, where they control the centre. Advance one only to open lines "
        "with tempo, usually d5 when your pieces are ready; then they stop being a target.",
        "Pressure them with rooks on the c- and d-files and pieces until one has to advance, "
        "then blockade the square in front of the pawn left behind.",
        "Owner: d4-d5 or c4-c5 at the right moment. Defender: e3-e4 or b3-b4 to undermine.",
        "Owner: rooks behind the pawns, minor pieces supporting d5. Defender: knights that "
        "blockade, a bishop on the long diagonal.",
        lambda o, x: _has(o, "c4", "d4") and _none(o, "b", "e") and _none(x, "c", "d"),
    ),
    Structure(
        "carlsbad", "Carlsbad structure",
        "White pawn on d4 with no c-pawn; Black pawns on c6 and d5 with no e-pawn - the "
        "Queen's Gambit Exchange.",
        "White (the minority-attack side)", "Black",
        "The minority attack: push the b-pawn to b5 and trade it on c6, leaving Black with "
        "a weak pawn on c6 or an isolated d5. The alternative is f3 and e4 in the centre.",
        "Play on the kingside and in the centre: a knight on e4, pieces aimed at the king, "
        "and the f-pawn if the position allows. Meet b4-b5 with ...c6xb5 or let it come and "
        "keep pieces near c6.",
        "White: b4-b5, or f3 and e4. Black: ...c5 rarely; ...f5 and ...f4 sometimes.",
        "White: rooks on b1 and c1, knight to e5 or d3-c5. Black: knight to e4, a bishop on d6 "
        "aiming at the king.",
        lambda o, x: _has(o, "d4") and _none(o, "c") and bool(_file(o, "e"))
        and _has(x, "d5", "c6") and _none(x, "e"),
    ),
    Structure(
        "maroczy", "Maroczy bind",
        "White pawns on c4 and e4 with no d-pawn; Black pawn on d6 with no c-pawn - often "
        "from the Sicilian.",
        "White (the side with the bind)", "Black",
        "Squeeze. The bind takes away ...b5 and ...d5; keep both breaks unavailable, gain "
        "space slowly, and avoid trades that relieve the cramp.",
        "Trade pieces - a cramped side benefits from every exchange - and prepare ...b5 or "
        "...d5 patiently. The dark squares (a bishop on g7, a knight on c5) are yours.",
        "Black: ...b5 and ...d5. White: c5 or f4-f5 when it costs nothing.",
        "White: a knight on d5 when it can't be traded, a bishop on e3. Black: Bg7, a knight "
        "to c5 via d7, queen to a5.",
        lambda o, x: _has(o, "c4", "e4") and _none(o, "d") and bool(_file(x, "d"))
        and _none(x, "c"),
    ),
    Structure(
        "french-advance", "Advance French chain",
        "White pawns on d4 and e5, Black pawns on d5 and e6 - two locked chains.",
        "White (the side with the e5 spearhead)", "Black",
        "Attack where the chain points: the kingside. Space for a knight on f3 and a bishop "
        "on d3, f4-f5 when it can be supported. Hold d4, the base of the chain.",
        "Attack the base of the chain: ...c5 against d4, queen to b6, knights to c6 and f5. "
        "If d4 falls, e5 is just a weak pawn. The ...f6 break hits the head.",
        "Black: ...c5 and ...f6. White: f4-f5.",
        "White: knight f3, bishop d3, rooks behind the f-pawn. Black: Qb6, Nc6, Nge7-f5.",
        lambda o, x: _has(o, "d4", "e5") and _has(x, "d5", "e6"),
    ),
    Structure(
        "kid-chain", "King's Indian chain",
        "White pawns on d5 and e4, Black pawns on d6 and e5.",
        "White (queenside space)", "Black (kingside attack)",
        "Play on the queenside, where your chain points: c4-c5 to open the c-file, a knight "
        "to c4 or b5. The race is real - count it before you go.",
        "Attack on the kingside, where your chain points: ...f5 and ...f4, then ...g5-g4. "
        "A pawn storm against the king is worth more than a pawn on the queenside.",
        "White: c4-c5. Black: ...f5, then ...f4 or ...fxe4.",
        "White: rooks on the c-file, a knight on c4. Black: knights rerouted kingside, the "
        "g7 bishop often kept behind the pawns.",
        lambda o, x: _has(o, "d5", "e4") and _has(x, "d6", "e5"),
    ),
    Structure(
        "benoni", "Benoni structure",
        "White pawn on d5 (often with e4); Black pawns on c5 and d6 and no e-pawn.",
        "White (central majority)", "Black (queenside majority)",
        "Break with e4-e5 when you can support it - it opens the centre against a king that "
        "has given up its e-pawn. Restrain ...b5 first.",
        "Use the queenside majority: ...a6 and ...b5, and the long diagonal for the g7 "
        "bishop. The e-file is half-open: rooks there tie White to e4.",
        "White: e4-e5 (sometimes f4 first). Black: ...b5, and ...c4 to open the diagonal.",
        "White: knights on c4 and d2, a bishop on d3 or e2. Black: Bg7, a knight on e5, "
        "rooks on e8 and b8.",
        lambda o, x: _has(o, "d5") and _has(x, "c5", "d6") and _none(x, "e"),
    ),
    Structure(
        "stonewall", "Stonewall",
        "Pawns on c3, d4, e3 and f4 (for Black: c6, d5, e6, f5).",
        "the Stonewall side", "the side playing against it",
        "A knight on e5 and an attack on the kingside: the wall frees your pieces to go "
        "there. Mind the hole on e4 and the bishop shut behind your own pawns.",
        "Occupy the hole on e4 (e5 against Black) with a knight and trade off the good "
        "bishop, leaving the side with the wall a bad one. Then the queenside.",
        "Owner: g4 or a kingside pawn storm. Against: c4 or e4 to hit the wall.",
        "Owner: Ne5, Bd3, a rook lift via f3. Against: a knight on e4, a bishop on b2 if "
        "the long diagonal opens.",
        lambda o, x: _has(o, "c3", "d4", "e3", "f4"),
    ),
    Structure(
        "hedgehog", "Hedgehog",
        "Pawns on a6, b6, d6 and e6 with no c-pawn, against White pawns on c4 and e4.",
        "the hedgehog side", "the side with more space",
        "Wait compactly. Every piece behind the third rank, nothing loose - and strike with "
        "...b5 or ...d5 exactly when White over-reaches. The breaks are the whole plan.",
        "Use the space but don't over-extend: restrain ...b5 and ...d5, improve your pieces, "
        "and look for e4-e5 or c4-c5 when it actually works. Patience wins hedgehogs.",
        "Hedgehog: ...b5 and ...d5. Against: e5, c5, or a kingside storm with g4.",
        "Hedgehog: bishops on b7 and e7, queen c7, rooks c8 and d8. Against: pieces on "
        "d4, c3 and e3; rooks on c1 and d1.",
        lambda o, x: _has(o, "a3", "b3", "d3", "e3") and _none(o, "c") and _has(x, "c5", "e5"),
    ),
    Structure(
        "small-centre", "Scheveningen small centre",
        "Pawns on d6 and e6 with no c-pawn, against a White e-pawn with no d-pawn.",
        "the side with the small centre", "the side with the e-pawn",
        "The small centre is solid and flexible: strike with ...d5 or ...e5 when the moment "
        "comes, and play on the half-open c-file against the queenside.",
        "Attack the king: g4-g5 or f4-f5. The small centre is slow to react to a pawn storm, "
        "and the breaks need preparing.",
        "Small centre: ...d5 and ...e5, ...b5. Against: g4-g5, f4-f5, e4-e5.",
        "Small centre: Be7, Nc6 or Nbd7, rook to c8. Against: Be3, Qf3 or Qd2, castling long.",
        lambda o, x: _has(o, "d3", "e3") and _none(o, "c") and bool(_file(x, "e"))
        and _none(x, "d"),
    ),
    Structure(
        "big-centre", "Broad pawn centre",
        "White pawns on d4 and e4, Black with no d- or e-pawn - common in the Grünfeld "
        "and the Alekhine.",
        "the side with the centre", "the side playing against it",
        "Keep the centre and gain space with it: d5 or e5 when it cramps the opponent and "
        "can be supported. A centre that is pushed too early becomes a target.",
        "Pressure the centre with pieces and the c-pawn: a bishop on g7, ...c5, knights on "
        "c6. Make it a target to defend, then break it.",
        "Owner: d5 or e5. Against: ...c5, ...e5 or ...f5.",
        "Owner: a bishop on e3, a knight on e2 so f-pawn stays flexible, rooks on c1 and d1. "
        "Against: Bg7, Nc6, Qa5.",
        lambda o, x: _has(o, "d4", "e4") and _none(x, "d", "e"),
    ),
    Structure(
        "doubled-c", "Doubled c-pawns",
        "Two pawns on the c-file (c3 and c4), usually after a bishop took a knight on c3.",
        "the side with the doubled pawns (and the bishop pair)", "the side playing against them",
        "Open the position for the bishops: f3 and e4, a centre and an attack. The doubled "
        "pawns are a long-term weakness - make the game short-term.",
        "Keep it closed and aim at c4: ...b6, ...Ba6, ...Na5 and ...Rc8. A blocked position "
        "turns the bishop pair into two bad pieces.",
        "Owner: e3-e4, f3, d4-d5. Against: ...c5 and ...e5 to fix the pawns.",
        "Owner: bishops on long diagonals, the queen on c2. Against: knights on a5 and e4, "
        "a bishop on a6.",
        lambda o, x: len(_file(o, "c")) >= 2,
    ),
    Structure(
        "symmetrical", "Symmetrical d-pawns, open e-file",
        "Both sides have a d-pawn on d4/d5 and no e-pawns - the Exchange French and similar.",
        "either side", "either side",
        "Equal pawns, so pieces decide: the e-file and the outposts on e5 and e4. The side "
        "that gets a rook to the e-file and a knight to e5 first takes over.",
        "The same, mirrored: contest the e-file at once, put a knight on e4. Symmetry rewards "
        "whoever is a tempo more active.",
        "c4 or ...c5 to make the position unbalanced.",
        "Rooks to the e-file, knights to e5/e4, bishops to d3/d6.",
        lambda o, x: _has(o, "d4") and _has(x, "d5") and _none(o, "e") and _none(x, "e"),
        symmetric=True,
    ),
    Structure(
        "boleslavsky-hole", "Boleslavsky hole",
        "Pawns on d6 and e5 with no c-pawn (the Najdorf), against a White e-pawn with no "
        "d-pawn. The d5 square can never be defended by a pawn.",
        "the side with the d6/e5 pawns", "the side with the d5 outpost",
        "Fight for d5 with pieces - ...Be6, ...Nbd7-f6 - and break with ...d5 when you can: "
        "it dissolves the weakness at once. Meanwhile play on the queenside.",
        "Own d5: a knight there that cannot be traded is worth more than a pawn. Trade the "
        "pieces that defend d5 (the f6 knight, the light bishop) and plant yours.",
        "Owner: ...d5 above all, ...b5-b4. Against: Nd5, c4 to fix the structure.",
        "Owner: Be6, Be7, a rook on c8. Against: knights to d5, a bishop on c4 or g4 "
        "against the defenders.",
        lambda o, x: _has(o, "d3", "e4") and _none(o, "c") and bool(_file(x, "e"))
        and _none(x, "d"),
    ),
    Structure(
        "queenside-majority", "Queenside pawn majority",
        "One side has more pawns on the a- to c-files than the other, with material level.",
        "the side with the majority", "the side with the central or kingside majority",
        "A majority is a passed pawn waiting to happen: advance it - the candidate pawn "
        "first, the one with no opposing pawn in front - and aim for an endgame, where the "
        "enemy king is too far away.",
        "Use your own majority in the centre or on the kingside while you still have pieces, "
        "and keep the queenside majority blocked: a piece in front of the candidate pawn.",
        "Owner: b4-b5 or c4-c5 to create the passer. Against: e4-e5, f4-f5 on the other wing.",
        "Owner: rooks behind the pawns, the king to the centre in an endgame. Against: a "
        "knight as blockader, pieces aimed at the king.",
        lambda o, x: _qs_count(o) > _qs_count(x) and len(o) == len(x),
    ),
)

BY_KEY = {s.key: s for s in STRUCTURES}


_PIECE_VALUES = {"n": 3, "b": 3, "r": 5, "q": 9}


def _parse_fen(fen: str) -> tuple[Pawns, Pawns, int, int]:
    """(white pawns, black pawns, non-pawn material, fullmove) straight from the
    FEN string. Building a `chess.Board` per position to read its pawns was most
    of the cost of profiling a library: tens of thousands of positions."""
    parts = fen.split()
    white: Pawns = set()
    black: Pawns = set()
    npm = 0
    for i, row in enumerate(parts[0].split("/")):
        rank = 8 - i
        f = 0
        for ch in row:
            if ch.isdigit():
                f += int(ch)
                continue
            if ch == "P":
                white.add((f, rank))
            elif ch == "p":
                black.add((f, rank))
            else:
                npm += _PIECE_VALUES.get(ch.lower(), 0)
            f += 1
    try:
        fullmove = int(parts[5])
    except (IndexError, ValueError):
        fullmove = 1
    return white, black, npm, fullmove


def _mirror(p: Pawns) -> Pawns:
    return {(f, 9 - r) for f, r in p}


def detect_fen(fen: str) -> list[tuple[str, str | None]]:
    """`detect` on a FEN string, without building a board."""
    white, black, npm, fullmove = _parse_fen(fen)
    # The opening proper and bare endings are not where these plans live.
    if npm < 20 or fullmove < 8:
        return []
    views = ((white, black, "white"), (_mirror(black), _mirror(white), "black"))
    found: list[tuple[str, str | None]] = []
    for s in STRUCTURES:
        for own, opp, colour in views:
            if s.rule(own, opp):
                found.append((s.key, None if s.symmetric else colour))
                break
    return found


def detect(board: chess.Board) -> list[tuple[str, str | None]]:
    """(structure key, owner colour) for each structure on the board. The owner
    is "white"/"black", or None for a symmetric structure."""
    return detect_fen(board.fen())


def describe_for(key: str, owner: str | None, viewer: str) -> dict:
    """The lesson from ``viewer``'s side of the board."""
    s = BY_KEY[key]
    is_owner = owner is None or owner == viewer
    return {
        "key": s.key, "name": s.name, "recognise": s.recognise,
        "role": s.owner_label if is_owner else s.against_label,
        "plan": s.owner_plan if is_owner else s.against_plan,
        "other_plan": s.against_plan if is_owner else s.owner_plan,
        "breaks": s.breaks, "pieces": s.pieces, "is_owner": is_owner,
    }


def lesson(key: str) -> dict:
    s = BY_KEY[key]
    return {"key": s.key, "name": s.name, "recognise": s.recognise,
            "owner_label": s.owner_label, "against_label": s.against_label,
            "owner_plan": s.owner_plan, "against_plan": s.against_plan,
            "breaks": s.breaks, "pieces": s.pieces}


# ---------------------------------------------------------------------------
# Your games, structure by structure.

_MIDDLE = (16, 60)   # plies where the structure is "the position" rather than the opening


def _user_positions(session: Session, account: str | None, limit_games: int = 400):
    q = (select(Move.fen_before, Move.cp_loss, Move.decision_w, Game.id, Game.color, Game.result)
         .join(Game, Move.game_id == Game.id)
         .where(Move.is_user_move.is_(True), Move.cp_loss.isnot(None),
                Move.ply.between(*_MIDDLE), Game.source != "sparring")
         .order_by(Game.played_at.desc()))
    if account:
        q = q.where(Game.account == account)
    return session.execute(q.limit(limit_games * 25)).all()


_PROFILE_CACHE: dict[tuple, list[dict]] = {}


def _library_stamp(session: Session, account: str | None) -> tuple:
    """Changes whenever an analysed game is added, so the cache can't go stale."""
    from sqlalchemy import func

    q = select(func.count(Game.id), func.max(Game.id)).where(Game.analyzed.is_(True))
    if account:
        q = q.where(Game.account == account)
    return tuple(session.execute(q).one())


def profile(account: str | None = None, db_path=None) -> list[dict]:
    """How the player does in each structure, as its owner and against it.

    Cached per library state: it reads every middlegame position the player has
    reached, and the answer only changes when a new game is analysed."""
    with session_factory(db_path)() as session:
        key = (str(db_path), account, _library_stamp(session, account))
        if key in _PROFILE_CACHE:
            return _PROFILE_CACHE[key]
        rows = _user_positions(session, account)
    out = _profile_rows(rows)
    _PROFILE_CACHE.clear()
    _PROFILE_CACHE[key] = out
    return out


def _profile_rows(rows) -> list[dict]:
    agg: dict[tuple[str, bool], dict] = defaultdict(lambda: {"cp": [], "games": {}})
    for fen, cp, dw, gid, color, result in rows:
        for key, owner in detect_fen(fen):
            is_owner = owner is None or owner == color
            a = agg[(key, is_owner)]
            a["cp"].append((int(cp), 1.0 if dw is None else float(dw)))
            a["games"][gid] = result
    out = []
    for (key, is_owner), a in agg.items():
        if len(a["games"]) < 2:
            continue
        w = sum(x for _, x in a["cp"]) or 1e-9
        score = statistics.mean(1.0 if r == "win" else 0.5 if r == "draw" else 0.0
                                for r in a["games"].values())
        s = BY_KEY[key]
        out.append({"key": key, "name": s.name, "is_owner": is_owner,
                    "role": s.owner_label if is_owner else s.against_label,
                    "games": len(a["games"]), "decisions": round(w, 1),
                    "avg_cp_loss": round(sum(c * x for c, x in a["cp"]) / w, 1),
                    "score": round(score, 3)})
    out.sort(key=lambda r: (-r["avg_cp_loss"]))
    return out


def example_position(session: Session, key: str, account: str | None = None,
                     rng: random.Random | None = None) -> dict | None:
    """A position from the player's own games with this structure."""
    rng = rng or random.Random()
    rows = list(_user_positions(session, account))
    # Scan in random order and stop at a handful of hits: one example is needed,
    # not every position in the library.
    rng.shuffle(rows)
    hits = []
    for fen, _cp, _dw, gid, color, _res in rows:
        for k, owner in detect_fen(fen):
            if k == key:
                hits.append((fen, color, owner, gid))
        if len(hits) >= 12:
            break
    if not hits:
        return None
    fen, color, owner, gid = rng.choice(hits)
    return {"fen": fen, "colour": color, "owner": owner, "game_id": gid}


def quiz(key: str, viewer_is_owner: bool, rng: random.Random | None = None) -> dict:
    """Three plans, one right: the plan for this side of this structure, and two
    distractors from other structures. Shuffled; the answer is an index."""
    rng = rng or random.Random()
    s = BY_KEY[key]
    right = s.owner_plan if viewer_is_owner else s.against_plan
    others = [x for x in STRUCTURES if x.key != key and not x.symmetric]
    picks = rng.sample(others, 2)
    wrong = [(p.owner_plan if rng.random() < 0.5 else p.against_plan) for p in picks]
    options = [right, *wrong]
    rng.shuffle(options)
    return {"options": options, "answer": options.index(right),
            "role": s.owner_label if viewer_is_owner else s.against_label}


_QUIZ_KEY = "structure_quiz"


def record_quiz(session: Session, key: str, correct: bool) -> dict:
    raw = get_setting(session, _QUIZ_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    row = data.setdefault(key, {"n": 0, "right": 0})
    row["n"] += 1
    row["right"] += 1 if correct else 0
    set_setting(session, _QUIZ_KEY, json.dumps(data, sort_keys=True))
    return row


def quiz_stats(session: Session) -> dict:
    raw = get_setting(session, _QUIZ_KEY)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}
