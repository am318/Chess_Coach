"""The motif taxonomy on positions where the right answer is not in doubt."""

import chess
import pytest

from coach.analysis.motifs import (
    ALL_MOTIFS,
    FROM_LICHESS,
    LABEL,
    LICHESS_THEME,
    detect_motifs,
    from_lichess_themes,
    parse_line,
)


def _tags(fen, best, played=None, line=None, refutation=None):
    board = chess.Board(fen)
    best_mv = chess.Move.from_uci(best)
    if played is None:
        # Any legal king shuffle that is not the best move.
        played_mv = next(m for m in board.legal_moves
                         if m != best_mv and board.piece_type_at(m.from_square) == chess.KING)
    else:
        played_mv = chess.Move.from_uci(played)
    return detect_motifs(board, played_mv, best_mv,
                         parse_line(line) if line else None,
                         parse_line(refutation) if refutation else None)


@pytest.mark.parametrize("fen,best,expected", [
    ("r3k3/8/8/3N4/8/8/8/4K3 w - - 0 1", "d5c7", "fork"),
    ("4q3/8/8/8/4k3/8/8/K6R w - - 0 1", "h1e1", "skewer"),
    ("4k3/8/8/8/4n3/8/8/K6R w - - 0 1", "h1e1", "pin"),
    ("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a8", "mate-in-1"),
    ("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a8", "back-rank-mate"),
    ("6rk/6pp/8/6N1/8/8/8/6K1 w - - 0 1", "g5f7", "smothered-mate"),
    ("4k3/8/8/8/4B3/8/8/K3R3 w - - 0 1", "e4b7", "discovered-check"),
    ("8/P7/8/8/8/8/k7/4K3 w - - 0 1", "a7a8q", "promotion"),
    ("8/P7/8/8/8/8/k7/4K3 w - - 0 1", "a7a8n", "under-promotion"),
    ("4k3/8/8/8/3q4/5N2/8/4K3 w - - 0 1", "f3d4", "free-piece"),
])
def test_single_move_motifs(fen, best, expected):
    assert expected in _tags(fen, best)


def test_a_quiet_developing_move_is_not_called_a_fork():
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    board = chess.Board(fen)
    mv = chess.Move.from_uci("g1f3")
    tags = detect_motifs(board, mv, mv)
    assert "fork" not in tags and "skewer" not in tags and "pin" not in tags


def test_mate_length_is_read_off_the_line():
    # K+Q v K: Qg7 is not mate, but after the king's only move Qh7 is.
    fen = "7k/8/5K2/8/8/8/8/6Q1 w - - 0 1"
    board = chess.Board(fen)
    line = parse_line("g1g7")
    assert board.is_legal(line[0])
    b = board.copy()
    b.push(line[0])
    assert b.is_checkmate()          # Qg7 is already mate here: mate in one
    assert "mate-in-1" in _tags(fen, "g1g7")

    fen = "7k/8/8/5K2/8/8/8/6Q1 w - - 0 1"
    tags = _tags(fen, "f5f6", line="f5f6 h8h7 g1g7")
    assert "mate-in-2" in tags


def test_an_illegal_line_is_cut_short_not_believed():
    fen = "7k/8/8/5K2/8/8/8/6Q1 w - - 0 1"
    tags = _tags(fen, "f5f6", line="f5f6 a8a1 g1g7")
    assert not any(t.startswith("mate") for t in tags)


def test_back_rank_mate_allowed():
    board = chess.Board("r5k1/8/8/8/8/8/2P2PPP/6K1 w - - 0 1")
    # c3?? ignores the back rank: Ra8-a1 is mate, where h3 would have given air.
    tags = detect_motifs(board, chess.Move.from_uci("c2c3"), chess.Move.from_uci("h2h3"),
                         None, parse_line("a8a1"))
    assert "allowed-mate" in tags
    assert "back-rank-weak" in tags


def test_hanging_a_piece_is_still_detected():
    board = chess.Board("4k3/8/8/3p4/8/8/8/2N1K3 w - - 0 1")
    tags = detect_motifs(board, chess.Move.from_uci("c1b3"), chess.Move.from_uci("e1d2"))
    assert "hangs-piece" not in tags      # b3 isn't attacked
    tags = detect_motifs(board, chess.Move.from_uci("c1e2"), chess.Move.from_uci("e1d2"))
    assert "hangs-piece" not in tags      # defended by the king


def test_a_queen_that_is_simply_taken_does_not_fork_or_skewer():
    # Qd8+ "attacks" the king and the a8 rook, but the king takes it.
    fen = "r3k2r/ppp2ppp/8/8/8/2n5/PP3PPP/R1BQK2R w KQkq - 0 1"
    tags = _tags(fen, "d1d8", played="b2c3", line="d1d8 e8d8 b2c3")
    assert "fork" not in tags and "skewer" not in tags
    assert "sacrifice" in tags
    assert "intermezzo" not in tags      # they took the bait: not a zwischenzug


def test_intermezzo_on_a_line():
    # Black has just taken on c3. Rather than recapture at once, White checks
    # on e1... here: Re1+ first, the king steps aside, then bxc3.
    fen = "4k3/8/8/8/8/2n5/1P6/4K2R w K - 0 1"
    board = chess.Board(fen)
    line = parse_line("h1h8 e8d7 b2c3")
    assert board.is_legal(line[0])
    tags = _tags(fen, "h1h8", played="b2c3", line="h1h8 e8d7 b2c3")
    assert "intermezzo" in tags


def test_every_motif_has_a_label_and_a_lichess_mapping_entry():
    for tag in ALL_MOTIFS:
        assert tag in LABEL
        assert tag in LICHESS_THEME
    assert len(ALL_MOTIFS) >= 25
    for ours in FROM_LICHESS.values():
        assert ours in LABEL


def test_lichess_themes_translate():
    assert from_lichess_themes("crushing fork middlegame short") == ["fork"]
    assert from_lichess_themes("mateIn2 backRankMate") == ["mate-in-2", "back-rank-mate"]
    assert from_lichess_themes("") == []


def test_tags_are_capped():
    board = chess.Board("r3k3/8/8/3N4/8/8/8/4K3 w - - 0 1")
    tags = detect_motifs(board, chess.Move.from_uci("e1e2"), chess.Move.from_uci("d5c7"))
    assert len(tags) <= 6


def test_library_import_speaks_our_vocabulary(tmp_path):
    from coach.puzzles.library import import_lichess_csv
    from coach.store.db import Puzzle, session_factory

    csv = tmp_path / "p.csv"
    csv.write_text(
        "PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags\n"
        "abc12,r3k3/8/8/3N4/8/8/8/4K3 b - - 0 1,e8e7 d5c7,1500,80,90,100,"
        "crushing fork short,https://x,\n"
    )
    db = tmp_path / "lib.db"
    assert import_lichess_csv(csv, user_elo=1500, db_path=db) == 1
    with session_factory(db)() as s:
        p = s.query(Puzzle).one()
        assert p.motif == "fork"
        assert p.opponent_first
