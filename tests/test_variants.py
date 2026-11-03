"""Same idea, new position."""

from __future__ import annotations

from datetime import timedelta

from coach.puzzles import srs, variants
from coach.store.db import Puzzle, SrsCard, session_factory

FEN = "8/8/8/8/8/8/8/8 w - - 0 1"


def _p(s, motif, game_id, source="own-game", pillar="tactics", rating=1400):
    p = Puzzle(source=source, fen=FEN, solution="e2e4", pillar=pillar, motif=motif,
               game_id=game_id, rating_est=rating)
    s.add(p)
    s.flush()
    return p


def test_a_sibling_shares_the_idea_but_not_the_game(tmp_path):
    with session_factory(tmp_path / "v.db")() as s:
        orig = _p(s, "fork", None)
        orig.game_id = None
        same_game = _p(s, "fork", None)
        other_motif = _p(s, "skewer", None)
        lib = _p(s, "fork", None, source="library")
        own = _p(s, "pawn-fork,fork", None)
        far = _p(s, "fork", None, rating=2400)
        for p, gid in ((orig, 1), (same_game, 1), (other_motif, 2), (lib, None),
                       (own, 3), (far, 4)):
            p.game_id = gid
        s.flush()
        pick = variants.find_variant(s, orig)
        assert pick is own            # own games first, same idea, near difficulty


def test_library_fills_in_when_own_games_have_nothing(tmp_path):
    with session_factory(tmp_path / "l.db")() as s:
        orig = _p(s, "deflection", 1)
        lib = _p(s, "deflection", None, source="library")
        assert variants.find_variant(s, orig) is lib


def test_scheduled_only_once_the_position_is_well_known(tmp_path):
    with session_factory(tmp_path / "s.db")() as s:
        orig = _p(s, "fork", 1)
        sib = _p(s, "fork", 2)
        card = srs.ensure_card(s, orig)
        sib_card = srs.ensure_card(s, sib)
        sib_card.due_at = srs._now() + timedelta(days=20)
        sib_card.reps = 3

        card.reps = 1
        assert variants.maybe_schedule(s, orig, card, srs.GOOD) is None   # too new
        card.reps = 3
        assert variants.maybe_schedule(s, orig, card, srs.AGAIN) is None  # failed
        got = variants.maybe_schedule(s, orig, card, srs.GOOD)
        assert got is sib and sib.variant_of == orig.id
        assert s.get(SrsCard, sib_card.id).due_at <= srs._now()

        variants.record_transfer(s, sib, True)
        assert sib.variant_of is None
        assert variants.summary(s)["all"] == {"n": 1, "rate": 1.0}
        assert variants.summary(s)["fork"]["n"] == 1


def test_weak_motif_import_filter(tmp_path):
    from coach.puzzles.library import import_lichess_csv

    csv = tmp_path / "p.csv"
    rows = ["PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags"]
    for i, theme in enumerate(["fork", "skewer", "fork", "pin"]):
        rows.append(f"id{i},{FEN},e2e4 e7e5,1500,80,90,100,{theme} middlegame,u,")
    csv.write_text("\n".join(rows) + "\n")
    n = import_lichess_csv(csv, user_elo=1500, db_path=tmp_path / "i.db",
                           want_motifs={"fork"}, max_per_motif=1)
    assert n == 1


def test_transfer_tests_are_capped(tmp_path):
    with session_factory(tmp_path / "cap.db")() as s:
        orig = _p(s, "fork", 1)
        card = srs.ensure_card(s, orig)
        card.reps = 3
        for g in range(2, 4):
            _p(s, "fork", g)
        first = variants.maybe_schedule(s, orig, card, srs.GOOD)
        assert first is not None
        # The same original never has two waiting at once.
        assert variants.maybe_schedule(s, orig, card, srs.GOOD) is None
        # And there is a ceiling across the library.
        for i in range(variants.MAX_PENDING):
            _p(s, "skewer", 100 + i).variant_of = 999
        other = _p(s, "pin", 50)
        oc = srs.ensure_card(s, other)
        oc.reps = 3
        _p(s, "pin", 51)
        assert variants.maybe_schedule(s, other, oc, srs.GOOD) is None
