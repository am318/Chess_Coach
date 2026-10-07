"""Playing a puzzle position on after it has been graded.

``coach.puzzles.explore`` takes an injected scorer for the same reason the
solver takes an injected verifier: the logic being tested is "when has this
demonstration made its point", which has nothing to do with Stockfish being
installed. A tiny deterministic scorer stands in for one.
"""

from __future__ import annotations

import chess
import pytest

from coach.analysis.classify import detect_motifs
from coach.puzzles import explore


class FixedScorer:
    """Scores every root move the same, so the verdict logic is the only variable.

    ``cp`` is from the point of view of the side to move, matching the real
    backend's contract. Note what that means for a step: the position the scorer
    is asked about is the one *before* the engine's reply, so the side to move
    there is the opponent, and the user's evaluation afterwards is ``-cp``. The
    helper below spells that out rather than leaving it to be rediscovered.
    """

    def __init__(self, cp: int = 0, pick: str | None = None) -> None:
        self.cp = cp
        self.pick = pick

    def _moves(self, board: chess.Board) -> list[chess.Move]:
        legal = list(board.legal_moves)
        if self.pick:
            want = chess.Move.from_uci(self.pick)
            if want in legal:
                return [want] + [m for m in legal if m != want]
        return legal

    def score_roots(self, board, budget, limit=None):
        out = [(m, self.cp - i) for i, m in enumerate(self._moves(board))]
        return out if limit is None else out[:limit]

    def best(self, board, budget):
        mv = self._moves(board)
        return (mv[0], self.cp) if mv else None

    def line(self, board, budget, limit=8):
        return self._moves(board)[:1]


def scorer_for_user_eval(cp: int) -> FixedScorer:
    """A scorer that leaves the user on ``cp`` once the engine has replied."""
    return FixedScorer(cp=-cp)


START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def test_start_lets_the_engine_answer_first_when_it_is_not_your_turn():
    """`punish` opens on the position your wrong move reached, so it is the
    opponent to move. You should arrive holding the consequences, not be asked
    to play the refutation against yourself."""
    board = chess.Board(START)
    board.push_uci("e2e4")                       # "your" move; black to move
    st = explore.start(board.fen(), "punish", user_colour="white",
                       scorer=FixedScorer(cp=0))
    assert st["status"] == "continue"
    assert st["reply_san"]                        # the engine moved
    assert chess.Board(st["fen"]).turn == chess.WHITE
    assert st["user_colour"] == "white"


def test_start_does_not_move_for_you_when_it_is_your_turn():
    st = explore.start(START, "convert", scorer=FixedScorer(cp=0))
    assert st["reply_san"] is None
    assert st["fen"] == chess.Board(START).fen()
    assert st["dests"]["e2"]


def test_punish_says_so_once_the_evaluation_is_decisive():
    """A step always ends with the user to move, so the scorer's side-to-move
    score *is* the user's evaluation."""
    board = chess.Board(START)
    board.push_uci("e2e4")
    scorer = scorer_for_user_eval(-(explore.DECISIVE_CP + 200))
    st = explore.start(board.fen(), "punish", user_colour="white",
                       scorer=FixedScorer(cp=0))
    assert st["status"] == "continue"
    out = explore.step(st["fen"], "g1f3", "punish", "white", st["hold_cp"], 0,
                       scorer=scorer)
    assert out["status"] == "settled"
    assert out["tone"] == "bad"
    assert "ran into" in out["message"]


def test_a_settled_position_keeps_its_legal_moves():
    """The case against ending on the verdict is the same as the case against
    ending on the answer: you have to be allowed to play into it."""
    scorer = scorer_for_user_eval(-(explore.DECISIVE_CP + 200))
    out = explore.step(START, "e2e4", "punish", "white", 0, 0, scorer=scorer)
    assert out["status"] == "settled"
    assert out["dests"], "settled must stay playable"
    assert "Play on" in out["message"]


def test_punish_runs_out_of_rope_rather_than_forever():
    scorer = FixedScorer(cp=0)
    st = explore.start(START, "punish", scorer=scorer)
    fen, ply = st["fen"], 0
    for _ in range(explore.MAX_USER_MOVES):
        uci = next(iter(explore._dests(chess.Board(fen)).items()))
        out = explore.step(fen, uci[0] + uci[1][0], "punish", st["user_colour"],
                           st["hold_cp"], ply, scorer=scorer)
        if out["status"] == "done":
            break
        fen, ply = out["fen"], out["moves_played"]
    assert out["status"] == "done"
    assert out["moves_played"] <= explore.MAX_USER_MOVES


def test_convert_fails_when_the_advantage_is_spilled():
    """The point of `convert`: finding the move is not the same as winning."""
    st = explore.start(START, "convert", scorer=FixedScorer(cp=600))
    assert st["hold_cp"] == 600
    # The position now evaluates far below what the tactic was worth.
    out = explore.step(st["fen"], "e2e4", "convert", "white", 600, 0,
                       scorer=FixedScorer(cp=0))
    assert out["status"] == "settled"
    assert out["tone"] == "bad"
    assert "advantage has gone" in out["message"]
    assert out["dests"], "you may fight on from a spilled advantage"


def test_convert_holding_the_evaluation_keeps_going():
    out = explore.step(START, "e2e4", "convert", "white", 600, 0,
                       scorer=scorer_for_user_eval(600))
    assert out["status"] == "continue"
    assert out["cp"] == 600


def test_a_refused_move_is_reported_not_applied():
    out = explore.step(START, "e2e5", "punish", "white", 0, 0,
                       scorer=FixedScorer(cp=0))
    assert out["status"] == "error"
    assert out["fen"] == chess.Board(START).fen()


def test_underspecified_promotion_is_read_as_a_queen():
    fen = "8/P6k/8/8/8/8/7K/8 w - - 0 1"
    out = explore.step(fen, "a7a8", "convert", "white", 0, 0, scorer=FixedScorer(cp=0))
    assert out["status"] != "error"
    assert out["played_san"].startswith("a8=Q")


def test_mate_ends_a_conversion_as_a_success():
    # Back-rank mate in one for White.
    fen = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"
    out = explore.step(fen, "a1a8", "convert", "white", 500, 0, scorer=FixedScorer(cp=0))
    assert out["status"] == "done"
    assert "Mate" in out["message"]


def test_check_square_is_reported_so_the_board_can_mark_it():
    """The board had no way to show a king in check, which is a thing you
    otherwise only notice by losing to it."""
    quiet = explore.start(START, "convert", scorer=FixedScorer(cp=0))
    assert quiet["check"] is None

    in_check = explore.start("R5k1/8/8/8/8/8/8/6K1 b - - 0 1", "punish",
                             scorer=FixedScorer(cp=0))
    assert in_check["check"] == "g8"

    mated = explore.step("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "a1a8", "convert",
                         "white", 500, 0, scorer=FixedScorer(cp=0))
    assert mated["check"] == "g8"


def test_candidates_rank_and_explain_the_position():
    scorer = FixedScorer(cp=320, pick="e2e4")
    r = explore.candidates(START, top_n=3, scorer=scorer)
    assert r["moves"][0]["san"] == "e4"
    assert r["moves"][0]["behind"] == 0
    assert r["moves"][1]["behind"] > 0
    assert all(m["fen_after"] for m in r["moves"])
    assert r["user_colour"] == "white"


def test_candidates_name_the_capture_they_make():
    fen = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    r = explore.candidates(fen, top_n=1, scorer=FixedScorer(cp=40, pick="e4d5"))
    assert r["moves"][0]["san"] == "exd5"
    assert "takes the pawn" in r["moves"][0]["note"]


def test_candidates_survive_an_engine_that_is_not_there():
    class Broken:
        def score_roots(self, board, budget, limit=None):
            raise RuntimeError("no stockfish")

    assert explore.candidates(START, scorer=Broken())["moves"] == []


def test_exploring_never_touches_the_srs():
    """Structural: nothing in the module may import or call the scheduler.

    The grade is fixed by the attempt. If exploring could change it, a player
    who is unsure would stop exploring, which is the one thing this must not
    cost them.
    """
    with open(explore.__file__) as fh:
        src = fh.read()
    assert "srs" not in src.replace("the SRS", "")


@pytest.mark.parametrize("cp,want", [
    (None, "?"), (0, "+0.0"), (-250, "-2.5"), (310, "+3.1"),
    # A mate says its distance: "#3" and "#-1" are not the same situation, and
    # a bare "#" hid which of the two you were in.
    (99_997, "#3"), (-99_997, "#-3"), (99_999, "#1"), (-100_000, "#-"),
])
def test_evaluations_read_the_way_a_player_would_say_them(cp, want):
    assert explore.fmt_cp(cp) == want


def test_the_reply_search_doubles_as_the_evaluation():
    """The optimisation that made the trainer playable, pinned down.

    One search per move, not two: the score of the position the engine searched
    is the score of the position it moved to, negated. A scorer that counts its
    calls proves we only ask once.
    """
    class Counting(FixedScorer):
        calls = 0

        def best(self, board, budget):
            Counting.calls += 1
            return super().best(board, budget)

    out = explore.step(START, "e2e4", "convert", "white", 0, 0,
                       scorer=Counting(cp=-250))
    assert Counting.calls == 1
    assert out["cp"] == 250          # the reply score, negated into the user's view


def test_the_evaluation_still_gets_asked_for_when_nobody_replied():
    """`start` on your own turn has no reply search to borrow from."""
    st = explore.start(START, "convert", scorer=FixedScorer(cp=175))
    assert st["reply_san"] is None
    assert st["cp"] == 175


def test_a_mate_is_not_compared_to_an_evaluation_in_centipawns():
    """Subtracting a mate score from a number produced "991.9 worse than best",
    which is arithmetic about a quantity that does not exist."""
    assert explore._gap_phrase(-810, -99_998) == "walks into mate in 2"
    assert explore._gap_phrase(-810, -100_000) == "walks into mate"
    assert explore._gap_phrase(99_997, 40) == "misses the mate"
    assert explore._gap_phrase(0, 99_997) is None          # the note says "mate"
    assert explore._gap_phrase(40, -180) == "but 2.2 worse than best"
    assert explore._gap_phrase(40, 20) is None             # near enough to best


def test_a_losing_position_is_not_asked_to_be_won():
    """A defensive puzzle reaches the convert panel at -8 often enough that
    "Now win it" is simply the wrong instruction."""
    assert "win it" in explore._opening_line("convert", 400)
    assert "hold it" in explore._opening_line("convert", 0)
    assert "already lost" in explore._opening_line("convert", -800)


def test_holding_a_lost_position_is_not_called_converting_it():
    _, msg, tone = explore._verdict("convert", -700, -750, explore.MAX_USER_MOVES, 0)
    assert "Held" in msg and "Converted" not in msg
    assert tone == "good"
    _, msg, _ = explore._verdict("convert", 700, 650, explore.MAX_USER_MOVES, 0)
    assert "Converted" in msg


def test_each_candidate_is_labelled_with_its_own_tactic():
    """Every row used to inherit the best move's motif, so a list of four
    unrelated moves all claimed to fork."""
    # White to move: Nd5 forks nothing; Nxe6 is a plain capture. Whatever the
    # engine ranks first, no two different moves should carry an identical
    # motif list unless they genuinely share it.
    fen = "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2N2N2/PPPP1PPP/R1BQK2R w KQkq - 6 5"
    r = explore.candidates(fen, top_n=4, scorer=FixedScorer(cp=20))
    best = r["moves"][0]
    # The best move's own motifs must be derived from the best move itself, and
    # a quiet developing move must not be described as a fork.
    for m in r["moves"]:
        board = chess.Board(fen)
        mv = chess.Move.from_uci(m["uci"])
        own = [explore.MOTIF_LABEL.get(x, x)
               for x in detect_motifs(board, mv, mv) if x != "back-rank-weak"]
        assert m["motifs"] == own, f"{m['san']} was labelled from another move"
    assert best["san"]


def test_near_equal_candidates_are_told_apart_from_the_best_one():
    """A top-four list usually spans well under a pawn. At a 100cp threshold
    every row came back "quiet", which is not advice."""
    assert explore._gap_phrase(-480, -577) == "but 1.0 worse than best"
    assert explore._gap_phrase(-480, -500) is None          # a real alternative

    board = chess.Board(START)
    mv = chess.Move.from_uci("e2e4")
    assert explore._candidate_note(board, mv, 30, 30, _after(board, mv)) == "quiet"
    assert explore._candidate_note(board, mv, 10, 30, _after(board, mv)) == "about as good"
    assert "0.9 worse" in explore._candidate_note(board, mv, -60, 30, _after(board, mv))


def _after(board, mv):
    nxt = board.copy(stack=False)
    nxt.push(mv)
    return nxt


def test_losing_a_forced_mate_is_not_losing_the_game():
    """Mate scores are ~100000, so `hold_cp - slack` made *every* non-mate a
    collapse. Letting #3 slip to +4.2 costs the mate, not the win, and a player
    who is still a queen up must not be told the advantage has gone."""
    status, msg, tone = explore._verdict("convert", 420, 99_997, 1, 0)
    assert status == "continue"
    assert "forced mate has gone" in msg
    assert tone is None

    # Genuinely throwing it away is still called what it is.
    status, msg, _ = explore._verdict("convert", -50, 99_997, 1, 0)
    assert status == "settled"
    assert "you had #3" in msg

    # An ordinary advantage keeps the old, tighter floor: CONVERT_SLACK_CP of
    # spill is tolerated, more than that is not.
    inside = 600 - explore.CONVERT_SLACK_CP + 10
    outside = 600 - explore.CONVERT_SLACK_CP - 10
    assert explore._verdict("convert", inside, 600, 1, 0)[0] == "continue"
    assert explore._verdict("convert", outside, 600, 1, 0)[0] == "settled"
