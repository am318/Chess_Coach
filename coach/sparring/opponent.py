"""Adaptive sparring opponent.

Wraps the competition search core but deliberately does NOT always play the best
move. It:
  * plays near a target Elo (shallower search + softmax move sampling + a small
    chance of a genuine slip), so games stay competitive and instructive;
  * biases move choice, among near-equal options, toward positions that exercise
    the user's weakest pillar (keep pieces on vs a tactically weak trainee, steer
    to an endgame vs a weak-endgame trainee, keep it quiet/closed vs someone who
    struggles to find moves in quiet positions);
  * never resigns - it plays on so the user practises converting / holding.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass

import chess

from coach.analysis.classify import forcingness, game_phase
from coach.sparring.backend import RootScorer, make_backend

_PIECE_VAL = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


@dataclass
class Strength:
    nodes: int          # search budget for *scoring* the root moves
    window_cp: int      # candidate moves within this of the best are "reasonable"
    temperature: float  # softmax temp over -cp; higher => more random among candidates
    slip_prob: float    # chance of picking from a deliberately widened pool
    depth: int          # nominal depth, kept for display/back-compat

    @staticmethod
    def for_elo(elo: int) -> Strength:
        elo = max(400, min(2400, elo))
        # The scoring budget stays honest at every level - the *ranking* should be
        # accurate, and the playing strength comes from how loosely we sample it.
        # Calibration points below are for the sampling, not the search.
        # The two bottom anchors are measured rather than guessed: `coach
        # calibrate` compares the bot's expected cp-loss against what humans in
        # each rating band in the development corpus actually lose. The original pair
        # was far looser than any human in it - the bot at its 400 floor
        # dropped 120cp a move where 200-399 players drop 90 and 400-599 players
        # 73 - so a bot wearing a 650 label was playing several hundred points
        # below it, the games ended in fifteen moves, and the adaptive Elo spent
        # them climbing back toward a number it was never honouring.
        pts = [
            (400,  Strength(30_000, 231, 143.0, 0.187, 2)),
            (800,  Strength(40_000, 205, 118.5, 0.174, 3)),
            (1200, Strength(50_000, 170, 100.0, 0.13, 5)),
            (1600, Strength(70_000, 85, 55.0, 0.055, 8)),
            (2000, Strength(110_000, 38, 26.0, 0.02, 12)),
            (2400, Strength(180_000, 14, 9.0, 0.0, 16)),
        ]
        for (lo, a), (hi, b) in itertools.pairwise(pts):
            if lo <= elo <= hi:
                t = (elo - lo) / (hi - lo)
                return Strength(
                    nodes=round(a.nodes + t * (b.nodes - a.nodes)),
                    window_cp=round(a.window_cp + t * (b.window_cp - a.window_cp)),
                    temperature=a.temperature + t * (b.temperature - a.temperature),
                    slip_prob=a.slip_prob + t * (b.slip_prob - a.slip_prob),
                    depth=round(a.depth + t * (b.depth - a.depth)),
                )
        return pts[-1][1]


def _material(board: chess.Board) -> int:
    return sum(
        v * (len(board.pieces(pt, chess.WHITE)) + len(board.pieces(pt, chess.BLACK)))
        for pt, v in _PIECE_VAL.items()
    )


class SparringOpponent:
    def __init__(self, target_elo: int = 1500, weak_pillar: str | None = None,
                 style: str = "solid", seed: int | None = None,
                 backend: RootScorer | None = None) -> None:
        self.target_elo = target_elo
        self.weak_pillar = weak_pillar
        self.style = style  # "solid" | "provocative" | "gambit"
        # Built on first use, not on construction. The web app creates one of
        # these the moment a websocket is accepted - before anyone has asked for
        # a game - so opening the Today tab on a laptop and a phone used to hold
        # two Stockfish processes that never played a move. An injected backend
        # (the tests, the calibration) is used as given.
        self._backend: RootScorer | None = backend
        self._rng = random.Random(seed)

    @property
    def backend(self) -> RootScorer:
        if self._backend is None:
            self._backend = make_backend()
        return self._backend

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
            self._backend = None

    def set_profile(self, target_elo: int | None = None, weak_pillar: str | None = None,
                    style: str | None = None) -> None:
        if target_elo is not None:
            self.target_elo = target_elo
        if weak_pillar is not None:
            self.weak_pillar = weak_pillar
        if style is not None:
            self.style = style

    # ------------------------------------------------------------------ move gen

    def move_distribution(
        self,
        board: chess.Board,
        ranked: list[tuple[chess.Move, int]],
        s: Strength,
        *,
        slip: bool = False,
        provoking: bool = False,
    ) -> tuple[list[tuple[chess.Move, int]], list[float]]:
        """The candidate pool and the weight on each, given a root ranking.

        Split out of ``choose`` so that measuring the bot's strength exercises
        exactly the code that plays it. A calibration that reimplemented the
        sampling would be measuring a second bot that merely resembles this one,
        and would go quietly out of date the first time either changed.
        """
        best_cp = ranked[0][1]
        # Always keep a safety net: never hang mate / massive material on a "slip".
        window = s.window_cp * (3 if slip else 1)
        # Provocative / gambit play tolerates a wider band of "not actually losing".
        if self.style == "provocative":
            window = max(window, 130)
        elif self.style == "gambit":
            window = max(window, 220)
        floor = max(best_cp - window, best_cp - 900)
        pool = [(m, cp) for m, cp in ranked if cp >= floor] or [ranked[0]]

        weights = []
        for mv, cp in pool:
            w = math.exp((cp - best_cp) / max(1.0, s.temperature))
            w *= 1.0 + self._weakness_bias(board, mv)
            if provoking:
                w *= 1.0 + 1.6 * self._provocation_score(board, mv)
            weights.append(max(w, 1e-6))
        return pool, weights

    def choose(self, board: chess.Board) -> chess.Move:
        legal = list(board.legal_moves)
        if len(legal) == 1:
            return legal[0]

        s = Strength.for_elo(self.target_elo)
        ranked = self.backend.score_roots(board, s.nodes)
        if not ranked:
            return self._rng.choice(legal)

        slip = self._rng.random() <= s.slip_prob
        provoking = self.style in ("provocative", "gambit") and self._rng.random() < 0.55
        pool, weights = self.move_distribution(board, ranked, s, slip=slip,
                                               provoking=provoking)
        return self._rng.choices([m for m, _ in pool], weights=weights, k=1)[0]

    def _provocation_score(self, board: chess.Board, move: chess.Move) -> float:
        """0..~1: how 'offbeat / against the opening rules' this move is, among
        moves that aren't actually losing. Used so the trainee practises meeting
        and punishing dubious play - premature queen sorties, rook-pawn lunges,
        moving the same piece repeatedly, early king walks, greedy grabs."""
        score = 0.0
        piece = board.piece_type_at(move.from_square)
        from_file = chess.square_file(move.from_square)
        to_rank = chess.square_rank(move.to_square)
        early = board.fullmove_number <= 12
        stm = board.turn

        if early:
            if piece == chess.PAWN and from_file in (0, 7):        # a/h pawn push
                score += 0.4
            if piece == chess.QUEEN and board.fullmove_number <= 7:  # early queen
                adv = to_rank >= 3 if stm == chess.WHITE else to_rank <= 4
                score += 0.5 if adv else 0.2
            if piece == chess.KNIGHT and chess.square_file(move.to_square) in (0, 7):
                score += 0.3                                        # knight to the rim
            if piece == chess.KING and not board.is_castling(move):
                score += 0.35
            # Moving a piece that already left its home square again.
            home_rank = 0 if stm == chess.WHITE else 7
            if piece in (chess.KNIGHT, chess.BISHOP, chess.QUEEN) and \
                    chess.square_rank(move.from_square) != home_rank:
                score += 0.2

        if board.is_capture(move):
            victim = board.piece_type_at(move.to_square)
            if victim == chess.PAWN and piece == chess.QUEEN:
                score += 0.3                                        # queen grabs a pawn

        # A pawn push in front of the opponent's castled king (attacking gesture).
        if piece == chess.PAWN and to_rank in (5, 6) and stm == chess.WHITE:
            score += 0.15
        if piece == chess.PAWN and to_rank in (1, 2) and stm == chess.BLACK:
            score += 0.15

        return min(1.0, score)

    # --------------------------------------------------------------- bias signal

    def _weakness_bias(self, board: chess.Board, move: chess.Move) -> float:
        """Return a small non-negative multiplier bump (0..~0.8) for moves that
        steer the game toward the user's weak pillar."""
        pillar = self.weak_pillar
        if not pillar:
            return 0.0

        after = board.copy(stack=False)
        after.push(move)
        f_after = forcingness(after, None, None)
        mat_before = _material(board)
        mat_after = _material(after)
        traded = mat_before - mat_after  # >0 => this move removed material from the board
        queens = len(after.pieces(chess.QUEEN, chess.WHITE)) + len(after.pieces(chess.QUEEN, chess.BLACK))

        if pillar == "tactics":
            # Keep the position sharp and pieces on the board.
            bias = 0.5 * f_after
            if traded > 0:
                bias -= 0.3
            if queens == 2:
                bias += 0.15
            return max(0.0, bias)
        if pillar == "endgame":
            # Encourage simplification toward an endgame.
            bias = 0.0
            if traded > 0:
                bias += 0.25
            if queens == 0 and game_phase(after) != "opening":
                bias += 0.35
            if game_phase(after) == "endgame":
                bias += 0.3
            return bias
        if pillar in ("quiet", "positional"):
            # Reward quiet, maneuvering continuations.
            return max(0.0, 0.5 * (1.0 - f_after) - (0.2 if traded > 0 else 0.0))
        return 0.0
