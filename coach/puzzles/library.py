"""Import a slice of the Lichess puzzle database (CC0).

We do not download it automatically (it's large). Point this at a local copy:

    # https://database.lichess.org/#puzzles
    zstd -d lichess_db_puzzle.csv.zst          # -> lichess_db_puzzle.csv
    python -m coach.cli puzzles import-lichess lichess_db_puzzle.csv

Only puzzles within a rating band around the user and matching the user's weak
pillars are kept, so the local set stays small and relevant.
"""

from __future__ import annotations

import csv
from pathlib import Path

from coach.analysis.motifs import from_lichess_themes
from coach.puzzles.srs import ensure_card
from coach.store.db import Puzzle, session_factory

# Lichess theme tokens -> our five pillars.
_THEME_PILLAR = {
    "endgame": "endgame", "rookEndgame": "endgame", "pawnEndgame": "endgame",
    "queenEndgame": "endgame", "bishopEndgame": "endgame", "knightEndgame": "endgame",
    "queenRookEndgame": "endgame", "zugzwang": "endgame",
    "fork": "tactics", "pin": "tactics", "skewer": "tactics", "doubleCheck": "tactics",
    "discoveredAttack": "tactics", "sacrifice": "tactics", "deflection": "tactics",
    "attraction": "tactics", "clearance": "tactics", "interference": "tactics",
    "xRayAttack": "tactics", "hangingPiece": "tactics", "trappedPiece": "tactics",
    "backRankMate": "tactics", "mateIn1": "tactics", "mateIn2": "tactics",
    "mateIn3": "tactics", "capturingDefender": "tactics", "intermezzo": "tactics",
    "advancedPawn": "positional", "quietMove": "quiet", "defensiveMove": "quiet",
    "opening": "opening",
}


def _pillars_for(themes: str) -> set[str]:
    toks = themes.split()
    out = {_THEME_PILLAR[t] for t in toks if t in _THEME_PILLAR}
    return out or {"tactics"}


def import_lichess_csv(
    path: str | Path,
    user_elo: int = 1500,
    band: int = 300,
    want_pillars: set[str] | None = None,
    max_per_pillar: int = 400,
    db_path=None,
    want_motifs: set[str] | None = None,
    max_per_motif: int = 60,
) -> int:
    """Import a filtered slice. ``want_motifs`` (our tags) narrows the import
    to puzzles exercising those patterns, at most ``max_per_motif`` each - the
    way to give the variant scheduler something to draw on for the motifs your
    own games are thin on."""
    path = Path(path)
    kept_by_motif: dict[str, int] = {}
    lo, hi = user_elo - band, user_elo + band
    want = want_pillars or {"tactics", "endgame", "positional", "quiet", "opening"}
    kept_by_pillar: dict[str, int] = dict.fromkeys(want, 0)

    Session = session_factory(db_path)
    added = 0
    with path.open(newline="") as fh, Session() as session:
        reader = csv.DictReader(fh) if _has_header(path) else csv.reader(fh)
        for row in reader:
            if isinstance(row, dict):
                pid, fen, moves = row["PuzzleId"], row["FEN"], row["Moves"]
                rating, themes = int(row["Rating"]), row.get("Themes", "")
            else:
                pid, fen, moves, rating = row[0], row[1], row[2], int(row[3])
                themes = row[7] if len(row) > 7 else ""
            if not (lo <= rating <= hi):
                continue
            pillars = _pillars_for(themes) & want
            if not pillars:
                continue
            pillar = min(pillars)
            if kept_by_pillar.get(pillar, max_per_pillar) >= max_per_pillar:
                continue
            ours = from_lichess_themes(themes)
            if want_motifs is not None:
                hit = [t for t in ours if t in want_motifs
                       and kept_by_motif.get(t, 0) < max_per_motif]
                if not hit:
                    continue
                for t in hit:
                    kept_by_motif[t] = kept_by_motif.get(t, 0) + 1
            if session.query(Puzzle.id).filter_by(external_id=pid).first():
                continue

            puzzle = Puzzle(
                source="library",
                external_id=pid,
                fen=fen,
                solution=moves,           # already space-sep UCI; first move is opponent's
                pillar=pillar,
                # Our own tag vocabulary, so a library fork and a fork mined
                # from the player's games count as the same pattern.
                motif=",".join(ours[:6]) or None,
                opponent_first=True,
                rating_est=rating,
            )
            session.add(puzzle)
            session.flush()
            ensure_card(session, puzzle)
            kept_by_pillar[pillar] = kept_by_pillar.get(pillar, 0) + 1
            added += 1
        session.commit()
    return added


def weak_motifs(db_path=None, k: int = 8) -> set[str]:
    """The motifs worth importing for: lowest-rated in puzzles, then the most
    expensive in your games."""
    from coach.profile.weakness_model import latest_profile
    from coach.puzzles.srs import motif_summary

    out: list[str] = []
    with session_factory(db_path)() as session:
        out += [r["motif"] for r in motif_summary(session) if r["vs_overall"] < 0]
    prof = latest_profile(None) or {}
    out += [m["key"] for m in prof.get("tactics_motifs") or []]
    from coach.analysis.motifs import FROM_LICHESS

    allowed = set(FROM_LICHESS.values())
    return set([m for m in dict.fromkeys(out) if m in allowed][:k])


def _has_header(path: Path) -> bool:
    with path.open() as fh:
        first = fh.readline()
    return first.startswith("PuzzleId")
