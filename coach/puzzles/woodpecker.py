"""Woodpecker cycles: one fixed set of tactics, solved faster each pass.

Smith and Tikkanen's method. Spaced repetition schedules each puzzle on its own
clock so it is remembered; the Woodpecker method does the opposite on purpose:
the same 100-200 positions, in the same order, again and again, each pass in
less time than the last. By the third pass the answers are known - that is the
point. What is being trained is no longer finding the move but *seeing it
instantly*, so the pattern fires in a game without being looked for.

The set is chosen below the player's puzzle rating (the method wants positions
that are solvable, not hard) from their own games first, the library second,
and tactics only: this is pattern speed, and quiet positions have no pattern
to speed up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from coach.puzzles.srs import puzzle_rating
from coach.store.db import Puzzle, WoodpeckerSet

DEFAULT_SIZE = 150
MIN_SIZE, MAX_SIZE = 30, 200
# Below the solver's rating by this much: the method wants a set that can be
# finished, then finished fast.
EASIER_BY = 250
BAND = 350
TACTICAL_PILLARS = ("tactics",)
# A pass at this accuracy counts towards "done with this set".
CLEAN_PASS = 0.9


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _passes(ws: WoodpeckerSet) -> list[dict]:
    try:
        data = json.loads(ws.passes or "[]")
    except ValueError:
        data = []
    return data if isinstance(data, list) else []


def _ids(ws: WoodpeckerSet) -> list[int]:
    try:
        return [int(x) for x in json.loads(ws.puzzle_ids)]
    except (ValueError, TypeError):
        return []


def active(session: Session) -> WoodpeckerSet | None:
    return session.scalar(
        select(WoodpeckerSet).where(WoodpeckerSet.finished_at.is_(None))
        .order_by(WoodpeckerSet.id.desc())
    )


def available(session: Session) -> int:
    return len(_pool(session))


def _pool(session: Session) -> list[Puzzle]:
    q = select(Puzzle).where(Puzzle.pillar.in_(TACTICAL_PILLARS),
                             Puzzle.source.in_(("own-game", "library")))
    return list(session.scalars(q))


def build(session: Session, size: int = DEFAULT_SIZE) -> WoodpeckerSet:
    """Choose the set. Raises ValueError when there isn't enough material."""
    size = max(MIN_SIZE, min(MAX_SIZE, int(size)))
    pool = _pool(session)
    if len(pool) < MIN_SIZE:
        raise ValueError(
            f"a Woodpecker set needs at least {MIN_SIZE} tactical puzzles; you have "
            f"{len(pool)}. Mine more from your games or import some from Lichess."
        )
    target = puzzle_rating(session) - EASIER_BY

    def key(p: Puzzle) -> tuple:
        gap = abs((p.rating_est or target) - target)
        return (0 if gap <= BAND else 1, 0 if p.source == "own-game" else 1, gap)

    pool.sort(key=key)
    chosen = pool[:size]
    # One game should not dominate: interleave so neighbouring puzzles come from
    # different games where possible.
    chosen.sort(key=lambda p: (p.game_id or 0, p.id))
    by_game: dict[int, list[Puzzle]] = {}
    for p in chosen:
        by_game.setdefault(p.game_id if p.game_id is not None else -p.id, []).append(p)
    order: list[int] = []
    queues = list(by_game.values())
    while queues:
        for q in list(queues):
            order.append(q.pop(0).id)
            if not q:
                queues.remove(q)
    ws = WoodpeckerSet(puzzle_ids=json.dumps(order), passes="[]")
    session.add(ws)
    session.flush()
    return ws


def _current_pass(ws: WoodpeckerSet, passes: list[dict]) -> dict:
    if not passes or passes[-1].get("finished"):
        passes.append({"n": len(passes) + 1, "started": _now().isoformat(),
                       "finished": None, "done": [], "solved": 0, "ms": 0})
    return passes[-1]


def next_puzzle_id(ws: WoodpeckerSet) -> int | None:
    passes = _passes(ws)
    done = set(passes[-1]["done"]) if passes and not passes[-1].get("finished") else set()
    for pid in _ids(ws):
        if pid not in done:
            return pid
    return None


def answer(session: Session, ws: WoodpeckerSet, puzzle_id: int, solved: bool,
           elapsed_ms: int | None) -> dict:
    ids = _ids(ws)
    if puzzle_id not in ids:
        raise ValueError("that puzzle is not in this set")
    passes = _passes(ws)
    cur = _current_pass(ws, passes)
    if puzzle_id not in cur["done"]:
        cur["done"].append(puzzle_id)
        cur["solved"] += 1 if solved else 0
        # A tab left open is not thinking time; the same cap the SRS uses.
        cur["ms"] += min(int(elapsed_ms or 0), 180_000)
    if len(cur["done"]) >= len(ids):
        cur["finished"] = _now().isoformat()
        if len(passes) >= ws.max_passes or _clean_and_faster(passes):
            ws.finished_at = _now()
    ws.passes = json.dumps(passes)
    return state(ws)


def _clean_and_faster(passes: list[dict]) -> bool:
    """Done with the set: two clean passes in a row, the second no slower."""
    fin = [p for p in passes if p.get("finished")]
    if len(fin) < 3:
        return False
    a, b = fin[-2], fin[-1]
    n = len(a["done"]) or 1
    return (a["solved"] / n >= CLEAN_PASS and b["solved"] / n >= CLEAN_PASS
            and b["ms"] <= a["ms"])


def state(ws: WoodpeckerSet) -> dict:
    ids = _ids(ws)
    passes = _passes(ws)
    rows = []
    for p in passes:
        n = len(p["done"])
        rows.append({
            "n": p["n"], "answered": n, "solved": p["solved"],
            "accuracy": round(p["solved"] / n, 3) if n else None,
            "minutes": round(p["ms"] / 60000, 1),
            "sec_per_puzzle": round(p["ms"] / 1000 / n, 1) if n else None,
            "finished": p.get("finished"),
        })
    fin = [r for r in rows if r["finished"]]
    target = None
    if fin:
        # The method's rhythm: each pass in about half the time of the last,
        # floored so the target stays humanly reachable.
        target = max(round(fin[-1]["minutes"] / 2, 1), round(len(ids) * 4 / 60, 1))
    return {
        "id": ws.id, "size": len(ids), "passes": rows,
        "pass": rows[-1]["n"] if rows and not rows[-1]["finished"] else len(rows) + 1,
        "target_minutes": target, "max_passes": ws.max_passes,
        "finished": ws.finished_at.isoformat() if ws.finished_at else None,
        "next_puzzle_id": None if ws.finished_at else next_puzzle_id(ws),
    }
