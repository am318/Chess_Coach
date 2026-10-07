"""The sparring bot measures what it plays.

No Stockfish here: a stub scorer stands in for the backend, so these test the
expectation arithmetic and the shared code path rather than the engine.
"""

import chess

from coach.profile.calibration import expected_cp_loss, human_bands
from coach.sparring.opponent import Strength


class _Stub:
    """A fixed root ranking: one good move and a spread of worse ones."""

    def __init__(self, losses=(0, 50, 150, 400, 900)):
        self.losses = losses

    def score_roots(self, board, budget):
        moves = list(board.legal_moves)[: len(self.losses)]
        return [(mv, -loss) for mv, loss in zip(moves, self.losses)]

    def best(self, board, budget):
        return self.score_roots(board, budget)[0]

    def line(self, board, budget, limit=8):
        return []

    def close(self):
        return None


BOARDS = [chess.Board()]


def test_weaker_settings_lose_more_centipawns():
    """The whole point of the rating table: lower Elo, looser sampling, more
    centipawns given away. If this ever inverts the table is broken."""
    stub = _Stub()
    losses = [expected_cp_loss(BOARDS, elo, stub, [stub.score_roots(BOARDS[0], 0)])
              for elo in (400, 800, 1200, 1600, 2000, 2400)]
    assert losses == sorted(losses, reverse=True), losses


def test_the_strongest_setting_nearly_always_finds_the_best_move():
    stub = _Stub()
    assert expected_cp_loss(BOARDS, 2400, stub, [stub.score_roots(BOARDS[0], 0)]) < 20


def test_expectation_is_deterministic():
    """Exact under the distribution, not sampled - so it needs no repetitions to
    stop being noisy."""
    stub = _Stub()
    ranked = [stub.score_roots(BOARDS[0], 0)]
    a = expected_cp_loss(BOARDS, 1200, stub, ranked)
    b = expected_cp_loss(BOARDS, 1200, stub, ranked)
    assert a == b


def test_strength_table_is_monotone_in_looseness():
    """Sanity on the table itself, independent of any position."""
    windows = [Strength.for_elo(e).window_cp for e in (400, 800, 1200, 1600, 2000, 2400)]
    temps = [Strength.for_elo(e).temperature for e in (400, 800, 1200, 1600, 2000, 2400)]
    assert windows == sorted(windows, reverse=True)
    assert temps == sorted(temps, reverse=True)


def test_human_bands_empty_database_is_not_an_error(tmp_path):
    assert human_bands(db_path=tmp_path / "empty.db") == []
