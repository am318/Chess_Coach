"""Search invariants that are not about finding the right move.

These cover the ways the search can hand back something the caller cannot use:
a mutated board, a mate score that means a different distance than it did when
it was stored, or a stalemate scored as a win.
"""

import chess
import pytest

from engine.search import MATE, MATE_IN_MAX, Searcher, SearchLimits, _score_from_tt, _score_to_tt

# Sharp enough that a 1ms budget lands the hard deadline mid-line rather than
# after a completed iteration.
_BUSY = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"


def test_search_hands_the_board_back_unchanged_when_it_runs_out_of_time():
    """A timed-out search used to return with the board several plies into a line
    it never played. The move it reported was then illegal in the position the
    caller still held, so Engine.get_move's own legality check threw it away and
    played the first legal move instead - in time trouble, which is exactly when
    the hard deadline fires."""
    # A spread of budgets: the deadline has to land in the middle of an
    # iteration, not tidily between two of them.
    for hard_ms in (40, 80, 160, 250, 400):
        board = chess.Board(_BUSY)
        before = board.fen()
        result = Searcher().search_position(
            board, SearchLimits(soft_time_ms=10**7, hard_time_ms=hard_ms, max_depth=64)
        )
        assert not board.move_stack, f"{hard_ms}ms left the board mid-line"
        assert board.fen() == before
        assert result.best_move in board.legal_moves


def test_rank_root_moves_hands_the_board_back_unchanged_when_it_runs_out_of_time():
    for hard_ms in (1, 60, 200):
        board = chess.Board(_BUSY)
        before = board.fen()
        Searcher().rank_root_moves(board, depth=6, hard_time_ms=hard_ms)
        assert not board.move_stack, f"{hard_ms}ms left the board mid-line"
        assert board.fen() == before


def test_mate_scores_round_trip_through_the_tt():
    """Stored raw, "mate in 2 from here" reads back as a different distance at a
    node a different number of plies from the root."""
    for ply in (0, 1, 7):
        score = MATE - 5
        assert _score_from_tt(_score_to_tt(score, ply), ply) == score
        assert _score_from_tt(_score_to_tt(-score, ply), ply) == -score
    # Ordinary scores are left alone.
    assert _score_to_tt(42, 9) == 42
    assert _score_from_tt(-42, 9) == -42


def test_a_forced_mate_reports_a_sane_distance():
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
    result = Searcher().search_position(
        board, SearchLimits(soft_time_ms=1500, hard_time_ms=2500, max_depth=12)
    )
    assert result.best_move.uci() == "a1a8"
    assert result.score >= MATE_IN_MAX
    assert (MATE - result.score + 1) // 2 == 1  # mate in one, not in some other number


def test_a_stalemating_move_is_not_played_while_a_queen_up():
    """1.Qc7 leaves Black with no legal move: a draw, not the +9 the material
    count says. The prunings that answer "am I far enough ahead to stop looking?"
    from the static score alone have to stand aside where stalemate is possible,
    or the search walks into it."""
    stalemated = chess.Board("k7/2Q5/8/8/8/8/8/7K b - - 0 1")
    assert not stalemated.is_check()
    assert not list(stalemated.legal_moves)

    board = chess.Board("k7/8/8/8/8/8/8/2Q4K w - - 0 1")
    result = Searcher().search_position(
        board, SearchLimits(soft_time_ms=1200, hard_time_ms=2000, max_depth=14)
    )
    assert result.best_move.uci() != "c1c7", "walked into stalemate holding a queen"


# ------------------------------------------------------ transposition table size


def test_the_tt_budget_is_expressed_in_megabytes():
    from engine.search import tt_entries_for_budget

    assert tt_entries_for_budget(256) == pytest.approx(813_000, rel=0.02)
    # Monotonic, and never so small the table is useless.
    assert tt_entries_for_budget(512) > tt_entries_for_budget(256)
    assert tt_entries_for_budget(0) >= 50_000


def test_a_full_table_is_aged_rather_than_wiped():
    """Clearing the whole table mid-search threw away the root entry, which is
    where search_position reads its best move from."""
    searcher = Searcher()
    searcher.max_tt_entries = 1000
    for key in range(1000):
        searcher._store_tt(key, 4, 0, 0, chess.Move.from_uci("e2e4"))
    assert len(searcher.tt) == 1000

    searcher._store_tt(10_000, 4, 0, 0, chess.Move.from_uci("d2d4"))
    assert len(searcher.tt) == 751, "expected the oldest quarter to go, not the lot"
    assert 999 in searcher.tt, "the newest entries must survive"
    assert 0 not in searcher.tt, "the oldest must not"
    assert 10_000 in searcher.tt


def test_the_search_stays_correct_while_the_table_is_being_evicted():
    board = chess.Board()
    searcher = Searcher(tt_budget_mb=2)     # small enough to evict repeatedly
    for _ in range(12):
        if board.is_game_over():
            break
        result = searcher.search_position(
            board, SearchLimits(soft_time_ms=120, hard_time_ms=250, max_depth=64)
        )
        assert result.best_move in board.legal_moves
        assert len(searcher.tt) <= searcher.max_tt_entries
        board.push(result.best_move)
