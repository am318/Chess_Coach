"""The session endpoints the Today tab actually calls.

The unit tests in ``test_session.py`` cover the planner and the progress
bookkeeping. This one covers the review block's single question - "what did I
just play?" - because the answer coming back empty after two real games is the
failure that cost a session its whole coaching half.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from coach.store.db import Game, TrainingSession, session_factory


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("coach.server.app._REFRESH_ON_START", False, raising=False)
    from coach.server.app import app

    return TestClient(app)


def _session_row(progress: dict, started: dt.datetime) -> int:
    Session = session_factory()
    with Session() as s:
        ts = TrainingSession(
            plan=json.dumps({"minutes": 60, "blocks": [{"kind": "game", "minutes": 25,
                                                        "title": "g", "detail": "",
                                                        "params": {}}]}),
            progress=json.dumps(progress),
            started_at=started,
            block_index=0,
        )
        s.add(ts)
        s.commit()
        return ts.id


def _sparring_game(played_at: dt.datetime, marker: str) -> int:
    Session = session_factory()
    with Session() as s:
        g = Game(source="sparring", source_id=marker, account="me", color="white",
                 result="win", played_at=played_at, pgn="1. e4 e5", ply_count=2)
        s.add(g)
        s.commit()
        return g.id


def test_review_moments_uses_the_ids_the_block_reported(client):
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    gid = _sparring_game(now, "reported")
    sid = _session_row({"1": {"game_ids": [gid], "games": 1}}, now - dt.timedelta(minutes=30))

    body = client.get(f"/api/session/{sid}/review-moments").json()
    assert [g["game_id"] for g in body["games"]] == [gid]


def test_a_session_that_never_reported_its_games_still_finds_them(client):
    """Belt and braces. The ids reach the session from the browser, so a tab
    that is closed between the last move and the review block used to take the
    entire review with it - and an empty review block is the one outcome in the
    hour worth a second query to avoid."""
    started = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(minutes=20)
    stale = _sparring_game(started - dt.timedelta(days=2), "before-the-session")
    played = _sparring_game(started + dt.timedelta(minutes=5), "during-the-session")
    sid = _session_row({"0": {"skipped": True}, "1": {"skipped": True}}, started)

    ids = [g["game_id"] for g in client.get(f"/api/session/{sid}/review-moments").json()["games"]]
    assert played in ids, "the game was filed; the session just never heard about it"
    assert stale not in ids, "and nothing from before the session started"


def test_the_review_block_is_capped_at_the_last_few_games(client):
    from coach.server.app import MAX_REVIEW_GAMES

    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    ids = [_sparring_game(now, f"capped-{i}") for i in range(MAX_REVIEW_GAMES + 2)]
    sid = _session_row({"1": {"game_ids": ids}}, now - dt.timedelta(minutes=30))

    body = client.get(f"/api/session/{sid}/review-moments").json()
    assert [g["game_id"] for g in body["games"]] == ids[-MAX_REVIEW_GAMES:]
