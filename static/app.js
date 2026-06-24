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

  const msgs = await api.get(`/api/chats/${id}/messages`);
  const box = $("#messages");
  box.innerHTML = "";
  if (!msgs.length) {
    box.innerHTML = `<div class="empty-hint">开始聊天吧 ✦<br/>她的情绪会随着你说的话真实地变化</div>`;
  }
  msgs.forEach((m) => addMessage(m.speaker === "agent" ? "agent" : "user", m.content));
  box.scrollTop = box.scrollHeight;
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
    addMessage("agent", res.content);
    if (res.debug) renderDebug(res.debug);
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

function renderDebug(d) {
  setMode(d.mode);
  $("#dbg-thinking").textContent = d.thinking || "(无内心独白)";
  $("#dbg-events").textContent = JSON.stringify(d.events, null, 1);

  const s = d.scalars || {};
  setGauge("#g-arousal", s.arousal);
  setGauge("#g-security", s.security);
  setGauge("#g-patience", (s.patience ?? 0) / 7);
  $("#g-warm").textContent = s.warm_streak ?? 0;

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
