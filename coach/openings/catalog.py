"""A small, curated book of named openings to learn on purpose.

The repertoire trainer (``coach/drills/repertoire.py``) drills the openings you
already play. This is the other half: picking an opening you *don't* play yet -
or play by feel - and learning its main lines properly.

Each line is written as movetext with ``{comments}``, so python-chess parses it
(and rejects an illegal move the moment the catalogue loads) and the comments
become the study notes for the move they follow. Comments are kept to the moves
where something is actually being decided.

Two rules the test suite holds the catalogue to:

* every line is legal from the initial position;
* within one opening, the learner's move in any given position is the same in
  every line that reaches it. A drill marks every other move wrong, so two lines
  disagreeing about one position would mark the player wrong whichever they
  chose.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    key: str
    name: str
    pgn: str


@dataclass(frozen=True)
class Opening:
    key: str
    name: str
    side: str            # the side the learner plays: "white" | "black"
    eco: str
    # The position that means "this game is in this opening", as movetext. Used
    # to find the opening in the player's own games, so it should stop where the
    # opening is named rather than at the first move.
    defining: str
    summary: str
    plans: tuple[str, ...]
    lines: tuple[Line, ...]


CATALOG: tuple[Opening, ...] = (
    # ================================================================ WHITE
    Opening(
        key="italian",
        name="Italian Game",
        side="white",
        eco="C50",
        defining="1.e4 e5 2.Nf3 Nc6 3.Bc4",
        summary="Develop fast, aim the bishop at f7, and build slowly with c3 and d3 "
                "before opening the centre. Quiet on the surface, with a kingside "
                "attack underneath.",
        plans=(
            "c3 and d3 first: a pawn chain that keeps the centre closed until you want it open.",
            "The knight takes the long road b1-d2-f1-g3, heading for f5 or h5.",
            "Retreat the bishop to b3 before Black can trade it with ...Na5.",
        ),
        lines=(
            Line("pianissimo", "Giuoco Pianissimo", """
                1.e4 e5 2.Nf3 Nc6 3.Bc4 Bc5
                4.c3 {Preparing d4 one day, and giving the bishop a retreat square on c2.}
                Nf6 5.d3 {The slow plan: hold e4 and keep the centre closed.} d6
                6.O-O O-O 7.Re1 a6
                8.Bb3 {Out of reach of ...Na5 or ...b5 before Black can gain time on it.} Ba7
                9.Nbd2 {Next comes Nf1-g3: the knight's road to the kingside.}
            """),
            Line("two-knights", "Two Knights with 4.d3", """
                1.e4 e5 2.Nf3 Nc6 3.Bc4 Nf6
                4.d3 {Protects e4 calmly. 4.Ng5 is sharper, but leads to positions you
                have to know move by move.} Be7 5.O-O O-O 6.Re1 d6
                7.c3 {Same pawn structure as the Pianissimo, so the plans are the same too.}
            """),
            Line("two-knights-bc5", "Two Knights, ...Bc5", """
                1.e4 e5 2.Nf3 Nc6 3.Bc4 Nf6 4.d3 Bc5
                5.c3 {By a different order you are back in the Pianissimo.} d6
                6.O-O O-O 7.Re1 a6 8.Bb3 Ba7 9.Nbd2
            """),
            Line("blackburne", "Blackburne Shilling trap", """
                1.e4 e5 2.Nf3 Nc6 3.Bc4
                Nd4 {A trap rather than a developing move.}
                4.Nxd4 {Not 4.Nxe5? Qg5!, hitting the knight and g2 at once.} exd4
                5.O-O {Castle and play c3 or d3 next: Black's pawn on d4 is a liability.}
            """),
        ),
    ),
    Opening(
        key="ruy-lopez",
        name="Ruy Lopez",
        side="white",
        eco="C60",
        defining="1.e4 e5 2.Nf3 Nc6 3.Bb5",
        summary="Pressure the knight that defends e5, castle, and build the big centre "
                "with c3 and d4. The most deeply studied opening there is, and it "
                "rewards understanding over memory.",
        plans=(
            "The pin is indirect: the bishop leans on the knight that guards e5.",
            "c3 then d4 builds the ideal centre; h3 first stops ...Bg4 from pinning the Nf3.",
            "After ...b5 the bishop lives on b3, still aiming at f7.",
        ),
        lines=(
            Line("closed", "Closed main line", """
                1.e4 e5 2.Nf3 Nc6 3.Bb5 a6
                4.Ba4 {Keep the pin. 4.Bxc6 is a different opening, the Exchange.} Nf6
                5.O-O {e4 is "hanging", but taking it opens the e-file towards Black's own king.}
                Be7 6.Re1 b5 7.Bb3 d6
                8.c3 {Gives the bishop a retreat on c2 and prepares d4.} O-O
                9.h3 {Prevents ...Bg4 before playing d4.}
            """),
            Line("marshall", "Marshall Attack", """
                1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4 Nf6 5.O-O Be7 6.Re1 b5 7.Bb3 O-O
                8.c3 d5 {The Marshall: a pawn for a lasting attack on your king.}
                9.exd5 Nxd5 10.Nxe5 Nxe5 11.Rxe5 c6
                12.d4 {Control e5 and free the c1 bishop. ...Bd6, ...Qh4 and ...Qh3 are
                coming: hold the kingside first and count the pawns later.}
            """),
            Line("berlin", "Berlin Defence", """
                1.e4 e5 2.Nf3 Nc6 3.Bb5 Nf6 4.O-O
                Nxe4 5.d4 {Not trying to win the pawn back at once: open lines first.} Nd6
                6.Bxc6 dxc6 7.dxe5 Nf5
                8.Qxd8+ {The Berlin endgame. Black has the bishops, you have the
                kingside pawn majority.} Kxd8
                9.Nc3 {Develop and connect the rooks; Black's king has to find a home.}
            """),
            Line("open", "Open Ruy Lopez", """
                1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4 Nf6 5.O-O
                Nxe4 {Black takes the pawn while it can.}
                6.d4 {Open the centre while Black's king is still in it.} b5 7.Bb3 d5
                8.dxe5 Be6 9.c3 {A retreat square for the bishop, and a solid centre.}
            """),
        ),
    ),
    Opening(
        key="scotch",
        name="Scotch Game",
        side="white",
        eco="C44",
        defining="1.e4 e5 2.Nf3 Nc6 3.d4",
        summary="Strike in the centre on move three and trade the tension away. "
                "Fewer long theoretical lines than the Ruy Lopez, and easy "
                "development for your pieces.",
        plans=(
            "After the trade on d4 your knight sits in the centre and e4 gives you space.",
            "Against ...Nf6, e5 pushes the knight away and cramps Black.",
            "Develop with Be3, Nd2 or Nc3, and castle short.",
        ),
        lines=(
            Line("classical", "Classical, ...Bc5", """
                1.e4 e5 2.Nf3 Nc6 3.d4 exd4 4.Nxd4
                Bc5 {Black hits the knight straight away.}
                5.Be3 {Defend it with a developing move.} Qf6
                6.c3 {Now the knight on d4 is solidly protected.} Nge7
                7.Bc4
            """),
            Line("mieses", "Mieses, ...Nf6", """
                1.e4 e5 2.Nf3 Nc6 3.d4 exd4 4.Nxd4 Nf6
                5.Nxc6 bxc6 6.e5 {Gain space and push the knight out of f6.} Qe7
                7.Qe2 {Pins the e-pawn, so the knight has to move.} Nd5
                8.c4 Ba6 9.b3 {Holds c4 and gets ready for Bb2.}
            """),
        ),
    ),
    Opening(
        key="london",
        name="London System",
        side="white",
        eco="D00",
        defining="1.d4 d5 2.Bf4",
        summary="Get the dark-squared bishop out before e3 shuts it in, then build the "
                "same solid pyramid against almost anything. A system to learn once "
                "rather than a tree of variations.",
        plans=(
            "Bf4, e3, Nf3, c3, Nbd2, Bd3: the same set-up against most replies.",
            "Bg3 answers ...Bd6, so your bishop isn't simply traded off.",
            "The kingside attack comes from Ne5, Qf3 and sometimes g4.",
        ),
        lines=(
            Line("main", "Against ...d5 and ...c5", """
                1.d4 d5 2.Bf4 {Develop the bishop before e3 can shut it in.} Nf6
                3.e3 e6 4.Nf3 c5
                5.c3 {The base of the pyramid: d4 stays solid if Black trades on it.} Nc6
                6.Nbd2 Bd6
                7.Bg3 {Keep the bishop. If Black takes on g3, hxg3 gives you an open h-file.}
                O-O 8.Bd3
            """),
            Line("qb6", "Against early ...Qb6", """
                1.d4 d5 2.Bf4 c5 3.e3 Nc6 4.c3
                Qb6 {The London's main headache: b2 has lost the bishop that guarded it.}
                5.Qb3 {Meet queen with queen. If Black trades, your a-file opens up.}
            """),
            Line("kid", "Against a King's Indian set-up", """
                1.d4 Nf6 2.Bf4 g6 3.e3 Bg7 4.Nf3 O-O
                5.Be2 {Against ...g6 the bishop goes to e2: on d3 it would bite on the pawn on g6.} d6
                6.h3 {A retreat square on h2, so ...Nh5 doesn't win the bishop pair.}
            """),
        ),
    ),
    Opening(
        key="queens-gambit",
        name="Queen's Gambit",
        side="white",
        eco="D06",
        defining="1.d4 d5 2.c4",
        summary="Offer the c-pawn to pull Black's d-pawn away from the centre. Black "
                "can hold it, take it, or support it, and in each case you get "
                "pressure and space.",
        plans=(
            "The point of c4 is to take the d5 pawn away from the centre.",
            "Against a solid centre, Bg5 pins and Rc1 brings pressure down the c-file.",
            "If Black takes on c4, get it back calmly with e3 and Bxc4.",
        ),
        lines=(
            Line("orthodox", "Declined: Orthodox", """
                1.d4 d5 2.c4 e6 3.Nc3 Nf6
                4.Bg5 {Pins the knight that defends d5.} Be7 5.e3 O-O 6.Nf3 Nbd7
                7.Rc1 {The c-file is where your pressure will come from.} c6
                8.Bd3 dxc4 9.Bxc4 {You have spent no extra time getting the pawn back.}
            """),
            Line("accepted", "Accepted", """
                1.d4 d5 2.c4 dxc4 {Black takes, but can't hold the pawn.}
                3.Nf3 Nf6 4.e3 {Opens the bishop's diagonal: Bxc4 is coming.} e6
                5.Bxc4 c5 6.O-O {Castle, and you are left with more space.}
            """),
            Line("slav", "Slav", """
                1.d4 d5 2.c4 c6 {The Slav: support d5 and keep the light-squared bishop free.}
                3.Nf3 Nf6 4.Nc3 dxc4
                5.a4 {Stops ...b5, which would hold on to the pawn.} Bf5
                6.e3 e6 7.Bxc4 Bb4 8.O-O
            """),
            Line("albin", "Albin Countergambit", """
                1.d4 d5 2.c4 e5 {A counter-gambit.}
                3.dxe5 d4 4.Nf3 Nc6
                5.g3 {The bishop goes to g2, the best square against the d4 pawn,
                and the extra e5 pawn stays.}
            """),
        ),
    ),
    # ================================================================ BLACK
    Opening(
        key="caro-kann",
        name="Caro-Kann Defence",
        side="black",
        eco="B12",
        defining="1.e4 c6 2.d4 d5",
        summary="Challenge e4 with ...d5 after preparing it with ...c6, so the "
                "light-squared bishop gets out before ...e6. Solid, and sound "
                "against everything.",
        plans=(
            "...c6 and ...d5 attack e4 without blocking in the c8 bishop.",
            "Develop the bishop to f5 or g4 first, then play ...e6.",
            "In the Advance, ...c5 attacks White's centre from the side.",
        ),
        lines=(
            Line("classical", "Classical", """
                1.e4 c6 2.d4 d5 3.Nc3 dxe4 4.Nxe4
                Bf5 {The bishop comes out before ...e6 shuts it in.} 5.Ng3 Bg6 6.h4
                h6 {Keep a retreat on h7, so h5 can't trap the bishop.} 7.Nf3 Nd7
                8.h5 Bh7 9.Bd3 Bxd3 10.Qxd3 e6 {Solid. Castle long or short next.}
            """),
            Line("advance", "Advance", """
                1.e4 c6 2.d4 d5 3.e5 Bf5 {Develop outside the pawn chain before ...e6.}
                4.Nf3 e6 5.Be2
                c5 {Hit White's chain at its base, the d4 pawn.} 6.Be3 cxd4 7.Nxd4 Ne7
            """),
            Line("exchange", "Exchange", """
                1.e4 c6 2.d4 d5 3.exd5 cxd5 4.Bd3 Nc6 5.c3 Nf6 6.Bf4
                Bg4 {Develop the bishop before ...e6, with a gain of time.}
                7.Qb3 Qd7 {Defend b7 and keep developing.} 8.Nd2 e6
            """),
            Line("panov", "Panov Attack", """
                1.e4 c6 2.d4 d5 3.exd5 cxd5 4.c4 Nf6 5.Nc3 e6 6.Nf3 Be7
                7.cxd5 Nxd5 {Blockade d5. White's isolated pawn is the thing to play against.}
            """),
            Line("two-knights", "Two Knights", """
                1.e4 c6 2.Nc3 d5 3.Nf3
                Bg4 {Pin the knight, and be ready to give the bishop for it.}
                4.h3 Bxf3 5.Qxf3 e6 {Solid pawns on light squares, and easy development.}
            """),
        ),
    ),
    Opening(
        key="french",
        name="French Defence",
        side="black",
        eco="C00",
        defining="1.e4 e6 2.d4 d5",
        summary="Build a wall with ...e6 and ...d5, let White take space, then attack "
                "the pawn chain from the side with ...c5 and ...f6. Your "
                "light-squared bishop is the price you pay.",
        plans=(
            "When White plays e5, attack the base of the chain with ...c5.",
            "...Qb6 and ...Nc6 pile up on d4.",
            "...f6 is the second lever against the head of the chain.",
        ),
        lines=(
            Line("advance", "Advance", """
                1.e4 e6 2.d4 d5 3.e5 c5 {Strike at the base of the chain straight away.}
                4.c3 Nc6 5.Nf3 Qb6 {Three pieces now aim at d4.} 6.Bd3 cxd4 7.cxd4
                Bd7 {Not 7...Nxd4? 8.Nxd4 Qxd4 9.Bb5+, winning the queen.}
            """),
            Line("advance-a3", "Advance with 6.a3", """
                1.e4 e6 2.d4 d5 3.e5 c5 4.c3 Nc6 5.Nf3 Qb6
                6.a3 c4 {Lock the queenside and take space there. b2 is fixed.}
            """),
            Line("exchange", "Exchange", """
                1.e4 e6 2.d4 d5 3.exd5 exd5 {Symmetrical. Develop and the game is level.}
                4.Nf3 Nf6 5.Bd3 Bd6 6.O-O O-O
            """),
            Line("classical", "Classical, 4.e5", """
                1.e4 e6 2.d4 d5 3.Nc3 Nf6 4.e5 Nfd7 5.f4
                c5 {The usual lever against the base of the chain.} 6.Nf3 Nc6 7.Be3 cxd4
                8.Nxd4 Bc5 {Trade off White's best defender of the dark squares.} 9.Qd2 O-O
            """),
            Line("classical-bg5", "Classical, 4.Bg5", """
                1.e4 e6 2.d4 d5 3.Nc3 Nf6 4.Bg5 Be7 {Break the pin by developing.}
                5.e5 Nfd7 6.Bxe7 Qxe7 7.f4 O-O 8.Nf3 c5
            """),
            Line("tarrasch", "Tarrasch", """
                1.e4 e6 2.d4 d5 3.Nd2
                c5 {Against Nd2, open the centre at once; the knight can't press on d5.}
                4.exd5 Qxd5 5.Ngf3 cxd4 6.Bc4 Qd6 7.O-O Nf6 8.Nb3 Nc6 9.Nbxd4 a6
            """),
        ),
    ),
    Opening(
        key="scandinavian",
        name="Scandinavian Defence",
        side="black",
        eco="B01",
        defining="1.e4 d5",
        summary="Hit e4 on move one and take back with the queen. Very little theory; "
                "your queen comes out early, but to a safe square.",
        plans=(
            "...Qa5 keeps the queen out of reach and eyes the a5-e1 diagonal.",
            "...c6, ...Bf5 and ...e6 make a solid Caro-Kann-like set-up.",
            "Against Nf3 lines, ...Bg4, ...Nc6 and castling long put pressure on d4.",
        ),
        lines=(
            Line("main", "Main line, ...Qa5", """
                1.e4 d5 2.exd5 Qxd5 3.Nc3
                Qa5 {Out of reach. Once the d-pawn moves, the knight on c3 is pinned.}
                4.d4 Nf6 5.Nf3 c6 {A retreat for the queen, and support for ...Bf5.}
                6.Bc4 Bf5 7.Bd2 e6
            """),
            Line("nf3", "Against 3.Nf3", """
                1.e4 d5 2.exd5 Qxd5 3.Nf3 Bg4 4.Be2 Nc6 5.d4
                O-O-O {Everything now aims at d4.}
            """),
        ),
    ),
    Opening(
        key="qgd",
        name="Queen's Gambit Declined",
        side="black",
        eco="D30",
        defining="1.d4 d5 2.c4 e6",
        summary="Hold d5 with ...e6, develop quietly, castle, then free your game "
                "with exchanges. The most reliable answer to the Queen's Gambit.",
        plans=(
            "...e6 holds d5 and opens the diagonal for the dark-squared bishop.",
            "Free your game with trades, or with the ...c5 break.",
            "In the Exchange, ...Nbd7-f8-g6 brings the knight round to the kingside.",
        ),
        lines=(
            Line("tartakower", "Tartakower", """
                1.d4 d5 2.c4 e6 3.Nc3 Nf6 4.Bg5 Be7 5.e3 O-O 6.Nf3
                h6 {Ask the bishop now.} 7.Bh4
                b6 {The Tartakower: the light-squared bishop goes to b7.}
                8.cxd5 Nxd5 9.Bxe7 Qxe7 10.Nxd5 exd5 {Trades free your game.}
            """),
            Line("exchange", "Exchange", """
                1.d4 d5 2.c4 e6 3.Nc3 Nf6 4.cxd5 exd5 5.Bg5 c6 6.e3 Be7 7.Bd3 Nbd7
                8.Qc2 O-O 9.Nf3 Re8 10.O-O
                Nf8 {The knight heads for g6, where it guards the king and eyes f4 and e5.}
            """),
            Line("bf4", "With 5.Bf4", """
                1.d4 d5 2.c4 e6 3.Nf3 Nf6 4.Nc3 Be7 5.Bf4 O-O 6.e3
                c5 {The freeing break.} 7.dxc5 Bxc5
            """),
            Line("london", "Against the London", """
                1.d4 d5 2.Bf4 Nf6 3.e3
                c5 {Hit d4 before White's set-up is complete.} 4.c3 Nc6 5.Nd2 e6 6.Ngf3
                Bd6 {Offer to trade the bishop that White builds the London around.}
                7.Bg3 O-O
            """),
        ),
    ),
    Opening(
        key="kings-indian",
        name="King's Indian Defence",
        side="black",
        eco="E60",
        defining="1.d4 Nf6 2.c4 g6",
        summary="Let White build a big centre, then strike at it with ...e5 or ...c5. "
                "With a closed centre, both sides race: you on the kingside, White on "
                "the queenside.",
        plans=(
            "Fianchetto, castle, then ...e5 to fight for the centre.",
            "When White plays d5, the knight goes ...Ne7 and ...f5 follows.",
            "The Bg7 is your best piece: avoid trading it if you can.",
        ),
        lines=(
            Line("classical", "Classical", """
                1.d4 Nf6 2.c4 g6 3.Nc3 Bg7 4.e4 d6 5.Nf3 O-O 6.Be2
                e5 {The key break. If d4 takes on e5, the queens come off, and that's fine.}
                7.O-O Nc6 8.d5
                Ne7 {The Mar del Plata. Next comes ...Nd7 or ...Ne8, then ...f5.}
            """),
            Line("saemisch", "Sämisch", """
                1.d4 Nf6 2.c4 g6 3.Nc3 Bg7 4.e4 d6
                5.f3 {The Sämisch: e4 is solid, and g4 and h4 may follow.} O-O 6.Be3
                Nc6 7.Nge2 a6 8.Qd2 Rb8 {Prepare ...b5 to open the queenside.}
            """),
            Line("fianchetto", "Fianchetto", """
                1.d4 Nf6 2.c4 g6 3.Nf3 Bg7 4.g3 O-O 5.Bg2 d6 6.O-O Nbd7 7.Nc3
                e5 8.e4 c6 {Support ...d5 or ...b5 later, and give the queen b6.}
            """),
            Line("london", "Against the London", """
                1.d4 Nf6 2.Bf4 g6 3.e3 Bg7 4.Nf3 O-O 5.Be2 d6 6.O-O
                Nbd7 {Prepare ...e5. If it comes in with tempo, the Bf4 has to move.}
            """),
        ),
    ),
)

BY_KEY: dict[str, Opening] = {o.key: o for o in CATALOG}
