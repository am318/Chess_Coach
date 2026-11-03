"""Sparring Elo: tuned to a 45% score, capped steps, and a measured scale error."""

from __future__ import annotations

from coach.profile import rating
from coach.store.db import Game, session_factory, set_setting


def test_converges_where_the_player_scores_the_target(tmp_path):
    db = tmp_path / "r.db"
    with session_factory(db)() as s:
        set_setting(s, "sparring_elo", "1200")
        s.commit()
    # A player who beats anything under 1500 and loses to anything above.
    for _ in range(80):
        cur = rating.adaptive_elo(db_path=db)
        rating.record_result(1.0 if cur < 1500 else 0.0, db_path=db)
    assert abs(rating.adaptive_elo(db_path=db) - 1500) <= 40


def test_a_45_percent_score_is_equilibrium(tmp_path):
    db = tmp_path / "e.db"
    with session_factory(db)() as s:
        set_setting(s, "sparring_elo", "1400")
        set_setting(s, "sparring_games", "100")      # settled K
        s.commit()
    pattern = [1.0, 0.0, 0.5, 1.0, 0.0, 0.0, 0.5, 1.0, 0.0, 0.5]   # 45%
    for sc in pattern * 3:
        rating.record_result(sc, db_path=db)
    assert abs(rating.adaptive_elo(db_path=db) - 1400) <= 2


def test_no_single_game_moves_it_far(tmp_path):
    db = tmp_path / "c.db"
    with session_factory(db)() as s:
        set_setting(s, "sparring_elo", "1400")
        s.commit()
    d = rating.record_result_detail(1.0, db_path=db)
    assert 0 < d["change"] <= rating.MAX_STEP
    assert d["before"] == 1400 and d["after"] == 1400 + d["change"]


def test_status_reports_the_scale_offset_once_settled(tmp_path):
    db = tmp_path / "s.db"
    with session_factory(db)() as s:
        for i in range(5):
            s.add(Game(source="lichess", source_id=str(i), account="me", color="white",
                       result="win", pgn="", white_elo=1600, black_elo=1500))
        set_setting(s, "sparring_elo", "1100")
        s.commit()
    st = rating.sparring_status(db_path=db)
    assert st["games"] == 0 and "scale_offset" not in st
    for sc in [0.5] * rating.SETTLED_GAMES:
        rating.record_result(sc, db_path=db)
    st = rating.sparring_status(db_path=db)
    assert st["settled"] and st["online_rating"] == 1600
    # Scoring 50% against a ~1100 bot from a 1600 online rating: the bot's
    # scale reads several hundred points low.
    assert st["scale_offset"] > 300
    assert st["recent_score"] == 0.5
