"""Candidate moves first: train the search, not only the answer.

In a quiet or positional position the skill is not recognising a pattern, it
is *generating options*: a player who considers only one move cannot find a
better one, however good their judgement. Every other block in the app grades
the move that was played, so it can only ever see whether the search happened
to land on the answer.

So on quiet and positional puzzles the player lists two or three candidate
moves before choosing. The list is graded against the engine's own short list
*after* the puzzle resolves - grading it before the move would hand over the
answer - and the result is fed back separately from whether the move itself
was right. "You found Rd1 but never considered it" and "you considered f4 and
chose wrong" are different lessons.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import chess
from sqlalchemy.orm import Session

from coach.store.db import Puzzle, get_setting, set_setting

# Pillars where the list is required. Tactics are recognition problems where a
# forced candidate list mostly slows the player down; quiet and positional
# positions are exactly where options go unconsidered.
CANDIDATE_PILLARS = ("quiet", "positional")
MIN_CANDIDATES, MAX_CANDIDATES = 2, 3
# A move within this of the best counts as a good candidate.
GOOD_CP = 60
# Within this it is still a reasonable thing to consider.
PLAYABLE_CP = 150
CANDIDATE_NODES = 150_000

_ALL_KEY = "candidates_all"
_STATS_KEY = "candidate_stats"

# board, budget, limit -> [(move, cp from the mover's POV)], best first.
Scorer = Callable[[chess.Board, int, int], "list[tuple[chess.Move, int]]"]


def candidates_everywhere(session: Session) -> bool:
    return (get_setting(session, _ALL_KEY, "0") or "0") == "1"


def set_candidates_everywhere(session: Session, on: bool) -> None:
    set_setting(session, _ALL_KEY, "1" if on else "0")


def required(puzzle: Puzzle, everywhere: bool = False) -> bool:
    """Should this puzzle open with a candidate list?

    Never for drills judged on a goal or a remembered line: an endgame drill is
    about technique over twenty moves, and a repertoire line is about recall.
    """
    if puzzle.source in ("endgame", "opening", "repertoire") or puzzle.kind == "line":
        return False
    return everywhere or puzzle.pillar in CANDIDATE_PILLARS


def _default_scorer(board: chess.Board, budget: int, limit: int):
    from coach.sparring.backend import shared_backend

    return shared_backend().score_roots(board, budget, limit=limit)


def grade(board: chess.Board, listed: list[str], scorer: Scorer | None = None) -> dict:
    """How good was the list, against the engine's own top few.

    ``board`` is the position the player was choosing in.
    """
    moves: list[chess.Move] = []
    for uci in listed[:MAX_CANDIDATES]:
        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            continue
        if mv not in board.legal_moves and len(uci) == 4:
            # The board sends from+to only; a pawn reaching the last rank means
            # the queen unless the list says otherwise.
            mv = chess.Move(mv.from_square, mv.to_square, promotion=chess.QUEEN)
        if mv in board.legal_moves and mv not in moves:
            moves.append(mv)
    if not moves:
        return {"graded": False, "note": "no candidates were listed"}
    try:
        ranked = (scorer or _default_scorer)(board, CANDIDATE_NODES, 8)
    except Exception:  # noqa: BLE001 - a grading failure must not break a puzzle
        ranked = []
    if not ranked:
        return {"graded": False, "note": "the engine could not rank this position"}

    best_cp = ranked[0][1]
    by_move = {mv: cp for mv, cp in ranked}
    good = [mv for mv, cp in ranked if best_cp - cp <= GOOD_CP][:MAX_CANDIDATES]

    rows = []
    for mv in moves:
        cp = by_move.get(mv)
        behind = None if cp is None else best_cp - cp
        if mv == ranked[0][0]:
            verdict = "best"
        elif behind is not None and behind <= GOOD_CP:
            verdict = "good"
        elif behind is not None and behind <= PLAYABLE_CP:
            verdict = "playable"
        else:
            verdict = "weak"
        rows.append({"uci": mv.uci(), "san": board.san(mv), "behind": behind,
                     "verdict": verdict})

    missed = [{"uci": mv.uci(), "san": board.san(mv), "behind": best_cp - by_move[mv]}
              for mv in good if mv not in moves]
    found_best = ranked[0][0] in moves
    hits = sum(1 for r in rows if r["verdict"] in ("best", "good"))
    # Coverage of the good moves that were there to find, out of as many as the
    # list had room for. One good move in a position that only has one is full
    # marks.
    score = hits / max(1, min(len(good), len(moves)))
    if found_best:
        note = "The best move was on your list."
    elif hits:
        note = f"Good list, but you didn't consider {missed[0]['san']}." if missed else "Good list."
    else:
        note = (f"None of your candidates was close. The move to consider was "
                f"{missed[0]['san'] if missed else board.san(ranked[0][0])}.")
    return {"graded": True, "listed": rows, "missed": missed, "found_best": found_best,
            "score": round(min(1.0, score), 2), "note": note}


def record(session: Session, result: dict, pillar: str | None) -> dict:
    """Keep a running tally, so progress at *considering* moves can be shown."""
    if not result.get("graded"):
        return stats(session)
    data = stats(session)
    for key in ("all", pillar or "other"):
        row = data.setdefault(key, {"n": 0, "found_best": 0, "score_sum": 0.0})
        row["n"] += 1
        row["found_best"] += 1 if result.get("found_best") else 0
        row["score_sum"] = round(row["score_sum"] + float(result.get("score", 0.0)), 3)
    set_setting(session, _STATS_KEY, json.dumps(data, sort_keys=True))
    return data


def stats(session: Session) -> dict:
    raw = get_setting(session, _STATS_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def summary(session: Session) -> dict:
    out = {}
    for key, row in stats(session).items():
        n = row.get("n", 0)
        if n:
            out[key] = {"n": n, "found_best_rate": round(row["found_best"] / n, 3),
                        "avg_score": round(row["score_sum"] / n, 3)}
    return out


def choosing_board(puzzle: Puzzle, auto_first: bool = True) -> chess.Board:
    """The position the player chooses their first move in."""
    board = chess.Board(puzzle.fen)
    sol = puzzle.solution.split()
    opp_first = bool(puzzle.opponent_first) or puzzle.source == "library"
    if opp_first and sol and auto_first:
        board.push(chess.Move.from_uci(sol[0]))
    return board
