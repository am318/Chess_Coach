"""FastAPI app for the local coaching web app.

Serves the board UI and drives sparring games over a websocket. Bind on
0.0.0.0 so a phone on the same network can reach it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import chess
import chess.pgn
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from coach.coaching.review import build_review, game_list
from coach.config import CoachConfig
from coach.ingest.common import store_game
from coach.profile.progress import dashboard as build_dashboard
from coach.profile.weakness_model import latest_profile, top_weak_pillar
from coach.puzzles import srs
from coach.sparring.backend import register_engine, unregister_engine
from coach.sparring.opening_picker import pick_start
from coach.sparring.opponent import SparringOpponent
from coach.store.db import Game, Puzzle, session_factory


def _pillar_weights(account: str | None) -> dict[str, float]:
    prof = latest_profile(account) or latest_profile(None)
    if not prof or not prof.get("ranked_weaknesses"):
        return {}
    return {w["pillar"]: 1.0 + 3.0 * w["severity"] for w in prof["ranked_weaknesses"]}

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Background refresh (sync + analyse + mine) state, surfaced at /api/sync/status.
_REFRESH: dict = {"state": "idle", "detail": "", "started": None, "finished": None,
                  "new_games": 0, "analysed": 0, "puzzles": 0, "error": None}
_REFRESH_LOCK = threading.Lock()


def default_account(cfg: CoachConfig, account: str | None) -> str | None:
    """Empty/absent account -> None (aggregate). Under unified identity the UI
    sends '' and we always aggregate."""
    if account:
        return account
    return None


def _run_refresh(cfg: CoachConfig) -> None:
    if not _REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        _REFRESH.update(state="syncing", detail="pulling games", error=None,
                        started=dt.datetime.now(dt.UTC).isoformat(), finished=None,
                        new_games=0, analysed=0, puzzles=0)
        new_games = 0
        if cfg.startup.auto_sync and cfg.all_users:
            from coach.ingest import chesscom, lichess
            from coach.store.db import session_factory

            Session = session_factory()
            with Session() as session:
                for u in cfg.lichess_users:
                    try:
                        new_games += lichess.sync_user(session, cfg, u, verbose=False)
                    except Exception as exc:  # noqa: BLE001
                        _REFRESH["error"] = f"lichess/{u}: {exc}"
                for u in cfg.chesscom_users:
                    try:
                        new_games += chesscom.sync_user(session, cfg, u, verbose=False)
                    except Exception as exc:  # noqa: BLE001
                        _REFRESH["error"] = f"chesscom/{u}: {exc}"
        _REFRESH.update(new_games=new_games)

        analysed = 0
        if cfg.startup.auto_analyze:
            _REFRESH.update(state="analysing", detail="running Stockfish")
            from coach.analysis.annotate import analyse_pending

            analysed = analyse_pending(cfg, limit=cfg.startup.analyze_limit)
        _REFRESH.update(analysed=analysed)

        pz = 0
        if cfg.startup.mine_puzzles:
            _REFRESH.update(state="mining", detail="building puzzles + profile")
            from coach.profile.weakness_model import build_profile
            from coach.puzzles.miner import mine_all

            pz = mine_all(account=None)
            # Endgame set is static but cheap to re-seed; repertoire lines change
            # as new games come in, so they're re-mined here too.
            from coach.drills.seed import seed_all

            seed_all(account=None)
            build_profile(account=None)
            if not cfg.unified_identity:
                for u in cfg.all_users:
                    build_profile(account=u)
        _REFRESH.update(puzzles=pz)
        _REFRESH.update(state="idle", detail="up to date",
                        finished=dt.datetime.now(dt.UTC).isoformat())
    except Exception as exc:  # noqa: BLE001
        _REFRESH.update(state="error", error=str(exc),
                        finished=dt.datetime.now(dt.UTC).isoformat())
    finally:
        _REFRESH_LOCK.release()


def trigger_refresh() -> bool:
    """Kick a background refresh if one isn't already running."""
    if _REFRESH_LOCK.locked():
        return False
    threading.Thread(target=_run_refresh, args=(_cfg(),), daemon=True).start()
    return True


_REFRESH_ON_START = True  # set False by serve(no_refresh=True)


def _deep_loop() -> None:
    """Low-key background thread: keep re-analysing games at the deep budget with
    a single worker, so it only sips spare compute."""
    import dataclasses
    import time

    from coach.analysis.annotate import analyse_pending

    while True:
        cfg = _cfg()
        if not cfg.startup.deep_pass:
            time.sleep(120)
            continue
        # One worker only -> sips spare compute. analyse_pending deepens the budget.
        loop_cfg = dataclasses.replace(cfg, engine=dataclasses.replace(cfg.engine, workers=1))
        try:
            did = analyse_pending(loop_cfg, limit=1, deep=True)
        except Exception:  # noqa: BLE001
            did = 0
        time.sleep(8 if did else 600)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    cfg = _cfg()
    if _REFRESH_ON_START and (cfg.startup.auto_sync or cfg.startup.auto_analyze):
        trigger_refresh()
    if cfg.startup.deep_pass:
        threading.Thread(target=_deep_loop, daemon=True).start()
    yield


app = FastAPI(title="Chess Coach", lifespan=_lifespan)


@app.middleware("http")
async def _no_cache(request, call_next):
    # Local dev tool: always serve the latest HTML/JS/CSS so edits show up.
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/sync/status")
def api_sync_status() -> JSONResponse:
    return JSONResponse(dict(_REFRESH))


@app.post("/api/sync/run")
def api_sync_run() -> JSONResponse:
    started = trigger_refresh()
    return JSONResponse({"started": started, **_REFRESH})


def _cfg() -> CoachConfig:
    try:
        return CoachConfig.load()
    except SystemExit:
        return CoachConfig()


@app.get("/")
def index() -> FileResponse:
    # Opening the app also triggers an incremental refresh (if one isn't running
    # and the last finished more than 10 min ago).
    cfg = _cfg()
    if _REFRESH_ON_START and (cfg.startup.auto_sync or cfg.startup.auto_analyze):
        fin = _REFRESH.get("finished")
        stale = fin is None or (
            dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(fin)
        ) > dt.timedelta(minutes=10)
        if stale:
            trigger_refresh()
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/profile")
def api_profile(account: str | None = None) -> JSONResponse:
    return JSONResponse(latest_profile(default_account(_cfg(), account)) or {"n_moves": 0})


@app.get("/api/accounts")
def api_accounts() -> JSONResponse:
    cfg = _cfg()
    return JSONResponse({
        "lichess": cfg.lichess_users,
        "chesscom": cfg.chesscom_users,
        "unified": cfg.unified_identity,
    })


# SQLite reads a negative LIMIT as "no limit", so `?limit=-1` returned the whole
# library over the wire.
MAX_GAME_LIST = 500


@app.get("/api/games")
def api_games(account: str | None = None, limit: int = 50) -> JSONResponse:
    limit = max(1, min(MAX_GAME_LIST, limit))
    return JSONResponse(game_list(account=default_account(_cfg(), account), limit=limit))


@app.get("/api/review/{game_id}")
def api_review(game_id: int) -> JSONResponse:
    from coach.coaching.llm import get_narrator

    review = build_review(game_id, narrator=get_narrator())
    # A missing game used to come back as 200 with an error body, so the client
    # rendered a review of nothing rather than saying what went wrong.
    if review.get("error") and not review.get("needs_analysis"):
        raise HTTPException(404, review["error"])
    return JSONResponse(review)


@app.post("/api/games/{game_id}/analyze")
async def api_analyze_game(game_id: int) -> JSONResponse:
    import chess.engine

    from coach.analysis.annotate import analyse_game

    cfg = _cfg()

    def _run() -> dict:
        Session = session_factory()
        with Session() as session:
            game = session.get(Game, game_id)
            if game is None:
                return {"error": "not found"}
            if game.analyzed:
                return {"ok": True, "already": True}
            try:
                engine = chess.engine.SimpleEngine.popen_uci(cfg.engine.stockfish_path)
            except FileNotFoundError:
                return {"error": f"stockfish not found at {cfg.engine.stockfish_path!r}"}
            register_engine(engine)
            try:
                engine.configure({"Threads": 1, "Hash": 128})
                analyse_game(engine, session, game, cfg)
            finally:
                unregister_engine(engine)
                engine.quit()
        return {"ok": True}

    result = await asyncio.to_thread(_run)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return JSONResponse(result)


@app.get("/api/dashboard")
def api_dashboard(account: str | None = None) -> JSONResponse:
    return JSONResponse(build_dashboard(default_account(_cfg(), account)))


@app.get("/api/puzzles/next")
def api_puzzle_next(account: str | None = None, speed: str | None = None,
                    pillar: str | None = None, sources: str | None = None) -> JSONResponse:
    account = default_account(_cfg(), account)
    Session = session_factory()
    with Session() as session:
        weights = _pillar_weights(account)
        if pillar:
            # A session block asking for a specific pillar gets a hard preference.
            weights = {**weights, pillar: weights.get(pillar, 1.0) * 4.0}
        src = [x for x in (sources or "").split(",") if x] or None
        queue = srs.due_queue(session, limit=1, pillar_weights=weights, speed=speed,
                              sources=src)
        # Counted over the same set the queue drew from, so an eight-minute
        # warm-up no longer reports the whole 1,200-card backlog, and a drill
        # block with eleven positions in existence does not claim 1,257.
        #
        # `speed` and `sources` only, deliberately: those are hard filters in
        # `due_queue`, while `pillar` is a weight. Counting the pillar as a
        # filter would report a smaller number than the block will actually
        # serve, which is the same class of mistake in the other direction.
        remaining = srs.due_count(session, speed=speed, sources=src)
        rating = srs.puzzle_rating(session)
        target = srs.selection_rating(session)
        if not queue:
            return JSONResponse({"puzzle": None, "remaining": 0, "your_rating": rating,
                                 "target_rating": target})
        p = queue[0]
        return JSONResponse({
            "remaining": remaining,
            "your_rating": rating,
            # What the queue is aiming at: below the ability estimate by the Elo
            # gap that predicts TARGET_SUCCESS.
            "target_rating": target,
            "puzzle": _puzzle_json(p),
        })


def _puzzle_json(p: Puzzle) -> dict:
    return {
        "id": p.id,
        "fen": p.fen,
        "opponent_first": bool(p.opponent_first) or p.source == "library",
        "pillar": p.pillar,
        "motif": p.motif,
        "kind": p.kind,
        "rating": p.rating_est,
        "source": p.source,
        "uniqueness_cp": p.uniqueness_cp,
        "name": p.name,
        "goal": p.goal,
        "idea": _drill_idea(p),
    }


def _drill_idea(p: Puzzle) -> str | None:
    """The one sentence worth remembering, for drills that carry one."""
    if p.source != "endgame" or not p.external_id:
        return None
    from coach.drills.endgames import BY_KEY

    entry = BY_KEY.get(p.external_id.removeprefix("endgame:"))
    return entry.idea if entry else None


@app.post("/api/puzzles/{puzzle_id}/attempt")
def api_puzzle_attempt(
    puzzle_id: int,
    moves: list[str] = Body(default_factory=list),
    auto_first: bool = Body(default=True),
    elapsed_ms: int | None = Body(default=None),
    hinted: bool = Body(default=False),
    practice: bool = Body(default=False),
) -> JSONResponse:
    """Evaluate an attempt and, when it has resolved, file the SRS review itself.

    The old flow asked the user to grade every puzzle Again/Hard/Good/Easy - up
    to forty decisions an hour that the outcome and the solve time already
    answer. The grade is now derived; ``POST /review`` stays for the manual
    override.

    ``practice`` is what "Take back" sends after a wrong answer. The miss has
    already been filed; finding the move on the second go must not file a
    success over the top of it.
    """
    from coach.puzzles.solve import (
        evaluate_attempt,
        stockfish_refuter,
        stockfish_verifier,
    )

    Session = session_factory()
    with Session() as session:
        p = session.get(Puzzle, puzzle_id)
        if p is None:
            raise HTTPException(404, "puzzle not found")
        # An opening drill asks for *this* line. Another sound move is not an
        # answer to "how does the Caro-Kann go", and an engine refutation of a
        # perfectly good sideline would be teaching the wrong lesson.
        book = p.source == "opening"
        result = evaluate_attempt(p, moves, auto_first,
                                  verifier=None if book else stockfish_verifier,
                                  refuter=None if book else stockfish_refuter)
        if book:
            result["book"] = True

        resolved = result["status"] in ("solved", "wrong", "alternative")
        if resolved and practice:
            result["graded"] = None
            result["practice"] = True
        elif resolved:
            solved = result["status"] in ("solved", "alternative")
            card = srs.ensure_card(session, p)
            grade = srs.grade_from_outcome(card, solved, elapsed_ms)
            if hinted and grade > srs.HARD:
                grade = srs.HARD          # a hint means it wasn't really recalled
            outcome = srs.review(card, grade, elapsed_ms=elapsed_ms)
            # Remembering a line is not evidence of tactical strength, so it
            # leaves the rating that picks puzzle difficulty alone.
            rating = (srs.puzzle_rating(session) if book
                      else srs.update_puzzle_rating(session, p, solved))
            session.commit()
            result["graded"] = {
                "grade": grade,
                "label": {1: "again", 2: "hard", 3: "good", 4: "easy"}[grade],
                "interval_days": round(outcome.interval_days, 3),
                "your_rating": rating,
            }
        return JSONResponse(result)


@app.post("/api/puzzles/{puzzle_id}/review")
def api_puzzle_review(puzzle_id: int, grade: int = Body(embed=True)) -> JSONResponse:
    """Manual grade override. The attempt endpoint grades automatically; this
    exists for the rare 'that was harder than it looked' correction."""
    if grade not in (srs.AGAIN, srs.HARD, srs.GOOD, srs.EASY):
        raise HTTPException(422, "grade must be 1 (again), 2 (hard), 3 (good) or 4 (easy)")
    Session = session_factory()
    with Session() as session:
        p = session.get(Puzzle, puzzle_id)
        if p is None:
            raise HTTPException(404, "puzzle not found")
        card = srs.ensure_card(session, p)
        outcome = srs.review(card, grade)
        session.commit()
        return JSONResponse({
            "interval_days": round(outcome.interval_days, 3),
            "due_at": outcome.due_at.isoformat(),
            "lapsed": outcome.lapsed,
        })


# --------------------------------------------------------------- explore API
#
# What happens *after* a puzzle is graded. The card is already filed by the time
# any of this is reachable, and nothing here writes to the SRS: a player who is
# afraid that poking at a position will cost them a review will not poke at it,
# and poking at it is the entire point.
#
# Stateless, like the drill endpoints - the position travels with the request.


@app.post("/api/explore/start")
async def api_explore_start(
    fen: str = Body(...),
    mode: str = Body(default="punish"),
    user_colour: str | None = Body(default=None),
) -> JSONResponse:
    from coach.puzzles.explore import start

    return JSONResponse(await asyncio.to_thread(start, fen, mode, user_colour))


@app.post("/api/explore/step")
async def api_explore_step(
    fen: str = Body(...),
    uci: str = Body(...),
    mode: str = Body(default="punish"),
    user_colour: str = Body(...),
    hold_cp: int | None = Body(default=None),
    moves_played: int = Body(default=0),
    start_fen: str | None = Body(default=None),
) -> JSONResponse:
    from coach.puzzles.explore import step

    return JSONResponse(await asyncio.to_thread(
        step, fen, uci, mode, user_colour, hold_cp, moves_played, start_fen
    ))


@app.post("/api/explore/candidates")
async def api_explore_candidates(
    fen: str = Body(...),
    top_n: int = Body(default=4),
) -> JSONResponse:
    from coach.puzzles.explore import candidates

    return JSONResponse(await asyncio.to_thread(candidates, fen, top_n))


# ------------------------------------------------------------------- drill API
#
# Endgame drills are played, not solved: they are judged on whether the result
# was preserved, so they need their own step endpoint rather than the puzzle
# solver's stored-line comparison. Stateless - the position travels with the
# request, so a reload doesn't lose the drill.


@app.post("/api/drills/endgame/{puzzle_id}/start")
def api_drill_start(puzzle_id: int) -> JSONResponse:
    from coach.drills.play import start

    Session = session_factory()
    with Session() as session:
        p = session.get(Puzzle, puzzle_id)
        if p is None or p.source != "endgame":
            raise HTTPException(404, "endgame drill not found")
        # A play-out drill's idea is a technique reminder for the twenty moves
        # ahead, so it belongs on screen from the start. A key-move drill's idea
        # names the move - "rook to the fourth rank first" above a Lucena is the
        # answer - so it is held back and returned with the verdict instead.
        idea = _drill_idea(p)
        return JSONResponse({
            **start(p.fen),
            "goal": p.goal,
            "mode": p.kind,
            "name": p.name,
            "idea": idea if p.kind != "key-move" else None,
            "idea_after": idea if p.kind == "key-move" else None,
        })


@app.post("/api/drills/endgame/{puzzle_id}/move")
async def api_drill_move(
    puzzle_id: int,
    fen: str = Body(...),
    uci: str = Body(...),
    moves_played: int = Body(default=0),
    elapsed_ms: int | None = Body(default=None),
    practice: bool = Body(default=False),
) -> JSONResponse:
    """Apply one move of an endgame drill.

    ``practice`` is what the "Try again" button sends. Without it the retry filed
    a second SRS review over the top of the failure it was retrying: the card's
    corrective eight-minute re-show became four and a half hours, difficulty fell
    (after a failure), and a win and twelve rating points were credited on a card
    the player had just got wrong. A second look at a position you have already
    been graded on is practice, and practice does not score.
    """
    from coach.drills.play import moves_played_between, step

    Session = session_factory()
    with Session() as session:
        p = session.get(Puzzle, puzzle_id)
        if p is None or p.source != "endgame":
            raise HTTPException(404, "endgame drill not found")
        goal, mode, start_fen = p.goal, p.kind, p.fen

    user_colour = "white" if chess.Board(start_fen).turn == chess.WHITE else "black"
    # Derived from the two positions rather than taken from the request. The
    # count decides when a "hold the draw" drill has been held long enough and
    # when a conversion has run out of moves, so a client sending
    # `moves_played: 999` - a stale tab, a replayed request - could end a drill
    # outright and file the lapse that went with it.
    counted = moves_played_between(start_fen, fen)
    if counted is None:
        counted = max(0, moves_played)
    result = await asyncio.to_thread(
        step, fen, uci, goal, mode, user_colour, counted
    )

    if result["status"] in ("success", "failed") and not practice:
        with Session() as session:
            p = session.get(Puzzle, puzzle_id)
            card = srs.ensure_card(session, p)
            grade = srs.grade_from_outcome(card, result["status"] == "success", elapsed_ms)
            srs.review(card, grade, elapsed_ms=elapsed_ms)
            srs.update_puzzle_rating(session, p, result["status"] == "success")
            session.commit()
            result["graded"] = {"grade": grade,
                                "label": {1: "again", 2: "hard", 3: "good", 4: "easy"}[grade]}
    elif result["status"] in ("success", "failed"):
        result["graded"] = None
        result["practice"] = True
    return JSONResponse(result)


@app.post("/api/drills/refresh")
async def api_drills_refresh() -> JSONResponse:
    """Re-seed the endgame set and re-mine repertoire lines from recent games."""
    from coach.drills.seed import seed_all

    return JSONResponse(await asyncio.to_thread(seed_all, None))


# ---------------------------------------------------------------- openings API
#
# Learning a named opening on purpose: browse the catalogue, study a line move
# by move, then drill it on the SRS. Drills are puzzle rows, so attempts go
# through `POST /api/puzzles/{id}/attempt` like everything else.


def _opening_or_404(key: str):
    from coach.openings.catalog import BY_KEY

    if key not in BY_KEY:
        raise HTTPException(404, "unknown opening")
    return BY_KEY[key]


@app.get("/api/openings")
def api_openings() -> JSONResponse:
    from coach.openings.trainer import catalog_overview

    return JSONResponse({"openings": catalog_overview()})


@app.get("/api/openings/{key}")
def api_opening(key: str) -> JSONResponse:
    from coach.openings.trainer import opening_detail

    _opening_or_404(key)
    return JSONResponse(opening_detail(key))


@app.post("/api/openings/{key}/learn")
def api_opening_learn(key: str) -> JSONResponse:
    from coach.openings.trainer import learn

    _opening_or_404(key)
    return JSONResponse({"added": learn(key)})


@app.post("/api/openings/{key}/forget")
def api_opening_forget(key: str) -> JSONResponse:
    from coach.openings.trainer import forget

    _opening_or_404(key)
    return JSONResponse({"removed": forget(key)})


@app.get("/api/openings/{key}/next")
def api_opening_next(key: str, line: str | None = None) -> JSONResponse:
    """The next line to drill in this opening.

    ``practice`` is true when nothing is due: the line comes back anyway, and
    the browser sends its attempts as practice so they are not scored.
    """
    from coach.openings.trainer import due_in_opening, next_drill

    _opening_or_404(key)
    p, practice, next_due = next_drill(key, line)
    return JSONResponse({
        "puzzle": _puzzle_json(p) if p else None,
        "practice": practice,
        "remaining": due_in_opening(key),
        "next_due": next_due.isoformat() if next_due else None,
    })


# ----------------------------------------------------------------- session API
#
# The Today tab. Everything the old Play tab asked the user to choose - Elo,
# style, colour, opening, which puzzles, what to review - is decided here from
# the weakness profile, so the app opens on one button.


@app.get("/api/session/today")
def api_session_today(account: str | None = None) -> JSONResponse:
    from coach.session import plan as sp

    account = default_account(_cfg(), account)
    existing = sp.current_session(account)
    if existing is not None:
        return JSONResponse({"active": True, **sp.as_dict(existing)})
    # No session yet: show what today would look like without committing to it.
    return JSONResponse({"active": False, "preview": sp.build_plan(account)})


@app.post("/api/session/start")
def api_session_start(account: str | None = Body(default=None),
                      minutes: int = Body(default=60)) -> JSONResponse:
    from coach.session import plan as sp

    return JSONResponse(sp.start_session(default_account(_cfg(), account), minutes))


@app.post("/api/session/{session_id}/advance")
def api_session_advance(session_id: int, record: dict | None = Body(default=None)) -> JSONResponse:
    from coach.session import plan as sp

    return JSONResponse(sp.advance(session_id, record))


@app.post("/api/session/{session_id}/progress")
def api_session_progress(session_id: int, record: dict = Body(default_factory=dict)) -> JSONResponse:
    from coach.session import plan as sp

    return JSONResponse(sp.note_progress(session_id, record))


@app.post("/api/session/{session_id}/finish")
def api_session_finish(session_id: int) -> JSONResponse:
    from coach.session import plan as sp

    return JSONResponse(sp.finish(session_id))


# How many of the block's games the review covers, newest last. Three games at
# three moments each is already more than a ten-minute block can work through.
MAX_REVIEW_GAMES = 3


@app.get("/api/session/{session_id}/review-moments")
def api_session_moments(session_id: int, game_id: int | None = None) -> JSONResponse:
    """The review block: the worst moments of the games just played, as positions
    to solve rather than prose to read."""
    import json as _json

    from sqlalchemy import select

    from coach.session import plan as sp
    from coach.store.db import TrainingSession

    Session = session_factory()
    with Session() as session:
        ts = session.get(TrainingSession, session_id)
        if ts is None:
            raise HTTPException(404, "session not found")
        progress = _json.loads(ts.progress or "{}")
        started = ts.started_at
    game_ids = []
    if game_id:
        game_ids = [game_id]
    else:
        for block in progress.values():
            game_ids.extend(block.get("game_ids") or [])
        if not game_ids and started is not None:
            # Belt and braces. A game is filed the moment it ends, but the ids
            # reach the session from the browser, and a tab that is closed or
            # crashes between the last move and the review block used to take
            # the whole review with it. The games are still there to be found by
            # when they were played, and an empty review block - the part of the
            # hour that turns the game into coaching - is the one outcome worth
            # a second query to avoid.
            with Session() as session:
                game_ids = list(
                    session.execute(
                        select(Game.id)
                        .where(Game.source == "sparring", Game.played_at >= started)
                        .order_by(Game.id)
                    ).scalars()
                )
    out = []
    for gid in game_ids[-MAX_REVIEW_GAMES:]:
        r = build_review(gid)
        if r.get("needs_analysis") or r.get("error"):
            out.append({"game_id": gid, "pending": True})
            continue
        out.append({
            "game_id": gid,
            "acpl": r.get("avg_cp_loss"),
            "focus_pillar": r.get("focus_pillar"),
            "moments": r.get("key_moments", [])[:3],
        })
    return JSONResponse({"games": out, "session_id": session_id,
                         "done": bool(sp.current_session(None) is None)})


def _legal_dests(board: chess.Board) -> dict[str, list[str]]:
    dests: dict[str, list[str]] = {}
    for mv in board.legal_moves:
        dests.setdefault(chess.square_name(mv.from_square), []).append(
            chess.square_name(mv.to_square)
        )
    return dests


def _state_msg(board: chess.Board, you: str, opening: str | None, elo: int,
               last_move: str | None, clocks: dict | None = None) -> dict:
    return {
        "type": "state",
        "fen": board.fen(),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "you": you,
        "dests": _legal_dests(board),
        "last_move": last_move,
        "opening": opening,
        "elo": elo,
        # The square of the king in check, not just whether there is one: the
        # board marks a square, and a bare boolean gave it nothing to mark.
        "check": (chess.square_name(board.king(board.turn))
                  if board.is_check() and board.king(board.turn) is not None else None),
        "fullmove": board.fullmove_number,
        "clocks": clocks or {},
    }


def _result_reason(board: chess.Board) -> tuple[str, str]:
    if board.is_checkmate():
        return ("1-0" if board.turn == chess.BLACK else "0-1", "checkmate")
    if board.is_stalemate():
        return ("1/2-1/2", "stalemate")
    if board.is_insufficient_material():
        return ("1/2-1/2", "insufficient material")
    if board.is_seventyfive_moves():
        return ("1/2-1/2", "75-move rule")
    if board.is_fivefold_repetition():
        return ("1/2-1/2", "fivefold repetition")
    if board.can_claim_draw():
        return ("1/2-1/2", "draw claimed")
    return ("*", "")


def _persist_game(board: chess.Board, account: str, you: str, elo: int,
                  start_fen: str, opening: str | None, result_override: str | None = None,
                  clock_log: list[float] | None = None,
                  time_control: str | None = None) -> int | None:
    game = chess.pgn.Game()
    game.headers["Event"] = "Coach sparring"
    game.headers["Date"] = dt.datetime.now(dt.UTC).strftime("%Y.%m.%d")
    game.headers["White"] = account if you == "white" else f"Coach {elo}"
    game.headers["Black"] = account if you == "black" else f"Coach {elo}"
    game.headers["Result"] = result_override or board.result(claim_draw=True)
    if opening:
        game.headers["Opening"] = opening
    if start_fen != chess.STARTING_FEN:
        game.headers["FEN"] = start_fen
        game.headers["SetUp"] = "1"
    if time_control:
        game.headers["TimeControl"] = time_control
    node = game
    # Emit [%clk] comments so the normal ingest path fills in clock_ms, which is
    # what feeds the time-trouble statistics. Without this, sparring games were
    # the only games in the library with no clock data - so the one failure mode
    # the profile explicitly tracks was the one it could never rehearse.
    for i, mv in enumerate(board.move_stack):
        node = node.add_variation(mv)
        if clock_log is not None and i < len(clock_log):
            node.set_clock(max(0.0, clock_log[i]))
    pgn_text = str(game)

    Session = session_factory()
    with Session() as session:
        g = store_game(
            session,
            source="sparring",
            source_id=f"{account}-{dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}",
            account=account,
            pgn_text=pgn_text,
            speed="sparring",
            played_at=dt.datetime.now(dt.UTC).replace(tzinfo=None),
            opening=opening,
        )
        session.commit()
        return g.id if g is not None else None


def _analyse_one_bg(game_id: int, on_ready=None) -> None:
    """Analyse a single freshly-finished sparring game in the background so its
    post-match report is ready without a manual step.

    ``on_ready(game_id)`` is called once the analysis lands, so the caller can
    push the report down an open websocket instead of having the client poll.
    """
    def _run() -> None:
        import chess.engine

        from coach.analysis.annotate import analyse_game
        cfg = _cfg()
        Session = session_factory()
        with Session() as session:
            game = session.get(Game, game_id)
            if game is None or game.analyzed:
                return
            try:
                eng = chess.engine.SimpleEngine.popen_uci(cfg.engine.stockfish_path)
            except FileNotFoundError:
                return
            register_engine(eng)
            try:
                # Nothing else is competing for the machine at this point and the
                # review block is sitting in front of the user waiting for this
                # exact result, so it gets more than the one thread the batch
                # pass uses.
                threads = max(1, min(4, cfg.engine.workers))
                eng.configure({"Threads": threads, "Hash": 256})
                analyse_game(eng, session, game, cfg)
            finally:
                unregister_engine(eng)
                eng.quit()
        # Refresh the profile + puzzles so the dashboard/puzzle views reflect it.
        try:
            from coach.profile.weakness_model import build_profile
            from coach.puzzles.miner import mine_all

            mine_all(account=None)
            build_profile(account=None)
        except Exception:  # noqa: BLE001, S110
            pass
        if on_ready is not None:
            try:
                on_ready(game_id)
            except Exception:  # noqa: BLE001, S110 - notifying must never break analysis
                pass

    threading.Thread(target=_run, daemon=True).start()


# ------------------------------------------------------------ live game store
#
# A sparring game used to live entirely in `ws_play`'s local variables, so the
# socket *was* the game: reloading the page, letting a phone sleep, or walking
# between rooms on a flaky network lost the position and dealt a fresh game. The
# block's earnings already survived that; the game in front of you did not.
#
# What is kept here is only what a game can be rebuilt from - the start position,
# the moves, the clocks and the settings. Deliberately not the Stockfish process:
# a stored opponent would hold an engine open for every abandoned tab. The
# opponent is rebuilt on resume from `elo`/`style`/`weak`, which is all it needs.
#
# The clock is paused while nobody is connected. There is no way to tell a closed
# lid from a long think, and charging a player for time they could not see the
# board is the worse of the two errors in a training app.

_LIVE_GAMES: dict[str, dict] = {}
_LIVE_LOCK = threading.Lock()
# Long enough to survive a commute, short enough that the dict cannot grow
# without bound on a machine that is never restarted.
LIVE_GAME_TTL_S = 6 * 3600
MAX_LIVE_GAMES = 32


def _prune_live(now: float) -> None:
    dead = [k for k, g in _LIVE_GAMES.items() if now - g["updated_at"] > LIVE_GAME_TTL_S]
    for k in dead:
        _LIVE_GAMES.pop(k, None)
    while len(_LIVE_GAMES) > MAX_LIVE_GAMES:
        oldest = min(_LIVE_GAMES, key=lambda k: _LIVE_GAMES[k]["updated_at"])
        _LIVE_GAMES.pop(oldest, None)


def save_live_game(key: str, state: dict) -> None:
    with _LIVE_LOCK:
        state["updated_at"] = dt.datetime.now(dt.UTC).timestamp()
        _LIVE_GAMES[key] = state
        _prune_live(state["updated_at"])


def load_live_game(key: str) -> dict | None:
    with _LIVE_LOCK:
        game = _LIVE_GAMES.get(key)
        if game is None:
            return None
        if dt.datetime.now(dt.UTC).timestamp() - game["updated_at"] > LIVE_GAME_TTL_S:
            _LIVE_GAMES.pop(key, None)
            return None
        return dict(game)


def drop_live_game(key: str | None) -> None:
    if not key:
        return
    with _LIVE_LOCK:
        _LIVE_GAMES.pop(key, None)


# One takeback per game, offered only for a genuine blunder. Feedback while the
# position is still live in working memory is worth far more than the same point
# made in a review the user has to go and read - but offering it every time would
# make it a crutch, so it fires once.
COACHED_DROP_CP = 200
_COACH_CHECK_NODES = 90_000

# Fewer user moves than this and the game is not stored, not analysed and not
# rated: it is someone changing their mind, not a game.
MIN_RATED_USER_MOVES = 4


def _blunder_check(board: chess.Board, move: chess.Move) -> dict | None:
    """Would ``move`` drop at least COACHED_DROP_CP against the best move here?"""
    try:
        from coach.sparring.backend import shared_backend

        ranked = shared_backend().score_roots(board, _COACH_CHECK_NODES)
    except Exception:  # noqa: BLE001 - coaching is best-effort, never blocks a move
        return None
    if not ranked:
        return None
    best_mv, best_cp = ranked[0]
    played_cp = next((cp for mv, cp in ranked if mv == move), None)
    if played_cp is None:
        return None
    drop = best_cp - played_cp
    if drop < COACHED_DROP_CP:
        return None
    try:
        played_san, best_san = board.san(move), board.san(best_mv)
    except (ValueError, AssertionError):
        return None
    return {"drop": int(drop), "played_san": played_san, "best_san": best_san,
            "played_uci": move.uci()}


@app.websocket("/ws/play")
async def ws_play(ws: WebSocket) -> None:
    await ws.accept()
    board = chess.Board()
    opp = SparringOpponent()
    you = "white"
    elo = 1500
    account = "me"
    opening: str | None = None
    start_fen = chess.STARTING_FEN
    started = False
    weak: str | None = None
    style = "solid"
    auto_state: dict = {}
    prof_acct: str | None = None
    # Clocks. base/inc come from the session plan's time control; `clock` holds
    # the remaining seconds per side and `clock_log` records the reading after
    # each move so the finished game carries real clock data.
    base_s, inc_s = 0, 0
    clock = {"white": 0.0, "black": 0.0}
    clock_log: list[float] = []
    turn_started: float | None = None
    flagged_side: str | None = None
    session_id: int | None = None
    takeback_used = False
    pending_blunder: dict | None = None
    coached = False
    game_key: str | None = None

    async def send(msg: dict) -> None:
        await ws.send_text(json.dumps(msg))

    def snapshot() -> None:
        """Write the game down so a reload can pick it back up.

        Called after every position change. The clock is stored as it stands with
        the running side's elapsed time already charged, so a resume restarts the
        turn from the stored figure rather than billing the gap.
        """
        if not game_key or not started:
            return
        live = dict(clock)
        if turn_started is not None and base_s:
            side = "white" if board.turn == chess.WHITE else "black"
            live[side] = max(0.0, live[side]
                             - (asyncio.get_event_loop().time() - turn_started))
        save_live_game(game_key, {
            "start_fen": start_fen,
            "moves": [m.uci() for m in board.move_stack],
            "you": you, "elo": elo, "style": style, "weak": weak,
            "opening": opening, "account": account, "prof_acct": prof_acct,
            "auto_state": auto_state, "base_s": base_s, "inc_s": inc_s,
            "clock": live, "clock_log": list(clock_log),
            "session_id": session_id, "takeback_used": takeback_used,
            "coached": coached,
        })

    def clock_state() -> dict:
        if not base_s:
            return {}
        live = dict(clock)
        if turn_started is not None and started:
            side = "white" if board.turn == chess.WHITE else "black"
            live[side] = max(0.0, live[side] - (asyncio.get_event_loop().time() - turn_started))
        return {"white": round(live["white"], 1), "black": round(live["black"], 1),
                "base_s": base_s, "inc_s": inc_s}

    def start_turn() -> None:
        nonlocal turn_started
        turn_started = asyncio.get_event_loop().time() if base_s else None

    def stop_turn() -> str | None:
        """Charge elapsed time to the side to move. Returns their colour if they
        flagged. Must be called *before* the move is pushed, while ``board.turn``
        still names the player whose clock was running."""
        nonlocal turn_started
        if not base_s or turn_started is None:
            return None
        side = "white" if board.turn == chess.WHITE else "black"
        spent = asyncio.get_event_loop().time() - turn_started
        clock[side] -= spent
        turn_started = None
        if clock[side] <= 0:
            clock[side] = 0.0
            return side
        clock[side] += inc_s
        return None

    def full_state(last: str | None) -> dict:
        return {**_state_msg(board, you, opening, elo, last, clock_state()),
                "weak_pillar": weak, "style": style, "auto": auto_state,
                "session_id": session_id, "takeback_used": takeback_used,
                "game_key": game_key}

    async def finish(result: str, reason: str) -> None:
        nonlocal started
        started = False
        drop_live_game(game_key)
        tc = f"{base_s}+{inc_s}" if base_s else None
        # A game the user bailed out of after a move or two says nothing about
        # how they play. Filed anyway it would enter the library, get a slice of
        # Stockfish time, skew the weakness profile it feeds, and move the
        # adaptive Elo a full K-factor - so an immediate resign used to drop the
        # sparring bot 40 points. Below the threshold it is simply discarded.
        white_moved_first = chess.Board(start_fen).turn == chess.WHITE
        you_are_white = you == "white"
        user_plies = sum(
            1 for i in range(len(board.move_stack))
            if (white_moved_first == (i % 2 == 0)) == you_are_white
        )
        counts = user_plies >= MIN_RATED_USER_MOVES
        gid = None
        if counts:
            gid = _persist_game(board, account, you, elo, start_fen, opening,
                                result_override=result if reason == "resignation" else None,
                                clock_log=clock_log or None, time_control=tc)
        user_score = (
            1.0 if (result == "1-0") == (you == "white") and result != "1/2-1/2"
            else 0.5 if result == "1/2-1/2" else 0.0
        )
        new_elo = None
        if counts and auto_state.get("elo"):
            from coach.profile import rating
            new_elo = rating.record_result(user_score, prof_acct)
        await send({
            "type": "gameover", "result": result, "reason": reason,
            "pgn": _game_pgn(board, start_fen),
            "game_id": gid, "review_pending": gid is not None,
            "next_elo": new_elo, "counted": counts,
            "user_moves": user_plies,
        })
        if gid is not None:
            # Analyse in the background and push the report down this socket when
            # it lands, instead of the client polling the full review endpoint
            # every three seconds for two minutes.
            loop = asyncio.get_running_loop()

            def _on_ready(game_id: int) -> None:
                report = build_review(game_id)
                payload = {"type": "report", "game_id": game_id,
                           "acpl": report.get("avg_cp_loss"),
                           "focus_pillar": report.get("focus_pillar"),
                           "key_moments": report.get("key_moments", [])[:3]}
                asyncio.run_coroutine_threadsafe(send(payload), loop)

            _analyse_one_bg(gid, on_ready=_on_ready)

    async def opponent_move() -> bool:
        """Play the opponent's move if it's their turn. Returns True if game ended."""
        nonlocal board, flagged_side
        if board.is_game_over(claim_draw=True):
            return True
        await send({"type": "thinking"})
        start_turn()
        mv = await asyncio.to_thread(opp.choose, board)
        flagged = stop_turn()
        board.push(mv)
        clock_log.append(clock["white" if board.turn == chess.BLACK else "black"])
        await send(full_state(mv.uci()))
        if flagged:
            flagged_side = flagged
            return True
        if board.is_game_over(claim_draw=True):
            return True
        start_turn()
        snapshot()
        return False

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "new":
                cfg = _cfg()
                canon = cfg.all_users[0] if cfg.all_users else "me"
                account = (msg.get("account") or canon) if not cfg.unified_identity else canon
                prof_acct = None if cfg.unified_identity else account
                colour = msg.get("colour", "auto")
                weak = top_weak_pillar(prof_acct) or top_weak_pillar(None)

                from coach.profile import rating

                raw_elo = msg.get("elo", 1500)
                auto_elo = raw_elo in ("auto", None) or msg.get("auto_elo")
                elo = rating.adaptive_elo(prof_acct) if auto_elo else int(raw_elo)

                raw_style = msg.get("style", "solid")
                auto_style = raw_style == "auto"
                style = rating.next_style(weak, prof_acct) if auto_style else raw_style
                auto_state = {"elo": auto_elo, "style": auto_style}
                if msg.get("use_weak_opening", True):
                    pick = pick_start(prof_acct, None if colour == "auto" else colour,
                                      mode=msg.get("opening_mode", "mix"))
                    start_fen = pick["fen"]
                    opening = pick["opening"]
                    you = pick["user_colour"]
                else:
                    start_fen = chess.STARTING_FEN
                    opening = None
                    you = colour if colour in ("white", "black") else "white"
                board = chess.Board(start_fen)
                opp.set_profile(target_elo=elo, weak_pillar=weak, style=style)

                base_s = int(msg.get("base_s") or 0)
                inc_s = int(msg.get("inc_s") or 0)
                clock = {"white": float(base_s), "black": float(base_s)}
                clock_log = []
                flagged_side = None
                session_id = msg.get("session_id")
                takeback_used = False
                pending_blunder = None
                coached = bool(msg.get("coached", False))

                drop_live_game(game_key)
                game_key = uuid.uuid4().hex
                started = True
                await send(full_state(None))
                start_turn()
                snapshot()
                # If it's the opponent's move in the chosen start position, play it.
                if (board.turn == chess.WHITE) != (you == "white"):
                    await opponent_move()

            elif mtype == "resume":
                # A reload, a slept phone, a dropped wifi. The game is rebuilt
                # from what was written down rather than dealt again.
                saved = load_live_game(msg.get("key") or "")
                if not saved:
                    await send({"type": "no_game", "key": msg.get("key")})
                    continue
                game_key = msg["key"]
                start_fen = saved["start_fen"]
                board = chess.Board(start_fen)
                for uci in saved["moves"]:
                    try:
                        board.push(chess.Move.from_uci(uci))
                    except ValueError:
                        break
                you, elo, style = saved["you"], saved["elo"], saved["style"]
                weak, opening = saved["weak"], saved["opening"]
                account, prof_acct = saved["account"], saved["prof_acct"]
                auto_state = saved["auto_state"]
                base_s, inc_s = saved["base_s"], saved["inc_s"]
                clock = dict(saved["clock"])
                clock_log = list(saved["clock_log"])
                session_id = saved["session_id"]
                takeback_used = saved["takeback_used"]
                coached = saved["coached"]
                pending_blunder = None
                flagged_side = None
                opp.set_profile(target_elo=elo, weak_pillar=weak, style=style)
                if board.is_game_over(claim_draw=True):
                    drop_live_game(game_key)
                    await send({"type": "no_game", "key": game_key})
                    continue
                started = True
                last = board.move_stack[-1].uci() if board.move_stack else None
                await send(full_state(last))
                start_turn()
                snapshot()
                # Resuming on the bot's turn: it owes a move.
                if (board.turn == chess.WHITE) != (you == "white"):
                    over = await opponent_move()
                    if over:
                        result, reason = _result_reason(board)
                        await finish(result, reason)

            elif mtype == "move" and started:
                uci = msg.get("uci", "")
                if len(uci) == 4:
                    piece = board.piece_at(chess.parse_square(uci[:2]))
                    dest_rank = uci[3]
                    if piece and piece.piece_type == chess.PAWN and dest_rank in ("1", "8"):
                        uci += msg.get("promotion", "q")
                try:
                    mv = chess.Move.from_uci(uci)
                except ValueError:
                    await send({"type": "error", "msg": f"bad move {uci}"})
                    continue
                if mv not in board.legal_moves:
                    await send({"type": "error", "msg": "illegal move"})
                    continue

                # Coached mode: one chance to take a genuine blunder back, offered
                # while the position is still in front of you. Skipped when the
                # user has already confirmed this exact move.
                if (coached and not takeback_used
                        and msg.get("confirm") != uci
                        and pending_blunder is None):
                    warn = await asyncio.to_thread(_blunder_check, board, mv)
                    if warn:
                        pending_blunder = warn
                        await send({"type": "coach_warning", **warn})
                        continue
                pending_blunder = None
                if msg.get("confirm") == uci:
                    takeback_used = True

                flagged = stop_turn()          # before the push, while it's still our turn
                board.push(mv)
                clock_log.append(clock["white" if board.turn == chess.BLACK else "black"])
                await send(full_state(mv.uci()))
                snapshot()
                over = bool(flagged) or board.is_game_over(claim_draw=True)
                if flagged:
                    flagged_side = flagged
                elif not over:
                    start_turn()
                    over = await opponent_move()
                if over:
                    if flagged_side:
                        result = "0-1" if flagged_side == "white" else "1-0"
                        await finish(result, "time")
                    else:
                        result, reason = _result_reason(board)
                        await finish(result, reason)

            elif mtype == "takeback" and started:
                # Decline the move we warned about: nothing was pushed, so there is
                # nothing to undo - just clear the warning and let them play again.
                pending_blunder = None
                takeback_used = True
                snapshot()
                await send(full_state(board.move_stack[-1].uci() if board.move_stack else None))

            elif mtype == "resign" and started:
                await finish("0-1" if you == "white" else "1-0", "resignation")

    except WebSocketDisconnect:
        # The game is not over, it is unattended. `snapshot` has already written
        # the clock with the current turn's elapsed time charged, so the stored
        # position is correct and the clock simply stops until someone resumes.
        return
    finally:
        # Each connection owns a Stockfish process; without this they accumulate
        # for the life of the server, one per tab ever opened.
        opp.close()


def _game_pgn(board: chess.Board, start_fen: str) -> str:
    g = chess.pgn.Game()
    if start_fen != chess.STARTING_FEN:
        g.headers["FEN"] = start_fen
        g.headers["SetUp"] = "1"
    node = g
    for mv in board.move_stack:
        node = node.add_variation(mv)
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
    return g.accept(exporter)


# Static assets (JS/CSS). Mounted last so it doesn't shadow the API routes.
if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def serve(host: str = "0.0.0.0", port: int = 8000, open_browser: bool = False,
          no_refresh: bool = False) -> None:
    import uvicorn

    global _REFRESH_ON_START
    _REFRESH_ON_START = not no_refresh

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
    uvicorn.run(app, host=host, port=port)
