"""Server-side puzzle attempt evaluation."""

from coach.puzzles.solve import evaluate_attempt
from coach.store.db import Puzzle


def _own_game_puzzle():
    # White to move; solution is a 3-ply line the user must start.
    return Puzzle(
        source="own-game",
        fen="r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
        solution="f3g5 d7d5 e4d5",
        pillar="tactics",
        kind="find-best",
    )


def test_correct_first_move_continues():
    p = _own_game_puzzle()
    r = evaluate_attempt(p, ["f3g5"], auto_first=True)
    assert r["status"] == "continue"
    assert r["user_colour"] == "white"
    # opponent reply d7d5 auto-applied -> white to move again
    assert r["fen"].split()[1] == "w"


def test_wrong_move_reports_best():
    p = _own_game_puzzle()
    r = evaluate_attempt(p, ["a2a3"], auto_first=True)
    assert r["status"] == "wrong"
    assert r["best_uci"] == "f3g5"
    assert r["best_san"] == "Ng5"


def test_wrong_move_carries_both_positions_so_the_ui_can_show_them():
    """Naming the right move is not the same as showing it. The UI told the user
    to "play it to continue" over a locked board; it now needs the position the
    mistake reaches and the position the right move reaches."""
    import chess

    p = _own_game_puzzle()
    r = evaluate_attempt(p, ["a2a3"], auto_first=True)
    assert r["played_uci"] == "a2a3"
    assert r["played_san"] == "a3"

    # Each is the position in "fen" plus exactly the one move it names.
    for fen, uci in ((r["fen_played"], r["played_uci"]), (r["best_fen"], r["best_uci"])):
        b = chess.Board(r["fen"])
        b.push(chess.Move.from_uci(uci))
        assert b.fen() == fen


def test_full_line_solves():
    p = _own_game_puzzle()
    r = evaluate_attempt(p, ["f3g5", "e4d5"], auto_first=True)
    assert r["status"] == "solved"


def test_library_puzzle_auto_first_skips_setup_move():
    # Lichess-style: first move is the opponent's, solver plays from move 2.
    p = Puzzle(
        source="library",
        external_id="x1",
        fen="6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1",
        solution="g1h2 a1a8",  # setup g1h2, then mate-ish a1a8
        pillar="tactics",
        kind="find-best",
    )
    r = evaluate_attempt(p, [], auto_first=True)
    assert r["status"] == "continue"
    assert r["fen"].split()[1] == "b"  # black to move after white's auto setup move
    solved = evaluate_attempt(p, ["a1a8"], auto_first=True)
    assert solved["status"] == "solved"


def test_equal_alternative_is_accepted_not_marked_wrong():
    """An equally good move must never be marked wrong.

    Measured on real games, the median mined puzzle had only ~49cp between the
    best and second-best move, so strict string-matching against one stored line
    was telling the user "✗" for moves that were fine.
    """
    p = _own_game_puzzle()
    # Stub verifier: a2a3 is 10cp behind best, i.e. effectively equal.
    r = evaluate_attempt(p, ["a2a3"], auto_first=True, verifier=lambda b, m: 10)
    assert r["status"] == "alternative"
    assert r["played_san"] == "a3"
    assert r["cp_behind"] == 10


def test_a_genuinely_worse_move_is_still_wrong():
    p = _own_game_puzzle()
    r = evaluate_attempt(p, ["a2a3"], auto_first=True, verifier=lambda b, m: 400)
    assert r["status"] == "wrong"
    assert r["best_san"] == "Ng5"


def test_without_a_verifier_the_check_is_skipped_entirely():
    """No engine available (or a unit test) -> strict comparison, no hang."""
    p = _own_game_puzzle()
    assert evaluate_attempt(p, ["a2a3"], auto_first=True)["status"] == "wrong"


def test_wrong_answer_carries_the_refutation_when_one_is_offered():
    """Naming the right move is half a lesson; the sequence that punishes the
    wrong one is the half the player was actually asking about."""
    p = _own_game_puzzle()

    def refuter(board, played):
        assert played.uci() == "a2a3"          # asked about the move really played
        return ["d7d5", "exd5", "Nxd5"]

    r = evaluate_attempt(p, ["a2a3"], auto_first=True, refuter=refuter)
    assert r["status"] == "wrong"
    assert r["refutation_san"] == ["d7d5", "exd5", "Nxd5"]


def test_wrong_answer_without_a_refuter_is_unchanged():
    """The module stays pure and offline: no engine, no refutation, no error."""
    r = evaluate_attempt(_own_game_puzzle(), ["a2a3"], auto_first=True)
    assert r["status"] == "wrong" and r["refutation_san"] is None


def test_a_wrong_answer_carries_the_rest_of_the_line_to_play_on_with():
    """Being handed the move is not the lesson - the line it starts is, and nine
    in ten mined puzzles run three plies or more. The whole continuation travels
    with the answer so the browser, which has no chess rules, can play it out
    without asking anything else."""
    import chess

    p = _own_game_puzzle()                      # f3g5 d7d5 e4d5
    r = evaluate_attempt(p, ["a2a3"], auto_first=True)
    steps = r["continuation"]

    # Starts *after* the move they missed, and alternates from there.
    assert [s["uci"] for s in steps] == ["d7d5", "e4d5"]
    assert [s["side"] for s in steps] == ["opponent", "user"]
    assert [s["san"] for s in steps] == ["d5", "exd5"]

    # Each step is the position it reaches, walked from the puzzle position.
    board = chess.Board(r["fen"])
    board.push(chess.Move.from_uci(r["best_uci"]))
    for s in steps:
        board.push(chess.Move.from_uci(s["uci"]))
        assert s["fen"] == board.fen()


def test_the_line_offers_legal_moves_only_where_it_is_the_users_turn():
    """The user has to *find* the next move, so the position after their
    opponent's reply ships its legal destinations. Their own moves don't need
    any, and sending them would just pad every wrong answer."""
    p = _own_game_puzzle()
    steps = evaluate_attempt(p, ["a2a3"], auto_first=True)["continuation"]
    theirs, mine = steps[0], steps[1]
    assert theirs["side"] == "opponent" and theirs["dests"]
    assert "e4" in theirs["dests"], "the pawn that plays the next move of the line"
    assert "dests" not in mine


def test_a_one_move_puzzle_has_nothing_to_play_on_with():
    p = Puzzle(source="own-game", fen=_own_game_puzzle().fen, solution="f3g5",
               pillar="tactics", kind="find-best")
    assert evaluate_attempt(p, ["a2a3"], auto_first=True)["continuation"] == []


def test_a_line_that_will_not_replay_stops_rather_than_raising():
    """Stored lines come from a miner, and a truncated or corrupt one must cost
    the continuation, not the answer."""
    p = Puzzle(source="own-game", fen=_own_game_puzzle().fen,
               solution="f3g5 a1a8 e4d5", pillar="tactics", kind="find-best")
    r = evaluate_attempt(p, ["a2a3"], auto_first=True)
    assert r["status"] == "wrong" and r["best_san"] == "Ng5"
    assert r["continuation"] == []
