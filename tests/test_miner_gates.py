"""What the puzzle miner keeps and what it throws away.

The gates matter more than the mining: a puzzle that marks an equally good move
wrong teaches the player to distrust their own correct judgement, and a gate that
silently rejects a whole class of mistake means that mistake never gets drilled.
"""

from coach.puzzles import miner
from coach.store.db import Move


def _move(*, side="white", eval_before, second=None, cp_loss=300, mate_missed=False):
    return Move(ply=10, side=side, is_user_move=True, san="Qd2", uci="d1d2",
                fen_before="8/8/8/8/8/8/8/8 w - - 0 1", cp_loss=cp_loss,
                eval_before_cp=eval_before, second_best_cp=second,
                mate_missed=mate_missed, best_move_uci="d1d8", best_line="d1d8")


# ------------------------------------------------------- ordinary positions

def test_clear_best_move_passes_the_cp_gate():
    m = _move(eval_before=400, second=100)
    assert miner._uniqueness_cp(m) == 300
    assert not miner._is_mate_position(m)


def test_two_equal_moves_are_rejected():
    m = _move(eval_before=400, second=380)
    assert miner._uniqueness_cp(m) < miner._MIN_UNIQUENESS_CP


def test_black_moves_are_read_from_black_s_side():
    """Stored scores are White-POV; a good move for Black is a negative number."""
    m = _move(side="black", eval_before=-400, second=-100)
    assert miner._uniqueness_cp(m) == 300


# ------------------------------------------------------------ mate positions

def test_missed_mate_while_already_winning_is_kept():
    """The case the centipawn gate could never see.

    At +20 pawns a forced mate and a merely-crushing move both clamp to +1500,
    the gap collapses to zero, and every mate puzzle from a winning position was
    silently discarded - which is most of them, and the most instructive ones.
    """
    m = _move(eval_before=99_995, second=2_000, mate_missed=True)
    assert miner._uniqueness_cp(m) == 0          # centipawns cannot tell
    assert miner._is_mate_position(m)
    assert miner._mate_is_unique(m)              # raw scores can


def test_two_mating_moves_are_still_rejected():
    """If a second move also mates, the position has no single right answer."""
    m = _move(eval_before=99_995, second=99_993, mate_missed=True)
    assert miner._is_mate_position(m)
    assert not miner._mate_is_unique(m)


def test_being_mated_is_not_a_mate_position_for_the_mover():
    """Losing to a forced mate is not a missed mate; nothing was on offer."""
    m = _move(eval_before=-99_995)
    assert not miner._is_mate_position(m)


def test_unknown_second_line_defers_to_the_solver():
    """A single-PV pass can't rule out a second mate, and rejecting on that basis
    would throw away good material. `solve.py` re-checks alternatives live."""
    m = _move(eval_before=99_995, second=None, mate_missed=True)
    assert miner._mate_is_unique(m)
