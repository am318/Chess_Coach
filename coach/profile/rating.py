"""Estimate the user's playing strength and keep an adaptive sparring Elo.

`estimate_rating` reads the user's own rating from their recent games (Lichess and
Chess.com both put it in the PGN headers). `adaptive_elo` / `record_result`
maintain a separate running number that tracks how the user does *against the
sparring bot*, so difficulty self-corrects.
"""

from __future__ import annotations

import statistics

from sqlalchemy import select

from coach.store.db import Game, get_setting, session_factory, set_setting

_KEY = "sparring_elo"
_STYLE_ROT = "sparring_style_rot"
_MIN, _MAX = 350, 2700
_STYLES = ["solid", "provocative", "gambit"]


def estimate_rating(account: str | None = None, db_path=None) -> int | None:
    """Median of the user's own rating across their most recent 60 rated games."""
    Session = session_factory(db_path)
    with Session() as session:
        q = (
            select(Game.color, Game.white_elo, Game.black_elo)
            .where(Game.source.in_(("lichess", "chesscom")))
            .order_by(Game.played_at.desc())
            .limit(60)
        )
        if account:
            q = q.where(Game.account == account)
        rows = session.execute(q).all()
    mine = [
        (w if c == "white" else b)
        for c, w, b in rows
        if (w if c == "white" else b)
    ]
    return int(statistics.median(mine)) if mine else None


def adaptive_elo(account: str | None = None, db_path=None) -> int:
    Session = session_factory(db_path)
    with Session() as session:
        stored = get_setting(session, _KEY)
        if stored:
            return max(_MIN, min(_MAX, int(float(stored))))
    seed = estimate_rating(account, db_path) or 1400
    return max(_MIN, min(_MAX, seed))


_GAMES_KEY = "sparring_games"
_K_START, _K_FLOOR, _K_HALFLIFE = 40.0, 10.0, 12.0


def _k_factor(games: int) -> float:
    """Provisional-rating shape: react fast at first, then settle.

    A single game carries about one bit of information. The original update moved
    the bot +-96 Elo per result, so two losses dropped it nearly 200 points and
    the level oscillated around the user instead of converging on them.
    """
    return _K_FLOOR + (_K_START - _K_FLOOR) * (0.5 ** (games / _K_HALFLIFE))


def record_result(user_score: float, account: str | None = None, db_path=None) -> int:
    """user_score: 1.0 win / 0.5 draw / 0.0 loss vs the bot. Nudges the stored Elo
    toward the level that yields ~50%."""
    Session = session_factory(db_path)
    with Session() as session:
        cur = float(get_setting(session, _KEY) or adaptive_elo(account, db_path))
        games = int(get_setting(session, _GAMES_KEY, "0") or "0")
        cur += _k_factor(games) * (user_score - 0.5) * 2.0
        cur = max(_MIN, min(_MAX, cur))
        set_setting(session, _KEY, f"{cur:.0f}")
        set_setting(session, _GAMES_KEY, str(games + 1))
        session.commit()
        return int(cur)


def next_style(weak_pillar: str | None, account: str | None = None, db_path=None) -> str:
    """Auto style: mostly targeted at the current weakness, with every 3rd game a
    rotation for variety."""
    Session = session_factory(db_path)
    with Session() as session:
        rot = int(get_setting(session, _STYLE_ROT, "0") or "0")
        set_setting(session, _STYLE_ROT, str(rot + 1))
        session.commit()

    if rot % 3 == 2:
        return _STYLES[rot // 3 % len(_STYLES)]  # cycle for variety

    targeted = {
        "tactics": "provocative",
        "quiet": "solid",
        "positional": "solid",
        "endgame": "solid",
        "opening": "solid",
    }
    # "defending"/"punishing" weaknesses surface as motifs, handled by provocation.
    return targeted.get(weak_pillar or "", "solid")
