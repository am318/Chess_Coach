"""Maia integration, against a fake: lc0 isn't available in CI."""

from __future__ import annotations

import chess

from coach.analysis import human
from coach.sparring.opponent import SparringOpponent
from coach.store.db import Game, Move, session_factory

# What lc0 prints with VerboseMoveStats, one line per move (format from lc0 0.30).
LC0_LINES = [
    "g1f3  (159 ) N:       0 (+ 0) (P: 31.50%) (WL:  -.-----) (D: -.---) (M:  -.-) (Q: -0.0) (V:  -.----)",
    "e2e4  (322 ) N:       0 (+ 0) (P: 52.25%) (WL:  -.-----) (D: -.---) (M:  -.-) (Q: -0.0) (V:  -.----)",
    "d2d4  (293 ) N:       0 (+ 0) (P: 10.25%) (WL:  -.-----) (D: -.---) (M:  -.-) (Q: -0.0) (V:  -.----)",
    "node  ( 20 ) N:       1 (+ 0) (P: 100.0%) (WL:  0.04) (D: 0.31) (M: 133) (Q: 0.04) (V: 0.04)",
]


def test_parse_policy_reads_lc0_verbose_stats():
    pol = human.parse_policy(LC0_LINES, chess.Board())
    assert set(pol) == {"g1f3", "e2e4", "d2d4"}            # the "node" summary is ignored
    assert abs(sum(pol.values()) - 1.0) < 1e-9
    assert max(pol, key=pol.get) == "e2e4"


def test_castling_in_lc0_notation_is_translated():
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    pol = human.parse_policy(["e1h1  (1 ) N: 0 (+ 0) (P: 60.00%)",
                              "e1a1  (1 ) N: 0 (+ 0) (P: 40.00%)"], board)
    assert set(pol) == {"e1g1", "e1c1"}


def test_weights_are_matched_to_the_nearest_rating(tmp_path):
    for r in (1100, 1500, 1900):
        (tmp_path / f"maia-{r}.pb.gz").write_bytes(b"x")
    assert human.weights_path(1530, tmp_path).name == "maia-1500.pb.gz"
    assert human.weights_path(700, tmp_path).name == "maia-1100.pb.gz"
    assert human.weights_path(1700, tmp_path).name in ("maia-1500.pb.gz", "maia-1900.pb.gz")
    assert human.weights_path(1500, tmp_path / "missing") is None


def test_not_available_without_lc0(tmp_path):
    p = human.MaiaPredictor(lc0_path="definitely-not-lc0", weights_dir=tmp_path)
    assert not p.available()
    assert human.classify(0.4) == "typical" and human.classify(0.01) == "personal"
    assert human.classify(None) is None


class FakeMaia:
    def __init__(self, table):
        self.table = table

    def policy(self, board, rating):
        return self.table.get(board.fen().split(" ")[0], {"e2e4": 1.0})

    def top(self, board, rating):
        pol = self.policy(board, rating)
        uci, p = max(pol.items(), key=lambda kv: kv[1])
        return chess.Move.from_uci(uci), p


def test_tag_mistakes_stores_how_human_the_move_was(tmp_path):
    db = tmp_path / "h.db"
    with session_factory(db)() as s:
        g = Game(source="lichess", source_id="1", account="me", color="white", result="loss",
                 pgn="", white_elo=1500)
        s.add(g)
        s.flush()
        s.add(Move(game_id=g.id, ply=0, side="white", is_user_move=True, san="d4", uci="d2d4",
                   fen_before=chess.STARTING_FEN, error_bucket="mistake", cp_loss=150))
        s.add(Move(game_id=g.id, ply=2, side="white", is_user_move=True, san="Nf3", uci="g1f3",
                   fen_before=chess.STARTING_FEN, error_bucket="ok", cp_loss=0))
        s.commit()
    fake = FakeMaia({chess.Board().fen().split(" ")[0]: {"e2e4": 0.7, "d2d4": 0.3}})
    assert human.tag_mistakes(db, predictor=fake) == 1      # only the mistake
    with session_factory(db)() as s:
        m = s.query(Move).filter_by(uci="d2d4").one()
        assert m.human_p == 0.3 and m.human_top == "e2e4"


class FixedScorer:
    """Scores every reply 0 except the ones listed."""

    def __init__(self, bad):
        self.bad = bad

    def score_roots(self, board, budget, limit=None):
        out = [(m, -400 if m.uci() in self.bad else 0) for m in board.legal_moves]
        return sorted(out, key=lambda t: -t[1])


def test_the_bot_leans_towards_traps_humans_fall_into():
    board = chess.Board()
    a, b = chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4")
    after_a = board.copy()
    after_a.push(a)
    after_b = board.copy()
    after_b.push(b)
    # After e4 the likeliest human reply (f6) is a blunder; after d4 it is fine.
    maia = FakeMaia({after_a.fen().split(" ")[0]: {"f7f6": 0.6, "e7e5": 0.4},
                     after_b.fen().split(" ")[0]: {"d7d5": 0.8, "g8f6": 0.2}})
    opp = SparringOpponent(backend=FixedScorer({"f7f6"}), predictor=maia, seed=1)
    w = opp._trap_weights(board, [(a, 30), (b, 30)], [1.0, 1.0])
    assert w[0] > 1.5 and w[1] == 1.0
