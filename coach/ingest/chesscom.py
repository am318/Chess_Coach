"""Pull a user's games from the Chess.com public API into the local DB.

https://www.chess.com/news/view/published-data-api
No auth; a descriptive User-Agent is required or the API returns 403.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from coach.config import CoachConfig
from coach.ingest.common import store_game
from coach.store.db import SyncState

_ARCHIVES = "https://api.chess.com/pub/player/{user}/games/archives"
_UA = {"User-Agent": "chess-coach/0.1 (personal training tool; contact via local use)"}


def _month_key(archive_url: str) -> tuple[int, int]:
    """``.../games/2026/05`` -> ``(2026, 5)``. Unparseable sorts last so it is
    always fetched rather than silently skipped."""
    parts = archive_url.rstrip("/").split("/")
    try:
        return int(parts[-2]), int(parts[-1])
    except (IndexError, ValueError):
        return (9999, 12)


def _since_key(last_played_at: datetime | None) -> tuple[int, int]:
    """The earliest archive month still worth fetching.

    The month of the last game we stored, not the one after it: that month is
    almost certainly still being added to.
    """
    if last_played_at is None:
        return (0, 1)
    return (last_played_at.year, last_played_at.month)


def _sync_row(session: Session, user: str) -> SyncState:
    row = session.query(SyncState).filter_by(source="chesscom", username=user).one_or_none()
    if row is None:
        row = SyncState(source="chesscom", username=user)
        session.add(row)
        session.flush()
    return row


def sync_user(session: Session, cfg: CoachConfig, user: str, *, verbose: bool = True) -> int:
    state = _sync_row(session, user)
    speeds = set(cfg.chesscom_speeds)
    added = 0
    newest_ts = state.last_played_at

    # The API canonicalises usernames to lowercase and 301s otherwise, so ask for
    # the canonical form directly and still follow redirects if it moves again.
    with httpx.Client(timeout=60.0, headers=_UA, follow_redirects=True) as client:
        arch = client.get(_ARCHIVES.format(user=user.lower()))
        arch.raise_for_status()
        months = arch.json().get("archives", [])
        # Oldest-first so a mid-run failure still makes forward progress, but
        # start from the month we already reached. Every archive URL ends
        # ".../games/YYYY/MM", and each one is a full JSON document with every
        # PGN in it - so walking the lot on every sync re-downloaded the whole
        # history to find nothing. Twenty-four months of it, on every app open.
        # Lichess has had this right all along via its `since` parameter.
        months = [u for u in months if _month_key(u) >= _since_key(state.last_played_at)]
        for month_url in months:
            m = client.get(month_url)
            if m.status_code == 404:
                continue
            m.raise_for_status()
            for obj in m.json().get("games", []):
                if obj.get("time_class") not in speeds:
                    continue
                if obj.get("rules") not in (None, "chess"):
                    continue
                end = obj.get("end_time")
                played_at = (
                    datetime.fromtimestamp(end, tz=UTC).replace(tzinfo=None)
                    if end else None
                )
                if state.last_played_at and played_at and played_at <= state.last_played_at:
                    continue
                source_id = obj.get("uuid") or obj["url"].rsplit("/", 1)[-1]
                g = store_game(
                    session,
                    source="chesscom",
                    source_id=source_id,
                    account=user,
                    pgn_text=obj["pgn"],
                    speed=obj.get("time_class"),
                    played_at=played_at,
                    eco=(obj.get("eco") or "").rsplit("/", 1)[-1] or None,
                )
                if g is not None:
                    added += 1
                    if played_at and (newest_ts is None or played_at > newest_ts):
                        newest_ts = played_at
                    if added >= cfg.chesscom_max_games:
                        break
            if verbose and added:
                print(f"  chesscom/{user}: {added} games...", flush=True)
            if added >= cfg.chesscom_max_games:
                break

    if newest_ts:
        state.last_played_at = newest_ts
    session.commit()
    if verbose:
        print(f"  chesscom/{user}: +{added} new games")
    return added
