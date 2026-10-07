"""Shared PGN -> DB logic for all ingestion sources."""

from __future__ import annotations

import io
from datetime import datetime

import chess
import chess.pgn
from sqlalchemy import select
from sqlalchemy.orm import Session

from coach.store.db import Game, Move


def _result_for(color: str, pgn_result: str) -> str:
    if pgn_result == "1/2-1/2":
        return "draw"
    if pgn_result == "1-0":
        return "win" if color == "white" else "loss"
    if pgn_result == "0-1":
        return "win" if color == "black" else "loss"
    return "draw"


def game_exists(session: Session, source: str, source_id: str) -> bool:
    return session.scalar(
        select(Game.id).where(Game.source == source, Game.source_id == source_id)
    ) is not None


def store_game(
    session: Session,
    *,
    source: str,
    source_id: str,
    account: str,
    pgn_text: str,
    speed: str | None = None,
    played_at: datetime | None = None,
    eco: str | None = None,
    opening: str | None = None,
) -> Game | None:
    """Parse one PGN and persist Game + Move rows. Returns None if unparseable or
    the account did not play in it."""
    if game_exists(session, source, source_id):
        return None

    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    headers = game.headers

    white = headers.get("White", "?")
    black = headers.get("Black", "?")
    low = account.lower()
    if low == white.lower():
        color = "white"
    elif low == black.lower():
        color = "black"
    else:
        return None  # not our game

    def _elo(key: str) -> int | None:
        try:
            return int(headers.get(key, ""))
        except ValueError:
            return None

    white_elo, black_elo = _elo("WhiteElo"), _elo("BlackElo")
    opponent_elo = black_elo if color == "white" else white_elo

    row = Game(
        source=source,
        source_id=str(source_id),
        account=account,
        color=color,
        result=_result_for(color, headers.get("Result", "*")),
        termination=headers.get("Termination"),
        eco=eco or headers.get("ECO"),
        opening=opening or headers.get("Opening"),
        speed=speed,
        time_control=headers.get("TimeControl"),
        white=white,
        black=black,
        white_elo=white_elo,
        black_elo=black_elo,
        opponent_elo=opponent_elo,
        played_at=played_at,
        pgn=pgn_text,
    )

    board = game.board()
    user_is_white = color == "white"
    ply = 0
    for node in game.mainline():
        move = node.move
        side = "white" if board.turn == chess.WHITE else "black"
        clock = node.clock()
        eval_cp = None
        if node.eval() is not None:
            pov = node.eval().pov(chess.WHITE)
            eval_cp = pov.score(mate_score=100_000)

        row.moves.append(
            Move(
                ply=ply,
                side=side,
                is_user_move=(board.turn == chess.WHITE) == user_is_white,
                san=board.san(move),
                uci=move.uci(),
                fen_before=board.fen(),
                clock_ms=int(clock * 1000) if clock is not None else None,
                eval_after_cp=eval_cp,
            )
        )
        board.push(move)
        ply += 1

    row.ply_count = ply
    # Backfill eval_before from the previous node's eval_after.
    prev_eval = 0
    for m in row.moves:
        m.eval_before_cp = prev_eval
        if m.eval_after_cp is not None:
            prev_eval = m.eval_after_cp

    session.add(row)
    return row
