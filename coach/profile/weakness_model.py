"""Aggregate analysed moves into a ranked weakness profile.

Output is a plain dict (JSON-serialisable) consumed by the sparring opponent,
the puzzle scheduler, and the web dashboard. A snapshot is saved each run so
progress over time can be charted.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from coach.analysis.timing import (
    CONSIDERED_SECONDS,
    RUSHED_SECONDS,
    is_critical,
)
from coach.store.db import Game, Move, ProfileSnapshot, session_factory

PILLARS = ["opening", "tactics", "positional", "endgame", "quiet"]

# Games the coach dealt itself are training, not evidence. The sparring bot picks
# its moves to steer into whatever pillar currently ranks first, so counting those
# games as measurement closes a loop with nothing outside it: the pillar is ranked
# first, the bot attacks it, those games become the pillar's evidence, and it stays
# ranked first. With a 90-day half-life over a library whose newest real game is
# months old, 327 sparring moves - 1.8% of the library - were supplying 23% of the
# weighted profile and 37% of the tactics pillar, and excluding them collapsed
# tactics' lead over endgame from 34% to 0.8%. The same rule is already applied in
# `timecontrol.REAL_SPEEDS`, in `opening_picker._worst_opening_games` and in
# `repertoire._collect`, whose comment puts it best: the trainer must not mark its
# own homework. `progress.sparring_trend` is where sparring results belong.
EVIDENCE_SOURCES_EXCLUDED = ("sparring",)

# Moves decay with age so the profile describes the player you are now, not a
# career average. 90 days -> a game from three months ago counts half as much as
# today's, one from a year ago about a fourteenth.
HALF_LIFE_DAYS = 90.0
# Trend compares the most recent games against the ones before them, over a fixed
# window, so it reacts within a week of training rather than within a year.
TREND_WINDOW_GAMES = 30

# Site-specific speed names collapsed to four buckets we can reason about.
SPEED_BUCKETS = {
    "ultrabullet": "bullet", "bullet": "bullet",
    "blitz": "blitz",
    "rapid": "rapid",
    "classical": "classical", "daily": "classical", "correspondence": "classical",
    "sparring": "sparring",
}


def speed_bucket(speed: str | None) -> str:
    return SPEED_BUCKETS.get((speed or "").lower(), "other")

_PILLAR_LABEL = {
    "opening": "Openings",
    "tactics": "Tactics",
    "positional": "Positional understanding",
    "endgame": "Endgames",
    "quiet": "Finding moves in quiet positions",
}


@dataclass
class _Row:
    cp_loss: int
    bucket: str
    pillar: str
    motif: str | None
    endgame_type: str | None
    eco: str | None
    opening: str | None
    color: str
    time_trouble: bool
    context: str
    punish_miss: bool
    half: str    # "recent" | "older" - fixed-size recent window, not a median split
    speed: str   # bullet | blitz | rapid | classical | sparring | other
    weight: float  # recency weight, 1.0 for today decaying with HALF_LIFE_DAYS
    decision_w: float    # 0..1, how much choice the position offered
    critical: bool       # the position was worth spending clock on
    seconds: float | None  # wall-clock seconds spent on the move, if known


def _fetch(session: Session, account: str | None) -> list[_Row]:
    q = (
        select(Move, Game)
        .join(Game, Move.game_id == Game.id)
        .where(
            Move.is_user_move.is_(True),
            Game.analyzed.is_(True),
            Move.cp_loss.isnot(None),
            Game.source.notin_(EVIDENCE_SOURCES_EXCLUDED),
        )
    )
    if account:
        q = q.where(Game.account == account)
    pairs = session.execute(q.order_by(Game.played_at)).all()

    now = datetime.now(UTC).replace(tzinfo=None)
    # "recent" = the last TREND_WINDOW_GAMES distinct games, by game not by move,
    # so a long game can't swamp the window on its own.
    game_order: list[int] = []
    seen: set[int] = set()
    for _, g in pairs:
        if g.id not in seen:
            seen.add(g.id)
            game_order.append(g.id)
    recent_ids = set(game_order[-TREND_WINDOW_GAMES:])
    prior_ids = set(game_order[-2 * TREND_WINDOW_GAMES:-TREND_WINDOW_GAMES])

    rows: list[_Row] = []
    for m, g in pairs:
        age_days = (now - g.played_at).total_seconds() / 86400.0 if g.played_at else 0.0
        weight = 0.5 ** (max(0.0, age_days) / HALF_LIFE_DAYS)
        if g.id in recent_ids:
            half = "recent"
        elif g.id in prior_ids:
            half = "older"
        else:
            half = "archive"
        rows.append(_Row(
            cp_loss=int(m.cp_loss),
            bucket=m.error_bucket or "ok",
            pillar=m.pillar or "positional",
            motif=m.motif,
            endgame_type=m.endgame_type,
            eco=g.eco,
            opening=g.opening,
            color=g.color,
            time_trouble=bool(m.time_trouble),
            context=m.context or "balanced",
            punish_miss=bool(m.punish_miss),
            half=half,
            speed=speed_bucket(g.speed),
            weight=weight,
            # Rows written before `decision_w` existed default to 1.0, which is
            # the old behaviour - the profile degrades gracefully rather than
            # zeroing out every move it has not re-classified yet.
            decision_w=1.0 if m.decision_w is None else float(m.decision_w),
            critical=is_critical(m.forcingness, m.eval_before_cp, m.second_best_cp),
            seconds=m.seconds_spent,
        ))
    return rows


def _repertoire(session: Session, account: str | None) -> dict:
    """Per-colour opening spread: how concentrated is the user's repertoire, and
    which openings dominate. Used to nudge them toward variety."""
    q = select(Game.color, Game.opening, func.count()).where(
        Game.opening.isnot(None),
        Game.source.notin_(EVIDENCE_SOURCES_EXCLUDED),
    )
    if account:
        q = q.where(Game.account == account)
    rows = session.execute(q.group_by(Game.color, Game.opening)).all()

    by_colour: dict[str, list[tuple[str, int]]] = {"white": [], "black": []}
    for colour, opening, n in rows:
        by_colour.setdefault(colour, []).append((opening, n))

    out = {}
    for colour, items in by_colour.items():
        total = sum(n for _, n in items)
        if not total:
            continue
        items.sort(key=lambda t: -t[1])
        hhi = sum((n / total) ** 2 for _, n in items)
        out[colour] = {
            "games": total,
            "distinct": len(items),
            "concentration": round(hhi, 3),                 # 1.0 = always one opening
            "effective_openings": round(1 / hhi, 1) if hhi else 0.0,
            "top": [
                {"opening": o, "games": n, "share": round(n / total, 3)}
                for o, n in items[:6]
            ],
            "narrow": total >= 15 and (items[0][1] / total >= 0.6 or hhi >= 0.5),
        }
    return out


# An aggregate needs this much effective weight - recency times decision weight -
# before its averages mean anything. Raw move counts overstate that: a hundred
# forced recaptures from last year are not a hundred observations.
MIN_EFFECTIVE_W = 6.0


def _summ(losses: list[tuple[int, float]] | list[int], blunder_cp: int = 250) -> dict:
    """Recency-weighted summary of (cp_loss, weight) pairs.

    The weight carried in each pair is recency times *decision weight*, so an
    average here is an average over decisions the player actually made rather
    than over moves that happened. Forced recaptures used to contribute a
    cp-loss of zero apiece and quietly flatter every number they landed in.

    Plain ints are accepted too (weight 1.0) so callers that don't care about
    decay - and the tests - stay simple.

    ``blunder_cp`` is the threshold from ``coach.toml``. It used to be hardcoded
    at 250 here while ``classify.error_bucket`` honoured the configured value, so
    raising the setting relabelled the moves and left ``blunder_rate`` - which is
    what the ranking multiplies by - counting the old population.
    """
    pairs = [(x, 1.0) if isinstance(x, int) else x for x in losses]
    if not pairs:
        return {"n": 0, "avg_cp_loss": 0.0, "blunder_rate": 0.0,
                "total_cp_cost": 0, "weight": 0.0}
    wsum = sum(w for _, w in pairs) or 1e-9
    blunder_w = sum(w for x, w in pairs if x >= blunder_cp)
    return {
        "n": len(pairs),
        "avg_cp_loss": round(sum(x * w for x, w in pairs) / wsum, 1),
        "blunder_rate": round(blunder_w / wsum, 3),
        # Weighted cost: this is what severity ranks on, so an old leak fades.
        "total_cp_cost": int(sum(x * w for x, w in pairs)),
        "weight": round(wsum, 2),
    }


def _time_allocation(rows: list[_Row]) -> dict:
    """Where the clock went, against where the game was actually decided.

    Average centipawn loss cannot see this and neither can a puzzle rating. Two
    seconds in the one position where the evaluation could swing two pawns, and
    fourteen in a position where every sensible move holds, is a specific habit
    with a specific fix - and it currently presents as an ordinary tactical
    weakness, which sends the player off to do more tactics puzzles instead of
    telling them to stop and look.
    """
    timed = [r for r in rows if r.seconds is not None]
    if len(timed) < 30:
        return {"n_timed": len(timed), "note": "not enough games with clock data yet"}

    def bucket(items: list[_Row]) -> dict:
        if not items:
            return {"n": 0, "avg_s": 0.0, "median_s": 0.0, "avg_cp_loss": 0.0}
        secs = sorted(r.seconds for r in items)
        summ = _summ([(r.cp_loss, r.weight * r.decision_w) for r in items])
        # Weighted by how much of a decision the position was, for the same reason
        # the cp-loss on the next line is. Critical positions are forcing far more
        # often than quiet ones (44% against 0% on real data), and a forced
        # recapture is fast because there is nothing to choose - so an unweighted
        # mean credited "rushing" for moves that had no decision in them. On the
        # development corpus that overstated the gap by about a fifth (4.62s vs
        # 5.90s unweighted, 4.85s vs 5.91s over genuine decisions only). The
        # finding survives; it was just bigger than the truth.
        dw = sum(r.decision_w for r in items) or 1e-9
        return {
            "n": len(items),
            "avg_s": round(sum(r.seconds * r.decision_w for r in items) / dw, 1),
            "median_s": round(secs[len(secs) // 2], 1),
            "avg_cp_loss": summ["avg_cp_loss"],
            "blunder_rate": summ["blunder_rate"],
            "effective_n": round(dw, 1),
        }

    critical = [r for r in timed if r.critical]
    quiet = [r for r in timed if not r.critical]
    rushed = [r for r in critical if r.seconds < RUSHED_SECONDS]
    considered = [r for r in critical if r.seconds >= RUSHED_SECONDS]

    out = {
        "n_timed": len(timed),
        "critical": bucket(critical),
        "quiet": bucket(quiet),
        "rushed_critical": bucket(rushed),
        "considered_critical": bucket(considered),
        "rushed_critical_rate": round(len(rushed) / len(critical), 3) if critical else 0.0,
        "rushed_seconds": RUSHED_SECONDS,
    }
    # Comparing cp-loss between rushed and considered moves is tempting and does
    # NOT measure what rushing costs: time spent is chosen by the player, and
    # they spend it on the positions they can already tell are difficult. On real
    # data the considered moves score *worse* (-184cp against -100cp), which
    # reads as "thinking is harmful" and means nothing of the sort. The numbers
    # stay in the payload because they are real, flagged so nothing downstream
    # phrases them as a cost.
    if len(rushed) >= 10 and len(considered) >= 10:
        out["rushed_vs_considered_cp"] = round(
            out["rushed_critical"]["avg_cp_loss"]
            - out["considered_critical"]["avg_cp_loss"], 1
        )
        out["confounded"] = (
            "not a causal cost: players spend longer on positions they can "
            "already see are hard, so this comparison runs backwards"
        )

    # This one is clean, and it is the finding. It compares two *kinds of
    # position* rather than two outcomes, so nothing the player chooses after
    # seeing the position can bias it. Below 1.0 means they give the positions
    # that decide the game less time than the ones that don't.
    if critical and quiet:
        out["allocation_ratio"] = round(
            out["critical"]["avg_s"] / max(0.1, out["quiet"]["avg_s"]), 2
        )
    out["considered_seconds"] = CONSIDERED_SECONDS
    return out


def _blunder_cp() -> int:
    """The configured blunder threshold, or the default when there is no config."""
    try:
        from coach.config import CoachConfig

        return int(CoachConfig.load().profile.blunder_cp)
    except Exception:  # noqa: BLE001 - no config is not a reason to fail a profile
        return 250


def build_profile(account: str | None = None, db_path=None) -> dict:
    # One session for the whole build. It used to open, close, do the work, then
    # open a second one to write the snapshot.
    Session = session_factory(db_path)
    session = Session()
    try:
        return _build(session, account)
    finally:
        session.close()


def _build(session: Session, account: str | None) -> dict:
    blunder_cp = _blunder_cp()

    def summ(vals):
        return _summ(vals, blunder_cp)

    rows = _fetch(session, account)
    games = session.scalar(
        select(Game.id).where(Game.analyzed.is_(True)).limit(1)
    )
    if not rows:
        return {"account": account, "n_moves": 0,
                "note": "no analysed games yet - run `coach sync` then `coach analyze`",
                "has_games": bool(games)}

    by_pillar: dict[str, list[tuple[int, float]]] = defaultdict(list)
    by_pillar_half: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    by_motif: dict[str, list[tuple[int, float]]] = defaultdict(list)
    by_eg: dict[str, list[tuple[int, float]]] = defaultdict(list)
    by_open: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    by_speed: dict[str, list[tuple[int, float]]] = defaultdict(list)
    by_speed_pillar: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    tt_losses: list[tuple[int, float]] = []
    non_tt_losses: list[tuple[int, float]] = []
    by_context: dict[str, list[tuple[int, float]]] = defaultdict(list)
    punish_losses: list[tuple[int, float]] = []

    for r in rows:
        # Recency times decision weight: a forced move is not an observation.
        wv = (r.cp_loss, r.weight * r.decision_w)
        by_pillar[r.pillar].append(wv)
        by_pillar_half[(r.pillar, r.half)].append(wv)
        by_speed[r.speed].append(wv)
        by_speed_pillar[(r.speed, r.pillar)].append(wv)
        for mt in (r.motif.split(",") if r.motif else []):
            if mt:
                by_motif[mt].append(wv)
        if r.endgame_type:
            by_eg[r.endgame_type].append(wv)
        if r.opening:
            by_open[(r.opening, r.color)].append(wv)
        (tt_losses if r.time_trouble else non_tt_losses).append(wv)
        by_context[r.context].append(wv)
        if r.punish_miss:
            punish_losses.append(wv)

    pillars = {}
    for p in PILLARS:
        s = summ(by_pillar.get(p, []))
        older = summ(by_pillar_half.get((p, "older"), []))
        recent = summ(by_pillar_half.get((p, "recent"), []))
        # Only call it a trend when both windows have enough moves to mean anything.
        if older["n"] >= 10 and recent["n"] >= 10:
            s["trend_cp_loss"] = round(recent["avg_cp_loss"] - older["avg_cp_loss"], 1)
        else:
            s["trend_cp_loss"] = 0.0
        s["label"] = _PILLAR_LABEL[p]
        pillars[p] = s

    # Per-time-control view: the same pillar can be sound at 15+10 and fall apart
    # at 3+2. Anything with too few moves to be meaningful is dropped.
    speeds: dict[str, dict] = {}
    for sp, vals in by_speed.items():
        if sp in ("other",) or len(vals) < 40:
            continue
        entry = summ(vals)
        entry["pillars"] = {
            pl: summ(by_speed_pillar.get((sp, pl), []))
            for pl in PILLARS
            if len(by_speed_pillar.get((sp, pl), [])) >= 12
        }
        if entry["pillars"]:
            entry["worst_pillar"] = max(
                entry["pillars"], key=lambda k: entry["pillars"][k]["total_cp_cost"]
            )
        speeds[sp] = entry

    # Rank weaknesses by RATE, with volume used only as a confidence gate.
    #
    # A weakness is a rate: how badly you play this kind of position, per decision
    # you make in it. Volume is how often it comes up, which is a different
    # question and belongs in a different sentence.
    #
    # Ranking on cost share alone measures volume outright. Blending it with rate
    # share as a geometric mean - the previous attempt - stopped a pillar winning
    # on volume but still let one *lose* on it: cost share varies about 3.4x across
    # pillars on real data while rate share varies only 2.6x, so volume still
    # decided. It put Openings (49cp per decision, 3% blunders, 5,397 decisions)
    # above Positional (129cp per decision, 17% blunders, 889) - the pillar played
    # best above the pillar played worst - and ranked_weaknesses[0] drives the
    # entire daily plan.
    #
    # So: severity is the rate, scaled by blunder rate and worsening trend. Volume
    # does not enter it at all. It is kept out by MIN_EFFECTIVE_W, which is the
    # honest role for a sample size - deciding whether a number means anything,
    # not how bad it is. `cost_share` and `rate_share` are still reported so the
    # dashboard can say how often a pillar comes up alongside how bad it is.
    ranked = []
    total_cost = sum(p["total_cp_cost"] for p in pillars.values()) or 1
    total_rate = sum(p["avg_cp_loss"] for p in pillars.values()) or 1.0
    for p, s in pillars.items():
        if s["weight"] < MIN_EFFECTIVE_W:
            continue
        cost_share = s["total_cp_cost"] / total_cost
        rate_share = s["avg_cp_loss"] / total_rate
        severity = rate_share * (1 + s["blunder_rate"]) * (1 + max(0.0, s["trend_cp_loss"]) / 50.0)
        ranked.append({
            "pillar": p, "label": s["label"], "severity": round(severity, 4),
            "cost_share": round(cost_share, 3), "rate_share": round(rate_share, 3),
            "avg_cp_loss": s["avg_cp_loss"], "blunder_rate": s["blunder_rate"],
            "n": s["n"], "weight": s["weight"], "trend_cp_loss": s["trend_cp_loss"],
        })
    ranked.sort(key=lambda x: x["severity"], reverse=True)

    def _top(d: dict, key_fmt, min_n=5, k=6):
        # Gate on effective weight, not raw count: everything else in the profile
        # decays with age, so a leak closed six months ago could still appear
        # here on the strength of how many times it happened back then.
        items = [(summ(v), k_) for k_, v in d.items() if len(v) >= min_n]
        items = [(t, k_) for t, k_ in items if t["weight"] >= min_n * 0.25]
        items.sort(key=lambda t: t[0]["total_cp_cost"], reverse=True)
        return [{"key": key_fmt(k_), **s} for s, k_ in items[:k]]

    profile = {
        "account": account or "all",
        "n_moves": len(rows),
        "n_games_analysed": 0,  # filled in below
        "pillars": pillars,
        "speeds": speeds,
        "ranked_weaknesses": ranked,
        "half_life_days": HALF_LIFE_DAYS,
        # `missed-punish` is deliberately not in here. It is a context flag, not
        # a pattern - "your opponent had just erred" - and it is set on a looser
        # gate (cp_loss >= 60) than the real motifs, which are only computed for
        # moves already labelled mistake or blunder (>= 120). Ranked alongside
        # them it won by inclusion rule rather than by frequency, topping the
        # table at 1,137 against hangs-piece's 455. It has its own section below.
        "tactics_motifs": _top(
            {k: v for k, v in by_motif.items() if k != "missed-punish"}, lambda k: k
        ),
        "endgames": _top(by_eg, lambda k: f"{k} endgame", min_n=4),
        "openings_worst": _top(by_open, lambda k: f"{k[0]} as {k[1]}", min_n=6),
        "time_trouble": {
            "in_time_trouble": summ(tt_losses),
            "otherwise": summ(non_tt_losses),
        },
        "attack_defence": {
            "attacking": summ(by_context.get("attacking", [])),
            "defending": summ(by_context.get("defending", [])),
            "balanced": summ(by_context.get("balanced", [])),
        },
        "punishing": {
            "missed_punish": summ(punish_losses),
            "note": "positions where the opponent erred and you had a stronger reply",
        },
        "time_allocation": _time_allocation(rows),
    }

    profile["repertoire"] = _repertoire(session, account)
    profile["freshness"] = _freshness(session, account)
    profile["n_games_analysed"] = session.query(Game).filter(Game.analyzed.is_(True)).count()
    _save_snapshot(session, account, profile)
    session.commit()
    return profile


# Past this, the profile is describing a player from a while ago. One half-life is
# the honest line: the *newest* evidence in the library already counts for half,
# so everything the profile is built on is archive - and the app says "the player
# you are now" without qualification.
STALE_AFTER_DAYS = HALF_LIFE_DAYS


def _freshness(session: Session, account: str | None) -> dict:
    """How old the newest real game is, per site and overall.

    The whole profile is recency-weighted, which quietly assumes there is
    something recent to weight. When the newest real game is four months old the
    numbers are still the best available - but presenting them as "the player you
    are now" without saying so is the difference between a stale profile and a
    misleading one.
    """
    q = select(Game.source, func.max(Game.played_at)).where(
        Game.source.notin_(EVIDENCE_SOURCES_EXCLUDED),
        Game.played_at.isnot(None),
    )
    if account:
        q = q.where(Game.account == account)
    rows = session.execute(q.group_by(Game.source)).all()

    now = datetime.now(UTC).replace(tzinfo=None)
    sites = {}
    newest = None
    for source, when in rows:
        if when is None:
            continue
        sites[source] = {"newest": when.isoformat(),
                         "age_days": round((now - when).total_seconds() / 86400.0)}
        if newest is None or when > newest:
            newest = when
    if newest is None:
        return {"sites": {}, "age_days": None, "stale": False,
                "note": "no real games ingested yet"}

    age = (now - newest).total_seconds() / 86400.0
    out = {"sites": sites, "age_days": round(age),
           "stale": age >= STALE_AFTER_DAYS,
           "half_life_days": HALF_LIFE_DAYS}
    if out["stale"]:
        out["note"] = (
            f"your newest real game is {round(age)} days old, so today's plan is "
            f"built mostly on older games - run `coach sync` if you have played since"
        )
    return out


def _save_snapshot(session: Session, account: str | None, profile: dict) -> None:
    """Keep at most one snapshot per account per day.

    The trend chart is built from these, and its docstring assumes a weekly-ish
    cadence. In practice `build_profile` runs on every app open, so opening the
    app four times in an evening drew four points and stored four copies of the
    whole profile. One per day is what the chart was always trying to show.
    """
    label = account or "all"
    now = datetime.now(UTC).replace(tzinfo=None)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    session.execute(
        delete(ProfileSnapshot).where(
            ProfileSnapshot.account == label,
            ProfileSnapshot.created_at >= day_start,
        )
    )
    session.add(ProfileSnapshot(account=label, payload=json.dumps(profile),
                                created_at=now))


def prune_snapshots(db_path=None) -> int:
    """Collapse pre-existing history to one snapshot per account per day.

    Keeps the last snapshot of each day, which is the one that saw the most
    games, so the shape of the trend survives and only the intra-day duplicates
    go.
    """
    Session = session_factory(db_path)
    removed = 0
    with Session() as session:
        rows = session.execute(
            select(ProfileSnapshot.id, ProfileSnapshot.account, ProfileSnapshot.created_at)
            .order_by(ProfileSnapshot.created_at)
        ).all()
        keep: dict[tuple[str, str], int] = {}
        for sid, acct, created in rows:
            keep[(acct, created.date().isoformat())] = sid
        survivors = set(keep.values())
        doomed = [sid for sid, _, _ in rows if sid not in survivors]
        if doomed:
            session.execute(delete(ProfileSnapshot).where(ProfileSnapshot.id.in_(doomed)))
            session.commit()
            removed = len(doomed)
    return removed


def latest_profile(account: str | None = None, db_path=None) -> dict | None:
    """Most recently computed snapshot, without recomputing. None if never run."""
    Session = session_factory(db_path)
    with Session() as session:
        # `_save_snapshot` files the aggregate under the literal "all", so asking
        # for None has to ask for that row. Without the filter it returned the
        # newest snapshot of *any* account: harmless while `unified = true` and
        # only one row is ever written, and silently wrong the moment a
        # per-account profile is built, which is exactly when it matters.
        label = account or "all"
        q = (
            select(ProfileSnapshot)
            .where(ProfileSnapshot.account == label)
            .order_by(ProfileSnapshot.created_at.desc())
        )
        snap = session.scalars(q.limit(1)).first()
        return json.loads(snap.payload) if snap else None


def top_weak_pillar(account: str | None = None, db_path=None) -> str | None:
    prof = latest_profile(account, db_path)
    if prof and prof.get("ranked_weaknesses"):
        return prof["ranked_weaknesses"][0]["pillar"]
    return None


def render_text(profile: dict) -> str:
    if not profile.get("n_moves"):
        return profile.get("note", "no data")
    lines = [
        (
            f"Weakness profile for {profile['account']} "
            f"({profile['n_moves']} of your moves across {profile['n_games_analysed']} games)"
        ),
    ]
    fresh = profile.get("freshness") or {}
    if fresh.get("note"):
        lines.append(f"  ! {fresh['note']}")
    lines += ["", "Ranked focus areas:"]
    for i, w in enumerate(profile["ranked_weaknesses"], 1):
        trend = "improving" if w["trend_cp_loss"] < -3 else "worsening" if w["trend_cp_loss"] > 3 else "flat"
        lines.append(
            f"  {i}. {w['label']:<34} avg -{w['avg_cp_loss']:.0f}cp/move, "
            f"blunders {w['blunder_rate']*100:.0f}%, {trend}  (n={w['n']})"
        )
    if profile["tactics_motifs"]:
        lines += ["", "Recurring tactical motifs:"]
        for mt in profile["tactics_motifs"]:
            lines.append(f"  - {mt['key']:<22} {mt['n']}x, -{mt['total_cp_cost']}cp total")
    if profile["endgames"]:
        lines += ["", "Endgames by damage:"]
        for eg in profile["endgames"]:
            lines.append(f"  - {eg['key']:<22} avg -{eg['avg_cp_loss']:.0f}cp (n={eg['n']})")
    if profile["openings_worst"]:
        lines += ["", "Worst-scoring openings:"]
        for op in profile["openings_worst"]:
            lines.append(f"  - {op['key']:<40} avg -{op['avg_cp_loss']:.0f}cp (n={op['n']})")
    ad = profile.get("attack_defence", {})
    if ad.get("attacking", {}).get("n", 0) >= 5 and ad.get("defending", {}).get("n", 0) >= 5:
        lines += [
            "",
            (f"Attack vs defence: avg -{ad['attacking']['avg_cp_loss']:.0f}cp when attacking, "
            f"-{ad['defending']['avg_cp_loss']:.0f}cp when defending"),
        ]
    pun = profile.get("punishing", {}).get("missed_punish", {})
    if pun.get("n", 0) >= 3:
        lines += [
            "",
            (f"Punishing opponent errors: {pun['n']} times you were handed an edge and "
            f"missed the strongest reply (avg -{pun['avg_cp_loss']:.0f}cp)"),
        ]

    rep = profile.get("repertoire", {})
    for colour, r in rep.items():
        if r.get("narrow"):
            top = r["top"][0]
            lines += [
                "",
                (
                    f"Repertoire ({colour}): {r['effective_openings']} effective openings "
                    f"across {r['games']} games — {top['opening']} is "
                    f"{top['share']*100:.0f}%. Broaden."
                ),
            ]

    ta = profile.get("time_allocation", {})
    if ta.get("critical", {}).get("n", 0) >= 20:
        ratio = ta.get("allocation_ratio")
        lines += [
            "",
            (
                f"Time allocation: {ta['critical']['avg_s']:.1f}s on average in the "
                f"{ta['critical']['n']} positions where the game was actually decided, "
                f"{ta['quiet']['avg_s']:.1f}s in the {ta['quiet']['n']} quiet ones."
            ),
        ]
        if ratio is not None and ratio < 1.0:
            lines.append(
                f"  That is the wrong way round: {(1 - ratio) * 100:.0f}% LESS time on the "
                f"positions that matter, and {ta['rushed_critical_rate'] * 100:.0f}% of them "
                f"go by in under {ta['rushed_seconds']:.0f}s."
            )
        elif ratio is not None:
            lines.append(f"  Ratio {ratio:.2f}x - time is going to the right places.")

    tt = profile["time_trouble"]
    if tt["in_time_trouble"]["n"] >= 5:
        lines += [
            "",
            (
                f"Time trouble: blunder rate {tt['in_time_trouble']['blunder_rate']*100:.0f}% "
                f"vs {tt['otherwise']['blunder_rate']*100:.0f}% otherwise"
            ),
        ]
    return "\n".join(lines)
