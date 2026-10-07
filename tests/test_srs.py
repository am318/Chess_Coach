"""SRS scheduler behaviour."""


from coach.puzzles import srs
from coach.store.db import SrsCard


def _card():
    return SrsCard(puzzle_id=1, stability=0.0, difficulty=5.0, due_at=srs._now(), reps=0)


def test_good_grows_interval_again_shrinks_it():
    c = _card()
    o1 = srs.review(c, srs.GOOD)
    assert o1.interval_days > 0.5
    o2 = srs.review(c, srs.GOOD, now=c.due_at)
    assert o2.interval_days > o1.interval_days       # spacing expands on repeated success
    o3 = srs.review(c, srs.AGAIN, now=c.due_at)
    assert o3.lapsed and o3.interval_days < 0.02     # back within minutes
    assert c.lapses == 1


def test_easy_beats_hard_interval():
    e = srs.review(_card(), srs.EASY)
    h = srs.review(_card(), srs.HARD)
    assert e.interval_days > h.interval_days


def test_due_queue_prefers_weak_pillars(tmp_path):
    from coach.store.db import Puzzle, session_factory

    Session = session_factory(tmp_path / "t.db")
    with Session() as s:
        for pil in ("tactics", "endgame"):
            p = Puzzle(source="own-game", fen="8/8/8/8/8/8/8/8 w - - 0 1",
                       solution="e2e4", pillar=pil, kind="find-best")
            s.add(p)
            s.flush()
            srs.ensure_card(s, p)
        s.commit()
        q = srs.due_queue(s, limit=2, pillar_weights={"endgame": 5.0, "tactics": 1.0})
        assert q[0].pillar == "endgame"


def test_selection_sits_below_ability_by_the_target_gap(tmp_path):
    """Ability and difficulty are two numbers, not one.

    `puzzle_rating` says how good you are; `selection_rating` says what to serve,
    which is the Elo gap at which you are expected to score TARGET_SUCCESS.
    """
    from coach.store.db import session_factory, set_setting

    Session = session_factory(tmp_path / "sel.db")
    with Session() as s:
        set_setting(s, "puzzle_rating", "1500")
        s.commit()
        assert srs.puzzle_rating(s) == 1500
        target = srs.selection_rating(s)
        # 400 * log10(0.8/0.2) == ~241 points below ability.
        assert 1250 < target < 1265
        # Raising the demanded success rate must lower the difficulty served.
        assert srs.selection_rating(s, 0.9) < target
        assert srs.selection_rating(s, 0.5) == 1500


def test_rating_update_is_plain_elo_with_no_hidden_drift(tmp_path):
    """A solve at your own level should move you up, not down.

    The update used to subtract a constant on every single review to drag the
    queue toward the target success rate - which meant a correct answer against
    an equal puzzle *lowered* the ability estimate.
    """
    from coach.store.db import Puzzle, session_factory, set_setting

    Session = session_factory(tmp_path / "elo.db")
    with Session() as s:
        set_setting(s, "puzzle_rating", "1500")
        p = Puzzle(source="own-game", fen="8/8/8/8/8/8/8/8 w - - 0 1",
                   solution="e2e4", pillar="tactics", kind="find-best", rating_est=1500)
        s.add(p)
        s.commit()
        assert srs.update_puzzle_rating(s, p, solved=True) > 1500
        set_setting(s, "puzzle_rating", "1500")
        s.commit()
        assert srs.update_puzzle_rating(s, p, solved=False) < 1500


def test_unrated_puzzle_barely_moves_the_estimate(tmp_path):
    """A drill with no rating is an even match, not an easy one."""
    from coach.store.db import Puzzle, session_factory, set_setting

    Session = session_factory(tmp_path / "unrated.db")
    with Session() as s:
        set_setting(s, "puzzle_rating", "1500")
        p = Puzzle(source="endgame", fen="8/8/8/8/8/8/8/8 w - - 0 1",
                   solution="e2e4", pillar="endgame", kind="find-best")
        s.add(p)
        s.commit()
        after = srs.update_puzzle_rating(s, p, solved=True)
        assert 1500 < after <= 1512     # half of K, no constant drift on top


def test_due_queue_targets_the_selection_band(tmp_path):
    """With a backlog larger than the candidate window, difficulty targeting must
    still work - the old query took the oldest N and re-ranked inside them."""
    from coach.store.db import Puzzle, session_factory, set_setting

    Session = session_factory(tmp_path / "band.db")
    with Session() as s:
        set_setting(s, "puzzle_rating", "1500")
        # 40 very old, far-too-hard cards, then one at the right level.
        for i in range(40):
            p = Puzzle(source="own-game", fen="8/8/8/8/8/8/8/8 w - - 0 1",
                       solution="e2e4", pillar="tactics", kind="find-best",
                       rating_est=2400 + i)
            s.add(p); s.flush(); srs.ensure_card(s, p)
        fit = Puzzle(source="own-game", fen="8/8/8/8/8/8/8/8 w - - 0 1",
                     solution="e2e4", pillar="tactics", kind="find-best",
                     rating_est=srs.selection_rating(s))
        s.add(fit); s.flush(); srs.ensure_card(s, fit)
        s.commit()
        q = srs.due_queue(s, limit=3, candidate_pool=5)
        assert fit.id in [p.id for p in q]
