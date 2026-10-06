"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  status: null,
  tab: "work",
  mode: "hybrid",
  filter: "all",
  lastEvent: 0,
  firstEvents: true,
  query: "",
  previewSeq: 0,
  searchSeq: 0,
  guideOpen: false,
};

const SCOPE = {
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
  $("lock-title").textContent = state.setup ? `Set a PIN for ${info.device}` : `${info.device} is locked`;
  $("lock-hint").textContent = state.setup
    ? (info.can_set_up ? "Choose a PIN of at least 4 characters. It protects every note on this device."
      : "This device has no PIN yet. Set the first PIN on the device itself.")
    : "Enter the PIN for this device.";
  $("lock-confirm").hidden = !state.setup;
  $("lock-submit").textContent = state.setup ? "Set PIN" : "Unlock";
  $("lock-submit").disabled = state.setup && !info.can_set_up;
  $("lock-error").textContent = "";
  $("lock-pin").value = "";
  $("lock-confirm").value = "";
  $("lock").hidden = false;
  $("lock-pin").focus();
}

async function submitLock(event) {
  event.preventDefault();
  const pin = $("lock-pin").value;
  if (state.setup && pin !== $("lock-confirm").value) {
    $("lock-error").textContent = "The two PINs do not match.";
    return;
  }
  try {
    await api(state.setup ? "/api/auth/setup" : "/api/auth/login", { method: "POST", body: { pin } });
  } catch (error) {
    $("lock-error").textContent = error.message;
    $("lock-pin").select();
    return;
  }
  state.locked = false;
  $("lock").hidden = true;
  state.lastEvent = 0;
  state.firstEvents = true;
  $("events").innerHTML = "";
  poll();
  refresh();
}

// ---------------------------------------------------------------- badges

function badges(m, options = {}) {
  const out = [];
  if (m.asset) out.push(`<span class="badge asset">${esc(m.asset)}</span>`);
  if (m.source === "replica" || options.cloud) {
    out.push(`<span class="badge replica">${m.via_peer ? `From ${esc(m.via_peer)} nearby` : "From cloud"} · ${esc(m.origin_device || m.device_id || "unknown")}</span>`);
    if (m.redacted) out.push(`<span class="badge">Details masked</span>`);
  } else {
    out.push(`<span class="badge ${esc(m.scope)}">${esc(SCOPE[m.scope] || m.scope)}</span>`);
    if (SYNC[m.sync_state]) out.push(`<span class="badge ${esc(m.sync_state)}">${SYNC[m.sync_state]}</span>`);
    if (m.origin_device && m.mine === false) out.push(`<span class="badge">First written on ${esc(m.origin_device)}</span>`);
  }
  if (m.priority === 2 && m.scope !== "private") out.push(`<span class="badge urgent">Urgent</span>`);
  if (m.status === "superseded") out.push(`<span class="badge">Replaced</span>`);
  if (m.relation && m.supersedes) out.push(`<span class="badge">${esc(RELATION[m.relation] || "")}</span>`);
  return `<div class="badges">${out.join("")}</div>`;
}

function thumbs(photos, size = "") {
  if (!photos || !photos.length) return "";
  return `<div class="thumbs ${size}">${photos.map((p) =>
    `<img src="${esc(p.url)}" alt="Photo attached to this note" loading="lazy" width="${p.width}" height="${p.height}">`).join("")}</div>`;
}

function memoryItem(m, extra = "") {
  return `<button class="item ${m.status === "superseded" ? "old" : ""}" data-open="${esc(m.id)}">
    <p class="item-text">${esc(m.text)}</p>
    ${thumbs(m.photos)}
    ${extra}
    <div class="item-foot">${badges(m)}<span class="when">${ago(m.updated_at)}</span></div>
  </button>`;
}

// ---------------------------------------------------------------- status

function renderStatus(s) {
  state.status = s;
  const { link, memory, outbox, device } = s;
  $("device-line").textContent = `${device.id} · ${device.site}`;
  document.title = `${device.id} · FieldMind`;

  const box = document.querySelector(".link-state");
  box.className = `link-state ${link.online ? "online" : "offline"}`;
  $("link-title").textContent = link.online ? "Connected to cloud" : "Working offline";
  if (link.online) {
    const last = s.sync.last_sync_at;
    $("link-sub").textContent = last ? `In sync, checked ${ago(last)}` : "Not synced yet";
  } else if (link.forced_offline) {
    $("link-sub").textContent = `Network off for ${span(s.now - (link.offline_since || s.now))}`;
  } else {
    $("link-sub").textContent = `Cloud unreachable for ${span(s.now - (link.offline_since || s.now))}`;
  }
  $("network-toggle").checked = !link.forced_offline;
  $("auto-sync").checked = link.auto_sync;
  $("sync-now").disabled = !link.online;

  const waiting = outbox.pending || 0;
  const stats = [
    ["Written on this device", memory.local, ""],
    ["Received from cloud", memory.replica, ""],
    ["Kept private", memory.private, ""],
    ["Waiting to sync", waiting, waiting ? "attention" : ""],
    ["Need a decision", s.open_conflicts, s.open_conflicts ? "alert" : ""],
    ["Replaced by newer notes", memory.superseded, ""],
  ];
  $("stats").innerHTML = stats.map(([label, value, cls]) => `
    <div class="stat ${cls}"><div class="stat-label">${label}</div><div class="stat-value">${value}</div></div>`).join("");

  const attention = waiting + s.open_conflicts;
  $("tab-sync-count").textContent = attention ? String(attention) : "";

  const disk = s.shards.local.disk_bytes + s.shards.replica.disk_bytes;
  $("engine").innerHTML = `<h2>On this device</h2>
    <dl class="kv">
      <dt>Vector store</dt><dd>${esc(s.engine.vector_store)}</dd>
      <dt>Meaning model</dt><dd>${esc(s.engine.dense_model.split("/").pop())} · ${s.engine.dimensions}d</dd>
      <dt>Keyword model</dt><dd>${esc(s.engine.sparse_model)}</dd>
      <dt>Name masking</dt><dd>${esc(s.engine.name_model || "Title rules only")}</dd>
      <dt>Reranking</dt><dd>${esc(s.engine.reranker || "Off")}</dd>
      <dt>Learned choices</dt><dd>${s.engine.learned_examples || 0} from this device's overrides</dd>
      <dt>Answers</dt><dd>${esc(s.engine.answer_model || "Composed from notes")}</dd>
      <dt>Photos</dt><dd>${s.engine.vision_model ? `${esc(s.engine.vision_model)} · ${s.memory.photos || 0} stored` : s.engine.photos ? "Vision model loading…" : "Off"}</dd>
      <dt>On disk</dt><dd>${s.engine.sealed ? "Private text and activity sealed with the device key" : "Not sealed"}</dd>
      <dt>Storage reserved</dt><dd>${bytes(disk)}</dd>
    </dl>`;
  $("rerank-label").hidden = !s.engine.reranker;
  $("add-photo").hidden = !s.engine.photos;
  renderFilters();
  if (!link.online) seen("offline", true);
  if (state.guideOpen) renderGuide();
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
    state.firstEvents = false;
    if (changed) refresh();
  } catch (error) {
    if (state.locked) return;
    $("link-title").textContent = "Device not responding";
    $("link-sub").textContent = "Is the FieldMind process running?";
  }
}

function refresh() {
  if (state.tab === "memory") loadMemory();
  if (state.tab === "sync") loadSync();
  if (state.tab === "cloud") { loadCloud(); seen("cloud", true); }
  if (state.tab === "work" && state.query) runSearch(false);
}

// ---------------------------------------------------------------- capture

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
      <span class="preview-title">${esc(SCOPE[d.scope])}</span>
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

// ---------------------------------------------------------------- search

async function runSearch(withAnswer = true) {
  const query = state.query;
  if (!query) return;
  const seq = ++state.searchSeq;
  seen("search", true);
  const body = { query, mode: state.mode, limit: 8, include_superseded: $("include-old").checked, rerank: $("rerank").checked };
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
    `<b>${t.search} ms</b> search · ${t.embed} ms to read the question${found.reranked ? ` · ${t.rerank} ms to rerank` : ""} · ${total} memories · <b>${found.network_calls} network calls</b>`;

  $("results").innerHTML = found.results.length ? found.results.map((r) => {
    const parts = [];
    if (r.matched.semantic !== undefined) {
      parts.push(`<span class="signal">Meaning <span class="bar"><span style="width:${Math.round(r.strength.semantic * 100)}%"></span></span> ${r.matched.semantic.toFixed(2)}</span>`);
    }
    if (r.matched.keyword !== undefined) {
      parts.push(`<span class="signal">Keywords <span class="bar"><span style="width:${Math.round(r.strength.keyword * 100)}%"></span></span> ${r.matched.keyword.toFixed(2)}</span>`);
    }
    if (r.matched.rerank !== undefined) {
      parts.push(`<span class="signal">Read together <span class="bar"><span style="width:${Math.round(r.strength.rerank * 100)}%"></span></span> ${r.matched.rerank.toFixed(2)}</span>`);
    }
    if (r.matched.image !== undefined) {
      parts.push(`<span class="signal">Photo <span class="bar"><span style="width:${Math.round(r.strength.image * 100)}%"></span></span> ${r.matched.image.toFixed(2)}</span>`);
    }
    return memoryItem(r, `<div class="signals">${parts.join("")}</div>`);
  }).join("") : `<div class="empty-state">Nothing in device memory matches that.</div>`;
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

// ---------------------------------------------------------------- memory

const FILTERS = [
  ["all", "All", {}, (m) => m.local + m.replica],
  ["local", "Written here", { source: "local" }, (m) => m.local],
  ["replica", "From cloud", { source: "replica" }, (m) => m.replica],
  ["private", "Private", { scope: "private" }, (m) => m.private],
  ["pending", "Waiting to sync", { sync_state: "pending" }, (m) => m.pending],
  ["conflict", "Need a decision", { sync_state: "conflict" }, (m) => m.conflict],
  ["superseded", "Replaced", { status: "superseded" }, (m) => m.superseded],
];

function renderFilters() {
  const memory = state.status?.memory;
  if (!memory) return;
  $("memory-filters").innerHTML = FILTERS.map(([key, label, , count]) =>
    `<button class="chip ${state.filter === key ? "active" : ""}" data-filter="${key}">${label}<span>${count(memory)}</span></button>`).join("");
}

async function loadMemory() {
  const params = new URLSearchParams(FILTERS.find((f) => f[0] === state.filter)[2]);
  const text = $("memory-text").value.trim();
  if (text) params.set("text", text);
  const data = await api(`/api/memories?${params}`).catch(() => null);
  if (!data) return;
  $("memory-list").innerHTML = data.items.length
    ? data.items.map((m) => memoryItem(m)).join("")
    : `<div class="empty-state">No memories here yet.</div>`;
}

// ---------------------------------------------------------------- drawer

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
    <div class="drawer-head"><h2>Memory</h2><button class="btn ghost small" data-close>Close</button></div>
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

// ---------------------------------------------------------------- demo guide

const GUIDE = [
  { title: "Bring headquarters knowledge onto the device",
    text: "Headquarters publishes manuals to the cloud. The device pulls them into its replica shard.",
    done: (s) => s.memory.replica > 0, action: ["Publish manuals", "seed-cloud"] },
  { title: "Lose the network",
    text: "Turn the Network switch off at the top. The device keeps everything it already has.",
    done: () => seen("offline") },
  { title: "Record notes while offline",
    text: "Write your own note, or load samples. Watch the activity log: each note is kept private, shared, or shared with names masked.",
    done: (s) => s.memory.local > 0, action: ["Load sample notes", "seed"] },
  { title: "Search and ask with no network",
    text: "Try: is a vibration of 7.2 mm/s acceptable. Results come back in about a millisecond with 0 network calls.",
    done: () => seen("search") },
  { title: "Reconnect and sync",
    text: "Turn Network back on. The queue on the Sync tab drains, urgent notes first.",
    done: (s) => s.link.online && s.outbox.done > 0 && s.outbox.pending === 0 },
  { title: "Check what actually left the device",
    text: "Open the Cloud tab. Private notes are absent; names and phone numbers are masked.",
    done: () => seen("cloud") },
  { title: "Make two devices disagree",
    text: "On this device and another, turn Network off, edit the same shared note differently, then reconnect one after the other. The second shows both versions on its Sync tab.",
    done: (s) => s.open_conflicts > 0 || s.sync.totals.conflicts > 0 || seen("conflict") },
  { title: "Restore the replica from a cloud snapshot",
    text: "On the Sync tab, choose Rebuild cloud replica. The device downloads one Qdrant Server snapshot. Private notes are untouched.",
    done: () => seen("snapshot") },
];

function renderGuide() {
  const s = state.status;
  if (!s) return;
  const done = GUIDE.map((step) => Boolean(step.done(s)));
  const next = done.indexOf(false);
  const steps = GUIDE.map((step, i) => `
    <div class="guide-step ${done[i] ? "done" : i === next ? "next" : ""}">
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
  state.guideOpen = true;
  renderGuide();
  $("drawer").hidden = false;
}

// ---------------------------------------------------------------- sync

function diff(text, other) {
  const seen = new Set(other.toLowerCase().split(/\s+/));
  return text.split(/(\s+)/).map((word) =>
    !word.trim() || seen.has(word.toLowerCase()) ? esc(word) : `<mark>${esc(word)}</mark>`).join("");
}

async function loadSync() {
  const data = await api("/api/sync").catch(() => null);
  if (!data) return;
  const OPS = { upsert: "Send", delete: "Delete", retract: "Withdraw" };

  const pending = data.outbox.filter((o) => o.state === "pending");
  const failed = data.outbox.filter((o) => o.state === "failed");
  const queueItem = (o, i) => `
      <div class="queue-item ${o.state === "failed" ? "failed" : ""}">
        <span class="order">${o.state === "failed" ? "!" : i + 1}</span>
        <div><div>${esc(o.text || "(removed note)")}</div>
          <div class="when">${OPS[o.op]} · queued ${ago(o.created_at)}${o.last_error ? ` · ${esc(o.last_error)}` : ""}</div></div>
        <span class="badge ${o.priority === 2 ? "urgent" : ""}">${PRIORITY[o.priority]}</span>
      </div>`;
  $("outbox").innerHTML = (pending.length ? pending.map(queueItem).join("")
    : `<div class="empty-state">Nothing waiting. This device and the cloud agree.</div>`)
    + (failed.length ? `<div class="notice warn" style="margin-top:10px">${failed.length} change${failed.length === 1 ? "" : "s"}
        set aside after repeated failures. <button class="link-btn" id="retry-failed">Try again</button></div>${failed.map(queueItem).join("")}` : "");
  if (failed.length) {
    $("retry-failed").onclick = async (event) => {
      const result = await attempt(() => api("/api/sync/retry", { method: "POST" }), event.target);
      if (result) { toast(`Retrying ${result.retried}.`); poll(); loadSync(); }
    };
  }

  const t = data.totals;
  const link = data.link;
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
  }).join("") : `<div class="empty-state">No conflicts.</div>`;

  $("runs").innerHTML = data.runs.length ? `<table>
    <thead><tr><th>When</th><th>Started by</th><th>Result</th><th class="num">Sent</th><th class="num">Received</th>
      <th class="num">Merged</th><th class="num">Conflicts</th><th class="num">Data moved</th></tr></thead>
    <tbody>${data.runs.map((r) => `<tr>
      <td>${clock(r.started_at)}</td><td>${esc(r.trigger)}</td>
      <td><span class="badge ${r.status === "ok" ? "synced" : "conflict"}">${r.status === "ok" ? "Done" : esc(r.status)}</span></td>
      <td class="num">${r.pushed}</td><td class="num">${r.pulled}</td><td class="num">${r.merged}</td>
      <td class="num">${r.conflicts}</td><td class="num">${bytes(r.bytes_up + r.bytes_down)}</td></tr>`).join("")}
    </tbody></table>` : `<div class="empty-state">No sync has run yet.</div>`;
}

// ---------------------------------------------------------------- cloud

async function loadCloud() {
  const data = await api("/api/cloud").catch(() => null);
  if (!data) return;
  const memory = state.status?.memory || {};
  if (!data.online) {
    $("cloud-hint").textContent = "The cloud cannot be reached right now.";
    $("cloud-compare").innerHTML = "";
    $("cloud-list").innerHTML = `<div class="empty-state">This device is working from its own memory.
      Everything on the Work tab still runs. The cloud view returns when the link is up.</div>`;
    return;
  }
  $("cloud-hint").textContent = "Read live from the cloud. Private notes never appear here.";
  $("cloud-compare").innerHTML = `
    <span class="badge replica">${data.total} in the cloud</span>
    <span class="badge private">${memory.private ?? 0} kept only on this device</span>
    <span class="badge pending">${memory.pending ?? 0} waiting to sync</span>`;
  $("cloud-list").innerHTML = data.items.length ? data.items.map((m) => `
    <div class="item static">
      <p class="item-text">${esc(m.text)}</p>
      <div class="item-foot">${badges({ ...m, source: "replica" }, { cloud: true })}
        <span class="when">rev ${m.rev} · ${ago(m.updated_at)}</span></div>
    </div>`).join("") : `<div class="empty-state">The cloud is empty.</div>`;
}

// ---------------------------------------------------------------- wiring

function showTab(tab) {
  if (!document.getElementById(`panel-${tab}`)) tab = "work";
  state.tab = tab;
  if (location.hash.slice(1) !== tab) history.replaceState(null, "", `#${tab}`);
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === tab));
  document.querySelectorAll(".panel").forEach((el) => el.classList.toggle("active", el.id === `panel-${tab}`));
  refresh();
}

$("tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (tab) showTab(tab.dataset.tab);
});

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
  if (!state.query) { $("results").innerHTML = ""; $("answer").innerHTML = ""; $("search-meta").innerHTML = ""; return; }
  runSearch(true);
});
$("mode").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.mode = button.dataset.mode;
  document.querySelectorAll("#mode button").forEach((b) => b.classList.toggle("active", b === button));
  runSearch(false);
});
$("include-old").addEventListener("change", () => runSearch(false));
$("rerank").addEventListener("change", () => runSearch(false));

$("memory-filters").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  state.filter = chip.dataset.filter;
  renderFilters();
  loadMemory();
});
let filterTimer;
$("memory-text").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(loadMemory, 200); });

document.addEventListener("click", (event) => {
  const open = event.target.closest("[data-open]");
  if (open) { state.guideOpen = false; return openMemory(open.dataset.open); }
  const guided = event.target.closest("[data-guide]");
  if (guided) return $(guided.dataset.guide).click();
  if (event.target.closest("[data-close]") || event.target === $("drawer")) return closeDrawer();

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
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });

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

$("guide").addEventListener("click", openGuide);
$("change-pin").addEventListener("click", openPinChange);
$("lock-form").addEventListener("submit", submitLock);
$("lock-now").addEventListener("click", async () => {
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

// Hands are often gloved or dirty in the field. Dictation uses the browser's own
// speech recognition; where the browser can run it on the device (recent Chrome
// and Safari offer this for some languages) it is asked to, otherwise the browser
// sends audio to its vendor's speech service, which needs internet.
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let listening = null;

function stopDictation() {
  if (listening) { try { listening.recognition.stop(); } catch (error) { /* already stopped */ } }
}

function startDictation(button) {
  const target = $(button.dataset.dictate);
  const recognition = new Recognition();
  recognition.lang = navigator.language || "en-IN";
  recognition.interimResults = true;
  recognition.continuous = false;
  try { recognition.processLocally = true; } catch (error) { /* not offered by this browser */ }
  const before = target.value ? `${target.value.trim()} ` : "";
  recognition.onresult = (event) => {
    const heard = Array.from(event.results).map((r) => r[0].transcript).join(" ").trim();
    target.value = before + heard;
    target.dispatchEvent(new Event("input"));
  };
  recognition.onerror = (event) => {
    if (event.error === "not-allowed") toast("Microphone access was refused.");
    else if (event.error === "network") toast("This browser's speech service needs internet. Type the note instead.");
    else if (event.error !== "aborted" && event.error !== "no-speech") toast(`Dictation stopped: ${event.error}`);
  };
  recognition.onend = () => {
    button.classList.remove("listening");
    button.textContent = button.dataset.idle;
    listening = null;
    if (button.dataset.submit && target.value.trim()) $(button.dataset.submit).requestSubmit();
    else target.focus();
  };
  button.dataset.idle = button.textContent;
  button.textContent = button.classList.contains("icon") ? "■" : "■ Listening…";
  button.classList.add("listening");
  listening = { recognition, button };
  recognition.start();
}

if (Recognition) {
  document.querySelectorAll("[data-dictate]").forEach((button) => {
    button.hidden = false;
    button.addEventListener("click", () => {
      if (listening) { stopDictation(); return; }
      startDictation(button);
    });
  });
}

// ---------------------------------------------------------------- install

// Installable on a phone or tablet. The worker caches only the page shell, never
// anything from /api/, so no note text lives in the browser.
if ("serviceWorker" in navigator && (location.protocol === "https:" || ["localhost", "127.0.0.1"].includes(location.hostname))) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* the dashboard works without it */ });
}

// Deep links: #sync opens a tab, ?q=... runs a search, ?note=... drafts a note.
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
if (location.hash) showTab(location.hash.slice(1));
const params = new URLSearchParams(location.search);
if (params.get("theme")) document.documentElement.dataset.theme = params.get("theme");
if (params.get("note")) {
  $("note").value = params.get("note");
  schedulePreview();
}
const linked = params.get("q");
if (linked) {
  $("query").value = linked;
  state.query = linked;
  runSearch(true);
}

poll();
setInterval(poll, 1500);
