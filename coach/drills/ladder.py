"""The endgame ladder: named technique in the order it builds on itself.

The drill set was a flat list, served by the SRS in whatever order cards fell
due - so a player could meet the Lucena before they could mate with a rook,
and nothing said what to learn next. The ladder orders it. Each rung unlocks
when every position on the one below has been converted (or held) at least
once, and the daily endgame drill draws only from rungs that are open.

A player who already knows the basics should not have to grind them: any rung
can be **tested out** of by passing all its positions in one go, first try,
which opens it and everything beneath it. The test-out result is recorded by
the server from the drill's own adjudication, not reported by the browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from coach.drills.endgames import BY_KEY
from coach.store.db import Puzzle, SrsCard, get_setting, set_setting


@dataclass(frozen=True)
class Rung:
    name: str
    keys: tuple[str, ...]
    why: str


RUNGS: tuple[Rung, ...] = (
    Rung("Basic mates", ("mate-kq", "mate-kr"),
         "Every won ending ends in one of these. Without them a queen up is not a win."),
    Rung("King and pawn", ("square-of-the-pawn", "king-first", "defensive-opposition"),
         "Opposition and the square: the arithmetic every pawn ending is built from."),
    Rung("Pawn endings", ("outside-passer", "triangulation"),
         "Using the arithmetic: decoys, and losing a move on purpose."),
    Rung("Rook endings: foundations", ("rook-behind-passer", "rook-cutoff", "philidor"),
         "Half of all endings are rook endings. Activity, cutting the king off, and the "
         "one defence everyone must know."),
    Rung("Winning technique", ("lucena", "queen-v-pawn"),
         "The two conversions that come up again and again once you are winning."),
    Rung("Minor pieces", ("wrong-bishop", "opposite-bishops-hold"),
         "Where material stops counting: the draws you can save and must not allow."),
    Rung("Two bishops", ("mate-two-bishops",),
         "The hardest of the basic mates, and the last rung."),
)

_KEY = "endgame_ladder"


def _state(session: Session) -> dict:
    raw = get_setting(session, _KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save(session: Session, data: dict) -> None:
    set_setting(session, _KEY, json.dumps(data, sort_keys=True))


def _puzzles(session: Session) -> dict[str, Puzzle]:
    rows = session.scalars(select(Puzzle).where(Puzzle.source == "endgame"))
    return {p.external_id.removeprefix("endgame:"): p for p in rows if p.external_id}


def _converted(session: Session, puzzles: dict[str, Puzzle]) -> set[str]:
    ids = {p.id: k for k, p in puzzles.items()}
    if not ids:
        return set()
    out = set()
    for pid, wins in session.execute(
        select(SrsCard.puzzle_id, SrsCard.wins).where(SrsCard.puzzle_id.in_(ids))
    ):
        if wins:
            out.add(ids[pid])
    return out


def status(session: Session) -> dict:
    """Every rung, whether it is open, passed, and which positions remain."""
    puzzles = _puzzles(session)
    done = _converted(session, puzzles)
    tested = set(_state(session).get("tested_out", []))
    rungs = []
    open_until = 0
    for i, r in enumerate(RUNGS):
        passed = i in tested or all(k in done for k in r.keys)
        rungs.append({"index": i, "name": r.name, "why": r.why, "passed": passed,
                      "tested_out": i in tested,
                      "positions": [{"key": k, "name": BY_KEY[k].name,
                                     "id": puzzles[k].id if k in puzzles else None,
                                     "done": k in done, "rating": BY_KEY[k].rating}
                                    for k in r.keys]})
    for r in rungs:
        open_until = r["index"]
        if not r["passed"]:
            break
    else:
        open_until = len(RUNGS) - 1
    for r in rungs:
        r["open"] = r["index"] <= open_until
        r["current"] = r["index"] == open_until and not r["passed"]
    return {"rungs": rungs, "current": open_until,
            "complete": all(r["passed"] for r in rungs),
            "testout": _state(session).get("testout")}


def locked_ids(session: Session) -> list[int]:
    """Endgame drills on rungs that are not open yet - kept out of the queue."""
    st = status(session)
    return [p["id"] for r in st["rungs"] if not r["open"]
            for p in r["positions"] if p["id"] is not None]


def start_testout(session: Session, rung: int) -> dict:
    if not 0 <= rung < len(RUNGS):
        raise ValueError("no such rung")
    data = _state(session)
    data["testout"] = {"rung": rung, "results": {}}
    _save(session, data)
    puzzles = _puzzles(session)
    return {"rung": rung, "name": RUNGS[rung].name,
            "positions": [{"key": k, "name": BY_KEY[k].name,
                           "id": puzzles[k].id if k in puzzles else None}
                          for k in RUNGS[rung].keys]}


def record_testout(session: Session, puzzle: Puzzle, success: bool) -> dict | None:
    """File one test-out drill result; on a clean sweep open the rung and below.

    Returns the test-out state, or None when no test-out covers this puzzle.
    Only the *first* result for each position counts: a test-out is first try.
    """
    data = _state(session)
    t = data.get("testout")
    key = (puzzle.external_id or "").removeprefix("endgame:")
    if not t or key not in RUNGS[t["rung"]].keys:
        return None
    t["results"].setdefault(key, bool(success))
    keys = RUNGS[t["rung"]].keys
    out = {"rung": t["rung"], "results": dict(t["results"]), "finished": False, "passed": False}
    if any(v is False for v in t["results"].values()):
        out["finished"] = True
        data.pop("testout", None)
    elif all(k in t["results"] for k in keys):
        out["finished"] = out["passed"] = True
        tested = set(data.get("tested_out", []))
        tested.update(range(t["rung"] + 1))
        data["tested_out"] = sorted(tested)
        data.pop("testout", None)
    _save(session, data)
    return out


def cancel_testout(session: Session) -> None:
    data = _state(session)
    data.pop("testout", None)
    _save(session, data)
