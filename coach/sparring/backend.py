"""Root-move scoring for the sparring opponent.

The sparring bot needs a *ranked list of every legal move with a centipawn
score* - not just the best move - because it picks among near-equal options to
hit a target Elo and to steer the game toward the user's weak pillar.

Two backends provide that:

* ``StockfishBackend`` - a persistent Stockfish process with
  ``MultiPV = len(legal_moves)``. Scores every root move in well under a tenth
  of a second, which is what makes the board feel responsive.
* ``NativeBackend`` - the repo's own engine, kept as a fallback for machines
  with no Stockfish and used by the tests so they need no binary.

The competition engine in ``engine/`` is unchanged and unaffected; ``coach/`` was
never sandbox-constrained and already depends on Stockfish for analysis.
"""

from __future__ import annotations

import threading
from typing import Protocol

import chess


class RootScorer(Protocol):
    def score_roots(self, board: chess.Board, budget: int,
                    limit: int | None = None) -> list[tuple[chess.Move, int]]:
        """Every legal move with a score in the mover's POV (cp), best first.

        ``limit`` asks for only the best *n* lines. The sparring bot needs the
        whole list, because it samples among near-equal moves; a caller that
        only shows a handful should say so, since the node budget is split
        across however many lines are requested and thirty-five shallow ones
        rank worse than six deep ones.
        """
        ...

    def best(self, board: chess.Board, budget: int) -> tuple[chess.Move, int] | None:
        """Just the best move and its score, searched at full strength."""
        ...

    def line(self, board: chess.Board, budget: int, limit: int = 8) -> list[chess.Move]:
        """The principal variation from here - what actually happens next."""
        ...

    def close(self) -> None: ...


_MATE_CP = 100_000

# Every Stockfish we spawn is a child *process*, not a thread, so it does not go
# away when the interpreter exits - a quit would leave orphans holding memory
# until they were noticed in Activity Monitor. Anything that opens an engine
# registers it here so shutdown can close the lot.
_OPEN_ENGINES: set = set()
_OPEN_LOCK = threading.Lock()


def register_engine(engine) -> None:
    with _OPEN_LOCK:
        _OPEN_ENGINES.add(engine)


def unregister_engine(engine) -> None:
    with _OPEN_LOCK:
        _OPEN_ENGINES.discard(engine)


def close_all_engines() -> int:
    """Quit every open engine. Returns how many were closed."""
    with _OPEN_LOCK:
        engines = list(_OPEN_ENGINES)
        _OPEN_ENGINES.clear()
    closed = 0
    for engine in engines:
        try:
            engine.quit()
            closed += 1
        except Exception:  # noqa: BLE001, S110 - shutting down regardless
            pass
    return closed


class StockfishBackend:
    """Persistent Stockfish process, MultiPV over the whole root move list.

    The process is started lazily on first use and reused for the rest of the
    session. ``budget`` is a node count: nodes rather than depth keeps the think
    time predictable across quiet and sharp positions.
    """

    def __init__(self, path: str = "stockfish", hash_mb: int = 64, threads: int = 1) -> None:
        self._path = path
        self._hash_mb = hash_mb
        self._threads = threads
        self._engine = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._engine is None:
            import chess.engine

            self._engine = chess.engine.SimpleEngine.popen_uci(self._path)
            self._engine.configure({"Threads": self._threads, "Hash": self._hash_mb})
            register_engine(self._engine)
        return self._engine

    def score_roots(self, board: chess.Board, budget: int,
                    limit: int | None = None) -> list[tuple[chess.Move, int]]:
        import chess.engine

        legal = list(board.legal_moves)
        if not legal:
            return []
        if len(legal) == 1:
            return [(legal[0], 0)]

        want = len(legal) if limit is None else max(1, min(limit, len(legal)))
        with self._lock:
            engine = self._ensure()
            infos = engine.analyse(
                board,
                chess.engine.Limit(nodes=budget),
                multipv=want,
            )

        if isinstance(infos, dict):
            infos = [infos]
        out: list[tuple[chess.Move, int]] = []
        seen: set[chess.Move] = set()
        for info in infos:
            pv = info.get("pv") or []
            if not pv:
                continue
            mv = pv[0]
            if mv in seen:
                continue
            seen.add(mv)
            # POV of the side to move, so "higher is better for the mover".
            out.append((mv, info["score"].pov(board.turn).score(mate_score=_MATE_CP)))
        # Any move MultiPV didn't report (it can drop illegal-after-TT edge cases)
        # goes to the back rather than vanishing from the pool. A limited request
        # asked for a short list and gets one.
        if limit is None and len(out) < len(legal):
            worst = min((cp for _, cp in out), default=0)
            out.extend((mv, worst - 1) for mv in legal if mv not in seen)
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    def best(self, board: chess.Board, budget: int) -> tuple[chess.Move, int] | None:
        """Single-PV search.

        ``score_roots`` splits its node budget across every root move, so at a
        fixed budget each line is searched shallowly - fine for *sampling* among
        near-equal moves, but not for questions with one right answer like "is
        this still winning?" or "what is the best defence?". Those get MultiPV 1
        and the whole budget.
        """
        import chess.engine

        if not list(board.legal_moves):
            return None
        with self._lock:
            engine = self._ensure()
            info = engine.analyse(board, chess.engine.Limit(nodes=budget))
        pv = info.get("pv") or []
        if not pv:
            return None
        return pv[0], info["score"].pov(board.turn).score(mate_score=_MATE_CP)

    def line(self, board: chess.Board, budget: int, limit: int = 8) -> list[chess.Move]:
        """Single-PV search, keeping the whole variation rather than its head.

        Used to show a player what their move ran into. Naming the right move is
        half a lesson; the other half is the sequence that punishes the wrong one.
        """
        import chess.engine

        if board.is_game_over() or not list(board.legal_moves):
            return []
        with self._lock:
            engine = self._ensure()
            info = engine.analyse(board, chess.engine.Limit(nodes=budget))
        return list((info.get("pv") or [])[:limit])

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                unregister_engine(self._engine)
                try:
                    self._engine.quit()
                except Exception:  # noqa: BLE001, S110 - shutting down anyway
                    pass
                self._engine = None


class NativeBackend:
    """Fallback on the repo's own searcher.

    Unlike the old ``rank_root_moves`` this deepens iteratively and keeps only
    *complete* iterations, so a timeout degrades to a shallower full ranking
    rather than to an arbitrary prefix of the move list.
    """

    def __init__(self, max_depth: int = 5, hard_time_ms: int = 1500) -> None:
        from engine.engine import Engine

        self._engine = Engine()
        self._engine.book.close()
        self._searcher = self._engine.searcher
        self._max_depth = max_depth
        self._hard_time_ms = hard_time_ms

    def score_roots(self, board: chess.Board, budget: int,
                    limit: int | None = None) -> list[tuple[chess.Move, int]]:
        legal = list(board.legal_moves)
        if not legal:
            return []
        if len(legal) == 1:
            return [(legal[0], 0)]

        import time

        deadline = time.monotonic() + self._hard_time_ms / 1000.0
        best: list[tuple[chess.Move, int]] = [(mv, 0) for mv in legal]
        for depth in range(1, self._max_depth + 1):
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            scored = self._searcher.rank_root_moves(board, depth, hard_time_ms=remaining_ms)
            if len(scored) < len(legal):
                break          # incomplete iteration - keep the previous full one
            best = scored
        return best if limit is None else best[:limit]

    def best(self, board: chess.Board, budget: int) -> tuple[chess.Move, int] | None:
        ranked = self.score_roots(board, budget, limit=1)
        return ranked[0] if ranked else None

    def line(self, board: chess.Board, budget: int, limit: int = 8) -> list[chess.Move]:
        """Walk the best move forward. The native searcher has no PV to hand
        back, so the line is reconstructed one search at a time - acceptable for
        a fallback that only runs when Stockfish is missing."""
        out: list[chess.Move] = []
        probe = board.copy(stack=False)
        for _ in range(limit):
            if probe.is_game_over():
                break
            top = self.best(probe, budget)
            if top is None:
                break
            out.append(top[0])
            probe.push(top[0])
        return out

    def close(self) -> None:
        return None


def _configured_path() -> str:
    """``[engine] stockfish_path`` from coach.toml, or the bare name.

    Callers used to take the default argument, so a Stockfish that was installed
    somewhere other than PATH silently produced a sparring bot backed by the
    (much weaker, much slower) native searcher, with nothing said about it.
    """
    try:
        from coach.config import CoachConfig

        return CoachConfig.load().engine.stockfish_path or "stockfish"
    except Exception:  # noqa: BLE001 - no config is not a reason to fail here
        return "stockfish"


def make_backend(stockfish_path: str | None = None) -> RootScorer:
    """Stockfish when it's available, the native searcher when it isn't."""
    backend = StockfishBackend(stockfish_path or _configured_path())
    try:
        backend._ensure()
    except (FileNotFoundError, OSError):
        return NativeBackend()
    return backend


_SHARED: RootScorer | None = None
_SHARED_LOCK = threading.Lock()


def shared_backend() -> RootScorer:
    """Process-wide scorer for short one-off checks.

    Puzzle-alternative verification and the in-game blunder check each need a
    single quick evaluation; they share one engine process rather than paying
    Stockfish startup per request. ``StockfishBackend`` serialises on its own
    lock, so this is safe to call from the request threadpool.
    """
    global _SHARED
    if _SHARED is None:
        with _SHARED_LOCK:
            if _SHARED is None:
                _SHARED = make_backend()
    return _SHARED
