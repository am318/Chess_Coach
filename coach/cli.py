"""Command-line entry point for the coaching bot.

    python -m coach.cli sync              # pull new games from Lichess + Chess.com
    python -m coach.cli import-pgn FILE   # add games from a PGN file
    python -m coach.cli analyze [--limit N] [--refresh]
    python -m coach.cli repair [--force] [--prune-snapshots]
    python -m coach.cli calibrate [--positions N]
    python -m coach.cli profile [--account NAME] [--json]
    python -m coach.cli stats
    python -m coach.cli games [--account NAME]
    python -m coach.cli review GAME_ID [--llm]
    python -m coach.cli puzzles mine | import-lichess FILE | stats
    python -m coach.cli drills list | refresh
    python -m coach.cli serve [--host H --port P]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from coach.config import CoachConfig
from coach.store.db import Game, session_factory


def _cmd_sync(args) -> None:
    cfg = CoachConfig.load()
    from coach.ingest import chesscom, lichess

    Session = session_factory()
    total = 0
    failures: list[str] = []
    # One account failing must not abandon the others - a single site outage or a
    # renamed handle used to abort the whole sync partway through.
    with Session() as session:
        for site, users, sync in (
            ("lichess", cfg.lichess_users, lichess.sync_user),
            ("chess.com", cfg.chesscom_users, chesscom.sync_user),
        ):
            for user in users:
                print(f"{site}: {user}")
                try:
                    total += sync(session, cfg, user)
                except Exception as exc:  # noqa: BLE001
                    session.rollback()
                    failures.append(f"{site}/{user}: {exc}")
                    print(f"  ! failed: {exc}")
    print(f"done: {total} new games" + (f", {len(failures)} account(s) failed" if failures else ""))


def _cmd_import_pgn(args) -> None:
    cfg = CoachConfig.load()
    from coach.ingest.pgn_import import import_pgn_text

    accounts = cfg.lichess_users + cfg.chesscom_users + list(args.also_named or [])
    if not accounts:
        sys.exit("no accounts configured; set them in coach.toml or pass --also-named")
    text = Path(args.file).read_text()
    Session = session_factory()
    with Session() as session:
        added = import_pgn_text(session, text, accounts)
    print(f"imported {added} games")


def _cmd_analyze(args) -> None:
    cfg = CoachConfig.load()
    from coach.analysis.annotate import analyse_pending

    done = analyse_pending(cfg, limit=args.limit, deep=args.deep, refresh=args.refresh)
    verb = "re-analysed" if args.refresh else ("deep-analysed" if args.deep else "analysed")
    print(f"{verb} {done} games")
    if args.refresh:
        print("run `coach profile` to rebuild the weakness profile from the new numbers")


def _cmd_repair(args) -> None:
    """Recompute everything derivable from stored evaluations. No engine needed."""
    cfg = CoachConfig.load()
    from coach.analysis.repair import repair_all
    from coach.profile.weakness_model import prune_snapshots

    out = repair_all(cfg=cfg, force=args.force)
    for label, n in out.items():
        print(f"  {label}: {n}")
    if args.prune_snapshots:
        print(f"  snapshots pruned: {prune_snapshots()}")


def _cmd_calibrate(args) -> None:
    from coach.profile.calibration import calibrate, render_text

    result = calibrate(positions=args.positions)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render_text(result))


def _cmd_drills(args) -> None:
    from coach.drills.repertoire import repertoire_summary
    from coach.drills.seed import seed_all

    if args.action == "refresh":
        print(seed_all(account=args.account))
        return
    from coach.drills.endgames import ENDGAMES

    print(f"{len(ENDGAMES)} endgame drills:")
    for e in ENDGAMES:
        print(f"  [{e.category:<11}] {e.name:<32} {e.mode:<9} goal={e.goal:<5} ~{e.rating}")
    summary = repertoire_summary(args.account)
    print(f"\n{summary['lines']} repertoire lines:")
    for name, n in sorted(summary["by_opening"].items()):
        print(f"  {name}" + (f"  x{n}" if n > 1 else ""))


def _cmd_profile(args) -> None:
    from coach.profile.weakness_model import build_profile, render_text

    profile = build_profile(account=args.account)
    if args.json:
        print(json.dumps(profile, indent=2))
    else:
        print(render_text(profile))


def _cmd_games(args) -> None:
    from coach.coaching.review import game_list

    for g in game_list(account=args.account, limit=args.limit):
        flag = "A" if g["analyzed"] else " "
        print(f"[{flag}] {g['id']:>5}  {g['played_at'] or '?':<20}  {g['colour']:<5} "
              f"{g['result']:<5} vs {g['opponent'] or '?':<18} {g['opening'] or ''}")


def _cmd_review(args) -> None:
    from coach.coaching.llm import get_narrator
    from coach.coaching.review import build_review

    r = build_review(args.game_id, narrator=get_narrator(args.llm))
    if args.json:
        print(json.dumps(r, indent=2))
        return
    if "error" in r:
        print(r["error"]); return
    print(f"Game {r['game_id']}  {r['colour']} vs {r['opponent']}  ({r['result']}, {r['opening']})")
    print(r["summary"])
    print("\nKey moments:")
    for m in r["key_moments"]:
        print(f"  move {m['move_number']}{'.' if m['side']=='white' else '...'} "
              f"{m['played_san']}  (-{m['cp_loss']}cp, {m['pillar']})")
        if m["note"]:
            print(f"      {m['note']}")
    print(f"\nFocus next: {r['focus_pillar']}")


def _cmd_puzzles(args) -> None:
    if args.action == "mine":
        from coach.puzzles.miner import mine_all

        n = mine_all(account=args.account, user_elo=args.elo)
        print(f"mined {n} new puzzles from your games")
    elif args.action == "import-lichess":
        from coach.puzzles.library import import_lichess_csv

        n = import_lichess_csv(args.file, user_elo=args.elo, band=args.band)
        print(f"imported {n} library puzzles")
    elif args.action == "stats":
        from sqlalchemy import func

        from coach.store.db import Puzzle, SrsCard
        Session = session_factory()
        with Session() as session:
            total = session.query(Puzzle).count()
            by_pillar = dict(session.query(Puzzle.pillar, func.count()).group_by(Puzzle.pillar).all())
            due = session.query(SrsCard).filter(SrsCard.due_at <= _now()).count()
        print(f"puzzles: {total}  (due now: {due})")
        for p, n in sorted(by_pillar.items()):
            print(f"  {p}: {n}")


def _now():
    import datetime as _dt
    return _dt.datetime.now(_dt.UTC).replace(tzinfo=None)


def _cmd_serve(args) -> None:
    from coach.server.app import serve

    print(f"coach web app on http://{args.host}:{args.port}  (reachable from your phone on the LAN)")
    serve(host=args.host, port=args.port, open_browser=args.open, no_refresh=args.no_refresh)


def _cmd_stats(args) -> None:
    Session = session_factory()
    with Session() as session:
        total = session.query(Game).count()
        analysed = session.query(Game).filter(Game.analyzed.is_(True)).count()
        by_src = {}
        for (src,) in session.query(Game.source).distinct():
            by_src[src] = session.query(Game).filter(Game.source == src).count()
    print(f"games: {total} ({analysed} analysed)")
    for src, n in by_src.items():
        print(f"  {src}: {n}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="coach")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync").set_defaults(func=_cmd_sync)

    p_imp = sub.add_parser("import-pgn")
    p_imp.add_argument("file")
    p_imp.add_argument("--also-named", nargs="*", help="extra name spellings that are you")
    p_imp.set_defaults(func=_cmd_import_pgn)

    p_an = sub.add_parser("analyze")
    p_an.add_argument("--limit", type=int, default=None)
    p_an.add_argument("--deep", action="store_true",
                      help="re-analyse already-analysed games at a deeper budget")
    p_an.add_argument("--refresh", action="store_true",
                      help="re-analyse games already marked analysed, newest first, "
                           "at the normal budget (needed once after the cp-loss fix)")
    p_an.set_defaults(func=_cmd_analyze)

    p_rp = sub.add_parser("repair", help="recompute derived fields; no Stockfish needed")
    p_rp.add_argument("--force", action="store_true",
                      help="re-classify every move even if the classifier is unchanged")
    p_rp.add_argument("--prune-snapshots", action="store_true",
                      help="collapse profile snapshot history to one per day")
    p_rp.set_defaults(func=_cmd_repair)

    p_cal = sub.add_parser("calibrate",
                           help="is the sparring bot the strength it claims to be?")
    p_cal.add_argument("--positions", type=int, default=120)
    p_cal.add_argument("--json", action="store_true")
    p_cal.set_defaults(func=_cmd_calibrate)

    p_pr = sub.add_parser("profile")
    p_pr.add_argument("--account", default=None)
    p_pr.add_argument("--json", action="store_true")
    p_pr.set_defaults(func=_cmd_profile)

    sub.add_parser("stats").set_defaults(func=_cmd_stats)

    p_g = sub.add_parser("games")
    p_g.add_argument("--account", default=None)
    p_g.add_argument("--limit", type=int, default=50)
    p_g.set_defaults(func=_cmd_games)

    p_rv = sub.add_parser("review")
    p_rv.add_argument("game_id", type=int)
    p_rv.add_argument("--llm", action="store_true", help="use LLM narration (needs ANTHROPIC_API_KEY)")
    p_rv.add_argument("--json", action="store_true")
    p_rv.set_defaults(func=_cmd_review)

    p_pz = sub.add_parser("puzzles")
    p_pz.add_argument("action", choices=["mine", "import-lichess", "stats"])
    p_pz.add_argument("file", nargs="?", help="CSV path for import-lichess")
    p_pz.add_argument("--account", default=None)
    p_pz.add_argument("--elo", type=int, default=1500)
    p_pz.add_argument("--band", type=int, default=300)
    p_pz.set_defaults(func=_cmd_puzzles)

    p_dr = sub.add_parser("drills", help="endgame technique set + repertoire lines")
    p_dr.add_argument("action", nargs="?", default="list", choices=["list", "refresh"])
    p_dr.add_argument("--account", default=None)
    p_dr.set_defaults(func=_cmd_drills)

    p_srv = sub.add_parser("serve")
    p_srv.add_argument("--host", default="0.0.0.0")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.add_argument("--open", action="store_true", help="open a browser tab on start")
    p_srv.add_argument("--no-refresh", action="store_true",
                       help="skip the automatic sync+analyse on startup")
    p_srv.set_defaults(func=_cmd_serve)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
