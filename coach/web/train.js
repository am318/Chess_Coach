/* ================================ TRAIN ================================ *
 * Deliberate-practice modes that sit outside the daily hour: calculation,
 * Woodpecker cycles, the endgame ladder and pawn-structure lessons. Each mode
 * registers itself in TRAIN_MODES; the tab just switches between them.
 *
 * Shares app.js's globals (Board, $, esc, post, the Today block helpers):
 * both files are classic scripts, so their top-level bindings are one scope. */

const TRAIN_MODES = [];
let trainMode = null;

function registerTrainMode(key, label, render) {
  TRAIN_MODES.push({ key, label, render });
}

window.loadTrain = function loadTrain() {
  const bar = $("trainModes");
  if (!bar.childElementCount) {
    for (const m of TRAIN_MODES) {
      const b = document.createElement("button");
      b.textContent = m.label;
      b.dataset.mode = m.key;
      b.addEventListener("click", () => openTrainMode(m.key));
      bar.appendChild(b);
    }
  }
  openTrainMode(trainMode || TRAIN_MODES[0].key);
};

function openTrainMode(key) {
  trainMode = key;
  document.querySelectorAll("#trainModes button").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === key));
  const stage = $("trainStage");
  stage.innerHTML = "";
  const m = TRAIN_MODES.find((x) => x.key === key);
  if (m) m.render(stage);
}

/* ------------------------------ CALCULATION ----------------------------- *
 * The position is shown and nothing moves. Type the whole line - your moves
 * and the replies you expect - and only then is it checked, ply by ply.     */

function CalcUI(host, opts = {}) {
  host.innerHTML =
    `<p class="muted" data-r="intro"></p>` +
    `<div class="board" data-r="board"></div>` +
    `<div class="status" data-r="status">Loading…</div>` +
    `<input class="calcIn" data-r="input" autocomplete="off" autocapitalize="off" ` +
    `spellcheck="false" placeholder="e.g. Rxe8+ Qxe8 Qxf7#">` +
    `<div class="plies" data-r="plies"></div>` +
    `<div class="row"><button class="primary" data-r="check">Check line</button>` +
    `<button data-r="next" hidden>Next</button>` +
    `<span class="muted" data-r="meta"></span></div>`;
  const R = (k) => host.querySelector(`[data-r="${k}"]`);
  const board = Board(R("board"));
  let task = null;

  async function next() {
    R("plies").innerHTML = "";
    R("input").value = "";
    R("input").disabled = false;
    R("check").hidden = false;
    R("next").hidden = true;
    const r = await fetch("/api/calc/next").then((x) => x.json());
    task = r.task;
    const sm = r.summary || {};
    if (!task) {
      R("status").textContent =
        "No puzzles with lines long enough yet — mine some from your games first.";
      R("check").hidden = true;
      if (opts.onEmpty) opts.onEmpty();
      return;
    }
    board.set({ fen: task.fen, flipped: task.user_colour === "black", dests: {}, onMove: null });
    R("intro").textContent =
      `Calculate ${task.depth} of your moves deep (${task.plies} plies: yours and their best ` +
      `replies). Type the whole line, then check it. Don't move the pieces.`;
    R("status").textContent = `${task.user_colour[0].toUpperCase()}${task.user_colour.slice(1)} to play.`;
    R("meta").textContent = `depth ${task.depth} · ~${task.rating} · your calc rating ${sm.rating}`;
    R("input").focus();
  }

  async function check() {
    if (!task) return;
    R("check").disabled = true;
    const res = await post(`/api/calc/${task.id}/check`,
                           { line: R("input").value, depth: task.depth });
    R("check").disabled = false;
    if (!res) return;
    if (res.status === "unreadable") {
      R("status").textContent = `${res.message} Fix it and check again — nothing was counted.`;
      R("input").focus();
      return;
    }
    R("input").disabled = true;
    R("check").hidden = true;
    R("next").hidden = false;
    const tone = (p) => (p.ok ? "good" : "bad");
    R("plies").innerHTML = res.plies.map((p) =>
      `<span class="chip ${tone(p)}">${p.mine ? "you" : "them"}: ${esc(p.san || "—")}` +
      `${p.ok ? " ✓" : ""}</span>` +
      (!p.ok ? ` <span class="muted">${esc(p.note || "")}` +
        `${p.expected ? ` — the line was ${esc(p.expected)}` : ""}</span>` : "")
    ).join(" ") +
      `<p class="muted">Solution: ${esc(res.solution_san.join(" "))}</p>`;
    R("status").textContent = res.status === "solved"
      ? `✓ Calculated to the end (${res.needed} plies).`
      : `✗ Got ${res.reached} of ${res.needed} plies.`;
    const f = res.filed || {};
    if (f.depth_next && f.depth_next !== task.depth) {
      R("status").textContent += f.depth_next > task.depth
        ? `  Next lines go ${f.depth_next} moves deep.` : `  Next lines drop to ${f.depth_next} moves.`;
    }
    board.set({ fen: res.fen_end, dests: {}, onMove: null });
    if (opts.onResolved) opts.onResolved(res);
  }

  R("check").addEventListener("click", check);
  R("input").addEventListener("keydown", (ev) => { if (ev.key === "Enter") check(); });
  R("next").addEventListener("click", () => (opts.onNext ? opts.onNext(next) : next()));
  next();
  return { next };
}

registerTrainMode("calc", "Calculate", (stage) => {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = "<h2>Calculation</h2><div></div>";
  stage.appendChild(card);
  CalcUI(card.lastChild);
});

/* The same widget as a Today drill block: runs until the block's minutes are
 * spent, counting lines done. */
window.startCalcBlock = function startCalcBlock(block, body) {
  let done = ((SESSION.progress || {})[SESSION.block_index] || {}).puzzles || 0;
  blockRecord = () => ({ puzzles: done });
  CalcUI(body, {
    onResolved: () => { done++; noteBlockProgress(); },
    onNext: (next) => (blockTimeIsUp()
      ? advance({ ...blockRecord(), ended: "time" }) : next()),
    onEmpty: () => advance({ ...blockRecord(), ended: "empty" }),
  });
};

/* ------------------------------- WOODPECKER ----------------------------- *
 * One fixed set, the same order every pass, each pass faster than the last.
 * Solves go through the ordinary attempt endpoint in practice mode, so the SRS
 * is never touched: this set is drilled to speed, not scheduled.            */

function fmtMin(m) { return m == null ? "—" : `${Math.floor(m)}:${String(Math.round((m % 1) * 60)).padStart(2, "0")}`; }

registerTrainMode("cycle", "Cycle", async (stage) => {
  const card = document.createElement("div");
  card.className = "card";
  stage.appendChild(card);
  const r = await fetch("/api/woodpecker").then((x) => x.json());
  if (!r.set) return renderCycleStart(card, r);
  renderCycle(card, r.set);
});

function cycleTable(st) {
  if (!st.passes.length) return "";
  return `<table class="tbl"><tr><th>Pass</th><th>Done</th><th>Accuracy</th>` +
    `<th>Time</th><th>s / puzzle</th></tr>` +
    st.passes.map((p) => `<tr><td>${p.n}</td><td>${p.answered}/${st.size}</td>` +
      `<td>${p.accuracy == null ? "—" : (p.accuracy * 100).toFixed(0) + "%"}</td>` +
      `<td>${fmtMin(p.minutes)}</td><td>${p.sec_per_puzzle ?? "—"}</td></tr>`).join("") +
    `</table>`;
}

function renderCycleStart(card, r) {
  const enough = r.available >= r.min_size;
  const size = Math.min(r.default_size, Math.max(r.min_size, r.available));
  card.innerHTML =
    `<h2>Woodpecker cycle</h2>` +
    `<p class="muted">One fixed set of tactics from your games, solved in the same order ` +
    `again and again — each pass in less time than the last. By the third pass you will ` +
    `know the answers; that is the point. What's trained is <i>seeing</i> the pattern ` +
    `instantly, so it fires in a game without being looked for. Separate from your ` +
    `spaced-repetition queue.</p>` +
    (enough
      ? `<div class="row"><label class="muted">Set size <input id="wpSize" type="number" ` +
        `min="${r.min_size}" max="${Math.min(r.max_size, r.available)}" value="${size}" ` +
        `style="width:70px"></label><button id="wpStart" class="primary">Start a set</button></div>` +
        `<p class="muted">${r.available} tactical puzzles available.</p>`
      : `<p>You need at least ${r.min_size} tactical puzzles for a set and have ${r.available}. ` +
        `Mine more from your games, or import some from Lichess ` +
        `(<code>coach puzzles import-lichess FILE --weak-motifs</code>).</p>`);
  if (enough) {
    $("wpStart").onclick = async () => {
      const res = await post("/api/woodpecker/start", { size: +$("wpSize").value });
      if (!res || res.error) {
        card.insertAdjacentHTML("beforeend", `<p class="muted">${esc((res || {}).error || "failed")}</p>`);
        return;
      }
      renderCycle(card, res.set);
    };
  }
}

function renderCycle(card, st) {
  card.innerHTML =
    `<h2>Woodpecker cycle — pass <span data-r="pass"></span></h2>` +
    `<p class="muted" data-r="meta"></p>` +
    `<div class="board" data-r="board"></div>` +
    `<div class="status" data-r="status"></div>` +
    `<div class="row"><button class="primary" data-r="next" hidden>Next</button>` +
    `<button data-r="abandon">Abandon set</button></div>` +
    `<div data-r="table"></div>`;
  const R = (k) => card.querySelector(`[data-r="${k}"]`);
  const board = Board(R("board"));
  let puzzle = null, moves = [], start = 0, state = st;

  function paintMeta() {
    const cur = state.passes.find((p) => !p.finished);
    const done = cur ? cur.answered : 0;
    R("pass").textContent = state.pass;
    R("meta").textContent = `${done}/${state.size} this pass` +
      (state.target_minutes ? ` · target ${fmtMin(state.target_minutes)} for the whole pass` : "") +
      ` · up to ${state.max_passes} passes`;
    R("table").innerHTML = cycleTable(state);
  }

  async function load() {
    paintMeta();
    R("next").hidden = true;
    if (state.finished || !state.next_puzzle_id) {
      R("status").textContent = state.finished
        ? "Set complete. Start a new one from the Cycle tab whenever you like."
        : "Pass finished.";
      board.set({ fen: "8/8/8/8/8/8/8/8 w - - 0 1", dests: {}, onMove: null });
      if (state.finished) {
        R("abandon").textContent = "New set";
        R("abandon").onclick = () => openTrainMode("cycle");
      }
      return;
    }
    puzzle = await fetch(`/api/puzzles/${state.next_puzzle_id}`).then((x) => x.json());
    moves = []; start = Date.now();
    step(null);
  }

  async function step(uci) {
    if (uci) moves.push(uci);
    const res = await post(`/api/puzzles/${puzzle.id}/attempt`,
                           { moves, auto_first: true, practice: true });
    if (!res) return;
    const flip = res.user_colour === "black";
    if (res.status === "continue") {
      R("status").textContent = uci ? "✓ keep going…" : "Find it — fast.";
      board.set({ fen: res.fen, flipped: flip, last: res.last_move, check: res.check,
                  dests: res.dests, onMove: step });
      return;
    }
    const solved = res.status === "solved" || res.status === "alternative";
    const ans = await post(`/api/woodpecker/${state.id}/answer`,
                           { puzzle_id: puzzle.id, solved, elapsed_ms: Date.now() - start });
    if (ans) state = ans.set;
    board.set({ fen: res.fen, flipped: flip, last: res.last_move,
                hint: solved ? null : res.best_uci, dests: {}, onMove: null });
    if (solved) {
      R("status").textContent = "✓";
      paintMeta();
      setTimeout(load, 450);          // speed is the point: no extra click
    } else {
      R("status").textContent = `✗ It was ${res.best_san || "the marked move"}.`;
      R("next").hidden = false;
      paintMeta();
    }
  }

  R("next").onclick = load;
  R("abandon").onclick = async () => {
    if (!confirm("Abandon this set? Its passes stay in the record.")) return;
    await post(`/api/woodpecker/${state.id}/abandon`, {});
    openTrainMode("cycle");
  };
  load();
}
