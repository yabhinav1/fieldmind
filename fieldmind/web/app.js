"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  status: null,
  tab: "work",
  mode: "hybrid",
  filter: "all",
  sort: "relevance",
  queueTab: "pending",
  cloudTab: "mine",
  lastEvent: 0,
  firstEvents: true,
  events: [],
  unseenEvents: 0,
  query: "",
  previewSeq: 0,
  searchSeq: 0,
  guideOpen: false,
};

const SCOPE = {
  private: "Private",
  shared: "Shared",
  redacted: "Shared, masked",
};
const SCOPE_LONG = {
  private: "On this device only",
  shared: "Shared with the fleet",
  redacted: "Shared, personal details masked",
};
const SYNC = {
  pending: "Waiting to sync",
  synced: "Synced",
  conflict: "Needs a decision",
  failed: "Could not sync",
};
const RELATION = {
  updates: "Updates an earlier note",
  resolves: "Resolves an earlier note",
  duplicate: "Already recorded",
  related: "Related note",
};
const PRIORITY = ["Low", "Normal", "Urgent"];
const CATEGORY = {
  safety_hazard: "Safety", equipment_fault: "Fault", procedure_fix: "Fix", routine_reading: "Reading",
  personal_health: "Personal", people_hr: "People", scratch_note: "Note",
};

const icon = (name, cls = "icon") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

// ---------------------------------------------------------------- helpers

function seen(flag, value) {
  const key = `fieldmind-seen-${location.port}`;
  let flags = {};
  try { flags = JSON.parse(localStorage.getItem(key) || "{}"); } catch (error) { /* storage unavailable */ }
  if (value === undefined) return Boolean(flags[flag]);
  if (!flags[flag]) {
    flags[flag] = true;
    try { localStorage.setItem(key, JSON.stringify(flags)); } catch (error) { /* storage unavailable */ }
  }
  return true;
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && !path.startsWith("/api/auth/")) showLock();
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function dateOf(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const sameDay = new Date().toDateString() === d.toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

function span(seconds) {
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function bytes(n) {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

let toastTimer;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

async function attempt(action, button) {
  if (button) button.disabled = true;
  try {
    return await action();
  } catch (error) {
    toast(error.message);
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

function emptyState(iconName, title, text) {
  return `<div class="empty-state"><span class="empty-icon">${icon(iconName)}</span><b>${esc(title)}</b><span>${esc(text)}</span></div>`;
}

// ---------------------------------------------------------------- device lock

async function showLock() {
  if (state.locked) return;
  state.locked = true;
  closeDrawer();
  let info;
  try {
    info = await api("/api/auth/state");
  } catch (error) {
    info = { configured: true, device: "This device" };
  }
  if (info.authenticated) { state.locked = false; return; }
  state.setup = !info.configured;
  state.needsCode = Boolean(info.needs_setup_code);
  $("lock-title").textContent = state.setup ? `Set a PIN for ${info.device}` : `${info.device} is locked`;
  $("lock-hint").textContent = state.setup
    ? (state.needsCode
      ? "This device has no PIN yet. Enter the setup code shown in its console, then choose a PIN of at least 4 characters."
      : "Choose a PIN of at least 4 characters. It protects every note on this device.")
    : "Enter the PIN for this device.";
  $("lock-code").hidden = !(state.setup && state.needsCode);
  $("lock-confirm").hidden = !state.setup;
  $("lock-submit").textContent = state.setup ? "Set PIN" : "Unlock";
  $("lock-submit").disabled = false;
  $("lock-error").textContent = "";
  $("lock-code").value = "";
  $("lock-pin").value = "";
  $("lock-confirm").value = "";
  $("lock").hidden = false;
  ($("lock-code").hidden ? $("lock-pin") : $("lock-code")).focus();
}

async function submitLock(event) {
  event.preventDefault();
  const pin = $("lock-pin").value;
  if (state.setup && pin !== $("lock-confirm").value) {
    $("lock-error").textContent = "The two PINs do not match.";
    return;
  }
  const body = { pin };
  if (state.setup && state.needsCode) body.setup_code = $("lock-code").value.trim();
  try {
    await api(state.setup ? "/api/auth/setup" : "/api/auth/login", { method: "POST", body });
  } catch (error) {
    $("lock-error").textContent = error.message;
    $("lock-pin").select();
    return;
  }
  state.locked = false;
  $("lock").hidden = true;
  state.lastEvent = 0;
  state.firstEvents = true;
  state.events = [];
  $("events").innerHTML = "";
  poll();
  refresh();
}

// ---------------------------------------------------------------- memory items

function badges(m, options = {}) {
  const out = [];
  if (m.source === "replica" || options.cloud) {
    out.push(`<span class="badge replica">${m.via_peer ? `From ${esc(m.via_peer)} nearby` : "From cloud"} · ${esc(m.origin_device || m.device_id || "unknown")}</span>`);
    if (m.redacted) out.push(`<span class="badge redacted">Masked</span>`);
  } else {
    out.push(`<span class="badge ${esc(m.scope)}">${esc(SCOPE[m.scope] || m.scope)}</span>`);
    if (SYNC[m.sync_state] && m.sync_state !== "synced") out.push(`<span class="badge ${esc(m.sync_state)}">${SYNC[m.sync_state]}</span>`);
    if (m.origin_device && m.mine === false) out.push(`<span class="badge">First written on ${esc(m.origin_device)}</span>`);
  }
  if (m.category && CATEGORY[m.category] && m.kind !== "reference") out.push(`<span class="badge">${CATEGORY[m.category]}</span>`);
  if (m.kind === "reference") out.push(`<span class="badge">Manual</span>`);
  if (m.priority === 2 && m.scope !== "private") out.push(`<span class="badge urgent">Urgent</span>`);
  if (m.status === "superseded") out.push(`<span class="badge">Replaced</span>`);
  if (m.relation && m.supersedes) out.push(`<span class="badge">${esc(RELATION[m.relation] || "")}</span>`);
  (m.tags || []).slice(0, 3).forEach((t) => out.push(`<span class="badge">${esc(t)}</span>`));
  return `<div class="badges">${out.join("")}</div>`;
}

function thumbs(photos, size = "") {
  if (!photos || !photos.length) return "";
  return `<div class="thumbs ${size}">${photos.map((p) =>
    `<img src="${esc(p.url)}" alt="Photo attached to this note" loading="lazy" width="${p.width}" height="${p.height}">`).join("")}</div>`;
}

function itemThumb(m) {
  if (m.photos && m.photos.length) return `<span class="item-thumb"><img src="${esc(m.photos[0].url)}" alt=""></span>`;
  if (m.kind === "reference") return `<span class="item-thumb manual">${icon("book")}</span>`;
  if (m.kind === "photo") return `<span class="item-thumb photo">${icon("image")}</span>`;
  return `<span class="item-thumb">${icon("note")}</span>`;
}

function itemTitle(m) {
  if (m.asset) return m.asset;
  if (m.kind === "reference") return "Manual";
  return CATEGORY[m.category] ? `${CATEGORY[m.category]} note` : "Note";
}

function memoryItem(m, extra = "") {
  return `<button class="item ${m.status === "superseded" ? "old" : ""}" data-open="${esc(m.id)}">
    ${itemThumb(m)}
    <span class="item-main">
      <span class="item-title">${esc(itemTitle(m))}</span>
      <p class="item-text">${esc(m.text)}</p>
      ${extra}
      ${badges(m)}
    </span>
    <span class="item-side"><span class="when">${esc(dateOf(m.updated_at))}</span></span>
  </button>`;
}

// ---------------------------------------------------------------- status

function initials(author) {
  const parts = String(author || "").split(/[\s_-]+/).filter(Boolean);
  const two = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] || "FM").slice(0, 2);
  return two.toUpperCase();
}

function greeting() {
  const h = new Date().getHours();
  return h < 5 ? "Working late." : h < 12 ? "Good morning." : h < 17 ? "Good to see you back." : "Good evening.";
}

function renderStatus(s) {
  state.status = s;
  const { link, memory, outbox, device } = s;
  $("device-line").textContent = `${device.id} · ${device.site}`;
  $("top-device").textContent = `${device.id} · ${device.site}`;
  $("menu-head").textContent = `${device.author} · ${device.id}`;
  $("settings").textContent = initials(device.author);
  document.title = `${device.id} · FieldMind`;
  $("greeting").textContent = greeting();

  const box = $("link");
  box.className = `link-state ${link.online ? "online" : "offline"}`;
  $("link-title").textContent = link.online ? "Online" : "Offline";
  const detail = link.online
    ? (s.sync.last_sync_at ? `In sync, checked ${ago(s.sync.last_sync_at)}` : "Connected, not synced yet")
    : link.forced_offline ? `Network off for ${span(s.now - (link.offline_since || s.now))}`
    : `Cloud unreachable for ${span(s.now - (link.offline_since || s.now))}`;
  box.title = detail;
  $("network-toggle").checked = !link.forced_offline;
  $("auto-sync").checked = link.auto_sync;
  $("sync-now").disabled = !link.online;

  const waiting = outbox.pending || 0;
  const attention = waiting + s.open_conflicts;
  $("tab-sync-count").textContent = attention ? String(attention) : "";

  const disk = s.shards.local.disk_bytes + s.shards.replica.disk_bytes;
  const rows = [
    ["wifi", "Network", link.online ? `<span class="ok">Online</span>` : `<span class="warn">${link.forced_offline ? "Switched off" : "Unreachable"}</span>`],
    ["clock", "Last sync", s.sync.last_sync_at ? esc(ago(s.sync.last_sync_at)) : "never"],
    ["list", "Pending items", waiting ? `<span class="warn">${waiting}</span>` : "0"],
    ["merge", "Need a decision", s.open_conflicts ? `<span class="warn">${s.open_conflicts}</span>` : "0"],
    ["layers", "Memories", `${memory.local + memory.replica} <span class="hint">· ${memory.private} private</span>`],
    ["disk", "Storage used", esc(bytes(disk))],
  ];
  $("device-status").innerHTML = rows.map(([i, label, value]) =>
    `<div class="status-row">${icon(i)}<span>${label}</span><b>${value}</b></div>`).join("");

  $("engine").innerHTML = `<p class="hint">Everything below runs inside this device's process. Nothing here needs a network.</p>
    <dl class="kv">
      <dt>Vector store</dt><dd>${esc(s.engine.vector_store)}</dd>
      <dt>Meaning model</dt><dd>${esc(s.engine.dense_model.split("/").pop())} · ${s.engine.dimensions}d</dd>
      <dt>Keyword model</dt><dd>${esc(s.engine.sparse_model)}</dd>
      <dt>Name masking</dt><dd>${esc(s.engine.name_model || "Title rules only")}</dd>
      <dt>Reranking</dt><dd>${esc(s.engine.reranker || "Off")}</dd>
      <dt>Learned choices</dt><dd>${s.engine.learned_examples || 0} from this device's overrides</dd>
      <dt>Answers</dt><dd>${esc(s.engine.answer_model || "Composed from notes")}</dd>
      <dt>Dictation</dt><dd>${s.engine.speech_model ? esc(s.engine.speech_model) : s.engine.speech_loading ? "Speech model loading…" : s.engine.speech_error ? `Failed: ${esc(s.engine.speech_error)}` : "Browser speech service"}</dd>
      <dt>Photos</dt><dd>${s.engine.vision_model ? `${esc(s.engine.vision_model)} · ${s.memory.photos || 0} stored` : s.engine.photos ? "Vision model loading…" : "Off"}</dd>
      <dt>On disk</dt><dd>${s.engine.sealed ? "Private text and activity sealed with the device key" : "Not sealed"}</dd>
      <dt>Storage reserved</dt><dd>${bytes(disk)}</dd>
    </dl>`;
  $("rerank-label").hidden = !s.engine.reranker;
  $("add-photo").hidden = !s.engine.photos;
  updateDictateButtons(s.engine);
  renderFilters();
  if (!link.online) seen("offline", true);
  if (state.guideOpen) renderGuide();
}

const EVENT_ICON = { capture: "note", sync: "cloud-check", link: "wifi", conflict: "merge", merge: "merge", edit: "work",
  delete: "note", security: "lock", system: "cog", answer: "search", demo: "check", peer: "users", dedupe: "note" };

function renderRecent() {
  const events = state.events.slice(0, 6);
  $("recent").innerHTML = events.length ? events.map((e) => `
    <div class="recent-item">
      <span class="recent-icon ${esc(e.level)}">${icon(EVENT_ICON[e.type] || "note")}</span>
      <div><div class="recent-title">${esc(e.message.split(":")[0].slice(0, 60))}</div>
        <div class="recent-sub">${esc(e.message.includes(":") ? e.message.slice(e.message.indexOf(":") + 1).trim() : "")}</div></div>
      <span class="when">${ago(e.ts)}</span>
    </div>`).join("") : emptyState("inbox", "Nothing yet", "What the device decides and does shows up here.");
}

function renderEvents(events) {
  if (!events.length) return;
  const box = $("events");
  const fresh = !state.firstEvents;
  const html = events.map((e) => `
    <div class="event ${esc(e.level)} ${fresh ? "fresh" : ""}">
      <span class="event-dot"></span>
      <div><div class="event-msg">${esc(e.message)}</div><div class="when">${clock(e.ts)}</div></div>
    </div>`).reverse().join("");
  box.insertAdjacentHTML("afterbegin", html);
  while (box.children.length > 150) box.lastElementChild.remove();
  state.events = [...events].reverse().concat(state.events).slice(0, 150);
  if (fresh && $("activity-drawer").hidden) { state.unseenEvents += events.length; $("activity-pip").hidden = false; }
  renderRecent();
  if (events.some((e) => e.message.includes("cloud snapshot"))) seen("snapshot", true);
  if (events.some((e) => e.type === "conflict")) seen("conflict", true);
  state.lastEvent = events[events.length - 1].id;
  state.firstEvents = false;
}

async function poll() {
  if (state.locked) return;
  try {
    const [status, log] = await Promise.all([
      api("/api/status"),
      api(`/api/events?after=${state.lastEvent}`),
    ]);
    renderStatus(status);
    const changed = log.events.length > 0 && !state.firstEvents;
    renderEvents(log.events);
    if (state.firstEvents) renderRecent();
    state.firstEvents = false;
    if (changed) refresh();
  } catch (error) {
    if (state.locked) return;
    $("link").className = "link-state offline";
    $("link-title").textContent = "Device not responding";
  }
}

function refresh() {
  if (state.tab === "memory") { if (state.query) runSearch(false); else loadMemory(); }
  if (state.tab === "sync") loadSync();
  if (state.tab === "cloud") { loadCloud(); seen("cloud", true); }
}

// ---------------------------------------------------------------- capture

function openComposer() {
  $("composer").hidden = false;
  $("note").focus();
  $("composer").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

let previewTimer;
function schedulePreview() {
  clearTimeout(previewTimer);
  $("save-note").disabled = !$("note").value.trim();
  previewTimer = setTimeout(loadPreview, 220);
}

async function loadPreview() {
  const text = $("note").value.trim();
  const box = $("preview");
  if (!text) {
    box.className = "preview empty";
    box.textContent = "The sharing decision appears here as you type.";
    return;
  }
  const seq = ++state.previewSeq;
  let data;
  try {
    data = await api("/api/preview", { method: "POST", body: { text, scope: $("scope-override").value || null } });
  } catch (error) {
    return;
  }
  if (seq !== state.previewSeq || !data.decision) return;
  const d = data.decision;
  const masked = d.shared_text
    ? `<div class="masked"><b>What the cloud receives</b>${esc(d.shared_text)}</div>` : "";
  const replaces = data.related.find((r) => r.relation === "updates" || r.relation === "resolves");
  const keepSupersede = state.supersede !== false;
  const related = data.related.length ? `<div class="related">${data.related.map((r) => `
      <div class="related-item"><span class="badge ${r.relation === "duplicate" ? "pending" : ""}">${esc(RELATION[r.relation])}</span>
      ${esc(r.text)} <span class="when">${Math.round(r.similarity * 100)}% similar</span></div>`).join("")}
      ${replaces ? `<label class="check" style="margin-top:6px"><input type="checkbox" id="supersede-choice" ${keepSupersede ? "checked" : ""}>
        Mark the earlier note as replaced by this one</label>` : ""}</div>` : "";
  box.className = `preview ${d.scope}`;
  box.innerHTML = `
    <div class="preview-head">
      <span class="preview-title">${esc(SCOPE_LONG[d.scope])}</span>
      <span class="badges">
        <span class="badge">${esc(d.category_label)}</span>
        ${d.scope !== "private" ? `<span class="badge ${d.priority === 2 ? "urgent" : ""}">${PRIORITY[d.priority]} priority</span>` : ""}
        ${data.asset ? `<span class="badge asset">${esc(data.asset)}</span>` : ""}
      </span>
    </div>
    <ul>${d.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    ${masked}${related}
    <div class="when" style="margin-top:8px">Decided on this device in ${data.took_ms} ms</div>`;
  const choice = $("supersede-choice");
  if (choice) choice.onchange = () => { state.supersede = choice.checked; };
}

async function savePhoto(text) {
  const form = new FormData();
  form.append("file", state.photo, state.photo.name || "photo.jpg");
  form.append("caption", text);
  form.append("supersede", state.supersede !== false ? "true" : "false");
  if ($("scope-override").value) form.append("scope", $("scope-override").value);
  const response = await fetch("/api/photos", { method: "POST", body: form });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) showLock();
  if (!response.ok) throw new Error(data.detail || `Upload failed (${response.status})`);
  return { ...data, created: true };
}

function setPhoto(file) {
  state.photo = file || null;
  const box = $("photo-preview");
  if (!file) { box.hidden = true; box.innerHTML = ""; $("photo-file").value = ""; return; }
  const url = URL.createObjectURL(file);
  box.hidden = false;
  box.innerHTML = `<img src="${url}" alt="Photo to attach"><div>
    <div>${esc(file.name || "Photo")} · ${bytes(file.size)}</div>
    <div class="hint">Position, time and camera details are removed before it is stored. Say what it shows in the note.</div>
    <button class="link-btn" id="remove-photo">Remove</button></div>`;
  $("remove-photo").onclick = () => setPhoto(null);
  $("save-note").disabled = !$("note").value.trim();
}

async function saveNote(allowDuplicate = false) {
  const text = $("note").value.trim();
  if (!text) return;
  const result = await attempt(() => state.photo ? savePhoto(text) : api("/api/memories", {
    method: "POST",
    body: { text, scope: $("scope-override").value || null, allow_duplicate: allowDuplicate,
      supersede: state.supersede !== false },
  }), $("save-note"));
  if (!result) return;
  const box = $("capture-result");
  if (result.photo) setPhoto(null);
  if (!result.created) {
    box.innerHTML = `<div class="notice warn">This is already recorded: “${esc(result.duplicate_of.text)}”.
      <button class="link-btn" id="save-anyway">Save it anyway</button></div>`;
    $("save-anyway").onclick = () => saveNote(true);
    return;
  }
  const m = result.memory;
  const where = m.scope === "private" ? "It stays on this device."
    : state.status?.link.online ? "It will sync in a moment." : "It is queued and will sync when the link returns.";
  box.innerHTML = `<div class="notice">Saved${result.photo ? " with the photo" : ""}. ${where}</div>`;
  setTimeout(() => { box.innerHTML = ""; }, 5000);
  $("note").value = "";
  $("scope-override").value = "";
  state.supersede = true;
  schedulePreview();
  poll();
}

// ---------------------------------------------------------------- memory: search and browse

async function runSearch(withAnswer = true) {
  const query = state.query;
  if (!query) return;
  const seq = ++state.searchSeq;
  seen("search", true);
  $("memory-list").hidden = true;
  const body = { query, mode: state.mode, limit: 12, include_superseded: $("include-old").checked, rerank: $("rerank").checked };
  if (withAnswer) {
    $("answer").innerHTML = `<div class="answer pending"><div class="answer-head">Working out an answer on this device…</div></div>`;
    api("/api/ask", { method: "POST", body: { question: query } })
      .then((answer) => { if (seq === state.searchSeq) renderAnswer(answer); })
      .catch(() => { if (seq === state.searchSeq) $("answer").innerHTML = ""; });
  }
  const found = await api("/api/search", { method: "POST", body }).catch((e) => { toast(e.message); return null; });
  if (seq !== state.searchSeq || !found) return;

  const t = found.timing_ms;
  const total = found.searched.local + found.searched.replica;
  $("search-meta").innerHTML =
    `${found.results.length} result${found.results.length === 1 ? "" : "s"} · searched ${total} memories in <b>${t.total} ms</b> · <b>${found.network_calls} network calls</b>${found.reranked ? " · question and notes read together" : ""}`;

  const results = state.sort === "newest" ? [...found.results].sort((a, b) => b.updated_at - a.updated_at) : found.results;
  const SIGNAL = { semantic: "Meaning", keyword: "Keywords", rerank: "Read together", image: "Photo" };
  $("results").innerHTML = results.length ? results.map((r) => {
    const parts = Object.keys(SIGNAL).filter((k) => r.strength[k] !== undefined).map((k) =>
      `<span class="signal" title="${SIGNAL[k]} match: ${r.matched[k].toFixed(2)}">${SIGNAL[k]}
        <span class="bar"><span style="width:${Math.round(r.strength[k] * 100)}%"></span></span></span>`);
    return memoryItem(r, `<div class="signals">${parts.join("")}</div>`);
  }).join("") : emptyState("search", "Nothing matches that", "Try other words, or switch to Keywords in the search options.");
}

function renderAnswer(answer) {
  if (!answer.points.length) { $("answer").innerHTML = ""; return; }
  const sources = answer.points.map((p, i) => `
    <button class="source" data-open="${esc(p.memory.id)}"><span class="source-n">${i + 1}</span>
      <span class="source-text">${esc(p.memory.text)}</span></button>`).join("");
  const took = answer.timing_ms.total >= 1000
    ? `${(answer.timing_ms.total / 1000).toFixed(1)} s` : `${Math.round(answer.timing_ms.total)} ms`;
  const lines = answer.answer.split(String.fromCharCode(10)).filter((line) => line.trim());
  $("answer").innerHTML = `<div class="answer">
    <div class="answer-head">Answer from ${esc(answer.engine)} · ${took} · ${answer.network_calls} network calls</div>
    ${lines.map((line) => `<p>${esc(line)}</p>`).join("")}
    <div class="sources"><div class="answer-head">Based on</div>${sources}</div>
    ${answer.note ? `<div class="answer-note">${esc(answer.note)}</div>` : ""}</div>`;
}

function clearSearch() {
  state.query = "";
  $("results").innerHTML = ""; $("answer").innerHTML = ""; $("search-meta").innerHTML = "";
  $("memory-list").hidden = false;
  loadMemory();
}

const FILTERS = [
  ["all", "All", {}, (m) => m.local + m.replica],
  ["local", "Notes", { source: "local" }, (m) => m.local],
  ["manuals", "Manuals", { kind: "reference" }, () => null],
  ["replica", "From cloud", { source: "replica" }, (m) => m.replica],
  ["private", "Private", { scope: "private" }, (m) => m.private],
  ["pending", "Waiting to sync", { sync_state: "pending" }, (m) => m.pending],
  ["conflict", "Need a decision", { sync_state: "conflict" }, (m) => m.conflict],
  ["superseded", "Replaced", { status: "superseded" }, (m) => m.superseded],
];

function renderFilters() {
  const memory = state.status?.memory;
  if (!memory) return;
  $("memory-filters").innerHTML = FILTERS.map(([key, label, , count]) => {
    const n = count(memory);
    return `<button class="chip ${state.filter === key ? "active" : ""}" data-filter="${key}">${label}${n === null ? "" : `<span>${n}</span>`}</button>`;
  }).join("");
}

async function loadMemory() {
  const params = new URLSearchParams(FILTERS.find((f) => f[0] === state.filter)[2]);
  const text = $("memory-text").value.trim();
  if (text) params.set("text", text);
  const data = await api(`/api/memories?${params}`).catch(() => null);
  if (!data) return;
  if (!state.query) $("search-meta").innerHTML = `${data.total} memor${data.total === 1 ? "y" : "ies"} on this device`;
  $("memory-list").innerHTML = data.items.length
    ? data.items.map((m) => memoryItem(m)).join("")
    : emptyState("memory", "No memories here yet", "Record a note on the Work page, or publish headquarters manuals to the cloud.");
}

// ---------------------------------------------------------------- drawers

async function openMemory(id) {
  const data = await attempt(() => api(`/api/memories/${id}`));
  if (!data) return;
  const m = data.memory;
  const history = data.history.length > 1 ? `<div class="history"><h2>How this changed</h2>
    ${data.history.map((h, i) => `<div class="history-item ${i === 0 ? "current" : ""}">
      <div>${esc(h.text)}</div><div class="when">${i === 0 ? "Current" : "Replaced"} · ${esc(h.device_id)} · ${ago(h.updated_at)}</div>
    </div>`).join("")}</div>` : "";
  const masked = m.shared_text
    ? `<div class="masked"><b>What the cloud receives</b>${esc(m.shared_text)}</div>` : "";
  const reasons = m.policy?.reasons?.length
    ? `<ul class="hint" style="margin:6px 0 0;padding-left:18px">${m.policy.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "";
  const scopeValue = m.scope === "redacted" ? "shared" : m.scope;

  $("drawer-body").innerHTML = `
    <div class="drawer-head"><h2>${esc(itemTitle(m))}</h2><button class="btn ghost small" data-close>Close</button></div>
    ${badges(m)}
    ${thumbs(m.photos, "large")}
    <div class="field"><label for="edit-text">Note</label><textarea id="edit-text" rows="5">${esc(m.text)}</textarea></div>
    ${masked}
    <div class="field"><label for="edit-tags">Tags, separated by commas</label><input id="edit-tags" type="text" value="${esc(m.tags.join(", "))}"></div>
    <div class="field"><label for="edit-scope">Sharing</label>
      <select id="edit-scope">
        <option value="private" ${scopeValue === "private" ? "selected" : ""}>Keep on this device</option>
        <option value="shared" ${scopeValue === "shared" ? "selected" : ""}>Share with the fleet</option>
      </select>${reasons}
    </div>
    <div class="row between">
      <button class="btn danger" id="edit-delete">Delete</button>
      <button class="btn primary" id="edit-save">Save changes</button>
    </div>
    ${history}
    <dl class="kv">
      <dt>First written on</dt><dd>${esc(m.origin_device)}</dd>
      <dt>Last changed by</dt><dd>${esc(m.device_id)} · ${ago(m.updated_at)}</dd>
      <dt>Revision</dt><dd>${m.rev} (cloud agreed on ${m.base_rev})</dd>
      <dt>Stored in</dt><dd>${m.source === "replica" ? "Cloud replica shard" : "Local shard"}</dd>
    </dl>
    <div class="mono" style="margin-top:10px">${esc(m.id)}</div>`;
  $("drawer").hidden = false;

  $("edit-save").onclick = async (event) => {
    const tags = $("edit-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
    const scope = $("edit-scope").value;
    const body = { text: $("edit-text").value, tags };
    if (scope !== scopeValue) body.scope = scope;
    const saved = await attempt(() => api(`/api/memories/${id}`, { method: "PATCH", body }), event.target);
    if (saved) { closeDrawer(); toast("Saved on this device."); poll(); refresh(); }
  };
  $("edit-delete").onclick = async (event) => {
    if (!confirm("Delete this memory? If it was shared, it is also removed for other devices.")) return;
    const done = await attempt(() => api(`/api/memories/${id}`, { method: "DELETE" }), event.target);
    if (done) { closeDrawer(); toast("Deleted."); poll(); refresh(); }
  };
}

function closeDrawer() { $("drawer").hidden = true; state.guideOpen = false; }
function openActivity() { $("activity-drawer").hidden = false; $("activity-pip").hidden = true; state.unseenEvents = 0; }
function closeActivity() { $("activity-drawer").hidden = true; }

function openPinChange() {
  state.guideOpen = false;
  $("drawer-body").innerHTML = `
    <div class="drawer-head"><h2>Change the PIN</h2><button class="btn ghost small" data-close>Close</button></div>
    <p class="hint">Every other open session on this device is closed when the PIN changes.</p>
    <form id="pin-form" autocomplete="off">
      <div class="field"><label for="pin-current">Current PIN</label><input id="pin-current" type="password" inputmode="numeric"></div>
      <div class="field"><label for="pin-new">New PIN, at least 4 characters</label><input id="pin-new" type="password" inputmode="numeric"></div>
      <div class="field"><label for="pin-repeat">Repeat the new PIN</label><input id="pin-repeat" type="password" inputmode="numeric"></div>
      <div class="row between"><span class="lock-error" id="pin-error" role="alert"></span>
        <button class="btn primary" type="submit">Change PIN</button></div>
    </form>`;
  $("drawer").hidden = false;
  $("pin-current").focus();
  $("pin-form").onsubmit = async (event) => {
    event.preventDefault();
    if ($("pin-new").value !== $("pin-repeat").value) {
      $("pin-error").textContent = "The two new PINs do not match.";
      return;
    }
    try {
      await api("/api/auth/pin", { method: "POST", body: { current: $("pin-current").value, new: $("pin-new").value } });
    } catch (error) {
      $("pin-error").textContent = error.message;
      return;
    }
    closeDrawer();
    toast("PIN changed.");
  };
}

function openBackup() {
  state.guideOpen = false;
  $("drawer-body").innerHTML = `
    <div class="drawer-head"><h2>Back up this device</h2><button class="btn ghost small" data-close>Close</button></div>
    <p class="hint">One file with everything only this device holds: private notes, the unmasked originals of masked notes, photos and what the policy learned. Sealed with a passphrase you choose; the device key stays here.</p>
    <form id="backup-form" autocomplete="off">
      <div class="field"><label for="backup-pass">Passphrase, at least 8 characters</label><input id="backup-pass" type="password"></div>
      <div class="row between"><span class="lock-error" id="backup-error" role="alert"></span><button class="btn primary" type="submit">Download backup</button></div>
    </form>`;
  $("drawer").hidden = false;
  $("backup-pass").focus();
  $("backup-form").onsubmit = async (event) => {
    event.preventDefault();
    const response = await fetch("/api/backup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passphrase: $("backup-pass").value }) });
    if (!response.ok) { $("backup-error").textContent = (await response.json().catch(() => ({}))).detail || "Backup failed."; return; }
    const blob = await response.blob();
    const name = (response.headers.get("content-disposition") || "").split('filename="')[1]?.replace(/"$/, "") || "fieldmind-backup.fmbackup";
    const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
    link.click();
    closeDrawer();
    toast("Backup downloaded. Keep the passphrase safe.");
  };
}

function openRestore() {
  state.guideOpen = false;
  $("drawer-body").innerHTML = `
    <div class="drawer-head"><h2>Restore from a backup</h2><button class="btn ghost small" data-close>Close</button></div>
    <p class="hint">Brings a backup's notes, photos and learned choices onto this device. Notes already here are left alone; notes that were waiting to sync are queued again. This device keeps its own PIN.</p>
    <form id="restore-form" autocomplete="off">
      <div class="field"><label for="restore-file">Backup file (.fmbackup)</label><input id="restore-file" type="file" accept=".fmbackup"></div>
      <div class="field"><label for="restore-pass">Passphrase</label><input id="restore-pass" type="password"></div>
      <div class="row between"><span class="lock-error" id="restore-error" role="alert"></span><button class="btn primary" type="submit">Restore</button></div>
    </form>`;
  $("drawer").hidden = false;
  $("restore-form").onsubmit = async (event) => {
    event.preventDefault();
    const file = $("restore-file").files[0];
    if (!file) { $("restore-error").textContent = "Choose a backup file."; return; }
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("passphrase", $("restore-pass").value);
    const response = await fetch("/api/restore", { method: "POST", body: form });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { $("restore-error").textContent = data.detail || "Restore failed."; return; }
    closeDrawer();
    toast(`Restored ${data.memories} memories and ${data.photos} photos from ${data.from_device}.`);
    poll(); refresh();
  };
}

// ---------------------------------------------------------------- demo guide

const GUIDE = [
  { title: "Bring headquarters knowledge onto the device",
    text: "Headquarters publishes manuals to the cloud. The device pulls them into its replica shard.",
    done: (s) => s.memory.replica > 0, action: ["Publish manuals", "seed-cloud"] },
  { title: "Lose the network",
    text: "Turn the Network switch off at the top. The device keeps everything it already has.",
    done: () => seen("offline") },
  { title: "Record notes while offline",
    text: "Write your own note, or load samples. Watch the activity: each note is kept private, shared, or shared with names masked.",
    done: (s) => s.memory.local > 0, action: ["Load sample notes", "seed"] },
  { title: "Search and ask with no network",
    text: "On the Memory page try: is a vibration of 7.2 mm/s acceptable. Results come back in about a millisecond with 0 network calls.",
    done: () => seen("search") },
  { title: "Reconnect and sync",
    text: "Turn Network back on. The queue on the Sync page drains, urgent notes first.",
    done: (s) => s.link.online && s.outbox.done > 0 && s.outbox.pending === 0 },
  { title: "Check what actually left the device",
    text: "Open the Cloud page. Private notes are absent; names and phone numbers are masked.",
    done: () => seen("cloud") },
  { title: "Make two devices disagree",
    text: "Stage it in one click: another device's edit lands in the cloud while this one edits the same note differently. Or do it by hand with two browsers. Either way the Sync page shows both versions and nothing is overwritten.",
    done: (s) => s.open_conflicts > 0 || s.sync.totals.conflicts > 0 || seen("conflict"), action: ["Stage a conflict", "stage-conflict"] },
  { title: "Restore the replica from a cloud snapshot",
    text: "On the Sync page, choose Rebuild cloud replica. The device downloads one Qdrant Server snapshot. Private notes are untouched.",
    done: () => seen("snapshot") },
];

function renderGuide(force = false) {
  const s = state.status;
  if (!s) return;
  const done = GUIDE.map((step) => Boolean(step.done(s)));
  const key = done.join("");
  if (!force && key === state.guideKey) return;
  const before = state.guideKey || "";
  state.guideKey = key;
  const next = done.indexOf(false);
  const steps = GUIDE.map((step, i) => `
    <div class="guide-step ${done[i] ? "done" : i === next ? "next" : ""} ${done[i] && before[i] === "0" ? "just-done" : ""} ${force ? "reveal" : ""}" style="--i:${i}">
      <span class="guide-mark">${done[i] ? "✓" : i + 1}</span>
      <div><h3>${esc(step.title)}</h3><p>${esc(step.text)}</p>
        ${step.action && !done[i] ? `<button class="btn small" data-guide="${step.action[1]}">${esc(step.action[0])}</button>` : ""}
      </div>
    </div>`).join("");
  $("drawer-body").innerHTML = `
    <div class="drawer-head"><h2>Demo guide</h2><button class="btn ghost small" data-close>Close</button></div>
    <p class="guide-progress">${done.filter(Boolean).length} of ${GUIDE.length} done on ${esc(s.device.id)}. Steps tick themselves off as you go.</p>
    ${steps}`;
}

function openGuide() {
  closeMenu();
  state.guideOpen = true;
  renderGuide(true);
  $("drawer").hidden = false;
}

// ---------------------------------------------------------------- sync

function diff(text, other) {
  const seenWords = new Set(other.toLowerCase().split(/\s+/));
  return text.split(/(\s+)/).map((word) =>
    !word.trim() || seenWords.has(word.toLowerCase()) ? esc(word) : `<mark>${esc(word)}</mark>`).join("");
}

function showQueue(tab) {
  state.queueTab = tab;
  document.querySelectorAll("#queue-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.queue === tab));
  $("outbox").hidden = tab !== "pending";
  $("runs").hidden = tab !== "synced";
  $("conflicts").hidden = tab !== "conflicts";
}

async function loadSync() {
  const data = await api("/api/sync").catch(() => null);
  if (!data) return;
  const OPS = { upsert: "Send", delete: "Delete", retract: "Withdraw", photo: "Send photo", photo_delete: "Remove photo" };
  const link = data.link;
  const pending = data.outbox.filter((o) => o.state === "pending");
  const failed = data.outbox.filter((o) => o.state === "failed");
  const conflicts = data.conflicts.length;

  let cls = "", iconName = "cloud-check", title = "All caught up", text = "Your notes are synced with the cloud.";
  if (!link.online) { cls = "offline"; iconName = "cloud-off"; title = "Working offline"; text = `${pending.length} change${pending.length === 1 ? "" : "s"} waiting. Everything syncs when the link returns.`; }
  else if (conflicts) { cls = "attention"; iconName = "merge"; title = `${conflicts} need${conflicts === 1 ? "s" : ""} a decision`; text = "Two devices changed the same note. Nothing was overwritten; choose below."; }
  else if (failed.length) { cls = "attention"; iconName = "sync"; title = `${failed.length} set aside`; text = "A change kept failing while the cloud was up. Retry it from the queue."; }
  else if (pending.length) { cls = ""; iconName = "sync"; title = `${pending.length} change${pending.length === 1 ? "" : "s"} waiting`; text = "Sending now, urgent notes first."; }
  $("sync-state-card").className = `card sync-state ${cls}`;
  $("sync-state-card").innerHTML = `<span class="big-icon">${icon(iconName)}</span>
    <div><h2>${esc(title)}</h2><p>${esc(text)}</p></div>
    <button class="btn primary small" id="sync-now-2" ${link.online ? "" : "disabled"}>Sync now</button>`;
  $("sync-now-2").onclick = () => $("sync-now").click();

  $("q-pending").textContent = `(${pending.length + failed.length})`;
  $("q-conflicts").textContent = `(${conflicts})`;

  const queueItem = (o, i) => `
      <div class="queue-item ${o.state === "failed" ? "failed" : ""}">
        <span class="order">${o.state === "failed" ? "!" : i + 1}</span>
        <div><div>${esc(o.text || "(removed note)")}</div>
          <div class="when">${OPS[o.op] || o.op} · queued ${ago(o.created_at)}${o.last_error ? ` · ${esc(o.last_error)}` : ""}</div></div>
        <span class="badge ${o.priority === 2 ? "urgent" : ""}">${PRIORITY[o.priority]}</span>
      </div>`;
  $("outbox").innerHTML = (pending.length ? pending.map(queueItem).join("")
    : emptyState("inbox", "No pending items", link.online ? "Everything is synced. New notes will appear here when you're offline." : "New notes will wait here until the link returns."))
    + (failed.length ? `<div class="notice warn" style="margin-top:10px">${failed.length} change${failed.length === 1 ? "" : "s"}
        set aside after repeated failures. <button class="link-btn" id="retry-failed">Try again</button></div>${failed.map(queueItem).join("")}` : "");
  if (failed.length) {
    $("retry-failed").onclick = async (event) => {
      const result = await attempt(() => api("/api/sync/retry", { method: "POST" }), event.target);
      if (result) { toast(`Retrying ${result.retried}.`); poll(); loadSync(); }
    };
  }

  const t = data.totals;
  const peers = (link.peers || []).map((p) => p.device
    ? `${esc(p.device)} · ${p.received} received · seen ${ago(p.at)}`
    : `${esc(p.url)} · not reached yet`).join("<br>");
  $("sync-summary").innerHTML = `<dl class="kv">
    <dt>Cloud</dt><dd class="mono">${esc(link.cloud_url)}</dd>
    <dt>Link</dt><dd>${link.online ? "Up" : link.forced_offline ? "Network off on this device" : "Cloud unreachable"}</dd>
    ${peers ? `<dt>Nearby devices</dt><dd>${peers}<div class="hint">Consulted over the local network when the cloud is out of reach.</div></dd>` : ""}
    <dt>Successful syncs</dt><dd>${t.runs}</dd>
    <dt>Sent</dt><dd>${t.pushed} memories · ${bytes(t.bytes_up)}</dd>
    <dt>Received</dt><dd>${t.pulled} memories · ${bytes(t.bytes_down)}</dd>
    <dt>Merged automatically</dt><dd>${t.merged}</dd>
    <dt>Conflicts raised</dt><dd>${t.conflicts}</dd>
  </dl>`;

  $("conflicts").innerHTML = data.conflicts.length ? data.conflicts.map((c) => {
    const mine = c.local_payload, theirs = c.cloud_payload, base = c.base_payload;
    return `<div class="conflict-card" data-conflict="${c.id}">
      <div class="badges"><span class="badge conflict">Both changed: ${esc(c.fields.join(", "))}</span>
        ${mine.asset ? `<span class="badge asset">${esc(mine.asset)}</span>` : ""}
        <span class="when">found ${ago(c.detected_at)}</span></div>
      <div class="versions">
        <div class="version"><h3>This device</h3>${diff(mine.text || "", theirs.text || "")}</div>
        <div class="version"><h3>${esc(theirs.device_id)}, already in the cloud</h3>${diff(theirs.text || "", mine.text || "")}</div>
      </div>
      ${base ? `<div class="base">Both started from: “${esc(base.text)}”</div>` : ""}
      <div class="row wrap">
        <button class="btn small" data-resolve="mine">Keep mine</button>
        <button class="btn small" data-resolve="theirs">Accept theirs</button>
        <button class="btn small" data-resolve="both">Keep both</button>
        <button class="btn small" data-combine>Combine…</button>
      </div>
      <div class="combine" hidden>
        <div class="field"><label>Combined note</label><textarea rows="3">${esc(mine.text || "")}</textarea></div>
        <div class="row"><button class="btn small primary" data-resolve="merge">Save combined note</button></div>
      </div>
    </div>`;
  }).join("") : emptyState("check", "No conflicts", "When two devices change the same note differently, both versions show up here.");

  $("runs").innerHTML = data.runs.length ? `<table>
    <thead><tr><th>When</th><th>Started by</th><th>Result</th><th class="num">Sent</th><th class="num">Received</th>
      <th class="num">Merged</th><th class="num">Conflicts</th><th class="num">Data moved</th></tr></thead>
    <tbody>${data.runs.map((r) => `<tr>
      <td>${clock(r.started_at)}</td><td>${esc(r.trigger)}</td>
      <td><span class="badge ${r.status === "ok" ? "synced" : "conflict"}">${r.status === "ok" ? "Done" : esc(r.status)}</span></td>
      <td class="num">${r.pushed}</td><td class="num">${r.pulled}</td><td class="num">${r.merged}</td>
      <td class="num">${r.conflicts}</td><td class="num">${bytes(r.bytes_up + r.bytes_down)}</td></tr>`).join("")}
    </tbody></table>` : emptyState("sync", "No sync has run yet", "Each completed sync is listed here with what moved.");
  showQueue(state.queueTab);
}

// ---------------------------------------------------------------- cloud

function showCloudTab(tab) {
  state.cloudTab = tab;
  document.querySelectorAll("#cloud-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.cloud === tab));
  renderCloudList();
}

function renderCloudList() {
  const data = state.cloud;
  if (!data) return;
  const me = state.status?.device.id;
  const items = data.items.filter((m) =>
    state.cloudTab === "mine" ? m.origin_device === me : state.cloudTab === "redacted" ? Boolean(m.redacted) : true);
  $("cloud-list").innerHTML = items.length ? items.map((m) => `
    <div class="item static">
      ${itemThumb(m)}
      <span class="item-main">
        <span class="item-title">${esc(itemTitle(m))}</span>
        <p class="item-text">${esc(m.text)}</p>
        ${badges({ ...m, source: "replica" }, { cloud: true })}
      </span>
      <span class="item-side"><span class="when">${esc(dateOf(m.updated_at))}</span><span class="badge ${m.redacted ? "redacted" : "shared"}">${m.redacted ? "Redacted" : "Shared"}</span></span>
    </div>`).join("")
    : emptyState("cloud", state.cloudTab === "mine" ? "Nothing from this device yet" : state.cloudTab === "redacted" ? "No masked notes" : "The cloud is empty",
      state.cloudTab === "mine" ? "Shared notes appear here once they have synced." : "Private notes never appear here.");
}

async function loadCloud() {
  const data = await api("/api/cloud").catch(() => null);
  if (!data) return;
  const memory = state.status?.memory || {};
  const me = state.status?.device.id;
  state.cloud = data;
  if (!data.online) {
    $("cloud-hint").textContent = "The cloud cannot be reached right now. Everything else keeps working from device memory.";
    $("cloud-compare").innerHTML = "";
    $("cloud-list").innerHTML = emptyState("cloud-off", "Working offline", "The cloud view returns when the link is up.");
    return;
  }
  $("cloud-hint").textContent = "Read live from the cloud. Private notes never appear here.";
  const mine = data.items.filter((m) => m.origin_device === me).length;
  $("cloud-compare").innerHTML = `
    <div class="tile"><span class="tile-icon cloud">${icon("cloud")}</span><span class="tile-label">Total notes in the cloud</span><span class="tile-value">${data.total}</span></div>
    <div class="tile"><span class="tile-icon">${icon("users")}</span><span class="tile-label">Shared from this device</span><span class="tile-value">${mine}</span></div>
    <div class="tile"><span class="tile-icon private">${icon("lock")}</span><span class="tile-label">Private, kept here</span><span class="tile-value">${memory.private ?? 0}</span></div>`;
  renderCloudList();
}

// ---------------------------------------------------------------- wiring

function showTab(tab) {
  if (!document.getElementById(`panel-${tab}`)) tab = "work";
  state.tab = tab;
  if (location.hash.slice(1) !== tab) history.replaceState(null, "", `#${tab}`);
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === tab));
  document.querySelectorAll(".panel").forEach((el) => el.classList.toggle("active", el.id === `panel-${tab}`));
  window.scrollTo({ top: 0 });
  refresh();
}

$("tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (tab) showTab(tab.dataset.tab);
});

$("open-composer").addEventListener("click", openComposer);
$("close-composer").addEventListener("click", () => { $("composer").hidden = true; });
$("open-search").addEventListener("click", () => { showTab("memory"); $("query").focus(); });
$("view-activity").addEventListener("click", openActivity);
$("activity-btn").addEventListener("click", openActivity);

$("note").addEventListener("input", schedulePreview);
$("scope-override").addEventListener("change", loadPreview);
$("add-photo").addEventListener("click", () => $("photo-file").click());
$("photo-file").addEventListener("change", (event) => setPhoto(event.target.files[0]));
$("save-note").addEventListener("click", () => saveNote(false));
$("note").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) saveNote(false);
});

$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.query = $("query").value.trim();
  if (!state.query) { clearSearch(); return; }
  runSearch(true);
});
$("query").addEventListener("input", () => { if (!$("query").value.trim() && state.query) clearSearch(); });
$("mode").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.mode = button.dataset.mode;
  document.querySelectorAll("#mode button").forEach((b) => b.classList.toggle("active", b === button));
  runSearch(false);
});
$("include-old").addEventListener("change", () => runSearch(false));
$("rerank").addEventListener("change", () => runSearch(false));
$("sort").addEventListener("change", (event) => { state.sort = event.target.value; runSearch(false); });

$("memory-filters").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  state.filter = chip.dataset.filter;
  renderFilters();
  if (state.query) { $("query").value = ""; clearSearch(); } else loadMemory();
});
let filterTimer;
$("memory-text").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(loadMemory, 200); });

$("queue-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button) showQueue(button.dataset.queue);
});
$("cloud-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button) showCloudTab(button.dataset.cloud);
});

document.addEventListener("click", (event) => {
  const open = event.target.closest("[data-open]");
  if (open) { state.guideOpen = false; return openMemory(open.dataset.open); }
  const guided = event.target.closest("[data-guide]");
  if (guided) return $(guided.dataset.guide).click();
  if (event.target.closest("[data-close]") || event.target === $("drawer")) return closeDrawer();
  if (event.target.closest("[data-close-activity]") || event.target === $("activity-drawer")) return closeActivity();

  const combine = event.target.closest("[data-combine]");
  if (combine) {
    combine.closest(".conflict-card").querySelector(".combine").hidden = false;
    return;
  }
  const resolve = event.target.closest("[data-resolve]");
  if (resolve) {
    const card = resolve.closest(".conflict-card");
    const body = { choice: resolve.dataset.resolve };
    if (body.choice === "merge") body.text = card.querySelector("textarea").value;
    attempt(() => api(`/api/conflicts/${card.dataset.conflict}/resolve`, { method: "POST", body }), resolve)
      .then((done) => { if (done) { toast("Decision saved."); poll(); loadSync(); } });
  }
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeDrawer(); closeActivity(); closeMenu(); } });

$("network-toggle").addEventListener("change", async (event) => {
  await attempt(() => api("/api/link/offline", { method: "POST", body: { value: !event.target.checked } }));
  poll();
});
$("auto-sync").addEventListener("change", (event) =>
  attempt(() => api("/api/sync/auto", { method: "POST", body: { value: event.target.checked } })));

$("sync-now").addEventListener("click", async (event) => {
  const result = await attempt(() => api("/api/sync/run", { method: "POST" }), event.target);
  if (!result) return;
  if (result.status === "offline") toast("The cloud cannot be reached. Changes stay queued.");
  else if (result.status === "failed") toast(`Sync failed: ${result.error}`);
  else if (result.status === "ok") toast(`Sent ${result.pushed}, received ${result.pulled}.`);
  poll();
});
$("rebuild").addEventListener("click", async (event) => {
  const result = await attempt(() => api("/api/replica/rebuild", { method: "POST" }), event.target);
  if (result) { toast(`Replica cleared (${result.cleared}) and refilled.`); poll(); }
});

$("seed").addEventListener("click", async (event) => {
  const result = await attempt(() => api("/api/demo/seed", { method: "POST" }), event.target);
  if (result) { toast(`Added ${result.created} sample notes.`); poll(); }
});
$("seed-cloud").addEventListener("click", async (event) => {
  const result = await attempt(() => api("/api/demo/seed-cloud", { method: "POST" }), event.target);
  if (result) { toast(`Published ${result.published} manuals to the cloud.`); poll(); }
});
$("stage-conflict").addEventListener("click", async (event) => {
  const result = await attempt(() => api("/api/demo/conflict", { method: "POST" }), event.target);
  if (!result) return;
  toast(`${result.other_device} and this device now disagree.`);
  seen("conflict", true);
  closeDrawer();
  state.queueTab = "conflicts";
  showTab("sync");
  poll();
});

function closeMenu() { $("settings-menu").hidden = true; $("settings").setAttribute("aria-expanded", "false"); }
function toggleMenu(event) {
  event.stopPropagation();
  const open = $("settings-menu").hidden;
  $("settings-menu").hidden = !open;
  $("settings").setAttribute("aria-expanded", String(open));
}
$("settings").addEventListener("click", toggleMenu);
$("settings-link").addEventListener("click", toggleMenu);
$("device-menu-btn").addEventListener("click", toggleMenu);
document.addEventListener("click", (event) => { if (!event.target.closest(".menu-wrap")) closeMenu(); });
$("guide").addEventListener("click", openGuide);
$("guide-menu").addEventListener("click", openGuide);
$("change-pin").addEventListener("click", () => { closeMenu(); openPinChange(); });
$("backup").addEventListener("click", () => { closeMenu(); openBackup(); });
$("restore").addEventListener("click", () => { closeMenu(); openRestore(); });
$("lock-form").addEventListener("submit", submitLock);
$("lock-now").addEventListener("click", async () => {
  closeMenu();
  await api("/api/auth/logout", { method: "POST" }).catch(() => null);
  showLock();
});

$("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("fieldmind-theme", root.dataset.theme); } catch (error) { /* storage unavailable */ }
});
try {
  const saved = localStorage.getItem("fieldmind-theme");
  if (saved) document.documentElement.dataset.theme = saved;
} catch (error) { /* storage unavailable */ }

// ---------------------------------------------------------------- voice

// Preferred path: record a few seconds here, transcribe on the device (Whisper).
// Works in every browser and with no network. The browser's own recogniser is
// only a fallback for devices where speech to text is turned off.
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let listening = null;
let recording = null;

function setButtonLabel(button, html) {
  if (!button.dataset.idle) button.dataset.idle = button.innerHTML;
  button.innerHTML = html === null ? button.dataset.idle : html;
}

function stopDictation() {
  if (listening) { try { listening.recognition.stop(); } catch (error) { /* already stopped */ } }
}

function startDictation(button, onDevice = true) {
  const target = $(button.dataset.dictate);
  const recognition = new Recognition();
  recognition.lang = navigator.language || "en-IN";
  recognition.interimResults = true;
  recognition.continuous = false;
  if (onDevice) { try { recognition.processLocally = true; } catch (error) { onDevice = false; } }
  let retry = false;
  const before = target.value ? `${target.value.trim()} ` : "";
  recognition.onresult = (event) => {
    const heard = Array.from(event.results).map((r) => r[0].transcript).join(" ").trim();
    target.value = before + heard;
    target.dispatchEvent(new Event("input"));
  };
  recognition.onerror = (event) => {
    if ((event.error === "language-not-supported" || event.error === "service-not-allowed") && onDevice) { retry = true; return; }
    if (event.error === "not-allowed") toast("Microphone access was refused.");
    else if (event.error === "network") toast("This browser's speech service needs internet. Type the note instead.");
    else if (event.error === "language-not-supported") toast(`This browser cannot recognise ${recognition.lang}. Type the note instead.`);
    else if (event.error !== "aborted" && event.error !== "no-speech") toast(`Dictation stopped: ${event.error}`);
  };
  const watchdog = setTimeout(() => {
    if (listening && listening.recognition === recognition && !target.value.trim()) {
      toast("This browser's speech recognition did not respond. Type the note instead.");
      try { recognition.abort(); } catch (error) { /* already stopped */ }
    }
  }, 12000);
  recognition.onend = () => {
    clearTimeout(watchdog);
    if (retry) { startDictation(button, false); return; }
    button.classList.remove("listening");
    setButtonLabel(button, null);
    listening = null;
    if (button.dataset.submit && target.value.trim()) $(button.dataset.submit).requestSubmit();
    else target.focus();
  };
  setButtonLabel(button, button.classList.contains("icon") ? "■" : "■ Listening…");
  button.classList.add("listening");
  listening = { recognition, button };
  recognition.start();
}

function recorderMime() {
  for (const type of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

async function startRecording(button) {
  const target = $(button.dataset.dictate);
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("The microphone needs a secure address (https or localhost). Open the dashboard over https to dictate.");
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (error) {
    toast(error.name === "NotAllowedError" ? "Microphone access was refused." : `No microphone: ${error.message}`);
    return;
  }
  const mime = recorderMime();
  const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
  recorder.onstop = async () => {
    stream.getTracks().forEach((track) => track.stop());
    clearTimeout(recording?.timer);
    recording = null;
    button.classList.remove("listening");
    setButtonLabel(button, "… transcribing");
    button.disabled = true;
    try {
      const form = new FormData();
      form.append("file", new Blob(chunks, { type: recorder.mimeType || mime || "audio/webm" }), "dictation.webm");
      form.append("language", (navigator.language || "en").slice(0, 2));
      const response = await fetch("/api/transcribe", { method: "POST", body: form });
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) showLock();
      if (!response.ok) throw new Error(data.detail || `Transcription failed (${response.status})`);
      if (data.text) {
        target.value = (target.value.trim() ? `${target.value.trim()} ` : "") + data.text;
        target.dispatchEvent(new Event("input"));
        if (button.dataset.submit) $(button.dataset.submit).requestSubmit();
      } else {
        toast("Nothing was heard. Try again a little closer to the microphone.");
      }
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
      setButtonLabel(button, null);
      target.focus();
    }
  };
  setButtonLabel(button, button.classList.contains("icon") ? "■" : "■ Stop");
  button.classList.add("listening");
  recording = { recorder, button, timer: setTimeout(() => recorder.state === "recording" && recorder.stop(), 30000) };
  recorder.start();
}

function stopRecording() {
  if (recording && recording.recorder.state === "recording") recording.recorder.stop();
}

document.querySelectorAll("[data-dictate]").forEach((button) => {
  button.addEventListener("click", () => {
    if (recording) { stopRecording(); return; }
    if (listening) { stopDictation(); return; }
    const engine = state.status?.engine || {};
    if (engine.speech_model) return startRecording(button);
    if (engine.speech_loading) return toast("The speech model is still loading on the device. Try again in a moment.");
    if (engine.speech_error) return toast(`Speech to text failed to load on the device: ${engine.speech_error}`);
    if (Recognition) startDictation(button);
    else toast("This browser has no speech recognition and the device's speech model is turned off.");
  });
});

function updateDictateButtons(engine) {
  const usable = Boolean(engine?.speech_enabled || Recognition);
  document.querySelectorAll("[data-dictate]").forEach((button) => {
    button.hidden = !usable;
    button.title = engine?.speech_model
      ? "Dictate. Recorded here, transcribed on this device by Whisper. Nothing leaves the device."
      : engine?.speech_loading ? "Dictate. The speech model is still loading."
      : engine?.speech_error ? `Speech to text failed to load: ${engine.speech_error}`
      : "Dictate. Uses the browser's speech service.";
  });
}

// ---------------------------------------------------------------- install

if ("serviceWorker" in navigator && (location.protocol === "https:" || ["localhost", "127.0.0.1"].includes(location.hostname))) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* the dashboard works without it */ });
}

// Deep links: #sync opens a page, ?q=... runs a search, ?note=... drafts a note.
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
if (location.hash) showTab(location.hash.slice(1));
const params = new URLSearchParams(location.search);
if (params.get("theme")) document.documentElement.dataset.theme = params.get("theme");
if (params.get("note")) {
  openComposer();
  $("note").value = params.get("note");
  schedulePreview();
}
const linked = params.get("q");
if (linked) {
  $("query").value = linked;
  state.query = linked;
  showTab("memory");
  runSearch(true);
}

poll();
setInterval(poll, 1500);
