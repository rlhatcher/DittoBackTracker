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
     library               fetching, the view, the rows, the ticked set
     boot                  the load the stream would otherwise wait for

   One file on purpose. Splitting it into ES modules would cost a round trip
   each on a page served no-cache from a Pi Zero, serialise on the import graph
   the preload scanner cannot see, and add an ASSETS allowlist entry per module
   where forgetting one is a silent 404. Revisit if this passes ~1500 lines. */

const $ = s => document.querySelector(s);

/* ---------------------------------------------------------- module state */

/* One rule runs under most of what follows, so it is stated once here rather
   than re-derived at each declaration: render() runs on every SSE frame, up to
   5 Hz while a conversion reports progress, and it rebuilds both lists from
   scratch. Anything the user is part-way through — a rename, a half-typed slot
   number, which rows are ticked — would be destroyed by that unless it lives
   out here and is read back on the way in. */

let state = null;
/* The over-the-air update, as one small state machine.

   Idle is every field at its value below, and endUpdating() is the reset.
   Grouped because the lifecycle is split across two functions far apart:
   doUpdate() sets `fromRev`, and render() is what clears `ota.updating`, by
   noticing that a snapshot now reports a different revision. Four loose flags
   made that look like four unrelated booleans.

   Only this one is an object. The other state here stays as plain bindings —
   a mistyped `let` is a ReferenceError, a mistyped property is undefined
   flowing quietly into a falsy check, and nothing in this project can catch
   the second. If a linter with no-undef is ever adopted, that reverses. */
const ota = {
  updating: false,   // an update is in flight, or its restart is pending
  fromRev: null,     // the revision we are leaving; completion is != this
  timer: null,       // the 180s "didn't confirm" safety net
  checking: false,   // a check is in flight (a check is not an update)
};

/* Two sources of truth, deliberately kept apart.

   `state` is the SSE snapshot: server-authoritative, arrives on its own, and
   render() rebuilds the world from it. `library` is pulled from /api/library
   and only changes when we ask. The snapshot doesn't carry the library — it is
   emitted several times a second during a conversion and must stay small.

   They meet in one place: which slots hold a track is read from `state.slots`,
   not from the library response, so those badges stay live for free. */
let library = null;
let editingHash = null;    // a track rename in progress; freezes renderLibrary
let nowPlaying = null;     // hash being auditioned, for the row's play button

/* Both lists rebuild from scratch, and render() runs on every SSE frame — up to
   5 Hz while a conversion reports progress. Rebuilding then is not just wasted
   work on a phone: it destroys focus, so a keyboard user is thrown back to the
   document five times a second and the list becomes unusable while anything is
   converting.

   So each list computes a key from exactly what it draws and skips the rebuild
   when that key is unchanged. Progress is not in either key, which is what
   makes a conversion quiet.

   libRev stands in for the library's contents: hashing 100 rows every frame
   would just move the cost. It is bumped wherever `library` is replaced or a
   row is edited in place. */
let libRev = 0;
let lastListKey = null, lastLibKey = null;

/* Say that the library's DOM no longer matches its key, so the next render
   redraws even though the data has not moved.

   The dirty check compares what the rows are *derived from*. That is wrong
   whenever something has changed the DOM directly — an inline rename replaces a
   row's name span with an input, and cancelling it, or committing it unchanged,
   leaves the key exactly where it was. Without this the input stays on screen
   for good. */
function libraryDomDirty(){ lastLibKey = null; }

/* Uncommitted text in a pedal row's slot field, keyed by slot number.

   The rows are rebuilt whenever the snapshot moves, which during a conversion
   is five times a second, so a field's value cannot live only in the DOM. It is
   read back when the row is built, which is what makes a rebuild reconstruct
   what was being typed instead of wiping it. */
let slotDraft = {};

/* How many times each field has been edited, so a late failure can tell whether
   the field it wants to roll back is still the one it was sent for. A commit
   deletes the draft and then waits on the network; the user can be typing again
   before the answer comes. Without this, a slow refusal wipes what they have
   since typed. */
let slotEdit = {};

/* Which library rows are ticked, by source_hash. Read back when a row is
   built, so a rebuild keeps the ticks. */
const checked = new Set();

/* ---------------------------------------------------------- on the pedal */

const pad2 = n => String(n).padStart(2, "0");
// One vocabulary for slot state. The wire words are not the shown words:
// "staged" is the wire word for a slot whose audio is converted and waiting to
// be written, and the row calls that queued.
const STATE_LABEL = {converting: "converting", staged: "queued",
                     synced: "on pedal", error: "error"};
const stateLabel = st => STATE_LABEL[st] || st;

function mmss(s){ s=Math.max(0,Math.round(s)); return Math.floor(s/60)+":"+pad2(s%60); }

/* The row's slot number, as a field you can type another one into. Enter or
   blur moves the track there, swapping if the slot is taken; Escape reverts.
   The field is the one way to move a track: one number, typed, is what a
   musician with a set list already has in front of them. */
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
  f.oninput = () => {
    slotDraft[r.slot] = f.value;
    slotEdit[r.slot] = (slotEdit[r.slot] || 0) + 1;
  };
  f.onkeydown = e => {
    e.stopPropagation();
    if (e.key === "Enter"){ e.preventDefault(); f.blur(); }
    else if (e.key === "Escape"){ revertSlotField(r.slot); }
  };
  f.onblur = () => commitSlotField(r);
  return f;
}

function revertSlotField(slot){
  delete slotDraft[slot];
  lastListKey = null;     // the data has not moved, only the DOM
  if (state) render(state);
}

/* Enter or blur commits what was typed.

   Clearing the draft is not enough to put a rejected number back. The field's
   value is in the DOM, and only a rebuild reconstructs it — which happens when
   the list key moves, which happens when the snapshot changes. A refused move
   changes nothing, so without an explicit revert the field would sit there
   showing a number the pedal never accepted. */
async function commitSlotField(r){
  if (slotDraft[r.slot] === undefined) return;    // nothing was typed
  const raw = slotDraft[r.slot].trim();
  delete slotDraft[r.slot];
  // Whose edit this is. Anything awaited below must check it before rolling the
  // field back, or it will roll back somebody else's typing.
  const gen = slotEdit[r.slot];
  const stillMine = () => slotEdit[r.slot] === gen;
  const max = (state && state.slot_count) || 99;

  const n = /^\d{1,2}$/.test(raw) ? parseInt(raw, 10) : NaN;
  if (!Number.isFinite(n) || n < 1 || n > max){
    // Not a literal "01–99": docs/api.md says clients read slot_count rather
    // than assuming the pedal has 99 slots.
    warn(`Slot numbers run 01–${pad2(max)}`);
    revertSlotField(r.slot);
    return;
  }
  if (n === r.slot){ revertSlotField(r.slot); return; }
  // One call, not assign-then-delete: two calls can fail between them and
  // leave the track in both slots. move already does move-or-swap.
  if (!await moveTo(r.slot, n) && stillMine()) revertSlotField(r.slot);
}

/* Download and delete for a pedal-recorded loop. One definition, used both on a
   loop-only row and on the continuation line under a row that has both. */
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

/* The tracks list: what is on the pedal right now, plus any loop-only slots.
   Split out of render() so a dirty check can skip it wholesale. */
function drawTrackList(list, s, byslot, loops){
  list.innerHTML = "";
  // Union of backing-track slots and loop-bearing slots, in slot order: a slot
  // that holds only a pedal-recorded loop (no backing track) still gets a row,
  // so its download/remove controls are reachable.
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
      const label = stateLabel(r.state);
      el.innerHTML = `
        <span class="nm">${escapeHtml(r.display_name)}</span>
        <span class="dur">${mmss(r.duration)}</span>
        <span class="st ${r.state}">${label}</span>`;
      el.prepend(slotField(r));
      const x = document.createElement("button");
      x.className = "x"; x.textContent = "×"; x.title = "Clear slot";
      x.setAttribute("aria-label", `Clear slot ${pad2(n)}`);
      x.dataset.fk = "slot:" + r.slot + ":clear";
      x.onclick = () => removeSlot(r.slot, r.display_name);
      el.appendChild(x);
    } else {
      // Loop-only slot: no backing track, so nothing to clear and no slot to
      // move. DELETE /api/slots/<n> deliberately leaves LOOP.WAV alone, so a ×
      // here would look broken. The loop's own controls are the row's actions.
      el.innerHTML = `
        <span class="num">${pad2(n)}</span>
        <span class="nm loop-only">Recorded loop — kept, never overwritten</span>
        <span class="st loop">loop</span>`;
    }
    list.appendChild(el);
    // A loop's controls always go on their own indented line, whether or not
    // the slot also holds a backing track: a row carrying two × that mean
    // different things is worse, and "Recorded loop — kept, never overwritten"
    // plus two buttons ellipsises the sentence away on half a laptop screen.
    // Not behind hover: it is rare, and it deletes a recording.
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
  // A class, not a colour: the stylesheet owns the palette, and naming one here
  // would be the one place a token could drift without the CSS noticing.
  const map = {mounted:["ok","connected"],absent:["off","no pedal"],error:["off","error"]};
  const [cls,t] = map[s.pedal] || map.absent;
  pd.className = cls;
  pd.innerHTML = `<span class="dot"></span>${t}`;

  if (s.version){
    setText($("#ver"), "v" + s.version + (s.revision ? " · " + s.revision : ""));
  }
  // The update completes when a snapshot shows the deployed revision has changed
  // (the restart brought up the new build). Gate on that, not on the SSE
  // reconnect — reconnects also happen on the routine stream rotation, well
  // before any update, and clearing early could admit a second update.
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

  // Print applies to loaded backing tracks; hide it when there's nothing to print.
  $("#print").hidden = !s.slots.length;

  const list = $("#list");
  // Everything this list draws, and nothing else. Progress is absent on
  // purpose: it is what changes 5 times a second during a conversion, and it
  // does not appear here.
  // JSON, not delimiter joins: a display_name or an error is arbitrary user
  // text and can contain whatever separator we picked, so two different lists
  // could hash the same and a required redraw would be skipped.
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
    // The bar is decorative on its own; this is what a screen reader reads.
    $("#progwrap").setAttribute("aria-valuenow", pct);
  }
  const m = $("#msg");
  // Everything below yields to a confirmation the user has not had time to
  // read yet.
  if (Date.now() < msgHold) { /* a confirmation owns the line */ }
  else if (s.busy)         { setText(m, s.busy); m.className="msg"; }
  else if (s.error)        { setText(m, s.error); m.className="msg err"; }
  else if (s.pedal==="mounted") { setText(m, "Ready"); m.className="msg"; }
  else                     { setText(m, "Plug in the pedal"); m.className="msg"; }

  // The library's own rows come from /api/library, but its slot badges come
  // from the snapshot — so a new snapshot re-renders it.
  renderLibrary();
}

/* ----------------------------------------------- DOM helpers, and printing */

/* Rebuild `host` while keeping focus where the user put it.

   A dirty check keeps most rebuilds from happening at all, but the ones that do
   still happen — a track finishing its write, a rename landing — must not steal
   focus. Nodes opt in by setting data-fk to something stable across rebuilds.
*/
function rebuild(host, draw){
  const active = document.activeElement;
  const key = active && host.contains(active) ? active.dataset.fk : null;
  // Restoring focus to a text field but not the caret puts it at the end, so a
  // rebuild landing mid-word moves the cursor out from under the typist. Five
  // times a second, during a conversion, in a two-character field.
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

/* Write only when the text actually differs.

   #msg is an aria-live region, and replacing its text node is what makes a
   screen reader announce. During a conversion the same string is assigned 5
   times a second, so without this the reader repeats "Converting…" over and
   over. The others are plain text, but the same reasoning makes them free.
*/
function setText(el, text){
  if (el.textContent !== text) el.textContent = text;
}

function escapeHtml(s){ const d=document.createElement("div"); d.textContent=s; return d.innerHTML; }

// Build a concise, self-contained printable document: a bare list of slot number
// + track name for the loaded backing tracks, in slot order. No headings and no
// row rules — both span the full page width, so scaling the print down leaves
// long lines against short text. Loops and empty slots are left out. Kept
// separate from printList so the output is easy to inspect; scaling and margins
// are left to the browser's print dialog.
function printHtml(s){
  const rows = (s.slots || []).slice().sort((a,b) => a.slot - b.slot);
  const body = rows.length
    ? rows.map(r =>
        `<tr><td class="n">${pad2(r.slot)}</td>` +
        `<td>${escapeHtml(r.display_name)}</td></tr>`).join("")
    : `<tr><td></td><td>No backing tracks loaded.</td></tr>`;
  return `<!doctype html><html><head><meta charset="utf-8">` +
    `<title>DittoBackTracker — backing tracks</title><style>` +
    // Zero the page margin so the browser has nowhere to draw its own header and
    // footer (date, document title, URL, page number) — those are browser chrome,
    // not content, so they never scale with the print. The body margin below
    // supplies the actual inset instead.
    `@page{margin:0}` +
    // Pin white paper / black ink so a dark-mode browser doesn't render the
    // print preview (and save-to-PDF) as black text on a dark background.
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
  // Opened synchronously from the click so it isn't treated as a pop-up.
  const w = window.open("", "_blank");
  if (!w){ fail("Couldn't open the print view — allow pop-ups for this page"); return; }
  w.document.write(printHtml(state));
  w.document.close();
  w.focus();
  w.print();   // no external resources in the doc, so it's ready immediately
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
    // Up to date, or not checked yet — offer a manual re-check.
    b.textContent = "Check for update";
    b.className = "link";
    b.title = "Check GitHub for a newer version";
  }
}

/* --------------------------------------------------- talking to the device */

/* Send it, parse whatever came back, say whether it worked. `status` is 0 when
   the request never reached the device: the device's own error is worth
   showing, a network failure is not. */
async function api(url, opts){
  try {
    const r = await fetch(url, opts);
    return {ok: r.ok, status: r.status,
            body: await r.json().catch(() => ({}))};
  } catch {
    return {ok: false, status: 0, body: {}};
  }
}

/* Report a failed api() call. `what` is the fallback for when the device did
   not answer, or answered without an error of its own. */
function failFrom(r, what){
  fail(r.status ? (r.body.error || what) : what + " — check the connection");
}

const jsonBody = body => ({method: "POST",
                           headers: {"Content-Type": "application/json"},
                           body: JSON.stringify(body)});

/* --------------------------------------------------------- the status line */

/* #msg says what just happened, and is the aria-live region.

   The hold is what makes these messages survive being said. render() rewrites
   #msg from the snapshot, and an assign changes the snapshot, so without it a
   confirmation is overwritten by "Ready" in the same tick it was written — the
   user is told nothing and a screen reader announces nothing. For six seconds
   after one of these, the snapshot does not get the line back. */
let msgHold = 0, msgTimer = null;
function holdMsg(){
  msgHold = Date.now() + 6000;
  clearTimeout(msgTimer);
  // Hand the line back afterwards, rather than leaving the last confirmation up
  // until whenever the next frame happens to arrive.
  msgTimer = setTimeout(() => { if (state) render(state); }, 6000);
}
function say(text){ setText($("#msg"), text); $("#msg").className = "msg"; holdMsg(); }
function warn(text){ setText($("#msg"), text); $("#msg").className = "msg warn"; holdMsg(); }
function fail(text){ setText($("#msg"), text); $("#msg").className = "msg err"; holdMsg(); }

/* ---------------------------------------------------------------- uploads */

// Returns whether it landed, so the slot field knows whether the number the
// user typed is now true.
async function moveTo(src, dst){
  const r = await api(`/api/slots/${src}/move`, jsonBody({to: dst}));
  if (!r.ok) failFrom(r, "Move failed");
  return r.ok;
}

/* One multipart POST, with the two answer shapes flattened into one.

   A 413 or a 500 carries no `errors` list, and a dropped connection carries no
   body at all, so both become an error entry here — otherwise a whole batch can
   fail and leave the status line on "Uploading…". */
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

/* A file dropped on the left goes to the pedal. The device decides where: a
   leading number in the name picks the slot, anything else takes the lowest
   free one, and slots holding a loop are skipped. That rule lives in one place,
   /api/upload, so this page does not keep a copy of it. */
async function send(files){
  if (!files || !files.length) return;
  const list = [...files];
  setText($("#msg"), "Uploading…");
  const res = await postFiles("/api/upload", list);
  reportUpload(res, true);
  // A slot upload creates a library row too. Without this the new track is
  // missing from the Library until the next reconnect — in the common case
  // the five-minute stream rotation.
  loadLibrary();
}

/* Errors win the line: a batch that half landed is the case a confirmation
   would paper over. */
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

/* No click handler here: #drop is the input's <label>, so the browser opens
   the picker. Calling .click() as well would open it twice. */
$("#file").onchange = e => { send(e.target.files); e.target.value=""; };
/* The zone only paints itself while a file is over it. The drop is handled
   once, by the pane below, which the zone's own drop bubbles up to. */
["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add("over"); }));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => {
  drop.classList.remove("over"); }));
/* A file dropped anywhere on the left half of the page lands. */
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

/* Clear a slot, and offer to put the track back for twelve seconds.

   The track stays in the library, so undo is an assign back to the slot it came
   from: no trash on the device, nothing to expire. The hash is read before the
   delete, because afterwards the slot cannot say what it held. */
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
    // A 404 means the track has since been deleted from the library.
    const r = await api(`/api/slots/${slot}/assign`,
                        jsonBody({hash: row.source_hash}));
    if (!r.ok) failFrom(r, "Undo failed — the track has gone");
    host.innerHTML = "";
  };
  host.appendChild(b);
  setTimeout(() => { if (host.firstChild === b) host.innerHTML = ""; }, 12000);
}

async function removeLoop(slot){
  // A loop is a live take with no source — deleting it is irreversible, so
  // guard the accidental click. No undo: there is nothing to restore from.
  const pad = pad2(slot);
  if (!confirm(`Delete the loop in slot ${pad} from the pedal? You can't undo this.`)) return;
  const r = await api(`/api/loops/${slot}`, {method:"DELETE"});
  if (!r.ok) failFrom(r, "Could not remove the loop");
}

/* ------------------------------------------------- the over-the-air update */

function endUpdating(){
  ota.updating = false; clearTimeout(ota.timer); ota.timer = null;
}

// The single header button has two resting states: "Update available" (runs the
// OTA update) and "Check for update" (asks the device to re-check the remote).
// The click dispatches on the current state.
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
  if (!r.status){
    ota.checking = false; updateBtnState(state);
    fail("Update check failed — check the connection");
    return;
  }
  const j = r.body;
  ota.checking = false;
  if (!j.ok){
    // The check couldn't run (offline, or not a git deployment).
    updateBtnState(state);
    fail(j.error ? "Update check failed: " + j.error : "Update check failed");
    return;
  }
  // Reflect the result. A change also arrives over SSE, but update from the
  // response so a no-change result (still up to date) still gives feedback.
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
  // A known deployed revision is required: completion is detected by the
  // revision changing (see render). Without one — the first snapshot hasn't
  // arrived, or this isn't a git deployment — a pre-restart snapshot would
  // differ from a null baseline and clear it before the update lands.
  if (!state || !state.revision){
    fail("Device state isn't ready yet — try again in a moment");
    return;
  }
  if (!confirm("Update to the latest version and restart? The device will briefly disconnect.")) return;
  ota.updating = true;
  // Remember the revision we're leaving; render() clears ota.updating once a
  // snapshot reports a different one (the new build is up).
  ota.fromRev = state.revision;
  btn.disabled = true; btn.textContent = "Updating…"; btn.className = "link";
  // Safety net: if no new revision ever arrives (restart failed to come back),
  // don't leave the button stuck forever.
  clearTimeout(ota.timer);
  ota.timer = setTimeout(() => {
    if (!ota.updating) return;
    endUpdating();
    if (state) render(state);
    fail("Update didn't confirm — reload the page to check");
  }, 180000);
  let j;
  try {
    const r = await fetch("/api/update", {method:"POST"});
    j = await r.json().catch(() => ({}));
    if (!r.ok){
      // 409 = busy, 502 = update failed. Restore the button so they can retry.
      fail(j.error || "Update failed");
      endUpdating(); updateBtnState(state);
      return;
    }
  } catch {
    fail("Update failed — check the connection");
    endUpdating(); updateBtnState(state);
    return;
  }
  // Success: the service is restarting. ota.updating stays set until a snapshot
  // shows the new revision (see render), keeping the button locked so a second
  // update can't start while the restart is pending.
  setText($("#msg"), "Updating… the page will reconnect"); $("#msg").className = "msg";
}

/* -------------------------------------------------------- the event stream */

const es = new EventSource("/api/events");
let reconnectTimer = null;
es.onopen = () => {
  if (reconnectTimer){ clearTimeout(reconnectTimer); reconnectTimer = null; }
  // Refetch the library on every (re)connect. That covers the first connect,
  // the server's five-minute stream rotation, and the reconnect after an
  // over-the-air restart — so a second tab is never more than one rotation
  // behind another tab's rename or delete, without the snapshot having to
  // carry a change counter.
  loadLibrary();
};
es.onmessage = e => render(JSON.parse(e.data));
es.onerror = () => {
  // The server retires each stream after five minutes and EventSource
  // reconnects on its own (readyState CONNECTING), which completes in well
  // under a second — so a planned rotation must stay silent. Only surface a
  // real outage: CLOSED immediately, or CONNECTING that hasn't recovered
  // within the grace window (onopen clears the timer, the next snapshot
  // overwrites the message).
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

/* ------------------------------------------------------------------ library

   The pedal holds about twelve five-minute tracks; the card holds as many as
   you like. So "what I own" and "what the pedal is carrying today" are two
   lists, and this is the first one. Putting a track on the pedal from here
   costs a transcode at most — usually not even that, since the staged WAV may
   still be cached — rather than another upload over WiFi. */

// One <audio> for the whole page, retargeted per row. Ninety-nine elements with
// src set would have the browser fetching metadata for the entire library, and
// reassigning src is also what guarantees two tracks can't play at once.
const player = new Audio();
player.preload = "none";
player.addEventListener("ended", () => { nowPlaying = null; renderLibrary(); });
player.addEventListener("error", () => {
  // Four of the ten accepted formats (.ogg, .opus, .wma, older .flac) don't
  // play in every browser — Safari in particular. The file is fine and the
  // pedal will take it; this browser just can't preview it.
  if (nowPlaying){
    fail("This browser can't play that format — it will still convert fine");
    nowPlaying = null;
    renderLibrary();
  }
});

// Fetches can finish out of order — a delete and the refresh behind it, say —
// and the loser would otherwise overwrite newer data with an older list, putting
// a deleted or pre-rename row back on screen. Stamp each request and ignore any
// response that is not the newest one still outstanding.
let libSeq = 0;

async function loadLibrary(){
  const mine = ++libSeq;
  try {
    const r = await fetch("/api/library");
    if (!r.ok) return;
    const rows = await r.json();
    if (mine !== libSeq) return;      // a newer request has already answered
    // es.onopen refetches on every reconnect, including the routine five-minute
    // stream rotation, and the answer is usually identical. Only move the
    // revision when something actually changed, or each rotation costs a full
    // redraw for nothing.
    const changed = JSON.stringify(rows) !== JSON.stringify(library);
    library = rows;
    if (changed) libRev++;
    // A tick on a track that has since been deleted (in another tab, say)
    // would otherwise count towards "Add 3 to pedal" and then 404 the batch.
    [...checked].forEach(h => { if (!rows.some(r => r.source_hash === h)) checked.delete(h); });
  } catch {
    return;             // a snapshot or a later refetch will put it right
  }
  renderLibrary();
}

/* What the library pane draws: every track, filtered by the search box and
   sorted by the select. "Newest first" is the order the server already
   returned. */
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
  // An inline rename owns the row it's in. Freezing at most a screenful of
  // static rows for the few seconds an edit takes is free, and far more robust
  // than trying to preserve the editing node across a rebuild.
  if (editingHash !== null) return;

  const host = $("#librows");
  if (!host) return;
  if (library === null){ host.innerHTML = ""; return; }

  const all = library.length;
  // Nothing to search or sort through yet.
  const hideTools = all < 2;
  // The one place this function writes the search box. The standing rule is
  // that it never does — that is what stops a snapshot arriving mid-keystroke
  // from wiping what is being typed — but the field is about to be hidden, so
  // there is no keystroke in flight to lose. Leaving a stale query applied
  // would filter the last remaining track out of the list with no visible
  // control to clear it.
  if (hideTools) $("#libq").value = "";
  $("#libtools").hidden = hideTools;
  updateAddSel();

  // Which slots hold each track, from the snapshot — so these badges follow an
  // upload or a clear without refetching the library.
  const bySlot = {};
  ((state && state.slots) || []).forEach(s => {
    (bySlot[s.source_hash] = bySlot[s.source_hash] || []).push(s.slot);
  });

  // Everything the rows depend on. libRev stands in for the library's contents
  // so this stays cheap with a large library; the rest is what the snapshot
  // contributes (which slots hold what) plus the two uncontrolled inputs.
  // Progress is deliberately absent. The ticked set is not here either: a tick
  // changes one checkbox the user just clicked, and the toolbar button reads
  // the set directly.
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

/* The toolbar button that acts on the ticked rows. Hidden when none are: a
   button with nothing to act on is worse than no button. */
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
    // Where it is on the pedal. A track can be in more than one slot — the
    // API is happy to put one track in two places — so the lowest is shown
    // and the rest are counted.
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

/* Swap a name for an input, commit on Enter or blur, revert on Escape. The
   focus-key discipline in here is the part that is easy to get subtly wrong. */
function inlineRename(row, nm, opts){
  const input = document.createElement("input");
  input.className = "libedit";
  input.type = "text";
  input.value = opts.name;
  input.maxLength = 200;
  input.setAttribute("aria-label", "Rename " + opts.name);
  // The same focus key as the span it replaces. Ending an edit rebuilds the
  // row, and rebuild() can only restore focus to a key it can find — without
  // this the input is focused, then removed, and focus falls to the document,
  // which is the exact failure the rebuild helper exists to prevent.
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
    // This replaced a span with an input, so the DOM is dirty on every path out
    // of here — including the two that change no data at all.
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
      // Optimistic: the row already reads the new name, and a failure re-reads
      // the server's version rather than leaving a lie on screen.
      r.name = name;
      libRev++;        // edited in place, so the key must move
      renderLibrary();
      const resp = await api(`/api/library/${r.source_hash}`,
                             {...jsonBody({name}), method: "PATCH"});
      if (!resp.ok) failFrom(resp, "Rename failed");
      // The optimistic write went to the row object we captured, but
      // renderLibrary is only frozen during the edit — loadLibrary is not, and
      // es.onopen fires on the five-minute rotation. If it replaced `library`
      // while this was in flight, that object is detached and the row would
      // render the old name until some later refresh. Re-read from the server
      // so the rendered name is the committed one either way.
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
  // Setting src is what stops whatever was playing before.
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

/* Put tracks on the pedal from the next free slot, in the order given. One
   call for the lot: the device knows which slots hold a loop and this page
   does not, and one snapshot comes back rather than one per track. */
async function addToPedal(rows){
  if (!rows.length) return;
  const r = await api("/api/slots/assign",
                      jsonBody({hashes: rows.map(x => x.source_hash)}));
  if (!r.ok){ failFrom(r, "Could not put that on the pedal"); return; }
  const p = r.body;
  // The pending undo restores one slot, and this may have just refilled it.
  $("#undoslot").innerHTML = "";
  rows.forEach(x => checked.delete(x.source_hash));
  updateAddSel();
  // The snapshot the fill caused usually lands before this response does, so
  // the rows have already been rebuilt with their ticks still on. The set is
  // not in the library key, so say the DOM is stale and redraw.
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
  // The capacity bar reads from bytes on the card and will not know about this
  // until the writes land, so the warning has to come from the plan itself.
  const secs = p.assigned.reduce((t, a) => {
    const row = (library || []).find(x => x.source_hash === a.source_hash);
    return t + ((row && row.duration) || 0);
  }, 0);
  const total = (state && state.capacity && state.capacity.total_seconds) || 0;
  if (total && secs > total){
    warn(`${line} · that is ${mmss(secs)} on a ${mmss(total)} pedal, so some won't fit`);
  } else if (p.unplaced.length){
    warn(line);
  } else {
    say(line);
  }
  loadLibrary();
}

async function forget(r, force){
  const label = r.name;
  if (!force && !confirm(
      `Delete “${label}” from the device? This removes the audio, not just the `
      + `pedal slot, and you can't undo it.`)) return;
  let resp, j;
  try {
    resp = await fetch(`/api/library/${r.source_hash}` + (force ? "?force" : ""),
                       {method:"DELETE"});
    j = await resp.json().catch(()=>({}));
  } catch {
    fail("Delete failed — check the connection");
    return;
  }
  if (resp.status === 409){
    // It's on the pedal. Say which slots, rather than refusing opaquely.
    const where = (j.slots || []).map(n => pad2(n)).join(", ");
    if (confirm(`“${label}” is on the pedal in slot ${where}. Clear `
                + `${(j.slots || []).length > 1 ? "those slots" : "that slot"} `
                + `and delete it?`)){
      return forget(r, true);
    }
    return;
  }
  if (!resp.ok){
    fail(j.error || "Delete failed");
    // A 404 here means the row had already gone — and the server may still
    // have cleared slots that pointed at it. Refresh either way, or the list
    // keeps offering a track the device no longer has and every retry repeats
    // the same 404 until the next reconnect.
    loadLibrary();
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
// Uncontrolled inputs: read on demand, never written by a render, so a snapshot
// arriving mid-keystroke can't wipe what's being typed.
$("#libq").oninput = renderLibrary;
$("#libsort").onchange = renderLibrary;
// In view order, so a sorted library goes onto the pedal in the order shown.
$("#addsel").onclick = () =>
  addToPedal(libraryView().filter(r => checked.has(r.source_hash)));

// es.onopen also loads this, but only once the stream handshake completes. Ask
// now so the library fills even if the event stream is slow or never comes up.
loadLibrary();
