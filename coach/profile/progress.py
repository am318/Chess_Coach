"""Time-series for the dashboard: are the weak pillars actually improving?

Three series:
  * pillar avg-cp-loss over successive profile snapshots (one per day);
  * sparring score vs the bot over time, bucketed by week;
  * **recurrence**: how often each tactical motif still shows up in real games,
    per hundred decisions, month by month.

The third is the one that answers the question the other two only gesture at.
"Average centipawn loss in the tactics pillar drifted from 71 to 66" is noisy,
slow, and hard to feel. "You hung a piece nine times per hundred decisions in
January and twice in March" is the same claim, legible, and directly attributable
to the drilling - which is the point of the whole application.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from itertools import pairwise

from sqlalchemy import select

from coach.profile.weakness_model import EVIDENCE_SOURCES_EXCLUDED, PILLARS
from coach.store.db import Game, Move, ProfileSnapshot, session_factory


def pillar_trends(account: str | None = None, db_path=None) -> dict:
    Session = session_factory(db_path)
    with Session() as session:
        q = select(ProfileSnapshot).order_by(ProfileSnapshot.created_at)
        if account:
            q = q.where(ProfileSnapshot.account == account)
        snaps = list(session.scalars(q))

    points = []
    for s in snaps:
        try:
            payload = json.loads(s.payload)
        except ValueError:
            continue
        row = {"date": s.created_at.isoformat(), "n_moves": payload.get("n_moves", 0)}
        for p in PILLARS:
            row[p] = payload.get("pillars", {}).get(p, {}).get("avg_cp_loss")
        points.append(row)
    return {"series": points, "pillars": PILLARS}


def sparring_trend(account: str | None = None, db_path=None) -> dict:
    Session = session_factory(db_path)
    with Session() as session:
        q = select(Game).where(Game.source == "sparring").order_by(Game.played_at)
        if account:
            q = q.where(Game.account == account)
        games = list(session.scalars(q))

    weekly: dict[str, list[float]] = defaultdict(list)
    for g in games:
        when = g.played_at or datetime.now(UTC)
        key = f"{when.isocalendar().year}-W{when.isocalendar().week:02d}"
        score = {"win": 1.0, "draw": 0.5, "loss": 0.0}.get(g.result, 0.5)
        weekly[key].append(score)

    return {
        "series": [
            {"week": k, "games": len(v), "score": round(sum(v) / len(v), 3)}
            for k, v in sorted(weekly.items())
        ]
    }


# A month needs this many scored decisions before its recurrence rates are worth
# plotting; below it a single bad evening reads as a trend.
_MIN_MONTH_DECISIONS = 150
_TOP_MOTIFS = 6


def motif_recurrence(account: str | None = None, db_path=None, months: int = 12) -> dict:
    """Per-motif occurrences per 100 decisions, by month.

    Rate rather than count, because "I made fewer back-rank errors" is not
    evidence of anything if you also played a third as many games.

    Sparring games are excluded for the same reason they are excluded from the
    weakness profile: they are a different population, played against a bot that
    steers toward the user's weak pillar. Left in, the newest point on the chart
    was frequently made *entirely* of them - one real month at 2.14 hangs-piece
    per hundred decisions followed by a sparring-only month at 7.03, which reads
    as a collapse in form and is a change of opponent.
    """
    Session = session_factory(db_path)
    with Session() as session:
        q = (
            select(Move.motif, Game.played_at)
            .join(Game, Move.game_id == Game.id)
            .where(Move.is_user_move.is_(True), Game.analyzed.is_(True),
                   Move.cp_loss.isnot(None), Game.played_at.isnot(None),
                   Game.source.notin_(EVIDENCE_SOURCES_EXCLUDED))
        )
        if account:
            q = q.where(Game.account == account)
        rows = session.execute(q).all()

    denom: dict[str, int] = defaultdict(int)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for motif, played_at in rows:
        key = f"{played_at.year}-{played_at.month:02d}"
        denom[key] += 1
        for mt in (motif or "").split(","):
            if mt:
                counts[mt][key] += 1

    keys = sorted(k for k, n in denom.items() if n >= _MIN_MONTH_DECISIONS)[-months:]
    if not keys:
        return {"months": [], "motifs": [], "note": "not enough analysed games yet"}

    totals = {mt: sum(by_month.get(k, 0) for k in keys) for mt, by_month in counts.items()}
    top = sorted(totals, key=lambda m: -totals[m])[:_TOP_MOTIFS]

    series = []
    for mt in top:
        rates = [round(100.0 * counts[mt].get(k, 0) / denom[k], 2) for k in keys]
        series.append({
            "motif": mt,
            "rates": rates,
            "total": totals[mt],
            # Positive = happening less often now than at the start of the window.
            "improvement": _improvement(rates, [denom[k] for k in keys]),
            "current": rates[-1],
        })
    series.sort(key=lambda d: -d["current"])
    return {
        "months": keys,
        "decisions": [denom[k] for k in keys],
        "motifs": series,
        "unit": "occurrences per 100 decisions",
        # The months that qualify are rarely consecutive - a real library skips
        # the ones with too little play - so anything drawing this has to label
        # the axis with these keys rather than spacing the points evenly.
        "contiguous": _contiguous(keys),
    }


def _improvement(rates: list[float], weights: list[int]) -> float:
    """How much the rate has come down, comparing the window's two halves.

    The old version was ``rates[0] - rates[-1]``: two single months, which on a
    real library were April 2024 and September 2026 - twenty-nine months apart,
    each one a sample of a few hundred decisions, and either of them a bad
    fortnight away from reversing the sign. Halves, weighted by how many
    decisions each month actually carried, say the same thing without resting
    the whole claim on two points.
    """
    n = len(rates)
    if n < 2:
        return 0.0
    cut = n // 2
    def mean(vals, ws):
        total = sum(ws) or 1
        return sum(v * w for v, w in zip(vals, ws)) / total
    early = mean(rates[:cut], weights[:cut])
    late = mean(rates[n - cut:], weights[n - cut:])
    return round(early - late, 2)


def _contiguous(keys: list[str]) -> bool:
    """Are these month keys consecutive calendar months?"""
    months = []
    for k in keys:
        y, _, m = k.partition("-")
        try:
            months.append(int(y) * 12 + int(m))
        except ValueError:
            return False
    return all(b - a == 1 for a, b in pairwise(months))


def dashboard(account: str | None = None, db_path=None) -> dict:
    from coach.profile.weakness_model import latest_profile

    return {
        "profile": latest_profile(account) or {"n_moves": 0},
        "pillar_trends": pillar_trends(account, db_path),
        "sparring_trend": sparring_trend(account, db_path),
        "motif_recurrence": motif_recurrence(account, db_path),
    }
