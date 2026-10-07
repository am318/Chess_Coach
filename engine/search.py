"""Iterative-deepening negamax search with alpha-beta and the usual heuristics.

Pure python-chess move generation for v1. The plan (M6) swaps the hot path for a
numba bitboard generator; keep this module's public surface
(``Searcher.search_position``) stable so that swap is internal.

Heuristics: transposition table, PVS, aspiration windows, quiescence with SEE
pruning, null-move pruning, reverse futility pruning, late-move reductions,
check extensions, mate-distance pruning, in-search repetition detection,
killers + history move ordering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from itertools import islice

import chess
import chess.polyglot

from engine.eval_classical import evaluate
from engine.see import see

MATE = 30000
MATE_IN_MAX = MATE - 1000
INF = 40000

_EXACT, _LOWER, _UPPER = 0, 1, 2

_PIECE_VAL = {
    chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
    chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 20000,
}

# Measured on CPython 3.12 by filling a dict with a distinct Move per entry:
# 600k entries cost 186 MB resident, i.e. ~326 bytes each once the dict table,
# the int key, the tuple and the Move object are counted. Rounded up a little so
# the budget is not quietly exceeded.
_TT_BYTES_PER_ENTRY = 330
_DEFAULT_TT_BUDGET_MB = 256
# Fraction of the table discarded when it fills. A full clear threw away the
# root entry mid-search, which is where search_position reads its best move.
_TT_EVICT_FRACTION = 4
_ASPIRATION_DELTA = 30
# Quiescence checks for stalemate only when the side to move has at most this
# many pieces (king included). Above it, a genuine stalemate needs every one
# of them blocked, which does not happen in practice.
_STALEMATE_SCAN_PIECES = 4


class TimeUp(Exception):
    """Raised inside the search when the hard deadline is hit."""


@dataclass
class SearchResult:
    best_move: chess.Move | None
    score: int
    depth: int
    nodes: int
    pv: list[chess.Move] = field(default_factory=list)


@dataclass
class SearchLimits:
    soft_time_ms: int = 1_000
    hard_time_ms: int = 2_000
    max_depth: int = 64


def tt_entries_for_budget(budget_mb: int) -> int:
    """Entries that fit in ``budget_mb`` of memory, floored at something useful."""
    return max(50_000, int(budget_mb * 1024 * 1024 / _TT_BYTES_PER_ENTRY))


class Searcher:
    """Holds search state that persists between moves (TT, history, killers)."""

    def __init__(self, tt_budget_mb: int = _DEFAULT_TT_BUDGET_MB) -> None:
        self.max_tt_entries = tt_entries_for_budget(tt_budget_mb)
        # key -> (depth, flag, score, best_move). The move is a chess.Move
        # rather than a uci string: building one string per node, and another
        # per move in the ordering comparison, was pure overhead.
        self.tt: dict[int, tuple[int, int, int, chess.Move | None]] = {}
        self.history: dict[tuple[bool, int, int], int] = {}
        self.killers: list[list[chess.Move | None]] = [[None, None] for _ in range(128)]
        self.nodes = 0
        self._deadline = 0.0

    # ------------------------------------------------------------------ public

    def search_position(self, board: chess.Board, limits: SearchLimits) -> SearchResult:
        self.nodes = 0
        self._deadline = time.monotonic() + limits.hard_time_ms / 1000.0
        soft_deadline = time.monotonic() + limits.soft_time_ms / 1000.0

        self.killers = [[None, None] for _ in range(128)]
        # Age history so recent moves dominate without unbounded growth.
        self.history = {k: v >> 1 for k, v in self.history.items() if v > 1}

        best = SearchResult(best_move=None, score=0, depth=0, nodes=0)
        score = 0
        # TimeUp unwinds out of the middle of the move loop, so the pushes made
        # on the way down are never popped. Left as-is the caller gets a board
        # several plies deep in a line it never played: the root hash below
        # misses, and Engine.get_move's own legality check then rejects a
        # perfectly good move and plays the first legal one instead. Every
        # search that runs out of time has to hand the board back as it found it.
        root_ply = len(board.move_stack)

        for depth in range(1, limits.max_depth + 1):
            try:
                score = self._aspiration(board, depth, score)
            except TimeUp:
                _unwind(board, root_ply)
                break

            tt_move = self.tt.get(chess.polyglot.zobrist_hash(board), (0, 0, 0, None))[3]
            best = SearchResult(
                best_move=tt_move or best.best_move,
                score=score,
                depth=depth,
                nodes=self.nodes,
                pv=self._extract_pv(board, depth),
            )

            if abs(score) >= MATE_IN_MAX:
                break
            if time.monotonic() >= soft_deadline:
                break

        if best.best_move is None:
            best.best_move = next(iter(board.legal_moves), None)
        return best

    def rank_root_moves(
        self, board: chess.Board, depth: int, hard_time_ms: int = 5_000
    ) -> list[tuple[chess.Move, int]]:
        """Score every legal move to a fixed depth. Used by the coaching bot's
        sparring opponent to pick human-like / weakness-targeting moves, not just
        the best one. Returned sorted best-first, scores in the mover's POV (cp)."""
        self.nodes = 0
        self._deadline = time.monotonic() + hard_time_ms / 1000.0
        root_ply = len(board.move_stack)
        scored: list[tuple[chess.Move, int]] = []
        for move in board.legal_moves:
            board.push(move)
            try:
                score = -self._negamax(board, depth - 1, -INF, INF, 1)
            except TimeUp:
                _unwind(board, root_ply)
                break
            board.pop()
            scored.append((move, score))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored

    # ----------------------------------------------------------------- internal

    def _aspiration(self, board: chess.Board, depth: int, prev_score: int) -> int:
        if depth <= 3:
            return self._negamax(board, depth, -INF, INF, 0)
        delta = _ASPIRATION_DELTA
        alpha = max(-INF, prev_score - delta)
        beta = min(INF, prev_score + delta)
        while True:
            score = self._negamax(board, depth, alpha, beta, 0)
            if score <= alpha:
                alpha = max(-INF, alpha - delta)
                delta *= 2
            elif score >= beta:
                beta = min(INF, beta + delta)
                delta *= 2
            else:
                return score

    def _check_time(self) -> None:
        if self.nodes & 2047 == 0 and time.monotonic() >= self._deadline:
            raise TimeUp

    def _negamax(self, board: chess.Board, depth: int, alpha: int, beta: int, ply: int) -> int:
        self.nodes += 1
        self._check_time()
        is_pv = beta - alpha > 1

        if ply > 0:
            if board.is_repetition(2) or board.is_fifty_moves() or board.is_insufficient_material():
                return 0
            # Mate-distance pruning.
            alpha = max(alpha, -MATE + ply)
            beta = min(beta, MATE - ply - 1)
            if alpha >= beta:
                return alpha

        # Terminal positions are detected from the move list generated for the
        # search below, not with a separate ``is_checkmate()`` + full scan up
        # front: those cost two extra legal-move generations on *every* node,
        # and movegen is what this search spends its time on.
        key = chess.polyglot.zobrist_hash(board)
        tt_move: chess.Move | None = None
        entry = self.tt.get(key)
        if entry is not None:
            e_depth, e_flag, e_score, e_move = entry
            tt_move = e_move
            if e_depth >= depth and ply > 0 and not is_pv:
                e_score = _score_from_tt(e_score, ply)
                if e_flag == _EXACT:
                    return e_score
                if e_flag == _LOWER and e_score >= beta:
                    return e_score
                if e_flag == _UPPER and e_score <= alpha:
                    return e_score

        in_check = board.is_check()

        if depth <= 0 and not in_check:
            return self._quiescence(board, alpha, beta, ply)

        static_eval = None if in_check else evaluate(board)

        # Both prunings below answer "am I so far ahead that I need not look?"
        # from the static score alone, and a stalemate is the one position where
        # that reasoning is exactly backwards: the side with no moves holds a
        # draw however far behind it is. Detecting stalemate costs a full
        # movegen, so it is not worth paying on every node - but in a sparse
        # position, where stalemate is a real defensive resource, the prunings
        # step aside and the terminal test below settles it.
        sparse = chess.popcount(board.occupied_co[board.turn]) <= _STALEMATE_SCAN_PIECES

        # Reverse futility pruning (static null move).
        if (
            not is_pv
            and not in_check
            and not sparse
            and depth <= 4
            and abs(beta) < MATE_IN_MAX
            and static_eval - 90 * depth >= beta
        ):
            return static_eval

        # Null-move pruning.
        if (
            not is_pv
            and depth >= 3
            and not in_check
            and not sparse
            and ply > 0
            and static_eval >= beta
            and _has_non_pawn_material(board)
            and beta < MATE_IN_MAX
        ):
            board.push(chess.Move.null())
            reduction = 2 + (depth >= 6)
            null_score = -self._negamax(board, depth - 1 - reduction, -beta, -beta + 1, ply + 1)
            board.pop()
            if null_score >= beta:
                return beta

        # Generated once and reused for the terminal test and the move loop:
        # checkmate and stalemate are read off an empty list rather than paying
        # for a separate ``is_checkmate()`` and full scan on every node.
        moves = list(board.generate_legal_moves())
        if not moves:
            return -MATE + ply if in_check else 0

        best_score = -INF
        best_move: chess.Move | None = None
        alpha_orig = alpha
        moves = self._order(board, moves, tt_move, ply)

        for i, move in enumerate(moves):
            is_capture = board.is_capture(move)

            board.push(move)
            # After the push rather than before it: ``board.gives_check`` is
            # itself a push/pop, so asking first doubled the work per move.
            gives_check = board.is_check()
            is_quiet = not is_capture and not move.promotion and not gives_check
            extension = 1 if gives_check else 0
            new_depth = depth - 1 + extension

            if i == 0:
                score = -self._negamax(board, new_depth, -beta, -alpha, ply + 1)
            else:
                reduction = 0
                if depth >= 3 and i >= 3 and is_quiet and extension == 0:
                    reduction = 1 + (i >= 10) + (not is_pv)
                    reduction = min(reduction, new_depth - 1)
                score = -self._negamax(board, new_depth - reduction, -alpha - 1, -alpha, ply + 1)
                if reduction and score > alpha:
                    score = -self._negamax(board, new_depth, -alpha - 1, -alpha, ply + 1)
                if alpha < score < beta:
                    score = -self._negamax(board, new_depth, -beta, -alpha, ply + 1)
            board.pop()

            if score > best_score:
                best_score = score
                best_move = move
            alpha = max(alpha, score)
            if alpha >= beta:
                if not is_capture:
                    self._record_killer(move, ply)
                    hk = (board.turn, move.from_square, move.to_square)
                    self.history[hk] = self.history.get(hk, 0) + depth * depth
                break

        if best_move is None:
            return 0

        flag = _EXACT
        if best_score <= alpha_orig:
            flag = _UPPER
        elif best_score >= beta:
            flag = _LOWER
        self._store_tt(key, depth, flag, _score_to_tt(best_score, ply), best_move)
        return best_score

    def _quiescence(self, board: chess.Board, alpha: int, beta: int, ply: int) -> int:
        self.nodes += 1
        self._check_time()

        if board.is_check():
            # Resolve checks fully so we don't stand pat in a lost position.
            return self._negamax(board, 1, alpha, beta, ply)

        # Standing pat in a stalemate reports a won endgame as won when it is a
        # draw - the one place quiescence's "assume a quiet move exists" is
        # actively wrong. Checking every node would cost a full movegen each
        # time, so only sparse positions, where stalemate is a real resource,
        # pay for it.
        if chess.popcount(board.occupied_co[board.turn]) <= _STALEMATE_SCAN_PIECES and not any(
            board.generate_legal_moves()
        ):
            return 0

        stand_pat = evaluate(board)
        if stand_pat >= beta:
            return beta
        alpha = max(alpha, stand_pat)

        for move in self._ordered_captures(board):
            # Delta pruning: even winning this capture can't raise alpha.
            victim = board.piece_type_at(move.to_square)
            gain = _PIECE_VAL.get(victim, 100) if victim else 100
            if stand_pat + gain + 200 < alpha:
                continue
            if see(board, move) < 0:
                continue
            board.push(move)
            score = -self._quiescence(board, -beta, -alpha, ply + 1)
            board.pop()
            if score >= beta:
                return beta
            alpha = max(alpha, score)
        return alpha

    # ------------------------------------------------------------- move ordering

    def _order(self, board: chess.Board, moves: list[chess.Move],
               tt_move: chess.Move | None, ply: int) -> list[chess.Move]:
        killers = self.killers[ply] if ply < len(self.killers) else [None, None]

        def score(move: chess.Move) -> int:
            if move == tt_move:
                return 2_000_000
            if board.is_capture(move):
                s = see(board, move)
                base = 1_000_000 if s >= 0 else 200_000
                return base + s
            if move.promotion:
                return 900_000 + _PIECE_VAL.get(move.promotion, 0)
            if move in killers:
                return 800_000
            return self.history.get((board.turn, move.from_square, move.to_square), 0)

        return sorted(moves, key=score, reverse=True)

    def _ordered_captures(self, board: chess.Board) -> list[chess.Move]:
        """Captures and promotions, best-first by SEE.

        Generated straight from the capture/promotion masks rather than by
        filtering the full legal move list: quiescence is most of the tree, and
        generating every quiet move only to throw it away was the bulk of its
        cost.
        """
        caps = [(see(board, m), m) for m in board.generate_legal_captures()]
        # Capture-promotions are already in there; these are the quiet pushes to
        # the last rank, which change material just as much.
        rank7 = chess.BB_RANK_7 if board.turn == chess.WHITE else chess.BB_RANK_2
        promoters = board.pawns & board.occupied_co[board.turn] & rank7
        if promoters:
            for m in board.generate_legal_moves(promoters, ~board.occupied):
                if m.promotion:
                    caps.append((_PIECE_VAL[m.promotion] - _PIECE_VAL[chess.PAWN], m))
        caps.sort(key=lambda t: t[0], reverse=True)
        return [m for _, m in caps]

    def _record_killer(self, move: chess.Move, ply: int) -> None:
        if ply >= len(self.killers):
            return
        slot = self.killers[ply]
        if slot[0] != move:
            slot[1] = slot[0]
            slot[0] = move

    # -------------------------------------------------------------------- tt/pv

    def _store_tt(self, key: int, depth: int, flag: int, score: int,
                  move: chess.Move | None) -> None:
        if len(self.tt) >= self.max_tt_entries:
            self._evict()
        prev = self.tt.get(key)
        if prev is None or depth >= prev[0]:
            self.tt[key] = (depth, flag, score, move)

    def _evict(self) -> None:
        """Drop the oldest quarter of the table.

        Dicts preserve insertion order, so the first keys are the ones written
        longest ago - a cheap stand-in for aging that needs no extra bookkeeping
        in the hot path. (Overwriting a key keeps its original position, so this
        ages by first write rather than last; the entries that matter most, the
        ones near the root of the current search, are rewritten immediately
        after and land at the end.)
        """
        drop = max(1, len(self.tt) // _TT_EVICT_FRACTION)
        for key in list(islice(self.tt, drop)):
            del self.tt[key]

    def _extract_pv(self, board: chess.Board, max_len: int) -> list[chess.Move]:
        pv: list[chess.Move] = []
        b = board.copy(stack=False)
        seen: set[int] = set()
        for _ in range(max_len):
            key = chess.polyglot.zobrist_hash(b)
            if key in seen:
                break
            seen.add(key)
            entry = self.tt.get(key)
            if entry is None or entry[3] is None:
                break
            move = entry[3]
            if not b.is_legal(move):
                break
            pv.append(move)
            b.push(move)
        return pv


def _unwind(board: chess.Board, root_ply: int) -> None:
    """Pop back to ``root_ply`` after the search bailed out mid-line."""
    while len(board.move_stack) > root_ply:
        board.pop()


def _score_to_tt(score: int, ply: int) -> int:
    """Make a mate score absolute before storing it.

    A score of "mate in 2 from here" is only meaningful relative to the node it
    was found at. Stored raw, a transposition reached at a different distance
    from the root reads it back as a different - and wrong - mate distance.
    """
    if score >= MATE_IN_MAX:
        return score + ply
    if score <= -MATE_IN_MAX:
        return score - ply
    return score


def _score_from_tt(score: int, ply: int) -> int:
    """Re-anchor a stored mate score to the node reading it."""
    if score >= MATE_IN_MAX:
        return score - ply
    if score <= -MATE_IN_MAX:
        return score + ply
    return score


def _has_non_pawn_material(board: chess.Board) -> bool:
    stm = board.turn
    return bool(
        board.pieces(chess.KNIGHT, stm)
        or board.pieces(chess.BISHOP, stm)
        or board.pieces(chess.ROOK, stm)
        or board.pieces(chess.QUEEN, stm)
    )
