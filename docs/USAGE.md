# Using the coach

`coach.toml` (your usernames) and `coach.db` (your games) are **gitignored**, so they stay on your machine. All accounts are treated as one player by default.

**Easiest: build the Mac app once, then treat it like any other program.**

```bash
make app          # creates "Chess Coach.app" in the repo root
```

Double-click it, or drag it to your Dock or Applications folder. It opens with a
Dock icon and a menu bar; **Cmd-Q, the red close button and the Dock's Quit all
stop the server properly**, including the Stockfish processes — no stray
`uvicorn` left holding port 8000. The window shows the local URL and the LAN
address for your phone, and says so plainly if `coach.toml` or Stockfish is
missing rather than dying silently.

The bundle is a thin wrapper around this checkout rather than a frozen copy, so
edits take effect on the next launch and `make app` only needs rerunning if you
move the repository.

Alternatively, double-click **`start-coach.command`** in Finder (creates the venv
on first run, starts the app, opens a browser). Or from a terminal:

```bash
cp coach.example.toml coach.toml     # fill in usernames
python -m coach.cli serve --open     # web app on http://0.0.0.0:8000 — also reachable from your phone
```

### The daily hour

The app opens on **Today** with a single button. Everything else — target Elo,
opponent style, your colour, the opening, the time control, which puzzles, what
to review — is decided from your weakness profile, because it already knows.

    8 min   warm-up   due puzzles from your own games, graded automatically
    26 min  game      1-3 games at a clock chosen for today's weakness, coached
    10 min  review    your worst moments, handed back as positions to solve
   11 min   drill     a *second* weakness the game block couldn't reach
    5 min   close     one thing to remember, tomorrow's focus

Sessions are resumable: the plan, the block you're on and the time spent live in
`training_sessions`, so closing the laptop mid-game and reopening on your phone
picks up where you were.

**Time controls are part of the curriculum.** Tactics and openings are
recognition problems and want short games in volume; quiet positions and
positional play need a clock long enough to think; endgames need an increment.
The planner picks the control from the pillar being trained, trains a pillar at
the speed the profile says it is *actually* worst at, and reserves one session in
four as a slow day so the week spans blitz through rapid.

**Drills** come in two flavours, both scheduled by the same SRS. *Endgame
technique* is a fixed set of named positions (Lucena, Philidor, opposition,
square of the pawn, the basic mates) marked with a **goal** rather than a
solution: any move that keeps the win — or holds the draw — is accepted, because
a Lucena has several winning moves and failing one of them teaches the wrong
lesson. Play-out drills are played to the finish against the engine. *Repertoire*
lines are mined from your own games rather than a book: at each of your turns the
drill plays your own most-common move when it holds up and the engine's when it
doesn't, and branches on the replies you have actually faced.

**Coached games** offer exactly one takeback per game, and only for a genuine
blunder (≥200 cp): a correction while the position is still in front of you is
worth far more than the same point in a review you have to go and read.

Set `[startup] deep_pass = true` for a deeper background analysis pass using
spare compute.

**Where your time goes** is tracked alongside where your centipawns go. The clock
readings already in your PGNs give seconds-per-move; comparing that against
whether the position was critical (forcing, or with the top two moves far apart)
answers a question average centipawn loss cannot: *are you spending your time
where the game is decided?* Note the profile deliberately does **not** claim "rushing costs you
Xcp": you spend longer on positions you can already see are hard, so that
comparison runs backwards and is flagged as confounded rather than reported.

**Progress is measured as recurrence.** "Tactics avg cp-loss drifted 71 → 66" is
noisy and hard to feel. "You hung a piece 7.7 times per hundred decisions this
month against 2.6 back then" (illustrative numbers) is the same claim, legible, and directly
attributable to what you drilled.

CLI: `coach sync` · `coach analyze [--deep] [--refresh]` · `coach repair` ·
`coach profile` · `coach calibrate` · `coach puzzles mine` ·
`coach drills list|refresh` · `coach review <id>` · `coach games`.

`coach repair` recomputes everything derivable from stored evaluations —
cp-loss, the whole classification, per-move time — in about ten seconds and
without Stockfish. `coach analyze --refresh` re-runs the engine over games
already analysed; it is needed once, because cp-loss used to difference a
full-budget search against a quarter-budget one and those numbers are not
comparable with new ones. `coach calibrate` reports whether the sparring bot is
the strength it claims to be, measured against the players in the games database.

### First run

Analysis is the slow part — about 33 s of Stockfish per game at the default
budget. `max_games` defaults to 600 per site; with the 90-day recency weighting
in the profile, older games contribute almost nothing, so there is little value
in pulling more. Run one pass up front and the daily delta is a handful of games:

```bash
python -m coach.cli sync && python -m coach.cli analyze   # ~1 h for 500 games at workers=5
```
