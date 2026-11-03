"""Calculation training: see the whole line before you touch a piece.

Every other puzzle in the app is played one move at a time, with the board
updating after each one - which means the player never has to hold more than
one move in their head. That is recognition, not calculation, and calculation
is where club players lose most of their points: the tactic is seen, the line
is not followed to the end, and the third move turns out not to work.

So here the position is shown and the player types the *whole* line - their
moves and the replies they expect - before anything moves. Every ply is
checked: their own moves against the stored solution or anything the engine
rates as good, the opponent's against the best defence, because a line that
only works if the opponent cooperates has not been calculated.

Depth adapts: lines start two moves deep and grow while the player keeps
getting them right, shrinking again when they don't.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable

import chess
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coach.puzzles.candidates import choosing_board
from coach.store.db import Puzzle, get_setting, set_setting

MIN_DEPTH, MAX_DEPTH = 2, 4         # in the player's own moves
DEFAULT_DEPTH = 2
# Accept any move (either side's) within this of the best.
ACCEPT_CP = 50
_PROMOTE_AFTER, _PROMOTE_RATE = 5, 0.7
_DEMOTE_RATE = 0.3
_RECENT_KEEP = 200

_STATS_KEY = "calc_stats"
_RATING_KEY = "calc_rating"
_SEEN_KEY = "calc_seen"
_RATING_K = 24
# Each extra move of depth makes the same position harder to see to the end.
DEPTH_BONUS = 120

Verifier = Callable[[chess.Board, chess.Move], "int | None"]

_NOISE = re.compile(r"^\d+\.+$|^\.\.\.$")


def plies_for(depth: int) -> int:
    """User moves ``depth`` -> plies to type (the line ends on the user's move)."""
    return 2 * depth - 1


def _line(puzzle: Puzzle) -> list[str]:
    sol = puzzle.solution.split()
    opp_first = bool(puzzle.opponent_first) or puzzle.source == "library"
    return sol[1:] if opp_first and sol else sol


def parse_san_line(board: chess.Board, text: str) -> tuple[list[chess.Move], str | None]:
    """Typed notation -> moves. Tolerates move numbers, '...', commas, !? and
    missing +/#. Returns the legal prefix and, if parsing stopped early, the
    token that would not parse."""
    b = board.copy(stack=False)
    out: list[chess.Move] = []
    for raw in re.split(r"[\s,;]+", text or ""):
        tok = raw.strip()
        tok = re.sub(r"^\d+\.+", "", tok)        # "1.e4" / "12...Nf6"
        tok = tok.rstrip("!?")
        if not tok or _NOISE.match(tok):
            continue
        tok = tok.replace("0-0-0", "O-O-O").replace("0-0", "O-O")
        try:
            mv = b.parse_san(tok)
        except ValueError:
            try:
                mv = chess.Move.from_uci(tok.lower())
                if mv not in b.legal_moves:
                    return out, raw
            except ValueError:
                return out, raw
        out.append(mv)
        b.push(mv)
    return out, None


def check_line(puzzle: Puzzle, text: str, depth: int,
               verifier: Verifier | None = None) -> dict:
    """Grade a typed line ply by ply against the solution and the engine."""
    board = choosing_board(puzzle)
    stored = _line(puzzle)
    needed = min(plies_for(depth), len(stored))
    if needed % 2 == 0:
        needed -= 1          # always end on the player's own move
    moves, bad = parse_san_line(board, text)
    if bad is not None:
        # A typo is not a miscalculation: hand it back unfiled so it can be fixed.
        return {"status": "unreadable", "bad": bad, "parsed": len(moves),
                "message": (f"Couldn't read '{bad}' as a legal move there"
                            f"{' (after ' + str(len(moves)) + ' good plies)' if moves else ''}."),
                "needed": needed, "depth": (needed + 1) // 2}
    b = board.copy(stack=False)
    plies: list[dict] = []
    on_line = True
    failed_at = None
    for i in range(needed):
        mine = i % 2 == 0
        want = chess.Move.from_uci(stored[i]) if on_line and i < len(stored) else None
        want_san = b.san(want) if want is not None and want in b.legal_moves else None
        if i >= len(moves):
            plies.append({"san": None, "ok": False, "mine": mine, "expected": want_san,
                          "note": "the line stops here"})
            failed_at = i
            break
        mv = moves[i]
        san = b.san(mv)
        if want is not None and mv == want:
            plies.append({"san": san, "ok": True, "mine": mine})
            b.push(mv)
            continue
        behind = verifier(b, mv) if verifier is not None else None
        if behind is not None and behind <= ACCEPT_CP:
            plies.append({"san": san, "ok": True, "mine": mine,
                          "note": "different from the stored line, but just as good"})
            on_line = False
            b.push(mv)
            continue
        if mine:
            note = "not the strongest move here"
        else:
            note = "they have a better reply"
        plies.append({"san": san, "ok": False, "mine": mine, "expected": want_san,
                      "behind": behind, "note": note})
        failed_at = i
        break

    solved = failed_at is None
    full = board.copy(stack=False)
    line_san = []
    for u in stored[:needed]:
        mv = chess.Move.from_uci(u)
        if mv not in full.legal_moves:
            break
        line_san.append(full.san(mv))
        full.push(mv)
    return {
        "status": "solved" if solved else "wrong",
        "plies": plies,
        "reached": sum(1 for p in plies if p["ok"]),
        "needed": needed,
        "depth": (needed + 1) // 2,
        "solution_san": line_san,
        "fen_end": b.fen(),
    }


# ---------------------------------------------------------------------------
# Adaptive depth, rating, selection.

def _load(session: Session, key: str, default):
    raw = get_setting(session, key)
    try:
        val = json.loads(raw) if raw else default
    except ValueError:
        val = default
    return val if isinstance(val, type(default)) else default


def stats(session: Session) -> dict:
    return _load(session, _STATS_KEY, {})


def calc_rating(session: Session) -> int:
    stored = get_setting(session, _RATING_KEY)
    if stored:
        return int(float(stored))
    from coach.puzzles.srs import puzzle_rating

    seed = puzzle_rating(session) - DEPTH_BONUS
    set_setting(session, _RATING_KEY, str(seed))
    return seed


def current_depth(session: Session) -> int:
    data = stats(session)
    return int(data.get("depth", DEFAULT_DEPTH))


def record(session: Session, puzzle: Puzzle, result: dict) -> dict:
    """File a result: per-depth tally, recent window, adaptive depth, rating."""
    data = stats(session)
    depth = int(result["depth"])
    solved = result["status"] == "solved"
    row = data.setdefault("by_depth", {}).setdefault(str(depth), {"n": 0, "solved": 0})
    row["n"] += 1
    row["solved"] += 1 if solved else 0
    recent = data.setdefault("recent", [])
    recent.append([depth, 1 if solved else 0])
    del recent[:-20]

    cur = int(data.get("depth", DEFAULT_DEPTH))
    at_cur = [s for d, s in recent if d == cur][-_PROMOTE_AFTER:]
    if len(at_cur) >= _PROMOTE_AFTER:
        rate = sum(at_cur) / len(at_cur)
        if rate >= _PROMOTE_RATE and cur < MAX_DEPTH:
            cur += 1
        elif rate <= _DEMOTE_RATE and cur > MIN_DEPTH:
            cur -= 1
    data["depth"] = cur
    set_setting(session, _STATS_KEY, json.dumps(data, sort_keys=True))

    r = calc_rating(session)
    opp = (puzzle.rating_est or r) + DEPTH_BONUS * (depth - 1)
    expected = 1.0 / (1.0 + 10 ** ((opp - r) / 400.0))
    new = round(r + _RATING_K * ((1.0 if solved else 0.0) - expected))
    set_setting(session, _RATING_KEY, str(max(200, min(2800, new))))

    seen = _load(session, _SEEN_KEY, [])
    seen.append(puzzle.id)
    set_setting(session, _SEEN_KEY, json.dumps(seen[-_RECENT_KEEP:]))
    return {"depth_next": cur, "rating": max(200, min(2800, new))}


def next_task(session: Session, depth: int | None = None,
              rng: random.Random | None = None) -> dict | None:
    """A puzzle whose stored line is long enough for today's depth.

    Tactical lines first: a quiet positional PV is the engine's preference
    between near-equal moves several plies out, which is not something anyone
    should be asked to calculate.
    """
    rng = rng or random.Random()
    depth = depth or current_depth(session)
    seen = set(_load(session, _SEEN_KEY, []))
    target = calc_rating(session) - DEPTH_BONUS * (depth - 1)
    for d in range(depth, MIN_DEPTH - 1, -1):
        need = plies_for(d)
        for pillars in (("tactics", "endgame"), None):
            q = select(Puzzle).where(Puzzle.source.in_(("own-game", "library")))
            if pillars:
                q = q.where(Puzzle.pillar.in_(pillars))
            # Length in plies, from the number of spaces in the stored line - a
            # coarse prefilter; the opponent's opening move is allowed for below.
            spaces = func.length(Puzzle.solution) - func.length(
                func.replace(Puzzle.solution, " ", ""))
            q = q.where(spaces >= need - 1)
            rows = [p for p in session.scalars(q.limit(600)) if len(_line(p)) >= need]
            fresh = [p for p in rows if p.id not in seen] or rows
            if not fresh:
                continue
            fresh.sort(key=lambda p: abs((p.rating_est or target) - target))
            pick = rng.choice(fresh[:12])
            board = choosing_board(pick)
            return {
                "id": pick.id,
                "fen": board.fen(),
                "user_colour": "white" if board.turn == chess.WHITE else "black",
                "depth": d,
                "plies": plies_for(d),
                "pillar": pick.pillar,
                "motif": pick.motif,
                "rating": (pick.rating_est or target) + DEPTH_BONUS * (d - 1),
            }
    return None


def summary(session: Session) -> dict:
    data = stats(session)
    by = {d: {"n": r["n"], "rate": round(r["solved"] / r["n"], 3)}
          for d, r in (data.get("by_depth") or {}).items() if r.get("n")}
    return {"depth": int(data.get("depth", DEFAULT_DEPTH)), "by_depth": by,
            "rating": calc_rating(session)}
