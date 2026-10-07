"""Repertoire mining and endgame drill play, without needing a chess engine."""

import chess
import pytest

from coach.drills import repertoire as rep
from coach.drills.adjudicate import MATE_CP
from coach.drills.play import HOLD_MOVES, start, step

# ------------------------------------------------------------------ repertoire

def _stats(user_moves=None, opponent_moves=None, engine_best=None, openings=None):
    st = rep._PositionStats()
    for uci, (count, total_cp) in (user_moves or {}).items():
        st.user_moves[uci] = [count, total_cp]
    for uci, count in (opponent_moves or {}).items():
        st.opponent_moves[uci] = count
    st.engine_best = engine_best
    for name, n in (openings or {}).items():
        st.openings[name] = n
    return st


def test_your_own_move_is_kept_when_it_holds_up():
    """A repertoire trainer should reinforce what you already play - the point is
    to make it automatic, not to hand you someone else's opening."""
    st = _stats(user_moves={"e2e4": (20, 20 * 15.0)}, engine_best="d2d4")
    move, why = rep._recommend(st)
    assert move == "e2e4" and why == "yours"


def test_your_move_is_corrected_only_when_it_actually_costs_you():
    st = _stats(user_moves={"f2f3": (12, 12 * 180.0)}, engine_best="g1f3")
    move, why = rep._recommend(st)
    assert move == "g1f3" and why == "corrected"


def test_a_bad_move_with_no_engine_suggestion_is_left_alone():
    st = _stats(user_moves={"f2f3": (12, 12 * 180.0)}, engine_best=None)
    assert rep._recommend(st) == ("f2f3", "yours")


def test_most_played_move_wins_ties_on_cost():
    st = _stats(user_moves={"e2e4": (20, 20 * 30.0), "d2d4": (3, 0.0)})
    assert rep._recommend(st)[0] == "e2e4"


def test_positions_reached_by_transposition_are_the_same_position():
    a = chess.Board(); b = chess.Board()
    for u in ("e2e4", "e7e5", "g1f3"):
        a.push(chess.Move.from_uci(u))
    for u in ("g1f3", "e7e5", "e2e4"):
        b.push(chess.Move.from_uci(u))
    assert rep._fen_key(a.fen()) == rep._fen_key(b.fen())


def test_opening_name_is_the_most_common_one_not_the_first_seen():
    st = _stats(openings={"Sicilian Defense": 2, "Four Knights Game": 9})
    assert st.opening == "Four Knights Game"


def test_no_games_means_no_invented_repertoire():
    st = _stats()
    assert rep._recommend(st) == (None, "")


def _rep_db(tmp_path):
    from coach.store.db import session_factory

    return session_factory(tmp_path / "repertoire.db")


def _played(session, *, source, colour, fens, is_user=True, uci="e2e4", opening="Ruy Lopez"):
    """A game whose scored moves sit at the given positions, from ply 0."""
    from coach.store.db import Game, Move

    g = Game(source=source, source_id=f"{source}-{fens[0][:24]}", account="me",
             color=colour, result="loss", opening=opening, analyzed=True,
             pgn="*", ply_count=len(fens))
    session.add(g)
    session.flush()
    for ply, fen in enumerate(fens):
        session.add(Move(game_id=g.id, ply=ply, side=colour, san="e4", uci=uci,
                         fen_before=fen, is_user_move=is_user, cp_loss=10.0))
    session.commit()
    return g.id


def test_the_tree_bound_is_a_move_number_not_a_ply_index():
    """`Move.ply` counts from the start of the stored game, which is the start
    of the opening only for a game that began at move one."""
    shallow = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    edge = shallow.replace(" 0 1", f" 0 {rep.MAX_MOVE_NUMBER}")
    deep = shallow.replace(" 0 1", f" 0 {rep.MAX_MOVE_NUMBER + 1}")
    assert rep._within_tree(shallow)
    assert rep._within_tree(edge)
    assert not rep._within_tree(deep)
    assert not rep._within_tree("not a fen")


def test_a_game_that_began_mid_line_contributes_no_middlegame_positions(tmp_path):
    """A PGN dealt from a book position has its first plies land wherever it was
    dealt, so a ply filter let a move-20 position in as opening play. The drill
    tree walks from move one and can never reach it."""
    factory = _rep_db(tmp_path)
    with factory() as s:
        _played(s, source="lichess", colour="white", fens=[
            # ply 0 and 1 of a game that started at move 20.
            "r1bq1rk1/pp2bppp/2n1pn2/2pp4/3P1B2/2PBPN2/PP1N1PPP/R2Q1RK1 w - - 0 20",
            "r1bq1rk1/pp2bppp/2n1pn2/2pp4/3P1B2/2PBPN2/PP1N1PPP/R2Q1RK1 b - - 0 20",
        ])
        assert rep._collect(s, "white", None) == {}


def test_sparring_games_are_the_training_not_the_evidence(tmp_path):
    """Their opening moves were dealt by the picker rather than chosen, and the
    replies came from a deliberately weak bot. Counting them would have the
    trainer grading its own homework."""
    factory = _rep_db(tmp_path)
    start = chess.STARTING_FEN
    with factory() as s:
        _played(s, source="sparring", colour="white", fens=[start])
        assert rep._collect(s, "white", None) == {}

        _played(s, source="chesscom", colour="white", fens=[start])
        collected = rep._collect(s, "white", None)
    assert len(collected) == 1
    assert next(iter(collected.values())).visits == 1, "the real game, and only it"


# --------------------------------------------------------------- endgame drills

class StubScorer:
    """Returns a fixed score, so drill logic is testable with no engine."""

    def __init__(self, cp):
        self.cp = cp

    def best(self, board, budget):
        move = next(iter(board.legal_moves), None)
        return (move, self.cp) if move else None

    def score_roots(self, board, budget):
        return [(m, self.cp) for m in board.legal_moves]

    def close(self):
        return None


LUCENA = "1K1k4/1P6/8/8/8/8/r7/2R5 w - - 0 1"
PHILIDOR = "8/8/4k3/8/R3P3/4K3/8/5r2 b - - 0 1"


def test_start_reports_whose_move_it_is():
    st = start(LUCENA)
    assert st["user_colour"] == "white"
    assert st["status"] == "continue"
    assert st["dests"]


def test_illegal_and_malformed_moves_are_rejected_not_scored():
    assert step(LUCENA, "e2e4", "win", "key-move", "white")["status"] == "error"
    assert step(LUCENA, "zzzz", "win", "key-move", "white")["status"] == "error"


def test_key_move_accepts_anything_that_keeps_the_win():
    """There is more than one winning move in a Lucena; only the result matters."""
    r = step(LUCENA, "c1c4", "win", "key-move", "white", scorer=StubScorer(-900))
    assert r["status"] == "success"


def test_key_move_rejects_a_move_that_throws_the_win_away():
    # Stub says the position is level *for the side to move* after our move, i.e.
    # we no longer have a winning advantage.
    r = step(LUCENA, "c1c4", "win", "key-move", "white", scorer=StubScorer(0))
    assert r["status"] == "failed"
    assert "draw" in r["message"].lower()


def test_a_draw_goal_survives_an_equal_position():
    r = step(PHILIDOR, "f1f6", "draw", "key-move", "black", scorer=StubScorer(0))
    assert r["status"] == "success"


def test_a_draw_goal_fails_when_the_position_is_lost():
    # Positive score for the side to move (White) means Black is losing.
    r = step(PHILIDOR, "f1a1", "draw", "key-move", "black", scorer=StubScorer(900))
    assert r["status"] == "failed"


def test_delivering_mate_ends_a_win_drill_successfully():
    # Black is mated by Ra8#.
    fen = "7k/8/6K1/8/8/8/8/R7 w - - 0 1"
    r = step(fen, "a1a8", "win", "play-out", "white", scorer=StubScorer(0))
    assert r["status"] == "success" and "Mate" in r["message"]


def test_stalemating_fails_a_win_drill_and_passes_a_draw_drill():
    fen = "7k/5Q2/8/8/8/8/8/6K1 w - - 0 1"   # Qf7-g6?? is stalemate
    lost = step(fen, "f7g6", "win", "play-out", "white", scorer=StubScorer(0))
    assert lost["status"] == "failed" and "talemate" in lost["message"]
    held = step(fen, "f7g6", "draw", "play-out", "white", scorer=StubScorer(0))
    assert held["status"] == "success"


def test_holding_long_enough_wins_a_draw_drill():
    r = step(PHILIDOR, "f1f6", "draw", "play-out", "black",
             moves_played=HOLD_MOVES - 1, scorer=StubScorer(0))
    assert r["status"] == "success"
    assert str(HOLD_MOVES) in r["message"]


def test_a_mate_score_is_read_as_winning():
    r = step(LUCENA, "c1c4", "win", "key-move", "white", scorer=StubScorer(-MATE_CP + 5))
    assert r["status"] == "success"


def test_without_an_engine_a_playout_still_progresses():
    """No Stockfish: the drill must not crash or silently mark you wrong."""
    class NoEngine(StubScorer):
        def best(self, board, budget):
            raise RuntimeError("no engine")

        def score_roots(self, board, budget):
            raise RuntimeError("no engine")

    r = step(LUCENA, "c1c4", "win", "play-out", "white", scorer=NoEngine(0))
    assert r["status"] == "continue"


@pytest.mark.parametrize("goal", ["win", "draw"])
def test_result_payload_is_renderable(goal):
    r = step(LUCENA, "c1c4", goal, "key-move", "white", scorer=StubScorer(0))
    for key in ("status", "fen", "user_colour", "played_san", "moves_played", "message"):
        assert key in r
    chess.Board(r["fen"])   # must be a legal FEN
