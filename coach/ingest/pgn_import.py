"""Import games from a pasted/uploaded PGN blob (OTB games, other sites).

The importer needs to know which name in the PGN is you; pass ``accounts`` (all
your known handles / real name spellings) and every game where one of them
appears as White or Black is stored.
"""

from __future__ import annotations

import hashlib
import io

import chess.pgn
from sqlalchemy.orm import Session

from coach.ingest.common import store_game


def import_pgn_text(session: Session, pgn_text: str, accounts: list[str]) -> int:
    stream = io.StringIO(pgn_text)
    lowered = {a.lower() for a in accounts}
    added = 0
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        h = game.headers
        names = {h.get("White", "").lower(), h.get("Black", "").lower()}
        who = names & lowered
        if not who:
            continue
        account = next(a for a in accounts if a.lower() in who)
        exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
        one_pgn = game.accept(exporter)
        source_id = hashlib.sha1(one_pgn.encode()).hexdigest()[:24]
        if store_game(
            session,
            source="pgn",
            source_id=source_id,
            account=account,
            pgn_text=one_pgn,
        ) is not None:
            added += 1
    session.commit()
    return added
