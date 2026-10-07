"""Regressions for the 2026-09 audit.

Each test names the thing that was wrong, so a future change that reintroduces
it fails with the reason rather than with a number.
"""

from __future__ import annotations

import datetime as dt
import json

import chess

from coach.drills.endgames import ENDGAMES
from coach.puzzles import srs
from coach.store.db import (
    Game,
    Move,
    ProfileSnapshot,
    Puzzle,
    SrsCard,
    TrainingSession,
    session_factory,
)

# --------------------------------------------------------------- the drill set


def _sq_colour(square: int) -> str:
    return "light" if (chess.square_file(square) + chess.square_rank(square)) % 2 else "dark"


def _material(board: chess.Board, colour: chess.Color) -> int:
    vals = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
    return sum(v * len(board.pieces(pt, colour)) for pt, v in vals.items())


def test_a_draw_drill_puts_you_on_the_defending_side():
    """`defensive-opposition` used to be White to move, a pawn up, asked to draw.

    Every legal move passed and even losing the pawn succeeded, so the drill
    could not be failed - and the idea text described the opponent's king.
    """
    wrong = []
    for e in ENDGAMES:
        if e.goal != "draw":
            continue
        board = chess.Board(e.fen)
        mine = _material(board, board.turn)
        theirs = _material(board, not board.turn)
        if mine > theirs:
            wrong.append(f"{e.key}: asked to draw while {mine - theirs} ahead")
    assert not wrong, "draw drills must be drilled from the defending side:\n  " + "\n  ".join(wrong)


def test_a_drill_named_for_opposite_bishops_has_opposite_bishops():
    """It had both bishops on dark squares, so the name, the category and every
    word of the idea described a position the drill did not contain."""
    for e in ENDGAMES:
        if "opposite" not in e.key:
            continue
        board = chess.Board(e.fen)
        white = [_sq_colour(s) for s in board.pieces(chess.BISHOP, chess.WHITE)]
        black = [_sq_colour(s) for s in board.pieces(chess.BISHOP, chess.BLACK)]
        assert len(white) == 1 and len(black) == 1, f"{e.key}: needs one bishop each"
        assert white[0] != black[0], f"{e.key}: both bishops are on {white[0]} squares"


def test_no_drill_is_graded_on_a_single_move():
    """`key-move` accepted 75-89% of all legal moves - including `Rb1` in the
    drill named "rook behind the passed pawn". Centipawns cannot grade
    technique in a position whose result is already decided."""
    offenders = [e.key for e in ENDGAMES if e.mode == "key-move"]
    assert not offenders, (
        "these drills are graded on one move, which passes almost anything: "
        + ", ".join(offenders)
    )


# ------------------------------------------------------------------------ SRS


class _Card:
    """Just enough of an SrsCard for the pure scheduling functions."""

    def __init__(self, **kw):
        self.stability = 0.0
        self.difficulty = 5.0
        self.due_at = srs._now()
        self.reps = 0
        self.lapses = 0
        self.last_grade = None
        self.last_review = None
        self.last_ms = None
        self.avg_ms = None
        self.wins = 0
        self.__dict__.update(kw)


def test_a_failure_does_not_set_the_pace_for_the_next_solve():
    """`avg_ms` is the yardstick `grade_from_outcome` uses. Folding failures in
    raised it, so a card failed after 40s and then solved in 15s scored EASY."""
    card = _Card()
    srs.review(card, srs.AGAIN, elapsed_ms=40_000)
    assert card.avg_ms is None, "a failure is not a solve time"
    assert srs.grade_from_outcome(card, True, 15_000) == srs.GOOD


def test_an_abandoned_tab_is_not_a_solve_time():
    """One real card carried 4,599s - seventy-seven minutes - and it then
    mis-graded every later attempt on that card."""
    card = _Card()
    srs.review(card, srs.GOOD, elapsed_ms=4_599_600)
    assert card.avg_ms is None
    assert srs.grade_from_outcome(card, True, 4_599_600) == srs.GOOD


def test_the_ability_estimate_can_reach_a_beginner():
    """The floor was 600, above where a genuine beginner sits, so the estimate
    pinned there and `selection_rating` anchored the queue to a clamp."""
    assert srs.RATING_MIN <= 250


def test_due_count_answers_the_question_the_queue_was_asked(tmp_path):
    Session = session_factory(tmp_path / "due.db")
    with Session() as s:
        for i, source in enumerate(["own-game"] * 5 + ["endgame"] * 2):
            p = Puzzle(source=source, fen=chess.STARTING_FEN, solution="e2e4",
                       pillar="tactics" if source == "own-game" else "endgame")
            s.add(p)
            s.flush()
            s.add(SrsCard(puzzle_id=p.id, due_at=srs._now() - dt.timedelta(hours=i + 1)))
        s.commit()
        assert srs.due_count(s) == 7
        assert srs.due_count(s, sources=["endgame"]) == 2, (
            "the drill block reported the whole backlog while eleven drills existed"
        )
        assert srs.due_count(s, pillar="tactics") == 5


def test_new_cards_are_rationed(tmp_path):
    """Mining a few hundred games made 1,218 never-seen cards due at once."""
    Session = session_factory(tmp_path / "cap.db")
    with Session() as s:
        for _ in range(srs.NEW_CARDS_PER_DAY + 20):
            p = Puzzle(source="own-game", fen=chess.STARTING_FEN, solution="e2e4",
                       pillar="tactics", rating_est=800)
            s.add(p)
            s.flush()
            s.add(SrsCard(puzzle_id=p.id, due_at=srs._now() - dt.timedelta(hours=1)))
        s.commit()
        served = srs.due_queue(s, limit=100)
        assert len(served) == srs.NEW_CARDS_PER_DAY


def test_the_cap_yields_rather_than_ending_a_block_early(tmp_path):
    """Nothing in circulation and the intake spent is still better served with
    a new card than with an empty block."""
    Session = session_factory(tmp_path / "yield.db")
    with Session() as s:
        for _ in range(3):
            p = Puzzle(source="own-game", fen=chess.STARTING_FEN, solution="e2e4",
                       pillar="tactics", rating_est=800)
            s.add(p)
            s.flush()
            # reps == 1 and reviewed today: this is what spends the intake.
            s.add(SrsCard(puzzle_id=p.id, reps=1, last_review=srs._now(),
                          due_at=srs._now() + dt.timedelta(days=3)))
        for _ in range(2):
            p = Puzzle(source="own-game", fen=chess.STARTING_FEN, solution="e2e4",
                       pillar="tactics", rating_est=800)
            s.add(p)
            s.flush()
            s.add(SrsCard(puzzle_id=p.id, due_at=srs._now() - dt.timedelta(hours=1)))
        s.commit()
        monkey = srs.NEW_CARDS_PER_DAY
        srs.NEW_CARDS_PER_DAY = 0
        try:
            assert srs.due_queue(s, limit=5), "an empty block is worse than exceeding the cap"
        finally:
            srs.NEW_CARDS_PER_DAY = monkey


# ---------------------------------------------------------- the weakness model


def test_sparring_games_are_not_evidence(tmp_path):
    """23% of the recency-weighted profile came from games the coach dealt
    itself, against a bot biased toward the pillar already ranked first."""
    from coach.profile.weakness_model import _fetch

    Session = session_factory(tmp_path / "eve.db")
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with Session() as s:
        for source in ("chesscom", "sparring"):
            g = Game(source=source, source_id=f"{source}-1", account="me", color="white",
                     result="win", pgn="", played_at=now, analyzed=True, ply_count=2)
            s.add(g)
            s.flush()
            s.add(Move(game_id=g.id, ply=0, side="white", is_user_move=True, san="e4",
                       uci="e2e4", fen_before=chess.STARTING_FEN, cp_loss=100,
                       pillar="tactics", decision_w=1.0))
        s.commit()
        rows = _fetch(s, None)
    assert len(rows) == 1, "sparring moves must not reach the weakness profile"


def test_the_worst_pillar_is_not_ranked_below_the_best_one():
    """severity used to blend cost share with rate share, and volume still won:
    Openings (49cp/decision, 3% blunders) outranked Positional (129cp, 17%)."""
    from coach.profile.weakness_model import _build

    class _FakeSession:
        def __init__(self, rows):
            self._rows = rows

        def scalar(self, *a, **k):
            return 1

        def query(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def count(self):
            return 2

        def execute(self, *a, **k):
            return self

        def all(self):
            return []

        def add(self, *a, **k):
            pass

        def commit(self):
            pass

    rows = []
    from coach.profile.weakness_model import _Row

    def row(pillar, cp):
        return _Row(cp_loss=cp, bucket="ok", pillar=pillar, motif=None, endgame_type=None,
                    eco=None, opening=None, color="white", time_trouble=False,
                    context="balanced", punish_miss=False, half="archive", speed="blitz",
                    weight=1.0, decision_w=1.0, critical=False, seconds=None)

    rows += [row("opening", 49) for _ in range(5000)]     # huge volume, played well
    rows += [row("positional", 129) for _ in range(800)]  # small volume, played badly

    import coach.profile.weakness_model as wm

    fake = _FakeSession(rows)
    originals = {name: getattr(wm, name)
                 for name in ("_fetch", "_repertoire", "_save_snapshot", "_freshness")}
    wm._fetch = lambda s, a: rows
    wm._repertoire = lambda s, a: {}
    wm._save_snapshot = lambda s, a, p: None
    wm._freshness = lambda s, a: {}
    try:
        profile = _build(fake, None)
    finally:
        for name, fn in originals.items():
            setattr(wm, name, fn)
    order = [w["pillar"] for w in profile["ranked_weaknesses"]]
    assert order.index("positional") < order.index("opening"), (
        f"ranked {order} - the pillar played worst must not come last"
    )


def test_latest_profile_none_means_the_aggregate(tmp_path):
    """It returned the newest snapshot of *any* account, which is the "all" row
    only until a per-account profile is ever built."""
    from coach.profile.weakness_model import latest_profile

    db = tmp_path / "snap.db"
    Session = session_factory(db)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with Session() as s:
        s.add(ProfileSnapshot(account="all", payload=json.dumps({"who": "aggregate"}),
                              created_at=now - dt.timedelta(minutes=5)))
        s.add(ProfileSnapshot(account="bob", payload=json.dumps({"who": "bob"}),
                              created_at=now))
        s.commit()
    assert latest_profile(None, db)["who"] == "aggregate"
    assert latest_profile("bob", db)["who"] == "bob"


# -------------------------------------------------------------------- session


def test_today_is_a_local_day_not_a_utc_one():
    """On UTC+1 the boundary landed at 01:00 local, so a session begun at 00:30
    stopped being "today" half an hour into itself."""
    from coach.session.plan import _today_window

    start, end = _today_window()
    local_midnight = (
        dt.datetime.now().astimezone()
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(dt.UTC).replace(tzinfo=None)
    )
    assert start == local_midnight
    assert end - start == dt.timedelta(days=1)


def test_a_session_length_has_to_be_a_session_length():
    from coach.session.plan import MAX_SESSION_MINUTES, MIN_SESSION_MINUTES, clamp_minutes

    assert clamp_minutes(0) == MIN_SESSION_MINUTES
    assert clamp_minutes(-60) == MIN_SESSION_MINUTES
    assert clamp_minutes(100000) == MAX_SESSION_MINUTES
    assert clamp_minutes(None) == 60
    assert clamp_minutes(45) == 45


def test_current_session_does_not_hand_you_someone_elses_hour(tmp_path, monkeypatch):
    import coach.session.plan as sp

    db = tmp_path / "sess.db"
    monkeypatch.setattr(sp, "session_factory", lambda *a, **k: session_factory(db))
    Session = session_factory(db)
    with Session() as s:
        s.add(TrainingSession(account="alice", plan=json.dumps({"blocks": [], "minutes": 60}),
                              started_at=sp._now(), block_started_at=sp._now()))
        s.commit()
    assert sp.current_session("alice") is not None
    assert sp.current_session("bob") is None, "two players shared one daily session"


# ------------------------------------------------------------------ the drills


def test_a_drill_counts_its_own_moves():
    """`moves_played` travelled in the request and decides real outcomes: 999
    ended a drill outright and filed the lapse that went with it."""
    from coach.drills.play import moves_played_between

    start = "8/8/8/4k3/8/8/8/3QK3 w - - 0 1"
    assert moves_played_between(start, start) == 0
    board = chess.Board(start)
    board.push(chess.Move.from_uci("d1d3"))
    assert moves_played_between(start, board.fen()) == 1
    board.push(chess.Move.from_uci("e5e6"))
    assert moves_played_between(start, board.fen()) == 1
    board.push(chess.Move.from_uci("d3d5"))
    assert moves_played_between(start, board.fen()) == 2
    assert moves_played_between(start, "not a fen") is None


# ----------------------------------------------------------------- narration


def test_a_hung_piece_is_not_told_to_find_a_plan():
    """The pillar tip was appended regardless, so a move tagged `hangs-piece`
    and routed to `quiet` was advised to "improve your worst piece"."""
    from coach.coaching.llm import NullNarrator

    note = NullNarrator().explain({
        "best_san": "Re8", "best_line_san": ["Re8", "Re3"],
        "motif": "hangs-piece", "pillar": "quiet",
    })
    assert "hangs-piece" in note
    assert "needed a plan" not in note

    generic = NullNarrator().explain({
        "best_san": "Re8", "best_line_san": ["Re8"], "motif": None, "pillar": "quiet",
    })
    assert "needed a plan" in generic


# ------------------------------------------------------------------- ingest


def test_chesscom_sync_starts_from_where_it_left_off():
    """All 24 monthly archives were re-downloaded in full on every sync."""
    from coach.ingest.chesscom import _month_key, _since_key

    urls = [f"https://api.chess.com/pub/player/x/games/{y}/{m:02d}"
            for y in (2024, 2025, 2026) for m in range(1, 13)]
    last = dt.datetime(2026, 5, 16, tzinfo=dt.UTC)
    kept = [u for u in urls if _month_key(u) >= _since_key(last)]
    assert kept == [
        "https://api.chess.com/pub/player/x/games/2026/05",
        "https://api.chess.com/pub/player/x/games/2026/06",
        "https://api.chess.com/pub/player/x/games/2026/07",
        "https://api.chess.com/pub/player/x/games/2026/08",
        "https://api.chess.com/pub/player/x/games/2026/09",
        "https://api.chess.com/pub/player/x/games/2026/10",
        "https://api.chess.com/pub/player/x/games/2026/11",
        "https://api.chess.com/pub/player/x/games/2026/12",
    ]
    assert _since_key(None) == (0, 1), "a first sync must fetch everything"


# --------------------------------------------------------------- live games


def test_a_reload_does_not_lose_the_game_in_progress():
    from coach.server import app as srv

    key = "t-resume"
    srv.save_live_game(key, {"start_fen": chess.STARTING_FEN, "moves": ["e2e4"],
                             "you": "white", "elo": 900})
    got = srv.load_live_game(key)
    assert got["moves"] == ["e2e4"]
    srv.drop_live_game(key)
    assert srv.load_live_game(key) is None


def test_abandoned_games_do_not_accumulate():
    from coach.server import app as srv

    for i in range(srv.MAX_LIVE_GAMES + 10):
        srv.save_live_game(f"t-bulk-{i}", {"start_fen": chess.STARTING_FEN, "moves": []})
    assert len(srv._LIVE_GAMES) <= srv.MAX_LIVE_GAMES
    for i in range(srv.MAX_LIVE_GAMES + 10):
        srv.drop_live_game(f"t-bulk-{i}")
