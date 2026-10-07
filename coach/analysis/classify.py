"""Tag each analysed move: phase, error bucket, forcing-ness, pillar, motif.

The five pillars (see docs/PLAN.md): opening, tactics, positional, endgame,
quiet ("finding moves when there is no obvious move").
"""

from __future__ import annotations

from dataclasses import dataclass

import chess

from coach.config import ProfileCfg

# Bumped whenever the tag set or the pillar routing changes, so `repair.py` knows
# to walk the stored positions again instead of leaving rows classified by rules
# that no longer exist. v2: pillar routed by error type before phase, plus
# `decision_w`.
CLASSIFIER_VERSION = 3

_PIECE_VAL = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


@dataclass
class MoveTags:
    phase: str
    forcingness: float
    error_bucket: str
    pillar: str
    motif: str
    endgame_type: str | None
    context: str  # "attacking" | "defending" | "balanced"
    decision_w: float = 1.0  # how much genuine choice the position offered, 0..1


def _non_pawn_material(board: chess.Board) -> int:
    n = 0
    for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        n += _PIECE_VAL[pt] * (len(board.pieces(pt, chess.WHITE)) + len(board.pieces(pt, chess.BLACK)))
    return n


def game_phase(board: chess.Board) -> str:
    npm = _non_pawn_material(board)
    queens = len(board.pieces(chess.QUEEN, chess.WHITE)) + len(board.pieces(chess.QUEEN, chess.BLACK))
    if board.fullmove_number <= 12 and npm >= 26:
        return "opening"
    if npm <= 12 or (queens == 0 and npm <= 20):
        return "endgame"
    return "middlegame"


def endgame_type(board: chess.Board) -> str | None:
    if game_phase(board) != "endgame":
        return None
    w = {pt: len(board.pieces(pt, chess.WHITE)) for pt in range(1, 7)}
    b = {pt: len(board.pieces(pt, chess.BLACK)) for pt in range(1, 7)}
    majors = w[chess.ROOK] + b[chess.ROOK] + w[chess.QUEEN] + b[chess.QUEEN]
    minors = w[chess.KNIGHT] + b[chess.KNIGHT] + w[chess.BISHOP] + b[chess.BISHOP]
    if w[chess.QUEEN] + b[chess.QUEEN] > 0:
        return "queen"
    if w[chess.ROOK] + b[chess.ROOK] > 0 and minors == 0:
        return "rook"
    if minors > 0 and majors == 0:
        wb = w[chess.BISHOP] == 1 and b[chess.BISHOP] == 1 and w[chess.KNIGHT] == b[chess.KNIGHT] == 0
        if wb and _opposite_colored_bishops(board):
            return "opposite-bishops"
        return "minor-piece"
    if minors == 0 and majors == 0:
        return "king-pawn"
    return "other"


def _opposite_colored_bishops(board: chess.Board) -> bool:
    sqs = list(board.pieces(chess.BISHOP, chess.WHITE)) + list(board.pieces(chess.BISHOP, chess.BLACK))
    if len(sqs) != 2:
        return False
    return (chess.square_rank(sqs[0]) + chess.square_file(sqs[0])) % 2 != (
        chess.square_rank(sqs[1]) + chess.square_file(sqs[1])) % 2


def forcingness(board: chess.Board, best_cp: int | None, second_cp: int | None) -> float:
    """0 = dead quiet, 1 = highly forcing.

    Combines: is the side to move in check, fraction of legal moves that are
    captures/checks, how few 'reasonable' replies there are, and how large the
    gap is between the best and second-best engine move (a big gap => one move
    really matters => forcing / tactical).
    """
    legal = list(board.legal_moves)
    if not legal:
        return 1.0
    score = 0.0
    if board.is_check():
        score += 0.45

    tactical = 0
    for mv in legal:
        if board.is_capture(mv) or board.gives_check(mv):
            tactical += 1
    score += 0.30 * min(1.0, tactical / max(1, len(legal)) * 3)

    if len(legal) <= 3:
        score += 0.15

    if best_cp is not None and second_cp is not None:
        gap = abs(best_cp - second_cp)
        score += 0.35 * min(1.0, gap / 150.0)

    return max(0.0, min(1.0, score))


def tactical_tension(board: chess.Board) -> bool:
    """Is material actually hanging in this position, for either side?

    A position-only test - it looks at the board before the move and never at
    what was played or how it turned out, which is what lets it decide a pillar
    without making that pillar's statistics circular.

    It exists because ``forcingness`` cannot carry the load alone: with no check
    and few captures available its ceiling is about 0.41, so a typical opening
    position in which a piece is simply hanging never crosses the tactical
    threshold. Sixteen per cent of the opening positions in a real database have
    material en prise; those are tactics lessons whatever the move number says.
    """
    if board.is_check():
        return True
    for sq, piece in board.piece_map().items():
        if piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(not piece.color, sq)
        if not attackers:
            continue
        defenders = board.attackers(piece.color, sq)
        # An undefended piece under attack is hanging outright.
        if piece.piece_type >= chess.KNIGHT and not defenders:
            return True
        # Otherwise it takes more attackers than defenders *and* a cheaper
        # attacker, or the exchange simply loses material for the taker.
        if len(attackers) > len(defenders):
            cheapest = min(
                (_PIECE_VAL[board.piece_type_at(a)] for a in attackers
                 if board.piece_type_at(a) != chess.KING),
                default=99,
            )
            if cheapest < _PIECE_VAL.get(piece.piece_type, 0):
                return True
    return False


def _king_zone(king_sq: int) -> list[int]:
    f, r = chess.square_file(king_sq), chess.square_rank(king_sq)
    out = []
    for df in (-1, 0, 1):
        for dr in (-1, 0, 1):
            nf, nr = f + df, r + dr
            if 0 <= nf <= 7 and 0 <= nr <= 7:
                out.append(chess.square(nf, nr))
    return out


def attack_defence_context(board: chess.Board) -> str:
    """Whose initiative is it? Compare how many enemy pieces bear on each king's
    zone. Cheap proxy: if the side to move has materially more pressure on the
    opponent king than vice versa, they're attacking; if less, defending."""
    stm = board.turn
    my_king = board.king(stm)
    opp_king = board.king(not stm)
    if my_king is None or opp_king is None:
        return "balanced"
    pressure_on_them = sum(
        len(board.attackers(stm, sq)) for sq in _king_zone(opp_king)
    )
    pressure_on_me = sum(
        len(board.attackers(not stm, sq)) for sq in _king_zone(my_king)
    )
    if pressure_on_them >= pressure_on_me + 3:
        return "attacking"
    if pressure_on_me >= pressure_on_them + 3:
        return "defending"
    return "balanced"


def decision_weight(board: chess.Board) -> float:
    """How much of a decision this position actually was, 0..1.

    A forced recapture and a king walking out of check contribute a cp-loss of
    zero to every average they land in, which quietly drags the whole profile
    toward "you play fine". They are not evidence of skill and should not be
    counted as if they were. Weighting by the number of options ramps from 0 for
    a single legal move to 1 once there are four or more.
    """
    n = board.legal_moves.count()
    return max(0.0, min(1.0, (n - 1) / 3.0))


def error_bucket(cp_loss: int | None, cfg: ProfileCfg) -> str:
    if cp_loss is None:
        return "unknown"
    if cp_loss >= cfg.blunder_cp:
        return "blunder"
    if cp_loss >= cfg.mistake_cp:
        return "mistake"
    if cp_loss >= cfg.inaccuracy_cp:
        return "inaccuracy"
    return "ok"


def detect_motifs(board: chess.Board, played: chess.Move, best: chess.Move | None) -> list[str]:
    """Lightweight motif hints for a position where the user went wrong.

    Heuristic, not a solver: flags the tactic the user most likely missed or
    walked into, based on the position before their move and the engine's best
    reply.
    """
    motifs: list[str] = []
    stm = board.turn

    # Hanging piece: after the played move, does the opponent have a capture that
    # wins material by SEE-ish (attacker cheaper than victim, victim undefended)?
    test = board.copy(stack=False)
    test.push(played)
    for mv in test.legal_moves:
        if not test.is_capture(mv):
            continue
        victim = test.piece_type_at(mv.to_square)
        if victim is None:
            continue
        attackers = test.attackers(test.turn, mv.to_square)
        defenders = test.attackers(not test.turn, mv.to_square)
        if victim >= chess.KNIGHT and len(defenders) == 0:
            motifs.append("hangs-piece")
            break
        if victim >= chess.ROOK and len(attackers) > len(defenders):
            motifs.append("wins-exchange")
            break

    # Fork: the engine's best move attacks >=2 valuable pieces at once.
    if best is not None:
        after_best = board.copy(stack=False)
        after_best.push(best)
        moved_to = best.to_square
        piece = after_best.piece_type_at(moved_to)
        if piece:
            targets = 0
            for tsq in after_best.attacks(moved_to):
                tp = after_best.piece_type_at(tsq)
                if tp and after_best.color_at(tsq) != stm and tp >= chess.KNIGHT:
                    targets += 1
            if targets >= 2:
                motifs.append("fork")
            if piece == chess.PAWN and targets >= 2:
                motifs.append("pawn-fork")

    # Back-rank: king on the back rank boxed in by its own pawns.
    king_sq = board.king(stm)
    if king_sq is not None:
        back = 0 if stm == chess.WHITE else 7
        if chess.square_rank(king_sq) == back:
            fwd = 8 if stm == chess.WHITE else -8
            boxed = all(
                (0 <= king_sq + fwd + d < 64)
                and board.piece_type_at(king_sq + fwd + d) == chess.PAWN
                and board.color_at(king_sq + fwd + d) == stm
                for d in (-1, 0, 1)
                if 0 <= chess.square_file(king_sq) + d <= 7
            )
            if boxed:
                motifs.append("back-rank-weak")

    if board.is_pinned(stm, played.from_square):
        motifs.append("moved-pinned-piece")

    return list(dict.fromkeys(motifs))


def classify_move(
    board: chess.Board,
    played: chess.Move,
    best: chess.Move | None,
    cp_loss: int | None,
    best_cp: int | None,
    second_cp: int | None,
    cfg: ProfileCfg,
) -> MoveTags:
    phase = game_phase(board)
    f = forcingness(board, best_cp, second_cp)
    bucket = error_bucket(cp_loss, cfg)
    eg = endgame_type(board)
    motifs = detect_motifs(board, played, best) if bucket in ("mistake", "blunder") else []

    # Pillar routing, decided by what the POSITION was, before what the phase was.
    #
    # A hung knight on move nine is a tactical failure that happens to occur in an
    # opening; filing it under "Openings" - as routing on phase first did - sends
    # the player to study theory for a problem theory does not fix. Forcing-ness
    # is what makes a position tactical, so it goes first.
    #
    # Nothing here may depend on how large the error was. Routing blunders into
    # the tactics pillar is tempting and quietly ruinous: membership would then be
    # conditioned on the outcome, every other pillar's blunder rate would be
    # exactly zero by construction, and each pillar's average cp-loss would stop
    # being comparable with the others - which is precisely what the profile ranks
    # on. Measured over 18k real moves it drove the other four pillars to a 0.0%
    # blunder rate. Motifs are excluded for the same reason: they are only
    # computed for moves that already went wrong, so they are a tag, not a
    # category. `tactical_tension` is the honest substitute - it asks whether the
    # position was tactical, not whether the player fell over.
    #
    # The endgame is the one phase that keeps its claim over a tactical position.
    # A 300cp error in a rook ending is usually technique, and technique is what
    # the endgame drills actually train - sending it to "tactics" would prescribe
    # more puzzles for a problem that wants a Lucena.
    if phase == "endgame":
        pillar = "endgame"
    elif tactical_tension(board) or f >= 0.5:
        pillar = "tactics"
    elif phase == "opening":
        pillar = "opening"
    elif f <= cfg.quiet_forcingness_max:
        pillar = "quiet"
    else:
        pillar = "positional"

    return MoveTags(
        phase=phase,
        forcingness=round(f, 3),
        error_bucket=bucket,
        pillar=pillar,
        motif=",".join(motifs),
        endgame_type=eg,
        context=attack_defence_context(board),
        decision_w=round(decision_weight(board), 3),
    )
