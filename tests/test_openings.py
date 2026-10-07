"""The openings section: the catalogue itself, learning and drilling, and the
read-out of how each opening has gone in the player's own games."""

from __future__ import annotations

import tempfile
from pathlib import Path

import chess
import pytest
from fastapi.testclient import TestClient

from coach.openings import trainer
from coach.openings.catalog import BY_KEY, CATALOG
from coach.puzzles import srs
from coach.store.db import Game, Move, Puzzle, SrsCard, session_factory

# ------------------------------------------------------------------ catalogue


@pytest.mark.parametrize("opening", CATALOG, ids=lambda o: o.key)
def test_every_line_is_legal_and_notes_parse(opening):
    for line in opening.lines:
        plies = trainer.line_plies(opening.key, line.key)
        assert len(plies) >= 6, f"{line.key} is too short to be worth drilling"
        board = chess.Board()
        for p in plies:
            board.push(chess.Move.from_uci(p["uci"]))
            assert board.fen() == p["fen"]
    trainer._defining_key(opening.key)       # parses


@pytest.mark.parametrize("opening", CATALOG, ids=lambda o: o.key)
def test_lines_of_one_opening_agree_on_every_position(opening):
    """A drill marks every move but the stored one wrong, so two lines wanting
    different moves in one position would fail the player whichever they knew."""
    assert trainer.inconsistencies(opening) == []


def test_keys_fit_the_columns_they_are_stored_in():
    for o in CATALOG:
        assert len(o.name) <= 64                          # Puzzle.motif
        for line in o.lines:
            assert len(trainer.external_id(o, line)) <= 64
            assert len(f"{o.name}: {line.name}") <= 96    # Puzzle.name
    assert len(BY_KEY) == len(CATALOG)


def test_learners_side_is_marked_on_each_ply():
    plies = trainer.line_plies("caro-kann", "classical")
    assert plies[0]["san"] == "e4" and not plies[0]["yours"]
    assert plies[1]["san"] == "c6" and plies[1]["yours"]
    assert plies[0]["label"] == "1." and plies[1]["label"] == "1…"


def test_overlapping_openings_are_flagged_but_first_moves_are_not():
    italian = BY_KEY["italian"]
    clash = trainer.conflicts(italian, [BY_KEY["scotch"], BY_KEY["london"]])
    assert [c["key"] for c in clash] == ["scotch"]
    assert clash[0]["label"] == "3." and clash[0]["mine"] == "Bc4"
    assert clash[0]["theirs"] == "d4"
    # A Black opening is never in a White opening's way.
    assert trainer.conflicts(italian, [BY_KEY["caro-kann"]]) == []


# ----------------------------------------------------------- learn / drill


@pytest.fixture
def db():
    path = Path(tempfile.mkdtemp(prefix="coach-openings-")) / "t.db"
    return path


def test_learning_is_idempotent_and_forgetting_removes_the_cards(db):
    n = len(BY_KEY["french"].lines)
    assert trainer.learn("french", db) == n
    assert trainer.learn("french", db) == 0
    Session = session_factory(db)
    with Session() as s:
        rows = s.query(Puzzle).filter_by(source="opening").all()
        assert len(rows) == n
        # Black lines open with White's move, which the solver plays first.
        assert all(p.opponent_first for p in rows)
        assert all(p.srs is not None for p in rows)
    assert trainer.forget("french", db) == n
    with Session() as s:
        assert s.query(Puzzle).filter_by(source="opening").count() == 0
        assert s.query(SrsCard).count() == 0


def test_new_lines_come_in_catalogue_order_then_practice(db):
    trainer.learn("scotch", db)
    p, practice, _ = trainer.next_drill("scotch", db_path=db)
    assert not practice and p.external_id == "opening:scotch:classical"
    Session = session_factory(db)
    with Session() as s:
        for row in s.query(Puzzle).filter_by(source="opening"):
            srs.review(row.srs, srs.GOOD)
        s.commit()
    p, practice, next_due = trainer.next_drill("scotch", db_path=db)
    assert practice and p is not None and next_due is not None


def test_a_named_line_can_be_drilled_directly(db):
    trainer.learn("london", db)
    p, practice, _ = trainer.next_drill("london", "kid", db_path=db)
    assert p.external_id == "opening:london:kid" and not practice
    assert trainer.next_drill("london", "no-such-line", db_path=db)[0] is None


# --------------------------------------------------------------- your games


def _add_game(session, sans: str, colour: str, result: str, source="lichess", sid="1"):
    g = Game(source=source, source_id=sid, account="me", color=colour, result=result,
             pgn="", ply_count=0)
    session.add(g)
    session.flush()
    board = chess.Board()
    for ply, san in enumerate(sans.split()):
        mv = board.parse_san(san)
        side = "white" if board.turn == chess.WHITE else "black"
        session.add(Move(game_id=g.id, ply=ply, side=side, is_user_move=(side == colour),
                         san=san, uci=mv.uci(), fen_before=board.fen()))
        board.push(mv)
    return g


def test_your_games_finds_where_you_leave_the_book_and_ignores_sparring(db):
    Session = session_factory(db)
    with Session() as s:
        # Leaves the Pianissimo at move 4 with d3 instead of c3, twice.
        _add_game(s, "e4 e5 Nf3 Nc6 Bc4 Bc5 d3 Nf6", "white", "win", sid="a")
        _add_game(s, "e4 e5 Nf3 Nc6 Bc4 Bc5 d3 d6", "white", "loss", sid="b")
        # Follows the Two Knights line to the end.
        _add_game(s, "e4 e5 Nf3 Nc6 Bc4 Nf6 d3 Be7 O-O O-O Re1 d6 c3", "white", "draw", sid="c")
        # A sparring game in the opening: the trainer's choice, not evidence.
        _add_game(s, "e4 e5 Nf3 Nc6 Bc4 Bc5 b4", "white", "win", source="sparring", sid="d")
        # Never reaches the Italian.
        _add_game(s, "d4 d5 c4", "white", "win", sid="e")
        s.commit()
        out = trainer.your_games(s, [BY_KEY["italian"]])["italian"]
    assert out["games"] == 3
    assert (out["wins"], out["draws"], out["losses"]) == (1, 1, 1)
    assert out["stayed_in_book"] == 1
    assert out["departures"][0] == {"label": "4.", "played": "d3", "book": "c3", "games": 2}


# ---------------------------------------------------------------------- API


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("coach.server.app._REFRESH_ON_START", False, raising=False)
    from coach.server.app import app

    yield TestClient(app)
    trainer.forget("ruy-lopez")


def test_api_round_trip_and_strict_grading(client):
    r = client.get("/api/openings")
    assert r.status_code == 200
    assert {o["key"] for o in r.json()["openings"]} == set(BY_KEY)
    assert client.get("/api/openings/nope").status_code == 404

    assert client.post("/api/openings/ruy-lopez/learn").json()["added"] == 4
    d = client.get("/api/openings/ruy-lopez").json()
    assert d["learning"] and d["lines"][0]["card"]["state"] == "new"

    nxt = client.get("/api/openings/ruy-lopez/next").json()
    pid = nxt["puzzle"]["id"]
    assert nxt["puzzle"]["kind"] == "line" and not nxt["practice"]

    Session = session_factory()
    with Session() as s:
        rating_before = srs.puzzle_rating(s)
    # 3.Bc4 is a fine move - and not the Ruy Lopez. No engine is consulted, so
    # this also proves the attempt never asks Stockfish for an alternative.
    bad = client.post(f"/api/puzzles/{pid}/attempt",
                      json={"moves": ["e2e4", "g1f3", "f1c4"], "auto_first": True})
    body = bad.json()
    assert body["status"] == "wrong" and body["book"] is True
    assert body["best_san"] == "Bb5"
    assert body["graded"]["label"] == "again"
    with Session() as s:
        assert srs.puzzle_rating(s) == rating_before
