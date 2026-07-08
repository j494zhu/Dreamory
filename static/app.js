// Dreamory frontend — vanilla JS, talks to the FastAPI backend.
const $ = (sel) => document.querySelector(sel);
const api = {
  async get(url) { return (await fetch(url)).json(); },
  async post(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
};

let activeChat = null;
let eventSource = null;   // 当前对话的 SSE 订阅(主动消息推送通道)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 模拟打字耗时:字越多"打"得越久,但封顶,不让用户干等
const typingDelay = (text) => Math.min(400 + (text || "").length * 45, 2200);

// ── Chats ───────────────────────────────────────────────
async function loadChats() {
  const chats = await api.get("/api/chats");
  const list = $("#chat-list");
  list.innerHTML = "";
  chats.forEach((c) => {
    const el = document.createElement("div");
    el.className = "chat-item" + (activeChat === c.id ? " active" : "");
    el.innerHTML = `<span class="t">${escapeHtml(c.title)}</span>` +
      (c.goal ? `<span class="g">🎯 ${escapeHtml(c.goal)}</span>` : "");
    el.onclick = () => openChat(c.id);
    list.appendChild(el);
  });
}

async function openChat(id) {
  activeChat = id;
  await loadChats();
  const chat = await api.get(`/api/chats/${id}`);
  $("#chat-title").textContent = chat.title;
  $("#chat-goal").textContent = chat.goal ? `🎯 ${chat.goal}` : "";
  $("#composer").classList.remove("hidden");
  setMode(chat.affect?.mode || "neutral");
  renderAffection(chat.affect?.affection);

  const msgs = await api.get(`/api/chats/${id}/messages`);
  const box = $("#messages");
  box.innerHTML = "";
  if (!msgs.length) {
    box.innerHTML = `<div class="empty-hint">开始聊天吧 ✦<br/>她的情绪会随着你说的话真实地变化</div>`;
  }
  msgs.forEach((m) => addMessage(m.speaker === "agent" ? "agent" : "user", m.content));
  box.scrollTop = box.scrollHeight;

  subscribeEvents(id);
  refreshTimerHint();
  loadAffectChart();
}

// ── SSE:她主动发来的消息(定时器到点)从这里进来 ──────────
function subscribeEvents(id) {
  if (eventSource) { eventSource.close(); eventSource = null; }
  eventSource = new EventSource(`/api/chats/${id}/events`);
  eventSource.onmessage = async (e) => {
    let payload;
    try { payload = JSON.parse(e.data); } catch { return; }
    if (payload.type !== "proactive" || payload.chat_id !== activeChat) return;
    await playAgentMessages(payload.messages || []);
    if (payload.mode) setMode(payload.mode);
    if (payload.thinking) $("#dbg-thinking").textContent = payload.thinking;
    refreshTimerHint();
    loadAffectChart();
  };
}

// ── 定时器提示:她说过"过会儿来找你" ──────────────────────
async function refreshTimerHint() {
  const el = $("#timer-hint");
  if (!activeChat || !el) return;
  try {
    const timers = await api.get(`/api/chats/${activeChat}/timers`);
    if (timers.length) {
      const mins = Math.max(1, Math.round((timers[0].due_ms - Date.now()) / 60000));
      el.textContent = `⏰ 她说过会儿来找你(约${mins}分钟后)`;
      el.classList.remove("hidden");
    } else {
      el.classList.add("hidden");
    }
  } catch { el.classList.add("hidden"); }
}

// ── Messaging ───────────────────────────────────────────
function addMessage(role, content, meta) {
  const box = $("#messages");
  const hint = box.querySelector(".empty-hint");
  if (hint) hint.remove();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = escapeHtml(content) + (meta ? `<div class="meta">${meta}</div>` : "");
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
  return el;
}

// 连发消息逐条播放:条与条之间显示"正在输入…",像真人打字的节奏
async function playAgentMessages(msgs) {
  for (let i = 0; i < msgs.length; i++) {
    const typing = addMessage("agent", "…");
    typing.classList.add("typing");
    await sleep(i === 0 ? 250 : typingDelay(msgs[i]));
    typing.remove();
    addMessage("agent", msgs[i]);
  }
}

$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#msg-input");
  const text = input.value.trim();
  if (!text || !activeChat) return;
  input.value = "";
  addMessage("user", text);
  const typing = addMessage("agent", "…", "thinking");
  try {
    const res = await api.post(`/api/chats/${activeChat}/messages`, { content: text });
    typing.remove();
    const msgs = (res.messages && res.messages.length) ? res.messages : [res.content];
    await playAgentMessages(msgs);
    if (res.debug) renderDebug(res.debug);
    refreshTimerHint();
    loadAffectChart();
  } catch (err) {
    typing.remove();
    addMessage("agent", "⚠️ " + err.message, "error");
  }
});

// ── Debug panel ─────────────────────────────────────────
function setMode(mode) {
  const pill = $("#mode-pill");
  pill.className = "pill " + mode;
  pill.textContent = mode;
  pill.classList.remove("hidden");
}

// 与后端 app/affect/state.py 的 AFFECTION_TIERS 同一张表
const AFF_TIERS = [[0, "失望"], [20, "冷淡"], [40, "陌生"], [60, "友好"],
                   [85, "心动"], [100, "恋人"], [140, "挚爱"], [180, "誓约"]];
function affTierLabel(v) {
  let label = AFF_TIERS[0][1];
  for (const [floor, name] of AFF_TIERS) if (v >= floor) label = name;
  return label;
}

function renderAffection(value, tierLabel) {
  if (value == null) return;
  setGauge("#g-affection", value / 200);
  $("#g-aff-val").textContent = Math.round(value);
  $("#g-aff-tier").textContent = tierLabel || affTierLabel(value);
}

function renderDebug(d) {
  setMode(d.mode);
  $("#dbg-thinking").textContent = d.thinking || "(无内心独白)";
  $("#dbg-events").textContent = JSON.stringify(d.events, null, 1);

  const h = d.hormones || {};
  setGauge("#g-adrenaline", h.adrenaline);
  setGauge("#g-oxytocin", h.oxytocin);
  setGauge("#g-cortisol", h.cortisol);

  $("#dbg-schedule").textContent = d.schedule ? "📅 " + d.schedule : "📅 (无日程注入)";
  $("#dbg-seed").textContent = d.topic_seed
    ? "🌱 种子: " + d.topic_seed
    : `🌱 无话题种子 (dull_streak=${d.dull_streak ?? 0})`;
  $("#dbg-guard").textContent = d.guardrail
    ? `🛡 守护触发: ${(d.guardrail.triggered || []).join("、")} → 重生成${d.guardrail.clean_after_retry ? "已修复" : "仍有残留(照发)"}`
    : "🛡 守护未触发";
  const toolsUl = $("#dbg-tools");
  toolsUl.innerHTML = "";
  (d.tools || []).forEach((t) => {
    const el = document.createElement("li");
    el.textContent = `🔧 ${t.tool} ${t.args || ""} → ${t.result || ""}`;
    toolsUl.appendChild(el);
  });

  const s = d.scalars || {};
  setGauge("#g-arousal", s.arousal);
  setGauge("#g-security", s.security);
  setGauge("#g-patience", (s.patience ?? 0) / 7);
  $("#g-warm").textContent = s.warm_streak ?? 0;
  renderAffection(s.affection, d.affection_tier?.label);

  const trace = $("#dbg-trace");
  trace.innerHTML = "";
  (d.trace || []).forEach((t) => {
    const li = document.createElement("li");
    li.textContent = t;
    trace.appendChild(li);
  });

  const loops = $("#dbg-loops");
  loops.innerHTML = "";
  (d.open_loops || []).forEach((l) => loops.appendChild(li(l, "")));
  (d.grievances || []).forEach((g) => loops.appendChild(li("⛓ " + g, "grievance")));
  if (!loops.children.length) loops.innerHTML = `<li style="background:none;color:var(--muted)">— 无 —</li>`;

  const retr = $("#dbg-retrieved");
  retr.innerHTML = "";
  (d.l1?.retrieved || []).forEach((r) => {
    const el = document.createElement("li");
    el.innerHTML = `<span>${escapeHtml(r.content)}</span><span class="s">${r.score} · ${r.axis}</span>`;
    retr.appendChild(el);
  });
  if (!retr.children.length) retr.innerHTML = `<li style="color:var(--muted)">— 无检索命中 —</li>`;
  $("#dbg-tokens").textContent = d.l1?.tokens ?? 0;
  $("#dbg-tags").textContent = (d.tags_assigned || []).join(", ") || "—";
}

function li(text, cls) { const e = document.createElement("li"); if (cls) e.className = cls; e.textContent = text; return e; }
function setGauge(sel, v) { $(sel).style.width = `${Math.max(0, Math.min(1, v || 0)) * 100}%`; }

// ── 情绪曲线(affect_snapshots 时间序列)──────────────────
// 两张图分离两种量纲(好感度0~200 / 标量0~1),绝不用双轴。
// 系列色为在深色面板上验证过的调色板(CVD最小相邻ΔE 23.7,对比度≥3:1)。
const SCALAR_SERIES = [
  { key: "security", color: "#3987e5" },
  { key: "cortisol", color: "#c98500" },
  { key: "arousal", color: "#e66767" },
  { key: "oxytocin", color: "#d55181" },
];
let vizData = [];   // 最近的快照数组(升序)

function drawChart(canvas, rows, seriesDefs, yMax, { midline = null, hoverIdx = -1 } = {}) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || 260;
  const h = +canvas.dataset.h || 64;
  canvas.style.height = h + "px";        // CSS 高度固定,像素缓冲按 dpr 放大保证清晰
  canvas.width = W * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, h);
  const pad = 2;

  // 网格(退居背景):两条水平线 + 可选中线(如恋人线)
  ctx.strokeStyle = "#272c3a"; ctx.lineWidth = 1;
  [0.25, 0.75].forEach((f) => {
    ctx.beginPath(); ctx.moveTo(0, h * f); ctx.lineTo(W, h * f); ctx.stroke();
  });
  if (midline !== null) {
    ctx.setLineDash([3, 3]); ctx.strokeStyle = "#8b91a4";
    const y = h - pad - (midline / yMax) * (h - pad * 2);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.setLineDash([]);
  }
  if (rows.length < 2) return;

  const x = (i) => (rows.length === 1 ? W / 2 : (i / (rows.length - 1)) * W);
  const y = (v) => h - pad - Math.max(0, Math.min(1, v / yMax)) * (h - pad * 2);

  for (const s of seriesDefs) {
    ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.beginPath();
    rows.forEach((r, i) => { i ? ctx.lineTo(x(i), y(r[s.key])) : ctx.moveTo(x(i), y(r[s.key])); });
    ctx.stroke();
  }
  // 悬停十字线
  if (hoverIdx >= 0 && hoverIdx < rows.length) {
    ctx.strokeStyle = "#8b91a4"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x(hoverIdx), 0); ctx.lineTo(x(hoverIdx), h); ctx.stroke();
  }
}

function renderCharts(hoverIdx = -1) {
  const a = $("#viz-affection"), s = $("#viz-scalars");
  if (!a || !s) return;
  a.dataset.h = 64; s.dataset.h = 72;
  drawChart(a, vizData, [{ key: "affection", color: "#f78ca0" }], 200,
    { midline: 100, hoverIdx });
  drawChart(s, vizData, SCALAR_SERIES, 1, { hoverIdx });
}

async function loadAffectChart() {
  if (!activeChat) return;
  try {
    vizData = await api.get(`/api/chats/${activeChat}/affect-history?limit=200`);
    renderCharts();
    $("#viz-readout").textContent = vizData.length
      ? `${vizData.length} 个快照 · 悬停查看逐轮数值`
      : "还没有快照(聊一轮就有了)";
  } catch { /* 面板可视化失败不影响聊天 */ }
}

function vizHover(e) {
  if (!vizData.length) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const idx = Math.round(((e.clientX - rect.left) / rect.width) * (vizData.length - 1));
  const r = vizData[Math.max(0, Math.min(vizData.length - 1, idx))];
  renderCharts(idx);
  $("#viz-readout").textContent =
    `轮${r.turn} · ${r.mode}${r.event ? " · " + r.event : ""} · 好感${r.affection}` +
    ` · sec ${r.security} · cor ${r.cortisol} · aro ${r.arousal} · oxy ${r.oxytocin}`;
}
["#viz-affection", "#viz-scalars"].forEach((sel) => {
  const el = $(sel);
  if (el) {
    el.addEventListener("mousemove", vizHover);
    el.addEventListener("mouseleave", () => renderCharts());
  }
});

// ── 记忆健康体检 ─────────────────────────────────────────
async function checkHealth() {
  if (!activeChat) return;
  const scoreEl = $("#health-score"), flagsEl = $("#health-flags");
  scoreEl.textContent = "…";
  try {
    const h = await api.get(`/api/chats/${activeChat}/health`);
    scoreEl.textContent = h.score;
    scoreEl.className = h.score >= 80 ? "ok" : h.score >= 50 ? "warn" : "bad";
    flagsEl.innerHTML = "";
    (h.flags || []).forEach((f) =>
      flagsEl.appendChild(li(`⚠ ${f.label}(${f.value} > ${f.threshold})`)));
    if (!h.flags?.length) {
      flagsEl.innerHTML = `<li style="background:none;color:var(--muted)">— 一切健康 —</li>`;
    }
  } catch (err) {
    scoreEl.textContent = "—";
    flagsEl.innerHTML = `<li style="background:none;color:var(--muted)">体检失败: ${escapeHtml(err.message)}</li>`;
  }
}
const healthBtn = $("#health-btn");
if (healthBtn) healthBtn.onclick = checkHealth;
function escapeHtml(s) { return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// ── New chat modal ──────────────────────────────────────
$("#new-chat-btn").onclick = () => $("#modal").classList.remove("hidden");
$("#m-cancel").onclick = () => $("#modal").classList.add("hidden");
$("#m-create").onclick = async () => {
  const body = {
    title: $("#m-title").value || "新对话",
    preset: $("#m-preset").value,
    goal: $("#m-goal").value || null,
  };
  const chat = await api.post("/api/chats", body);
  $("#modal").classList.add("hidden");
  $("#m-goal").value = "";
  await openChat(chat.id);
};

// ── Boot ────────────────────────────────────────────────
(async function init() {
  try {
    const h = await api.get("/healthz");
    $("#backend-badge").textContent = `embed: ${h.embedding_backend} · dream: ${h.dream_enabled ? "on" : "off"}`;
  } catch { $("#backend-badge").textContent = "backend offline"; }
  await loadChats();
})();
