"""Time spent per move, and the criticality test it feeds."""

from dataclasses import dataclass

from coach.analysis.timing import (
    compute_seconds_spent,
    is_critical,
    parse_time_control,
)


@dataclass
class _M:
    ply: int
    side: str
    clock_ms: int | None
    seconds_spent: float | None = None


def test_parse_time_control():
    assert parse_time_control("600+5") == (600, 5)
    assert parse_time_control("300") == (300, 0)
    assert parse_time_control("1/259200") == (None, 0)   # correspondence
    assert parse_time_control(None) == (None, 0)
    assert parse_time_control("-") == (None, 0)


def test_seconds_spent_uses_same_side_delta_plus_increment():
    moves = [
        _M(0, "white", 300_000),
        _M(1, "black", 300_000),
        _M(2, "white", 292_000),   # 8s of clock gone, +3s increment => 11s thought
        _M(3, "black", 297_000),   # 3s gone, +3 => 6s
    ]
    filled = compute_seconds_spent(moves, "300+3")
    assert filled == 2
    # The first move of each side has nothing to difference against.
    assert moves[0].seconds_spent is None and moves[1].seconds_spent is None
    assert moves[2].seconds_spent == 11.0
    assert moves[3].seconds_spent == 6.0


def test_seconds_spent_skips_impossible_readings():
    """A clock that goes up, or a 'think' longer than the whole game, means the
    comments are unreliable - better no datapoint than a wrong one."""
    moves = [
        _M(0, "white", 300_000),
        _M(2, "white", 400_000),   # clock went up: takeback or server adjustment
    ]
    assert compute_seconds_spent(moves, "300+0") == 0
    assert moves[1].seconds_spent is None


def test_seconds_spent_ignores_moves_without_clocks():
    moves = [_M(0, "white", None), _M(2, "white", None)]
    assert compute_seconds_spent(moves, "300+0") == 0


def test_is_critical_by_forcingness_or_by_gap():
    assert is_critical(0.9, None, None)             # forcing position
    assert is_critical(0.1, 300, 20)                # 280cp between the top two
    assert not is_critical(0.1, 30, 20)             # quiet, and nothing to choose
    assert not is_critical(None, None, None)
