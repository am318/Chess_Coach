"""Pull a user's games from the Lichess API into the local DB.

https://lichess.org/api#tag/Games/operation/apiGamesUser
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from coach.config import CoachConfig
from coach.ingest.common import store_game
from coach.store.db import SyncState

_API = "https://lichess.org/api/games/user/{user}"


def _headers(token: str) -> dict[str, str]:
    h = {"Accept": "application/x-ndjson", "User-Agent": "chess-coach/0.1"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _sync_row(session: Session, user: str) -> SyncState:
    row = session.query(SyncState).filter_by(source="lichess", username=user).one_or_none()
    if row is None:
        row = SyncState(source="lichess", username=user)
        session.add(row)
        session.flush()
    return row


def sync_user(session: Session, cfg: CoachConfig, user: str, *, verbose: bool = True) -> int:
    state = _sync_row(session, user)
    params = {
        "max": cfg.lichess_max_games,
        "perfType": ",".join(cfg.lichess_speeds),
        "clocks": "true",
        "evals": "true",
        "opening": "true",
        "pgnInJson": "true",
        "sort": "dateDesc",
    }
    if state.last_played_at:
        # +1s so we don't re-fetch the boundary game every run.
        params["since"] = int(state.last_played_at.timestamp() * 1000) + 1000

    added = 0
    newest_ts: datetime | None = state.last_played_at
    with (
        httpx.Client(timeout=60.0) as client,
        client.stream("GET", _API.format(user=user), params=params,
                      headers=_headers(cfg.lichess_token)) as resp,
    ):
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.strip():
                continue
            obj = json.loads(line)
            created_ms = obj.get("createdAt")
            played_at = (
                datetime.fromtimestamp(created_ms / 1000, tz=UTC).replace(tzinfo=None)
                if created_ms else None
            )
            opening = (obj.get("opening") or {}).get("name")
            eco = (obj.get("opening") or {}).get("eco")
            g = store_game(
                session,
                source="lichess",
                source_id=obj["id"],
                account=user,
                pgn_text=obj["pgn"],
                speed=obj.get("speed"),
                played_at=played_at,
                eco=eco,
                opening=opening,
            )
            if g is not None:
                added += 1
                if played_at and (newest_ts is None or played_at > newest_ts):
                    newest_ts = played_at
                if verbose and added % 50 == 0:
                    print(f"  lichess/{user}: {added} games...", flush=True)

    if newest_ts:
        state.last_played_at = newest_ts
    session.commit()
    if verbose:
        print(f"  lichess/{user}: +{added} new games")
    return added
