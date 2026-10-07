"""Run Stockfish over stored games and fill in per-move analysis + tags.

Only the user's own moves are scored for cp-loss (that's what the weakness
profile is built from), but every position gets an eval so the review view has a
continuous graph.
"""

from __future__ import annotations

import queue
import threading

import chess
import chess.engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from coach.analysis.classify import classify_move
from coach.analysis.timing import compute_seconds_spent
from coach.config import CoachConfig
from coach.store.db import Game, session_factory


def _score_cp(pov_score: chess.engine.PovScore, mate_score: int = 100_000) -> int:
    return pov_score.pov(chess.WHITE).score(mate_score=mate_score)


# Evaluations are clamped to this before differencing into a centipawn loss.
# Without it a missed forced mate scores ~100000 cp_loss and a single such move
# swamps every average in the weakness profile - the position was already
# winning, and "mate in 4 instead of mate in 2" is not a 1000-pawn error. Mate
# detection still uses the raw score, via `mate_missed` / `mate_allowed`.
EVAL_CLAMP_CP = 1500


def clamped_cp_loss(eval_before_cp: int | None, eval_after_cp: int | None,
                    mover_is_white: bool) -> int | None:
    """Bounded cp-loss from two White-POV evaluations."""
    if eval_before_cp is None or eval_after_cp is None:
        return None
    sign = 1 if mover_is_white else -1
    before = max(-EVAL_CLAMP_CP, min(EVAL_CLAMP_CP, sign * eval_before_cp))
    after = max(-EVAL_CLAMP_CP, min(EVAL_CLAMP_CP, sign * eval_after_cp))
    return max(0, before - after)


# How much better the best move must be than the second before the position
# counts as one where the choice really mattered. Used for the mate-specific
# uniqueness test, which cannot use centipawns.
MATE_CP = 90_000


def _analyse_position(engine, board, cfg, *, fast: bool = False, multipv: int | None = None):
    nodes = cfg.engine.analysis_nodes // 4 if fast else cfg.engine.analysis_nodes
    depth = min(cfg.engine.analysis_depth, 14) if fast else cfg.engine.analysis_depth
    mpv = multipv if multipv is not None else (1 if fast else cfg.engine.multipv)
    info = engine.analyse(board, chess.engine.Limit(depth=depth, nodes=nodes), multipv=mpv)
    if isinstance(info, dict):
        info = [info]
    lines = []
    for pv_info in info:
        pv = pv_info.get("pv", [])
        lines.append({
            "cp_white": _score_cp(pv_info["score"]),
            "move": pv[0] if pv else None,
            "pv": pv,
        })
    return lines


def _terminal_eval(board: chess.Board) -> int | None:
    """White-POV score for a finished position, or None if it isn't finished.

    Asking the engine to analyse a checkmate raises; every game has at least one
    move that reaches one, so this is the difference between a clean pass and a
    game that fails at its last ply.
    """
    outcome = board.outcome(claim_draw=False)
    if outcome is None:
        return None
    if outcome.winner is None:
        return 0
    return 100_000 if outcome.winner == chess.WHITE else -100_000


def analyse_game(engine: chess.engine.SimpleEngine, session: Session, game: Game,
                 cfg: CoachConfig, mark_deep: bool = False) -> None:
    board = chess.Board()
    moves = sorted(game.moves, key=lambda m: m.ply)

    user_is_white = game.color == "white"
    prev_opp_swing = 0  # cp swing (user POV) produced by the opponent's last move

    for m in moves:
        board.set_fen(m.fen_before)
        played = chess.Move.from_uci(m.uci)
        mover_is_white = board.turn == chess.WHITE

        # Opponent moves only feed the eval graph -> analyse them cheaply.
        lines = _analyse_position(engine, board, cfg, fast=not m.is_user_move)
        best_white = lines[0]["cp_white"]
        best_move = lines[0]["move"]
        second_white = lines[1]["cp_white"] if len(lines) > 1 else None

        # Eval after the actually-played move. Reuse the matching multipv line
        # when the move is in it; otherwise search the child position.
        #
        # That second search runs at the SAME budget as the first for the user's
        # own moves. It used to run at a quarter of the nodes and two-thirds of
        # the depth, which meant cp-loss differenced two evaluations from two
        # different searches - and only ever for moves outside the top few, which
        # is exactly the population that becomes the mistakes, the blunders and
        # the puzzles. The mistake threshold was in effect applied more harshly
        # to the moves it was judging than to the moves it was judging them
        # against. Opponent moves still get the cheap pass; they only draw a
        # graph.
        after_white = None
        refutation: list[chess.Move] = []
        for ln in lines:
            if ln["move"] == played:
                after_white = ln["cp_white"]
                refutation = list(ln["pv"][1:])
                break
        if after_white is None:
            board.push(played)
            after_white = _terminal_eval(board)
            if after_white is None:
                child = _analyse_position(engine, board, cfg, fast=not m.is_user_move,
                                          multipv=1)
                after_white = child[0]["cp_white"]
                refutation = list(child[0]["pv"])
            board.pop()

        # Convert to the mover's POV. Raw values drive the mate flags; the clamped
        # difference drives cp-loss and everything statistical downstream.
        best_mover = best_white if mover_is_white else -best_white
        after_mover = after_white if mover_is_white else -after_white
        cp_loss = clamped_cp_loss(best_white, after_white, mover_is_white)

        m.eval_before_cp = best_white
        m.eval_after_cp = after_white
        m.best_move_uci = best_move.uci() if best_move else None
        m.best_line = " ".join(x.uci() for x in lines[0]["pv"][:12]) or None
        m.second_best_cp = second_white
        # Mate flags read the RAW scores, never cp-loss. Clamping is right for
        # statistics and wrong here: at +20 pawns, "mate in 3" and "still winning"
        # both clamp to +1500, cp_loss comes out 0, and missing a forced mate
        # while already winning - one of the most common and most teachable
        # errors there is - became invisible to the profile and impossible for
        # the puzzle miner to find.
        m.mate_missed = bool(best_move and best_mover >= MATE_CP and after_mover < MATE_CP)
        m.mate_allowed = bool(after_mover <= -MATE_CP < best_mover)

        # cp swing this move produced, from the user's point of view.
        swing_user = (after_white - best_white) * (1 if user_is_white else -1)

        # Scored for both sides now. The user's moves are what the profile is
        # built from, but the opponents' - who carry a rating in the PGN headers -
        # are the only sample of "what does a 1200 actually play" this tool has,
        # and calibrating the sparring bot needs it. Costs no extra engine time:
        # both evaluations were computed above regardless.
        m.cp_loss = cp_loss

        if m.is_user_move:
            m.refutation_line = " ".join(x.uci() for x in refutation[:10]) or None
            tags = classify_move(
                board, played, best_move, cp_loss, best_white, second_white, cfg.profile
            )
            m.forcingness = tags.forcingness
            m.phase = tags.phase
            m.pillar = tags.pillar
            m.error_bucket = tags.error_bucket
            m.motif = tags.motif or None
            m.endgame_type = tags.endgame_type
            m.context = tags.context
            m.decision_w = tags.decision_w
            # Handed an edge last move but didn't take the strongest continuation.
            m.punish_miss = prev_opp_swing >= 150 and cp_loss >= 60
            if m.punish_miss:
                m.motif = ((m.motif + ",") if m.motif else "") + "missed-punish"
            prev_opp_swing = 0
            if m.clock_ms is not None:
                m.time_trouble = m.clock_ms < 20_000
        else:
            m.phase = None
            prev_opp_swing = swing_user  # remember for the user's reply

    compute_seconds_spent(moves, game.time_control)
    game.analyzed = True
    if mark_deep:
        game.deep_analyzed = True
    session.commit()


def _worker(game_ids: queue.Queue[int | None], cfg: CoachConfig, errors: list,
            deep: bool = False, force: bool = False) -> None:
    Session = session_factory()
    try:
        engine = chess.engine.SimpleEngine.popen_uci(cfg.engine.stockfish_path)
    except FileNotFoundError:
        errors.append(f"stockfish not found at {cfg.engine.stockfish_path!r}")
        return
    engine.configure({"Threads": 1, "Hash": 128})
    from coach.sparring.backend import register_engine, unregister_engine

    register_engine(engine)
    try:
        while True:
            gid = game_ids.get()
            if gid is None:
                return
            with Session() as session:
                game = session.get(Game, gid)
                need = game and (
                    force or (not game.deep_analyzed if deep else not game.analyzed)
                )
                if need:
                    try:
                        analyse_game(engine, session, game, cfg, mark_deep=deep)
                        print(f"  {'deep-' if deep else ''}analysed game {gid} "
                              f"({game.opening or '?'})", flush=True)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"game {gid}: {exc}")
    finally:
        unregister_engine(engine)
        engine.quit()


def analyse_pending(cfg: CoachConfig, limit: int | None = None, deep: bool = False,
                    refresh: bool = False) -> int:
    """Analyse games that need it.

    ``refresh`` re-analyses games that are already marked analysed, newest first.
    It exists because the meaning of ``cp_loss`` changed: rows written before the
    child position was searched at the full budget hold values that are not
    comparable with new ones. Re-running is an hour of Stockfish for a few
    hundred games, so it is an explicit choice rather than something that
    happens the next time the app opens.
    """
    if deep:
        cfg = cfg.deepened()
    Session = session_factory()
    with Session() as session:
        if refresh:
            q = select(Game.id).where(Game.analyzed.is_(True))
        else:
            col = Game.deep_analyzed if deep else Game.analyzed
            q = select(Game.id).where(col.is_(False))
            if deep:
                q = q.where(Game.analyzed.is_(True))
        q = q.order_by(Game.played_at.desc())
        if limit:
            q = q.limit(limit)
        ids = list(session.scalars(q))
    if not ids:
        print("nothing to analyse")
        return 0

    work: queue.Queue[int | None] = queue.Queue()
    for gid in ids:
        work.put(gid)
    errors: list[str] = []
    n_workers = max(1, cfg.engine.workers)
    for _ in range(n_workers):
        work.put(None)

    threads = [threading.Thread(target=_worker, args=(work, cfg, errors, deep, refresh),
                                daemon=True)
               for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for e in errors:
        print(f"  ! {e}", flush=True)

    # Derived fields are cheap to recompute and must stay consistent with the
    # current rules, including for rows written by an older build.
    from coach.analysis.repair import repair_all

    fixed = repair_all(cfg=cfg)
    for label, n in fixed.items():
        if n:
            print(f"  repaired {label}: {n}", flush=True)
    return len(ids) - len(errors)
