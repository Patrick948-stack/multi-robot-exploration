"use strict";

const $ = id => document.getElementById(id);
const all = selector => [...document.querySelectorAll(selector)];

// Settings that define the experimental design. Changing them is allowed but flagged.
const PLAN_FIELDS = ["replan", "obstacle_density", "open_cells_per_robot", "sensing_radius", "step_cap_factor", "master_seed"];
const INTEGER_FIELDS = ["trial_index", "trials", "open_cells_per_robot", "sensing_radius", "step_cap_factor", "master_seed", "workers"];
const ROBOT_COLORS = ["#ffd23f", "#ff5d8f", "#2ee6c5", "#b98cff", "#9be15d", "#ff9f1c", "#f4f4f4", "#ff6b6b"];
const MAP = { bg: "#0e1112", unknown: "#2a3034", seen: "#8b969c", wall: "#07090a", wallUnseen: "#1c2023", repeated: "#f08a4b", link: "#3fa9f5" };
const SERIES = {
  coverage: { label: "Coverage", axis: "Coverage C(t)", color: "#d6dde2", format: v => Math.round(v * 100) + "%" },
  wasted: { label: "Repeated discoveries (W)", axis: "W (cumulative)", color: "#f08a4b" },
  age: { label: "Mean information age", axis: "Age (steps)", color: "#b98cff" },
  lambda2: { label: "Algebraic connectivity (λ₂)", axis: "λ₂", color: "#2ee6c5" },
  groups: { label: "Connected groups", axis: "Groups", color: "#ffd23f" },
};
const STATUS = { running: "Running", stopping: "Stopping", complete: "Complete", stopped: "Stopped", failed: "Failed" };
const PAGE_SIZE = 15;

const state = {
  mode: "trial", setup: null, job: null, results: [], resultsSignature: "", pending: false, panel: "live",
  replay: newReplay(), drafts: {},
  run: { key: "", version: "", rows: [], settings: [], failures: [], selected: null, page: 0, sort: { key: "setting", dir: "asc" } },
};

function newReplay(key = "") {
  return { key, offset: 0, frames: [], cursor: 0, follow: true, playing: false, drained: false, header: null };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function api(path, data) {
  const options = data === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  };
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`Dashboard request failed (${response.status}).`);
  const payload = await response.json();
  if (payload.error) throw new Error(payload.error);
  return payload;
}

const fileUrl = (key, name) => "/api/file?" + new URLSearchParams({ key, name });
const isActive = (job = state.job) => ["running", "stopping"].includes(job?.status);
const rangeValue = r => (r === "unlimited" || r === "inf" || r === "") ? Infinity : Number(r);
const rangeText = r => rangeValue(r) === Infinity ? "unlimited" : String(Number(r));
const num = v => (v === "" || v === null || v === undefined || Number.isNaN(Number(v))) ? null : Number(v);
const fmt = (v, digits = 0) => num(v) === null ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const pct = v => {
  const value = num(v);
  if (value === null) return "—";
  const rounded = (value * 100).toFixed(1);
  return value < 1 && rounded === "100.0" ? "<100%" : rounded + "%";
};

function node(tag, props = {}, ...children) {
  const n = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "dataset") Object.assign(n.dataset, value);
    else if (key.startsWith("aria-") || key === "role") n.setAttribute(key, value);
    else n[key] = value;
  }
  n.append(...children.filter(c => c !== null && c !== undefined));
  return n;
}

function message(id, text, tone) {
  const target = $(id);
  target.hidden = !text;
  target.textContent = text || "";
  if (tone) target.dataset.state = tone; else delete target.dataset.state;
}

function link(key, name, label) {
  return node("a", { href: fileUrl(key, name), download: name, textContent: label });
}

function saveBlob(blob, name) {
  const a = node("a", { href: URL.createObjectURL(blob), download: name });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ---------------------------------------------------------------------------
// Panels and the run form
// ---------------------------------------------------------------------------

function choosePanel(name) {
  state.panel = name;
  $("live").hidden = name !== "live";
  $("results").hidden = name !== "results";
  for (const b of all("nav [data-panel]")) {
    if (b.dataset.panel === name) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
  if (name === "live") requestAnimationFrame(renderReplay); else renderResults();
  window.scrollTo(0, 0);
}

const FORM_FIELDS = ["preset", "team_sizes", "comm_ranges", "obstacle_density", "replan", ...INTEGER_FIELDS];

function chooseMode(mode) {
  if (state.mode !== mode) state.drafts[state.mode] = FORM_FIELDS.map(id => $(id).value);
  state.mode = mode;
  for (const b of all("[data-mode]")) b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
  for (const item of all(".sweep-only, .trial-only")) {
    item.hidden = item.classList.contains("sweep-only") ? mode !== "sweep" : mode !== "trial";
    for (const input of item.querySelectorAll("input, select")) input.disabled = item.hidden;
  }
  $("start").textContent = mode === "trial" ? "Run trial" : "Run sweep";
  message("run-error", "");
  updateFormNotes();
  updateMonitor();
}

function updateMonitor() {
  const sweep = state.mode === "sweep" || (state.job?.action === "sweep" && isActive());
  const progress = sweep && state.job?.action === "sweep";
  $("sweep-progress").hidden = !progress;
  $("sweep-ready").hidden = !sweep || progress;
  $("map-card").hidden = sweep;
  $("chart-card").hidden = sweep;
}

function useConfig(config) {
  const given = Object.fromEntries(Object.entries(config).filter(([, v]) => v !== null && v !== undefined));
  const c = { ...state.setup.defaults, ...given };
  for (const id of INTEGER_FIELDS) if (id in c && $(id)) $(id).value = c[id];
  $("team_sizes").value = [].concat(c.team_sizes).join(", ");
  $("comm_ranges").value = [].concat(c.comm_ranges).join(", ");
  $("replan").value = c.replan;
  $("obstacle_density").value = Math.round(c.obstacle_density * 100);
  if (!("workers" in config)) $("workers").value = state.setup.workers;
  updateFormNotes();
}

function listInput(id) {
  return $(id).value.split(",").map(v => v.trim()).filter(Boolean);
}

function planValue(id) {
  if (id === "obstacle_density") return $(id).valueAsNumber / 100;
  if (id === "replan") return $(id).value;
  return $(id).valueAsNumber;
}

function updateFormNotes() {
  if (!state.setup) return;
  const changed = PLAN_FIELDS.filter(id => planValue(id) !== state.setup.defaults[id]);
  $("changed").hidden = !changed.length;
  $("changed").textContent = `${changed.length} changed`;
  for (const id of PLAN_FIELDS) $(id).closest("label").classList.toggle("changed", changed.includes(id));
  if (state.mode === "trial") {
    $("mode-note").textContent = "Range in cells: 0 for none, or “unlimited”. The trial number picks the map, start cells and robot seeds, so the same trial at another range is a paired comparison.";
  } else {
    const n = listInput("team_sizes").length, r = listInput("comm_ranges").length, k = $("trials").valueAsNumber || 0;
    $("mode-note").textContent = `${n} team size${n === 1 ? "" : "s"} × ${r} range${r === 1 ? "" : "s"} × ${k} trials = ${(n * r * k).toLocaleString()} runs. Trial k uses the same map at every range.`;
  }
}

function readConfig() {
  const data = { action: state.mode, replan: $("replan").value };
  for (const id of INTEGER_FIELDS) {
    if ($(id).disabled) continue;
    const value = $(id).valueAsNumber;
    if (!Number.isInteger(value)) throw new Error(`${$(id).closest("label").firstChild.textContent.trim()} must be a whole number.`);
    data[id] = value;
  }
  data.team_sizes = listInput("team_sizes").map(Number);
  data.comm_ranges = listInput("comm_ranges");
  if (!data.team_sizes.length || data.team_sizes.some(n => !Number.isInteger(n))) throw new Error("Team sizes must be whole numbers.");
  if (!data.comm_ranges.length) throw new Error("Enter a communication range.");
  data.obstacle_density = $("obstacle_density").valueAsNumber / 100;
  if (state.mode === "sweep" && $("preset").value) data.preset = $("preset").value;
  return data;
}

async function perform(path, data, errorId) {
  if (state.pending) return;
  state.pending = true;
  message(errorId, "");
  updateJob(state.job);
  try {
    const { job } = await api(path, data);
    if (path === "/api/start" && job.action === "trial") state.replay = newReplay(job.key);
    updateJob(job);
    renderReplay();
  } catch (error) {
    message(errorId, error.message);
  } finally {
    state.pending = false;
    updateJob(state.job);
  }
}

function updateJob(job) {
  const previous = state.job;
  state.job = job;
  const active = isActive();
  $("start").disabled = !state.setup || active || state.pending;
  $("parameters").disabled = active || state.pending;
  for (const b of all("[data-mode]")) b.disabled = active || state.pending;
  $("stop").hidden = !active || !["trial", "sweep"].includes(job?.action);
  $("stop").disabled = state.pending || job?.status === "stopping";
  $("stop").textContent = job?.action === "sweep" ? "Stop sweep" : "Stop trial";

  const sweep = job?.action === "sweep";
  updateMonitor();
  if (sweep) {
    $("sweep-status").textContent = STATUS[job.status];
    $("sweep-status").dataset.state = job.status;
    $("sweep-detail").textContent = job.status === "failed" ? job.message :
      job.status === "complete" ? `${job.done.toLocaleString()} of ${job.total.toLocaleString()} trials processed. See Results for failures and coverage.` :
      `${job.done.toLocaleString()} of ${job.total.toLocaleString()} trials finished`;
    $("sweep-bar").max = job.total || 1;
    $("sweep-bar").value = job.done || 0;
    $("sweep-results").hidden = isActive(job);
    $("sweep-log").href = fileUrl(job.key, "run.log");
  }
  if (job?.action === "trial" && job.status === "failed" && (previous?.key !== job.key || previous.status !== job.status)) {
    message("run-error", job.message);
  }
  updateMapHeader();
  updateRunButtons();
}

// ---------------------------------------------------------------------------
// Live replay
// ---------------------------------------------------------------------------

function appendFrames(replay, frames) {
  for (const data of frames) {
    if (data.shape) {
      const count = data.shape[0] * data.shape[1];
      replay.header = data;
      replay.firstSeen = new Int32Array(count).fill(-1);
      replay.firstRepeat = new Int32Array(count).fill(-1);
      replay.walls = new Uint8Array(count);
      for (const i of data.walls) replay.walls[i] = 1;
    } else {
      for (const i of data.seen) replay.firstSeen[i] = data.t;
      for (const i of data.repeated) if (replay.firstRepeat[i] < 0) replay.firstRepeat[i] = data.t;
      replay.frames.push(data);
    }
  }
  if (replay.follow) replay.cursor = Math.max(0, replay.frames.length - 1);
}

async function poll() {
  if (state.pending) return;
  const { job, results } = await api("/api/state");
  if (state.pending) return;
  if (isActive(job) && ["trial", "sweep"].includes(job.action) && state.job?.key !== job.key) {
    const saved = results.find(run => run.key === job.key);
    if (saved) {
      chooseMode(job.action);
      useConfig(saved.config);
      $("preset").value = saved.config.preset || "";
    }
  }
  if (job?.action === "trial" && isActive(job) && job.key !== state.replay.key) state.replay = newReplay(job.key);
  else if (!state.replay.key && !state.resultsSignature) {
    // First load: reopen the most recent live trial so the workspace picks up where it was left.
    const last = results.find(r => r.kind === "trial" && r.has_replay);
    if (last) state.replay = newReplay(last.key);
  }
  const signature = JSON.stringify(results);
  if (signature !== state.resultsSignature) {
    state.results = results;
    state.resultsSignature = signature;
    renderResults();
  }
  updateJob(job);
  const replay = state.replay;
  if (!replay.key || replay.drained) return;
  const data = await api("/api/frames?" + new URLSearchParams({ key: replay.key, offset: replay.offset }));
  if (replay !== state.replay) return;
  replay.offset = data.offset;
  if (data.frames.length) appendFrames(replay, data.frames);
  else if (job?.key !== replay.key || !isActive(job)) replay.drained = true;
  if (data.frames.length || replay.drained) renderReplay();
}

function mapTitle(header) {
  return `Trial ${header.trial} · N = ${header.robots} · range ${rangeText(header.range)} · seed ${header.seed ?? "—"}`;
}

function updateMapHeader() {
  const { header, key, follow, drained } = state.replay;
  const job = state.job?.action === "trial" && state.job.key === key ? state.job : null;
  const badge = $("map-status");
  badge.hidden = !key;
  badge.textContent = job?.status === "running" ? (follow ? "Live" : "Reviewing") : job ? STATUS[job.status] : "Replay";
  badge.dataset.state = job?.status === "running" ? "running" : job?.status || "";
  $("map-title").textContent = header ? mapTitle(header) : "Exploration map";
  $("map-detail").textContent = header ?
    `${header.shape[0]} × ${header.shape[1]} grid · F = ${header.open.toLocaleString()} open cells · step cap ${header.cap?.toLocaleString() ?? "—"}` :
    !key ? "Run a live trial to watch the team explore." :
    job && ["failed", "stopped"].includes(job.status) ? "The trial ended before its first step." :
    drained ? "No replay frames were saved for this run." : "Starting the simulation…";
}

function canvasContext(canvas) {
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width || !height) return null;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

// Where each map panel sits on the canvas, kept for hover lookups.
let mapGeometry = null;
const TRUTH = { open: "#d5dce0", wall: "#07090a" };
const robotColor = i => ROBOT_COLORS[i % ROBOT_COLORS.length];

/** One panel (team view) or two (team view and true map), side by side when there is room. */
function mapPanels(width, height, rows, cols, withTruth) {
  const boxes = !withTruth ? [{ kind: "team", x0: 0, y0: 0, w: width, h: height }] :
    width >= height * 1.25 ?
      [{ kind: "team", x0: 0, y0: 0, w: width / 2, h: height }, { kind: "truth", x0: width / 2, y0: 0, w: width / 2, h: height }] :
      [{ kind: "team", x0: 0, y0: 0, w: width, h: height / 2 }, { kind: "truth", x0: 0, y0: height / 2, w: width, h: height / 2 }];
  const title = withTruth ? 18 : 0;
  return boxes.map(b => {
    const cell = Math.min((b.w - 44) / cols, (b.h - 36 - title) / rows);
    return {
      ...b, cell, title,
      left: b.x0 + (b.w - cols * cell) / 2 + 8,
      top: b.y0 + title + (b.h - title - rows * cell) / 2 + 6,
    };
  });
}

function drawMap(replay, frame) {
  const surface = canvasContext($("map"));
  mapGeometry = null;
  if (!surface) return;
  const { ctx, width, height } = surface;
  ctx.fillStyle = MAP.bg;
  ctx.fillRect(0, 0, width, height);
  if (!frame) {
    ctx.fillStyle = "#6c767c";
    ctx.font = "14px system-ui";
    ctx.textAlign = "center";
    ctx.fillText(replay.key ? "Waiting for the first step…" : "Choose settings and run a live trial.", width / 2, height / 2);
    return;
  }
  const [rows, cols] = replay.header.shape;
  const panels = mapPanels(width, height, rows, cols, $("show-truth").checked);
  mapGeometry = { panels, rows, cols };
  for (const panel of panels) {
    if (panel.title) {
      ctx.font = "bold 11px system-ui";
      ctx.fillStyle = "#c7cfd5";
      ctx.textAlign = "center";
      ctx.fillText(panel.kind === "team" ? `Seen by the team at step ${frame.t}` : "True map (the robots never see this)",
        panel.left + cols * panel.cell / 2, panel.y0 + 14);
    }
    drawPanel(ctx, panel, replay, frame);
  }
}

function drawPanel(ctx, panel, replay, frame) {
  const { header, walls, firstSeen, firstRepeat } = replay;
  const [rows, cols] = header.shape;
  const { cell, left, top } = panel;
  const team = panel.kind === "team";
  const x = c => left + (c + .5) * cell, y = r => top + (r + .5) * cell;
  const showRepeated = $("show-repeated").checked;
  const gap = cell > 6 ? .6 : 0;
  for (let i = 0; i < rows * cols; i++) {
    if (team) {
      const seen = firstSeen[i] >= 0 && firstSeen[i] <= frame.t;
      ctx.fillStyle = walls[i] ? (seen ? MAP.wall : MAP.wallUnseen) :
        showRepeated && firstRepeat[i] >= 0 && firstRepeat[i] <= frame.t ? MAP.repeated : seen ? MAP.seen : MAP.unknown;
    } else {
      ctx.fillStyle = walls[i] ? TRUTH.wall : TRUTH.open;
    }
    ctx.fillRect(left + (i % cols) * cell, top + Math.floor(i / cols) * cell, cell - gap, cell - gap);
  }
  // Axis ticks (row, column) so a moment can be described precisely.
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillStyle = "#7d878d";
  const every = Math.max(5, Math.round(cols / 8 / 5) * 5);
  ctx.textAlign = "center";
  for (let c = 0; c < cols; c += every) ctx.fillText(String(c), x(c), top - 4);
  ctx.textAlign = "right";
  for (let r = 0; r < rows; r += every) ctx.fillText(String(r), left - 5, y(r) + 3);
  const [er, ec] = header.entrance;
  ctx.fillStyle = "#eef1f3";
  ctx.beginPath();
  ctx.moveTo(x(ec) - cell * .5 - 6, y(er) - 4); ctx.lineTo(x(ec) - cell * .5 - 1, y(er)); ctx.lineTo(x(ec) - cell * .5 - 6, y(er) + 4);
  ctx.fill();

  if (team && $("show-trails").checked) {
    ctx.lineWidth = Math.max(1, cell * .18);
    ctx.globalAlpha = .6;
    ctx.lineJoin = "round";
    for (let i = 0; i < frame.positions.length; i++) {
      ctx.strokeStyle = robotColor(i);
      ctx.beginPath();
      for (let t = Math.max(0, replay.cursor - 20); t <= replay.cursor; t++) {
        const [r, c] = replay.frames[t].positions[i];
        ctx.lineTo(x(c), y(r));
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
  if (team && $("show-sensing").checked) {
    ctx.lineWidth = 1;
    ctx.globalAlpha = .55;
    frame.positions.forEach(([r, c], i) => {
      ctx.strokeStyle = robotColor(i);
      ctx.beginPath();
      ctx.arc(x(c), y(r), header.sensing * cell, 0, 2 * Math.PI);
      ctx.stroke();
    });
    ctx.globalAlpha = 1;
  }
  if ($("show-links").checked && frame.links.length) {
    for (const [w, c] of [[3.6, "#05080a"], [1.8, MAP.link]]) {
      ctx.lineWidth = w;
      ctx.strokeStyle = c;
      ctx.beginPath();
      for (const [i, j] of frame.links) {
        ctx.moveTo(x(frame.positions[i][1]), y(frame.positions[i][0]));
        ctx.lineTo(x(frame.positions[j][1]), y(frame.positions[j][0]));
      }
      ctx.stroke();
    }
  }
  const radius = robotRadius(cell);
  frame.positions.forEach(([r, c], i) => {
    ctx.fillStyle = robotColor(i);
    ctx.strokeStyle = "#05080a";
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    ctx.arc(x(c), y(r), radius, 0, 2 * Math.PI);
    ctx.fill();
    ctx.stroke();
    if (radius >= 6 && frame.positions.length <= 16) {
      ctx.fillStyle = "#05080a";
      ctx.font = "bold 8px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.fillText(String(i), x(c), y(r) + 3);
    }
  });
}

const robotRadius = cell => Math.max(2.6, Math.min(7.5, cell * .55));

// ---------------------------------------------------------------------------
// Hover details for the map
// ---------------------------------------------------------------------------

let mapPointer = null;  // last pointer position over the map, in canvas pixels

/** Which panel, cell and robots are under a point on the map canvas. */
function mapHit(px, py) {
  const geometry = mapGeometry;
  const frame = state.replay.frames[state.replay.cursor];
  if (!geometry || !frame) return null;
  const panel = geometry.panels.find(p => px >= p.x0 && px < p.x0 + p.w && py >= p.y0 && py < p.y0 + p.h);
  if (!panel) return null;
  const reach = Math.max(robotRadius(panel.cell) + 2, panel.cell / 2);
  let best = Infinity, hitCell = null;
  frame.positions.forEach(([r, c]) => {
    const d = Math.hypot(px - (panel.left + (c + .5) * panel.cell), py - (panel.top + (r + .5) * panel.cell));
    if (d <= reach && d < best) { best = d; hitCell = [r, c]; }
  });
  const c = Math.floor((px - panel.left) / panel.cell), r = Math.floor((py - panel.top) / panel.cell);
  const [row, col] = hitCell || [r, c];
  if (row < 0 || col < 0 || row >= geometry.rows || col >= geometry.cols) return null;
  const robots = frame.positions.flatMap(([pr, pc], i) => pr === row && pc === col ? [i] : []);
  return { panel, row, col, robots: hitCell ? robots : [] };
}

/** Robot groups at one step, from the links (robots joined directly or through a chain). */
function groupsOf(frame) {
  const parent = frame.positions.map((_, i) => i);
  const find = i => parent[i] === i ? i : (parent[i] = find(parent[i]));
  for (const [i, j] of frame.links) parent[find(i)] = find(j);
  return frame.positions.map((_, i) => find(i));
}

const robotList = ids => ids.length === 1 ? `robot ${ids[0]}` : `robots ${ids.slice(0, -1).join(", ")} and ${ids.at(-1)}`;
const swatch = color => node("i", { className: "swatch", style: `background:${color}` });

function robotDetails(i, frame, replay) {
  const { header, frames, cursor } = replay;
  const [r, c] = frame.positions[i];
  const range = rangeText(header.range);
  const lines = [];
  if (header.robots === 1) {
    lines.push("A single robot: there are no teammates to share maps with.");
  } else {
    const direct = frame.links.flatMap(([a, b]) => a === i ? [b] : b === i ? [a] : []).sort((a, b) => a - b);
    const roots = groupsOf(frame);
    const group = roots.flatMap((root, j) => root === roots[i] && j !== i ? [j] : []);
    if (rangeValue(header.range) === 0) lines.push("Communication range is 0, so it never shares its map.");
    else if (direct.length) lines.push(`Directly linked to ${robotList(direct)} (closer than ${range} cells).`);
    else lines.push(`No teammate is closer than ${range} cells, so it has no direct links.`);
    if (group.length) {
      lines.push(`Shares one combined map this step with ${robotList(group)}` +
        (group.length > direct.length ? ", some through a chain of links." : "."));
    } else {
      lines.push("On its own: it works only from its own map, so it can rediscover cells its teammates have already seen.");
      if (rangeValue(header.range) > 0) {
        let last = -1;
        for (let t = cursor - 1; t >= 0 && last < 0; t--) {
          if (frames[t].links.some(([a, b]) => a === i || b === i)) last = frames[t].t;
        }
        lines.push(last < 0 ? "It has not been linked to a teammate since the start." :
          `Last linked to a teammate at step ${last} (${frame.t - last} step${frame.t - last === 1 ? "" : "s"} ago).`);
      }
    }
  }
  if (cursor > 0) {
    const [pr, pc] = frames[cursor - 1].positions[i];
    lines.push(pr === r && pc === c ? "Stayed in place this step." : `Moved here from (${pr}, ${pc}) this step.`);
  }
  lines.push(`Sees open cells and walls up to ${header.sensing} cells away; walls block its view.`);
  return [
    node("strong", {}, node("i", { className: "tip-robot", style: `background:${robotColor(i)}` }), `Robot ${i} · cell (${r}, ${c})`),
    ...lines.map(text => node("p", { textContent: text })),
  ];
}

function cellDetails(hit, frame, replay) {
  const { header, walls, firstSeen, firstRepeat, cols } = { ...replay, cols: replay.header.shape[1] };
  const i = hit.row * cols + hit.col;
  const t = frame.t, seenAt = firstSeen[i], repeatAt = firstRepeat[i];
  const seen = seenAt >= 0 && seenAt <= t;
  const later = seenAt > t ? ` The team first sees it at step ${seenAt}.` : "";
  const [er, ec] = header.entrance;
  const title = `Cell (${hit.row}, ${hit.col})${hit.row === er && hit.col === ec ? " · entrance" : ""}`;
  let color, label, text;
  if (hit.panel.kind === "truth") {
    color = walls[i] ? TRUTH.wall : TRUTH.open;
    label = walls[i] ? "Wall on the true map" : "Open ground on the true map";
    text = [
      "The simulator's full map. Robots never read it; they learn about a cell only by seeing it or from a linked teammate.",
      seen ? `At step ${t} the team has seen this cell (first at step ${seenAt}).` : `At step ${t} no robot has seen this cell yet.${later}`,
    ];
  } else if (walls[i]) {
    color = seen ? MAP.wall : MAP.wallUnseen;
    label = seen ? "Wall, seen" : "Wall, not yet seen";
    text = seen ? [`First seen at step ${seenAt}. Robots that know about it plan their paths around it.`] :
      [`No robot has seen this wall yet, so no robot knows it is here. It is drawn a little darker than other unseen cells only to make the layout readable.${later}`];
  } else if (!seen) {
    color = MAP.unknown;
    label = "Not yet seen";
    text = [`No robot has seen this open cell yet, so it does not count toward coverage.${later}`];
  } else if (repeatAt >= 0 && repeatAt <= t && $("show-repeated").checked) {
    color = MAP.repeated;
    label = "Repeated discovery";
    text = [
      `First seen by the team at step ${seenAt}. At step ${repeatAt} a robot that had not received that information saw it again for itself, which added to wasted effort (W).`,
      "Any later rediscoveries of this cell count toward W too.",
    ];
  } else {
    color = MAP.seen;
    label = "Seen by the team";
    text = [`First seen at step ${seenAt}.` + (repeatAt >= 0 && repeatAt <= t ?
      ` Rediscovered at step ${repeatAt} (orange shading is turned off).` : " No robot has rediscovered it so far."),
      "Grey means at least one robot has seen it. Robots that have not been in contact since may still think it is unknown."];
  }
  return [
    node("strong", {}, swatch(color), title),
    node("p", {}, node("b", { textContent: label + ". " }), text[0]),
    ...text.slice(1).map(line => node("p", { textContent: line })),
  ];
}

function updateTip() {
  const tip = $("map-tip");
  const hit = mapPointer && mapHit(mapPointer.x, mapPointer.y);
  if (!hit) { tip.hidden = true; return; }
  const replay = state.replay, frame = replay.frames[replay.cursor];
  const parts = hit.robots.flatMap((i, k) => [...(k ? [node("hr")] : []), ...robotDetails(i, frame, replay)]);
  if (parts.length) parts.push(node("hr"));
  parts.push(...cellDetails(hit, frame, replay));
  tip.replaceChildren(...parts);
  tip.hidden = false;
  const stage = $("map-stage").getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight;
  const left = mapPointer.x + 16 + w > stage.width ? mapPointer.x - 16 - w : mapPointer.x + 16;
  const top = Math.min(Math.max(4, mapPointer.y + 16), Math.max(4, stage.height - h - 4));
  tip.style.left = Math.max(4, left) + "px";
  tip.style.top = top + "px";
}

// ---------------------------------------------------------------------------
// Playback speed
// ---------------------------------------------------------------------------

const LIVE_STEPS_PER_SECOND = 20;  // 1x replays at the pace a live trial runs
const SPEEDS = [0.25, 0.5, 1, 1.5, 2, 4, 8];

function setSpeed(value) {
  $("speed").value = String(value);
  try { localStorage.setItem("fieldwork-speed", String(value)); } catch { /* storage unavailable */ }
}

function stepSpeed(direction) {
  const index = SPEEDS.indexOf(Number($("speed").value));
  setSpeed(SPEEDS[Math.max(0, Math.min(SPEEDS.length - 1, index + direction))]);
}

let lastTick = null, carry = 0;
function playbackTick(now) {
  requestAnimationFrame(playbackTick);
  const replay = state.replay;
  if (!replay.playing) { lastTick = null; carry = 0; return; }
  if (lastTick !== null) carry += (now - lastTick) / 1000 * LIVE_STEPS_PER_SECOND * Number($("speed").value);
  lastTick = now;
  const steps = Math.floor(carry);
  if (!steps) return;
  carry -= steps;
  const end = replay.frames.length - 1;
  if (replay.cursor < end) replay.cursor = Math.min(end, replay.cursor + steps);
  else if (replay.drained || !replay.key) replay.playing = false;
  renderReplay();
}

function drawChart(replay) {
  const surface = canvasContext($("chart"));
  const metric = $("chart-metric").value;
  const series = SERIES[metric];
  const { frames, cursor } = replay;
  const values = frames.map(f => f[metric]);
  const defined = values.filter(v => v !== null && v !== undefined);
  $("chart-note").hidden = !frames.length || defined.length > 0;
  $("chart-note").textContent = replay.header?.robots === 1 ? "This measure is not defined for a single robot." :
    "This measure was not recorded in this run's replay.";
  if (!surface || !defined.length) return;
  const { ctx, width, height } = surface;
  const tStep = niceStep(Math.max(frames.length - 1, 10) / 5);
  const maxT = Math.ceil(Math.max(frames.length - 1, 10) / tStep) * tStep;
  const top = Math.max(...defined);
  const maxY = metric === "coverage" ? 1 : metric === "groups" ? Math.max(1, replay.header.robots) : niceCeil(top);
  const left = 58, right = width - 14, upper = 10, bottom = height - 36;
  const x = t => left + t / maxT * (right - left);
  const y = v => bottom - v / maxY * (bottom - upper);
  chartGeometry = { left, right, maxT };
  ctx.font = "11px ui-monospace, monospace";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#9ea8ae";
  for (let i = 0; i <= 4; i++) {
    const v = i * maxY / 4;
    ctx.strokeStyle = "#2a3034";
    ctx.beginPath(); ctx.moveTo(left, y(v)); ctx.lineTo(right, y(v)); ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(series.format ? series.format(v) : fmtTick(v), left - 7, y(v) + 4);
  }
  ctx.textAlign = "center";
  for (let t = 0; t <= maxT; t += tStep) ctx.fillText(t.toLocaleString(), x(t), bottom + 15);
  ctx.font = "11px system-ui";
  ctx.fillText("Simulation step", (left + right) / 2, height - 3);
  ctx.save();
  ctx.translate(12, (upper + bottom) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(series.axis || series.label, 0, 0);
  ctx.restore();
  ctx.strokeStyle = series.color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  let pen = false;
  const stride = Math.max(1, Math.floor(frames.length / (right - left)));
  for (let i = 0; i < frames.length; i += i + stride < frames.length ? stride : Math.max(1, frames.length - 1 - i)) {
    const v = values[i];
    if (v === null || v === undefined) { pen = false; continue; }
    if (pen) ctx.lineTo(x(i), y(v)); else ctx.moveTo(x(i), y(v));
    pen = true;
  }
  ctx.stroke();
  ctx.strokeStyle = "#5b656c";
  ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(x(cursor), upper); ctx.lineTo(x(cursor), bottom); ctx.stroke();
  ctx.setLineDash([]);
  const v = values[cursor];
  if (v !== null && v !== undefined) {
    ctx.fillStyle = series.color;
    ctx.beginPath(); ctx.arc(x(cursor), y(v), 4, 0, 2 * Math.PI); ctx.fill();
  }
}

let chartGeometry = null;
function niceStep(v) {
  const p = 10 ** Math.floor(Math.log10(Math.max(v, 1)));
  return [1, 2, 5, 10].map(m => m * p).find(m => m >= v);
}

function niceCeil(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map(m => m * p).find(m => m >= v);
}
const fmtTick = v => Number.isInteger(v) ? v.toLocaleString() : v.toFixed(v < 1 ? 2 : 1);

function renderReplay() {
  const replay = state.replay;
  const frame = replay.frames[replay.cursor];
  const count = replay.frames.length;
  const header = replay.header;
  $("timeline").max = Math.max(0, count - 1);
  $("timeline").value = replay.cursor;
  $("timeline").disabled = count < 2;
  $("timeline-label").textContent = frame ? `step ${frame.t} / ${count - 1}` : "—";
  $("play").disabled = count < 2;
  $("play").textContent = replay.playing ? "❚❚" : "▶";
  $("play").title = replay.playing ? "Pause" : "Play from here";
  $("play").setAttribute("aria-label", $("play").title);
  const live = isActive() && state.job?.key === replay.key;
  $("latest").hidden = !live;
  $("latest").disabled = replay.follow;
  $("save-map").disabled = !frame;
  $("save-chart").disabled = !frame;
  $("metric-coverage").textContent = frame ? pct(frame.coverage) : "—";
  $("metric-wasted").textContent = frame ? frame.wasted.toLocaleString() : "—";
  $("metric-wpf").textContent = frame ? `W / F = ${(frame.wasted / header.open).toFixed(3)}` : "";
  $("metric-step").textContent = frame ? frame.t.toLocaleString() : "—";
  $("metric-cap").textContent = header?.cap ? `cap ${header.cap.toLocaleString()}` : "";
  $("metric-groups").textContent = frame ? frame.groups.toLocaleString() : "—";
  updateMapHeader();
  const truth = $("show-truth").checked;
  $("map-stage").classList.toggle("split", truth);
  for (const item of all(".truth-only")) item.hidden = !truth;
  if (state.panel === "live") {
    drawMap(replay, frame);
    drawChart(replay);
    updateTip();
  }
}

/** Copy a canvas onto a captioned PNG so a saved figure says what it shows. */
function exportCanvas(canvas, title, lines, filename) {
  const scale = window.devicePixelRatio || 1;
  const pad = 16, head = 26 + lines.length * 17;
  const png = document.createElement("canvas");
  png.width = canvas.width + 2 * pad * scale;
  png.height = canvas.height + (head + pad + 6) * scale;
  const ctx = png.getContext("2d");
  ctx.fillStyle = "#111416";
  ctx.fillRect(0, 0, png.width, png.height);
  ctx.drawImage(canvas, pad * scale, (head + 6) * scale);
  ctx.scale(scale, scale);
  ctx.fillStyle = "#eef1f3";
  ctx.font = "bold 15px system-ui";
  ctx.fillText(title, pad, 22);
  ctx.fillStyle = "#9ea8ae";
  ctx.font = "12px system-ui";
  lines.forEach((line, i) => ctx.fillText(line, pad, 40 + i * 17));
  png.toBlob(blob => { if (blob) saveBlob(blob, filename); });
}

function frameSummary() {
  const { header, frames, cursor } = state.replay;
  const f = frames[cursor];
  return `Step ${f.t}: coverage ${pct(f.coverage)}, W = ${f.wasted} (W / F = ${(f.wasted / header.open).toFixed(3)}), ${f.groups} group${f.groups === 1 ? "" : "s"}`;
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

function runLabel(run) {
  const c = run.config;
  const kind = run.kind === "trial" ? "Live trial" : run.kind === "sweep" ? "Sweep" : "Sweep (command line)";
  return run.kind === "command line" ? `${run.name}` : `${kind}${c.preset ? " · " + c.preset : ""}`;
}

function runMeta(run) {
  const c = run.config;
  const ranges = (c.comm_ranges || []).map(r => rangeValue(r) === Infinity ? "∞" : r).join(", ");
  return run.kind === "trial" ?
    `N ${c.team_sizes?.[0]} · range ${ranges} · trial ${c.trial_index ?? 0}` :
    `N ${c.team_sizes?.join(", ")} · range ${ranges} · ${c.trials} trials`;
}

function runDate(run) {
  return new Date(run.updated * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function currentRun() {
  return state.results.find(r => r.key === state.run.key);
}

function renderResults() {
  if (!state.setup) return;
  const results = state.results;
  $("results-empty").hidden = results.length > 0;
  $("run-detail").hidden = !results.length;
  $("result-count").textContent = results.length ? String(results.length) : "";
  if (!results.length) return;
  if (!currentRun()) state.run.key = results[0].key;
  $("runs").replaceChildren(...results.map(run => {
    const chip = run.status !== "complete" ? node("span", { className: "chip warn", textContent: run.status }) :
      run.trials_failed ? node("span", { className: "chip bad", textContent: `${run.trials_failed} failed` }) : null;
    const item = node("button", { className: "run-item", role: "option", "aria-selected": String(run.key === state.run.key) },
      node("strong", {}, runLabel(run), chip), node("span", { textContent: runMeta(run) }), node("span", { textContent: runDate(run) }));
    item.addEventListener("click", () => { state.run.key = run.key; message("results-error", ""); renderResults(); });
    return item;
  }));
  const run = currentRun();
  renderRunSummary(run);
  void loadRun(run);
  updateRunButtons();
}

function renderRunSummary(run) {
  const c = run.config, d = state.setup.defaults;
  $("run-title").textContent = runLabel(run) + (run.kind === "command line" ? "" : " · " + runDate(run));
  $("run-path").textContent = run.path + "/";
  const items = [
    [run.kind === "trial" ? "Robots (N)" : "Team sizes", c.team_sizes?.join(", ")],
    [run.kind === "trial" ? "Comm. range" : "Comm. ranges", c.comm_ranges?.map(rangeText).join(", ")],
    run.kind === "trial" ? ["Trial", c.trial_index ?? 0] : ["Trials per setting", c.trials],
    ["Master seed", c.master_seed, c.master_seed !== d.master_seed],
    ["Replanning", c.replan === "immediate" ? "Immediate" : "On arrival (check run)", c.replan !== d.replan],
    ["Obstacles", Math.round(c.obstacle_density * 100) + "%", c.obstacle_density !== d.obstacle_density],
    ["Open cells / robot", c.open_cells_per_robot, c.open_cells_per_robot !== d.open_cells_per_robot],
    ["Sensing radius", c.sensing_radius, c.sensing_radius !== d.sensing_radius],
    ["Step cap", `${c.step_cap_factor} × F`, c.step_cap_factor !== d.step_cap_factor],
    ["Runtime", run.runtime === null || run.runtime === undefined ? "—" : `${fmt(run.runtime, 1)} s`],
    ["Code version", run.git?.commit ? run.git.commit.slice(0, 7) + (run.git.uncommitted_changes ? " + local edits" : "") : "—"],
  ];
  $("run-settings").replaceChildren(...items.map(([label, value, changed]) =>
    node("div", {}, node("dt", { textContent: label }), node("dd", { textContent: value ?? "—", className: changed ? "changed" : "", title: changed ? "Differs from the plan value" : "" }))));
  $("replay").hidden = !run.has_replay;
  $("figure-path").textContent = run.figure_path + "/";
  const figures = run.figures.filter(n => n.endsWith(".png"));
  $("figures").replaceChildren(...figures.map(name => {
    const label = state.setup.figures[name] || name;
    return node("figure", {},
      node("figcaption", {}, label, link(run.key, name, "Download")),
      node("a", { href: fileUrl(run.key, name), target: "_blank", rel: "noopener" },
        node("img", { src: fileUrl(run.key, name) + "&v=" + run.updated, alt: label, loading: "lazy" })));
  }));
  $("tables").replaceChildren(...run.figures.filter(n => n.endsWith(".csv")).map(n => link(run.key, n, state.setup.figures[n])));
  $("analyze").textContent = run.figures.length ? "Regenerate figures" : "Generate figures";
  $("downloads").replaceChildren(
    ...run.files.map(n => link(run.key, n, `${state.setup.data_files[n]} · ${n}`)),
    ...run.logs.filter(n => !run.files.includes(n)).map(n => link(run.key, n, n)));
}

function renderAlerts(run) {
  const alerts = [];
  const { rows, failures } = state.run;
  if (["incomplete", "failed", "stopped"].includes(run.status) && !failures.length) {
    alerts.push(node("div", { className: "alert warn", textContent: run.has_replay ?
      "This run ended before saving a summary. View the available replay or check the run log." :
      "This run did not finish and saved no results." }));
  }
  if (failures.length) {
    alerts.push(node("div", { className: "alert bad" },
      `${failures.length} failed trial${failures.length === 1 ? "" : "s"}. Setting counts show how many results were recorded. Affected paired comparisons have missing results.`,
      node("ul", {}, ...failures.slice(0, 5).map(f =>
        node("li", { textContent: `N = ${f.n_robots}, range ${rangeText(f.r_c)}, trial ${f.trial_index}: ${f.error}` })))));
  }
  const capped = rows.filter(r => r.stop_reason === "step_cap").length;
  if (capped) {
    alerts.push(node("div", { className: "alert warn", textContent:
      `${capped} trial${capped === 1 ? "" : "s"} hit the step cap before reaching full coverage. Their W counts the whole capped run, and steps to 100% is “not reached”.` }));
  }
  if (run.git?.uncommitted_changes) {
    alerts.push(node("div", { className: "alert warn", textContent:
      "The code had uncommitted edits when this ran, so the recorded commit alone may not reproduce it." }));
  }
  $("run-alerts").replaceChildren(...alerts);
}

async function loadRun(run) {
  const version = `${run.key}|${run.status}|${run.updated}`;
  if (state.run.version === version) { renderAlerts(run); return; }
  const sameRun = state.run.key === run.key && state.run.loadedKey === run.key;
  state.run.version = version;
  if (!sameRun) {
    Object.assign(state.run, { rows: [], settings: [], failures: [], selected: null, loadedKey: "" });
    for (const id of ["compare-card", "trials-card", "trial-card"]) $(id).hidden = true;
    $("run-alerts").replaceChildren();
  }
  try {
    const data = await api("/api/run?" + new URLSearchParams({ key: run.key }));
    if (state.run.version !== version) return;
    Object.assign(state.run, data, { loadedKey: run.key });
    if (!sameRun) {
      state.run.page = 0;
      state.run.selected = data.rows.length === 1 ? data.rows[0] : null;
      fillFilters();
    } else if (state.run.selected) {
      state.run.selected = data.rows.find(r => r.index === state.run.selected.index) || null;
    }
    renderAlerts(run);
    renderCompare();
    renderTrials();
    renderSelectedTrial();
  } catch (error) {
    if (state.run.version === version) {
      state.run.version = "";
      message("results-error", error.message);
    }
  }
}

function fillFilters() {
  const rows = state.run.rows;
  const sizes = [...new Set([...rows.map(r => Number(r.N)), ...state.run.settings.map(s => s.N)])].sort((a, b) => a - b);
  const ranges = [...new Set([...rows.map(r => r.r_c_label || r.r_c), ...state.run.settings.map(s => s.r_c_label)])].sort((a, b) => rangeValue(a) - rangeValue(b));
  $("filter-size").replaceChildren(new Option("All", ""), ...sizes.map(n => new Option(n, n)));
  $("filter-range").replaceChildren(new Option("All", ""), ...ranges.map(r => new Option(rangeText(r), r)));
}

function renderCompare() {
  const settings = state.run.settings;
  $("compare-card").hidden = settings.length < 2 && state.run.rows.length < 2 && !settings.some(s => s.trials < s.expected);
  const size = $("filter-size").value, range = $("filter-range").value;
  $("compare-rows").replaceChildren(...settings.map(s => {
    const short = s.trials < s.expected;
    const tr = node("tr", { "aria-selected": String(String(s.N) === size && s.r_c_label === range) },
      node("td", { textContent: s.N }),
      node("td", {}, node("button", { type: "button", className: "link-button", disabled: !s.trials,
        textContent: rangeText(s.r_c_label), "aria-label": `Show trials for N ${s.N}, range ${s.r_c_label}` })),
      node("td", { textContent: s.expected ? `${s.trials} / ${s.expected}` : s.trials, className: "num" + (short ? " orange" : "") }),
      node("td", { textContent: s.trials ? `${s.completed} / ${s.trials}` : "—", className: "num" + (s.completed < s.trials ? " orange" : "") }),
      node("td", { textContent: s.W_per_F_median === null ? "—" : `${fmt(s.W_per_F_median, 3)} (${fmt(s.W_per_F_q25, 3)}–${fmt(s.W_per_F_q75, 3)})`, className: "num" }),
      node("td", { textContent: s.reached_90 < s.trials ? `${fmt(s.t90_median)} (${s.reached_90} reached)` : fmt(s.t90_median), className: "num" }));
    tr.querySelector("button").addEventListener("click", () => {
      $("filter-size").value = String(s.N);
      $("filter-range").value = s.r_c_label;
      state.run.page = 0;
      renderCompare();
      renderTrials();
      $("trials-card").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return tr;
  }));
}

const TRIAL_COLUMNS = [
  { key: "trial", label: "Trial", num: true },
  { key: "N", label: "N", num: true },
  { key: "r_c_label", label: "Range", show: r => rangeText(r.r_c_label || r.r_c), sort: r => rangeValue(r.r_c_label || r.r_c) },
  { key: "steps", label: "Steps", num: true },
  { key: "final_coverage", label: "Coverage", num: true, show: r => pct(r.final_coverage) },
  { key: "W", label: "W", num: true },
  { key: "W_per_F", label: "W / F", num: true, show: r => fmt(r.W_per_F, 3) },
  { key: "t90", label: "Steps to 90%", num: true, show: r => num(r.t90) === null ? "not reached" : fmt(r.t90) },
  { key: "stop_reason", label: "Stopped by", show: r => r.stop_reason === "step_cap" ? "step cap" : "full coverage" },
];

function filteredTrials() {
  const size = $("filter-size").value, range = $("filter-range").value;
  const { key, dir } = state.run.sort;
  const column = TRIAL_COLUMNS.find(c => c.key === key);
  const value = column ? (column.sort || (r => column.num ? num(r[key]) : r[key])) : null;
  const sign = dir === "asc" ? 1 : -1;
  const bySetting = (a, b) => a.N - b.N || rangeValue(a.r_c_label || a.r_c) - rangeValue(b.r_c_label || b.r_c) || a.trial - b.trial;
  return state.run.rows
    .filter(r => (!size || String(r.N) === size) && (!range || (r.r_c_label || r.r_c) === range))
    .sort((a, b) => {
      if (!value) return bySetting(a, b);
      const va = value(a), vb = value(b);
      if (va === null || vb === null) return (va === null) - (vb === null) || bySetting(a, b);
      return (va < vb ? -1 : va > vb ? 1 : 0) * sign || bySetting(a, b);
    });
}

function renderTrials() {
  const run = state.run;
  $("trials-card").hidden = run.rows.length < 2;
  $("trial-head").replaceChildren(...TRIAL_COLUMNS.map(c => {
    const b = node("button", { type: "button", textContent: c.label, title: "Sort" });
    if (run.sort.key === c.key) b.dataset.dir = run.sort.dir;
    b.addEventListener("click", () => {
      run.sort = { key: c.key, dir: run.sort.key === c.key && run.sort.dir === "desc" ? "asc" : "desc" };
      run.page = 0;
      renderTrials();
    });
    return node("th", { className: c.num ? "num" : "", scope: "col",
      "aria-sort": run.sort.key === c.key ? (run.sort.dir === "asc" ? "ascending" : "descending") : "none" }, b);
  }), node("th", { scope: "col", textContent: "Details" }));
  const rows = filteredTrials();
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  run.page = Math.min(run.page, pages - 1);
  const offset = run.page * PAGE_SIZE;
  $("trial-rows").replaceChildren(...rows.slice(offset, offset + PAGE_SIZE).map(r => {
    const tr = node("tr", { "aria-selected": String(r.index === run.selected?.index) },
      ...TRIAL_COLUMNS.map(c => node("td", {
        textContent: c.show ? c.show(r) : r[c.key],
        className: (c.num ? "num" : "") + (c.key === "stop_reason" && r.stop_reason === "step_cap" ? " orange" : ""),
      })));
    const choose = () => { selectTrial(r); $("trial-card").scrollIntoView({ behavior: "smooth", block: "nearest" }); };
    const button = node("button", { type: "button", className: "link-button", textContent: r === run.selected ? "Selected" : "Inspect",
      "aria-label": `Inspect trial ${r.trial}, N ${r.N}, range ${rangeText(r.r_c_label || r.r_c)}` });
    button.addEventListener("click", choose);
    tr.append(node("td", {}, button));
    return tr;
  }));
  $("trial-count").textContent = rows.length ?
    `${offset + 1}–${Math.min(offset + PAGE_SIZE, rows.length)} of ${rows.length} trials. Select a column to sort.` : "No trials match.";
  $("trial-prev").disabled = run.page === 0;
  $("trial-next").disabled = run.page >= pages - 1;
}

function selectTrial(row) {
  state.run.selected = row;
  renderTrials();
  renderSelectedTrial();
}

function videoPrefix(row, watch) {
  return `N${row.N}_rc${row.r_c_label || row.r_c}_trial${row.trial}_robot${watch}.`;
}

function renderSelectedTrial() {
  const row = state.run.selected;
  $("trial-card").hidden = !row;
  if (!row) return;
  $("trial-title").textContent = `Trial ${row.trial} · N = ${row.N} · range ${rangeText(row.r_c_label || row.r_c)}`;
  $("trial-subtitle").textContent = row.stop_reason === "step_cap" ?
    `Hit the step cap at ${fmt(row.steps)} steps with ${pct(row.final_coverage)} coverage. W = ${fmt(row.W)}.` :
    `Reached full coverage in ${fmt(row.steps)} steps. W = ${fmt(row.W)} (W / F = ${fmt(row.W_per_F, 3)}).`;
  const pairs = new Map(state.run.rows.filter(r => r.N === row.N && r.trial === row.trial)
    .map(r => [rangeText(r.r_c_label || r.r_c), r]));
  const ranges = [...new Set([...currentRun().config.comm_ranges.map(rangeText), ...pairs.keys()])].sort((a, b) => rangeValue(a) - rangeValue(b));
  $("paired-trials").hidden = ranges.length < 2;
  $("pair-rows").replaceChildren(...ranges.map(range => {
    const r = pairs.get(range);
    if (!r) return node("tr", {}, node("td", { textContent: range }),
      node("td", { colSpan: 4, className: "orange", textContent: "No recorded result" }));
    const button = node("button", { type: "button", className: "link-button", textContent: range,
      "aria-label": `Inspect paired trial at range ${range}` });
    const tr = node("tr", { "aria-selected": String(r.index === row.index) },
      node("td", {}, button), node("td", { textContent: fmt(r.steps), className: "num" }),
      node("td", { textContent: fmt(r.W), className: "num" }), node("td", { textContent: fmt(r.W_per_F, 3), className: "num" }),
      node("td", { textContent: r.stop_reason === "step_cap" ? "step cap" : "full coverage", className: r.stop_reason === "step_cap" ? "orange" : "" }));
    button.addEventListener("click", () => selectTrial(r));
    return tr;
  }));
  const watch = $("video-watch");
  const keep = watch.value !== "" && Number(watch.value) < Number(row.N) ? watch.value : "0";
  watch.replaceChildren(...Array.from({ length: Number(row.N) }, (_, i) => new Option(`Robot ${i}`, i)));
  watch.value = keep;
  updateRunButtons();
}

async function renderVideo(run, row, watch) {
  const target = $("video-preview");
  const prefix = videoPrefix(row, watch);
  const legacy = `trial-${row.index}-robot-${watch}.`;
  const name = run.videos.find(v => v.startsWith(prefix) && v.endsWith(".mp4")) ||
    run.videos.find(v => v.startsWith(prefix)) || run.videos.find(v => v.startsWith(legacy));
  const updated = state.job?.key === run.key && state.job.video === name && state.job.status === "complete"
    ? state.job.finished : run.updated;
  const id = `${run.key}|${name}|${updated}`;
  if (target.dataset.id === id) return;
  target.dataset.id = id;
  if (!name) { target.replaceChildren(); return; }
  const src = fileUrl(run.key, name) + "&v=" + updated;
  const media = name.endsWith(".mp4") ? node("video", { src, controls: true, muted: true, playsInline: true }) : node("img", { src, alt: "Exploration video" });
  const check = node("span", { className: "chip", textContent: "checking…" });
  target.replaceChildren(media, node("div", { className: "row-actions" },
    node("a", { className: "secondary", href: src, download: `fieldwork-${prefix}${name.split(".").at(-1)}`, textContent: "Download video" }), check));
  try {
    const response = await fetch(fileUrl(run.key, prefix + "json"));
    const result = response.ok ? await response.json() : null;
    check.className = "chip " + (result ? (result.match ? "good" : "bad") : "");
    check.textContent = result ? (result.match ? (result.mismatches ? "Saved summary matches" : "Steps, repeats, and stop match") : "Summary mismatch") : "not checked";
    check.title = result?.detail || "Rendered before reproduction checks were added.";
  } catch { check.textContent = "not checked"; }
}

function updateRunButtons() {
  const run = currentRun();
  if (!run || !state.setup) return;
  const active = isActive();
  const analysis = state.job?.action === "analyze" && state.job.key === run.key ? state.job : null;
  $("analyze").disabled = !run.can_analyze || active || state.pending;
  $("analyze").title = run.can_analyze ? "" : "This run has no finished results.";
  $("stop-analysis").hidden = !analysis || !isActive(analysis);
  const analysisText = { running: "Generating figures…", stopping: "Stopping…", complete: "Figures and tables are up to date.",
    stopped: "Figure generation stopped.", failed: `Figure generation failed: ${analysis?.message}` };
  message("analysis-status", analysis && analysisText[analysis.status], analysis?.status === "failed" ? "bad" : analysis?.status === "complete" ? "good" : "");

  const row = state.run.selected;
  if (!row) return;
  const watch = Number($("video-watch").value || 0);
  const video = state.job?.action === "video" && state.job.key === run.key ? state.job : null;
  const sameVideo = video?.video?.startsWith(videoPrefix(row, watch));
  $("render-video").disabled = active || state.pending || !run.can_analyze;
  $("stop-video").hidden = !video || !isActive(video);
  const has = run.videos.some(v => v.startsWith(videoPrefix(row, watch)));
  $("render-video").textContent = has ? "Render again" : `Render ${state.setup.video_format.toUpperCase()}`;
  let text = "", tone = "";
  if (video && isActive(video)) text = sameVideo ? "Re-running this trial and rendering…" : "Rendering another trial…";
  else if (sameVideo && video.status === "failed") { text = `Render failed: ${video.message}`; tone = "bad"; }
  message("video-status", text, tone);
  void renderVideo(run, row, watch);
}

function loadIntoWorkspace(config, overrides = {}) {
  const mode = overrides.mode || "trial";
  chooseMode(mode);
  useConfig({ ...config, ...overrides });
  $("preset").value = mode === "sweep" && config.preset in state.setup.presets ? config.preset : "";
  choosePanel("live");
  $("start").focus();
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

for (const b of all("[data-panel]")) b.addEventListener("click", () => choosePanel(b.dataset.panel));
for (const b of all("[data-mode]")) {
  b.addEventListener("click", () => {
    if (state.mode === b.dataset.mode) return;
    chooseMode(b.dataset.mode);
    const draft = state.drafts[state.mode];
    if (draft) {
      FORM_FIELDS.forEach((id, i) => { $(id).value = draft[i]; });
      updateFormNotes();
    } else if (state.setup) {
      $("preset").value = state.mode === "sweep" && state.setup.presets.core ? "core" : "";
      useConfig(state.mode === "sweep" ? { ...state.setup.presets[$("preset").value] } : {});
    }
  });
}
$("preset").addEventListener("change", () => useConfig({ ...state.setup.presets[$("preset").value] }));
$("parameters").addEventListener("input", event => {
  if (event.target.id !== "preset" && state.mode === "sweep") $("preset").value = "";
  updateFormNotes();
});
$("reset-plan").addEventListener("click", () => {
  const d = state.setup.defaults;
  for (const id of PLAN_FIELDS) $(id).value = id === "obstacle_density" ? Math.round(d[id] * 100) : d[id];
  if (state.mode === "sweep") $("preset").value = "";
  updateFormNotes();
});
$("run-form").addEventListener("submit", event => {
  event.preventDefault();
  try { void perform("/api/start", readConfig(), "run-error"); }
  catch (error) { message("run-error", error.message); }
});
$("stop").addEventListener("click", () => perform("/api/stop", {}, "run-error"));
$("sweep-results").addEventListener("click", () => { state.run.key = state.job.key; choosePanel("results"); });

$("timeline").addEventListener("input", () => {
  Object.assign(state.replay, { follow: false, playing: false, cursor: $("timeline").valueAsNumber });
  renderReplay();
});
$("latest").addEventListener("click", () => {
  Object.assign(state.replay, { follow: true, playing: false, cursor: state.replay.frames.length - 1 });
  renderReplay();
});
$("play").addEventListener("click", () => {
  const replay = state.replay;
  replay.follow = false;
  replay.playing = !replay.playing;
  if (replay.playing && replay.cursor >= replay.frames.length - 1) replay.cursor = 0;
  renderReplay();
});
document.addEventListener("keydown", event => {
  if (state.panel !== "live" || ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(event.target.tagName)) return;
  const replay = state.replay;
  if (event.key === ">" || event.key === "<") {
    event.preventDefault();
    stepSpeed(event.key === ">" ? 1 : -1);
    return;
  }
  if ((event.key === " " || event.key === "k") && replay.frames.length > 1) {
    event.preventDefault();
    $("play").click();
    return;
  }
  if (!replay.frames.length || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  const delta = (event.key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 10 : 1);
  Object.assign(replay, { follow: false, playing: false, cursor: Math.max(0, Math.min(replay.frames.length - 1, replay.cursor + delta)) });
  renderReplay();
});
$("save-map").addEventListener("click", () => {
  const { header, cursor } = state.replay;
  const legend = ["Grey: seen by the team", "dark: unseen", "black: walls"];
  if ($("show-repeated").checked) legend.push("orange: repeated discoveries");
  if ($("show-links").checked) legend.push("blue: links");
  if ($("show-truth").checked) legend.push("right or below: the true map (light: open, black: walls)");
  exportCanvas($("map"), mapTitle(header), [frameSummary(), legend.join(" · ")],
    `fieldwork-N${header.robots}-rc${header.range}-trial${header.trial}-step${state.replay.frames[cursor].t}.png`);
});
$("save-chart").addEventListener("click", () => {
  const { header } = state.replay;
  const metric = $("chart-metric").value;
  exportCanvas($("chart"), SERIES[metric].label + " over simulation steps",
    [mapTitle(header), "Dashed line marks " + frameSummary().toLowerCase()],
    `fieldwork-N${header.robots}-rc${header.range}-trial${header.trial}-${metric}.png`);
});
for (const input of [...all(".legend input"), $("chart-metric")]) input.addEventListener("change", renderReplay);
function seekFromChart(event) {
  const replay = state.replay;
  if (!chartGeometry || !replay.frames.length) return;
  const { left, right, maxT } = chartGeometry;
  const box = $("chart").getBoundingClientRect();
  const t = Math.round((event.clientX - box.left - left) / (right - left) * maxT);
  Object.assign(replay, { follow: false, playing: false, cursor: Math.max(0, Math.min(replay.frames.length - 1, t)) });
  renderReplay();
}
$("chart").addEventListener("pointerdown", event => {
  seekFromChart(event);
  $("chart").setPointerCapture(event.pointerId);
});
$("chart").addEventListener("pointermove", event => { if (event.buttons) seekFromChart(event); });
const resize = new ResizeObserver(() => renderReplay());
resize.observe($("map"));
resize.observe($("chart"));

requestAnimationFrame(playbackTick);
try {
  const saved = Number(localStorage.getItem("fieldwork-speed"));
  if (SPEEDS.includes(saved)) $("speed").value = String(saved);
} catch { /* storage unavailable: keep 1x */ }
$("speed").addEventListener("change", () => setSpeed(Number($("speed").value)));
$("map").addEventListener("pointermove", event => {
  const box = $("map").getBoundingClientRect();
  mapPointer = { x: event.clientX - box.left, y: event.clientY - box.top };
  updateTip();
});
$("map").addEventListener("pointerleave", () => { mapPointer = null; updateTip(); });

$("analyze").addEventListener("click", () => perform("/api/start", { action: "analyze", key: state.run.key }, "results-error"));
$("stop-analysis").addEventListener("click", () => perform("/api/stop", {}, "results-error"));
$("replay").addEventListener("click", () => {
  const run = currentRun();
  state.replay = newReplay(run.key);
  loadIntoWorkspace(run.config);
});
$("load-settings").addEventListener("click", () => {
  const run = currentRun();
  loadIntoWorkspace(run.config, { mode: run.kind === "trial" ? "trial" : "sweep" });
});
$("watch-live").addEventListener("click", () => {
  const row = state.run.selected;
  loadIntoWorkspace(currentRun().config, {
    team_sizes: [Number(row.N)], comm_ranges: [row.r_c_label || row.r_c], trial_index: Number(row.trial),
  });
});
for (const id of ["filter-size", "filter-range"]) {
  $(id).addEventListener("change", () => { state.run.page = 0; renderCompare(); renderTrials(); });
}
$("trial-prev").addEventListener("click", () => { state.run.page--; renderTrials(); });
$("trial-next").addEventListener("click", () => { state.run.page++; renderTrials(); });
$("video-watch").addEventListener("change", updateRunButtons);
$("render-video").addEventListener("click", () => perform("/api/start", {
  action: "video", key: state.run.key, index: state.run.selected.index, watch: Number($("video-watch").value),
}, "results-error"));
$("stop-video").addEventListener("click", () => perform("/api/stop", {}, "results-error"));

async function watchEngine() {
  try {
    await poll();
    $("engine").dataset.state = "up";
    $("engine").lastChild.textContent = "Local engine";
  } catch (error) {
    $("engine").dataset.state = "down";
    $("engine").lastChild.textContent = "Engine disconnected";
    console.warn(error);
  } finally {
    setTimeout(watchEngine, 300);
  }
}

async function initialize() {
  try {
    state.setup = await api("/api/setup");
    for (const key of Object.keys(state.setup.presets)) {
      $("preset").add(new Option(key.replaceAll("_", " "), key));
    }
    $("workers").max = state.setup.max_workers;
    chooseMode("trial");
    useConfig({});
    renderReplay();
    await watchEngine();
  } catch (error) {
    message("run-error", error.message);
  }
}
void initialize();
