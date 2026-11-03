"""Woodpecker cycles: a fixed set, solved faster each pass."""

from __future__ import annotations

import json
from itertools import pairwise

import pytest

from coach.puzzles import woodpecker as wp
from coach.store.db import Puzzle, SrsCard, session_factory, set_setting

FEN = "8/8/8/8/8/8/8/8 w - - 0 1"


def _fill(s, n, pillar="tactics", rating=1200, games=5):
    for i in range(n):
        s.add(Puzzle(source="own-game", fen=FEN, solution="e2e4", pillar=pillar,
                     rating_est=rating, game_id=i % games))
    s.flush()


def test_needs_enough_material(tmp_path):
    with session_factory(tmp_path / "a.db")() as s:
        _fill(s, 10)
        _fill(s, 40, pillar="quiet")          # quiet positions have no pattern to speed up
        with pytest.raises(ValueError, match="at least 30"):
            wp.build(s)


def test_set_is_fixed_interleaved_and_easier_than_you(tmp_path):
    with session_factory(tmp_path / "b.db")() as s:
        set_setting(s, "puzzle_rating", "1500")
        _fill(s, 40, rating=1250)
        _fill(s, 40, rating=2200)
        ws = wp.build(s, size=30)
        ids = json.loads(ws.puzzle_ids)
        assert len(ids) == 30
        chosen = [s.get(Puzzle, i) for i in ids]
        assert all(p.rating_est == 1250 for p in chosen)
        # neighbours come from different games
        assert all(a.game_id != b.game_id for a, b in pairwise(chosen))


def test_passes_repeat_the_same_order_and_finish(tmp_path):
    with session_factory(tmp_path / "c.db")() as s:
        _fill(s, 30)
        ws = wp.build(s, size=30)
        ws.max_passes = 3
        ids = json.loads(ws.puzzle_ids)
        for n_pass, ms in enumerate((9000, 5000, 4000), 1):
            seen = []
            while (pid := wp.next_puzzle_id(ws)) is not None and len(seen) < len(ids):
                seen.append(pid)
                st = wp.answer(s, ws, pid, True, ms)
                if st["finished"] or st["passes"][-1]["finished"]:
                    break
            assert seen == ids, f"pass {n_pass} changed the order"
        assert ws.finished_at is not None
        st = wp.state(ws)
        assert [p["accuracy"] for p in st["passes"]] == [1.0, 1.0, 1.0]
        assert st["passes"][2]["sec_per_puzzle"] == 4.0
        # Nothing was filed to spaced repetition.
        assert s.query(SrsCard).count() == 0


def test_answering_twice_does_not_double_count(tmp_path):
    with session_factory(tmp_path / "d.db")() as s:
        _fill(s, 30)
        ws = wp.build(s, size=30)
        pid = wp.next_puzzle_id(ws)
        wp.answer(s, ws, pid, True, 1000)
        st = wp.answer(s, ws, pid, False, 1000)
        assert st["passes"][0]["answered"] == 1 and st["passes"][0]["solved"] == 1
        with pytest.raises(ValueError):
            wp.answer(s, ws, 999_999, True, 1000)
