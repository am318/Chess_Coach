"""Play a puzzle position on, after it has been graded.

A puzzle that ends the moment you answer teaches the answer, not the position.
Three things are missing once the card is filed, and this module supplies all
three from the same stateless machinery:

* **punish** - keep playing your own wrong move. The engine answers with the
  refutation as moves on a board rather than as a line of notation, and the
  evaluation is shown falling away under you until the point is unarguable.
* **convert** - keep playing the *right* move. The engine defends with its best
  try and you have to actually realise the advantage, which is where most of the
  learning in a tactic lives.
* **candidates** - the position's other strong ideas, ranked, each one a
  starting point for either of the above.

Stateless, like the endgame drills in ``coach.drills.play``: the position
travels with the request, so a reload loses nothing and the server holds no
per-user exploration session. Nothing here touches the SRS - the grade was
fixed by the attempt, and exploring must never feel like a second test.
"""

from __future__ import annotations

import chess

from coach.analysis.classify import detect_motifs

# Exploration is interactive, so these are sized for a reply that lands inside a
# human pause rather than for analysis-grade accuracy. 150k nodes of Stockfish
# is still far past anything this trainer's opponents need to be; spending 200k
# bought nothing a player could see and cost a fifth of a second a move.
STEP_NODES = 150_000
# Candidates ask for six lines rather than all thirty-five (see `candidates`),
# and MultiPV splits its budget across the lines requested. Six lines at 180k is
# ~30k nodes each against ~8.5k for the old full-width 300k search: faster than
# it was, and each ranking actually searched deep enough to mean something.
CANDIDATE_NODES = 180_000

# How many of your own moves an exploration runs for before it stops and sums
# up. Long enough to see a point land, short enough to stay a lesson.
MAX_USER_MOVES = 8

# Once the evaluation is this lopsided the demonstration has made its case.
DECISIVE_CP = 300
# How much of the advantage you may spill in `convert` and still be converting.
CONVERT_SLACK_CP = 150
# Inside this, a candidate is a genuine alternative rather than an error - the
# same judgement the solver makes when it accepts an equally good move. The
# spread across a top-four list is usually well under a pawn, so a 100cp
# threshold described every one of them as "quiet" and said nothing.
ALT_CP = 40
# The backend reports a forced mate as ``MATE_CP - n``, n being the distance in
# moves. Anything past the threshold is a mate rather than an evaluation.
MATE_CP = 100_000
MATE_THRESHOLD = 90_000

_PIECE_NAME = {
    chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
    chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king",
}

MOTIF_LABEL = {
    "hangs-piece": "leaves a piece hanging",
    "wins-exchange": "wins the exchange",
    "fork": "forks two pieces",
    "pawn-fork": "pawn fork",
    "back-rank-weak": "back rank is weak",
    "moved-pinned-piece": "moves a pinned piece",
}


def _parse(fen: str) -> chess.Board | None:
    """A board, or None when the position will not parse.

    These endpoints take a FEN from the request body. In normal use it came
    from one of our own responses a moment earlier, but "normal use" is not a
    guarantee the server gets to rely on: a typo in a curl, a stale tab posting
    a truncated string, and an unguarded ``chess.Board`` turns into a 500 with a
    stack trace in it.
    """
    try:
        return chess.Board(fen)
    except (ValueError, TypeError, AttributeError):
        return None


def _error(message: str, fen: str = "") -> dict:
    return {"status": "error", "message": message, "fen": fen, "cp": None,
            "eval": "?", "user_colour": "white", "moves_played": 0,
            "check": None, "dests": {}}


def _dests(board: chess.Board) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for mv in board.legal_moves:
        out.setdefault(chess.square_name(mv.from_square), []).append(
            chess.square_name(mv.to_square)
        )
    return out


def _check_square(board: chess.Board) -> str | None:
    """The square of the king that is in check, for the board to mark."""
    if not board.is_check():
        return None
    sq = board.king(board.turn)
    return chess.square_name(sq) if sq is not None else None


def fmt_cp(cp: int | None) -> str:
    """A centipawn score as a player would say it.

    A forced mate is worth saying *how far away* it is: "#3" and "#-1" are
    different situations, and a bare "#" hides which.
    """
    if cp is None:
        return "?"
    if abs(cp) > MATE_THRESHOLD:
        n = MATE_CP - abs(cp)
        sign = "" if cp > 0 else "-"
        return f"#{sign}{n}" if n > 0 else f"#{sign}"
    return f"{cp / 100:+.1f}"


def _material(board: chess.Board, side: chess.Color) -> int:
    vals = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
    return sum(v * len(board.pieces(pt, side)) for pt, v in vals.items())


def _material_swing(start: chess.Board, now: chess.Board, side: chess.Color) -> int:
    """Net material the user has gained (+) or lost (-) since the start."""
    before = _material(start, side) - _material(start, not side)
    after = _material(now, side) - _material(now, not side)
    return after - before


def _backend():
    from coach.sparring.backend import shared_backend

    return shared_backend()


def _after_reply_cp(board: chess.Board, side: chess.Color, reply_cp: int) -> int:
    """The evaluation after the engine has played its own best move - free.

    A search of a position returns that position's value under best play, from
    the mover's point of view. Push the move it found and you are looking at the
    same value negated: the child's score is the parent's score, from the other
    side of the board. Asking the engine again was a second search for a number
    it had already handed over, and it was half the cost of every move.
    """
    if board.is_checkmate():
        return -MATE_CP if board.turn == side else MATE_CP
    if board.is_game_over():
        return 0
    return -reply_cp if board.turn == side else reply_cp


def _eval_cp(board: chess.Board, side: chess.Color, scorer=None) -> int | None:
    """Evaluation from ``side``'s point of view, or None if it can't be had."""
    if board.is_checkmate():
        return -100_000 if board.turn == side else 100_000
    if board.is_game_over():
        return 0
    try:
        top = (scorer or _backend()).best(board, STEP_NODES)
    except Exception:  # noqa: BLE001 - exploration must never break the app
        return None
    if top is None:
        return None
    cp = top[1]
    return cp if board.turn == side else -cp


def _state(board: chess.Board, side: chess.Color, status: str, message: str,
           cp: int | None, ply: int, extra: dict | None = None) -> dict:
    out = {
        "status": status,
        "message": message,
        "fen": board.fen(),
        "cp": cp,
        "eval": fmt_cp(cp),
        "user_colour": "white" if side == chess.WHITE else "black",
        "moves_played": ply,
        "check": _check_square(board),
        "dests": _dests(board) if status in ("continue", "settled") else {},
    }
    if extra:
        out.update(extra)
    return out


def _terminal_message(board: chess.Board, side: chess.Color,
                      mode: str) -> tuple[str, str, str | None] | None:
    """(status, message, tone) when the position has ended on its own."""
    if board.is_checkmate():
        user_won = board.turn != side
        if user_won:
            return ("done", "Mate. That is what the move was for.", "good") if mode == "convert" \
                else ("done", "Mate. The engine let you off.", "good")
        return ("done", "Mated. The advantage was there and it got away.", "bad") \
            if mode == "convert" \
            else ("done", "Mate — and it is you being mated. That is the refutation.", "bad")
    if board.is_stalemate():
        return "done", "Stalemate.", "bad" if mode == "convert" else None
    if board.is_insufficient_material():
        return "done", "Nothing left to mate with.", "bad" if mode == "convert" else None
    if board.is_fivefold_repetition() or board.is_seventyfive_moves():
        return "done", "Drawn by repetition.", "bad" if mode == "convert" else None
    return None


def start(fen: str, mode: str, user_colour: str | None = None, scorer=None) -> dict:
    """Open an exploration at ``fen``.

    ``user_colour`` defaults to whoever is to move. When it is *not* their turn -
    which is the normal case for ``punish``, since the wrong move has just been
    played - the engine answers first, so the user arrives at the position
    holding the consequences rather than being asked to move for the opponent.
    """
    board = _parse(fen)
    if board is None:
        return _error("that position will not parse", fen)
    side = (chess.WHITE if user_colour == "white"
            else chess.BLACK if user_colour == "black" else board.turn)

    reply_san = None
    reply_cp: int | None = None
    if board.turn != side and not board.is_game_over():
        try:
            top = (scorer or _backend()).best(board, STEP_NODES)
        except Exception:  # noqa: BLE001
            top = None
        if top is not None:
            reply_san = board.san(top[0])
            board.push(top[0])
            reply_cp = top[1]

    cp = (_after_reply_cp(board, side, reply_cp) if reply_cp is not None
          else _eval_cp(board, side, scorer))
    term = _terminal_message(board, side, mode)
    if term:
        status, message, tone = term
    else:
        status, message, tone = "continue", _opening_line(mode, cp), None
    return _state(board, side, status, message, cp, 0, {
        "mode": mode,
        # What `convert` is measured against later: not "are you winning" but
        # "are you still as winning as the move made you".
        "hold_cp": cp,
        "reply_san": reply_san,
        "last_move": None,
        "tone": tone,
    })


def _opening_line(mode: str, cp: int | None) -> str:
    """What the exploration is actually asking of you, from here.

    "Now win it" is the wrong instruction in a position that is losing, and a
    defensive puzzle reaches this panel at -8 often enough for it to matter.
    """
    if mode == "convert":
        if cp is None or cp >= DECISIVE_CP // 2:
            return "Now win it. The engine defends with its best try."
        if cp > -DECISIVE_CP // 2:
            return "Now hold it. The engine will press."
        return "This is already lost. Play it out and see where it goes."
    # The evaluation is already on the badge beside this line; repeating it here
    # just made the panel read "-# / -# already".
    if cp is not None and cp <= -DECISIVE_CP:
        return "Already losing. Play on and find out what that means over the board."
    return "Your move stands. Play on and see where it goes."


def step(fen: str, uci: str, mode: str, user_colour: str, hold_cp: int | None,
         moves_played: int = 0, start_fen: str | None = None, scorer=None) -> dict:
    """Apply the user's move, let the engine answer, and judge where it stands.

    ``hold_cp`` is the evaluation the exploration opened on, which is what
    ``convert`` is measured against: the question is not "are you winning" but
    "are you still as winning as the move made you".
    """
    board = _parse(fen)
    if board is None:
        return _error("that position will not parse", fen)
    side = chess.WHITE if user_colour == "white" else chess.BLACK

    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return _state(board, side, "error", f"bad move {uci}", None, moves_played)
    if move not in board.legal_moves:
        promoted = chess.Move(move.from_square, move.to_square, promotion=chess.QUEEN)
        if promoted not in board.legal_moves:
            return _state(board, side, "error", "illegal move", None, moves_played)
        move = promoted

    played_san = board.san(move)
    board.push(move)
    moves_played += 1
    last_move = move.uci()

    reply_san = None
    reply_cp: int | None = None
    term = _terminal_message(board, side, mode)
    if term is None:
        try:
            top = (scorer or _backend()).best(board, STEP_NODES)
        except Exception:  # noqa: BLE001
            top = None
        if top is not None:
            reply_san = board.san(top[0])
            board.push(top[0])
            last_move = top[0].uci()
            reply_cp = top[1]
        term = _terminal_message(board, side, mode)

    cp = (_after_reply_cp(board, side, reply_cp) if reply_cp is not None
          else _eval_cp(board, side, scorer))
    extra = {"mode": mode, "hold_cp": hold_cp, "played_san": played_san,
             "reply_san": reply_san, "last_move": last_move}

    if term is not None:
        return _state(board, side, term[0], term[1], cp, moves_played,
                      {**extra, "tone": term[2]})

    origin = _parse(start_fen) if start_fen else None
    swing = _material_swing(origin, board, side) if origin else 0
    status, message, tone = _verdict(mode, cp, hold_cp, moves_played, swing)
    return _state(board, side, status, message, cp, moves_played, {**extra, "tone": tone})


def _verdict(mode: str, cp: int | None, hold_cp: int | None, ply: int,
             swing: int) -> tuple[str, str, str | None]:
    """(status, message, tone) for a position the exploration has reached.

    Three statuses, and the distinction between the middle two is the whole
    point of the module:

    * ``continue`` - nothing has been shown yet, keep playing.
    * ``settled``  - the demonstration has made its case, *and the board stays
      live*. Taking the position away the moment the evaluation turns decisive
      would be the same mistake as ending the puzzle on the answer: it asserts
      the conclusion instead of letting someone play into it.
    * ``done``     - the position is genuinely over, or the rope has run out.
    """
    mat = (f" You are {'up' if swing > 0 else 'down'} {abs(swing)} points of material."
           if swing else "")
    capped = ply >= MAX_USER_MOVES

    if cp is None:                       # no engine: let them play, say nothing
        return ("done", "", None) if capped else ("continue", "", None)

    if mode == "convert":
        # A tactic that opened on a forced mate must not be judged as though
        # anything short of mate were a collapse: letting #3 slip to +4.2 loses
        # the mate, not the game, and calling that "the advantage has gone" while
        # the player is still a queen up teaches them to distrust the readout.
        had_mate = hold_cp is not None and hold_cp > MATE_THRESHOLD
        floor = (DECISIVE_CP if had_mate
                 else (hold_cp if hold_cp is not None else DECISIVE_CP) - CONVERT_SLACK_CP)
        if cp < floor:
            lost = "advantage" if (hold_cp or 0) > 0 else "position"
            msg = (f"The {lost} has gone: you had {fmt_cp(hold_cp)}, now "
                   f"{fmt_cp(cp)}.{mat} Keeping what a move wins is the hard half.")
            return ("done" if capped else "settled", msg, "bad")
        if capped:
            kept = ("Converted" if (hold_cp or 0) > 0 else "Held")
            msg = (f"Still {fmt_cp(cp)} after {ply} moves.{mat} {kept} — and "
                   "that, not the first move, is the whole idea.")
            return "done", msg, "good"
        if had_mate and cp <= MATE_THRESHOLD:
            msg = (f"{fmt_cp(cp)} — still winning, but the forced mate has gone. "
                   "Keep going.")
            return "continue", msg, None
        return "continue", f"{fmt_cp(cp)}. Holding it. Keep going.", None

    # punish
    if cp <= -DECISIVE_CP:
        tail = ("" if capped else
                " Play on if you want to see it through.")
        msg = (f"{fmt_cp(cp)}.{mat} That is what the move ran into — not a fact "
               f"to remember, a position you have sat in.{tail}")
        return ("done" if capped else "settled", msg, "bad")
    if capped:
        msg = (f"{fmt_cp(cp)} after {ply} moves.{mat} "
               + ("The punishment is slow, but it is there." if cp < -60
                  else "You have held it better than the line suggested."))
        return "done", msg, None
    return "continue", f"{fmt_cp(cp)}. Keep going.", None


def candidates(fen: str, top_n: int = 4, scorer=None) -> dict:
    """The position's strongest ideas, ranked, with what each one is doing.

    "Best was Nxe5" is one idea out of a position that usually holds several.
    Showing the next few, with the motif each one plays on and how far behind
    best it is, turns the answer back into a position worth thinking about.
    """
    board = _parse(fen)
    if board is None:
        return {"moves": [], "user_colour": "white",
                "message": "that position will not parse"}
    # Only the top few are shown, so only the top few are searched: MultiPV
    # splits the budget across the lines it is asked for, and ranking four ideas
    # properly is the whole job here. A couple of spares so the list is still
    # full if the engine drops one.
    try:
        ranked = (scorer or _backend()).score_roots(board, CANDIDATE_NODES,
                                                    limit=top_n + 2)
    except Exception:  # noqa: BLE001
        ranked = []
    if not ranked:
        return {"moves": [], "user_colour": "white" if board.turn == chess.WHITE else "black"}

    best_cp = ranked[0][1]
    out = []
    for mv, cp in ranked[:top_n]:
        # `detect_motifs` was written for game analysis, where the question is
        # "you played X, the engine wanted Y". Here every row *is* the move under
        # discussion, so it is passed as both - otherwise each candidate was
        # labelled with the best move's tactic and the whole list claimed to
        # fork. `back-rank-weak` is dropped: it describes the position, so
        # repeating it against every move says nothing about any of them.
        motifs = [MOTIF_LABEL.get(m, m) for m in detect_motifs(board, mv, mv)
                  if m != "back-rank-weak"]
        after = board.copy(stack=False)
        after.push(mv)
        out.append({
            "uci": mv.uci(),
            "san": board.san(mv),
            "cp": cp,
            "eval": fmt_cp(cp),
            "behind": best_cp - cp,
            "motifs": motifs,
            "note": _candidate_note(board, mv, cp, best_cp, after),
            "fen_after": after.fen(),
        })
    return {
        "moves": out,
        "user_colour": "white" if board.turn == chess.WHITE else "black",
    }


def _gap_phrase(best_cp: int, cp: int) -> str | None:
    """How much worse than best this move is, said in the right units.

    Subtracting a mate score from an evaluation produces a number like 99188,
    which rendered as "991.9 worse than best" - true arithmetic about a quantity
    that does not exist. Once a mate is involved the only honest comparison is
    in mates.
    """
    if abs(cp) > MATE_THRESHOLD or abs(best_cp) > MATE_THRESHOLD:
        if cp < -MATE_THRESHOLD:
            n = MATE_CP - abs(cp)
            return f"walks into mate in {n}" if n > 0 else "walks into mate"
        if cp > MATE_THRESHOLD:
            return None                      # the note already says "mate"
        return "misses the mate" if best_cp > MATE_THRESHOLD else None
    behind = best_cp - cp
    return f"but {behind / 100:.1f} worse than best" if behind >= ALT_CP else None


def _candidate_note(board: chess.Board, mv: chess.Move, cp: int, best_cp: int,
                    after: chess.Board) -> str:
    """One clause on what the move is, beyond its number."""
    if after.is_checkmate():
        return "mate"
    bits = []
    if board.is_capture(mv):
        victim = board.piece_type_at(mv.to_square)
        bits.append(f"takes the {_PIECE_NAME[victim]}" if victim else "takes en passant")
    if after.is_check():
        bits.append("with check")
    if mv.promotion:
        bits.append(f"promotes to a {_PIECE_NAME[mv.promotion]}")
    gap = _gap_phrase(best_cp, cp)
    if not bits:
        # A quiet move that is not the best one is still worth distinguishing
        # from the best one: "about as good" is a different recommendation from
        # "this is the move".
        bits.append("quiet" if best_cp - cp <= 0
                    else "playable" if gap else "about as good")
    if gap:
        bits.append(gap)
    return ", ".join(bits)
