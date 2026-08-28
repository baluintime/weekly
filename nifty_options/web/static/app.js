/* Nifty 50 options console -- polls the local engine and renders it.
   No build step, no external requests: the page is served by the same
   process that runs the strategies. */

const KEY = document.documentElement.dataset.key;
const $ = (id) => document.getElementById(id);
const rupee = (n, digits = 0) => {
  const value = Number(n || 0);
  const body = Math.abs(value).toLocaleString("en-IN", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
  return (value < 0 ? "−₹" : "₹") + body;
};
const signed = (n, digits = 0) => (n > 0 ? "+" : "") + rupee(n, digits);
const cls = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");

let state = {};
let journal = { rows: [], equity: [], track_a: {}, track_b: {} };
let lastLogId = 0;
let pendingMode = null;

/* ------------------------------------------------------------------ api */
async function api(path, body) {
  const options = body
    ? { method: "POST", headers: { "Content-Type": "application/json", "X-Dashboard-Key": KEY },
        body: JSON.stringify(body) }
    : {};
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  return type.includes("json") ? response.json() : response.text();
}

function toast(message, ok = true) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show " + (ok ? "ok" : "err");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.className = "toast"), 5200);
}

async function act(path, body, label) {
  try {
    const result = await api(path, body || {});
    toast(result.message || label || "Done", result.ok !== false);
    await refresh();
    return result;
  } catch (error) {
    toast(String(error), false);
    return { ok: false };
  }
}

/* --------------------------------------------------------------- render */
function renderConnection() {
  const upstox = state.upstox || {};
  $("connState").textContent = upstox.connected ? "connected" : "not connected";
  $("maskedKey").textContent = upstox.api_key || "—";
  $("tokenSource").textContent = upstox.source || "—";
  $("tokenExpiry").textContent = upstox.expires_at ? upstox.expires_at.slice(0, 16).replace("T", " ") : "—";

  const hasCreds = upstox.has_credentials;
  const editing = $("credForm").dataset.editing === "1";
  $("credForm").classList.toggle("hidden", hasCreds && !editing);
  $("connected").classList.toggle("hidden", !hasCreds || editing);
  $("connectPrompt").classList.toggle("hidden", !hasCreds || upstox.connected || editing);
  if (!$("redirectUri").value) $("redirectUri").value = upstox.redirect_uri || "";

  const login = state.login || {};
  if (login.status === "failed" && login.message !== renderConnection.lastLogin) {
    toast(login.message, false);
  }
  renderConnection.lastLogin = login.message;
}

function renderMode() {
  const live = state.mode === "live";
  const pill = $("modePill");
  pill.className = "mode-pill " + (live ? "live" : "paper");
  $("modeText").textContent = live ? (state.dry_run ? "live · dry run" : "live · real money") : "paper";
  document.querySelectorAll(".segmented button").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
  });

  const description = $("modeDescription");
  description.textContent = state.mode_description || "";
  description.className = "notice " + (live ? "danger" : "info");

  $("guards").innerHTML = (state.guards || [])
    .map((g) => `<li class="${g.ok ? "ok" : "no"}">
        <span class="mark">${g.ok ? "✓" : "!"}</span>
        <span>${escapeHtml(g.label)}</span>
        <span class="detail">${escapeHtml(g.detail || "")}</span>
      </li>`)
    .join("");

  $("confirmInput").placeholder = state.confirmation_phrase || "";

  const banner = $("banner");
  if (state.risk && state.risk.kill_switch) {
    banner.innerHTML = `<div class="notice danger row-notice" style="margin-top:1rem">
      <strong>Kill switch engaged.</strong> No new entries will be taken.
      <button id="resumeBtn" class="ghost" style="margin-left:auto">Release</button></div>`;
    $("resumeBtn").onclick = () => act("/api/resume", {}, "Kill switch released");
  } else if (live && !state.live_blocked) {
    banner.innerHTML = `<div class="notice danger" style="margin-top:1rem">
      <strong>Live trading is armed.</strong> Orders placed from here use real money.</div>`;
  } else {
    banner.innerHTML = "";
  }
}

function renderEngine() {
  const running = state.running;
  $("engineText").textContent = running ? "running" : "idle";
  $("engineDot").className = "dot" + (running ? " pulse" : "");
  $("enginePill").style.color = running ? "var(--good-text)" : "var(--text-secondary)";
  $("runBtn").textContent = running ? "Stop engine" : "Start engine";
  $("runBtn").className = running ? "" : "primary";
  $("tickBtn").disabled = running;
}

function renderTiles() {
  const broker = state.broker || {};
  const risk = state.risk || {};
  const overall = journal.overall || {};
  const realised = Number(overall.gross_pnl || 0);
  const unrealised = Number(broker.unrealized_pnl || 0);
  // risk.daily_loss_limit is reported as a negative floor; the kill switch
  // trips when realised P&L falls to it, so headroom is the gap to that floor.
  const limit = Math.abs(Number(risk.daily_loss_limit || 0));
  const headroom = limit + Number(risk.realized_pnl || 0);

  const tiles = [
    { k: "Realised P&L", v: signed(realised), c: cls(realised), s: `${overall.trades || 0} closed trades` },
    { k: "Open P&L", v: signed(unrealised), c: cls(unrealised), s: `${state.positions?.length || 0} open` },
    { k: "Win rate", v: (overall.win_rate || 0).toFixed(0) + "%", s: `${overall.wins || 0}W / ${overall.losses || 0}L` },
    {
      k: state.mode === "live" ? "Available margin" : "Simulated cash",
      v: rupee(broker.available_margin ?? broker.cash ?? 0),
      s: `of ${rupee(state.capital?.total || 0)} allocated`,
    },
    { k: "Charges", v: rupee(overall.charges || 0), s: "brokerage, STT, GST" },
    {
      k: "Daily loss headroom", v: rupee(Math.max(headroom, 0)),
      c: headroom <= 0 ? "neg" : "",
      s: `kill switch at ${rupee(limit)} down`,
    },
  ];
  $("tiles").innerHTML = tiles
    .map((t) => `<div class="tile"><div class="k">${t.k}</div>
      <div class="v ${t.c || ""}">${t.v}</div><div class="s">${t.s || ""}</div></div>`)
    .join("");
}

/** The per-track answer to "it has been an hour and nothing has happened". */
function renderWaiting() {
  const waiting = state.waiting_on || {};
  const tracks = Object.keys(waiting).sort();
  const card = $("waitingCard");
  card.classList.toggle("hidden", tracks.length === 0);
  if (!tracks.length) return;

  const tick = state.last_tick || {};
  $("waitingTime").textContent = tick.time
    ? `as of ${String(tick.time).slice(11, 16)}` + (tick.spot ? ` · spot ${Math.round(tick.spot)}` : "")
    : "";

  $("waiting").innerHTML = tracks
    .map((track) => {
      const reason = waiting[track];
      const entered = /^entered /.test(reason);
      return `<li class="${entered ? "ok" : "no"}">
        <span class="mark">${entered ? "✓" : "…"}</span>
        <span><strong>${escapeHtml(track)}</strong></span>
        <span class="detail" style="margin-left:auto;text-align:right;max-width:34rem;white-space:normal">
          ${escapeHtml(reason)}</span></li>`;
    })
    .join("");
}

function renderComparison() {
  const rows = [
    ["Trades", "trades", (v) => v],
    ["Win rate", "win_rate", (v) => v.toFixed(1) + "%"],
    ["Net P&L", "gross_pnl", (v) => signed(v, 2)],
    ["Average win", "avg_win", (v) => rupee(v, 2)],
    ["Average loss", "avg_loss", (v) => rupee(v, 2)],
    ["Expectancy / trade", "expectancy", (v) => signed(v, 2)],
    ["Profit factor", "profit_factor", (v) => (isFinite(v) ? v.toFixed(2) : "∞")],
    ["Sharpe (annualised)", "sharpe", (v) => v.toFixed(2)],
    ["Max drawdown", "max_drawdown", (v) => rupee(v, 2)],
    ["Charges", "charges", (v) => rupee(v, 2)],
  ];
  const a = journal.track_a || {}, b = journal.track_b || {};
  $("comparison").querySelector("tbody").innerHTML = rows
    .map(([label, key, format]) => {
      const va = Number(a[key] || 0), vb = Number(b[key] || 0);
      return `<tr><td>${label}</td>
        <td class="num ${key.includes("pnl") || key === "expectancy" ? cls(va) : ""}">${format(va)}</td>
        <td class="num ${key.includes("pnl") || key === "expectancy" ? cls(vb) : ""}">${format(vb)}</td></tr>`;
    })
    .join("");
}

function renderPositions() {
  const rows = state.positions || [];
  $("positionsEmpty").classList.toggle("hidden", rows.length > 0);
  $("positions").querySelector("tbody").innerHTML = rows
    .map((p) => {
      const legs = p.legs
        .map((l) => `${l.side === "BUY" ? "+" : "−"}${escapeHtml(l.symbol)} @ ${l.entry.toFixed(2)}`)
        .join("<br>");
      return `<tr title="${p.legs.length} leg(s)">
        <td><span class="tag ${p.track.endsWith("A") ? "a" : "b"}">${escapeHtml(p.track)}</span></td>
        <td>${escapeHtml(p.description)}<div class="mono" style="color:var(--muted)">${legs}</div></td>
        <td class="num">${p.lots}</td><td class="num">${p.entry.toFixed(2)}</td>
        <td class="num">${p.stop ? p.stop.toFixed(2) : "—"}</td>
        <td class="num">${p.target ? p.target.toFixed(2) : "—"}</td>
        <td>${escapeHtml(p.expiry || "—")}</td><td>${escapeHtml(p.opened_at)}</td></tr>`;
    })
    .join("");
}

function renderJournal() {
  const rows = journal.rows || [];
  $("journalPath").textContent = journal.path || "";
  $("journalEmpty").classList.toggle("hidden", rows.length > 0);
  $("journal").querySelector("tbody").innerHTML = rows
    .slice()
    .reverse()
    .map((r) => {
      const pnl = Number(r["Realized PnL (Rs)"] || 0);
      const track = r.Track || "";
      return `<tr>
        <td>${escapeHtml(r.Date)}</td>
        <td><span class="tag ${track.endsWith("A") ? "a" : "b"}">${escapeHtml(track)}</span></td>
        <td>${escapeHtml(r["Strategy / Legs"])}</td>
        <td class="num">${escapeHtml(r.Entry)}</td><td class="num">${escapeHtml(r.Exit)}</td>
        <td class="num">${escapeHtml(r["Net Points"])}</td>
        <td class="num ${cls(pnl)}">${signed(pnl, 2)}</td>
        <td style="color:var(--text-secondary)">${escapeHtml(r["Exit Reason"] || "")}</td></tr>`;
    })
    .join("");
}

/* --------------------------------------------------------------- charts */
const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

/** Ticks that fully CONTAIN [min, max] -- the axis is the plot's domain, so a
 *  range that stops short of the data would push marks outside the chart. */
function niceTicks(min, max, count = 4) {
  if (min === max) { min -= 1; max += 1; }
  const raw = (max - min) / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(Math.abs(raw) || 1)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || magnitude * 10;
  const ticks = [];
  const first = Math.floor(min / step) * step;
  const last = Math.ceil(max / step) * step;
  for (let t = first; t <= last + step * 1e-9; t += step) ticks.push(Number(t.toFixed(6)));
  return ticks;
}

/** Cumulative realised P&L, one line per track, with a hover crosshair. */
function renderEquityChart() {
  const host = $("equityChart");
  host.innerHTML = "";
  const rows = journal.rows || [];
  if (!rows.length) {
    host.innerHTML = '<p class="empty">Trades will plot here once the first one closes.</p>';
    $("equityMeta").textContent = "";
    return;
  }

  // Build a cumulative series per track over the shared trade sequence.
  const series = [
    { name: "Track A", color: "var(--series-1)", points: [] },
    { name: "Track B", color: "var(--series-2)", points: [] },
  ];
  let a = 0, b = 0;
  rows.forEach((row, index) => {
    const pnl = Number(row["Realized PnL (Rs)"] || 0);
    if ((row.Track || "").endsWith("A")) a += pnl; else b += pnl;
    series[0].points.push({ x: index + 1, y: a, date: row.Date });
    series[1].points.push({ x: index + 1, y: b, date: row.Date });
  });

  const W = 640, H = 260, pad = { t: 14, r: 54, b: 26, l: 16 };
  const values = series.flatMap((s) => s.points.map((p) => p.y)).concat([0]);
  const ticks = niceTicks(Math.min(...values), Math.max(...values));
  const yMin = Math.min(...ticks), yMax = Math.max(...ticks);
  const n = rows.length;
  const px = (x) => pad.l + ((x - 1) / Math.max(n - 1, 1)) * (W - pad.l - pad.r);
  const py = (y) => pad.t + (1 - (y - yMin) / (yMax - yMin || 1)) * (H - pad.t - pad.b);

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Cumulative realised profit and loss by track" });

  ticks.forEach((t) => {
    svg.appendChild(svgEl("line", {
      x1: pad.l, x2: W - pad.r, y1: py(t), y2: py(t),
      stroke: t === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1,
    }));
    const label = svgEl("text", {
      x: W - pad.r + 8, y: py(t) + 4, fill: "var(--muted)", "font-size": 11,
    });
    label.textContent = "₹" + Math.round(t).toLocaleString("en-IN");
    svg.appendChild(label);
  });

  series.forEach((s) => {
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${px(p.x).toFixed(1)},${py(p.y).toFixed(1)}`).join(" ");
    svg.appendChild(svgEl("path", {
      d, fill: "none", stroke: s.color, "stroke-width": 2,
      "stroke-linecap": "round", "stroke-linejoin": "round",
    }));
    const last = s.points[s.points.length - 1];
    svg.appendChild(svgEl("circle", {
      cx: px(last.x), cy: py(last.y), r: 4, fill: s.color,
      stroke: "var(--surface-1)", "stroke-width": 2,
    }));
  });

  // Hover crosshair + tooltip over the whole plot.
  const cross = svgEl("line", {
    y1: pad.t, y2: H - pad.b, stroke: "var(--axis)", "stroke-width": 1,
    "stroke-dasharray": "3 3", opacity: 0,
  });
  svg.appendChild(cross);
  const overlay = svgEl("rect", {
    x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.t - pad.b,
    fill: "transparent", style: "cursor:crosshair",
  });
  svg.appendChild(overlay);

  overlay.addEventListener("pointermove", (event) => {
    const box = svg.getBoundingClientRect();
    const ratio = (event.clientX - box.left) / box.width * W;
    const index = Math.round(((ratio - pad.l) / (W - pad.l - pad.r)) * Math.max(n - 1, 1));
    const i = Math.max(0, Math.min(n - 1, index));
    cross.setAttribute("x1", px(i + 1));
    cross.setAttribute("x2", px(i + 1));
    cross.setAttribute("opacity", 1);
    showTip(event, `<div class="h">Trade ${i + 1} · ${rows[i].Date}</div>` +
      series.map((s) => `<div class="r"><span>${s.name}</span><span>${signed(s.points[i].y, 0)}</span></div>`).join(""));
  });
  overlay.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hideTip(); });

  host.appendChild(svg);
  const total = a + b;
  $("equityMeta").textContent = `${n} trades · net ${signed(total, 2)}`;
}

/** One column per closed trade: wins above the baseline, losses below. */
function renderTradesChart() {
  const host = $("tradesChart");
  host.innerHTML = "";
  const rows = journal.rows || [];
  if (!rows.length) {
    host.innerHTML = '<p class="empty">No closed trades yet.</p>';
    return;
  }

  const values = rows.map((r) => Number(r["Realized PnL (Rs)"] || 0));
  const W = 640, H = 260, pad = { t: 14, r: 54, b: 26, l: 16 };
  const ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values));
  const yMin = Math.min(...ticks), yMax = Math.max(...ticks);
  const py = (y) => pad.t + (1 - (y - yMin) / (yMax - yMin || 1)) * (H - pad.t - pad.b);
  const slot = (W - pad.l - pad.r) / rows.length;
  const barWidth = Math.max(Math.min(slot - 2, 28), 3);   // 2px surface gap

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Realised profit or loss for each closed trade" });

  ticks.forEach((t) => {
    svg.appendChild(svgEl("line", {
      x1: pad.l, x2: W - pad.r, y1: py(t), y2: py(t),
      stroke: t === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1,
    }));
    const label = svgEl("text", { x: W - pad.r + 8, y: py(t) + 4, fill: "var(--muted)", "font-size": 11 });
    label.textContent = "₹" + Math.round(t).toLocaleString("en-IN");
    svg.appendChild(label);
  });

  const zero = py(0);
  rows.forEach((row, index) => {
    const value = values[index];
    const x = pad.l + index * slot + (slot - barWidth) / 2;
    const y = value >= 0 ? py(value) : zero;
    const height = Math.max(Math.abs(py(value) - zero), 1.5);
    const bar = svgEl("rect", {
      x, y, width: barWidth, height, rx: Math.min(4, barWidth / 2),
      fill: value >= 0 ? "var(--good)" : "var(--critical)",
    });
    bar.addEventListener("pointerenter", (event) =>
      showTip(event, `<div class="h">${escapeHtml(row["Strategy / Legs"] || "")}</div>` +
        `<div class="r"><span>${escapeHtml(row.Track)}</span><span>${escapeHtml(row.Date)}</span></div>` +
        `<div class="r"><span>Net points</span><span>${escapeHtml(row["Net Points"])}</span></div>` +
        `<div class="r"><span>Realised</span><span>${signed(value, 2)}</span></div>` +
        `<div class="r"><span>Exit</span><span>${escapeHtml(row["Exit Reason"] || "")}</span></div>`));
    bar.addEventListener("pointerleave", hideTip);
    svg.appendChild(bar);
  });

  host.appendChild(svg);
}

function showTip(event, html) {
  const tip = $("tip");
  tip.innerHTML = html;
  tip.style.opacity = 1;
  const box = tip.getBoundingClientRect();
  tip.style.left = Math.min(event.clientX + 14, window.innerWidth - box.width - 12) + "px";
  tip.style.top = Math.min(event.clientY + 14, window.innerHeight - box.height - 12) + "px";
}
function hideTip() { $("tip").style.opacity = 0; }

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------------------------------------------------------- polls */
async function refresh() {
  try {
    state = await api("/api/state");
    journal = await api("/api/journal");
  } catch (error) {
    return;                       // server restarting; the next poll retries
  }
  renderConnection();
  renderMode();
  renderEngine();
  renderTiles();
  renderWaiting();
  renderComparison();
  renderPositions();
  renderJournal();
  renderEquityChart();
  renderTradesChart();
}

async function pollLogs() {
  try {
    const { logs } = await api(`/api/logs?after=${lastLogId}`);
    if (!logs?.length) return;
    const host = $("logs");
    const atBottom = host.scrollTop + host.clientHeight >= host.scrollHeight - 24;
    logs.forEach((entry) => {
      lastLogId = entry.id;
      const line = document.createElement("div");
      line.innerHTML = `<span class="t">${entry.time}</span>` +
        `<span class="l ${entry.level}">${entry.level}</span>` +
        `<span class="m">${escapeHtml(entry.message)}</span>`;
      host.appendChild(line);
    });
    while (host.children.length > 300) host.removeChild(host.firstChild);
    if (atBottom) host.scrollTop = host.scrollHeight;
  } catch (error) { /* transient */ }
}

/* --------------------------------------------------------------- wiring */
$("runBtn").onclick = () =>
  act(state.running ? "/api/engine/stop" : "/api/engine/start", { poll: 60 });
$("tickBtn").onclick = () => act("/api/engine/tick", {}, "Evaluated once");
$("panicBtn").onclick = () => {
  if (confirm("Engage the kill switch and square off every open position?")) {
    act("/api/panic", { flatten: true });
  }
};
$("saveCredsBtn").onclick = async () => {
  const result = await act("/api/credentials", {
    api_key: $("apiKey").value,
    api_secret: $("apiSecret").value,
    redirect_uri: $("redirectUri").value,
  });
  if (result.ok) { $("apiSecret").value = ""; $("credForm").dataset.editing = "0"; await refresh(); }
};
$("editCredsBtn").onclick = () => { $("credForm").dataset.editing = "1"; renderConnection(); };
$("reconnectBtn").onclick = () => window.open("/auth/login", "_blank", "noopener");

document.querySelectorAll(".segmented button").forEach((button) => {
  button.onclick = () => {
    const mode = button.dataset.mode;
    if (mode === state.mode) return;
    if (mode === "live") {
      pendingMode = "live";
      $("confirmBox").classList.remove("hidden");
      $("confirmInput").focus();
    } else {
      act("/api/mode", { mode: "paper" });
    }
  };
});
$("armBtn").onclick = async () => {
  const result = await act("/api/mode", { mode: "live", confirmation: $("confirmInput").value });
  if (result.ok) { $("confirmBox").classList.add("hidden"); $("confirmInput").value = ""; pendingMode = null; }
};
$("cancelArmBtn").onclick = () => {
  $("confirmBox").classList.add("hidden");
  $("confirmInput").value = "";
  pendingMode = null;
  renderMode();
};
$("clearLogs").onclick = () => ($("logs").innerHTML = "");

$("themeBtn").onclick = () => {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "dark" ? "light" : current === "light" ? "" : "dark";
  if (next) document.documentElement.setAttribute("data-theme", next);
  else document.documentElement.removeAttribute("data-theme");
  try { localStorage.setItem("theme", next); } catch (e) { /* private mode */ }
};
try {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
} catch (e) { /* private mode */ }

if (new URLSearchParams(location.search).has("connected")) {
  toast("Upstox connected — the access token was stored automatically.");
  history.replaceState({}, "", "/");
}

refresh();
pollLogs();
setInterval(refresh, 4000);
setInterval(pollLogs, 2000);
