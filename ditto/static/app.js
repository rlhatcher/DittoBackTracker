/* The whole page, in one file and in this order:

     module state          what render() and the library view read
     on the pedal          the slot field, loop controls, the track list
     render                the one function the SSE snapshot drives
     DOM helpers           rebuild-with-focus, setText, printing
     talking to the device api(), failFrom(), jsonBody
     the status line       say/warn/fail, and the six-second hold
     uploads               one POST, and what it reports
     listeners             the drop zone, clearing a slot, undo
     the update            check and deploy, and the button's two states
     the event stream      EventSource, and the reconnect grace
     library               fetching, the view, the rows, the ticked set */

const $ = s => document.querySelector(s);

/* ---------------------------------------------------------- module state */

/* render() runs on every SSE frame, up to 5 Hz while a conversion reports
   progress, and rebuilds both lists. Anything the user is part-way through
   lives out here and is read back on the way in, or the rebuild destroys it. */

let state = null;
/* The over-the-air update. doUpdate() sets `fromRev`; render() clears
   `updating` when a snapshot reports a different revision. */
const ota = {
  updating: false,   // an update is in flight, or its restart is pending
  fromRev: null,     // the revision we are leaving; completion is != this
  timer: null,       // the 180s "didn't confirm" safety net
  checking: false,   // a check is in flight
};

/* `state` is the SSE snapshot; `library` is pulled from /api/library and only
   changes when we ask. Which slots hold a track is read from the snapshot, so
   the badges stay live for free. */
let library = null;
let editingHash = null;    // a track rename in progress; freezes renderLibrary
let nowPlaying = null;     // hash being auditioned

/* Each list computes a key from exactly what it draws and skips the rebuild
   when the key is unchanged. Progress is in neither key, which is what keeps a
   conversion from destroying focus five times a second. libRev stands in for
   the library's contents. */
let libRev = 0;
let lastListKey = null, lastLibKey = null;
/* Say the library's DOM no longer matches its key: an inline rename swaps a
   span for an input, and cancelling it leaves the key where it was. */
function libraryDomDirty(){ lastLibKey = null; }

/* Uncommitted text in a pedal row's slot field, by slot, read back when the
   row is rebuilt. */
let slotDraft = {};

/* Which library rows are ticked, by source_hash. */
const checked = new Set();

/* ---------------------------------------------------------- on the pedal */

const pad2 = n => String(n).padStart(2, "0");
// "staged" is the wire word for converted and waiting to be written.
const STATE_LABEL = {converting: "converting", staged: "queued",
                     synced: "on pedal", error: "error"};
const stateLabel = st => STATE_LABEL[st] || st;

function mmss(s){ s=Math.max(0,Math.round(s)); return Math.floor(s/60)+":"+pad2(s%60); }

/* The row's slot number, as a field you can type another one into. Enter or
   blur moves the track there, swapping if the slot is taken; Escape reverts. */
function slotField(r){
  const f = document.createElement("input");
  f.type = "text";
  f.inputMode = "numeric";
  f.maxLength = 2;
  f.className = "slotfield";
  const draft = slotDraft[r.slot];
  f.value = draft !== undefined ? draft : pad2(r.slot);
  f.dataset.fk = "slot:" + r.slot + ":field";
  f.title = `Type another slot number to move “${r.display_name}” there`;
  f.setAttribute("aria-label", `Slot number for ${r.display_name}`);
  f.oninput = () => { slotDraft[r.slot] = f.value; };
  f.onkeydown = e => {
    e.stopPropagation();
    if (e.key === "Enter"){ e.preventDefault(); f.blur(); }
    else if (e.key === "Escape"){ revertSlotField(r.slot); }
  };
  f.onblur = () => commitSlotField(r);
  return f;
}

/* The field's value lives in the DOM, so a refused number only goes away with
   a rebuild. */
function revertSlotField(slot){
  delete slotDraft[slot];
  lastListKey = null;
  if (state) render(state);
}

async function commitSlotField(r){
  if (slotDraft[r.slot] === undefined) return;    // nothing was typed
  const raw = slotDraft[r.slot].trim();
  delete slotDraft[r.slot];
  const max = (state && state.slot_count) || 99;
  const n = /^\d{1,2}$/.test(raw) ? parseInt(raw, 10) : NaN;
  if (!Number.isFinite(n) || n < 1 || n > max){
    warn(`Slot numbers run 01–${pad2(max)}`);
    revertSlotField(r.slot);
    return;
  }
  if (n === r.slot){ revertSlotField(r.slot); return; }
  if (!await moveTo(r.slot, n)) revertSlotField(r.slot);
}

/* Download and delete for a pedal-recorded loop. */
function loopControls(n){
  const pad = pad2(n);
  const dl = document.createElement("a");
  dl.className = "loopbtn";
  dl.href = `/api/loops/${n}`;
  dl.setAttribute("download", `loop-${pad}.wav`);
  dl.textContent = "Download loop";
  dl.title = `Download the loop from slot ${pad} (leaves it on the pedal)`;
  dl.dataset.fk = "slot:" + n + ":loopdl";

  const rm = document.createElement("button");
  rm.type = "button";
  rm.className = "loopbtn danger";
  rm.textContent = "Remove loop";
  rm.title = `Delete the loop in slot ${pad} from the pedal`;
  rm.setAttribute("aria-label", `Delete the recorded loop in slot ${pad}`);
  rm.dataset.fk = "slot:" + n + ":looprm";
  rm.onclick = () => removeLoop(n);
  return [dl, rm];
}

/* What is on the pedal, plus any loop-only slots, in slot order. */
function drawTrackList(list, s, byslot, loops){
  list.innerHTML = "";
  const slotNums = [...new Set([...s.slots.map(x=>x.slot), ...loops])]
    .sort((a,b)=>a-b);
  if (!slotNums.length){
    list.innerHTML = '<div class="empty">Nothing loaded yet.</div>';
  }
  slotNums.forEach(n => {
    const r = byslot[n];
    const el = document.createElement("div");
    el.className = "track";
    el.dataset.slot = n;
    if (r){
      el.innerHTML = `
        <span class="nm">${escapeHtml(r.display_name)}</span>
        <span class="dur">${mmss(r.duration)}</span>
        <span class="st ${r.state}">${stateLabel(r.state)}</span>`;
      el.prepend(slotField(r));
      const x = document.createElement("button");
      x.className = "x"; x.textContent = "×"; x.title = "Clear slot";
      x.setAttribute("aria-label", `Clear slot ${pad2(n)}`);
      x.dataset.fk = "slot:" + r.slot + ":clear";
      x.onclick = () => removeSlot(r.slot, r.display_name);
      el.appendChild(x);
    } else {
      // A loop with no backing track: nothing to clear or move. The loop's own
      // controls, below, are the row's actions.
      el.innerHTML = `
        <span class="num">${pad2(n)}</span>
        <span class="nm loop-only">Recorded loop — kept, never overwritten</span>
        <span class="st loop">loop</span>`;
    }
    list.appendChild(el);
    if (loops.has(n)){
      const lr = document.createElement("div");
      lr.className = "looprow";
      loopControls(n).forEach(c => lr.appendChild(c));
      list.appendChild(lr);
    }
    if (r && r.state === "error" && r.error){
      const e = document.createElement("div");
      e.className = "err"; e.style.cssText = "font-size:12px;padding:0 0 8px 56px";
      e.textContent = r.error;
      list.appendChild(e);
    }
  });
}

/* ------------------------------------------------------------------ render */

function render(s){
  state = s;

  const pd = $("#pedal");
  const map = {mounted:["ok","connected"],absent:["off","no pedal"],error:["off","error"]};
  const [cls,t] = map[s.pedal] || map.absent;
  pd.className = cls;
  pd.innerHTML = `<span class="dot"></span>${t}`;

  if (s.version){
    setText($("#ver"), "v" + s.version + (s.revision ? " · " + s.revision : ""));
  }
  // An update is complete when a snapshot reports a new revision, not when the
  // stream reconnects: that also happens on the routine rotation.
  if (ota.updating && s.revision && s.revision !== ota.fromRev){
    endUpdating();
  }
  updateBtnState(s);

  const cap = s.capacity;
  const frac = cap.total_seconds ? cap.used_seconds/cap.total_seconds : 0;
  setText($("#captxt"), cap.used_label + " of " + cap.total_label);
  const fill = $("#capfill");
  fill.style.width = Math.min(100, frac*100) + "%";
  fill.className = "capfill" + (frac>0.95?" err":frac>0.8?" warn":"");
  const total = s.slot_count || 99;
  setText($("#capnote"), cap.total_seconds
    ? `${mmss(Math.max(0,cap.total_seconds-cap.used_seconds))} free · `
      + `${s.slots.length} of ${total} slots`
    : "connect the pedal to see capacity");

  const byslot = {};
  s.slots.forEach(x => byslot[x.slot] = x);
  const loops = new Set(s.loops || []);

  $("#print").hidden = !s.slots.length;

  const list = $("#list");
  // JSON, not a delimiter join: a name can contain any separator.
  const listKey = JSON.stringify([
    s.slots.map(x => [x.slot, x.display_name, x.duration, x.state, x.error]),
    [...loops],
  ]);
  if (listKey !== lastListKey){
    lastListKey = listKey;
    rebuild(list, () => drawTrackList(list, s, byslot, loops));
  }

  const busy = !!s.busy;
  $("#progwrap").classList.toggle("hide", !busy || s.progress==null);
  if (s.progress!=null){
    const pct = Math.round(s.progress*100);
    $("#progfill").style.width = pct + "%";
    $("#progwrap").setAttribute("aria-valuenow", pct);
  }
  const m = $("#msg");
  if (Date.now() < msgHold) { /* a confirmation owns the line */ }
  else if (s.busy)         { setText(m, s.busy); m.className="msg"; }
  else if (s.error)        { setText(m, s.error); m.className="msg err"; }
  else if (s.pedal==="mounted") { setText(m, "Ready"); m.className="msg"; }
  else                     { setText(m, "Plug in the pedal"); m.className="msg"; }

  renderLibrary();      // its slot badges come from the snapshot
}

/* ----------------------------------------------- DOM helpers, and printing */

/* Rebuild `host` while keeping focus, and the caret, where the user put it.
   Nodes opt in with a data-fk that is stable across rebuilds. */
function rebuild(host, draw){
  const active = document.activeElement;
  const key = active && host.contains(active) ? active.dataset.fk : null;
  const caret = key && active.selectionStart != null
    ? [active.selectionStart, active.selectionEnd] : null;
  draw();
  if (key){
    const again = host.querySelector(`[data-fk="${CSS.escape(key)}"]`);
    if (again){
      again.focus();
      if (caret && again.setSelectionRange){
        try { again.setSelectionRange(caret[0], caret[1]); } catch { /* not a text field */ }
      }
    }
  }
}

/* Write only when the text differs: #msg is an aria-live region, and
   rewriting the same string at 5 Hz would make a screen reader repeat it. */
function setText(el, text){
  if (el.textContent !== text) el.textContent = text;
}

function escapeHtml(s){ const d=document.createElement("div"); d.textContent=s; return d.innerHTML; }

// A bare table of slot number and name, in slot order, with the page margin
// zeroed so the browser has nowhere to print its own header and footer.
function printHtml(s){
  const rows = (s.slots || []).slice().sort((a,b) => a.slot - b.slot);
  const body = rows.length
    ? rows.map(r =>
        `<tr><td class="n">${pad2(r.slot)}</td>` +
        `<td>${escapeHtml(r.display_name)}</td></tr>`).join("")
    : `<tr><td></td><td>No backing tracks loaded.</td></tr>`;
  return `<!doctype html><html><head><meta charset="utf-8">` +
    `<title>DittoBackTracker — backing tracks</title><style>` +
    `@page{margin:0}` +
    `body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;` +
    `margin:16mm;color:#000;background:#fff}` +
    `table{border-collapse:collapse}` +
    `td{padding:2px 0;vertical-align:baseline}` +
    `td.n{padding-right:10px;font-family:ui-monospace,Menlo,monospace;color:#555;text-align:right;white-space:nowrap}` +
    `</style></head><body>` +
    `<table>${body}</table>` +
    `</body></html>`;
}

function printList(){
  if (!state) return;
  const w = window.open("", "_blank");   // synchronously, so it is not a pop-up
  if (!w){ fail("Couldn't open the print view — allow pop-ups for this page"); return; }
  w.document.write(printHtml(state));
  w.document.close();
  w.focus();
  w.print();
}

function updateBtnState(s){
  if (ota.updating || ota.checking) return;  // don't clobber a transient label
  const b = $("#update");
  b.hidden = false;
  b.disabled = false;
  if (s && s.update_available){
    b.textContent = "Update available";
    b.className = "link avail";
    b.title = s.remote_revision ? "New version " + s.remote_revision : "A newer version is available";
  } else {
    b.textContent = "Check for update";
    b.className = "link";
    b.title = "Check GitHub for a newer version";
  }
}

/* --------------------------------------------------- talking to the device */

/* `status` is 0 when the request never reached the device. */
async function api(url, opts){
  try {
    const r = await fetch(url, opts);
    return {ok: r.ok, status: r.status,
            body: await r.json().catch(() => ({}))};
  } catch {
    return {ok: false, status: 0, body: {}};
  }
}

function failFrom(r, what){
  fail(r.status ? (r.body.error || what) : what + " — check the connection");
}

const jsonBody = body => ({method: "POST",
                           headers: {"Content-Type": "application/json"},
                           body: JSON.stringify(body)});

/* --------------------------------------------------------- the status line */

/* render() rewrites #msg from every snapshot, so a confirmation holds the line
   for six seconds or it would be overwritten by "Ready" in the same tick. */
let msgHold = 0, msgTimer = null;
function holdMsg(){
  msgHold = Date.now() + 6000;
  clearTimeout(msgTimer);
  msgTimer = setTimeout(() => { if (state) render(state); }, 6000);
}
function say(text){ setText($("#msg"), text); $("#msg").className = "msg"; holdMsg(); }
function warn(text){ setText($("#msg"), text); $("#msg").className = "msg warn"; holdMsg(); }
function fail(text){ setText($("#msg"), text); $("#msg").className = "msg err"; holdMsg(); }

/* ---------------------------------------------------------------- uploads */

async function moveTo(src, dst){
  const r = await api(`/api/slots/${src}/move`, jsonBody({to: dst}));
  if (!r.ok) failFrom(r, "Move failed");
  return r.ok;
}

/* One multipart POST. A 413, a 500 or a dropped connection carries no
   `errors` list, so each becomes one here. */
async function postFiles(url, files){
  const fd = new FormData();
  files.forEach(f => fd.append("file", f));
  try {
    const r = await fetch(url, {method: "POST", body: fd});
    const j = await r.json().catch(() => ({}));
    if (!r.ok && !(j.errors && j.errors.length)){
      return {added: [], errors: [{error: j.error || `Upload failed (${r.status})`}]};
    }
    return {added: j.added || [], errors: j.errors || []};
  } catch {
    return {added: [], errors: [{error: "Upload failed — check the connection"}]};
  }
}

/* A file dropped on the left goes to the pedal. The device decides the slot. */
async function send(files){
  if (!files || !files.length) return;
  const list = [...files];
  setText($("#msg"), "Uploading…");
  const res = await postFiles("/api/upload", list);
  reportUpload(res, true);
  loadLibrary();        // a slot upload creates a library row too
}

function reportUpload(res, toPedal){
  if (res.errors.length){
    fail(res.errors.map(e => e.name ? `${e.name}: ${e.error}` : e.error).join("; "));
    return;
  }
  const n = res.added.length;
  if (!n) return;
  if (!toPedal) say(`${n} track${n === 1 ? "" : "s"} added to the library`);
  else if (n === 1 && res.added[0].slot){
    say(`“${res.added[0].display_name}” → slot ${pad2(res.added[0].slot)}`);
  } else say(`${n} tracks queued for the pedal`);
}

/* ------------------------------------------------------------- listeners */

const drop = $("#drop");

// #drop is the input's <label>, so the browser opens the picker itself.
$("#file").onchange = e => { send(e.target.files); e.target.value=""; };
// The zone only paints itself; the pane handles the drop, which bubbles to it.
["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add("over"); }));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => {
  drop.classList.remove("over"); }));
const paneL = document.querySelector(".pane-l");
["dragenter", "dragover"].forEach(ev => paneL.addEventListener(ev, e => {
  e.preventDefault();
}));
paneL.addEventListener("drop", e => {
  if (!e.dataTransfer.files.length) return;
  e.preventDefault();
  send(e.dataTransfer.files);
});
// Anywhere else, a dropped file would navigate the page away from the app.
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => e.preventDefault());

$("#print").onclick = printList;

/* Clear a slot and offer to put the track back for twelve seconds. The track
   stays in the library, so undo is an assign back to the slot it came from. */
async function removeSlot(slot, name){
  const row = ((state && state.slots) || []).find(x => x.slot === slot);
  const resp = await api(`/api/slots/${slot}`, {method:"DELETE"});
  if (!resp.ok){ failFrom(resp, "Could not clear the slot"); return; }
  const host = $("#undoslot");
  host.innerHTML = "";
  if (!row) return;
  const label = name || "slot " + pad2(slot);
  const b = document.createElement("button");
  b.className = "undo";
  b.textContent = `Undo “${label.length > 16 ? label.slice(0,15)+"…" : label}”`;
  b.onclick = async () => {
    const r = await api(`/api/slots/${slot}/assign`,
                        jsonBody({hash: row.source_hash}));
    if (!r.ok) failFrom(r, "Undo failed — the track has gone");
    host.innerHTML = "";
  };
  host.appendChild(b);
  setTimeout(() => { if (host.firstChild === b) host.innerHTML = ""; }, 12000);
}

async function removeLoop(slot){
  // A loop is a live take with no source, so this is the one delete with a
  // confirmation and no undo.
  const pad = pad2(slot);
  if (!confirm(`Delete the loop in slot ${pad} from the pedal? You can't undo this.`)) return;
  const r = await api(`/api/loops/${slot}`, {method:"DELETE"});
  if (!r.ok) failFrom(r, "Could not remove the loop");
}

/* ------------------------------------------------- the over-the-air update */

function endUpdating(){
  ota.updating = false; clearTimeout(ota.timer); ota.timer = null;
}

// One header button: "Update available" runs the update, "Check for update"
// asks the device to look.
$("#update").onclick = () => {
  if ($("#update").disabled) return;
  if (state && state.update_available) doUpdate();
  else doCheck();
};

async function doCheck(){
  const btn = $("#update");
  ota.checking = true;
  btn.disabled = true; btn.textContent = "Checking…"; btn.className = "link";
  const r = await api("/api/update/check", {method:"POST"});
  ota.checking = false;
  if (!r.status){
    updateBtnState(state);
    fail("Update check failed — check the connection");
    return;
  }
  const j = r.body;
  if (!j.ok){
    updateBtnState(state);
    fail(j.error ? "Update check failed: " + j.error : "Update check failed");
    return;
  }
  // From the response, so an unchanged result still gives feedback.
  if (state){
    state.update_available = j.update_available;
    state.remote_revision = j.remote_revision;
  }
  updateBtnState(state);
  if (!j.update_available){
    setText($("#msg"), "Up to date"); $("#msg").className = "msg";
  }
}

async function doUpdate(){
  const btn = $("#update");
  // Completion is detected by the revision changing, so one has to be known.
  if (!state || !state.revision){
    fail("Device state isn't ready yet — try again in a moment");
    return;
  }
  if (!confirm("Update to the latest version and restart? The device will briefly disconnect.")) return;
  ota.updating = true;
  ota.fromRev = state.revision;
  btn.disabled = true; btn.textContent = "Updating…"; btn.className = "link";
  clearTimeout(ota.timer);
  ota.timer = setTimeout(() => {
    if (!ota.updating) return;
    endUpdating();
    if (state) render(state);
    fail("Update didn't confirm — reload the page to check");
  }, 180000);
  const r = await api("/api/update", {method:"POST"});
  if (!r.ok){
    failFrom(r, "Update failed");
    endUpdating(); updateBtnState(state);
    return;
  }
  // The button stays locked until a snapshot shows the new revision.
  setText($("#msg"), "Updating… the page will reconnect"); $("#msg").className = "msg";
}

/* -------------------------------------------------------- the event stream */

const es = new EventSource("/api/events");
let reconnectTimer = null;
es.onopen = () => {
  if (reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
  // Every (re)connect, including the five-minute rotation, so a second tab is
  // never more than one rotation behind another tab's rename or delete.
  loadLibrary();
};
es.onmessage = e => render(JSON.parse(e.data));
es.onerror = () => {
  // The server retires each stream after five minutes and EventSource
  // reconnects on its own, so only a real outage is surfaced: CLOSED, or
  // CONNECTING that has not recovered within the grace.
  if (es.readyState === EventSource.CLOSED){
    setText($("#msg"), "Disconnected"); $("#msg").className = "msg err";
  } else if (es.readyState === EventSource.CONNECTING && reconnectTimer === null){
    reconnectTimer = setTimeout(() => {
      if (es.readyState !== EventSource.OPEN){
        setText($("#msg"), "Reconnecting…"); $("#msg").className = "msg err";
      }
    }, 8000);
  }
};

/* ------------------------------------------------------------------ library */

// One <audio> for the page, retargeted per row; setting src is what stops
// whatever was playing.
const player = new Audio();
player.preload = "none";
player.addEventListener("ended", () => { nowPlaying = null; renderLibrary(); });
player.addEventListener("error", () => {
  // .ogg, .opus, .wma and older .flac don't play everywhere. The pedal takes
  // them fine.
  if (nowPlaying){
    fail("This browser can't play that format — it will still convert fine");
    nowPlaying = null;
    renderLibrary();
  }
});

// Fetches can finish out of order; only the newest outstanding one lands.
let libSeq = 0;

async function loadLibrary(){
  const mine = ++libSeq;
  try {
    const r = await fetch("/api/library");
    if (!r.ok) return;
    const rows = await r.json();
    if (mine !== libSeq) return;
    const changed = JSON.stringify(rows) !== JSON.stringify(library);
    library = rows;
    if (changed) libRev++;
    // A tick on a track deleted in another tab would 404 the whole batch.
    [...checked].forEach(h => { if (!rows.some(r => r.source_hash === h)) checked.delete(h); });
  } catch {
    return;
  }
  renderLibrary();
}

/* Every track, filtered by the search box and sorted by the select. "Newest
   first" is the order the server returned. */
function libraryView(){
  const q = ($("#libq").value || "").trim().toLowerCase();
  const sort = $("#libsort").value;
  const rows = (library || []).filter(r => !q || r.name.toLowerCase().includes(q));
  if (sort === "name"){
    rows.sort((a,b) => a.name.localeCompare(b.name, undefined, {sensitivity:"base"}));
  } else if (sort === "duration"){
    rows.sort((a,b) => b.duration - a.duration);
  }
  return rows;
}

function renderLibrary(){
  rebuild($("#librows"), _renderLibrary);
}

function _renderLibrary(){
  if (editingHash !== null) return;     // an inline rename owns its row

  const host = $("#librows");
  if (!host) return;
  if (library === null){ host.innerHTML = ""; return; }

  const all = library.length;
  const hideTools = all < 2;
  // The one place a render writes the search box: it is about to be hidden,
  // so no keystroke is in flight, and a stale query would filter the last
  // track out with no control to clear it.
  if (hideTools) $("#libq").value = "";
  $("#libtools").hidden = hideTools;
  updateAddSel();

  const bySlot = {};
  ((state && state.slots) || []).forEach(s => {
    (bySlot[s.source_hash] = bySlot[s.source_hash] || []).push(s.slot);
  });

  const libKey = JSON.stringify([
    libRev, nowPlaying,
    ((state && state.slots) || []).map(s => [s.slot, s.source_hash]),
    $("#libq").value, $("#libsort").value,
  ]);
  if (libKey === lastLibKey) return;
  lastLibKey = libKey;

  const rows = libraryView();
  host.innerHTML = "";
  if (!all){
    host.innerHTML = '<div class="empty">Nothing in the library yet. '
      + 'Anything you upload stays here until you delete it.</div>';
    $("#libfoot").textContent = "";
    return;
  }
  if (!rows.length){
    host.innerHTML = '<div class="empty">Nothing matches that search.</div>';
  }
  rows.forEach(r => host.appendChild(libraryRow(r, bySlot[r.source_hash] || [])));

  const q = ($("#libq").value || "").trim();
  const mins = library.reduce((t, r) => t + (r.duration || 0), 0);
  $("#libfoot").innerHTML =
    `<span>${all} track${all === 1 ? "" : "s"} · ${mmss(mins)}</span>`
    + (q ? `<span>${rows.length} match${rows.length === 1 ? "" : "es"}</span>` : "");
}

/* The toolbar button for the ticked rows, hidden when there are none. */
function updateAddSel(){
  const b = $("#addsel");
  const n = checked.size;
  b.hidden = !n;
  b.textContent = `Add ${n} to pedal`;
}

function libraryRow(r, slots){
  const el = document.createElement("div");
  el.className = "librow";

  const tick = document.createElement("input");
  tick.type = "checkbox";
  tick.className = "libcheck";
  tick.checked = checked.has(r.source_hash);
  tick.setAttribute("aria-label", "Select " + r.name);
  tick.dataset.fk = "lib:" + r.source_hash + ":tick";
  tick.onchange = () => {
    if (tick.checked) checked.add(r.source_hash); else checked.delete(r.source_hash);
    updateAddSel();
  };
  el.appendChild(tick);

  const nm = document.createElement("span");
  nm.className = "libname";
  nm.textContent = r.name;
  nm.title = "Click to rename";
  nm.tabIndex = 0;
  nm.setAttribute("role", "button");
  nm.dataset.fk = "lib:" + r.source_hash + ":name";
  const edit = () => startRename(el, nm, r);
  nm.onclick = edit;
  nm.onkeydown = e => { if (e.key === "Enter" || e.key === " "){ e.preventDefault(); edit(); } };
  el.appendChild(nm);

  const dur = document.createElement("span");
  dur.className = "libdur";
  dur.textContent = mmss(r.duration);
  el.appendChild(dur);

  if (slots.length){
    // A track can be in more than one slot; show the lowest, count the rest.
    const badge = document.createElement("span");
    badge.className = "num";
    const lowest = Math.min(...slots);
    badge.textContent = pad2(lowest) + (slots.length > 1 ? ` +${slots.length - 1}` : "");
    badge.title = `On the pedal in slot ${slots.map(pad2).join(", ")}`;
    el.appendChild(badge);
  } else {
    const add = document.createElement("button");
    add.className = "libbtn";
    add.type = "button";
    add.textContent = "Add to pedal";
    add.title = `Put “${r.name}” in the next free slot`;
    add.dataset.fk = "lib:" + r.source_hash + ":add";
    add.onclick = () => addToPedal([r]);
    el.appendChild(add);
  }

  const play = document.createElement("button");
  play.className = "libbtn" + (nowPlaying === r.source_hash ? " playing" : "");
  play.type = "button";
  play.textContent = nowPlaying === r.source_hash ? "■" : "▶";
  play.title = nowPlaying === r.source_hash ? "Stop" : "Listen";
  play.setAttribute("aria-label",
    (nowPlaying === r.source_hash ? "Stop " : "Listen to ") + r.name);
  play.dataset.fk = "lib:" + r.source_hash + ":play";
  play.onclick = () => audition(r);
  el.appendChild(play);

  const del = document.createElement("button");
  del.className = "libbtn danger";
  del.type = "button";
  del.textContent = "×";
  del.title = `Delete “${r.name}” from the device`;
  del.setAttribute("aria-label", "Delete " + r.name);
  del.dataset.fk = "lib:" + r.source_hash + ":del";
  del.onclick = () => forget(r);
  el.appendChild(del);

  return el;
}

/* Swap the name for an input; Enter or blur commits, Escape reverts. The
   input keeps the span's focus key, so the rebuild afterwards finds it. */
function inlineRename(row, nm, opts){
  const input = document.createElement("input");
  input.className = "libedit";
  input.type = "text";
  input.value = opts.name;
  input.maxLength = 200;
  input.setAttribute("aria-label", "Rename " + opts.name);
  input.dataset.fk = nm.dataset.fk;
  row.replaceChild(input, nm);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const name = input.value.trim();
    opts.release();
    libraryDomDirty();
    if (!save || !name || name === opts.name){ renderLibrary(); return; }
    await opts.commit(name);
  };

  input.onblur = () => finish(true);
  input.onkeydown = e => {
    e.stopPropagation();
    if (e.key === "Enter"){ e.preventDefault(); finish(true); }
    else if (e.key === "Escape"){ e.preventDefault(); finish(false); }
  };
}

function startRename(row, nm, r){
  if (editingHash !== null) return;
  editingHash = r.source_hash;
  inlineRename(row, nm, {
    name: r.name,
    release: () => { editingHash = null; },
    commit: async (name) => {
      // Optimistic; the reload afterwards shows the committed name either way.
      r.name = name;
      libRev++;
      renderLibrary();
      const resp = await api(`/api/library/${r.source_hash}`,
                             {...jsonBody({name}), method: "PATCH"});
      if (!resp.ok) failFrom(resp, "Rename failed");
      loadLibrary();
    },
  });
}

function audition(r){
  if (nowPlaying === r.source_hash){
    player.pause();
    nowPlaying = null;
    renderLibrary();
    return;
  }
  player.src = `/api/library/${r.source_hash}/audio`;
  nowPlaying = r.source_hash;
  renderLibrary();
  player.play().catch(() => {
    if (nowPlaying === r.source_hash){
      fail("This browser can't play that format — it will still convert fine");
      nowPlaying = null;
      renderLibrary();
    }
  });
}

/* Put tracks on the pedal from the next free slot, in the order given. */
async function addToPedal(rows){
  if (!rows.length) return;
  const r = await api("/api/slots/assign",
                      jsonBody({hashes: rows.map(x => x.source_hash)}));
  if (!r.ok){ failFrom(r, "Could not put that on the pedal"); return; }
  const p = r.body;
  $("#undoslot").innerHTML = "";      // its slot may have just been refilled
  rows.forEach(x => checked.delete(x.source_hash));
  updateAddSel();
  // The snapshot usually lands before this response, with the ticks still on.
  libraryDomDirty();
  renderLibrary();
  if (!p.assigned.length){
    warn("No room on the pedal");
    return;
  }
  let line = p.assigned.length === 1
    ? `“${p.assigned[0].name}” → slot ${pad2(p.start)}`
    : `${p.assigned.length} tracks → slots ${pad2(p.start)}–${pad2(p.end)}`;
  if (p.unplaced.length) line += ` · ${p.unplaced.length} didn't fit`;
  if (!p.loops_known) line += " · plug the pedal in to skip its loops";
  if (p.unplaced.length) warn(line); else say(line);
  loadLibrary();
}

async function forget(r, force){
  const label = r.name;
  if (!force && !confirm(
      `Delete “${label}” from the device? This removes the audio, not just the `
      + `pedal slot, and you can't undo it.`)) return;
  const resp = await api(`/api/library/${r.source_hash}` + (force ? "?force" : ""),
                         {method:"DELETE"});
  if (resp.status === 409){
    // It's on the pedal. Say which slots, rather than refusing opaquely.
    const slots = resp.body.slots || [];
    const where = slots.map(n => pad2(n)).join(", ");
    if (confirm(`“${label}” is on the pedal in slot ${where}. Clear `
                + `${slots.length > 1 ? "those slots" : "that slot"} and delete it?`)){
      return forget(r, true);
    }
    return;
  }
  if (!resp.ok){
    failFrom(resp, "Delete failed");
    loadLibrary();      // a 404 means the row had already gone
    return;
  }
  if (nowPlaying === r.source_hash){ player.pause(); nowPlaying = null; }
  loadLibrary();
}

async function sendToLibrary(files){
  if (!files || !files.length) return;
  const list = [...files];
  setText($("#msg"), "Adding to the library…");
  const res = await postFiles("/api/library", list);
  reportUpload(res, false);
  loadLibrary();
}

$("#libfile").onchange = e => { sendToLibrary(e.target.files); e.target.value=""; };
// Uncontrolled inputs: read on demand, never written by a render.
$("#libq").oninput = renderLibrary;
$("#libsort").onchange = renderLibrary;
// In view order, so a sorted library goes onto the pedal in the order shown.
$("#addsel").onclick = () =>
  addToPedal(libraryView().filter(r => checked.has(r.source_hash)));

// es.onopen also loads this, but only once the stream handshake completes.
loadLibrary();
