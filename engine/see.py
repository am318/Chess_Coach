"""Static Exchange Evaluation.

Used for capture move ordering and for pruning obviously-losing captures in
quiescence. Pure python-chess attacker bitboards; no board mutation.
"""

from __future__ import annotations

import chess

_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}
_ORDER = (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN, chess.KING)


def _diag_attacks(square: int, occupied: int) -> int:
    return chess.BB_DIAG_ATTACKS[square][chess.BB_DIAG_MASKS[square] & occupied]


def _line_attacks(square: int, occupied: int) -> int:
    return (
        chess.BB_RANK_ATTACKS[square][chess.BB_RANK_MASKS[square] & occupied]
        | chess.BB_FILE_ATTACKS[square][chess.BB_FILE_MASKS[square] & occupied]
    )


def _attackers_to(board: chess.Board, square: int, occupied: int) -> int:
    """Bitboard of all pieces (both colours) attacking ``square`` for a given
    ``occupied`` mask (which may differ from the real board as the swap proceeds)."""
    bishops_queens = board.bishops | board.queens
    rooks_queens = board.rooks | board.queens
    attackers = (
        (chess.BB_KNIGHT_ATTACKS[square] & board.knights)
        | (chess.BB_KING_ATTACKS[square] & board.kings)
        | (chess.BB_PAWN_ATTACKS[chess.WHITE][square] & board.pawns & board.occupied_co[chess.BLACK])
        | (chess.BB_PAWN_ATTACKS[chess.BLACK][square] & board.pawns & board.occupied_co[chess.WHITE])
        | (_diag_attacks(square, occupied) & bishops_queens)
        | (_line_attacks(square, occupied) & rooks_queens)
    )
    return attackers & occupied


def _least_valuable(board: chess.Board, subset: int, color: bool) -> tuple[int, int]:
    for pt in _ORDER:
        s = subset & board.pieces_mask(pt, color)
        if s:
            return chess.lsb(s), pt
    return -1, 0


def see(board: chess.Board, move: chess.Move) -> int:
    """Signed material outcome (cp) of the capture sequence starting with ``move``.

    Positive => the side to move wins material by initiating the exchange.
    Non-captures return 0.
    """
    to_sq, from_sq = move.to_square, move.from_square

    if board.is_en_passant(move):
        captured_value = _VALUES[chess.PAWN]
    else:
        victim = board.piece_type_at(to_sq)
        if victim is None:
            return 0
        captured_value = _VALUES[victim]

    attacker_pt = board.piece_type_at(from_sq)
    if attacker_pt is None:
        return 0

    occupied = board.occupied & ~chess.BB_SQUARES[from_sq]
    if board.is_en_passant(move):
        occupied &= ~chess.BB_SQUARES[to_sq + (-8 if board.turn == chess.WHITE else 8)]

    gain = [captured_value]
    on_square_value = _VALUES[attacker_pt]
    side = not board.turn
    attackers = _attackers_to(board, to_sq, occupied)

    while True:
        side_attackers = attackers & board.occupied_co[side]
        if not side_attackers:
            break
        sq, pt = _least_valuable(board, side_attackers, side)
        if sq < 0:
            break
        gain.append(on_square_value - gain[-1])
        on_square_value = _VALUES[pt]
        occupied &= ~chess.BB_SQUARES[sq]
        attackers = _attackers_to(board, to_sq, occupied) & occupied
        side = not side
        if pt == chess.KING and (attackers & board.occupied_co[side]):
            break

    for i in range(len(gain) - 2, -1, -1):
        gain[i] = -max(-gain[i], gain[i + 1])
    return gain[0]


def see_ge(board: chess.Board, move: chess.Move, threshold: int = 0) -> bool:
    return see(board, move) >= threshold
