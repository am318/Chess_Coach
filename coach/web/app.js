"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const FILES = "abcdefgh";
const $ = (id) => document.getElementById(id);
// The Today tab tears its stage down between blocks, so any handler that
// outlives a block - the websocket's, the clock tick - can find its element
// gone. Writing through these keeps a stale handler from throwing and killing
// the callback that was supposed to run after it.
const setText = (id, txt) => { const e = $(id); if (e) e.textContent = txt; };
const setHTML = (id, html) => { const e = $(id); if (e) e.innerHTML = html; };
const PILLAR_LABEL = {
  tactics:"Tactics", endgame:"Endgames", opening:"Openings",
  positional:"Positional play", quiet:"Quiet positions",
};

let UNIFIED = false;
// Account to scope a request to. Under unified identity every feature aggregates
// across all configured accounts, so we send an empty value.
const acctFor = (id) => (UNIFIED ? "" : ($(id).value || ""));

function sqName(f, r) { return FILES[f] + (r + 1); }
function parseFenBoard(fen) {
  const rows = fen.split(" ")[0].split("/");
  const map = {};
  for (let r = 0; r < 8; r++) {
    let f = 0;
    for (const ch of rows[r]) {
      if (/\d/.test(ch)) { f += +ch; continue; }
      map[sqName(f, 7 - r)] = ch; f++;
    }
  }
  return map;
}
const turnOf = (fen) => (fen.split(" ")[1] === "w" ? "white" : "black");
const fmtClock = (s) => {
  if (s == null) return "—";
  const t = Math.max(0, s);
  const m = Math.floor(t / 60);
  const sec = Math.floor(t % 60);
  return t < 20 ? `${m}:${String(sec).padStart(2,"0")}.${Math.floor((t%1)*10)}`
                : `${m}:${String(sec).padStart(2,"0")}`;
};

/* Generic board widget: 64 cells built once, one delegated click handler,
 * renders by diffing (no innerHTML churn, no listener re-binding). */
function Board(el) {
  let fen = "8/8/8/8/8/8/8/8 w - - 0 1";
  let flipped = false, selected = null, dests = {}, last = [], hint = [], onMove = null;
  let check = null;   // square of the king in check, as the server reported it
  let ghost = null;   // optimistic {from,to} applied before the server confirms
  const cells = {};   // square name -> element

  el.classList.add("board");
  for (let i = 0; i < 64; i++) {
    const c = document.createElement("div");
    c.className = "sq";
    // Two layers under the piece: .mk carries the selection wash and the legal
    // move markers, the square's own background carries last-move/check/hint.
    // Keeping them apart is what lets a square be all of those at once.
    const mk = document.createElement("i");
    mk.className = "mk";
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "pc empty");
    svg.setAttribute("viewBox", "0 0 45 45");
    const use = document.createElementNS(SVG_NS, "use");
    svg.appendChild(use);
    c.appendChild(mk);
    c.appendChild(svg);
    c._svg = svg;
    c._use = use;
    el.appendChild(c);
  }
  el.addEventListener("click", (ev) => {
    const c = ev.target.closest(".sq");
    if (c && c.dataset.sq) click(c.dataset.sq);
  });

  function set(o) {
    if (o.fen !== undefined) { fen = o.fen; ghost = null; }
    if (o.flipped !== undefined) flipped = o.flipped;
    if (o.dests !== undefined) dests = o.dests || {};
    if (o.onMove !== undefined) onMove = o.onMove;
    if (o.last !== undefined) last = o.last ? [o.last.slice(0, 2), o.last.slice(2, 4)] : [];
    if (o.hint !== undefined) hint = o.hint ? [o.hint.slice(0, 2), o.hint.slice(2, 4)] : [];
    // A new position with nothing said about check has no check. Without this
    // the red king glow survived into the next position, which is worse than
    // never having drawn it.
    if (o.check !== undefined) check = o.check || null;
    else if (o.fen !== undefined) check = null;
    if (o.fen !== undefined || o.flipped !== undefined) selected = null;
    render();
  }

  function click(name) {
    if (!onMove) return;
    if (selected === null) {
      if (dests[name]) { selected = name; paint(); }
      return;
    }
    if (name === selected) { selected = null; paint(); return; }
    if ((dests[selected] || []).includes(name)) {
      const from = selected;
      selected = null;
      // Show the move immediately rather than waiting a round trip. The server's
      // next state message overwrites this, and corrects it if it was refused.
      ghost = { from, to: name };
      dests = {};
      render();
      onMove(from + name);
    } else if (dests[name]) {
      selected = name;
      paint();
    } else {
      selected = null;
      paint();
    }
  }

  // Full render: reposition cells for the current orientation + set pieces.
  function render() {
    const pieces = parseFenBoard(fen);
    if (ghost && pieces[ghost.from]) {
      pieces[ghost.to] = pieces[ghost.from];
      delete pieces[ghost.from];
    }
    const ranks = flipped ? [...Array(8).keys()] : [...Array(8).keys()].reverse();
    const files = flipped ? [...Array(8).keys()].reverse() : [...Array(8).keys()];
    let i = 0;
    for (const r of ranks) for (const f of files) {
      const name = sqName(f, r);
      const idx = i++;
      const cell = el.children[idx];
      cell.dataset.sq = name;
      cell._light = (f + r) % 2 === 1;
      const p = pieces[name] || "";
      if (cell._piece !== p) {
        cell._piece = p;
        if (p) {
          cell._use.setAttribute("href", "#pc-" + p);
          cell._svg.setAttribute("class", "pc");
        } else {
          cell._use.removeAttribute("href");
          cell._svg.setAttribute("class", "pc empty");
        }
      }
      // Coordinates belong to the edges of the board as displayed, so they
      // follow the flip rather than the square: rank down the left column,
      // file along the bottom row.
      if ((idx & 7) === 0) cell.dataset.crank = name[1];
      else delete cell.dataset.crank;
      if (idx >= 56) cell.dataset.cfile = name[0];
      else delete cell.dataset.cfile;
      cells[name] = cell;
    }
    paint();
  }

  // Cheap: only recompute class strings (highlights + selection).
  function paint() {
    const d = (selected && dests[selected]) || [];
    for (const name in cells) {
      const cell = cells[name];
      let cls = "sq " + (cell._light ? "light" : "dark");
      if (name === selected) cls += " sel";
      // A destination that already holds a piece is a capture, and gets a ring
      // round it instead of a dot the piece would sit on top of.
      if (d.includes(name)) cls += cell._piece ? " dest occupied" : " dest";
      if (last.includes(name)) cls += " last";
      if (hint.includes(name)) cls += " hint";
      if (name === check) cls += " check";
      if (cell.className !== cls) cell.className = cls;
    }
  }

  return { set, render, get fen() { return fen; } };
}

/* --------------------------------- tabs --------------------------------- */
// Cached payloads so switching tabs doesn't blank the view and refetch.
const cache = {};
function showView(name) {
  document.querySelectorAll("nav button").forEach((x) =>
    x.classList.toggle("active", x.dataset.view === name));
  document.querySelectorAll(".view").forEach((x) =>
    x.classList.toggle("active", x.id === name));
  if (name === "puzzles") loadPuzzle();
  if (name === "openings" && !OP.key) loadOpenings();
  if (name === "review") loadGames();
  if (name === "dashboard") loadDashboard();
  if (name === "today") loadToday();
}
// Which session/block the Today stage is currently showing. Re-entering the tab
// used to re-run the block from scratch: the game in progress was abandoned for
// a fresh one and half a warm-up's puzzles were forgotten.
let renderedBlock = null;
document.querySelectorAll("nav button").forEach((b) => {
  b.addEventListener("click", () => showView(b.dataset.view));
});

/* =============================== TODAY =============================== *
 * One button. Everything the old Play tab asked you to choose - Elo, style,
 * colour, opening, time control, which puzzles - is decided server-side from
 * the weakness profile. This controller just walks the block list.            */

let SESSION = null;      // {id, plan, block_index, block, done, progress}
let blockGameIds = [];   // games played inside the current block

async function loadToday() {
  const r = await fetch("/api/session/today").then((x) => x.json()).catch(() => null);
  if (!r) {
    if (!SESSION) setHTML("todayCard", "<p class='muted'>Could not load.</p>");
    return;
  }
  if (r.active) { SESSION = r; renderToday(); }
  else { SESSION = null; renderedBlock = null; renderPreview(r.preview); }
}

function planList(plan, idx, progress) {
  const done = progress || {};
  return `<ul class="plan">` + plan.blocks.map((b, i) => {
    const cls = i < idx ? "done" : i === idx ? "now" : "";
    const tick = i < idx ? "✓" : i === idx ? "▸" : "·";
    // A finished block shows what it actually took. Its budget is history by
    // then, and leaving it on screen made the list add up to more than the hour
    // once an early finish handed its minutes to the blocks after it.
    const secs = (done[i] || {}).seconds;
    const mins = (i < idx && secs != null)
      ? `${Math.max(1, Math.round(secs / 60))} min` : `${b.minutes} min`;
    return `<li class="${cls}"><span class="tick">${tick}</span>` +
      `<span class="mins">${mins}</span>` +
      `<span>${b.title}<span class="d">${b.detail}</span></span></li>`;
  }).join("") + `</ul>`;
}

function renderPreview(plan) {
  const focus = PILLAR_LABEL[plan.focus_pillar] || plan.focus_pillar;
  $("todayCard").innerHTML =
    `<h2>Today · ${plan.minutes} minutes</h2>` +
    `<p class="muted">Focus: <b style="color:#e8b96b">${focus}</b> · ` +
    `game at ${plan.time_control} · drill on ${plan.secondary_pillar}</p>` +
    planList(plan, -1) +
    `<div class="row" style="margin-top:12px">` +
    `<button id="startSession" class="primary big">Start today's session</button></div>` +
    plan.notes.map((n) => `<p class="muted" style="margin:6px 0 0">${n}</p>`).join("");
  $("startSession").addEventListener("click", async () => {
    $("startSession").disabled = true;
    SESSION = await fetch("/api/session/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes: plan.minutes }),
    }).then((x) => x.json());
    renderToday();
  });
  $("todayStage").innerHTML = "";
}

function renderToday() {
  const plan = SESSION.plan;
  const idx = SESSION.block_index;
  const pct = Math.round((Math.min(idx, plan.blocks.length) / plan.blocks.length) * 100);
  const focus = PILLAR_LABEL[plan.focus_pillar] || plan.focus_pillar;
  const el = SESSION.session_elapsed_s;
  const clock = el == null ? ""
    : `<span class="blockclock">${fmtMinSec(el)} / ${fmtMinSec(SESSION.session_budget_s)}</span>`;
  $("todayCard").innerHTML =
    `<h2>Today · ${focus} · ${plan.time_control}${clock}</h2>` +
    `<div class="progress"><i style="width:${pct}%"></i></div>` +
    planList(plan, idx, SESSION.progress);
  const key = `${SESSION.id}:${idx}:${SESSION.done ? "done" : "run"}`;
  const stage = $("todayStage");
  // Same block and it is still on screen: leave it alone. Re-running it is what
  // used to throw away a game in progress every time the tab was revisited.
  if (key === renderedBlock && stage && stage.children.length) return;
  renderedBlock = key;
  if (SESSION.done) return renderClose();
  renderBlock(SESSION.block);
}

function renderBlock(block) {
  blockGameIds = [];
  if (block.kind !== "game") {
    // Leaving the game block - by skipping it or finishing it. Drop the game
    // context so a later reconnect doesn't helpfully deal a game for a block
    // that has already been left behind, and forget the resume key with it:
    // a game abandoned by skipping is abandoned, and resuming it into the
    // review block's DOM would be worse than losing it.
    GAME_CTX = null;
    liveGame = false;
    lastOpts = null;
    rememberGameKey(null);
    stopClocks();
  }
  stopBlockClock();
  const stage = $("todayStage");
  stage.innerHTML = "";
  if (block.kind === "warmup") return startPuzzleBlock(block);
  if (block.kind === "drill") {
    if (block.params.drill === "endgame") return startEndgameBlock(block);
    return startPuzzleBlock(block);      // repertoire lines solve like puzzles
  }
  if (block.kind === "game") return startGameBlock(block);
  if (block.kind === "review") return startReviewBlock(block);
  if (block.kind === "close") return renderClose();
}

// Guards a double advance: the skip button and the end-of-game timer can both
// fire, and each one silently swallowed a whole block of the session.
let advancing = false;
async function advance(record) {
  if (advancing || !SESSION) return;
  advancing = true;
  try {
    const next = await fetch(`/api/session/${SESSION.id}/advance`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(record || {}),
    }).then((x) => x.json());
    if (next && next.error) throw new Error(next.error);
    SESSION = next;
    renderToday();
  } catch (e) {
    setHTML("todayStage",
      `<div class="card"><p class="muted">Couldn't move on: ${e.message}.</p>` +
      `<div class="row"><button id="advRetry" class="primary">Try again</button></div></div>`);
    const btn = $("advRetry");
    if (btn) btn.addEventListener("click", () => { renderedBlock = null; loadToday(); });
  } finally {
    advancing = false;
  }
}

/* ---- the block clock ---- *
 * The server owns the block's start time, so the budget survives a reload and a
 * move from desk to phone. It hands down seconds remaining; this just counts
 * that down locally and, at zero, lets the block finish at its next natural
 * boundary - between puzzles, between games. Nothing is ever cut off mid-move. */
let blockDeadline = null;    // performance.now() ms at which this block is due
let blockTimer = null;

function startBlockClock() {
  stopBlockClock();
  const left = SESSION && SESSION.block_seconds_left;
  blockDeadline = (left === undefined || left === null)
    ? null : performance.now() + left * 1000;
  blockTimer = setInterval(paintBlockClock, 1000);
  paintBlockClock();
}
function stopBlockClock() {
  if (blockTimer) { clearInterval(blockTimer); blockTimer = null; }
}
function blockSecondsLeft() {
  if (blockDeadline === null) return Infinity;
  return (blockDeadline - performance.now()) / 1000;
}
function blockTimeIsUp() { return blockSecondsLeft() <= 0; }

const fmtMinSec = (s) => {
  const t = Math.max(0, Math.round(s));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
};

function paintBlockClock() {
  const el = $("blockClock");
  if (!el) { stopBlockClock(); return; }
  const left = blockSecondsLeft();
  if (left === Infinity) { el.textContent = ""; return; }
  const budget = (SESSION && SESSION.block_budget_s) || 0;
  el.textContent = left >= 0
    ? `${fmtMinSec(budget - left)} / ${fmtMinSec(budget)}`
    : `${fmtMinSec(budget)} + ${fmtMinSec(-left)} over`;
  el.className = "blockclock" + (left <= 0 ? " over" : left <= 60 ? " low" : "");
}

// What the block running right now would report if it were left this second.
// Each block installs its own; the default covers the ones that earn nothing.
// The skip button used to send `{ skipped: true }` and nothing else, so two
// played games or five solved puzzles were simply forgotten - the block knew
// the count and told no one until it finished of its own accord.
let blockRecord = () => ({});

// Write that record through to the server now rather than when the block ends.
// Held in the tab alone it did not survive a skip, a reload or a closed lid -
// and for the game block that meant the review block, the part of the hour that
// turns a game into coaching, had nothing to review. `advance` and
// `note_progress` both merge, so nothing already recorded is lost.
function noteBlockProgress() {
  if (!SESSION) return;
  post(`/api/session/${SESSION.id}/progress`, blockRecord());
}

// `keepClock` is for a block that changes activity halfway through - the endgame
// drill running out of due positions and carrying on with puzzles. Restarting
// the clock there would hand the block a second full budget, because the server
// only reports seconds-remaining when the block is entered.
function blockShell(title, note, keepClock = false) {
  const stage = $("todayStage");
  blockRecord = () => ({});
  stage.innerHTML =
    `<div class="card"><h2>${title}<span id="blockClock" class="blockclock"></span></h2>` +
    (note ? `<p class="muted">${note}</p>` : "") +
    `<p class="muted" id="blockNote" hidden></p>` +
    `<div id="blockBody"></div>` +
    `<div class="row"><button id="skipBlock">Skip to next block →</button></div></div>`;
  $("skipBlock").addEventListener("click", () => advance({ ...blockRecord(), skipped: true }));
  if (keepClock) { stopBlockClock(); blockTimer = setInterval(paintBlockClock, 1000); paintBlockClock(); }
  else startBlockClock();
  return $("blockBody");
}

/* ---- Today: puzzle blocks (warm-up and drill) ---- */
let tpBoard = null, tpPuz = null, tpMoves = [], tpStart = 0, tpDone = 0, tpTarget = 10;
let tpHinted = false;
// Set after "Take back": the miss is filed, so the retry must not score or count.
let tpPractice = false;
// Set once the block has had to drop its pillar/source filter to find anything.
let tpWidened = false;
// The block being run, which is not always `SESSION.block`: a drill block whose
// own queue has run dry carries on here with a widened copy of itself.
let tpBlock = null;
// Anything the earlier half of the block earned, merged into the advance record
// so a handover doesn't lose the drills that were already done.
let tpCarry = {};

function startPuzzleBlock(block, keepClock = false, carry = {}) {
  const body = blockShell(block.title, block.detail, keepClock);
  tpWidened = false;
  tpBlock = block;
  // `keepClock` means this block is already running and is only changing
  // activity, so its tally is the one in hand. Otherwise pick up whatever the
  // server has: restarting the count after a reload and then writing the lower
  // number back is worse than never having counted.
  const sofar = keepClock ? {} : (SESSION.progress || {})[SESSION.block_index] || {};
  tpCarry = keepClock ? carry : (sofar.drills ? { drills: sofar.drills } : {});
  // The count is a backstop against a run of one-movers; the block's minutes are
  // the real limit, checked between puzzles so nothing is cut off mid-solve.
  tpTarget = block.params.max_puzzles || 10;
  tpDone = sofar.puzzles || 0;
  blockRecord = () => ({ ...tpCarry, puzzles: tpDone });
  body.innerHTML = `<div class="board" id="tpBoard"></div>
    <div class="status" id="tpStatus">Loading…</div>
    <div class="row"><button id="tpNext" class="primary" hidden>Next</button>
    <span class="muted" id="tpMeta"></span></div>`;
  tpBoard = Board($("tpBoard"));
  $("tpNext").addEventListener("click", nextTodayPuzzle);
  nextTodayPuzzle();
}

async function nextTodayPuzzle() {
  if (tpDone >= tpTarget || blockTimeIsUp()) {
    return advance({ ...blockRecord(), ended: blockTimeIsUp() ? "time" : "count" });
  }
  exReset();
  $("tpNext").hidden = true;
  tpHinted = false;
  tpPractice = false;
  const bp = (tpBlock || SESSION.block).params;
  const pillar = tpWidened ? "" : (bp.target_pillar || "");
  const src = tpWidened ? "" : (bp.sources || []).join(",");
  let r = await fetch(`/api/puzzles/next?pillar=${encodeURIComponent(pillar)}` +
                      `&sources=${encodeURIComponent(src)}`).then((x) => x.json());
  // A narrow queue running dry is not a reason to end a block with minutes left
  // on it - that is how an hour turns into twenty minutes. Drop the source and
  // pillar filters and keep going; the SRS ordering still picks what is due.
  if (!r.puzzle && !tpWidened && (pillar || src)) {
    tpWidened = true;
    r = await fetch("/api/puzzles/next").then((x) => x.json());
    // The block is still called "Drill: endgame" but is no longer serving
    // endgames. Saying so costs a line and stops the drill quietly becoming
    // something else while the heading insists otherwise.
    if (r.puzzle) {
      const note = $("blockNote");
      if (note) {
        note.textContent =
          "Nothing left due in this set — carrying on with whatever is due next.";
        note.hidden = false;
      }
    }
  }
  if (!r.puzzle) {
    $("tpStatus").textContent = "Nothing due — you are ahead of the schedule.";
    $("tpNext").hidden = false;
    $("tpNext").textContent = "Next block →";
    $("tpNext").onclick = () => advance(blockRecord());
    return;
  }
  tpPuz = r.puzzle; tpMoves = []; tpStart = Date.now();
  $("tpMeta").textContent = tpPuz.kind === "line"
    ? `${tpDone + 1}/${tpTarget} · ${tpPuz.name || "your line"}`
    : `${tpDone + 1}/${tpTarget} · ${tpPuz.pillar}` +
      `${tpPuz.motif ? " · " + tpPuz.motif : ""} · ~${tpPuz.rating} · you ${r.your_rating}`;
  stepTodayPuzzle(null);
}

async function stepTodayPuzzle(uci) {
  if (uci) tpMoves.push(uci);
  const res = await fetch(`/api/puzzles/${tpPuz.id}/attempt`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ moves: tpMoves, auto_first: true,
                           elapsed_ms: Date.now() - tpStart, hinted: tpHinted,
                           practice: tpPractice }),
  }).then((x) => x.json());

  const flip = res.user_colour === "black";
  if (res.status === "continue") {
    $("tpStatus").textContent = uci ? "✓ keep going…"
      : tpPractice ? "Try again — find the move."
      : tpPuz.kind === "line" ? "Play your line." : "Find the best move.";
    tpBoard.set({ fen: res.fen, flipped: flip, last: res.last_move,
                  check: res.check, dests: res.dests, onMove: stepTodayPuzzle });
    return;
  }
  // Resolved: the server has already filed the SRS review (unless practice).
  if (!tpPractice) {
    tpDone++;
    noteBlockProgress();
  }
  const g = res.graded || {};
  const finish = () => {
    $("tpNext").hidden = false;
    $("tpNext").textContent =
      (tpDone >= tpTarget || blockTimeIsUp()) ? "Next block →" : "Next";
    $("tpNext").onclick = nextTodayPuzzle;
  };
  if (res.status === "alternative") {
    $("tpStatus").textContent =
      `✓ ${res.played_san} — different from the line, but just as good.`;
  } else if (res.status === "solved") {
    $("tpStatus").textContent = res.practice
      ? "✓ Found it — practice, so the miss still stands."
      : `✓ Solved. (${g.label})`;
  } else {
    showMissedMove(tpBoard, $("tpStatus"), res, flip, finish, () => {
      tpMoves.pop();
      tpPractice = true;
      $("tpNext").hidden = true;
      stepTodayPuzzle(null);
    });
    finish();
    return;
  }
  tpBoard.set({ fen: res.fen, flipped: flip, last: res.last_move,
                hint: res.best_uci, check: res.check, dests: {}, onMove: null });
  // Solving it is not the end of what the position has to teach: the same
  // play-it-out and candidate-move doors are open on a correct answer.
  exOptions(tpBoard, $("tpStatus"), flip,
            { convert: res.fen, ideas: res.fen, colour: res.user_colour,
              lastMove: res.last_move });
  finish();
}

/* Naming the right move is half a lesson. The other half is the sequence that
 * punishes the wrong one, which is the thing the player was actually asking
 * about - so it goes directly under the status line, in the notation they read.
 * The element is reused rather than appended so repeated wrong answers in one
 * block don't stack up. */
function showRefutation(statusEl, res) {
  const parent = statusEl.parentElement;
  if (!parent) return;
  let el = parent.querySelector(".refutation");
  const line = res.refutation_san || [];
  if (!line.length) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement("p");
    el.className = "refutation muted";
    statusEl.insertAdjacentElement("afterend", el);
  }
  const moves = line.map((san, i) => (i % 2 === 0 ? `<b>${san}</b>` : san)).join(" ");
  el.innerHTML = `<span class="reflabel">what it runs into</span> ${moves}`;
}

/* A wrong answer used to snap the board back to the position before the move,
 * highlight the right square, and say "play it to continue" - with the board
 * locked, so you couldn't. Now the mistake is shown on the board, and the one
 * move that was right is the one move you can play, so the answer is something
 * you do rather than something you read. */
function showMissedMove(board, statusEl, res, flip, onPlayed, onRetry = null) {
  const best = res.best_san || res.best_uci;
  // An opening drill asks for one line, so a different move is "not the line",
  // not "doesn't work" - 3.Nc3 is a fine move, it just isn't the Ruy Lopez.
  const played = !res.played_san ? ""
    : res.book ? ` ${res.played_san} isn't the line.` : ` ${res.played_san} doesn't work.`;
  const ask = res.book ? `It goes ${best} — play it.` : `Best was ${best} — play it to see it.`;
  showRefutation(statusEl, res);
  if (!res.best_uci || !res.best_fen) {
    statusEl.textContent = res.book ? `✗ It goes ${best}.${played}` : `✗ Best was ${best}.${played}`;
    board.set({ fen: res.fen, flipped: flip, last: res.last_move,
                hint: res.best_uci, check: res.check, dests: {}, onMove: null });
    exOptions(board, statusEl, flip, {
      punish: res.fen_played, ideas: res.fen, colour: res.user_colour,
      retry: onRetry,
    });
    return;
  }
  statusEl.textContent = `✗${played} ${ask}`;
  const from = res.best_uci.slice(0, 2), to = res.best_uci.slice(2, 4);
  // Show what they actually played first, then hand back the position with only
  // the right move available.
  board.set({ fen: res.fen_played || res.fen, flipped: flip,
              last: res.fen_played ? res.played_uci : res.last_move,
              dests: {}, onMove: null });
  // Where the position ends up once the line has been played out: the same
  // three doors as before, opened from wherever the walk finished rather than
  // from the first move of it.
  const doors = (fen, lastMove) => exOptions(board, statusEl, flip, {
    convert: fen, punish: res.fen_played, ideas: res.fen,
    colour: res.user_colour, lastMove,
    retry: onRetry,
  });
  // Hand back the position with only the right move available. Wrapped in a
  // function because exploring and then coming back has to restore the *offer*
  // to play it, not just the picture of the position.
  const armBest = () => {
    statusEl.textContent = `✗${played} ${ask}`;
    board.set({ fen: res.fen, flipped: flip, last: res.last_move,
                hint: res.best_uci, check: res.check, dests: { [from]: [to] },
                onMove: () => {
                  statusEl.textContent = `${best} — that's the one.`;
                  board.set({ fen: res.best_fen, flipped: flip,
                              last: res.best_uci, hint: null, check: null,
                              dests: {}, onMove: null });
                  if (onPlayed) onPlayed();
                  // Playing the right move used to be where the puzzle stopped.
                  // Finding it is the easy half; the line it starts is the
                  // other one, so the rest of the solution is played out from
                  // here before the explore doors are offered.
                  const rest = res.continuation || [];
                  if (!rest.length) { doors(res.best_fen, res.best_uci); return; }
                  exReset();
                  walkLine(board, statusEl, flip, rest, doors);
                } });
  };
  setTimeout(() => {
    if (!document.body.contains(statusEl)) return;   // block moved on meanwhile
    armBest();
    exOptions(board, statusEl, flip, {
      punish: res.fen_played, ideas: res.fen, colour: res.user_colour,
      restore: armBest,
      retry: onRetry,
    });
  }, 900);
}

/* ---------------- Playing the answer on, along the puzzle's own line --------
 * Being handed the move is not the lesson; the lesson is the line it starts,
 * and nine in ten of the puzzles mined from this player's own games run three
 * plies or more. Until this existed the answer was given and the position taken
 * away in the same breath, so the half of a tactic that actually has to be
 * calculated - the follow-up, after the opponent's best try - was never seen.
 *
 * The whole continuation arrives with the wrong answer, so walking it asks the
 * server nothing: their reply is played, the next move of the line is yours to
 * find, and missing it just offers the move the way the first one was offered.
 * Nothing here writes to the SRS - the card was filed by the attempt. */
const LINE = { gen: 0 };

/* Abandon any walk in progress. Anything that takes the board over - the next
 * puzzle, an exploration - has to say so, or two things drive one board. */
function lineStop() { LINE.gen++; }

function walkLine(board, statusEl, flip, steps, onEnd) {
  const gen = ++LINE.gen;
  // A block can be advanced, or the next puzzle loaded, between two steps of
  // the walk. Both leave the timers armed, so every one of them checks that it
  // is still the current walk and that its status line is still on screen.
  const live = () => gen === LINE.gen && document.body.contains(statusEl);
  const later = (fn, ms) => setTimeout(() => { if (live()) fn(); }, ms);
  const show = (step, text) => {
    board.set({ fen: step.fen, flipped: flip, last: step.uci, check: step.check,
                hint: null, dests: {}, onMove: null });
    statusEl.textContent = text;
  };

  let i = 0;
  const done = (step, text) => later(() => {
    statusEl.textContent = text;
    onEnd(step.fen, step.uci);
  }, 550);

  // Their answer to the move just played, then hand the board back.
  const reply = () => {
    const step = steps[i++];
    show(step, `…${step.san}.`);
    if (i < steps.length) later(ask, 700);
    else done(step, `…${step.san} — and that's the line.`);
  };

  // The user's turn: full legal moves, and the line only continues on the
  // right one - being walked through it would make it a film, not a puzzle.
  const ask = () => {
    const want = steps[i];
    const prev = steps[i - 1];
    statusEl.textContent = "Your move — keep the line going.";
    board.set({ fen: prev.fen, flipped: flip, last: prev.uci, check: prev.check,
                hint: null, dests: prev.dests || {},
                onMove: (uci) => {
                  // The board sends four characters for a promotion; the stored
                  // line may carry five.
                  if (uci.slice(0, 4) === want.uci.slice(0, 4)) return played("✓ ");
                  offer(prev, want);
                } });
  };

  // Wrong again: show the move rather than the verdict, exactly as the first
  // one was shown, and carry on once it has been played.
  const offer = (prev, want) => {
    const from = want.uci.slice(0, 2), to = want.uci.slice(2, 4);
    statusEl.textContent = `Not this one — the line goes ${want.san}. Play it.`;
    board.set({ fen: prev.fen, flipped: flip, last: prev.uci, check: prev.check,
                hint: want.uci, dests: { [from]: [to] },
                onMove: () => played("") });
  };

  const played = (tick) => {
    const step = steps[i++];
    show(step, `${tick}${step.san}.`);
    if (i < steps.length) later(reply, 700);
    else done(step, `${tick}${step.san} — and that's the line.`);
  };

  later(reply, 750);
}

/* ---------------------- Explore: playing the position on --------------------
 * A puzzle that ends the moment you answer teaches the answer, not the
 * position. By the time any of this is reachable the SRS card is already filed
 * and nothing here writes to it: someone who suspects that poking at a position
 * might cost them a review will not poke at it, and poking at it is the point.
 *
 * Three doors, all onto the same stateless endpoints:
 *   punish  - play your own wrong move on, and have the refutation happen to
 *             you rather than be printed at you
 *   convert - play the right move on against best defence, which is where the
 *             difficult half of a tactic actually lives
 *   ideas   - the position's other strong moves, each one a door into convert
 */
const EX = { board: null, host: null, panel: null, st: null, mode: null,
             title: "", startFen: null, hold: null, line: [], restore: null,
             // Bumped on every new exploration and every move. A reply from a
             // request the user has already moved past is dropped rather than
             // painted over the top of what they asked for next.
             gen: 0 };

/* Engine calls take long enough that a failure has to be a message rather than
 * an exception: an exploration that dies silently leaves a dead board behind. */
async function post(url, body) {
  try {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (r.status === 404) {
      // The browser re-reads this file from disk on every load, but the server's
      // Python was imported when the process started. Leave the app running
      // across an update and you get the new buttons against the old routes -
      // which is a restart, not a bug, and the message should say so.
      return { status: "error",
               message: "this needs a newer server than the one running — "
                        + "restart the coach and try again" };
    }
    if (!r.ok) return { status: "error", message: `server said ${r.status}` };
    return await r.json();
  } catch (e) {
    return { status: "error", message: "lost contact with the coach" };
  }
}

/* Buttons go under the refutation line when there is one, so the reading order
 * stays: what you played, what it runs into, what you can do about it. */
const esc = (t) => String(t == null ? "" : t).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function exAnchor(statusEl) {
  const parent = statusEl.parentElement;
  return (parent && parent.querySelector(".refutation")) || statusEl;
}

function exOptions(board, statusEl, flip, o) {
  const parent = statusEl.parentElement;
  if (!parent) return;
  parent.querySelectorAll(".exrow.exmenu, .explore").forEach((x) => x.remove());

  // What to put back when the exploration is dismissed. A caller that left the
  // board in an interactive state passes its own restore, since replaying the
  // picture alone would silently take the move away from the user.
  const snap = { fen: board.fen, last: o.lastMove || null, text: statusEl.textContent };
  const restore = o.restore || (() => {
    board.set({ fen: snap.fen, flipped: flip, last: snap.last, hint: o.hint || null,
                check: null, dests: {}, onMove: null });
    statusEl.textContent = snap.text;
  });

  const row = document.createElement("div");
  row.className = "exrow exmenu";
  const add = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => {
      row.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      fn();
    });
    row.appendChild(b);
  };

  // Back to the position before the wrong move, to find it yourself this time.
  // The miss is already filed; the retry is practice and does not score.
  if (o.retry) {
    add("Take back", () => {
      exReset();
      const ref = parent.querySelector(".refutation");
      if (ref) ref.remove();
      o.retry();
    });
  }
  if (o.punish) {
    add("What mine ran into", () => exStart(board, row, {
      fen: o.punish, mode: "punish", colour: o.colour, flip, restore,
      title: "Playing your move on",
    }));
  }
  if (o.convert) {
    add("Play it out", () => exStart(board, row, {
      fen: o.convert, mode: "convert", colour: o.colour, flip, restore,
      title: "Now convert it",
    }));
  }
  if (o.ideas) {
    add("Other ideas", () => exIdeas(board, row, { fen: o.ideas, flip, restore }));
  }
  if (row.children.length) exAnchor(statusEl).insertAdjacentElement("afterend", row);
}

function exPanel() {
  if (EX.panel && document.body.contains(EX.panel)) return EX.panel;
  if (!EX.host || !document.body.contains(EX.host)) return null;   // block moved on
  const el = document.createElement("div");
  el.className = "explore";
  EX.host.insertAdjacentElement("afterend", el);
  EX.panel = el;
  return el;
}

/* Between puzzles: drop the panel *and* the menu, wherever they ended up. */
function exReset() {
  EX.gen++;
  lineStop();
  EX.panel = null; EX.st = null; EX.line = []; EX.restore = null; EX.host = null;
  document.querySelectorAll(".explore, .exrow.exmenu").forEach((x) => x.remove());
}

function exClose() {
  EX.gen++;
  if (EX.panel) EX.panel.remove();
  EX.panel = null;
  if (EX.host) EX.host.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
  const restore = EX.restore;
  EX.st = null; EX.line = []; EX.restore = null;
  if (restore) restore();
}

/* ±6 pawns fills the bar. Past that the number has stopped being informative
 * and the position has started being over. */
function exBar(cp) {
  if (cp == null) return "";
  const f = Math.max(-1, Math.min(1, cp / 600));
  const w = Math.abs(f) * 50;
  return `<div class="exbar"><i class="${f < 0 ? "neg" : ""}" ` +
         `style="left:${(f >= 0 ? 50 : 50 - w).toFixed(1)}%;width:${w.toFixed(1)}%"></i></div>`;
}

async function exStart(board, host, o) {
  lineStop();
  EX.board = board; EX.host = host; EX.restore = o.restore;
  EX.mode = o.mode; EX.title = o.title; EX.line = [];
  const gen = ++EX.gen;
  const p = exPanel();
  if (!p) return;
  p.innerHTML = `<h3>${esc(o.title)}</h3><p class="exmsg muted">thinking…</p>`;
  const st = await post("/api/explore/start",
                        { fen: o.fen, mode: o.mode, user_colour: o.colour });
  if (gen !== EX.gen) return;
  if (st.status === "error") { exFail(st.message); return; }
  EX.startFen = st.fen;
  EX.hold = st.hold_cp;
  if (st.reply_san) EX.line.push(st.reply_san);
  exRender(st, o.flip);
}

async function exMove(uci, flip) {
  const from = EX.st;
  const gen = ++EX.gen;
  const st = await post("/api/explore/step", {
    fen: from.fen, uci, mode: EX.mode, user_colour: from.user_colour,
    hold_cp: EX.hold, moves_played: from.moves_played, start_fen: EX.startFen,
  });
  if (gen !== EX.gen) return;
  // A refused move puts the position back rather than leaving a dead board:
  // the optimistic render has already shown a move the server did not take.
  if (st.status === "error") { exRender(from, flip); return; }
  if (st.played_san) EX.line.push(st.played_san);
  if (st.reply_san) EX.line.push(st.reply_san);
  exRender(st, flip);
}

function exFail(message) {
  const p = exPanel();
  if (!p) return;
  p.innerHTML = `<h3>${esc(EX.title || "Explore")}</h3>` +
    `<p class="exmsg lose">${esc(message || "that did not work")}</p>` +
    `<div class="exrow"><button class="exdone">Back to the puzzle</button></div>`;
  p.querySelector(".exdone").addEventListener("click", exClose);
}

function exRender(st, flip) {
  EX.st = st;
  if (!EX.panel || !document.body.contains(EX.panel)) return;  // dismissed mid-flight
  // `settled` means the point has landed but the position is not over: the
  // board stays live, because being told the verdict is the thing this whole
  // feature exists to stop doing.
  const going = st.status === "continue" || st.status === "settled";
  EX.board.set({ fen: st.fen, flipped: flip, last: st.last_move, check: st.check,
                 hint: null, dests: going ? st.dests : {},
                 onMove: going ? (uci) => exMove(uci, flip) : null });
  const tone = st.tone === "bad" ? " lose" : st.tone === "good" ? " win" : "";
  const p = EX.panel;
  p.innerHTML =
    `<h3>${esc(EX.title)}<span class="exeval">${esc(st.eval || "")}</span></h3>` +
    exBar(st.cp) +
    `<p class="exmsg${tone}">${esc(st.message || "")}</p>` +
    (EX.line.length ? `<p class="exmoves">${esc(EX.line.join(" "))}</p>` : "") +
    `<div class="exrow"><button class="exdone">` +
    `${going ? "Stop here" : "Back to the puzzle"}</button></div>`;
  p.querySelector(".exdone").addEventListener("click", exClose);
}

async function exIdeas(board, host, o) {
  lineStop();
  EX.board = board; EX.host = host; EX.restore = o.restore;
  EX.title = "Other ideas here";
  const gen = ++EX.gen;
  const head = "<h3>Other ideas here</h3>";
  const p = exPanel();
  if (!p) return;
  p.innerHTML = head + `<p class="exmsg muted">looking…</p>`;
  const r = await post("/api/explore/candidates", { fen: o.fen, top_n: 4 });
  if (gen !== EX.gen || !document.body.contains(p)) return;
  // A failed request is not the same as a position with no ideas in it, and
  // reporting "no engine" for a stale server sends you looking at Stockfish.
  if (r.status === "error") { exFail(r.message); return; }
  if (!r.moves || !r.moves.length) {
    p.innerHTML = head + `<p class="exmsg muted">No engine to ask.</p>`;
    return;
  }
  p.innerHTML = head + r.moves.map((m, i) =>
    `<div class="cand${i === 0 ? " top" : ""}" data-i="${i}">` +
    `<b>${esc(m.san)}</b><span class="cp">${esc(m.eval)}</span>` +
    `<span class="why">${esc([...m.motifs, m.note].filter(Boolean).join(" · "))}</span></div>`
  ).join("") +
  `<p class="exmoves">Pick one and play it out against the engine.</p>` +
  `<div class="exrow"><button class="exdone">Back to the puzzle</button></div>`;
  p.querySelector(".exdone").addEventListener("click", exClose);
  p.querySelectorAll(".cand").forEach((el) => el.addEventListener("click", () => {
    const m = r.moves[+el.dataset.i];
    exStart(board, host, { fen: m.fen_after, mode: "convert", colour: r.user_colour,
                           flip: o.flip, restore: o.restore,
                           title: `Playing out ${m.san}` });
  }));
}

/* ---- Today: the endgame drill block ---- *
 * Endgames are played out, not solved: the server adjudicates whether the result
 * was preserved, so any move that keeps the win (or the draw) is accepted.     */
let egBoard = null, egPuz = null, egFen = "", egMoves = 0, egStart = 0;
// A retry of a drill already graded: shown, played, and deliberately not scored.
let egPractice = false, egIdeaAfter = "";
let egDone = 0, egTarget = 4;

function startEndgameBlock(block, keepClock = false) {
  const body = blockShell(block.title, block.detail, keepClock);
  const sofar = keepClock ? {} : (SESSION.progress || {})[SESSION.block_index] || {};
  egTarget = block.params.max_drills || Math.max(2, Math.round(block.minutes / 3));
  egDone = sofar.drills || 0;
  blockRecord = () => ({ drills: egDone });
  body.innerHTML = `<div class="board" id="egBoard"></div>
    <div class="status" id="egStatus">Loading…</div>
    <p class="muted" id="egIdea"></p>
    <div class="row"><button id="egNext" class="primary" hidden>Next</button>
    <button id="egRetry" hidden>Try again</button>
    <span class="muted" id="egMeta"></span></div>`;
  egBoard = Board($("egBoard"));
  $("egNext").addEventListener("click", nextEndgame);
  // A retry is practice on a position you have already been graded on. It does
  // not score (the server skips the SRS review) and it does not count again
  // toward the block's tally - counting it did, so one drill ate two slots.
  $("egRetry").addEventListener("click", () => loadEndgame(egPuz, true));
  nextEndgame();
}

async function nextEndgame() {
  if (egDone >= egTarget || blockTimeIsUp()) {
    return advance({ ...blockRecord(), ended: blockTimeIsUp() ? "time" : "count" });
  }
  $("egNext").hidden = true; $("egRetry").hidden = true;
  const r = await fetch("/api/puzzles/next?sources=endgame").then((x) => x.json());
  if (!r.puzzle) {
    // Eleven named endgames, scheduled by the same SRS as everything else, so
    // the set runs dry long before the block's minutes do. Spend the rest on
    // endgame positions from the user's own games rather than ending here.
    const rest = { ...SESSION.block,
      title: SESSION.block.title + " — from your games",
      detail: "No named endgame due; these are endgame positions you actually reached.",
      params: { ...SESSION.block.params, sources: ["own-game"], drill: "puzzles" } };
    return startPuzzleBlock(rest, true, { drills: egDone });
  }
  loadEndgame(r.puzzle);
}

async function loadEndgame(puz, practice = false) {
  egPuz = puz;
  egPractice = practice;
  $("egNext").hidden = true; $("egRetry").hidden = true;
  const st = await fetch(`/api/drills/endgame/${puz.id}/start`, { method: "POST" })
    .then((x) => x.json());
  egFen = st.fen; egMoves = 0; egStart = Date.now();
  egIdeaAfter = st.idea_after || "";
  const goalWord = st.goal === "win" ? "Win this." : "Hold the draw.";
  const modeWord = st.mode === "key-move" ? "One move." : "Play it out against the engine.";
  $("egStatus").textContent =
    (practice ? "Practice — this one doesn't count. " : "") +
    `${st.name} — ${goalWord} ${modeWord}`;
  // A play-out drill's idea is a reminder for the moves ahead; a key-move
  // drill's idea names the move, so the server holds it back as `idea_after`.
  $("egIdea").textContent = st.idea || "";
  $("egMeta").textContent = practice
    ? `${egDone}/${egTarget} drills · retry`
    : `${egDone + 1}/${egTarget} drills`;
  egBoard.set({ fen: st.fen, flipped: st.user_colour === "black",
                dests: st.dests, onMove: stepEndgame });
}

async function stepEndgame(uci) {
  const res = await fetch(`/api/drills/endgame/${egPuz.id}/move`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fen: egFen, uci, moves_played: egMoves,
                           elapsed_ms: Date.now() - egStart,
                           practice: egPractice }),
  }).then((x) => x.json());

  if (res.status === "error") {
    $("egStatus").textContent = "⚠ " + res.message;
    return;
  }
  egFen = res.fen; egMoves = res.moves_played;
  const flip = res.user_colour === "black";
  if (res.status === "continue") {
    $("egStatus").textContent = `${res.played_san} — keep going. (${egMoves} moves)`;
    egBoard.set({ fen: res.fen, flipped: flip, last: res.last_move,
                  dests: res.dests, onMove: stepEndgame });
    return;
  }
  if (!egPractice) { egDone++; noteBlockProgress(); }
  const ok = res.status === "success";
  $("egStatus").textContent = (ok ? "✓ " : "✗ ") + res.message +
    (egPractice ? "  (practice — not scored)" : "");
  if (egIdeaAfter) $("egIdea").textContent = egIdeaAfter;
  egBoard.set({ fen: res.fen, flipped: flip, last: res.last_move,
                dests: {}, onMove: null });
  $("egRetry").hidden = ok;
  $("egNext").hidden = false;
  $("egNext").textContent =
    (egDone >= egTarget || blockTimeIsUp()) ? "Next block →" : "Next drill";
  $("egNext").onclick = nextEndgame;
}

/* ---- Today: the game block ---- *
 * The plan's game count is a target, not a cap. It was computed from an estimate
 * of how long a game takes; the block plays on while it still has room for
 * another, because stopping at the count is what left twenty minutes of a
 * twenty-six-minute block unused and turned the hour into twenty minutes. */
let blockGameStart = 0;      // performance.now() when the current game began
let blockGameSecs = [];      // how long each game in this block actually took
const MAX_BLOCK_GAMES = 12;  // guard: a game that ends instantly must not loop

function startGameBlock(block) {
  const p = block.params;
  const body = blockShell(block.title, block.detail);
  // Games this block has already got through, as the *server* has them. Held
  // only in the tab they did not survive a reload or a skip, and the review
  // block - the part of the hour that turns a game into coaching - then had
  // nothing to review after two games had just been played.
  const sofar = (SESSION.progress || {})[SESSION.block_index] || {};
  blockGameIds = (sofar.game_ids || []).slice();
  blockGameSecs = (sofar.game_secs || []).slice();
  blockGameStart = performance.now();
  // How long each game took is part of the record: it is what decides whether
  // the block has room for another, so a reload has to get it back too.
  blockRecord = () => ({ game_ids: blockGameIds.slice(),
                         games: blockGameSecs.length,
                         game_secs: blockGameSecs.map((x) => Math.round(x)) });
  body.innerHTML = `<div class="board" id="tgBoard"></div>
    <div class="clocks" id="tgClocks"><div class="clk" id="tgOpp"><small>coach</small><span>—</span></div>
    <div class="clk" id="tgYou"><small>you</small><span>—</span></div></div>
    <div id="tgWarn"></div>
    <div class="status" id="tgStatus">Starting…</div>
    <div class="row"><button id="tgResign">Resign</button>
    <span class="muted" id="tgCount"></span></div>`;
  playBoard = Board($("tgBoard"));
  PLAY_UI = { board: playBoard, status: "tgStatus", clocks: "tgClocks",
              opp: "tgOpp", you: "tgYou", warn: "tgWarn" };
  $("tgResign").addEventListener("click", () => wsSend({ type: "resign" }));
  gamesLeft = p.games || 1;
  paintGameCount();
  // The first game is why the block exists, so it is always played, however
  // little time is left. Later games have to fit.

  const opts = { base_s: p.base_s, inc_s: p.inc_s, coached: p.coached,
                 session_id: SESSION.id, style: "auto", elo: "auto", auto_elo: true };
  const ctx = { sessionId: SESSION.id, blockIndex: SESSION.block_index };
  if (savedGameKey()) {
    // A reload landing back in the game block. The socket's `onopen` is already
    // resuming that game; dealing a new one here would replace the position the
    // user was looking at a second ago, which is the bug this is fixing.
    GAME_CTX = ctx;
    lastOpts = opts;
    setText("tgStatus", "Reconnecting to your game…");
  } else {
    newGame(opts, ctx);
  }
}

/* The planned count is what the hour was budgeted from, so it is worth showing
 * even though the block actually stops on its clock. One format throughout: the
 * old line switched from "about 6 planned" to a copy of the block clock after
 * the first game, and then went stale because nothing repainted it. */
function paintGameCount(finished) {
  const el = $("tgCount");
  if (!el) return;
  const planned = ((SESSION && SESSION.block && SESSION.block.params) || {}).games || 1;
  const done = blockGameSecs.length;
  el.textContent = finished
    ? `${done} game${done === 1 ? "" : "s"} played`
    : `game ${done + 1} of ~${planned}`;
}

/* ---- Today: the review block ---- *
 * Stockfish needs the better part of a minute on a real game, and this block
 * starts a second and a half after the last move. Asking once and giving up -
 * which is what it used to do - meant the review block, the part of the hour
 * that turns the game into coaching, was almost always empty. So it waits.  */
const REVIEW_POLL_MS = 4000;
const REVIEW_WAIT_MS = 180000;
let reviewPoll = null;
let onReportReady = null;

async function startReviewBlock(block) {
  const body = blockShell(block.title, block.detail);
  const sessionId = SESSION.id;
  const startedAt = Date.now();
  let attempts = 0;

  const stop = () => {
    if (reviewPoll) { clearTimeout(reviewPoll); reviewPoll = null; }
    onReportReady = null;
  };
  stop();
  // The socket pushes a report the moment a game's analysis lands; take that as
  // a cue to look again immediately instead of waiting out the poll interval.
  onReportReady = () => { if (reviewPoll) { clearTimeout(reviewPoll); reviewPoll = null; } tick(); };

  async function tick() {
    if (!SESSION || SESSION.id !== sessionId
        || !SESSION.block || SESSION.block.kind !== "review") return stop();
    if (!document.body.contains(body)) return stop();
    attempts++;
    let r;
    try {
      r = await fetch(`/api/session/${sessionId}/review-moments`).then((x) => x.json());
    } catch (e) {
      r = null;
    }
    const games = ((r && r.games) || []).filter((g) => !g.pending);
    if (games.length) { stop(); return renderReviewMoments(body, games); }
    const waited = Math.round((Date.now() - startedAt) / 1000);
    const pending = ((r && r.games) || []).length;
    if (!pending) {
      body.innerHTML = "<p class='muted'>No games from this session to review — " +
        "skip on to the drill.</p>";
      return stop();
    }
    if (Date.now() - startedAt > REVIEW_WAIT_MS) {
      body.innerHTML = "<p class='muted'>Analysis is taking longer than usual. " +
        "It'll be waiting in the Review tab.</p>" +
        `<div class="row"><button id="trWait" class="primary">Keep waiting</button></div>`;
      const b = $("trWait");
      if (b) b.addEventListener("click", () => { b.disabled = true; startReviewBlock(block); });
      return stop();
    }
    body.innerHTML =
      `<p class="muted">Analysing ${pending} game${pending === 1 ? "" : "s"} with Stockfish` +
      `${".".repeat(1 + (attempts % 3))}</p>` +
      `<p class="muted">${waited}s — it usually takes under a minute per game.</p>`;
    reviewPoll = setTimeout(tick, REVIEW_POLL_MS);
  }
  body.innerHTML = "<p class='muted'>Fetching your game analysis…</p>";
  tick();
}

/* "Better: Nf3" tells you what to memorise. The line after the move you played
 * tells you why - so both go on every key moment. */
function refutationHTML(m) {
  const line = m.refutation_san || [];
  if (!line.length) return "";
  return `<span class="muted">yours ran into</span> ` +
         line.map((san, i) => (i % 2 === 0 ? `<b>${san}</b>` : san)).join(" ") + `<br>`;
}

function renderReviewMoments(body, games) {
  const withMoments = games.filter((g) => (g.moments || []).length);
  if (!withMoments.length) {
    // A clean game is a real outcome, not an error - and an empty board with a
    // "biggest leak —" line under it read like a bug.
    body.innerHTML =
      `<p class="muted">Nothing worth flagging in ${games.length} ` +
      `game${games.length === 1 ? "" : "s"} — no mistake big enough to drill.</p>` +
      `<div class="row"><button id="trDone" class="primary big">On to the drill</button></div>`;
    $("trDone").addEventListener("click", () => advance({ reviewed: games.length }));
    return;
  }
  body.innerHTML = `<div class="board" id="trBoard"></div><div id="trList"></div>`;
  const rb = Board($("trBoard"));
  const list = $("trList");
  for (const g of withMoments) {
    list.insertAdjacentHTML("beforeend",
      `<p class="muted" style="margin:8px 0 2px">Game #${g.game_id} — acpl ${g.acpl}` +
      `, biggest leak ${g.focus_pillar || "—"}</p>`);
    for (const m of g.moments) {
      const d = document.createElement("div");
      d.className = "moment";
      d.innerHTML = `<b>${m.move_number}${m.side === "white" ? "." : "…"} ${m.played_san}</b>` +
        ` −${m.cp_loss}cp · ${m.pillar}${m.motif ? " · " + m.motif : ""}<br>` +
        (m.best_san ? `better: <b>${m.best_san}</b> ${(m.best_line_san||[]).slice(1,4).join(" ")}<br>` : "") +
        refutationHTML(m) +
        (m.note ? `<span class="muted">${m.note}</span>` : "");
      d.addEventListener("click", () =>
        rb.set({ fen: m.fen, flipped: m.side === "black", hint: m.best_uci,
                 last: m.played_uci, dests: {}, onMove: null }));
      list.appendChild(d);
    }
    if (g.moments[0]) {
      const m = g.moments[0];
      rb.set({ fen: m.fen, flipped: m.side === "black", dests: {}, onMove: null });
    }
  }
  list.insertAdjacentHTML("beforeend",
    `<div class="row"><button id="trDone" class="primary big">Got it — next block</button></div>`);
  $("trDone").addEventListener("click", () => advance({ reviewed: withMoments.length }));
}

/* ---- Today: closing block ---- */
function renderClose() {
  stopBlockClock();
  const plan = SESSION.plan;
  const prog = SESSION.progress || {};
  const puzzles = Object.values(prog).reduce((a, b) => a + (b.puzzles || 0), 0);
  const games = Object.values(prog).reduce((a, b) => a + (b.game_ids || []).length, 0);
  const drills = Object.values(prog).reduce((a, b) => a + (b.drills || 0), 0);
  // What the hour actually cost, not what it was planned to.
  const spent = Object.values(prog).reduce((a, b) => a + (b.seconds || 0), 0);
  const planned = plan.minutes * 60;
  const timing = spent
    ? `<p class="muted">${fmtMinSec(spent)} spent against a ${plan.minutes}-minute plan` +
      (spent > planned * 1.15 ? " — ran long."
       : spent < planned * 0.7 ? " — you moved quickly." : ".") + `</p>`
    : "";
  $("todayStage").innerHTML =
    `<div class="card"><h2>Done for today</h2>` +
    `<p>${puzzles} puzzles · ${drills ? drills + " drills · " : ""}` +
    `${games} game${games === 1 ? "" : "s"} at ${plan.time_control}.</p>` + timing +
    `<p class="muted">Worked on <b>${PILLAR_LABEL[plan.focus_pillar] || plan.focus_pillar}</b>` +
    ` and drilled <b>${plan.secondary_pillar}</b>.</p>` +
    `<p class="muted">Tomorrow's plan is rebuilt from what you just played — the leak that` +
    ` costs the most is what you'll get.</p>` +
    `<div class="row"><button id="finishSession" class="primary big">Finish</button></div></div>`;
  $("finishSession").addEventListener("click", async () => {
    await fetch(`/api/session/${SESSION.id}/finish`, { method: "POST" });
    SESSION = null;
    loadToday();
  });
}

/* --------------------------------- PLAY --------------------------------- */
let playBoard = Board($("playBoard"));
let PLAY_UI = { board: playBoard, status: "playStatus", clocks: "playClocks",
                opp: "clkOpp", you: "clkYou", warn: "playWarn" };
let ws = null, gamesLeft = 0, clockTimer = null, lastClocks = null, youAre = "white";
// The game currently on the socket: which surface owns it, and - if it belongs
// to a Today block - which session and block. The socket is shared by both tabs,
// so without this a casual game on the Play tab counted as the session's game
// and advanced the plan out from under an unfinished block.
let GAME_CTX = null;
let liveGame = false;      // a game is in progress on the server right now
let lastOpts = null;       // enough to restart it after the socket comes back

function wsSend(m) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(m)); }

/* The server's handle on the game in progress. Kept in sessionStorage so a
 * reload finds it, and cleared the moment the game ends or the server says it
 * no longer has it. Storage can throw in a locked-down browser, so every access
 * is guarded and the app falls back to the old behaviour. */
const GAME_KEY_STORE = "coach.gameKey";
function savedGameKey() {
  try { return sessionStorage.getItem(GAME_KEY_STORE) || null; } catch (e) { return null; }
}
function rememberGameKey(key) {
  try {
    if (key) sessionStorage.setItem(GAME_KEY_STORE, key);
    else sessionStorage.removeItem(GAME_KEY_STORE);
  } catch (e) { /* private window: resume is simply unavailable */ }
}

function stopClocks() {
  if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
  lastClocks = null;
  const el = $(PLAY_UI.clocks);
  if (el) el.hidden = true;
}

function playConnect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  try {
    ws = new WebSocket(proto + "://" + location.host + "/ws/play");
  } catch (e) {
    setTimeout(playConnect, 1500);
    return;
  }
  ws.onmessage = (ev) => playMsg(JSON.parse(ev.data));
  ws.onopen = () => {
    // The server writes the game down under a key it hands back on every state
    // message, so a reload, a slept phone or a dropped connection resumes the
    // same position instead of dealing a fresh game over the top of it. The
    // clock was paused while nobody was connected.
    const key = savedGameKey();
    if (key) {
      setText(PLAY_UI.status, "Reconnecting to your game…");
      wsSend({ type: "resume", key });
      return;
    }
    if (liveGame && lastOpts) {
      setText(PLAY_UI.status, "Reconnected — that game was lost, starting a new one.");
      liveGame = false;
      stopClocks();
      newGame(lastOpts);
    }
  };
  ws.onclose = () => {
    // Anything that throws in here used to take the reconnect down with it, and
    // the app went quietly dead: New game did nothing, forever, on both tabs.
    try {
      stopClocks();
      setText(PLAY_UI.status, "Disconnected — reconnecting…");
    } finally {
      setTimeout(playConnect, 1500);
    }
  };
  ws.onerror = () => { try { ws.close(); } catch (e) { /* onclose reconnects */ } };
}

const NEW_GAME_TRIES = 25;   // ~10s of waiting for the socket before giving up

function newGame(opts, ctx, tries) {
  if (ctx !== undefined) GAME_CTX = ctx;
  lastOpts = opts;
  const send = () => wsSend(Object.assign({
    type: "new", account: acctFor("account"), colour: "auto",
    use_weak_opening: true, style: "auto", elo: "auto", auto_elo: true,
  }, opts));
  if (ws && ws.readyState === 1) { send(); return; }
  const n = (tries || 0) + 1;
  if (n > NEW_GAME_TRIES) {
    // Better a message than a button that silently does nothing.
    setText(PLAY_UI.status, "⚠ No connection to the coach. Is the server running?");
    return;
  }
  if (n === 3) setText(PLAY_UI.status, "Connecting…");
  setTimeout(() => newGame(opts, undefined, n), 400);
}

// True when the game just finished is the one the current Today block asked for.
function ctxOwnsSession() {
  return !!(GAME_CTX && SESSION && GAME_CTX.sessionId === SESSION.id
            && GAME_CTX.blockIndex === SESSION.block_index
            && SESSION.block && SESSION.block.kind === "game");
}

function tickClocks() {
  const box = $(PLAY_UI.clocks);
  // The Today stage can be replaced while the interval is still running; without
  // this the tick threw ten times a second and the reconnect never got a turn.
  if (!box) { stopClocks(); return; }
  if (!lastClocks || !lastClocks.base_s) { box.hidden = true; return; }
  box.hidden = false;
  const running = lastClocks._turn;
  const now = performance.now();
  for (const side of ["white", "black"]) {
    let v = lastClocks[side];
    if (side === running) v -= (now - lastClocks._at) / 1000;
    const el = side === youAre ? $(PLAY_UI.you) : $(PLAY_UI.opp);
    if (!el) continue;
    el.querySelector("span").textContent = fmtClock(v);
    el.className = "clk" + (side === running ? " run" : "") + (v < 20 ? " low" : "");
  }
}

function playMsg(m) {
  const board = PLAY_UI.board;
  if (m.type === "no_game") {
    // The server has no such game any more (finished, expired, or restarted).
    rememberGameKey(null);
    liveGame = false;
    stopClocks();
    if (lastOpts) { newGame(lastOpts); }
    else { setText(PLAY_UI.status, "That game has finished."); }
  } else if (m.type === "state") {
    youAre = m.you;
    liveGame = true;
    rememberGameKey(m.game_key);
    board.set({
      fen: m.fen, flipped: m.you === "black", last: m.last_move, check: m.check,
      dests: m.turn === m.you ? m.dests : {},
      onMove: (uci) => wsSend({ type: "move", uci }),
    });
    if (m.clocks && m.clocks.base_s) {
      lastClocks = Object.assign({}, m.clocks, { _turn: m.turn, _at: performance.now() });
      if (!clockTimer) clockTimer = setInterval(tickClocks, 100);
      tickClocks();
    } else { stopClocks(); }
    setHTML(PLAY_UI.warn, "");
    const a = m.auto || {};
    setText(PLAY_UI.status,
      (m.turn === m.you ? "Your move" : "Coach to move") +
      (m.check ? " — check!" : "") + (m.opening ? "  ·  " + m.opening : "") +
      "  ·  Coach ~" + m.elo + (a.elo ? " (auto)" : ""));
    if (a.elo && $("elo")) { $("elo").value = Math.max(400, m.elo); $("eloVal").textContent = m.elo; }
    if ($("pgn")) { $("pgn").hidden = true; $("reviewLink").hidden = true; }
  } else if (m.type === "thinking") {
    setText(PLAY_UI.status, "Coach is thinking…");
  } else if (m.type === "coach_warning") {
    // Contemporaneous correction: the point of coached mode.
    setHTML(PLAY_UI.warn,
      `<div class="warnbox">That drops about ${(m.drop/100).toFixed(1)} pawns` +
      ` — <b>${m.played_san}</b> runs into trouble. Want another look?` +
      `<div class="row"><button id="wbBack" class="primary">Let me think again</button>` +
      `<button id="wbGo">Play it anyway</button></div>` +
      `<p class="muted">You get one of these per game.</p></div>`);
    const back = $("wbBack"), go = $("wbGo");
    if (back) back.addEventListener("click", () => {
      setHTML(PLAY_UI.warn, "");
      wsSend({ type: "takeback" });
    });
    if (go) go.addEventListener("click", () => {
      setHTML(PLAY_UI.warn, "");
      wsSend({ type: "move", uci: m.played_uci, confirm: m.played_uci });
    });
  } else if (m.type === "gameover") {
    liveGame = false;
    rememberGameKey(null);
    const owns = ctxOwnsSession();
    let s = "Game over: " + m.result + " (" + m.reason + ")";
    if (m.next_elo) s += "  ·  next game ~" + m.next_elo + " Elo";
    // A game abandoned after a move or two is deliberately not filed, rated or
    // analysed; say so rather than leaving the user to wonder where it went.
    if (m.counted === false) s += "  ·  too short to count — not saved or rated";
    setText(PLAY_UI.status, s);
    stopClocks();
    if (m.game_id && owns) blockGameIds.push(m.game_id);
    if ($("pgn") && !owns) { $("pgn").value = m.pgn; $("pgn").hidden = false; }
    // Inside a session: play the next game, or move on to the review block.
    if (owns) {
      blockGameSecs.push((performance.now() - blockGameStart) / 1000);
      gamesLeft--;
      const p = SESSION.block.params;
      // Starting a game the block has no room for either overruns the hour or
      // gets abandoned half-played - but so does stopping at the planned count
      // when the games came in at half the estimated length. Today's own games
      // are the honest number; the planner's estimate only sets a floor, so one
      // freak twenty-second resignation can't authorise a game there is no time
      // for.
      const est = (p.est_minutes || 10) * 60;
      const typical = blockGameSecs.reduce((a, b) => a + b, 0) / blockGameSecs.length;
      const needed = Math.max(90, est * 0.5, typical);
      const room = blockSecondsLeft() >= needed && blockGameSecs.length < MAX_BLOCK_GAMES;
      noteBlockProgress();
      if (room) {
        paintGameCount();
        const ctx = { sessionId: SESSION.id, blockIndex: SESSION.block_index };
        setTimeout(() => {
          if (ctxOwnsSession()) {
            blockGameStart = performance.now();
            newGame({ base_s: p.base_s, inc_s: p.inc_s, coached: p.coached,
                      session_id: ctx.sessionId }, ctx);
          }
        }, 1200);
      } else {
        paintGameCount(true);
        setText(PLAY_UI.status,
          s + "  ·  that's the game block — on to the review");
        const record = blockRecord();
        setTimeout(() => {
          if (ctxOwnsSession()) advance(record);
        }, 1800);
      }
    }
  } else if (m.type === "report") {
    const worst = (m.key_moments || []).slice(0, 3).map((mm) =>
      `${mm.move_number}${mm.side === "white" ? "." : "…"} ${mm.played_san} (−${mm.cp_loss})`).join("   ");
    const link = $("reviewLink");
    if (link && !GAME_CTX) {
      link.hidden = false;
      link.innerHTML = `<b>Report:</b> acpl ${m.acpl}, focus <b>${m.focus_pillar || "—"}</b>. ` +
        (worst ? "Key: " + worst : "");
    }
    // The review block may be sitting there waiting for exactly this game.
    if (typeof onReportReady === "function") onReportReady(m.game_id);
  } else if (m.type === "error") {
    setText(PLAY_UI.status, "⚠ " + m.msg);
  }
}

$("elo").addEventListener("input", (e) => { $("eloVal").textContent = e.target.value; });
$("autoElo").addEventListener("change", (e) => { $("elo").disabled = e.target.checked; });
$("newGame").addEventListener("click", () => {
  PLAY_UI = { board: playBoard, status: "playStatus", clocks: "playClocks",
              opp: "clkOpp", you: "clkYou", warn: "playWarn" };
  const autoE = $("autoElo").checked;
  const [b, i] = ($("tc").value || "0,0").split(",").map(Number);
  gamesLeft = 1;
  // Free play: explicitly *not* the session's game, so finishing it can't
  // advance the Today plan.
  newGame({
    elo: autoE ? "auto" : +$("elo").value, auto_elo: autoE,
    use_weak_opening: $("weakOpening").checked, style: $("style").value,
    base_s: b, inc_s: i, coached: $("coached").checked,
  }, null);
});
$("resign").addEventListener("click", () => wsSend({ type: "resign" }));

/* -------------------------------- PUZZLES ------------------------------- */
const puzzleBoard = Board($("puzzleBoard"));
let puz = null, userMoves = [], puzStart = 0, puzPractice = false;

async function loadPuzzle() {
  exReset();
  $("nextPuzzle").hidden = true;
  $("puzzleStatus").textContent = "Loading…";
  const r = await fetch("/api/puzzles/next").then((x) => x.json());
  if (!r.puzzle) {
    $("puzzleStatus").textContent = "No puzzles due right now.";
    puzzleBoard.set({ fen: "8/8/8/8/8/8/8/8 w - - 0 1", onMove: null, dests: {} });
    $("puzzleMeta").textContent = "";
    return;
  }
  puz = r.puzzle; userMoves = []; puzStart = Date.now(); puzPractice = false;
  $("puzzleMeta").textContent = puz.kind === "line"
    ? `${puz.name || "opening line"} · ${r.remaining} due`
    : `${puz.pillar}${puz.motif ? " · " + puz.motif : ""} · ~${puz.rating} · ` +
      `you ${r.your_rating} · ${r.remaining} due` +
      (puz.kind === "punish" ? " · punish the mistake" : "");
  await stepPuzzle(null);
}

async function stepPuzzle(uci) {
  if (uci) userMoves.push(uci);
  const res = await fetch(`/api/puzzles/${puz.id}/attempt`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ moves: userMoves, auto_first: true,
                           elapsed_ms: Date.now() - puzStart,
                           practice: puzPractice }),
  }).then((x) => x.json());

  const flip = res.user_colour === "black";
  if (res.status === "continue") {
    const stm = turnOf(res.fen);
    $("puzzleStatus").textContent =
      (uci ? "✓ keep going — " : puzPractice ? "Try again — " : "") + `${stm[0].toUpperCase() + stm.slice(1)} to play.`;
    puzzleBoard.set({ fen: res.fen, flipped: flip, last: res.last_move,
                      check: res.check, dests: res.dests, onMove: stepPuzzle });
    return;
  }
  if (res.status === "alternative") {
    $("puzzleStatus").textContent = `✓ ${res.played_san} — not the stored line, but just as good.`;
  } else if (res.status === "solved") {
    $("puzzleStatus").textContent = res.practice
      ? "✓ Found it — practice, so the miss still stands."
      : `✓ Solved (${(res.graded || {}).label || ""}).`;
  } else {
    showMissedMove(puzzleBoard, $("puzzleStatus"), res, flip, null, () => {
      userMoves.pop();
      puzPractice = true;
      stepPuzzle(null);
    });
    $("nextPuzzle").hidden = false;
    return;
  }
  puzzleBoard.set({ fen: res.fen, flipped: flip, last: res.last_move,
                    hint: res.best_uci, check: res.check, dests: {}, onMove: null });
  exOptions(puzzleBoard, $("puzzleStatus"), flip,
            { convert: res.fen, ideas: res.fen, colour: res.user_colour,
              lastMove: res.last_move });
  $("nextPuzzle").hidden = false;
}
$("nextPuzzle").addEventListener("click", loadPuzzle);
$("skipPuzzle").addEventListener("click", loadPuzzle);

/* ------------------------------- OPENINGS ------------------------------ *
 * Learning a named opening on purpose. The repertoire drills rehearse what you
 * already play; this is for choosing something new, or putting names and
 * reasons to something you play by feel.
 *
 * Two modes on one board. Study steps through a line with the note for each
 * move, no scoring. Drill hands you the line to play from memory, through the
 * same attempt endpoint as every other puzzle, so it is graded and scheduled
 * like one - and a different move is "not the line", whatever the engine
 * thinks of it.                                                              */
const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const OP = { key: null, data: null, line: null, idx: 0, mode: "study",
             puz: null, moves: [], start: 0, practice: false };
const opBoard = Board($("opBoard"));

const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function opProgressPill(o) {
  if (!o.learning) return "";
  const p = o.progress || {};
  const bits = [];
  if (p.due) bits.push(`${p.due} due`);
  bits.push(`${p.solid}/${o.lines} solid`);
  return `<span class="pill on">learning · ${bits.join(" · ")}</span>`;
}

function opYoursLine(g) {
  if (!g || !g.games) return "";
  const dep = (g.departures || [])[0];
  return `<p class="opyours">In your games: ${plural(g.games, "game")}, ` +
    `scored ${g.score_pct}%` +
    (dep ? ` · you usually leave it at ${dep.label}${esc(dep.played)}` : "") + `</p>`;
}

async function loadOpenings() {
  OP.key = null;
  exReset(); lineStop();
  $("opDetail").hidden = true;
  $("opList").hidden = false;
  let r;
  try { r = await fetch("/api/openings").then((x) => x.json()); }
  catch { $("opList").innerHTML = `<p class="muted">Couldn't load the openings.</p>`; return; }
  const group = (side, title) => {
    const items = r.openings.filter((o) => o.side === side).map((o) => `
      <div class="opcard${o.learning ? " on" : ""}" data-key="${o.key}">
        <h3>${esc(o.name)} <span class="eco">${o.eco} · ${plural(o.lines, "line")}</span>
          ${opProgressPill(o)}</h3>
        <p>${esc(o.summary)}</p>
        ${opYoursLine(o.your_games)}
      </div>`).join("");
    return `<div class="opgroup">${title}</div>${items}`;
  };
  $("opList").innerHTML =
    `<p class="muted">Pick an opening to study its main lines move by move, then
      drill them from memory. Drills come back on a spaced-repetition schedule,
      and turn up in Today's opening block too.</p>` +
    group("white", "As White") + group("black", "As Black");
  $("opList").querySelectorAll(".opcard").forEach((el) =>
    el.addEventListener("click", () => openOpening(el.dataset.key)));
}

async function openOpening(key, keepBoard = false) {
  let d;
  try {
    const res = await fetch(`/api/openings/${encodeURIComponent(key)}`);
    if (!res.ok) throw new Error();
    d = await res.json();
  } catch { $("opStatus").textContent = "Couldn't load that opening."; return; }
  const fresh = OP.key !== key;
  OP.key = key; OP.data = d;
  $("opList").hidden = true;
  $("opDetail").hidden = false;
  $("opTitle").textContent = `${d.name} · as ${d.side}`;
  renderOpeningInfo();
  if (!keepBoard || fresh) {
    window.scrollTo(0, 0);
    studyLine(d.lines[0].key);
  }
}

function opStateChip(card) {
  if (!card) return "";
  if (card.due && card.reps) return `<span class="st due">due</span>`;
  return `<span class="st ${card.state}">${card.state}</span>`;
}

function renderOpeningInfo() {
  const d = OP.data;
  const g = d.your_games;
  let yours = `<p class="muted">Not in your games yet.</p>`;
  if (g && g.games) {
    const deps = (g.departures || []).map((x) =>
      `<li>At ${x.label} you played <b>${esc(x.played)}</b>, the line plays
        <b>${esc(x.book)}</b> <span class="muted">(${plural(x.games, "game")})</span></li>`).join("");
    // A game only counts against you here if *you* left the line first - an
    // opponent's sideline on move 3 is not something the drills can fix.
    const left = g.games - g.stayed_in_book;
    yours = `<p class="muted">You reached it in ${plural(g.games, "game")} ` +
      `(+${g.wins} =${g.draws} −${g.losses}, ${g.score_pct}%). ` +
      (left ? `You were first to leave the line in ${left} of them:`
            : "Whenever a game left the line, your opponent was the one to leave it.") +
      `</p>` +
      (deps ? `<ul class="opplans">${deps}</ul>` : "");
  }
  const clash = (d.conflicts || []).map((c) =>
    `<div class="warnbox">Overlaps with the ${esc(c.name)}, which you are also
      learning: at ${c.label} this plays <b>${esc(c.mine)}</b>, that plays
      <b>${esc(c.theirs)}</b>. Each drill says which line it wants; over the
      board you'll have to pick one.</div>`).join("");
  const lines = d.lines.map((l) => `
    <li data-line="${l.key}"${OP.line === l.key ? ' class="cur"' : ""}>
      <span class="nm">${esc(l.name)} <span class="muted">· ${l.plies.length} plies</span></span>
      ${opStateChip(l.card)}
      ${d.learning ? `<button data-drill="${l.key}">Drill</button>` : ""}
    </li>`).join("");
  const due = (d.progress || {}).due || 0;
  const actions = d.learning
    ? `<div class="row"><button class="primary g" id="opDrill">
         ${due ? `Drill ${plural(due, "due line")}` : "Drill (nothing due — practice)"}</button>
       <button id="opForget">Stop learning</button></div>`
    : `<button class="primary big" id="opLearn">Start learning — add
         ${plural(d.lines.length, "line")} to your drills</button>`;
  $("opInfo").innerHTML = `
    <div class="card">${actions}
      <h2 style="margin-top:12px">Lines</h2><ul class="oplines">${lines}</ul></div>
    ${clash}
    <div class="card"><h2>The idea</h2><p style="font-size:13px;line-height:1.45;margin:4px 0 0">
      ${esc(d.summary)}</p><ul class="opplans">${d.plans.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
    <div class="card"><h2>In your games</h2>${yours}</div>`;
  $("opInfo").querySelectorAll(".oplines li .nm").forEach((el) =>
    el.addEventListener("click", () => studyLine(el.parentElement.dataset.line)));
  $("opInfo").querySelectorAll("[data-drill]").forEach((el) =>
    el.addEventListener("click", () => startOpeningDrill(el.dataset.drill)));
  const learnBtn = $("opLearn");
  if (learnBtn) learnBtn.addEventListener("click", async () => {
    learnBtn.disabled = true;
    await post(`/api/openings/${OP.key}/learn`, {});
    await openOpening(OP.key, true);
  });
  const drillBtn = $("opDrill");
  if (drillBtn) drillBtn.addEventListener("click", () => startOpeningDrill(null));
  const forgetBtn = $("opForget");
  if (forgetBtn) forgetBtn.addEventListener("click", async () => {
    if (!confirm(`Stop learning the ${d.name}? Its drills and their review history are removed.`)) return;
    await post(`/api/openings/${OP.key}/forget`, {});
    if (OP.mode === "drill") studyLine(OP.line);
    await openOpening(OP.key, true);
  });
}

function opSetMode(mode) {
  OP.mode = mode;
  $("opStudyCtl").hidden = mode !== "study";
  $("opMoves").hidden = mode !== "study";
  $("opDrillCtl").hidden = mode !== "drill";
  if (mode === "study") { $("opDrillNext").hidden = true; $("opDrillMeta").textContent = ""; }
}

/* ---- study ---- */
function opLine() { return OP.data.lines.find((l) => l.key === OP.line); }

function studyLine(lineKey) {
  exReset(); lineStop();
  opSetMode("study");
  OP.line = lineKey;
  OP.idx = 0;
  $("opInfo").querySelectorAll(".oplines li").forEach((li) =>
    li.classList.toggle("cur", li.dataset.line === lineKey));
  const l = opLine();
  // Each move is kept on one line with its number, so "5." never ends a row
  // with "Ng3" starting the next.
  $("opMoves").innerHTML = l.plies.map((p, i) =>
    `<i class="pair">` +
    (p.label.endsWith(".") || i === 0 ? `<span class="lbl">${p.label}</span>` : "") +
    `<span class="mv ${p.yours ? "yours" : "theirs"}${p.note ? " noted" : ""}" data-i="${i + 1}">` +
    `${esc(p.san)}</span></i>`).join(" ");
  $("opMoves").querySelectorAll(".mv").forEach((el) =>
    el.addEventListener("click", () => studyGo(+el.dataset.i)));
  studyGo(0);
}

function studyGo(i) {
  const l = opLine();
  if (!l) return;
  OP.idx = Math.max(0, Math.min(l.plies.length, i));
  const p = OP.idx ? l.plies[OP.idx - 1] : null;
  opBoard.set({ fen: p ? p.fen : START_FEN, flipped: OP.data.side === "black",
                last: p ? p.uci : null, check: p ? p.check : null, hint: null,
                dests: {}, onMove: null });
  $("opMoves").querySelectorAll(".mv").forEach((el) =>
    el.classList.toggle("cur", +el.dataset.i === OP.idx));
  if (!p) {
    $("opStatus").textContent = `${l.name} — step through with ▶ or the arrow keys.`;
  } else {
    const who = p.yours ? "You" : "They";
    $("opStatus").textContent = `${p.label}${p.label.endsWith(".") ? "" : " "}${p.san} · ${who}` +
      (OP.idx === l.plies.length ? " · end of the line" : "");
  }
  $("opNote").textContent = p && p.note ? p.note : "";
}

$("opFirst").addEventListener("click", () => studyGo(0));
$("opPrev").addEventListener("click", () => studyGo(OP.idx - 1));
$("opNext").addEventListener("click", () => studyGo(OP.idx + 1));
$("opLast").addEventListener("click", () => studyGo(1e9));
$("opBack").addEventListener("click", loadOpenings);
document.addEventListener("keydown", (ev) => {
  if (!$("openings").classList.contains("active") || !OP.key || OP.mode !== "study") return;
  if (ev.target.closest && ev.target.closest("input,select,textarea")) return;
  if (ev.key === "ArrowLeft") { studyGo(OP.idx - 1); ev.preventDefault(); }
  if (ev.key === "ArrowRight") { studyGo(OP.idx + 1); ev.preventDefault(); }
});

/* ---- drill ---- */
async function startOpeningDrill(lineKey) {
  exReset(); lineStop();
  opSetMode("drill");
  $("opNote").textContent = "";
  $("opDrillNext").hidden = true;
  $("opStatus").textContent = "Loading…";
  const q = lineKey ? `?line=${encodeURIComponent(lineKey)}` : "";
  let r;
  try { r = await fetch(`/api/openings/${OP.key}/next${q}`).then((x) => x.json()); }
  catch { $("opStatus").textContent = "Couldn't load a line to drill."; return; }
  if (!r.puzzle) {
    $("opStatus").textContent = "Start learning this opening to drill it.";
    return;
  }
  OP.puz = r.puzzle; OP.moves = []; OP.start = Date.now();
  OP.practice = !!r.practice;
  const line = OP.data.lines.find((l) => l.puzzle_id === r.puzzle.id);
  if (line) OP.line = line.key;
  $("opInfo").querySelectorAll(".oplines li").forEach((li) =>
    li.classList.toggle("cur", li.dataset.line === OP.line));
  $("opDrillMeta").textContent = (line ? line.name : r.puzzle.name) +
    (OP.practice ? " · practice — not due, so not scored" : ` · ${r.remaining} due`);
  stepOpeningDrill(null);
}

async function stepOpeningDrill(uci) {
  if (uci) OP.moves.push(uci);
  const puz = OP.puz;
  const res = await post(`/api/puzzles/${puz.id}/attempt`, {
    moves: OP.moves, auto_first: true, elapsed_ms: Date.now() - OP.start,
    practice: OP.practice,
  });
  if (OP.puz !== puz) return;              // moved on while this was in flight
  if (!res || res.status === "error") {
    $("opStatus").textContent = `Couldn't check that move: ${(res && res.message) || "no reply"}.`;
    return;
  }
  const flip = res.user_colour === "black";
  const status = $("opStatus");
  if (res.status === "continue") {
    status.textContent = uci ? "✓ keep going…" : OP.practice && OP.moves.length
      ? "Try again — play the line." : "Play the line from memory.";
    opBoard.set({ fen: res.fen, flipped: flip, last: res.last_move, check: res.check,
                  hint: null, dests: res.dests, onMove: stepOpeningDrill });
    return;
  }
  const done = () => {
    $("opDrillNext").hidden = false;
    // Refresh the line states without disturbing the board.
    openOpening(OP.key, true);
  };
  if (res.status === "solved") {
    const g = res.graded;
    const days = g ? g.interval_days : null;
    const when = days == null ? "" : days < 1
      ? ` Back in ${Math.max(1, Math.round(days * 24 * 60))} min.`
      : ` Back in ${Math.round(days)} day${Math.round(days) === 1 ? "" : "s"}.`;
    status.textContent = res.practice
      ? "✓ Line complete — practice, so nothing was scored."
      : `✓ Line complete (${g ? g.label : "done"}).${when}`;
    opBoard.set({ fen: res.fen, flipped: flip, last: res.last_move, check: res.check,
                  hint: null, dests: {}, onMove: null });
    exOptions(opBoard, status, flip, { convert: res.fen, ideas: res.fen,
                                       colour: res.user_colour, lastMove: res.last_move });
    return done();
  }
  showMissedMove(opBoard, status, res, flip, null, () => {
    OP.moves.pop();
    OP.practice = true;
    $("opDrillNext").hidden = true;
    stepOpeningDrill(null);
  });
  done();
}

$("opDrillNext").addEventListener("click", () => startOpeningDrill(null));
$("opDrillStop").addEventListener("click", () => studyLine(OP.line));

/* -------------------------------- REVIEW ------------------------------- */
async function loadGames() {
  const acct = acctFor("reviewAccount");
  const games = await fetch("/api/games?account=" + encodeURIComponent(acct) + "&limit=60")
    .then((r) => r.json());
  cache.games = games;
  const ul = $("gameList"); ul.innerHTML = "";
  for (const g of games) {
    const li = document.createElement("li");
    li.textContent = `${g.analyzed ? "✓" : "·"} #${g.id}  ${(g.played_at || "").slice(0,10)}  ` +
      `${g.colour} ${g.result} vs ${g.opponent || "?"}  ${g.opening || ""}`;
    li.addEventListener("click", () => openReview(g.id));
    ul.appendChild(li);
  }
}
async function openReview(id) {
  const body = $("reviewBody");
  body.innerHTML = "<p class='muted'>Loading…</p>";
  const r = await fetch("/api/review/" + id).then((x) => x.json());
  if (r.needs_analysis) {
    body.innerHTML = "<p class='muted'>Not analysed yet.</p>";
    const btn = document.createElement("button");
    btn.textContent = "Analyse now (Stockfish, ~30 s)"; btn.className = "primary";
    btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "Analysing…";
      const resp = await fetch(`/api/games/${id}/analyze`, { method: "POST" });
      if (resp.ok) openReview(id);
      else { const e = await resp.json(); body.innerHTML = "<p class='muted'>" + (e.detail || "failed") + "</p>"; }
    });
    body.appendChild(btn);
    return;
  }
  if (r.error) { body.innerHTML = "<p class='muted'>" + r.error + "</p>"; return; }
  renderReview(r);
}
function renderReview(r) {
  const body = $("reviewBody"); body.innerHTML = "";
  body.insertAdjacentHTML("beforeend",
    `<p><b>#${r.game_id}</b> ${r.colour} vs ${r.opponent || "?"} — ${r.result} · ${r.opening || ""}` +
    `<br><span class="muted">${r.summary || ""}</span></p>`);
  body.appendChild(evalGraph(r.eval_graph, r.colour));
  const bd = document.createElement("div");
  bd.className = "board"; bd.style.margin = "8px 0";
  body.appendChild(bd);
  const rb = Board(bd);
  rb.set({ fen: r.start_fen || "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
           flipped: r.colour === "black" });
  for (const m of r.key_moments) {
    const d = document.createElement("div"); d.className = "moment";
    d.innerHTML = `<b>${m.move_number}${m.side === "white" ? "." : "…"} ${m.played_san}</b>` +
      ` −${m.cp_loss}cp · ${m.pillar}${m.motif ? " · " + m.motif : ""}<br>` +
      (m.best_san ? `better: ${m.best_san} (${m.best_line_san.join(" ")})<br>` : "") +
      refutationHTML(m) +
      (m.note ? `<span class="muted">${m.note}</span>` : "");
    d.addEventListener("click", () =>
      rb.set({ fen: m.fen, flipped: r.colour === "black", hint: m.best_uci,
               last: m.played_uci, dests: {}, onMove: null }));
    body.appendChild(d);
  }
}
function evalGraph(vals, colour) {
  const w = 500, h = 90, n = Math.max(1, vals.length - 1);
  const pts = vals.map((v, i) => {
    const x = (i / n) * w;
    const cp = colour === "black" ? -v : v;
    const y = h / 2 - (Math.max(-800, Math.min(800, cp)) / 800) * (h / 2 - 4);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "graph");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.innerHTML = `<line x1="0" y1="${h/2}" x2="${w}" y2="${h/2}" stroke="#555"/>` +
    `<polyline fill="none" stroke="#7ab8ff" stroke-width="2" points="${pts}"/>`;
  return svg;
}

/* ------------------------------ DASHBOARD ----------------------------- */
async function loadDashboard() {
  const d = await fetch("/api/dashboard").then((r) => r.json());
  const body = $("dashBody"); body.innerHTML = "";
  const prof = d.profile || {};
  if (!prof.n_moves) {
    body.innerHTML = "<p class='muted'>No analysed games yet.</p>";
    return;
  }
  body.insertAdjacentHTML("beforeend",
    `<p class="muted">${prof.n_moves} moves across ${prof.n_games_analysed} games` +
    (prof.half_life_days ? ` · weighted to the last ~${prof.half_life_days} days` : "") + `</p>`);
  const ranked = prof.ranked_weaknesses || [];
  const maxSev = Math.max(0.001, ...ranked.map((w) => w.severity));
  const bars = document.createElement("div"); bars.className = "bars";
  for (const w of ranked) {
    const el = document.createElement("div"); el.className = "bar";
    const arrow = w.trend_cp_loss < -3 ? "improving" : w.trend_cp_loss > 3 ? "worsening" : "flat";
    el.innerHTML = `<i style="width:${(w.severity / maxSev * 100).toFixed(0)}%"></i>` +
      `<span>${w.label} — avg −${w.avg_cp_loss}cp · blunders ${(w.blunder_rate*100).toFixed(0)}% · ${arrow}</span>`;
    bars.appendChild(el);
  }
  body.appendChild(bars);

  // Per-time-control breakdown: the same pillar can be sound slow and fall apart fast.
  const speeds = prof.speeds || {};
  if (Object.keys(speeds).length) {
    body.insertAdjacentHTML("beforeend", "<p class='muted'>By time control</p>");
    for (const [sp, e] of Object.entries(speeds)) {
      const inner = Object.entries(e.pillars || {})
        .sort((a, b) => b[1].total_cp_cost - a[1].total_cp_cost)
        .map(([k, v]) => `${k} −${v.avg_cp_loss}`).join(" · ");
      // "sparring" is a source, not a clock: games against the coach, at
      // whatever control the session picked. Labelling it like blitz or rapid
      // invites the comparison that used to steer the whole week to blitz.
      const label = sp === "sparring" ? "vs the coach (all clocks)" : sp;
      body.insertAdjacentHTML("beforeend",
        `<p class="muted" style="margin:2px 0">• <b style="color:#e8e4dc">${label}</b> ` +
        `(${e.n} moves, avg −${e.avg_cp_loss}cp): ${inner}</p>`);
    }
  }

  // Where the clock went, against where the game was decided. ACPL cannot see
  // this and it is usually the most actionable line on the page.
  const ta = prof.time_allocation || {};
  if (ta.critical && ta.critical.n >= 20) {
    body.insertAdjacentHTML("beforeend", "<p class='muted'>Time allocation</p>");
    body.insertAdjacentHTML("beforeend",
      `<p class="muted" style="margin:2px 0">• <b style="color:#e8e4dc">${ta.critical.avg_s}s</b> ` +
      `on the ${ta.critical.n} positions where the game was decided · ` +
      `<b style="color:#e8e4dc">${ta.quiet.avg_s}s</b> on the ${ta.quiet.n} quiet ones` +
      (ta.allocation_ratio != null ? ` (ratio ${ta.allocation_ratio}×)` : "") + `</p>`);
    if (ta.allocation_ratio != null && ta.allocation_ratio < 1) {
      body.insertAdjacentHTML("beforeend",
        `<p class="muted" style="margin:2px 0">• that's the wrong way round — ` +
        `<b style="color:#ff9e7a">${((1 - ta.allocation_ratio) * 100).toFixed(0)}% less</b> ` +
        `time where it matters, and ${(ta.rushed_critical_rate * 100).toFixed(0)}% of those ` +
        `positions go by in under ${ta.rushed_seconds}s</p>`);
    }
  }

  // Did the drilling work? Recurrence answers that far more legibly than a
  // centipawn curve: the same mistake, counted, month by month.
  const rec = d.motif_recurrence || { motifs: [] };
  if ((rec.motifs || []).length && (rec.months || []).length > 1) {
    body.insertAdjacentHTML("beforeend",
      `<p class='muted'>Recurring mistakes — ${rec.unit}, ` +
      `${rec.months[0]} to ${rec.months[rec.months.length - 1]}</p>`);
    const maxRate = Math.max(0.01, ...rec.motifs.flatMap((m) => m.rates));
    const rx = document.createElement("div"); rx.className = "bars";
    for (const m of rec.motifs) {
      const el = document.createElement("div"); el.className = "bar";
      const dir = m.improvement > 0.3 ? "↓ less often"
                : m.improvement < -0.3 ? "↑ more often" : "flat";
      el.innerHTML = `<i style="width:${(m.current / maxRate * 100).toFixed(0)}%"></i>` +
        `<span>${m.motif} — ${m.current}/100 now vs ${m.rates[0]}/100 then · ${dir}</span>`;
      rx.appendChild(el);
    }
    body.appendChild(rx);
    body.appendChild(multiLine({
      series: rec.months.map((mo, i) => {
        const row = { date: mo };
        rec.motifs.forEach((m) => { row[m.motif] = m.rates[i]; });
        return row;
      }),
      pillars: rec.motifs.map((m) => m.motif),
    }, "per 100 decisions"));
  }

  const trends = d.pillar_trends || { series: [], pillars: [] };
  if (trends.series.length > 1) {
    body.insertAdjacentHTML("beforeend", "<p class='muted'>Pillar avg cp-loss over snapshots</p>");
    body.appendChild(multiLine(trends, "avg cp lost"));
  }
  const sp = (d.sparring_trend || { series: [] }).series;
  if (sp.length) {
    body.insertAdjacentHTML("beforeend", "<p class='muted'>Sparring score vs bot (weekly)</p>");
    const bx = document.createElement("div"); bx.className = "bars";
    for (const s of sp) {
      const el = document.createElement("div"); el.className = "bar";
      el.innerHTML = `<i style="width:${(s.score*100).toFixed(0)}%"></i>` +
        `<span>${s.week} — ${(s.score*100).toFixed(0)}% of ${s.games}</span>`;
      bx.appendChild(el);
    }
    body.appendChild(bx);
  }
}
const LINE_COLOURS = ["#7ab8ff","#ff9e7a","#9ae37a","#e3d07a","#c79ae3","#7ae3d0"];

/* Six coloured lines with no key and no scale is a decoration, not a chart. The
 * legend names them and the axis says what the numbers are, and a series with a
 * gap now breaks the line there instead of diving to zero and inventing a
 * month in which the user made no mistakes at all. */
function multiLine(trends, label) {
  const w = 500, h = 120, pad = 26;
  const series = trends.series;
  const all = series.flatMap((p) => trends.pillars.map((k) => p[k]).filter((v) => v != null));
  const max = Math.max(30, ...all);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "graph"); svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.style.height = "120px";
  const x = (i) => pad + (i / Math.max(1, series.length - 1)) * (w - pad - 4);
  const y = (v) => h - 16 - (v / max) * (h - 30);
  let inner =
    `<line x1="${pad}" y1="${y(0)}" x2="${w - 4}" y2="${y(0)}" stroke="#3a352d"/>` +
    `<line x1="${pad}" y1="${y(max)}" x2="${w - 4}" y2="${y(max)}" stroke="#3a352d"/>` +
    `<text x="2" y="${y(max) + 4}" fill="#8b8378" font-size="9">${Math.round(max)}</text>` +
    `<text x="2" y="${y(0) + 4}" fill="#8b8378" font-size="9">0</text>`;
  trends.pillars.forEach((k, ci) => {
    // Break the polyline at gaps rather than reading a missing value as zero.
    let run = [];
    const flush = () => {
      if (run.length > 1) {
        inner += `<polyline fill="none" stroke="${LINE_COLOURS[ci % LINE_COLOURS.length]}"` +
                 ` stroke-width="1.6" points="${run.join(" ")}"/>`;
      } else if (run.length === 1) {
        const [px, py] = run[0].split(",");
        inner += `<circle cx="${px}" cy="${py}" r="1.8"` +
                 ` fill="${LINE_COLOURS[ci % LINE_COLOURS.length]}"/>`;
      }
      run = [];
    };
    series.forEach((p, i) => {
      const v = p[k];
      if (v == null) { flush(); return; }
      run.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    });
    flush();
  });
  if (series.length > 1) {
    const first = String(series[0].date ?? ""), last = String(series[series.length - 1].date ?? "");
    inner += `<text x="${pad}" y="${h - 3}" fill="#8b8378" font-size="9">${first}</text>` +
      `<text x="${w - 4}" y="${h - 3}" fill="#8b8378" font-size="9" text-anchor="end">${last}</text>`;
  }
  if (label) {
    inner += `<text x="${w - 4}" y="10" fill="#8b8378" font-size="9" text-anchor="end">${label}</text>`;
  }
  svg.innerHTML = inner;
  const wrap = document.createElement("div");
  wrap.appendChild(svg);
  wrap.insertAdjacentHTML("beforeend",
    `<p class="muted" style="margin:4px 0 0">` + trends.pillars.map((k, ci) =>
      `<span style="color:${LINE_COLOURS[ci % LINE_COLOURS.length]}">■</span> ${k}`
    ).join("  ") + `</p>`);
  return wrap;
}

/* ------------------------------ bootstrap ---------------------------- */
async function populateAccounts() {
  const a = await fetch("/api/accounts").then((r) => r.json()).catch(() => ({}));
  const names = [...(a.lichess || []), ...(a.chesscom || [])];
  UNIFIED = a.unified !== false;
  const list = names.length ? names : ["me"];
  for (const selId of ["account", "reviewAccount", "dashAccount"]) {
    const sel = $(selId);
    sel.innerHTML = "";
    if (UNIFIED) { sel.hidden = true; continue; }
    for (const n of list) {
      const o = document.createElement("option"); o.value = o.textContent = n;
      sel.appendChild(o);
    }
  }
  $("acctBadge").hidden = false;
  $("acctBadge").textContent = UNIFIED
    ? (names.length ? names.join(" + ") : "you")
    : list[0];
}

/* ------- auto refresh: sync + analyse on open, poll for progress -------- */
let syncTimer = null;
async function pollSync() {
  const s = await fetch("/api/sync/status").then((r) => r.json()).catch(() => null);
  if (!s) return;
  const busy = ["syncing", "analysing", "mining"].includes(s.state);
  const badge = $("syncBadge");
  if (busy) {
    badge.hidden = false;
    badge.textContent = s.state + (s.detail ? " · " + s.detail : "");
  } else {
    badge.hidden = true;
    if (syncTimer && (s.new_games || s.analysed)) {
      const active = document.querySelector("nav button.active")?.dataset.view;
      if (active === "dashboard") loadDashboard();
      if (active === "review") loadGames();
    }
  }
  if (busy && !syncTimer) syncTimer = setInterval(pollSync, 3000);
  if (!busy && syncTimer) { clearInterval(syncTimer); syncTimer = null; }
}

$("refreshGames").addEventListener("click", loadGames);
$("refreshDash").addEventListener("click", loadDashboard);

populateAccounts();
pollSync();
playConnect();
playBoard.set({ fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" });
loadToday();
