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
