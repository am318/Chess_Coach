"""Which time control trains which weakness.

The five pillars are not all trainable at the same speed, and this is the part of
the curriculum that a single "play a game" button gets wrong.

* **Tactics and openings** are pattern-recall problems. They want *many
  repetitions under mild pressure*: three short games beat one long one, because
  what's being trained is the speed of recognition, not the depth of
  calculation.
* **Quiet positions and positional play** are the opposite. There is no pattern
  to recall - the skill is generating candidate moves and choosing a plan, which
  simply cannot happen in 3 minutes. These need a clock long enough to think.
* **Endgames** need accuracy above all, and an increment: technique collapses
  when you are moving on the 2-second delay, and an endgame reached in a 3+2
  game teaches nothing except that you were flustered.

So the daily game block picks its time control from the pillar being trained,
subject to two corrections: if the profile shows the pillar is measurably worse
at one speed than another, train it at the speed where it actually hurts; and a
rotation keeps the week from becoming all-blitz or all-rapid.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TimeControl:
    key: str
    base_s: int
    inc_s: int
    speed: str            # bucket used by the weakness profile
    est_minutes: int      # realistic wall-clock for one game, both players
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or f"{self.base_s // 60}+{self.inc_s}"


TIME_CONTROLS: dict[str, TimeControl] = {
    "3+2":   TimeControl("3+2", 180, 2, "blitz", 8),
    "5+0":   TimeControl("5+0", 300, 0, "blitz", 11),
    "5+3":   TimeControl("5+3", 300, 3, "blitz", 13),
    "10+5":  TimeControl("10+5", 600, 5, "rapid", 23),
    "15+10": TimeControl("15+10", 900, 10, "rapid", 32),
}

# Preference order per pillar, best training value first. The planner walks the
# list and takes the first one that fits the block's minute budget.
PILLAR_TIME_CONTROLS: dict[str, list[str]] = {
    "tactics":    ["3+2", "5+3", "5+0"],
    "opening":    ["3+2", "5+3", "5+0"],
    "quiet":      ["15+10", "10+5", "5+3"],
    "positional": ["15+10", "10+5", "5+3"],
    "endgame":    ["10+5", "15+10", "5+3"],
}

_WHY: dict[str, str] = {
    "tactics": "short games, many of them - tactics are about recognition speed",
    "opening": "short games, many of them - you want the first ten moves automatic",
    "quiet": "a long clock: finding a plan in a quiet position needs thinking time",
    "positional": "a long clock: structure and piece placement reward slow play",
    "endgame": "a real increment, so technique isn't lost to the clock",
}


# How far a game block may run over its slice before a shorter control is used.
# Games don't divide evenly into a 25-minute budget, and leaving ten minutes of
# the hour unplanned is worse than running four minutes long.
OVERRUN_TOLERANCE = 0.20


@dataclass
class GamePlan:
    time_control: TimeControl
    games: int
    minutes: int
    why: str
    speed_note: str = ""
    alternatives: list[str] = field(default_factory=list)
    # Minutes one game is expected to take *for this player*. Falls back to the
    # table's generic figure when there is no clock history to measure.
    est_minutes: int = 0

    def __post_init__(self) -> None:
        if not self.est_minutes:
            self.est_minutes = self.time_control.est_minutes


# Bounds on the pace correction. A player who is measurably twice as fast as the
# table still gets a sane estimate, and a single freak long game can't inflate
# the block into one game an hour.
PACE_MIN, PACE_MAX = 0.45, 1.4
MIN_GAMES_FOR_PACE = 6


def observed_minutes(control: TimeControl, observed: dict[str, float] | None) -> int:
    """How long one game at ``control`` really takes this player, in minutes.

    ``observed`` maps a time-control key to the median wall-clock length of the
    user's own games at that control (see ``coach.session.plan``). Where there is
    no history for the control itself, the player's overall pace against the
    table - ``observed["_pace"]`` - scales the generic figure, because someone
    who plays 5+0 in six minutes is not going to take 23 over a 10+5.
    """
    if not observed:
        return control.est_minutes
    direct = observed.get(control.key)
    if direct:
        return max(2, round(direct))
    pace = observed.get("_pace")
    if pace:
        pace = min(PACE_MAX, max(PACE_MIN, pace))
        return max(2, round(control.est_minutes * pace))
    return control.est_minutes


# Buckets in the profile that name a real clock. The profile also carries a
# "sparring" bucket for games against the coach, which is not a time control at
# all: comparing a pillar's cost at blitz against its cost at sparring answers
# "am I worse against humans than against a bot pitched below me" - reliably yes
# - and then steers the session on that non-evidence. For a player whose whole
# online history is blitz, "blitz" and "sparring" were the only two buckets, so
# every day came back blitz and the slow pillars never got a clock to think on.
REAL_SPEEDS = ("bullet", "blitz", "rapid", "classical")


def _worst_speed_for_pillar(profile: dict | None, pillar: str) -> tuple[str | None, float]:
    """Speed bucket where this pillar costs the most, and how much worse it is
    than the same pillar at the user's other speeds (in avg cp-loss).

    Only real time controls count - see ``REAL_SPEEDS``.
    """
    if not profile:
        return None, 0.0
    speeds = profile.get("speeds") or {}
    scored: list[tuple[str, float]] = []
    for speed, entry in speeds.items():
        if speed not in REAL_SPEEDS:
            continue
        stats = (entry.get("pillars") or {}).get(pillar)
        if stats and stats.get("n", 0) >= 12:
            scored.append((speed, float(stats["avg_cp_loss"])))
    if len(scored) < 2:
        return (scored[0][0] if scored else None), 0.0
    scored.sort(key=lambda t: -t[1])
    return scored[0][0], scored[0][1] - scored[-1][1]


def plan_game_block(
    pillar: str | None,
    minutes: int,
    profile: dict | None = None,
    rotation: int = 0,
    observed: dict[str, float] | None = None,
) -> GamePlan:
    """Choose the time control and number of games for today's game block.

    ``observed`` is the user's own median game length per control, which is what
    decides how many games fit. The static table is a guess about chess in
    general; the clock data in their PGNs is a measurement of them.
    """
    pillar = pillar or "tactics"
    prefs = PILLAR_TIME_CONTROLS.get(pillar, ["5+3", "3+2", "10+5"])

    # If the profile can see this pillar at more than one speed and it is clearly
    # worse at one of them, train where it actually hurts.
    speed_note = ""
    worst_speed, gap = _worst_speed_for_pillar(profile, pillar)
    if worst_speed and gap >= 8.0:
        matching = [k for k in TIME_CONTROLS if TIME_CONTROLS[k].speed == worst_speed]
        if matching:
            prefs = sorted(matching, key=lambda k: -TIME_CONTROLS[k].est_minutes) + prefs
            speed_note = (
                f"your {pillar} moves cost about {gap:.0f}cp more at {worst_speed} "
                f"than at your other speeds, so today is {worst_speed}"
            )

    # Rotate the preference list so consecutive days don't repeat the same clock.
    if rotation and len(prefs) > 1:
        prefs = prefs[rotation % len(prefs):] + prefs[: rotation % len(prefs)]

    for key in prefs:
        tc = TIME_CONTROLS[key]
        est = observed_minutes(tc, observed)
        if est <= minutes * (1 + OVERRUN_TOLERANCE):
            # Round to the nearest whole number of games rather than flooring, so
            # a 25-minute budget at 13 minutes a game schedules two games (26 min)
            # instead of one and leaving 12 minutes of the hour unplanned.
            games = max(1, round(minutes / est))
            if games * est > minutes * (1 + OVERRUN_TOLERANCE):
                games = max(1, games - 1)
            return GamePlan(
                time_control=tc,
                games=games,
                minutes=games * est,
                why=_WHY.get(pillar, "a balanced clock"),
                speed_note=speed_note,
                alternatives=[k for k in prefs if k != key],
                est_minutes=est,
            )

    # Nothing fits the budget - take the shortest control and play one game.
    tc = min(TIME_CONTROLS.values(), key=lambda t: observed_minutes(t, observed))
    est = observed_minutes(tc, observed)
    return GamePlan(tc, 1, est, _WHY.get(pillar, "a balanced clock"), speed_note,
                    est_minutes=est)


def unplayed_speeds(profile: dict | None) -> list[str]:
    """Speed buckets the user has essentially never played.

    Someone whose whole history is blitz has no evidence about their play in
    positions that need thinking time - and, more to the point, no practice at
    it. Worth saying out loud rather than silently planning around.
    """
    if not profile:
        return []
    speeds = profile.get("speeds") or {}
    played = {s for s, e in speeds.items() if e.get("n", 0) >= 40}
    return [s for s in ("blitz", "rapid", "classical") if s not in played]
