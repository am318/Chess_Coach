"""Post-game review assembly."""

import chess

from coach.coaching.review import _move_number


def test_move_numbers_follow_the_board_not_the_move_list():
    """Sparring games start from a book position the opening picker chose, so
    ply 0 is not move 1. Numbering from the ply index made every key moment in
    those reviews disagree with the board it was describing."""
    start = "r1bqkbnr/ppp1pppp/1n6/1P1p4/8/2N5/P1PPPPPP/R1BQKBNR w KQkq - 3 4"
    assert _move_number(start, ply=0) == 4

    board = chess.Board(start)
    board.push_san("Nf3")
    board.push_san("f6")
    assert _move_number(board.fen(), ply=2) == 5


def test_move_numbers_still_work_from_the_initial_position():
    assert _move_number(chess.STARTING_FEN, ply=0) == 1


def test_a_malformed_fen_falls_back_to_the_ply_index():
    assert _move_number("not a fen", ply=6) == 4
