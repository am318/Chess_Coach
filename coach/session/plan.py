"""Generate and run the daily guided hour.

One function reads the weakness profile and produces an ordered list of blocks;
everything else - the UI, the resume logic - just walks that list. Keeping the
curriculum in one testable place is the point: the app should never ask the user
what to work on, and the answer should never be smeared across the front end.

The shape of the hour:

    warm-up   due puzzles from your own games, auto-graded
    game      one or more games at a time control chosen for today's weakness
    review    the worst moments of what you just played, handed back as puzzles
    drill     a *second*, different weakness, so the hour covers two
    close     one sentence to remember, and tomorrow's focus

Two weaknesses per session is deliberate. The game block trains the top-ranked
one at the time control that suits it; the drill block takes the next-ranked one
that the game block cannot reach - typically the slow-thinking pillar when the
game was blitz, or tactics when the game was long.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from coach.profile.weakness_model import latest_profile
from coach.session.timecontrol import (
    TIME_CONTROLS,
    GamePlan,
    plan_game_block,
    unplayed_speeds,
)
from coach.store.db import Game, Move, TrainingSession, get_setting, session_factory, set_setting

DEFAULT_MINUTES = 60
# A guided hour can be shortened or stretched, within reason. Outside this the
# input is a typo or a stale client: 0 and -60 both produced a plan of 2-minute
# blocks, and 100000 was accepted whole.
MIN_SESSION_MINUTES, MAX_SESSION_MINUTES = 15, 180
_ROTATION_KEY = "session_rotation"

# One session in four is a "slow day" so the week actually spans time controls.
SLOW_DAY_EVERY = 4
_SLOW_PILLARS = ("quiet", "positional", "endgame")

# Minute split for a 60-minute session, scaled proportionally for other lengths.
_SHAPE = {"warmup": 8, "game": 25, "review": 10, "drill": 12, "close": 5}

# Which drill suits which pillar when it can't be trained by playing a game.
_DRILL_FOR = {
    "opening": ("repertoire",
                ("Your own lines against the replies you actually face, "
                 "and the openings you are studying"),
                ["repertoire", "opening"]),
    "endgame": ("endgame", "Named technique positions: convert the win, or hold the draw",
                ["endgame"]),
    "tactics": ("puzzles", "Motif-focused puzzles from your own blunders", ["own-game"]),
    "quiet": ("puzzles", "Quiet positions from your own games - find the plan",
              ["own-game"]),
    "positional": ("puzzles", "Positional decisions from your own games", ["own-game"]),
}


@dataclass
class Block:
    kind: str            # warmup | game | review | drill | close
    title: str
    minutes: int
    detail: str
    pillar: str | None = None
    params: dict = field(default_factory=dict)


# A block may grow to this multiple of its planned length when an earlier block
# hands its unspent minutes on. Without a ceiling, skipping the warm-up in ten
# seconds would triple every block after it.
REBUDGET_CAP = 2.5
MIN_BLOCK_MINUTES = 3
# Enough games at a control before its measured length is trusted over the table.
MIN_GAMES_FOR_PACE = 6


def _tc_key(raw: str | None) -> str | None:
    """PGN ``TimeControl`` ("300", "180+2") as one of our own keys ("5+0")."""
    if not raw:
        return None
    head, _, inc = raw.partition("+")
    try:
        base_s, inc_s = int(head), int(inc or 0)
    except ValueError:
        return None
    return f"{base_s // 60}+{inc_s}"


def _observed_game_minutes(account: str | None = None) -> dict[str, float]:
    """Median real length of the user's own games, per time control.

    The clock comments already stored on every move answer "how long does a game
    actually take *you*" far better than a table of typical durations can. This
    player finishes a 5+0 in six and a half minutes rather than eleven, so a
    block budgeted from the table schedules two games and then has nothing to do
    with the twenty minutes it has left.

    The extra ``_pace`` key is the median ratio of measured to tabulated length
    across every control with data, so a control they have never played still
    gets an estimate that reflects how fast they actually play.

    Sparring games are deliberately *included* here, unlike everywhere the
    profile measures the player. This is not a judgement about how well anyone
    plays; it is "how long does a game take", and the games this number sizes a
    block for are themselves sparring games. It is the one place where the
    coach's own output is the most relevant evidence there is.
    """
    Session = session_factory()
    with Session() as s:
        totals = (
            select(Move.game_id.label("gid"), func.sum(Move.seconds_spent).label("secs"))
            .where(Move.seconds_spent.isnot(None))
            .group_by(Move.game_id)
            .subquery()
        )
        q = select(Game.time_control, totals.c.secs).join(totals, totals.c.gid == Game.id)
        if account:
            q = q.where(Game.account == account)
        rows = s.execute(q).all()

    per_control: dict[str, list[float]] = {}
    ratios: list[float] = []
    for raw, secs in rows:
        key = _tc_key(raw)
        if not key or not secs or secs <= 0:
            continue
        minutes = secs / 60.0
        per_control.setdefault(key, []).append(minutes)
        tc = TIME_CONTROLS.get(key)
        if tc:
            ratios.append(minutes / tc.est_minutes)

    out: dict[str, float] = {}
    for key, vals in per_control.items():
        if len(vals) >= MIN_GAMES_FOR_PACE:
            out[key] = statistics.median(vals)
    if len(ratios) >= MIN_GAMES_FOR_PACE:
        out["_pace"] = statistics.median(ratios)
    return out


# Fallback when there is no solve history yet, and the bounds the measurement is
# trusted within. A whole-library median of a few seconds or several minutes is
# a broken clock, not a fast or slow solver.
DEFAULT_SOLVE_SECONDS = 35.0
SOLVE_SECONDS_MIN, SOLVE_SECONDS_MAX = 8.0, 120.0
MIN_SOLVES_FOR_PACE = 8


def _observed_solve_seconds() -> float:
    """Median time this player actually takes over a puzzle.

    The block counts were ``minutes * 4`` - fifteen seconds a puzzle - against a
    measured median nearer thirty-five, so an eight-minute warm-up displayed
    "1/32" and ended, correctly, on its clock after about a dozen. The count is
    only a backstop against a run of one-movers, but it is also the number on
    screen, and a target nobody can reach is worse than no target.
    """
    from coach.store.db import SrsCard

    Session = session_factory()
    with Session() as s:
        vals = [
            float(v) / 1000.0
            for (v,) in s.execute(select(SrsCard.avg_ms).where(SrsCard.avg_ms.isnot(None)))
            if v
        ]
    if len(vals) < MIN_SOLVES_FOR_PACE:
        return DEFAULT_SOLVE_SECONDS
    return max(SOLVE_SECONDS_MIN, min(SOLVE_SECONDS_MAX, statistics.median(vals)))


def _puzzle_cap(minutes: int, solve_s: float) -> int:
    """How many puzzles could plausibly fit, with headroom for quick ones."""
    return max(5, round(minutes * 60 / solve_s * 1.5))


def _rotation() -> int:
    Session = session_factory()
    with Session() as s:
        return int(get_setting(s, _ROTATION_KEY, "0") or "0")


def _bump_rotation() -> None:
    Session = session_factory()
    with Session() as s:
        cur = int(get_setting(s, _ROTATION_KEY, "0") or "0")
        set_setting(s, _ROTATION_KEY, str(cur + 1))
        s.commit()


def _ranked_pillars(profile: dict | None) -> list[str]:
    if not profile:
        return ["tactics", "quiet", "endgame"]
    ranked = [w["pillar"] for w in profile.get("ranked_weaknesses", [])]
    return ranked or ["tactics", "quiet", "endgame"]


def _second_focus(ranked: list[str], primary: str, game_speed: str) -> str:
    """Pick the drill pillar: the highest-ranked weakness the game block did not
    already train. A blitz game can't train slow-thinking pillars and a long game
    gives few tactical reps, so prefer whichever the game left out."""
    slow = {"quiet", "positional", "endgame"}
    fast = {"tactics", "opening"}
    want = slow if game_speed in ("blitz", "bullet") else fast
    for p in ranked:
        if p != primary and p in want:
            return p
    for p in ranked:
        if p != primary:
            return p
    return "tactics"


def clamp_minutes(minutes: int | None) -> int:
    """A usable session length, whatever the caller sent."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_MINUTES
    return max(MIN_SESSION_MINUTES, min(MAX_SESSION_MINUTES, m))


def build_plan(account: str | None = None, minutes: int = DEFAULT_MINUTES) -> dict:
    """The whole curriculum decision for one session, as plain data."""
    minutes = clamp_minutes(minutes)
    profile = latest_profile(account) or latest_profile(None)
    scale = minutes / DEFAULT_MINUTES
    mins = {k: max(2, round(v * scale)) for k, v in _SHAPE.items()}

    ranked = _ranked_pillars(profile)
    rotation = _rotation()

    # Weekly rhythm. Left alone, a top-ranked fast pillar (tactics, openings)
    # would schedule blitz every single day, and the slow pillars would never get
    # a clock long enough to train them - which for a player with no rapid games
    # at all is exactly the wrong outcome. Every SLOW_DAY_EVERY-th session the
    # game block trains the slow weakness instead, and the drill picks up the
    # fast one.
    primary = ranked[0]
    slow_day = rotation % SLOW_DAY_EVERY == SLOW_DAY_EVERY - 1
    if slow_day:
        slow = next((p for p in ranked if p in _SLOW_PILLARS), None)
        if slow:
            primary = slow

    # On a slow day don't rotate the preference list - the whole point is to take
    # the longest control that fits, not to land back on blitz.
    game: GamePlan = plan_game_block(
        primary, mins["game"], profile, 0 if slow_day else rotation,
        observed=_observed_game_minutes(account),
    )
    # Games don't divide evenly into the slice. Settle up with the drill block so
    # the session still adds up to the hour the user asked for, in both
    # directions - a long game shortens the drill, a short one lengthens it.
    slack = mins["game"] - game.minutes
    mins["drill"] = max(4, mins["drill"] + slack)

    solve_s = _observed_solve_seconds()
    secondary = _second_focus(ranked, primary, game.time_control.speed)
    drill_kind, drill_detail, drill_sources = _DRILL_FOR.get(
        secondary, ("puzzles", "Mixed puzzles from your own games", ["own-game"])
    )

    blocks = [
        Block(
            kind="warmup",
            title="Warm-up",
            minutes=mins["warmup"],
            detail="Due puzzles from your own games. Graded automatically - just solve.",
            pillar=primary,
            # The block ends when its minutes are spent, not at a fixed count, so
            # this is a backstop against a run of one-movers rather than a target.
            # Sized from how long this player actually takes over a puzzle, with
            # half again for headroom, rather than from a guess.
            params={"target_pillar": primary,
                    "max_puzzles": _puzzle_cap(mins["warmup"], solve_s)},
        ),
        Block(
            kind="game",
            title=(f"{game.games} game{'s' if game.games > 1 else ''} at "
                   f"{game.time_control.name}"),
            minutes=game.minutes,
            detail=_game_detail(game),
            pillar=primary,
            params={
                "time_control": game.time_control.key,
                "base_s": game.time_control.base_s,
                "inc_s": game.time_control.inc_s,
                "games": game.games,
                # What one game is expected to take *this* player, which is what
                # decides whether the block has room for another.
                "est_minutes": game.est_minutes,
                "coached": True,
                "target_pillar": primary,
            },
        ),
        Block(
            kind="review",
            title="Review what you just played",
            minutes=mins["review"],
            detail="Your worst moments, handed back as positions to solve rather than read.",
            pillar=primary,
            params={"max_moments": 3, "as_puzzles": True},
        ),
        Block(
            kind="drill",
            title=f"Drill: {secondary}",
            minutes=mins["drill"],
            detail=drill_detail,
            pillar=secondary,
            # Both counts are backstops against a run of one-movers, not targets:
            # the block ends when its minutes are spent. The old "minutes // 2"
            # drill cap was the binding limit instead - five endgame drills ended
            # an eleven-minute block after four and a half.
            params={"drill": drill_kind, "target_pillar": secondary,
                    "sources": drill_sources,
                    "max_puzzles": _puzzle_cap(mins["drill"], solve_s),
                    # An endgame drill is played out over twenty moves, so it
                    # takes far longer than a puzzle - counted separately.
                    "max_drills": max(4, round(mins["drill"] / 3))},
        ),
        Block(
            kind="close",
            title="Close",
            minutes=mins["close"],
            detail="One thing to remember, and tomorrow's focus.",
            params={},
        ),
    ]

    return {
        "minutes": sum(b.minutes for b in blocks),
        "focus_pillar": primary,
        "secondary_pillar": secondary,
        "time_control": game.time_control.key,
        "speed": game.time_control.speed,
        "blocks": [asdict(b) for b in blocks],
        "notes": _notes(profile, primary, secondary, game),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _game_detail(game: GamePlan) -> str:
    """Why this clock. A speed override replaces the pillar's usual reason rather
    than following it, which used to read "a long clock: ... so today is blitz"."""
    note = game.speed_note
    return note[:1].upper() + note[1:] if note else game.why


def _notes(profile: dict | None, primary: str, secondary: str, game: GamePlan) -> list[str]:
    notes: list[str] = []
    # Said first, because it qualifies everything after it. A plan built on a
    # library whose newest game is months old is still the best plan available,
    # but the app should not present it as a reading of how you are playing now.
    fresh = (profile or {}).get("freshness") or {}
    if fresh.get("stale") and fresh.get("age_days"):
        notes.append(
            f"Heads up: your newest real game is {fresh['age_days']} days old, so "
            "today's focus is read from older games. Run a sync if you have played since."
        )
    if profile and profile.get("ranked_weaknesses"):
        top = profile["ranked_weaknesses"][0]
        notes.append(
            f"Biggest leak right now: {top['label'].lower()} "
            f"(avg -{top['avg_cp_loss']}cp, blunders {top['blunder_rate'] * 100:.0f}%)."
        )
    # Time allocation is not a pillar and cannot be drilled, but it is often the
    # cheapest thing a player can change today, so the session says it out loud.
    ta = (profile or {}).get("time_allocation") or {}
    ratio = ta.get("allocation_ratio")
    if ratio is not None and ratio < 0.95 and ta.get("rushed_critical_rate", 0) >= 0.4:
        notes.append(
            f"You give the positions that decide the game {(1 - ratio) * 100:.0f}% less "
            f"time than the quiet ones, and {ta['rushed_critical_rate'] * 100:.0f}% of them "
            f"go by in under {ta['rushed_seconds']:.0f}s. Today: when something is en "
            "prise or a check is available, stop before you move."
        )

    missing = unplayed_speeds(profile)
    if "rapid" in missing and game.time_control.speed == "rapid":
        notes.append(
            "You have essentially no rapid games on record. Slow pillars can't be "
            "trained at blitz, so today's game is deliberately longer than you're used to."
        )
    elif missing:
        notes.append("Little or no history at: " + ", ".join(missing) + ".")
    notes.append(f"Second weakness covered by the drill block: {secondary}.")
    return notes


# --------------------------------------------------------------------- runtime


def _now() -> datetime:
    """Naive UTC, matching what SQLite hands back on read."""
    return datetime.now(UTC).replace(tzinfo=None)


def _naive(value: datetime | None) -> datetime | None:
    """A freshly-committed row still holds the aware datetime it was written
    with, while a re-read row is naive. Normalise so arithmetic never mixes the
    two."""
    if value is not None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _today_window() -> tuple[datetime, datetime]:
    """Today, in the user's own time zone, expressed in the naive UTC the rows use.

    It used to take midnight UTC, which is not midnight anywhere the user is. On
    UTC+1 that put the boundary at 01:00 local: a session begun at 00:30 was
    filed under the previous day and then, half an hour into it, stopped being
    "today" - the app offered a fresh plan and the half-finished session was
    orphaned. Anchoring on the local date and converting back is the same two
    lines and is right at every offset.
    """
    local = datetime.now().astimezone()
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_local.astimezone(UTC).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def current_session(account: str | None = None) -> TrainingSession | None:
    """Today's unfinished session for this account, if there is one.

    ``account`` used to be accepted and then ignored, so two configured players
    shared one daily session - the second to open the app resumed the first
    one's hour. Under unified identity the argument is None and every session is
    stored with a None account, so the filter is a no-op there.
    """
    start, end = _today_window()
    Session = session_factory()
    with Session() as s:
        q = s.query(TrainingSession).filter(
            TrainingSession.finished_at.is_(None),
            TrainingSession.started_at >= start,
            TrainingSession.started_at < end,
        )
        q = q.filter(
            TrainingSession.account == account if account
            else TrainingSession.account.is_(None)
        )
        return q.order_by(TrainingSession.started_at.desc()).first()


def start_session(account: str | None = None, minutes: int = DEFAULT_MINUTES) -> dict:
    """Resume today's session if one is open, otherwise plan and start a new one."""
    existing = current_session(account)
    if existing is not None:
        return as_dict(existing)

    plan = build_plan(account, clamp_minutes(minutes))
    _bump_rotation()
    Session = session_factory()
    with Session() as s:
        ts = TrainingSession(
            account=account,
            plan=json.dumps(plan),
            focus_pillar=plan["focus_pillar"],
            speed=plan["speed"],
            progress="{}",
            block_started_at=_now(),
        )
        s.add(ts)
        s.commit()
        return as_dict(ts)


def as_dict(ts: TrainingSession) -> dict:
    plan = json.loads(ts.plan)
    progress = json.loads(ts.progress or "{}")
    blocks = plan["blocks"]
    idx = min(ts.block_index, len(blocks) - 1)
    block = blocks[idx] if ts.block_index < len(blocks) else None

    # The block budget is served as seconds *remaining*, computed here, so the
    # browser never has to reason about when the block started - it just counts
    # down from whatever it was told, and a reload asks again.
    now = _now()
    started = _naive(ts.started_at) or now
    block_started = _naive(ts.block_started_at) or started
    block_elapsed = max(0.0, (now - block_started).total_seconds())
    block_budget = (block["minutes"] * 60) if block else 0
    return {
        "id": ts.id,
        "plan": plan,
        "block_index": ts.block_index,
        "block": block,
        "done": ts.block_index >= len(blocks) or ts.finished_at is not None,
        "progress": progress,
        "elapsed_s": ts.elapsed_s,
        "started_at": ts.started_at.isoformat() if ts.started_at else None,
        "finished_at": ts.finished_at.isoformat() if ts.finished_at else None,
        "block_started_at": block_started.isoformat(),
        "block_elapsed_s": round(block_elapsed),
        "block_budget_s": block_budget,
        "block_seconds_left": round(block_budget - block_elapsed),
        "session_elapsed_s": round(max(0.0, (now - started).total_seconds())),
        "session_budget_s": plan["minutes"] * 60,
    }


def rebudget(plan: dict, left_index: int, spent_s: int) -> bool:
    """Hand a finished block's unspent minutes to the blocks still to come.

    A block that ends early used to take the rest of the hour down with it: two
    games at a clock they finish in six minutes vacated a twenty-six-minute slot,
    and the session simply ended forty minutes short of what it promised. The
    hour is what the user asked for, so the surplus (or the overrun, which
    shortens what follows) is spread across the remaining blocks in proportion to
    their size.

    ``close`` is left out - it is a summary screen, not training - and no block
    grows past ``REBUDGET_CAP`` times its planned length, so skipping the warm-up
    in ten seconds cannot triple everything after it. Returns whether anything
    moved.
    """
    blocks = plan.get("blocks") or []
    if left_index >= len(blocks):
        return False
    planned = plan.setdefault("planned_minutes", [b["minutes"] for b in blocks])
    surplus_min = (blocks[left_index]["minutes"] * 60 - spent_s) / 60.0
    remaining = [
        i for i in range(left_index + 1, len(blocks)) if blocks[i]["kind"] != "close"
    ]
    if not remaining or abs(surplus_min) < 1:
        return False

    total = sum(blocks[i]["minutes"] for i in remaining) or len(remaining)
    moved = False
    for i in remaining:
        share = surplus_min * (blocks[i]["minutes"] / total)
        ceiling = max(MIN_BLOCK_MINUTES, round(planned[i] * REBUDGET_CAP))
        new_min = int(min(ceiling, max(MIN_BLOCK_MINUTES, round(blocks[i]["minutes"] + share))))
        if new_min != blocks[i]["minutes"]:
            blocks[i]["minutes"] = new_min
            moved = True
    if moved:
        plan["rebudgeted"] = True
    return moved


def advance(session_id: int, record: dict | None = None) -> dict:
    """Mark the current block done and move to the next one."""
    Session = session_factory()
    with Session() as s:
        ts = s.get(TrainingSession, session_id)
        if ts is None:
            return {"error": "session not found"}
        plan = json.loads(ts.plan)
        progress = json.loads(ts.progress or "{}")
        # Charge the time actually spent to the block being left, so the closing
        # summary reflects the hour as it happened rather than as it was planned.
        now = _now()
        spent = round(max(0.0, (now - (_naive(ts.block_started_at) or now)).total_seconds()))
        key = str(ts.block_index)
        progress[key] = {**progress.get(key, {}), **(record or {}), "seconds": spent}
        ts.progress = json.dumps(progress)
        ts.elapsed_s = (ts.elapsed_s or 0) + spent
        if rebudget(plan, ts.block_index, spent):
            ts.plan = json.dumps(plan)
        ts.block_index += 1
        ts.block_started_at = now
        if ts.block_index >= len(plan["blocks"]):
            ts.finished_at = now
        s.commit()
        return as_dict(ts)


def note_progress(session_id: int, record: dict) -> dict:
    """Merge partial progress into the current block without advancing.

    Deliberately does not touch ``elapsed_s``: the session clock is derived from
    ``block_started_at`` on the server and settled up in ``advance``, so a client
    reporting its own idea of elapsed time can no longer overwrite it.
    """
    Session = session_factory()
    with Session() as s:
        ts = s.get(TrainingSession, session_id)
        if ts is None:
            return {"error": "session not found"}
        progress = json.loads(ts.progress or "{}")
        key = str(ts.block_index)
        progress[key] = {**progress.get(key, {}), **record}
        ts.progress = json.dumps(progress)
        s.commit()
        return as_dict(ts)


def finish(session_id: int) -> dict:
    Session = session_factory()
    with Session() as s:
        ts = s.get(TrainingSession, session_id)
        if ts is None:
            return {"error": "session not found"}
        ts.finished_at = _now()
        s.commit()
        return as_dict(ts)
