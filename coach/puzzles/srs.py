"""Spaced-repetition scheduling for puzzle cards.

A compact FSRS-flavoured model: each card carries a *stability* S (days until
retrievability decays to ~90%) and a *difficulty* D in [1, 10]. This is a
deliberate approximation of FSRS-6 - accurate enough to order a review queue,
and isolated here so it can be swapped for the real `fsrs` package later without
touching callers.

Grades (matching the UI): 1 = again (failed), 2 = hard, 3 = good, 4 = easy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from coach.store.db import Puzzle, SrsCard, get_setting, set_setting

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4

_TARGET_RETRIEVABILITY = 0.9
_MIN_STABILITY = 0.2
_MAX_INTERVAL_DAYS = 365

# Difficulty targeting, in two clearly separated halves.
#
#   * `puzzle_rating` MEASURES ability. It is a plain Elo number and moves only
#     on evidence: solved or not, against the puzzle's own rating.
#   * `selection_rating` CHOOSES difficulty. It sits a fixed distance below the
#     ability estimate - exactly the distance at which Elo predicts a
#     TARGET_SUCCESS chance of solving.
#
# These used to be the same number pulling in two directions: `due_queue` picked
# puzzles at the user's own rating (a coin flip by construction) while the rating
# update subtracted a constant on every single review to drag the queue back
# down. Two knobs fighting, and the constant also fired when a puzzle had no
# rating at all. Split apart, TARGET_SUCCESS is a real dial: change it and the
# queue moves, with the ability estimate left alone.
TARGET_SUCCESS = 0.80
_RATING_KEY = "puzzle_rating"
_DEFAULT_RATING = 1400
_RATING_K = 24
# Rating bounds. The floor used to be 600, which is above where a genuine
# beginner sits: a ~480-rated player's estimate pinned there and could not fall
# further, while the puzzles mined from their own blunders pinned on the miner's
# own 400 floor. Neither number could move, so difficulty targeting did nothing
# at all - 96% of one real library sat on a single rating.
RATING_MIN, RATING_MAX = 200, 2800
# What the floor used to be. `repair.rerate_puzzles` uses it to spot an estimate
# that is sitting on the old clamp rather than on evidence.
_OLD_RATING_FLOOR = 600
# How many cards the queue will introduce for the first time in a day. Mining a
# few hundred games produces a puzzle per mistake, and every one of them is born
# due: one real library had 1,257 cards due at once, 1,218 of which had never
# been seen. A backlog like that is not a schedule, it is a wall - and spaced
# repetition only works if the reviews you have earned come back on time, which
# they cannot when they are queued behind a thousand strangers. Cards already in
# circulation are never held back by this.
NEW_CARDS_PER_DAY = 15
# Only consider puzzles within this far of the selection target, in SQL, before
# the Python re-rank. Wide enough that the queue never starves.
SELECTION_BAND = 350
# Solving inside this fraction of your usual time for the card counts as "easy";
# taking longer than this multiple of it counts as "hard".
_FAST_RATIO = 0.5
_SLOW_RATIO = 1.8
_FAST_MS = 8_000     # a first-ever solve under this is easy regardless of history
# `elapsed_ms` is wall-clock since the position appeared, so a tab left open all
# afternoon arrives as a solve time. One real card carried 4,599s - seventy-seven
# minutes - and `avg_ms` is the yardstick `grade_from_outcome` measures the next
# attempt against, so a single reading like that mis-grades the card for months.
# Anything past this is a person who walked away, not a person thinking.
MAX_CREDIBLE_MS = 180_000


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class ReviewOutcome:
    stability: float
    difficulty: float
    interval_days: float
    due_at: datetime
    lapsed: bool


def _initial_stability(grade: int) -> float:
    return {AGAIN: 0.4, HARD: 1.0, GOOD: 3.0, EASY: 8.0}[grade]


def _initial_difficulty(grade: int) -> float:
    return min(10.0, max(1.0, 6.0 - (grade - 3) * 1.4))


def _next_stability(s: float, d: float, grade: int, elapsed_days: float) -> float:
    if grade == AGAIN:
        return max(_MIN_STABILITY, s * 0.35)
    # Retrievability at review time; success after a longer gap grows S more.
    r = math.exp(-elapsed_days / max(s, 1e-3)) if s > 0 else 1.0
    grade_bonus = {HARD: 0.9, GOOD: 1.25, EASY: 1.7}[grade]
    ease = (11.0 - d) / 10.0
    growth = 1.0 + grade_bonus * ease * (1.0 - r)
    return min(_MAX_INTERVAL_DAYS, max(_MIN_STABILITY, s * growth))


def _next_difficulty(d: float, grade: int) -> float:
    d = d - 0.8 * (grade - 3)          # good keeps it, easy lowers, again/hard raise
    d = d + 0.05 * (5.0 - d)           # mean-reversion toward 5
    return min(10.0, max(1.0, d))


def review(card: SrsCard, grade: int, now: datetime | None = None,
           elapsed_ms: int | None = None) -> ReviewOutcome:
    now = now or _now()
    last = card.last_review or card.due_at or now
    elapsed_days = max(0.0, (now - last).total_seconds() / 86400.0)

    if not card.reps:
        s = _initial_stability(grade)
        d = _initial_difficulty(grade)
    else:
        s = _next_stability(card.stability or 1.0, card.difficulty or 5.0, grade, elapsed_days)
        d = _next_difficulty(card.difficulty or 5.0, grade)

    # Interval == stability (S is defined as the ~90%-retrievability horizon).
    interval = max(10 / 1440, min(_MAX_INTERVAL_DAYS, s))  # >= 10 min
    if grade == AGAIN:
        interval = 8 / 1440  # 8 minutes, back into the session soon

    card.stability = s
    card.difficulty = d
    card.reps = (card.reps or 0) + 1
    if grade == AGAIN:
        card.lapses = (card.lapses or 0) + 1
    card.last_grade = grade
    card.last_review = now
    card.due_at = now + timedelta(days=interval)
    if elapsed_ms is not None and 0 < elapsed_ms <= MAX_CREDIBLE_MS:
        card.last_ms = int(elapsed_ms)
        # Only *successful* solves move the running pace. `avg_ms` exists to
        # answer "was that fast for you on this card", and a failure is not a
        # solve time - it is however long you stared before giving up, which is
        # usually the longest reading the card will ever see. Folded in, it
        # raised the baseline so the next genuine solve looked quick: a card
        # failed after 40s and then solved in 15s was graded *easy*.
        if grade != AGAIN:
            prior = card.avg_ms or float(elapsed_ms)
            card.avg_ms = round(0.7 * prior + 0.3 * elapsed_ms, 1)
    if grade != AGAIN:
        card.wins = (card.wins or 0) + 1

    return ReviewOutcome(s, d, interval, card.due_at, grade == AGAIN)


def grade_from_outcome(card: SrsCard, solved: bool, elapsed_ms: int | None) -> int:
    """Derive an SRS grade instead of asking the user to introspect.

    Whether the move was found, and how long it took relative to this player's
    own pace on this card, carries the information the four buttons were asking
    for - and asking forty times an hour is forty decisions the app can make
    itself.
    """
    if not solved:
        return AGAIN
    if elapsed_ms is None or elapsed_ms > MAX_CREDIBLE_MS:
        return GOOD
    baseline = card.avg_ms or 0.0
    if baseline <= 0:
        return EASY if elapsed_ms <= _FAST_MS else GOOD
    if elapsed_ms <= baseline * _FAST_RATIO:
        return EASY
    if elapsed_ms >= baseline * _SLOW_RATIO:
        return HARD
    return GOOD


def puzzle_rating(session: Session) -> int:
    """The user's current puzzle rating.

    Seeded from their real rating in their game headers the first time it's
    asked for - starting everyone at a fixed 1400 hands a 480-rated player a
    queue of puzzles far above their level, which is the opposite of the
    ``TARGET_SUCCESS`` the queue is trying to hit.
    """
    stored = get_setting(session, _RATING_KEY)
    if stored:
        return int(float(stored))
    seed = _DEFAULT_RATING
    try:
        from coach.profile.rating import estimate_rating

        seed = estimate_rating(None) or _DEFAULT_RATING
    except Exception:  # noqa: BLE001, S110 - fall back to the default seed
        pass
    set_setting(session, _RATING_KEY, str(int(seed)))
    return int(seed)


def selection_rating(session: Session, target_success: float = TARGET_SUCCESS) -> int:
    """The rating to *pick* puzzles at, given the rating the user *is*.

    Elo's own formula, run backwards: the gap at which a player is expected to
    score ``target_success``. At 0.80 that is about 241 points below their
    ability, which is where a training queue should sit.
    """
    q = min(0.99, max(0.01, target_success))
    offset = 400.0 * math.log10(q / (1.0 - q))
    return round(puzzle_rating(session) - offset)


def update_puzzle_rating(session: Session, puzzle: Puzzle, solved: bool) -> int:
    """Plain Elo against the puzzle's own rating.

    No target baked in: this number's only job is to estimate ability, and
    ``selection_rating`` turns it into a difficulty. A puzzle with no rating of
    its own is treated as an even match, so an unrated solve moves nothing much
    in either direction instead of dragging the estimate down.
    """
    cur = puzzle_rating(session)
    opponent = puzzle.rating_est or cur
    expected = 1.0 / (1.0 + 10 ** ((opponent - cur) / 400.0))
    new = cur + _RATING_K * ((1.0 if solved else 0.0) - expected)
    new = max(RATING_MIN, min(RATING_MAX, new))
    set_setting(session, _RATING_KEY, f"{new:.0f}")
    return int(new)


def ensure_card(session: Session, puzzle: Puzzle) -> SrsCard:
    if puzzle.srs is not None:
        return puzzle.srs
    card = SrsCard(puzzle_id=puzzle.id, due_at=_now())
    session.add(card)
    session.flush()
    return card


def due_queue(
    session: Session,
    limit: int = 20,
    pillar_weights: dict[str, float] | None = None,
    now: datetime | None = None,
    target_rating: int | None = None,
    speed: str | None = None,
    sources: list[str] | None = None,
    candidate_pool: int = 300,
) -> list[Puzzle]:
    """Cards due now, ordered by how much training value they carry today.

    Ordering blends four things: how badly the user needs that pillar, how close
    the puzzle is to their current puzzle rating (so the session lands near
    ``TARGET_SUCCESS`` rather than at random difficulty), how overdue the card
    is, and whether it has ever been seen.

    Only a bounded candidate window is pulled from SQL. The difficulty band is
    applied in SQL too, which matters more than it looks: the window is the
    *most overdue* rows, so once the backlog exceeds ``candidate_pool`` every
    later re-rank happens inside the same set of oldest cards and the rating fit
    stops doing anything at all. The queue silently degrades to "oldest first"
    exactly when the library gets big enough for difficulty targeting to matter.
    """
    now = now or _now()
    pillar_weights = pillar_weights or {}
    if target_rating is None:
        target_rating = selection_rating(session)
    room_for_new = new_cards_left(session, now)

    def _candidates(band: int | None, use_speed: bool) -> list:
        q = (
            select(Puzzle, SrsCard)
            .join(SrsCard, SrsCard.puzzle_id == Puzzle.id)
            .where(SrsCard.due_at <= now)
        )
        if room_for_new <= 0:
            # Today's intake is spent: serve only cards already in circulation.
            q = q.where(SrsCard.reps > 0)
        if sources:
            q = q.where(Puzzle.source.in_(sources))
        if use_speed and speed:
            q = q.where(Puzzle.speed == speed)
        if band is not None:
            # Unrated puzzles (drill sets, some library rows) stay eligible -
            # they are not "far from the target", they are unmeasured.
            q = q.where(
                or_(
                    Puzzle.rating_est.is_(None),
                    Puzzle.rating_est.between(target_rating - band, target_rating + band),
                )
            )
        return session.execute(q.order_by(SrsCard.due_at).limit(candidate_pool)).all()

    # Widen only as far as needed: right difficulty first, then any difficulty,
    # then any time control.
    rows = _candidates(SELECTION_BAND, True)
    if not rows:
        rows = _candidates(None, True)
    if not rows and speed:
        rows = _candidates(None, False)
    if not rows and room_for_new <= 0:
        # Nothing in circulation is due. An empty block is worse than exceeding
        # the intake, so the cap yields rather than ending the session early.
        room_for_new = limit
        rows = _candidates(None, bool(speed)) or _candidates(None, False)

    def score(row) -> float:
        puzzle, card = row
        overdue_days = (now - card.due_at).total_seconds() / 86400.0
        pillar_w = pillar_weights.get(puzzle.pillar, 1.0)
        # Rating proximity: full credit at the target, decaying over ~250cp.
        gap = abs((puzzle.rating_est or target_rating) - target_rating)
        fit = 1.0 / (1.0 + (gap / 250.0) ** 2)
        freshness = 1.25 if card.reps == 0 else 1.0
        urgency = 1.0 + min(overdue_days, 30.0) / 30.0
        return pillar_w * fit * freshness * urgency

    rows.sort(key=score, reverse=True)
    # Trim the unseen ones down to whatever is left of today's intake, keeping
    # the ordering the score produced.
    out: list[Puzzle] = []
    new_taken = 0
    for puzzle, card in rows:
        if card.reps == 0:
            if new_taken >= room_for_new:
                continue
            new_taken += 1
        out.append(puzzle)
        if len(out) >= limit:
            break
    return out


def new_cards_left(session: Session, now: datetime | None = None) -> int:
    """How many first-time cards today's intake still allows.

    Counted from the cards whose *first* review happened today rather than from
    a stored counter, so it cannot drift out of step with what actually
    happened - and it resets on its own at midnight.
    """
    now = now or _now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seen_today = session.scalar(
        select(func.count())
        .select_from(SrsCard)
        .where(SrsCard.reps == 1, SrsCard.last_review >= day_start)
    ) or 0
    return max(0, NEW_CARDS_PER_DAY - int(seen_today))


def due_count(session: Session, now: datetime | None = None,
              speed: str | None = None, sources: list[str] | None = None,
              pillar: str | None = None) -> int:
    """How many cards are due, under the same filters the queue was given.

    Unfiltered, this reported the whole backlog: an eight-minute warm-up said
    "1,257 due", and the endgame drill block - which has eleven positions in
    existence - said the same. The number is meant to tell you how much is left
    in front of you, so it has to be counted over the same set.
    """
    now = now or _now()
    q = (
        select(func.count())
        .select_from(SrsCard)
        .join(Puzzle, Puzzle.id == SrsCard.puzzle_id)
        .where(SrsCard.due_at <= now)
    )
    if sources:
        q = q.where(Puzzle.source.in_(sources))
    if speed:
        q = q.where(Puzzle.speed == speed)
    if pillar:
        q = q.where(Puzzle.pillar == pillar)
    return int(session.scalar(q) or 0)
