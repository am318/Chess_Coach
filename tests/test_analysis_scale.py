"""Bounded evaluations, puzzle soundness, and outcome-derived SRS grades.

These all guard the same class of bug: a quantity that is fine in the common
case and absurd at the extremes, quietly poisoning everything downstream.
"""

from types import SimpleNamespace

from coach.analysis.annotate import EVAL_CLAMP_CP, clamped_cp_loss
from coach.puzzles.miner import _MIN_UNIQUENESS_CP, _uniqueness_cp
from coach.puzzles.srs import AGAIN, EASY, GOOD, HARD, grade_from_outcome

MATE = 100_000


# --------------------------------------------------------------- cp-loss clamp

def test_ordinary_cp_loss_is_untouched():
    assert clamped_cp_loss(120, 40, mover_is_white=True) == 80
    # Black's move: White-POV evals invert.
    assert clamped_cp_loss(-120, -40, mover_is_white=False) == 80


def test_a_missed_forced_mate_is_bounded():
    # Was winning by mate, played a move that still leaves +13 pawns.
    loss = clamped_cp_loss(MATE, 1296, mover_is_white=True)
    assert 0 < loss <= 2 * EVAL_CLAMP_CP
    assert loss < 1000, "a missed mate in an already-winning position is not a 1000-pawn error"


def test_walking_into_mate_is_bounded_too():
    loss = clamped_cp_loss(50, -MATE, mover_is_white=True)
    assert loss <= 2 * EVAL_CLAMP_CP


def test_improving_your_position_is_never_negative_loss():
    assert clamped_cp_loss(10, 400, mover_is_white=True) == 0


def test_missing_evals_return_none():
    assert clamped_cp_loss(None, 10, mover_is_white=True) is None
    assert clamped_cp_loss(10, None, mover_is_white=True) is None


# ------------------------------------------------------------ puzzle soundness

def _move(side, before, second):
    return SimpleNamespace(side=side, eval_before_cp=before, second_best_cp=second)


def test_uniqueness_is_positive_for_both_colours():
    # White: best +300, second +100 -> a clear 200cp gap.
    assert _uniqueness_cp(_move("white", 300, 100)) == 200
    # Black: the same position mirrored in White-POV terms.
    assert _uniqueness_cp(_move("black", -300, -100)) == 200


def test_two_mating_moves_are_not_a_valid_puzzle():
    # Mate in 2 vs mate in 4: both win, so there is no single right answer.
    gap = _uniqueness_cp(_move("white", MATE, MATE - 4))
    assert gap < _MIN_UNIQUENESS_CP


def test_mate_versus_merely_winning_is_a_valid_puzzle():
    gap = _uniqueness_cp(_move("white", MATE, 120))
    assert gap >= _MIN_UNIQUENESS_CP


def test_two_equal_moves_are_rejected():
    assert _uniqueness_cp(_move("white", 210, 190)) < _MIN_UNIQUENESS_CP


def test_uniqueness_needs_both_evaluations():
    assert _uniqueness_cp(_move("white", 300, None)) is None
    assert _uniqueness_cp(_move("white", None, 100)) is None


# ---------------------------------------------------------------- auto-grading

def _card(avg_ms=None):
    return SimpleNamespace(avg_ms=avg_ms)


def test_a_miss_is_always_again():
    assert grade_from_outcome(_card(20_000), solved=False, elapsed_ms=1_000) == AGAIN


def test_first_sight_fast_is_easy_slow_is_good():
    assert grade_from_outcome(_card(None), solved=True, elapsed_ms=3_000) == EASY
    assert grade_from_outcome(_card(None), solved=True, elapsed_ms=45_000) == GOOD


def test_graded_against_your_own_pace_on_this_card():
    card = _card(avg_ms=20_000)
    assert grade_from_outcome(card, solved=True, elapsed_ms=5_000) == EASY
    assert grade_from_outcome(card, solved=True, elapsed_ms=20_000) == GOOD
    assert grade_from_outcome(card, solved=True, elapsed_ms=60_000) == HARD


def test_untimed_solve_falls_back_to_good():
    assert grade_from_outcome(_card(20_000), solved=True, elapsed_ms=None) == GOOD
