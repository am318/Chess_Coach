"""Self-play match runner with live SPRT.

Examples
--------
# Sanity: the engine should crush a greedy 1-ply opponent.
python -m tests.selfplay.run --a engine --b greedy --games 40 --movetime 100

# A/B two engine configs (JSON overrides merged into EngineConfig / search):
python -m tests.selfplay.run --a engine --b engine \
    --a-opts '{}' --b-opts '{"no_nullmove": true}' --games 400 --movetime 150
"""

from __future__ import annotations

import argparse
import json
import random

import chess

from engine.config import EngineConfig
from engine.engine import Engine
from engine.eval_classical import evaluate
from engine.search import SearchLimits
from tests.selfplay.match import play_game
from tests.selfplay.openings import BALANCED_FENS
from tests.selfplay.sprt import sprt


def _random_movefn(board: chess.Board, time_left_ms: int) -> str:
    return random.choice(list(board.legal_moves)).uci()


def _greedy_movefn(board: chess.Board, time_left_ms: int) -> str:
    best, best_score = None, -10**9
    for move in board.legal_moves:
        board.push(move)
        score = -evaluate(board)
        board.pop()
        if score > best_score:
            best_score, best = score, move
    return (best or next(iter(board.legal_moves))).uci()


def _make_engine_movefn(opts: dict, movetime_ms: int):
    cfg = EngineConfig()
    cfg.book_path = None  # deterministic, no book in self-play
    eng = Engine(cfg)

    depth = opts.get("depth")

    def move(board: chess.Board, time_left_ms: int) -> str:
        if depth:
            res = eng.searcher.search_position(
                board, SearchLimits(soft_time_ms=10**7, hard_time_ms=10**7, max_depth=depth)
            )
            return (res.best_move or next(iter(board.legal_moves))).uci()
        return eng.get_move(board.fen(), min(time_left_ms, movetime_ms * 30))

    return move


def _build(kind: str, opts: dict, movetime_ms: int):
    if kind == "random":
        return _random_movefn
    if kind == "greedy":
        return _greedy_movefn
    if kind == "engine":
        return _make_engine_movefn(opts, movetime_ms)
    raise SystemExit(f"unknown engine kind: {kind}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="engine", choices=["engine", "greedy", "random"])
    ap.add_argument("--b", default="greedy", choices=["engine", "greedy", "random"])
    ap.add_argument("--a-opts", default="{}")
    ap.add_argument("--b-opts", default="{}")
    ap.add_argument("--games", type=int, default=40)
    ap.add_argument("--movetime", type=int, default=100, help="ms per move for engine kind")
    ap.add_argument("--elo0", type=float, default=0.0)
    ap.add_argument("--elo1", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    random.seed(args.seed)
    a_opts, b_opts = json.loads(args.a_opts), json.loads(args.b_opts)

    wins = draws = losses = 0  # from A's perspective
    flags = 0
    fens = list(BALANCED_FENS)
    random.shuffle(fens)

    for i in range(args.games):
        start = fens[i % len(fens)]
        a_is_white = i % 2 == 0
        a_fn = _build(args.a, a_opts, args.movetime)
        b_fn = _build(args.b, b_opts, args.movetime)
        white, black = (a_fn, b_fn) if a_is_white else (b_fn, a_fn)
        out = play_game(white, black, start_fen=start, start_clock_ms=args.movetime * 40)
        if out.flagged:
            flags += 1

        a_score = out.result if a_is_white else 1.0 - out.result
        if a_score == 1.0:
            wins += 1
        elif a_score == 0.0:
            losses += 1
        else:
            draws += 1

        r = sprt(wins, draws, losses, args.elo0, args.elo1)
        print(
            f"[{i+1:4d}/{args.games}] W-D-L {wins}-{draws}-{losses} "
            f"elo {r.elo:+.0f} [{r.elo_ci95[0]:+.0f},{r.elo_ci95[1]:+.0f}] "
            f"LLR {r.llr:+.2f} ({r.lower_bound:.2f},{r.upper_bound:.2f}) "
            f"{r.decision}  last={out.reason}",
            flush=True,
        )
        if r.decision != "continue":
            print(f"\nSPRT {r.decision} after {i+1} games. flags={flags}")
            return

    print(f"\nInconclusive after {args.games} games. flags={flags}")


if __name__ == "__main__":
    main()
