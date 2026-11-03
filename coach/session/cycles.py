"""Two-week focus cycles: one pillar, a test before, a test after, a report.

The daily planner picks the worst pillar *today*. That is the right question
for one session and the wrong one for a curriculum: rankings wobble from game to
game, and a pillar that ranks first on Monday and second on Tuesday never gets
the run of consecutive days it takes to improve. A cycle holds the main pillar
for ``CYCLE_DAYS`` (the drill block still covers a second weakness every day),
then measures what happened.

Measurement uses two instruments, because each has a blind spot:

* a **test** - two parallel sets of positions from the pillar, matched on
  rating, one at the start and the other at the end. Parallel forms, not the
  same positions twice, or the post-test measures memory of the pre-test.
* **real games** - the pillar's cp-loss and blunder rate in games played during
  the cycle against the weeks before it. Sparring games are excluded as
  everywhere else: the bot steers into the pillar being trained, so counting
  them would mark the trainer's own homework.

Both are reported with their sample sizes, and a change is only called a change
when the samples can carry it.
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from coach.profile.weakness_model import EVIDENCE_SOURCES_EXCLUDED
from coach.puzzles.srs import ensure_card, puzzle_rating
from coach.store.db import FocusCycle, Game, Move, Puzzle, session_factory

CYCLE_DAYS = 14
TEST_SIZE = 8              # positions per test
MIN_TEST_SIZE = 5          # fewer than this and the test is skipped
BASELINE_DAYS = 42         # games before the cycle that set the baseline
MIN_GAME_DECISIONS = 40    # decisions in the pillar before a game-based change is reported
TEST_SOURCES = ("own-game", "library")
TEST_MINUTES = 6
# An end-of-cycle test still owed this long after the cycle ends is waived, so a
# skipped test cannot hold the next cycle back forever.
POST_TEST_GRACE_DAYS = 3

PILLAR_LABEL = {
    "opening": "Openings", "tactics": "Tactics", "positional": "Positional understanding",
    "endgame": "Endgames", "quiet": "Finding moves in quiet positions",
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _j(raw: str | None, default):
    try:
        val = json.loads(raw) if raw else default
    except ValueError:
        return default
    return val if isinstance(val, type(default)) else default


def active(session: Session, account: str | None = None) -> FocusCycle | None:
    q = select(FocusCycle).where(FocusCycle.finished_at.is_(None))
    if account:
        q = q.where(FocusCycle.account == account)
    return session.scalar(q.order_by(FocusCycle.id.desc()))


# ---------------------------------------------------------------------------
# Tests

def _test_sets(session: Session, pillar: str) -> tuple[list[int], list[int]]:
    """Two rating-matched sets of unseen positions from the pillar."""
    from coach.store.db import SrsCard

    q = (select(Puzzle).outerjoin(SrsCard, SrsCard.puzzle_id == Puzzle.id)
         .where(Puzzle.pillar == pillar, Puzzle.source.in_(TEST_SOURCES),
                Puzzle.kind != "line", or_(SrsCard.id.is_(None), SrsCard.reps == 0)))
    pool = list(session.scalars(q))
    if len(pool) < 2 * MIN_TEST_SIZE:
        return [], []
    target = puzzle_rating(session) - 120     # a test should be passable, not a wall
    pool.sort(key=lambda p: abs((p.rating_est or target) - target))
    pool = pool[: 2 * TEST_SIZE]
    pool.sort(key=lambda p: p.rating_est or target)
    # Deal alternately in rating order: the two forms end up matched in difficulty.
    a = [p.id for i, p in enumerate(pool) if i % 2 == 0]
    b = [p.id for i, p in enumerate(pool) if i % 2 == 1]
    return a, b


def _hold_back(session: Session, ids: list[int], until: datetime) -> None:
    """Keep the post-test positions out of the daily queue until they are needed."""
    for pid in ids:
        p = session.get(Puzzle, pid)
        if p is None:
            continue
        card = ensure_card(session, p)
        card.due_at = max(card.due_at or until, until)


# ---------------------------------------------------------------------------
# Game-based measurement

def _pillar_stats(session: Session, pillar: str, account: str | None,
                  start: datetime, end: datetime) -> dict:
    q = (
        select(Move.cp_loss, Move.error_bucket, Move.decision_w)
        .join(Game, Move.game_id == Game.id)
        .where(Move.is_user_move.is_(True), Move.pillar == pillar, Move.cp_loss.isnot(None),
               Game.analyzed.is_(True), Game.source.notin_(EVIDENCE_SOURCES_EXCLUDED),
               Game.played_at >= start, Game.played_at < end)
    )
    if account:
        q = q.where(Game.account == account)
    rows = session.execute(q).all()
    w = [(int(cp), b, 1.0 if dw is None else float(dw)) for cp, b, dw in rows]
    tot = sum(x for _, _, x in w)
    if tot <= 0:
        return {"decisions": 0.0, "moves": 0}
    return {
        "moves": len(w),
        "decisions": round(tot, 1),
        "avg_cp_loss": round(sum(cp * x for cp, _, x in w) / tot, 1),
        "blunder_rate": round(sum(x for _, b, x in w if b == "blunder") / tot, 3),
    }


# ---------------------------------------------------------------------------
# Lifecycle

def start(session: Session, pillar: str, account: str | None = None,
          now: datetime | None = None) -> FocusCycle:
    now = now or _now()
    ends = now + timedelta(days=CYCLE_DAYS)
    a, b = _test_sets(session, pillar)
    tests = {"pre": {"ids": a, "answers": {}}, "post": {"ids": b, "answers": {}}}
    if b:
        _hold_back(session, b, ends)
    baseline = _pillar_stats(session, pillar, account, now - timedelta(days=BASELINE_DAYS), now)
    cyc = FocusCycle(account=account, pillar=pillar, started_at=now, ends_at=ends,
                     tests=json.dumps(tests), baseline=json.dumps(baseline))
    session.add(cyc)
    session.flush()
    return cyc


def ensure(session: Session, ranked: list[str], account: str | None = None,
           now: datetime | None = None) -> FocusCycle | None:
    """The cycle in force, starting a fresh one on the top pillar when none is."""
    now = now or _now()
    cyc = active(session, account)
    if cyc is not None:
        return cyc
    if not ranked:
        return None
    # Don't run the same pillar three cycles in a row: if it hasn't moved in
    # four weeks, a different angle (the next weakness) is better than more of
    # the same, and it comes back on the following cycle if it is still worst.
    last = list(session.scalars(select(FocusCycle.pillar).where(FocusCycle.finished_at.isnot(None))
                                .order_by(FocusCycle.id.desc()).limit(2)))
    pillar = ranked[0]
    if len(last) == 2 and last[0] == last[1] == pillar and len(ranked) > 1:
        pillar = ranked[1]
    return start(session, pillar, account, now)


def day_of(cyc: FocusCycle, now: datetime | None = None) -> int:
    now = now or _now()
    return min(CYCLE_DAYS, max(1, (now - cyc.started_at).days + 1))


def pending_test(cyc: FocusCycle, now: datetime | None = None) -> str | None:
    """Which test the next session should open with, if any."""
    now = now or _now()
    tests = _j(cyc.tests, {})
    pre, post = tests.get("pre", {}), tests.get("post", {})
    # A pre-test only on the first days: one taken in week two measures nothing.
    if (len(pre.get("ids", [])) >= MIN_TEST_SIZE
            and len(pre.get("answers", {})) < len(pre["ids"]) and day_of(cyc, now) <= 3):
        return "pre"
    if now >= cyc.ends_at and len(post.get("ids", [])) >= MIN_TEST_SIZE \
            and len(post.get("answers", {})) < len(post["ids"]):
        return "post"
    return None


def test_block(cyc: FocusCycle, phase: str) -> dict:
    tests = _j(cyc.tests, {})
    t = tests.get(phase, {})
    left = [i for i in t.get("ids", []) if str(i) not in t.get("answers", {})]
    title = "Cycle test" if phase == "pre" else "End-of-cycle test"
    detail = (f"{len(left)} positions from {PILLAR_LABEL.get(cyc.pillar, cyc.pillar).lower()}, "
              + ("so the next two weeks have something to be measured against."
                 if phase == "pre" else "matched in difficulty to the first test - not the same "
                 "positions, so this measures the skill rather than the memory."))
    return {"kind": "test", "title": title, "minutes": TEST_MINUTES, "detail": detail,
            "pillar": cyc.pillar,
            "params": {"cycle_id": cyc.id, "phase": phase, "puzzle_ids": left}}


def answer(session: Session, cyc: FocusCycle, phase: str, puzzle_id: int, solved: bool,
           elapsed_ms: int | None) -> dict:
    tests = _j(cyc.tests, {})
    t = tests.get(phase)
    if t is None or puzzle_id not in t.get("ids", []):
        raise ValueError("that position is not in this test")
    t.setdefault("answers", {})[str(puzzle_id)] = {
        "solved": bool(solved), "ms": min(int(elapsed_ms or 0), 180_000)}
    cyc.tests = json.dumps(tests)
    return state(cyc)


def maybe_finish(session: Session, cyc: FocusCycle, now: datetime | None = None) -> dict | None:
    """Close the cycle once its two weeks are up and its post-test (if any) is done."""
    now = now or _now()
    if now < cyc.ends_at:
        return None
    if pending_test(cyc, now) == "post" and now < cyc.ends_at + timedelta(days=POST_TEST_GRACE_DAYS):
        return None
    report = build_report(session, cyc, now)
    cyc.report = json.dumps(report)
    cyc.finished_at = now
    return report


def _test_score(t: dict) -> dict | None:
    ans = t.get("answers", {})
    if len(ans) < MIN_TEST_SIZE:
        return None
    solved = sum(1 for a in ans.values() if a["solved"])
    ms = [a["ms"] for a in ans.values() if a["solved"] and a["ms"]]
    return {"n": len(ans), "solved": solved, "rate": round(solved / len(ans), 3),
            "median_s": round(statistics.median(ms) / 1000, 1) if ms else None}


def build_report(session: Session, cyc: FocusCycle, now: datetime | None = None) -> dict:
    now = now or _now()
    tests = _j(cyc.tests, {})
    pre, post = _test_score(tests.get("pre", {})), _test_score(tests.get("post", {}))
    base = _j(cyc.baseline, {})
    during = _pillar_stats(session, cyc.pillar, cyc.account, cyc.started_at, now)
    lines: list[str] = []
    if pre and post:
        d = round((post["rate"] - pre["rate"]) * 100)
        lines.append(f"Test: {pre['solved']}/{pre['n']} at the start, {post['solved']}/{post['n']} "
                     f"at the end ({'+' if d >= 0 else ''}{d} points).")
    games_ok = (base.get("decisions", 0) >= MIN_GAME_DECISIONS
                and during.get("decisions", 0) >= MIN_GAME_DECISIONS)
    if games_ok:
        dcp = round(during["avg_cp_loss"] - base["avg_cp_loss"])
        lines.append(
            f"Real games: {base['avg_cp_loss']:.0f} cp lost per decision before, "
            f"{during['avg_cp_loss']:.0f} during ({'+' if dcp > 0 else ''}{dcp}); blunders "
            f"{base['blunder_rate'] * 100:.0f}% then, {during['blunder_rate'] * 100:.0f}% now.")
    else:
        lines.append(
            f"Too few real games in this pillar to compare (needs {MIN_GAME_DECISIONS} decisions "
            f"either side; had {base.get('decisions', 0):.0f} before and "
            f"{during.get('decisions', 0):.0f} during). Play some games outside the trainer.")
    return {"pillar": cyc.pillar, "label": PILLAR_LABEL.get(cyc.pillar, cyc.pillar),
            "started": cyc.started_at.isoformat(), "ended": now.isoformat(),
            "pre_test": pre, "post_test": post, "baseline": base, "during": during,
            "games_comparable": games_ok, "summary": lines}


def state(cyc: FocusCycle, now: datetime | None = None) -> dict:
    now = now or _now()
    tests = _j(cyc.tests, {})
    return {
        "id": cyc.id, "pillar": cyc.pillar, "label": PILLAR_LABEL.get(cyc.pillar, cyc.pillar),
        "day": day_of(cyc, now), "days": CYCLE_DAYS,
        "started": cyc.started_at.isoformat(), "ends": cyc.ends_at.isoformat(),
        "pending_test": pending_test(cyc, now) if not cyc.finished_at else None,
        "tests": {k: {"size": len(v.get("ids", [])), "answered": len(v.get("answers", {})),
                      "score": _test_score(v)} for k, v in tests.items()},
        "finished": cyc.finished_at.isoformat() if cyc.finished_at else None,
        "report": _j(cyc.report, {}) if cyc.report else None,
    }


def history(account: str | None = None, db_path=None, limit: int = 6) -> list[dict]:
    with session_factory(db_path)() as session:
        q = select(FocusCycle).where(FocusCycle.finished_at.isnot(None))
        if account:
            q = q.where(FocusCycle.account == account)
        return [state(c) for c in session.scalars(q.order_by(FocusCycle.id.desc()).limit(limit))]
