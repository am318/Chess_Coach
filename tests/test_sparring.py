"""Sparring opponent: strength scaling + always returns a legal move.

Uses the native backend so the tests need no Stockfish binary and don't spawn a
process per test.
"""

import chess
import pytest

from coach.sparring.backend import NativeBackend
from coach.sparring.opponent import SparringOpponent, Strength


@pytest.fixture
def backend():
    b = NativeBackend(max_depth=3, hard_time_ms=800)
    yield b
    b.close()


def test_strength_monotonic_in_elo():
    weak = Strength.for_elo(1000)
    strong = Strength.for_elo(2300)
    assert strong.depth > weak.depth
    assert strong.window_cp < weak.window_cp
    assert strong.slip_prob <= weak.slip_prob


def test_strength_reaches_beginner_levels():
    """The floor has to sit below the user's actual rating or the bot can never
    be a fair opponent - it used to clamp at 800."""
    beginner = Strength.for_elo(400)
    assert beginner.window_cp > Strength.for_elo(1200).window_cp
    assert beginner.slip_prob > Strength.for_elo(1200).slip_prob


def test_choose_returns_legal_move(backend):
    opp = SparringOpponent(target_elo=1100, seed=0, backend=backend)
    board = chess.Board()
    mv = opp.choose(board)
    assert mv in board.legal_moves


def test_weakness_bias_shapes_choice_direction(backend):
    opp = SparringOpponent(target_elo=1500, weak_pillar="endgame", seed=1, backend=backend)
    # A position with a queen trade available; endgame bias should be non-negative
    # and reward the simplifying capture.
    board = chess.Board("4q1k1/5ppp/8/8/8/8/5PPP/3Q2K1 w - - 0 1")
    trade = chess.Move.from_uci("d1d8")
    quiet = chess.Move.from_uci("g1h1")
    assert opp._weakness_bias(board, trade) >= opp._weakness_bias(board, quiet)


# ------------------------------------------------- where a sparring game starts


def test_ply_ladder_only_offers_start_points_at_or_before_the_requested_depth():
    from coach.sparring.opening_picker import _ply_ladder

    assert _ply_ladder(12) == [12, 8, 6, 4]
    assert _ply_ladder(8) == [8, 6, 4]
    assert _ply_ladder(2) == [2], "never empty - something has to be playable"


def _stub_candidate(monkeypatch, evals):
    """One weak-opening candidate whose eval at each ply is `evals`."""
    import chess.pgn

    from coach.sparring import opening_picker as op

    game = chess.pgn.Game()
    node = game
    for uci in ("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6",
                "b5a4", "g8f6", "e1g1", "f8e7", "f1e1", "b7b5"):
        node = node.add_variation(chess.Move.from_uci(uci))
    monkeypatch.setattr(op, "_worst_opening_games",
                        lambda *a, **k: [(120.0, "Ruy Lopez", "white", str(game), 7)])
    monkeypatch.setattr(op, "_repertoire_concentration", lambda *a, **k: {"white": 0.1})
    monkeypatch.setattr(op, "_eval_after_plies",
                        lambda gid, plies, colour: evals.get(plies))
    return op


def test_a_playable_line_is_started_at_full_depth(monkeypatch):
    import random

    op = _stub_candidate(monkeypatch, {12: -140, 8: -20, 6: 0, 4: 10})
    r = op.pick_start(None, "white", rng=random.Random(0), mode="weakness")
    assert r["source"] == "weak-opening"
    assert r["plies"] == 12, "uncomfortable is the point - don't trim it away"
    assert r["start_cp"] == -140


def test_an_already_lost_line_is_backed_up_rather_than_abandoned(monkeypatch):
    """The rep is worth having; the position it was replayed into is not. Trim
    back to where the game was still a game, keeping the same opening."""
    import random

    op = _stub_candidate(monkeypatch, {12: -618, 8: -320, 6: -15, 4: -45})
    r = op.pick_start(None, "white", rng=random.Random(0), mode="weakness")
    assert r["source"] == "weak-opening"
    assert r["opening"] == "Ruy Lopez"
    assert r["plies"] == 6
    assert r["start_cp"] == -15


def test_a_line_that_is_lost_from_the_start_falls_back_to_the_balanced_pool(monkeypatch):
    import random

    op = _stub_candidate(monkeypatch, dict.fromkeys((12, 8, 6, 4), -900))
    r = op.pick_start(None, "white", rng=random.Random(0), mode="weakness")
    assert r["source"] == "breadth"


def test_a_winning_position_played_badly_is_still_a_rep_worth_having(monkeypatch):
    """The filter is one-sided on purpose: being +12 and still scoring badly in
    the opening is exactly the thing worth practising."""
    import random

    op = _stub_candidate(monkeypatch, {12: 1232, 8: 418, 6: 267, 4: 88})
    r = op.pick_start(None, "white", rng=random.Random(0), mode="weakness")
    assert r["plies"] == 12


def test_an_unanalysed_ply_is_not_treated_as_lost(monkeypatch):
    import random

    op = _stub_candidate(monkeypatch, {})     # nothing analysed
    r = op.pick_start(None, "white", rng=random.Random(0), mode="weakness")
    assert r["source"] == "weak-opening"
    assert r["plies"] == 12


# ------------------------------------- where a weak-opening rep starts from


def _picker_db(monkeypatch, tmp_path):
    """Point the opening picker's session factory at a throwaway database."""
    from coach.sparring import opening_picker as op
    from coach.store.db import session_factory

    factory = session_factory(tmp_path / "openings.db")
    monkeypatch.setattr(op, "session_factory", lambda *a, **k: factory)
    return op, factory


def _game(session, *, source, opening, colour="white", pgn, phases=("opening",) * 8,
          cp_loss=150.0):
    """An analysed game with `len(phases)` scored user moves."""
    from coach.store.db import Game, Move

    g = Game(source=source, source_id=f"{source}-{opening}-{pgn[:12]}", account="me",
             color=colour, result="loss", opening=opening, pgn=pgn, analyzed=True,
             ply_count=len(phases) * 2)
    session.add(g)
    session.flush()
    for i, phase in enumerate(phases):
        session.add(Move(game_id=g.id, ply=i * 2, side=colour, uci="e2e4", san="e4",
                         fen_before=chess.STARTING_FEN, is_user_move=True,
                         cp_loss=cp_loss, phase=phase, eval_before_cp=0))
    session.commit()
    return g.id


# A real game starts at move 1; a sparring game carries the book position it was
# dealt, which is what used to make the next start six moves deeper again.
_REAL_PGN = '[Event "?"]\n[Result "*"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 *\n'
_SPAR_PGN = ('[Event "Coach sparring"]\n[Result "*"]\n'
             '[FEN "r1bqkb1r/1ppp1ppp/p1n2n2/4p3/B3P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 5"]\n'
             '[SetUp "1"]\n\n5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O 9. h3 Nb8 10. d4 Nbd7 *\n')


def test_the_replayed_game_is_never_one_of_the_coachs_own(monkeypatch, tmp_path):
    """A sparring PGN begins at the book position it was dealt, so replaying
    twelve plies of it starts the next game twelve plies further on - and the
    one after that further still. Measured on real data the Four Knights walked
    from move 7 to 13 to 19. Only games that start at move 1 may supply moves."""
    op, factory = _picker_db(monkeypatch, tmp_path)
    with factory() as s:
        real = _game(s, source="lichess", opening="Ruy Lopez", pgn=_REAL_PGN)
        spar = _game(s, source="sparring", opening="Ruy Lopez", pgn=_SPAR_PGN)
    assert spar > real, "the sparring game is the newer one, and used to win"

    [(_, opening, _, pgn, gid)] = op._worst_opening_games(None, None)
    assert opening == "Ruy Lopez"
    assert gid == real
    assert "SetUp" not in pgn


def test_a_line_only_ever_sparred_has_nothing_to_replay(monkeypatch, tmp_path):
    op, factory = _picker_db(monkeypatch, tmp_path)
    with factory() as s:
        _game(s, source="sparring", opening="Vienna Game", pgn=_SPAR_PGN)
    assert op._worst_opening_games(None, None) == []


def test_middlegame_moves_are_not_averaged_into_an_opening_score(monkeypatch, tmp_path):
    """`phase` is read off the board's own move number, so it stays right for a
    game that began halfway down a line. The ply index does not, and the
    "or ply < 24" fallback let a sparring game's middlegame in as opening play."""
    op, factory = _picker_db(monkeypatch, tmp_path)
    with factory() as s:
        _game(s, source="lichess", opening="Ruy Lopez", pgn=_REAL_PGN,
              phases=("opening",) * 8, cp_loss=20.0)
        # Same opening, but every scored move is a middlegame move early in a
        # PGN that started deep: in range of the old ply filter, and terrible.
        _game(s, source="sparring", opening="Ruy Lopez", colour="white",
              pgn=_SPAR_PGN, phases=("middlegame",) * 8, cp_loss=900.0)

    [(avg, *_)] = op._worst_opening_games(None, None)
    assert avg == 20.0, "only opening-phase moves decide an opening's score"


def test_a_weak_opening_rep_starts_in_the_opening(monkeypatch, tmp_path):
    """End to end: whatever the picker hands back begins early enough to still
    be the opening it claims to be."""
    import random

    op, factory = _picker_db(monkeypatch, tmp_path)
    monkeypatch.setattr(op, "_repertoire_concentration", lambda *a, **k: {"white": 0.1})
    with factory() as s:
        _game(s, source="lichess", opening="Ruy Lopez", pgn=_REAL_PGN)
        _game(s, source="sparring", opening="Ruy Lopez", pgn=_SPAR_PGN)

    r = op.pick_start(None, "white", rng=random.Random(0), mode="weakness")
    assert r["source"] == "weak-opening"
    # Twelve plies of the sparring game would have begun this at move 11.
    assert chess.Board(r["fen"]).fullmove_number <= 7
