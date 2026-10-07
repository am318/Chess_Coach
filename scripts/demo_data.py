"""Build a throwaway demo database so the coach can be tried without any real account.

    python scripts/demo_data.py [--games 36] [--dir demo]

Plays synthetic games between the sparring bot at assorted Elo levels (a "demo
player" at club-beginner strength who likes provocative lines, against opponents
of varying strength), writes them out as PGN with made-up names and clock
readings, then runs the normal pipeline over them: import, Stockfish analysis,
puzzle mining, drill seeding, weakness profile. Nothing is downloaded and nothing
here comes from a real person's games.

The result lives in ``<dir>/coach.toml`` + ``<dir>/coach.db``. Run the app on it with

    COACH_TOML=demo/coach.toml COACH_DB=demo/coach.db python -m coach.cli serve --no-refresh
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import random
import subprocess
import sys
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parent.parent
PLAYER = "demo_player"

# Short, ordinary opening lines so the openings pillar has something to chew on:
# systems the demo player plays as White, and defences it plays as Black.
WHITE_OPENINGS = [
    ("Italian Game", "C50", "e4 e5 Nf3 Nc6 Bc4"),
    ("Ruy Lopez", "C60", "e4 e5 Nf3 Nc6 Bb5"),
    ("Scotch Game", "C45", "e4 e5 Nf3 Nc6 d4 exd4 Nxd4"),
    ("Queen's Gambit", "D06", "d4 d5 c4"),
    ("London System", "D00", "d4 d5 Bf4 Nf6 e3"),
]
BLACK_OPENINGS = [
    ("Sicilian Defense", "B20", "e4 c5 Nf3 d6 d4 cxd4 Nxd4"),
    ("French Defense", "C00", "e4 e6 d4 d5"),
    ("Caro-Kann Defense", "B10", "e4 c6 d4 d5"),
    ("King's Indian Defense", "E60", "d4 Nf6 c4 g6 Nc3 Bg7"),
]
CONTROLS = ["180+0", "300+3", "300+0", "600+0", "900+10"]


def _play(white, black, opening_san: str, rng: random.Random, base_s: int, inc: int):
    board = chess.Board()
    game = chess.pgn.Game()
    node = game
    clocks = {chess.WHITE: float(base_s), chess.BLACK: float(base_s)}
    book = opening_san.split()

    def push(move: chess.Move) -> None:
        nonlocal node
        side = board.turn
        spent = min(clocks[side] - 1, rng.lognormvariate(1.3, 0.8) * (base_s / 300) ** 0.5)
        clocks[side] = max(1.0, clocks[side] - max(0.5, spent) + inc)
        board.push(move)
        node = node.add_variation(move)
        node.set_clock(clocks[not board.turn])

    for san in book:
        push(board.parse_san(san))
    while not board.is_game_over(claim_draw=True) and board.fullmove_number < 90:
        mover = white if board.turn == chess.WHITE else black
        push(mover.choose(board))
    return game, board


def generate(n_games: int, seed: int) -> str:
    from coach.sparring.opponent import SparringOpponent

    rng = random.Random(seed)
    me = SparringOpponent(target_elo=950, style="provocative", seed=seed)
    opp = SparringOpponent(target_elo=1200, seed=seed + 1, backend=me.backend)
    today = dt.datetime.now(dt.UTC).date()
    out: list[str] = []
    for i in range(n_games):
        elo = rng.choice([800, 950, 1100, 1250, 1400, 1550])
        opp.set_profile(target_elo=elo)
        me.set_profile(target_elo=rng.choice([900, 950, 1000, 1050]))
        i_am_white = i % 2 == 0
        pool = WHITE_OPENINGS if i_am_white else BLACK_OPENINGS
        name, eco, line = pool[(i // 2) % len(pool)]
        control = CONTROLS[i % len(CONTROLS)]
        base_s, inc = (int(x) for x in control.split("+"))
        w, b = (me, opp) if i_am_white else (opp, me)
        game, board = _play(w, b, line, rng, base_s, inc)
        when = today - dt.timedelta(days=(n_games - i) * 40 // n_games + 1)
        opp_name = f"opponent_{i + 1:02d}"
        h = game.headers
        h["Event"] = "Demo game"
        h["Site"] = "demo"
        h["Date"] = when.strftime("%Y.%m.%d")
        h["UTCTime"] = f"{rng.randint(8, 22):02d}:{rng.randint(0, 59):02d}:00"
        h["White"], h["Black"] = (PLAYER, opp_name) if i_am_white else (opp_name, PLAYER)
        my_elo = 1000 + rng.randint(-40, 40)
        h["WhiteElo"], h["BlackElo"] = (
            (str(my_elo), str(elo)) if i_am_white else (str(elo), str(my_elo)))
        h["TimeControl"] = control
        h["Opening"], h["ECO"] = name, eco
        result = board.result(claim_draw=True)
        if result == "*":  # move cap hit: adjudicate on material rather than leave it open
            mat = sum(v * (len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK)))
                      for pt, v in ((1, 1), (2, 3), (3, 3), (4, 5), (5, 9)))
            result = "1-0" if mat > 2 else "0-1" if mat < -2 else "1/2-1/2"
        h["Result"] = result
        out.append(str(game))
        print(f"  game {i + 1}/{n_games}: {h['White']} v {h['Black']} {result}", flush=True)
    me.close()
    return "\n\n".join(out) + "\n"


def _cli(env: dict[str, str], *args: str) -> None:
    subprocess.run([sys.executable, "-m", "coach.cli", *args], check=True, cwd=ROOT, env=env)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--games", type=int, default=36)
    ap.add_argument("--dir", type=Path, default=ROOT / "demo")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    out = args.dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    db = out / "coach.db"
    if db.exists():
        sys.exit(f"{db} already exists; delete {out} first")

    toml = (ROOT / "coach.example.toml").read_text()
    toml = toml.replace('lichess = ["your_lichess_username"]', f'lichess = ["{PLAYER}"]')
    toml = toml.replace('chesscom = ["your_chesscom_username"]', "chesscom = []")
    toml = toml.replace("auto_sync = true", "auto_sync = false")
    # Demo analysis is shallow so it finishes in a minute or two, not an hour.
    toml = toml.replace("analysis_nodes = 300000", "analysis_nodes = 40000")
    toml = toml.replace("analysis_depth = 18", "analysis_depth = 14")
    toml = toml.replace("workers = 3", "workers = 4")
    (out / "coach.toml").write_text(toml)

    env = {**os.environ, "COACH_TOML": str(out / "coach.toml"), "COACH_DB": str(db)}
    os.environ.update(env)

    print(f"generating {args.games} synthetic games ...")
    pgn_path = out / "demo_games.pgn"
    pgn_path.write_text(generate(args.games, args.seed))

    _cli(env, "import-pgn", str(pgn_path))
    _stamp_dates_and_speeds(env)
    _cli(env, "analyze")
    _cli(env, "puzzles", "mine")
    _cli(env, "drills", "refresh")
    _cli(env, "profile")
    print(f"\ndemo ready in {out}")


def _stamp_dates_and_speeds(env: dict[str, str]) -> None:
    """PGN import leaves played_at and speed empty; fill them from the headers."""
    os.environ.update(env)
    from coach.store.db import Game, session_factory

    with session_factory()() as s:
        for g in s.query(Game).all():
            game = chess.pgn.read_game(io.StringIO(g.pgn))
            h = game.headers
            # The database stores naive UTC.
            g.played_at = dt.datetime.strptime(
                f"{h['Date']} {h['UTCTime']} +0000", "%Y.%m.%d %H:%M:%S %z").replace(tzinfo=None)
            base, inc = (int(x) for x in g.time_control.split("+"))
            est = base + 40 * inc
            g.speed = "blitz" if est < 480 else "rapid"
        s.commit()


if __name__ == "__main__":
    main()
