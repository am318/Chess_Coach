# Chess — design & status

Self-contained reference for both halves of this repo. If you are picking this up
without the original chat, everything you need is here + the code.

---

## 1. What this repo contains

### A. Chessathon competition engine  (`engine/`, `submission/`)
A from-scratch chess engine packaged as `agent.zip` for
[aichessathon.com](https://aichessathon.com/#format).

Sandbox (from the competition docs, 2026):
- one file `agent.py` exposing `get_move(fen, time_left_ms) -> uci` at the zip root
- **1 CPU core, 2 GB RAM, 50 MB unzipped, Python 3.12, no network, no GPU**
- preinstalled ONLY: `torch 2.13.0+cpu`, `numpy`, `python-chess` (pkg name `chess`),
  `onnxruntime`, `numba`; plus `chess.polyglot` / `chess.syzygy`
- time control **120 s + 0.5 s/move**; 60 s init budget; process stays alive between
  moves (state persistence + pondering allowed)
- games start "from a curated position that is close to level" → an opening **book**
  is low value here; time-management + search depth is where the Elo is
- **no third-party engine binary in the zip**; training data may be engine-annotated

### B. Personal coaching bot  (`coach/`)
Local web app. Not sandboxed — uses Stockfish, network, a SQLite DB. Nothing in
`coach/` is ever bundled into `agent.zip`.

Learns from the user's **Lichess + Chess.com** games (treated as **one player** by
default), builds a weakness profile, then:
- spars adaptively at a target Elo, steering games into weak areas and offbeat lines;
- serves spaced-repetition puzzles mined from the user's own mistakes;
- reviews games (eval graph, key moments, plain-English notes);
- tracks progress over time.

**Decision — monorepo.** The coach depends on the engine (`coach/sparring` wraps
`engine/`), and the coach's Stockfish pipeline generates NNUE training data for the
engine (M6). `engine/` imports nothing from `coach/`, so a future `git filter-repo`
split is cheap. `submission/build.py` whitelists exactly `engine/` + `agent.py` and
`submission/smoke_test.py` runs the zip in a clean 3.12 venv, so stray imports fail
loudly before submission.

---

## 2. Status

| Milestone | State | Notes |
|-----------|-------|-------|
| **M0** scaffold, 3.12 uv env | ✅ | deps resolve to the exact sandbox versions |
| **M1** baseline engine | ✅ | ID negamax, TT, quiescence, null-move, LMR, PeSTO eval; perft + tactics tests; `agent.zip` builder + smoke test |
| **M2** engine hardening | ✅ | SEE ordering/pruning, aspiration windows, reverse-futility & mate-distance pruning, in-search repetition; SPRT self-play harness + curated openings; Polyglot hook |
| **M3** coach data pipeline | ✅ | Lichess + Chess.com + PGN ingest; Stockfish annotate; 5-pillar + attack/defence + punish classification; ranked weakness profile |
| **M4** web app + sparring | ✅ | FastAPI + dependency-free board UI; adaptive opponent (target-Elo sampling, weakness bias, provocative/gambit styles); weak-opening + breadth picker |
| **M5** puzzles + review + dashboard | ✅ | own-mistake puzzle miner (find-best / punish / defend), server-side solver, FSRS-ish SRS; game review; progress dashboard; auto sync+analyse on open; unified identity |
| **M6** guided daily session | ✅ | Stockfish-backed sparring root scorer; clocked + coached games; recency-weighted per-time-control profile; puzzle uniqueness gate + alternative acceptance; outcome-derived SRS grades + difficulty targeting; `Today` tab driven by `coach/session/` |
| **M7** drill sets | ✅ | `coach/drills/`: 11 verified endgame positions judged by goal (`adjudicate.py`), stateless play-out (`play.py`), repertoire lines mined from the user's own games (`repertoire.py`); both seeded into the puzzle table so they share the SRS |
| **M8** measurement fixes | ✅ | same-budget cp-loss; refutation lines; pillars routed by position not phase; decision-weighted averages; volume-resistant severity; time allocation; motif recurrence; puzzle difficulty split from ability; mate-aware puzzle gate; bot calibration report |
| **Openings** learn a named opening | ✅ | `coach/openings/`: curated catalogue of 10 openings (5 per side) as annotated movetext; study mode steps through each line with notes; drills are `source="opening"` puzzle rows on the shared SRS, graded strictly (no engine alternatives) and kept out of the puzzle rating; "in your games" shows where *you* first leave the book, sparring excluded |
| **M9** engine upgrade | ⬜ | numba bitboard movegen, NNUE eval trained on coach's Stockfish labels, SPSA tuning |
| later | ⬜ | `user_model` (P(move|pos) net + style metrics), LLM narration wired on, submission tablebases, Elo/style calibration from real play |

Engine strength today: ~club level (plain eval, no book/NNUE). Passes perft (Kiwipete
etc.), a tactics smoke suite, and the harness smoke test with zero flags; beats a
greedy 1-ply opponent 12–0 in self-play.

---

## 3. Weakness taxonomy

Every analysed user move is tagged (`coach/analysis/classify.py`), aggregated into a
JSON profile (`coach/profile/weakness_model.py`) consumed by sparring, puzzles and
the dashboard.

**Five pillars.** Routing is decided by *what the position was*, never by how
large the error turned out to be, and phase only gets a say once the position has
had one: `endgame phase → endgame`, else `hanging material or forcing-ness ≥ 0.5
→ tactics`, else `opening phase → opening`, else the forcing-ness bands split
quiet from positional. See the decisions log for why the obvious alternative
(route blunders to tactics) is not usable.
1. **Openings** — repertoire gaps, early deviations, worst-scoring lines/colours.
2. **Tactics** — cp-loss blunders by motif (fork/pin/skewer/back-rank/hanging piece/
   discovered attack/overload/trapped/removing-the-defender); also tactics *allowed*.
3. **Positional** — persistent small cp bleed with no tactic: bad piece, conceded
   file/outpost, pawn-structure damage, king-safety erosion.
4. **Endgames** — bucketed by type (K+P, rook, minor, opposite-bishops, queen,
   fortress); conversion vs holding tracked separately.
5. **Quiet positions ("no obvious move")** — cp-loss specifically on low-forcing-ness
   positions: drift, aimless moves, missed prophylaxis / plan changes. Needs
   candidate-move training, not tactics puzzles.

**Cross-cutting dimensions** (also emitted in the profile):
- **Attack vs defence** — `attack_defence_context()` compares pressure on each king;
  avg cp-loss when attacking vs defending is reported separately.
- **Punishing opponent errors** — when the opponent's last move handed the user ≥150 cp
  and the user's reply still lost ≥60 cp, it's flagged `punish_miss`; these become
  "punish the mistake" puzzles. Teaches reacting to and refuting ill-principled play.
- **Repertoire breadth** — per-colour Herfindahl concentration + effective number of
  openings; `narrow` flag when one opening is ≥60% or HHI ≥0.5. The sparring opening
  picker then leans on **breadth** mode (a line outside the user's usual repertoire).
- **Time trouble** — blunder rate with clock < 20 s vs otherwise.
- **Time allocation** — seconds spent per move (differenced from the `[%clk]`
  readings, plus increment) against whether the position was *critical*: forcing,
  or with the top two engine moves ≥120 cp apart. Reported as the ratio of
  average time on critical positions to average time on quiet ones. The
  cp-loss-by-time-spent comparison is computed but explicitly flagged as
  confounded and never phrased as a cost — see the decisions log.
- **Decision weight** — 0 for a forced move, ramping to 1 by four legal options.
  Every profile average is weighted by it, so the profile averages over decisions
  the player made rather than over moves that happened.

---

## 4. Architecture map

```
engine/                      SANDBOX-SAFE (chess, numpy, numba, onnxruntime + stdlib only)
  eval_classical.py          tapered PeSTO eval, side-to-move cp
  see.py                     static exchange evaluation (ordering + q-search pruning)
  search.py                  Searcher: ID negamax, TT, PVS, aspiration, null-move, RFP,
                             LMR, mate-distance pruning, quiescence; rank_root_moves()
  timeman.py                 think-time budget from time_left_ms alone + safety buffer
  book.py / tablebase.py     Polyglot / Syzygy probing, both absent-safe
  engine.py                  Engine: holds board+TT across moves, get_move()
  config.py                  EngineConfig, overridable from strategy.json in the zip

submission/
  agent.py                   competition entry point (lazy Engine, never raises)
  build.py                   assemble agent.zip; assert <50 MB, no native binaries
  smoke_test.py              replay the harness contract in a clean env

coach/
  config.py                  loads coach.toml -> CoachConfig (accounts, engine, startup)
  store/db.py                SQLite ORM: games, moves, puzzles, srs_cards, sync_state,
                             profile_snapshots
  ingest/                    lichess.py, chesscom.py, pgn_import.py, common.py (PGN->rows)
  analysis/
    annotate.py              parallel Stockfish pass -> per-move eval, cp-loss, best line,
                             refutation line, mate flags, punish_miss. EVAL_CLAMP_CP bounds
                             cp-loss so a missed mate can't score ~100000 and swamp every
                             average; the mate FLAGS read the raw scores instead.
    timing.py                seconds-spent per move from clock readings; criticality test
    repair.py                recompute derived fields from stored evals (no re-analysis);
                             version-gated re-classification
    classify.py              phase, forcing-ness, error bucket, pillar, motif, endgame
                             type, attack/defence context
  profile/
    weakness_model.py        build_profile() -> ranked JSON + snapshot; render_text();
                             latest_profile(); top_weak_pillar(); _repertoire();
                             _time_allocation(); one snapshot per day
    progress.py              pillar-trend + sparring-trend + motif-recurrence series
    calibration.py           human ACPL by rating band vs the bot's expected cp-loss at
                             each Strength.for_elo point
  drills/
    endgames.py              canonical technique positions: fen + goal + the one idea
    adjudicate.py            "does this still win / still draw?" - result, not a line
    play.py                  stateless endgame play-out vs the engine
    repertoire.py            mine opening lines from the user's own games
    seed.py                  load both sets into the puzzle table (shares the SRS)
  openings/
    catalog.py               named openings to learn on purpose: lines as annotated
                             movetext, one learner move per position per opening
    trainer.py               study plies, learn/forget, next drill, conflicts between
                             openings, and where the player leaves the book in real games
  session/
    timecontrol.py           pillar -> time control; profile-driven speed override
    plan.py                  build_plan() -> the daily block list; start/advance/resume
  sparring/
    backend.py               RootScorer: StockfishBackend (MultiPV over all legal
                             moves, ~50ms) | NativeBackend (own engine, no binary
                             needed); shared_backend() for one-off checks
    opponent.py              SparringOpponent: Strength.for_elo(), softmax move sampling,
                             _weakness_bias(), _provocation_score(), styles solid/
                             provocative/gambit
    opening_picker.py        pick_start(mode=weakness|breadth|mix); repertoire-aware
    openings_pool.py         curated ~level FENs (shared with tests/selfplay)
  puzzles/
    miner.py                 own-game puzzles: find-best | punish | defend
    library.py               import a slice of the Lichess puzzle DB (local file, CC0)
    srs.py                   FSRS-flavoured scheduler (stability/difficulty); due_queue()
    solve.py                 server-side attempt evaluation (no chess engine in browser)
  coaching/
    review.py                build_review(): eval graph, key moments, focus pillar
    llm.py                   Narrator protocol; NullNarrator (offline) + AnthropicNarrator
                             (opt-in via COACH_LLM + ANTHROPIC_API_KEY)
  desktop/
    launcher.py              Tk shell: uvicorn on a thread, Dock icon, Cmd-Q that
                             actually stops the server and its Stockfish children
    build_app.py             assemble "Chess Coach.app" (Info.plist + generated icns)
  server/app.py              FastAPI: REST + /ws/play websocket; background sync+analyse
  web/                       index.html + app.js (tabs: Today / Play / Puzzles / Openings /
                             Review / Stats)

tests/
  test_perft.py              movegen node counts
  test_tactics.py            search finds known mates/wins
  test_classify.py           phase / endgame / forcing-ness / buckets
  test_sparring.py           strength scaling + legal-move + bias direction
  test_srs.py                interval growth / lapse / weak-pillar ordering
  test_puzzle_solve.py       server-side attempt evaluation
  selfplay/                  match.py + sprt.py + run.py (A/B with live SPRT)
```

---

## 5. `coach.toml` reference   (gitignored — copy from `coach.example.toml`)

```toml
[accounts]
lichess  = ["name1", ...]      # your handles; empty list skips the site
chesscom = ["name2", ...]
unified  = true                # treat all accounts as ONE player (default).
                               # false -> separate profile/puzzles per account.

[startup]
auto_sync    = true            # on app open: pull new games in the background
auto_analyze = true            # ... then run Stockfish on the new ones
analyze_limit = 60             # cap games analysed per open (~1 min each)
mine_puzzles = true            # ... then refresh puzzles + profile
deep_pass    = false           # keep re-analysing analysed games at deep_* budget
                               # in the background with ONE worker (spare compute)

[lichess]
token  = ""                    # optional PAT, lifts rate limits
speeds = ["blitz","rapid","classical"]
max_games = 3000

[chesscom]
speeds = ["blitz","rapid","daily"]
max_games = 3000

[engine]
stockfish_path = "stockfish"
analysis_nodes = 300000        # base budget (~depth 18-22 with SF18)
analysis_depth = 18
multipv = 3
workers = 3                    # parallel Stockfish processes
deep_nodes = 2500000          # deep_pass budget (~depth 28)
deep_depth = 28
deep_multipv = 3

[profile]
inaccuracy_cp = 50
mistake_cp    = 120
blunder_cp    = 250
quiet_forcingness_max = 0.25   # below this a position counts as "quiet / no obvious move"
```

---

## 6. Commands & endpoints

```bash
# setup
brew install uv stockfish
make setup           # 3.12 venv + deps

# competition engine
make test            # perft + tactics + classify + sparring + srs + solve
make smoke           # fast simulated-clock self-play
make package         # build + verify agent.zip
python -m tests.selfplay.run --a engine --b greedy --games 40   # A/B with SPRT

# coaching bot
cp coach.example.toml coach.toml     # fill in usernames
./start-coach.command                 # double-clickable macOS launcher (venv + serve + open)
python -m coach.cli sync             # pull games
python -m coach.cli analyze [--limit N] [--deep] [--refresh]
                                     # --deep re-analyses at deep_* budget
                                     # --refresh re-analyses at the normal budget
python -m coach.cli repair [--force] [--prune-snapshots]   # no Stockfish, ~10s
python -m coach.cli calibrate [--positions N]              # is the bot its stated Elo?
python -m coach.cli profile [--account NAME] [--json]
python -m coach.cli puzzles mine | import-lichess FILE | stats
python -m coach.cli review GAME_ID [--llm] [--json]
python -m coach.cli games ; python -m coach.cli stats
python -m coach.cli serve [--open] [--no-refresh]    # web app on 0.0.0.0:8000
```

Web API: `/api/accounts`, `/api/profile`, `/api/dashboard`, `/api/games`,
`/api/review/{id}`, `POST /api/games/{id}/analyze`, `/api/puzzles/next`,
`POST /api/puzzles/{id}/attempt`, `POST /api/puzzles/{id}/review`,
`/api/openings`, `/api/openings/{key}`, `POST /api/openings/{key}/learn`,
`POST /api/openings/{key}/forget`, `/api/openings/{key}/next[?line=]`,
`/api/sync/status`, `POST /api/sync/run`, `WS /ws/play`.

### Sparring "auto" mode
- **Auto Elo** (default): seeds from the user's own rating in their game headers,
  then a running number in `settings` (`coach/profile/rating.py`) moves ±~48 after
  each game toward the level that yields ~50%. Shown as "Coach ~N (auto)".
- **Auto style**: mostly targets the current top weak pillar (tactics → provocative,
  everything else → solid, with the weakness *bias* and provocation doing the rest),
  and every 3rd game rotates solid → provocative → gambit for variety.
- Finished sparring games are stored **and auto-analysed in the background**; the
  Play tab then shows a compact post-match report (worst moves, played vs best SAN,
  focus pillar) with a link to the full review.

---

## 7. Design decisions

- **Server-side puzzle solving** (`solve.py`) — the browser has no chess engine and
  no external JS deps (sandbox-friendly habit + simpler). The client replays whatever
  position the server returns. The engine-backed alternative check is *injected*
  (`verifier=`), so the module stays pure and offline for tests.
- **Sparring runs on Stockfish, not the competition engine.** The bot needs every
  root move scored, and `rank_root_moves` searched each one with a full window and
  truncated on timeout — with the target Elo at 1500 it scored 11 of 27 moves in 4.06 s, so the
  target Elo, the weakness bias and the style were all applied to an arbitrary
  slice of the move list. `StockfishBackend` scores all of them in ~50 ms.
  `engine/` is untouched and still sandbox-pure; only `coach/` gained the
  dependency, which it already had for analysis.
- **Puzzles must be unambiguous.** A mined puzzle is only kept when the best move
  beats the second best by ≥80 cp (measured: the median candidate had a 49 cp gap,
  so ~80% of them could mark an equally good move "wrong"), and the solver
  additionally accepts any move within 50 cp at solve time.
- **Grades are derived, not asked.** Outcome plus solve time relative to your own
  pace on that card determines the SRS grade — the four buttons were up to forty
  decisions an hour that the data already answered.
- **Endgame drills are judged on the result, never on a stored line.** A Lucena
  has several winning moves and a Philidor several holding ones; marking one of
  them wrong repeats exactly the mistake the puzzle miner used to make. Every
  shipped position is verified against Stockfish in `tests/test_endgames.py`, so
  the set cannot make a false claim about a position.
- **The repertoire trainer is mined, not shipped.** A book teaches someone
  else's openings. With ~28 effective openings this user has no repertoire at
  all, so the drill reinforces their own most-played move wherever it holds up
  (< 50cp average loss) and substitutes the engine's only where it doesn't -
  consolidation rather than yet another new line.
- **The desktop app runs the server in-process, not as a child.** A shell-script
  wrapper is easy but quits badly: the window goes away and `uvicorn` keeps the
  port. Running uvicorn on a thread inside the Tk process means one exit path.
  Stockfish is the remaining hazard - it is a child *process* per analysis
  worker, sparring game and drill adjudication - so `sparring/backend.py` keeps a
  registry of every open engine and the launcher closes them on quit, on SIGTERM
  and via `atexit`. Verified: three engines open while running, zero after Quit.
- **The profile decays.** 90-day half-life on every cp-loss contribution, and a
  fixed 30-game trend window; otherwise a leak closed a year ago keeps ranking
  first on lifetime accumulated cost.
- **FSRS approximation** (`srs.py`) — stability/difficulty model, not the full 19-param
  FSRS-6. Isolated behind `review()` / `due_queue()` so it can be swapped for the
  `fsrs` package without touching callers.
- **Unified identity** — games keep their real `account`, but profile/puzzles/dashboard
  pass `account=None` (aggregate) by default. `[accounts] unified=false` restores
  per-account features and the account pickers in the UI.
- **Auto-refresh on open** — server `lifespan` + a staleness check in `GET /` kick a
  background `sync → analyse (capped) → mine → profile`; progress shows in the header
  badge and views refresh when it finishes.
- **LLM is opt-in and explanatory only** — `NullNarrator` gives deterministic offline
  phrasing; `AnthropicNarrator` (needs `COACH_LLM=1` + `ANTHROPIC_API_KEY`) only turns
  engine lines into prose, never picks moves.
- **Opening book de-prioritised for the competition** — curated near-level start
  positions make it low-value; effort goes to search depth + time management.

### M8 measurement decisions

- **cp-loss must difference two searches of the same size.** The played move's
  evaluation was reused from the MultiPV list when it appeared there and
  otherwise recomputed at a quarter of the nodes and depth 14 — so the *only*
  moves scored against a shallower search were the ones outside the top few,
  which is exactly the population that becomes the mistakes, the blunders and the
  puzzles. The mistake threshold was applied more harshly to the moves being
  judged than to the moves judging them. User moves now re-search the child at
  the full budget; opponent moves keep the cheap pass because they only draw the
  eval graph. Costs roughly one extra search per user move outside MultiPV.
  Existing rows are not comparable, hence `analyze --refresh`.

- **Pillar routing may never depend on the size of the error.** Routing on phase
  first meant a hung knight on move nine was an "Openings" problem, which
  prescribes theory for something theory does not fix. The obvious repair —
  send blunders to tactics — was tried and rejected on the data: it drove the
  other four pillars to a **0.0% blunder rate by construction** and made each
  pillar's average cp-loss conditional on its own outcome, destroying the
  cross-pillar comparison the ranking performs. Motifs are excluded for the same
  reason (they are only computed for moves that already went wrong). The honest
  substitute is `tactical_tension()`: does either side have material actually en
  prise, asked of the position alone. It fires on 16% of opening positions, 33%
  of middlegames and 24% of endgames, and every pillar keeps a distinct, real
  blunder rate (3.5% → 21.8%).

- **Severity is a geometric mean of cost share and rate share.** Ranking on cost
  share alone measures volume: the middlegame pillars see ten times the decisions
  the endgame pillar does and win the share almost regardless of how well they
  are played. Since `ranked_weaknesses[0]` drives the entire daily plan, that
  decided the curriculum. Blending with rate means a pillar must be both
  expensive in total and expensive per decision to come first; both components
  ship in the payload so the dashboard can say which one put it there.

- **Averages are over decisions, not moves.** A forced recapture contributes a
  cp-loss of zero to every average it lands in. `decision_w` ramps from 0 for a
  single legal move to 1 at four options and multiplies the recency weight.

- **The refutation is the lesson.** The analysis pass always evaluated the
  position after the played move and threw the variation away, keeping only the
  score. "Better was Nf3" is a fact to memorise; the line that punishes what you
  actually played is the thing you were asking about. Stored as
  `Move.refutation_line`, carried onto mined puzzles, shown in review and under a
  failed puzzle attempt. During a live attempt it is asked of the engine for the
  move *just played* rather than read off the puzzle, since the stored line
  refutes the move made in the original game.

- **Mate detection reads raw scores; statistics read clamped ones.** Clamping is
  right for averages and wrong as a gate. At +20 pawns a forced mate and a merely
  crushing move both clamp to +1500, cp-loss comes out 0, `mate_missed` (which
  required `cp_loss > 0`) never fired and the uniqueness gap collapsed to zero —
  so **no mate puzzle from a winning position could ever be mined**, which is
  most of them and the most instructive ones. Mate positions are now gated on
  whether a second line also mates, which is a question only the raw scores can
  answer.

- **Ability and difficulty are two numbers.** `puzzle_rating` measures the player
  (plain Elo, no target baked in); `selection_rating` chooses what to serve, at
  the Elo gap that predicts `TARGET_SUCCESS` — about 241 points below ability at
  0.80. They used to be one number pulled in two directions: the queue selected
  at the player's own rating (a coin flip by construction) while the update
  subtracted a constant on every review to drag it back down, which also fired
  when a puzzle had no rating at all. The difficulty band is now applied in SQL,
  because the candidate window is the most-overdue rows and once the backlog
  exceeded it the re-rank happened inside the same set of oldest cards — the
  queue silently degraded to "oldest first" exactly when the library grew big
  enough for targeting to matter.

- **Time allocation is a dimension, not a pillar.** It is a meta-skill with no
  drill of its own, and making it a sixth pillar would have meant entries in
  `PILLAR_TIME_CONTROLS`, `_DRILL_FOR` and `_SLOW_PILLARS` for something the game
  block cannot train. It reports the ratio of time spent on critical positions to
  time spent on quiet ones — a comparison between two kinds of *position*, so
  nothing the player chooses after seeing the board can bias it.

- **"Rushing costs you Xcp" is not a claim this data supports.** Time spent is
  chosen by the player, and they spend it on the positions they can already see
  are difficult; on real data the considered critical moves score *worse*
  (−184 cp against −100 cp). The comparison is kept in the payload as
  `rushed_vs_considered_cp` with a `confounded` note, and nothing downstream
  phrases it as a cost. The allocation ratio carries the finding instead.

- **Progress is recurrence.** Pillar cp-loss over snapshots is noisy, slow and
  hard to feel. Occurrences of each motif per hundred decisions, month by month,
  is the same claim in a form the player can act on — and it is a rate, because
  "fewer back-rank errors" means nothing if you also played a third as many
  games.

- **One profile snapshot per day.** `build_profile` runs on every app open, so
  four launches in an evening drew four points on a chart whose docstring assumed
  a weekly cadence and stored four copies of the whole profile.
  `prune_snapshots()` collapses existing history, keeping the last of each day.

- **The bot is calibrated against the humans in the games database.** Every game
  carries both players' ratings and the analysis pass now scores the opponents'
  moves too — at no extra engine cost, since both evaluations were already being
  computed — so the database is a sample of real play across a rating range. The
  bot's side is an exact expectation under its own sampling distribution rather
  than a simulation, which needs one search per position no matter how many
  rating points are checked and needs no games played. `move_distribution()` was
  split out of `choose()` so the measurement exercises the code that actually
  plays; a reimplementation would measure a second bot resembling the first and
  drift out of date. In the development corpus the first calibration run showed the
  bot too weak below 800, about right near 1000 and too strong near 1800, which
  is what the revised anchor points correct.

---

## 8. Remaining work

- **M6 engine**: numba bitboard movegen (biggest nps lever on 1 core), NNUE eval
  trained on `coach/analysis` Stockfish labels, SPSA/SPRT tuning. Keep classical eval
  as fallback/blend.
- **`coach/profile/user_model.py`**: small torch `P(move | position)` net on the
  user's games + style metrics (aggression, trade/push tendency, castling-side
  variety) → lets sparring anticipate the user and quantifies style drift / narrowness
  beyond openings.
- **Calibration**: `Strength.for_elo` points and the provocation/breadth probabilities
  are educated guesses; tune from real bot-vs-user results (auto-adjust target Elo).
- **Submission tablebases**: 3-4-5 Syzygy is too big for 50 MB; decide on a subset or
  ship without.
- **LLM review**: wire the narration toggle into the web UI.
