"""Canonical endgame technique positions.

Own-game puzzles can only teach what you have already met. Endgame technique is
the opposite kind of knowledge: a small, finite, named set of positions that
recur for the rest of your chess life, which you either know or don't. At club
level it is the cheapest rating there is, which is why it gets its own drill set
rather than waiting for the right endgame to turn up in a blitz game.

**These are judged on the result, not on a stored line.** In a Lucena there are
usually several moves that win, differing only in how fast; in a Philidor several
that hold. Marking one of them "wrong" because it wasn't the move in the book
teaches the player to distrust correct judgement - the same mistake the tactics
miner used to make. So a drill states a *goal* (win or draw) and any move that
preserves it is accepted.

Two modes, because endgames fail in two different ways:

* ``play-out`` - played against the engine to the finish. The skill is accuracy
  over twenty moves, and the engine's resistance is the examiner.
* ``key-move`` - the position turns on one idea; one move, adjudicated. **No
  shipped drill uses this any more**, and the reason is worth recording.

Every named position here used to be ``key-move``, and the mode does not work.
A key-move drill succeeds the moment the adjudicator says the result is intact,
and in a theoretically won or drawn ending almost every move leaves it intact.
Measured across all legal first moves, the Lucena passed 12 of 14 - including
``Rh1``, which does nothing at all - "rook behind the passed pawn" passed 16 of
18 *including* ``Rb1``, putting the rook in front of its own pawn, and the
Philidor passed 15 of 20.

Tightening the threshold cannot fix it, because centipawns do not encode
technique. In a tablebase-decided position the engine's score is a heuristic
about which winning method it prefers: at 600k nodes it rates the Lucena's
``Rd1+`` (473) above the bridge-building ``Rc4`` (462), and rates the Philidor's
``Rf6`` - the whole point of the defence - dead last of the drawing moves, at
-12. The only instruments that could grade a single move here are a tablebase
DTZ or a hand-curated list.

So the drills are played out instead. Converting a Lucena against a 200k-node
defender is the same skill, tested by something that cannot be fooled.

Every position is verified against Stockfish in ``tests/test_endgames.py``: the
FEN must be legal and the stated goal must match the engine's evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass

WIN, DRAW = "win", "draw"


@dataclass(frozen=True)
class EndgamePosition:
    key: str
    name: str
    fen: str
    goal: str          # WIN or DRAW, from the point of view of the side to move
    category: str      # basic-mate | king-pawn | rook | minor
    rating: int        # roughly the level at which a player needs to know this
    mode: str          # "key-move" | "play-out"
    idea: str          # the one sentence worth remembering


ENDGAMES: list[EndgamePosition] = [
    # ---------------------------------------------------------------- basic mates
    EndgamePosition(
        key="mate-kq", name="King and queen mate",
        fen="8/8/8/4k3/8/8/8/3QK3 w - - 0 1",
        goal=WIN, category="basic-mate", rating=600, mode="play-out",
        idea="Box the king in with the queen a knight's move away, walk your king up, "
             "mate on the edge. Watch for stalemate every single move.",
    ),
    EndgamePosition(
        key="mate-kr", name="King and rook mate",
        fen="8/8/8/4k3/8/8/8/R3K3 w - - 0 1",
        goal=WIN, category="basic-mate", rating=700, mode="play-out",
        idea="Cut the king off with the rook, bring your king up to oppose it, "
             "then shrink the box one rank at a time.",
    ),
    EndgamePosition(
        key="mate-two-bishops", name="Two bishops mate",
        fen="8/8/8/4k3/8/8/8/2B1KB2 w - - 0 1",
        goal=WIN, category="basic-mate", rating=1400, mode="play-out",
        idea="Bishops on adjacent diagonals build a wall. Push the king to a corner - "
             "either corner will do - with the king helping.",
    ),

    # ----------------------------------------------------------------- king + pawn
    EndgamePosition(
        key="king-first", name="King goes first",
        fen="3k4/8/3K4/3P4/8/8/8/8 w - - 0 1",
        goal=WIN, category="king-pawn", rating=1000, mode="play-out",
        idea="Do not push the pawn - pushing it here only draws. Step the king sideways "
             "to take the opposition, and the pawn follows later.",
    ),
    EndgamePosition(
        key="square-of-the-pawn", name="Square of the pawn",
        fen="8/8/5k2/8/8/8/P7/6K1 b - - 0 1",
        goal=DRAW, category="king-pawn", rating=800, mode="play-out",
        idea="Count the square the pawn makes with its queening square. If your king "
             "can step inside it, you catch the pawn without calculating.",
    ),
    EndgamePosition(
        key="defensive-opposition", name="Holding with the opposition",
        # Black to move: the drilling side is the one *without* the pawn, which
        # is the side the idea is about. It used to be White to move - a pawn up,
        # asked to draw - so the drill handed the player the attacker's chair,
        # every single legal move passed, and even losing the pawn succeeded
        # (K v K reads as "insufficient material - held"). It could not be failed.
        fen="8/8/8/4k3/4P3/4K3/8/8 b - - 0 1",
        goal=DRAW, category="king-pawn", rating=1300, mode="play-out",
        idea="Your king in front of the pawn, taking the opposition each time it "
             "advances, draws. Knowing this stops you liquidating into a lost pawn "
             "endgame.",
    ),

    # ----------------------------------------------------------------------- rook
    EndgamePosition(
        key="lucena", name="Lucena position",
        fen="1K1k4/1P6/8/8/8/8/r7/2R5 w - - 0 1",
        goal=WIN, category="rook", rating=1500, mode="play-out",
        idea="Building a bridge: rook to the fourth rank first, so it can shelter your "
             "king from the checks when it steps out.",
    ),
    EndgamePosition(
        key="philidor", name="Philidor defence",
        fen="8/8/4k3/8/R3P3/4K3/8/5r2 b - - 0 1",
        goal=DRAW, category="rook", rating=1500, mode="play-out",
        idea="Rook to your third rank to stop the enemy king crossing. Once the pawn "
             "advances, drop behind and check from the rear forever.",
    ),
    EndgamePosition(
        key="rook-behind-passer", name="Rook behind the passed pawn",
        fen="8/1k6/8/1P6/8/8/8/R5K1 w - - 0 1",
        goal=WIN, category="rook", rating=1400, mode="play-out",
        idea="Rooks belong behind passed pawns - yours and the opponent's. Behind the "
             "pawn the rook gains range as the pawn advances; in front it suffocates.",
    ),

    # ---------------------------------------------------------------------- minor
    EndgamePosition(
        key="wrong-bishop", name="Wrong rook pawn and bishop",
        fen="7k/8/6K1/7P/8/8/8/5B2 b - - 0 1",
        goal=DRAW, category="minor", rating=1500, mode="play-out",
        idea="The bishop doesn't cover the queening square, so the defender sits in the "
             "corner and cannot be evicted. A draw a bishop and a pawn down.",
    ),
    EndgamePosition(
        key="opposite-bishops-hold", name="Opposite-coloured bishops hold",
        # Black Bd6 is dark-squared, White Bb3 light-squared - genuinely opposite,
        # which the previous FEN was not: it had both bishops on dark squares, so
        # the drill's name, its category and every word of its idea described a
        # position it did not contain. Two pawns down, and 5 of 11 first moves
        # throw the draw away, so there is something real to get right.
        fen="5k2/8/3b1K2/6PP/8/1B6/8/8 b - - 0 1",
        goal=DRAW, category="minor", rating=1600, mode="play-out",
        idea="Blockade on the colour your own bishop controls. Opposite bishops turn "
             "extra pawns into nothing - the classic way to save a lost position.",
    ),
]

BY_KEY = {e.key: e for e in ENDGAMES}
CATEGORIES = sorted({e.category for e in ENDGAMES})


def for_categories(categories: list[str] | None = None) -> list[EndgamePosition]:
    if not categories:
        return list(ENDGAMES)
    wanted = set(categories)
    return [e for e in ENDGAMES if e.category in wanted]
