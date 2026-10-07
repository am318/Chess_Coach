"""Server-side puzzle solving.

Keeps all chess rules on the server: the browser just replays the position the
server returns. ``evaluate_attempt`` takes the list of moves the user has played
so far and compares them against the stored solution line.
"""

from __future__ import annotations

from collections.abc import Callable

import chess

from coach.store.db import Puzzle

# A move an engine rates within this many centipawns of the best is accepted as
# an alternative solution. Marking a genuinely equal move "wrong" teaches the
# player to distrust their own correct judgement, which is worse than letting a
# near-miss through.
ACCEPT_ALT_CP = 50
VERIFY_NODES = 120_000

# How many plies of punishment to show. Long enough to see the point, short
# enough that it is a lesson rather than a database dump.
REFUTATION_PLIES = 6

# A verifier answers "how far behind best is this move, in cp?" - or None when it
# cannot say. It is injected rather than imported so this module stays pure and
# offline: the browser has no chess engine, and neither should the unit tests.
Verifier = Callable[[chess.Board, chess.Move], "int | None"]

# A refuter answers "what happens after this move?" as a list of SAN strings,
# from the position the move reaches. Injected for the same reason.
Refuter = Callable[[chess.Board, chess.Move], "list[str] | None"]


def stockfish_refuter(board: chess.Board, played: chess.Move) -> list[str] | None:
    """What the played move runs into, in SAN, from the position it reaches.

    "The move was Nxe5" answers a question nobody asked. The question is "what
    was wrong with what I played" - and until they see the sequence, the lesson
    is a fact to memorise rather than something understood.
    """
    try:
        from coach.sparring.backend import shared_backend

        after = board.copy(stack=False)
        after.push(played)
        pv = shared_backend().line(after, VERIFY_NODES, limit=REFUTATION_PLIES)
        out = []
        for mv in pv:
            if mv not in after.legal_moves:
                break
            out.append(after.san(mv))
            after.push(mv)
        return out or None
    except Exception:  # noqa: BLE001 - explanation must never break a puzzle
        return None


def stockfish_verifier(board: chess.Board, played: chess.Move) -> int | None:
    """Default verifier, used by the server. Best-effort: any engine failure
    returns None and the strict stored-line comparison stands."""
    try:
        from coach.sparring.backend import shared_backend

        ranked = shared_backend().score_roots(board, VERIFY_NODES)
    except Exception:  # noqa: BLE001 - verification must never break a puzzle
        return None
    if not ranked:
        return None
    best_cp = ranked[0][1]
    for mv, cp in ranked:
        if mv == played:
            return best_cp - cp
    return None


def _legal_dests(board: chess.Board) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for mv in board.legal_moves:
        out.setdefault(chess.square_name(mv.from_square), []).append(
            chess.square_name(mv.to_square)
        )
    return out


def _check_square(board: chess.Board) -> str | None:
    """The square of the king in check, so the board can mark it.

    A king in check was previously shown no differently from a king that wasn't,
    which is the kind of thing you only notice by losing to it.
    """
    if not board.is_check():
        return None
    sq = board.king(board.turn)
    return chess.square_name(sq) if sq is not None else None


def _line_from(board: chess.Board, moves: list[str]) -> list[dict]:
    """The rest of the solution after ``moves[0]``, ply by ply.

    A puzzle that stops at the move you missed teaches the move, not the line -
    and nine in ten of the puzzles mined from this player's own games run three
    plies or more. So the whole continuation travels with the wrong answer:
    each step is the position it reaches, tagged with whose move it was, and
    carrying legal destinations wherever the user is on move again. The browser
    has no chess rules and is not about to get any, so everything it needs to
    play the line out has to be in this one reply.
    """
    probe = board.copy(stack=False)
    out: list[dict] = []
    for i, uci in enumerate(moves):
        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            break
        if mv not in probe.legal_moves:
            break
        san = probe.san(mv)
        probe.push(mv)
        if i == 0:
            continue                      # the move they missed: already reported
        step = {
            "uci": uci,
            "san": san,
            "fen": probe.fen(),
            "side": "opponent" if i % 2 else "user",
            "check": _check_square(probe),
        }
        if i % 2:                         # their reply - the user is on move now
            step["dests"] = _legal_dests(probe)
        out.append(step)
    return out


def _same(board: chess.Board, played_uci: str, want_uci: str) -> bool:
    if played_uci == want_uci:
        return True
    # Accept a 4-char move where the solution auto-queens (or vice versa).
    if len(played_uci) == 4 and len(want_uci) == 5 and want_uci[:4] == played_uci:
        return True
    return len(played_uci) == 5 and len(want_uci) == 4 and played_uci[:4] == want_uci


def evaluate_attempt(puzzle: Puzzle, user_moves: list[str], auto_first: bool,
                     verifier: Verifier | None = None,
                     refuter: Refuter | None = None) -> dict:
    sol = puzzle.solution.split()
    # Rows written before the flag existed relied on source == "library".
    opp_first = bool(getattr(puzzle, "opponent_first", False)) or puzzle.source == "library"
    board = chess.Board(puzzle.fen)
    last_move: str | None = None
    line = sol

    if opp_first and sol and auto_first:
        board.push(chess.Move.from_uci(sol[0]))
        last_move = sol[0]
        line = sol[1:]

    user_colour = "white" if board.turn == chess.WHITE else "black"
    total_user_moves = (len(line) + 1) // 2

    for i, um in enumerate(user_moves):
        want_idx = 2 * i
        if want_idx >= len(line):
            break
        want = line[want_idx]
        if not _same(board, um, want):
            # Before calling it wrong, check whether the move played is simply a
            # different move of equal value. The stored line can't continue from
            # there, so an accepted alternative ends the puzzle successfully.
            try:
                played_mv = chess.Move.from_uci(um)
            except ValueError:
                played_mv = None
            if verifier is not None and played_mv is not None and played_mv in board.legal_moves:
                behind = verifier(board, played_mv)
                if behind is not None and behind <= ACCEPT_ALT_CP:
                    played_san = board.san(played_mv)
                    board.push(played_mv)
                    return {
                        "status": "alternative",
                        "fen": board.fen(),
                        "last_move": um,
                        "played_san": played_san,
                        "cp_behind": behind,
                        "user_colour": user_colour,
                        "check": _check_square(board),
                    }

            # Everything the UI needs to *show* the mistake rather than just
            # name it: the position the wrong move reaches, and the position the
            # right move reaches so the user can play it and see why.
            best_san = None
            best_fen = None
            try:
                best_mv = chess.Move.from_uci(want)
                best_san = board.san(best_mv)
                board.push(best_mv)
                best_fen = board.fen()
                board.pop()
            except (ValueError, AssertionError):
                pass
            played_san = fen_played = None
            refutation = None
            if played_mv is not None and played_mv in board.legal_moves:
                played_san = board.san(played_mv)
                board.push(played_mv)
                fen_played = board.fen()
                board.pop()
                # What their move actually runs into. Asked of the engine for the
                # move they really played, not read off the puzzle - the stored
                # line refutes the move made in the original game, which is only
                # sometimes the same move.
                if refuter is not None:
                    refutation = refuter(board, played_mv)
            return {
                "status": "wrong",
                "fen": board.fen(),
                "last_move": last_move,
                "best_uci": want,
                "best_san": best_san,
                "best_fen": best_fen,
                "played_uci": um,
                "played_san": played_san,
                "fen_played": fen_played,
                "refutation_san": refutation,
                "continuation": _line_from(board, line[want_idx:]),
                "user_colour": user_colour,
                "check": _check_square(board),
            }
        board.push(chess.Move.from_uci(want))
        last_move = want
        reply_idx = want_idx + 1
        if reply_idx < len(line):
            board.push(chess.Move.from_uci(line[reply_idx]))
            last_move = line[reply_idx]

    if len(user_moves) >= total_user_moves:
        return {
            "status": "solved",
            "fen": board.fen(),
            "last_move": last_move,
            "user_colour": user_colour,
            "check": _check_square(board),
        }
    return {
        "status": "continue",
        "fen": board.fen(),
        "last_move": last_move,
        "user_colour": user_colour,
        "check": _check_square(board),
        "dests": _legal_dests(board),
    }
