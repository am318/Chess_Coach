"""Post-game review: eval graph, key moments, per-pillar damage, next focus.

Works on any analysed game (imported or a sparring game once it's been run
through `coach analyze`). Pure data assembly - no engine calls here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import chess
import chess.pgn
from sqlalchemy.orm import Session

from coach.coaching.llm import Narrator, NullNarrator
from coach.store.db import Game, session_factory

_CLAMP = 800  # cap eval graph at +-8 pawns so one blunder doesn't flatten it


@dataclass
class Moment:
    ply: int
    move_number: int
    side: str
    fen: str
    played_san: str
    played_uci: str
    best_san: str | None
    best_uci: str | None
    cp_loss: int
    eval_before: int
    eval_after: int
    pillar: str | None
    motif: str | None
    phase: str | None
    best_line_san: list[str]
    # What the move that was actually played ran into. `best_line_san` is the
    # line after the move they *should* have played, which never answers the
    # question they are asking.
    refutation_san: list[str] = field(default_factory=list)
    note: str | None = None


def _move_number(fen_before: str, ply: int) -> int:
    """The move number the player actually saw on the board.

    ``ply // 2 + 1`` is only right for a game that started from the initial
    position. Sparring games start from a book position - the opening picker
    hands out a FEN several moves in - so every key moment in those reviews was
    numbered from one and disagreed with the board it was describing. The stored
    FEN carries the real fullmove counter, so read it from there.
    """
    try:
        return int(fen_before.split()[5])
    except (IndexError, ValueError):
        return ply // 2 + 1


def _san_line(fen: str, uci_line: str, limit: int = 8) -> list[str]:
    board = chess.Board(fen)
    out = []
    for tok in uci_line.split()[:limit]:
        try:
            mv = chess.Move.from_uci(tok)
        except ValueError:
            break
        if mv not in board.legal_moves:
            break
        out.append(board.san(mv))
        board.push(mv)
    return out


def _refutation_san(fen_before: str, played_uci: str, uci_line: str | None,
                    limit: int = 6) -> list[str]:
    """The punishment line, in SAN, read from after the move that was played."""
    if not uci_line:
        return []
    try:
        board = chess.Board(fen_before)
        played = chess.Move.from_uci(played_uci)
    except ValueError:
        return []
    if played not in board.legal_moves:
        return []
    board.push(played)
    return _san_line(board.fen(), uci_line, limit)


def build_review(game_id: int, narrator: Narrator | None = None, db_path=None) -> dict:
    narrator = narrator or NullNarrator()
    Session = session_factory(db_path)
    with Session() as session:
        game = session.get(Game, game_id)
        if game is None:
            return {"error": f"game {game_id} not found"}
        if not game.analyzed:
            return {"error": "game not analysed yet", "game_id": game_id, "needs_analysis": True}
        return _assemble(session, game, narrator)


def _assemble(session: Session, game: Game, narrator: Narrator) -> dict:
    moves = sorted(game.moves, key=lambda m: m.ply)
    user_is_white = game.color == "white"

    eval_graph = []
    for m in moves:
        cp = m.eval_after_cp if m.eval_after_cp is not None else m.eval_before_cp
        if cp is None:
            cp = 0
        eval_graph.append(max(-_CLAMP, min(_CLAMP, cp)))

    user_moves = [m for m in moves if m.is_user_move and m.cp_loss is not None]
    n = len(user_moves) or 1
    acpl = round(sum(m.cp_loss for m in user_moves) / n, 1)
    buckets = {"inaccuracy": 0, "mistake": 0, "blunder": 0}
    pillar_cost: dict[str, int] = {}
    for m in user_moves:
        if m.error_bucket in buckets:
            buckets[m.error_bucket] += 1
        pillar_cost[m.pillar or "positional"] = pillar_cost.get(m.pillar or "positional", 0) + int(m.cp_loss)

    key = sorted(user_moves, key=lambda m: m.cp_loss, reverse=True)[:5]
    key.sort(key=lambda m: m.ply)
    moments: list[dict] = []
    for m in key:
        if m.cp_loss < 40:
            continue
        best_uci = m.best_move_uci
        best_san = None
        if best_uci:
            b = chess.Board(m.fen_before)
            try:
                best_san = b.san(chess.Move.from_uci(best_uci))
            except (ValueError, AssertionError):
                best_san = None
        moment = Moment(
            ply=m.ply,
            move_number=_move_number(m.fen_before, m.ply),
            side=m.side,
            fen=m.fen_before,
            played_san=m.san,
            played_uci=m.uci,
            best_san=best_san,
            best_uci=best_uci,
            cp_loss=int(m.cp_loss),
            eval_before=m.eval_before_cp or 0,
            eval_after=m.eval_after_cp or 0,
            pillar=m.pillar,
            motif=m.motif,
            phase=m.phase,
            best_line_san=_san_line(m.fen_before, m.best_line or ""),
            refutation_san=_refutation_san(m.fen_before, m.uci, m.refutation_line),
        )
        d = asdict(moment)
        d["note"] = narrator.explain(d)
        moments.append(d)

    focus = max(pillar_cost, key=pillar_cost.get) if pillar_cost else None

    return {
        "game_id": game.id,
        # Where the game began. Sparring games start from a book position, and
        # the review board opened on the initial array regardless.
        "start_fen": moves[0].fen_before if moves else chess.STARTING_FEN,
        "opponent": game.black if user_is_white else game.white,
        "opponent_elo": game.opponent_elo,
        "colour": game.color,
        "result": game.result,
        "opening": game.opening,
        "eco": game.eco,
        "source": game.source,
        "played_at": game.played_at.isoformat() if game.played_at else None,
        "avg_cp_loss": acpl,
        "counts": buckets,
        "pillar_cost": pillar_cost,
        "eval_graph": eval_graph,
        "key_moments": moments,
        "focus_pillar": focus,
        "summary": narrator.summarise({
            "acpl": acpl, "counts": buckets, "focus": focus,
            "result": game.result, "opening": game.opening,
        }),
    }


def game_list(account: str | None = None, limit: int = 50, db_path=None) -> list[dict]:
    Session = session_factory(db_path)
    with Session() as session:
        q = session.query(Game).order_by(Game.played_at.desc())
        if account:
            q = q.filter(Game.account == account)
        return [
            {
                "id": g.id,
                "source": g.source,
                "colour": g.color,
                "result": g.result,
                "opponent": g.black if g.color == "white" else g.white,
                "opening": g.opening,
                "played_at": g.played_at.isoformat() if g.played_at else None,
                "analyzed": g.analyzed,
                "plies": g.ply_count,
            }
            for g in q.limit(limit)
        ]
