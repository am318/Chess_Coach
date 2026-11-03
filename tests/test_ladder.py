"""The endgame ladder: ordered rungs, gated, with test-out."""

from __future__ import annotations

from coach.drills import ladder
from coach.drills.endgames import BY_KEY, ENDGAMES
from coach.drills.seed import seed_endgames
from coach.store.db import Puzzle, SrsCard, session_factory


def test_every_drill_is_on_exactly_one_rung():
    on = [k for r in ladder.RUNGS for k in r.keys]
    assert sorted(on) == sorted(e.key for e in ENDGAMES)
    assert len(on) == len(set(on))
    assert all(k in BY_KEY for k in on)


def _win(s, key):
    p = s.query(Puzzle).filter_by(external_id=f"endgame:{key}").one()
    s.query(SrsCard).filter_by(puzzle_id=p.id).one().wins = 1
    s.flush()


def test_rungs_open_one_at_a_time(tmp_path):
    db = tmp_path / "l.db"
    seed_endgames(db)
    with session_factory(db)() as s:
        st = ladder.status(s)
        assert st["current"] == 0
        assert [r["open"] for r in st["rungs"]][:2] == [True, False]
        locked = set(ladder.locked_ids(s))
        first = {p["id"] for p in st["rungs"][0]["positions"]}
        assert not locked & first and locked

        for k in ladder.RUNGS[0].keys:
            _win(s, k)
        st = ladder.status(s)
        assert st["rungs"][0]["passed"] and st["rungs"][1]["open"] and st["rungs"][1]["current"]
        assert not st["rungs"][2]["open"]


def test_a_clean_test_out_opens_the_rung_and_everything_below(tmp_path):
    db = tmp_path / "t.db"
    seed_endgames(db)
    with session_factory(db)() as s:
        ladder.start_testout(s, 3)
        rung = ladder.RUNGS[3]
        for k in rung.keys:
            p = s.query(Puzzle).filter_by(external_id=f"endgame:{k}").one()
            out = ladder.record_testout(s, p, True)
        assert out["finished"] and out["passed"]
        st = ladder.status(s)
        assert all(st["rungs"][i]["passed"] for i in range(4))
        assert st["rungs"][4]["open"] and not st["rungs"][5]["open"]
        assert st["testout"] is None
        # Nothing reached the SRS.
        assert not any(c.wins for c in s.query(SrsCard))


def test_a_miss_ends_the_test_out_and_retries_do_not_count(tmp_path):
    db = tmp_path / "m.db"
    seed_endgames(db)
    with session_factory(db)() as s:
        ladder.start_testout(s, 1)
        keys = ladder.RUNGS[1].keys
        p0 = s.query(Puzzle).filter_by(external_id=f"endgame:{keys[0]}").one()
        out = ladder.record_testout(s, p0, False)
        assert out["finished"] and not out["passed"]
        assert ladder.record_testout(s, p0, True) is None      # no test-out running now
        assert not ladder.status(s)["rungs"][1]["passed"]
        # A drill outside the rung being tested is not part of it.
        ladder.start_testout(s, 1)
        other = s.query(Puzzle).filter_by(external_id="endgame:lucena").one()
        assert ladder.record_testout(s, other, True) is None
