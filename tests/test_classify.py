"""Unit tests for the pure classification helpers (no Stockfish needed)."""

import chess

from coach.analysis.classify import (
    endgame_type,
    error_bucket,
    forcingness,
    game_phase,
)
from coach.config import ProfileCfg

CFG = ProfileCfg()


def test_game_phase_opening_and_endgame():
    assert game_phase(chess.Board()) == "opening"
    assert game_phase(chess.Board("8/5k2/8/8/3K4/8/4P3/8 w - - 0 40")) == "endgame"


def test_endgame_type():
    assert endgame_type(chess.Board("8/5k2/8/8/3K4/8/4P3/8 w - - 0 40")) == "king-pawn"
    assert endgame_type(chess.Board("8/5k2/8/8/3K4/8/4P3/4R3 w - - 0 40")) == "rook"
    assert endgame_type(chess.Board()) is None


def test_error_bucket_thresholds():
    assert error_bucket(0, CFG) == "ok"
    assert error_bucket(60, CFG) == "inaccuracy"
    assert error_bucket(150, CFG) == "mistake"
    assert error_bucket(400, CFG) == "blunder"
    assert error_bucket(None, CFG) == "unknown"


def test_forcingness_bounds_and_check():
    quiet = chess.Board("8/5k2/8/8/3K4/8/4P3/8 w - - 0 40")
    sharp = chess.Board("r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4")
    fq = forcingness(quiet, 20, 15)
    fs = forcingness(sharp, 900, 100)
    assert 0.0 <= fq <= 1.0 and 0.0 <= fs <= 1.0
    assert fs > fq  # being in check / one legal reply is more forcing than a K+P shuffle
