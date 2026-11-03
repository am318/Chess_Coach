"""How a human at your level would play: Maia, through lc0, when it's installed.

Stockfish says what is *best*. It cannot say what is *likely*, and that is
the question behind two things the coach wants to know:

* **Is this mistake typical or personal?** A blunder that a third of players
  at your rating would also play is a known trap of that level; one that
  almost nobody makes is something particular to you, and the more useful
  thing to drill. The weakness profile reports the split.
* **What will you play next?** The sparring bot can lean towards moves whose
  most likely human reply is a mistake: setting the traps a player at your
  level actually falls into, rather than ones only an engine would see.

Maia (McIlroy-Young et al., CSSLab) is a set of Leela networks trained to
predict the move a human at a given rating plays. The policy head *is* that
prediction, so one node per position is enough. This module is optional
from end to end: without lc0 and the weights, ``available()`` is False and
every caller carries on without it.

Setup (once):

    brew install lc0
    # weights: https://github.com/CSSLab/maia-chess/tree/master/maia_weights
    # put maia-1100.pb.gz ... maia-1900.pb.gz in ~/.maia (or set [maia] weights_dir)
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from collections.abc import Iterable
from pathlib import Path

import chess

MAIA_RATINGS = tuple(range(1100, 2000, 100))
DEFAULT_WEIGHTS_DIR = Path(os.environ.get("MAIA_WEIGHTS_DIR", "~/.maia")).expanduser()
# P(played move) at or above this: a mistake many players at that level make.
TYPICAL_P = 0.25
# At or below: almost nobody at that level plays it.
PERSONAL_P = 0.05

# lc0 VerboseMoveStats line, e.g.
#   "e2e4  (322 ) N:       0 (+ 0) (P: 12.34%) (WL: ...) ..."
_STAT = re.compile(r"^(?P<uci>[a-h][1-8][a-h][1-8][qrbn]?)\s.*?\(P:\s*(?P<p>[\d.]+)%\)")


def weights_path(rating: int, weights_dir: Path | None = None) -> Path | None:
    """The Maia net nearest the rating, if it is on disk."""
    d = weights_dir or DEFAULT_WEIGHTS_DIR
    want = min(MAIA_RATINGS, key=lambda r: abs(r - (rating or 1500)))
    for r in sorted(MAIA_RATINGS, key=lambda r: abs(r - want)):
        for name in (f"maia-{r}.pb.gz", f"maia-{r}.pb"):
            p = d / name
            if p.exists():
                return p
    return None


def parse_policy(lines: Iterable[str], board: chess.Board) -> dict[str, float]:
    """lc0's verbose move stats -> {uci: probability}, normalised over legal moves."""
    out: dict[str, float] = {}
    legal = {m.uci() for m in board.legal_moves}
    for line in lines:
        m = _STAT.match(line.strip())
        if not m:
            continue
        uci = m.group("uci")
        # lc0 writes castling king-takes-rook (e1h1); python-chess wants e1g1.
        if uci not in legal:
            try:
                mv = board.parse_uci(uci)
                uci = mv.uci()
            except ValueError:
                continue
        out[uci] = float(m.group("p")) / 100.0
    total = sum(out.values())
    if total > 0:
        out = {k: v / total for k, v in out.items()}
    return out


class MaiaPredictor:
    """One lc0 process, its weights switched to match the rating asked for."""

    def __init__(self, lc0_path: str = "lc0", weights_dir: Path | None = None) -> None:
        self.lc0_path = lc0_path
        self.weights_dir = weights_dir or DEFAULT_WEIGHTS_DIR
        self._engine = None
        self._loaded: Path | None = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return bool(shutil.which(self.lc0_path)) and weights_path(1500, self.weights_dir) is not None

    def _ensure(self, weights: Path):
        import chess.engine

        if self._engine is None:
            self._engine = chess.engine.SimpleEngine.popen_uci(self.lc0_path)
            self._engine.configure({"VerboseMoveStats": True})
        if self._loaded != weights:
            self._engine.configure({"WeightsFile": str(weights)})
            self._loaded = weights
        return self._engine

    def policy(self, board: chess.Board, rating: int) -> dict[str, float]:
        """P(move) for a player of ``rating``, or {} if anything goes wrong."""
        import chess.engine

        if board.is_game_over():
            return {}
        w = weights_path(rating, self.weights_dir)
        if w is None:
            return {}
        try:
            with self._lock:
                eng = self._ensure(w)
                lines: list[str] = []
                with eng.analysis(board, chess.engine.Limit(nodes=1)) as an:
                    for info in an:
                        if "string" in info:
                            lines.append(info["string"])
            return parse_policy(lines, board)
        except Exception:  # noqa: BLE001 - an optional signal never breaks the caller
            return {}

    def top(self, board: chess.Board, rating: int) -> tuple[chess.Move, float] | None:
        pol = self.policy(board, rating)
        if not pol:
            return None
        uci, p = max(pol.items(), key=lambda kv: kv[1])
        return chess.Move.from_uci(uci), p

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:  # noqa: BLE001, S110
                pass
            self._engine = None


_SHARED: MaiaPredictor | None = None


def shared() -> MaiaPredictor | None:
    """The configured predictor, or None when Maia isn't set up."""
    global _SHARED
    if _SHARED is None:
        try:
            from coach.config import CoachConfig

            cfg = CoachConfig.load().maia
            p = MaiaPredictor(cfg.lc0_path, Path(cfg.weights_dir).expanduser())
        except Exception:  # noqa: BLE001 - no config means the defaults
            p = MaiaPredictor()
        _SHARED = p
    return _SHARED if _SHARED.available() else None


def classify(p: float | None) -> str | None:
    if p is None:
        return None
    if p >= TYPICAL_P:
        return "typical"
    if p <= PERSONAL_P:
        return "personal"
    return "between"


def tag_mistakes(db_path=None, predictor: MaiaPredictor | None = None, limit: int = 2000) -> int:
    """Store P(played move) under Maia at the player's rating, for mistakes and
    blunders not yet tagged. Returns how many were tagged."""
    from sqlalchemy import select

    from coach.puzzles.miner import user_elo_for
    from coach.store.db import Game, Move, session_factory

    predictor = predictor or shared()
    if predictor is None:
        return 0
    n = 0
    with session_factory(db_path)() as session:
        rows = session.execute(
            select(Move, Game).join(Game, Move.game_id == Game.id)
            .where(Move.is_user_move.is_(True), Move.human_p.is_(None),
                   Move.error_bucket.in_(("mistake", "blunder")))
            .limit(limit)
        ).all()
        for m, g in rows:
            try:
                board = chess.Board(m.fen_before)
            except ValueError:
                continue
            pol = predictor.policy(board, user_elo_for(g) or 1500)
            if not pol:
                continue
            m.human_p = round(pol.get(m.uci, 0.0), 4)
            m.human_top = max(pol.items(), key=lambda kv: kv[1])[0]
            n += 1
            if n % 50 == 0:
                session.commit()
        session.commit()
    return n
