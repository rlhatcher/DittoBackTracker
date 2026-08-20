const $ = s => document.querySelector(s);
const SLOT_MIME = "application/x-ditto-slot";
let state = null, selected = null, dragSrc = null, binMode = false;
let updating = false, updatingFromRev = null, updateTimer = null, checking = false;

/* Two sources of truth, deliberately kept apart.

   `state` is the SSE snapshot: server-authoritative, arrives on its own, and
   render() rebuilds the world from it. `library` is pulled from /api/library
   and only changes when we ask. The snapshot doesn't carry the library — it is
   emitted several times a second during a conversion and must stay small.

   They meet in one place: which slots hold a track is read from `state.slots`,
   not from the library response, so those badges stay live for free. */
let library = null;
let editingHash = null;    // a rename in progress; freezes renderLibrary
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

/* Which slot cell currently holds the grid's single tab stop. It has to live
   here rather than only on the element: render() rewrites every cell's
   tabIndex, so a position the user arrowed to would be reset to the selected
   slot by the next SSE frame — which arrives 5 times a second mid-conversion.
   null means "follow the selection, else the first cell". */
let rovingSlot = null;

const pad2 = n => String(n).padStart(2, "0");

function mmss(s){ s=Math.max(0,Math.round(s)); return Math.floor(s/60)+":"+pad2(s%60); }

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
      list.innerHTML = '<div style="color:var(--tx2);font-size:14px">Nothing loaded yet.</div>';
    }
    slotNums.forEach(n => {
      const r = byslot[n], pad = pad2(n);
      const el = document.createElement("div");
      el.className = "track";
      if (r){
        const label = {converting:"converting",staged:"staged",synced:"on pedal",error:"error"}[r.state]||r.state;
        el.innerHTML = `
          <span class="num">${pad}</span>
          <span class="nm">${escapeHtml(r.display_name)}</span>
          <span class="dur">${mmss(r.duration)}</span>
          <span class="st ${r.state}">${label}</span>`;
        const x = document.createElement("button");
        x.className = "x"; x.textContent = "×"; x.title = "Clear slot";
        x.dataset.fk = "slot:" + r.slot + ":clear";
        x.onclick = () => removeSlot(r.slot, r.display_name);
        el.appendChild(x);
      } else {
        // Loop-only slot: no backing track, so no name/duration/clear control.
        el.innerHTML = `
          <span class="num">${pad}</span>
          <span class="nm loop-only">Loop only</span>
          <span class="st loop">loop</span>`;
      }
      list.appendChild(el);
      if (loops.has(n)){
        const lr = document.createElement("div");
        lr.className = "looprow";
        const dl = document.createElement("a");
        dl.className = "loopbtn"; dl.href = `/api/loops/${n}`;
        dl.setAttribute("download", `loop-${pad}.wav`);
        dl.textContent = "Download loop";
        dl.title = `Download the loop from slot ${pad} (leaves it on the pedal)`;
        dl.dataset.fk = "slot:" + n + ":loopdl";
        lr.appendChild(dl);
        const rm = document.createElement("button");
        rm.className = "loopbtn danger"; rm.textContent = "Remove loop";
        rm.title = `Delete the loop in slot ${pad} from the pedal`;
        rm.dataset.fk = "slot:" + n + ":looprm";
        rm.onclick = () => removeLoop(n);
        lr.appendChild(rm);
        list.appendChild(lr);
      }
      if (r && r.state === "error" && r.error){
        const e = document.createElement("div");
        e.className = "err"; e.style.cssText = "font-size:12px;padding:0 0 8px 32px";
        e.textContent = r.error;
        list.appendChild(e);
      }
    });
}

function render(s){
  state = s;

  const pd = $("#pedal");
  const map = {mounted:["var(--ok)","connected"],absent:["var(--tx2)","no pedal"],error:["var(--err)","error"]};
  const [c,t] = map[s.pedal] || map.absent;
  pd.innerHTML = `<span class="dot" style="background:${c}"></span>${t}`;

  if (s.version){
    setText($("#ver"), "v" + s.version + (s.revision ? " · " + s.revision : ""));
  }
  // The update completes when a snapshot shows the deployed revision has changed
  // (the restart brought up the new build). Gate on that, not on the SSE
  // reconnect — reconnects also happen on the routine stream rotation, well
  // before any update, and clearing early could admit a second update.
  if (updating && s.revision && s.revision !== updatingFromRev){
    endUpdating();
  }
  updateBtnState(s);

  const cap = s.capacity;
  const frac = cap.total_seconds ? cap.used_seconds/cap.total_seconds : 0;
  setText($("#captxt"), cap.used_label + " of " + cap.total_label);
  const fill = $("#capfill");
  fill.style.width = Math.min(100, frac*100) + "%";
  fill.className = "capfill" + (frac>0.95?" err":frac>0.8?" warn":"");
  setText($("#capnote"), cap.total_seconds
    ? mmss(Math.max(0,cap.total_seconds-cap.used_seconds)) + " remaining"
    : "connect the pedal to see capacity");

  const byslot = {};
  s.slots.forEach(x => byslot[x.slot] = x);
  const loops = new Set(s.loops || []);

  const total = s.slot_count || 99;
  const g = $("#grid");
  if (g.children.length !== total){
    g.innerHTML = "";
    for (let i=1;i<=total;i++){
      const d = document.createElement("button");
      d.type = "button";
      d.className = "cell"; d.title = "Slot "+i; d.dataset.slot = i;
      d.setAttribute("aria-pressed", "false");
      // Roving tabindex: the grid is one stop, not 99. Reaching slot 50 used
      // to cost 50 tabs, and the grid sits above everything else in the order.
      d.tabIndex = -1;
      d.onclick = () => {
        // Before the branch, not inside it: the click has already moved
        // focus here, and the move path below returns early, so setting
        // this afterwards would leave the tab stop on whichever cell held
        // it before — focus and the tab stop on different slots.
        rovingSlot = i;
        // Pick-up-then-place reorder — the keyboard equivalent of the mouse
        // drag: with an occupied slot already selected, activating a different
        // slot moves or swaps into it. (state, not the once-built byslot
        // closure, so occupancy is read from the latest snapshot.)
        if (selected !== null && selected !== i &&
            state.slots.some(x => x.slot === selected)){
          const src = selected;
          selected = null;
          moveTo(src, i);
          render(state);
          return;
        }
        selected = selected===i?null:i;
        render(state);
      };
      d.addEventListener("dragstart", e => {
        dragSrc = i;
        e.dataTransfer.setData(SLOT_MIME, String(i));
        e.dataTransfer.effectAllowed = "move";
        d.classList.add("dragging");
        setBinMode(true);
      });
      d.addEventListener("dragend", () => {
        dragSrc = null;
        d.classList.remove("dragging");
        setBinMode(false);
      });
      ["dragenter","dragover"].forEach(ev => d.addEventListener(ev, e => {
        e.preventDefault(); e.stopPropagation();
        if (dragSrc !== i) d.classList.add("over"); }));
      ["dragleave","drop"].forEach(ev => d.addEventListener(ev, e => {
        e.preventDefault(); d.classList.remove("over"); }));
      d.addEventListener("drop", e => {
        e.stopPropagation();
        const from = e.dataTransfer.getData(SLOT_MIME);
        if (from){
          const src = parseInt(from, 10);
          if (src !== i) moveTo(src, i);
        } else {
          send(e.dataTransfer.files, i);
        }
      });
      g.appendChild(d);
    }
  }
  [...g.children].forEach((d,i) => {
    const n = i+1, row = byslot[n], hasLoop = loops.has(n);
    d.className = "cell" + (row?" "+row.state:"") + (hasLoop?" has-loop":"")
      + (selected===n?" sel":"");
    d.setAttribute("aria-pressed", selected===n ? "true" : "false");
    d.draggable = !!row;
    const loopNote = hasLoop ? " · holds a recorded loop" : "";
    d.title = row
      ? `Slot ${n}: ${row.display_name} — drag to another slot to move or swap${loopNote}`
      : hasLoop ? `Slot ${n}: recorded loop only` : `Slot ${n} (empty)`;
    // title is a tooltip, not a name — spell the name out for a screen reader.
    d.setAttribute("aria-label", row
      ? `Slot ${n}, ${row.display_name}, ${row.state}${loopNote}`
      : hasLoop ? `Slot ${n}, recorded loop only` : `Slot ${n}, empty`);
    // Exactly one cell is tabbable: wherever the user last arrowed to, else
    // the selected slot, else the first.
    const roving = rovingSlot !== null ? rovingSlot
                 : selected !== null ? selected : 1;
    d.tabIndex = n === roving ? 0 : -1;
  });

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

  if (binMode){
    /* leave the bin prompt in place while a slot is being dragged */
  } else if (selected != null){
    const pad = pad2(selected);
    const occ = byslot[selected];
    $("#drophead").innerHTML = `Drop here to fill slot <b>${pad}</b>`;
    const nxt = [selected+1, selected+2].filter(n => n <= total)
      .map(n => pad2(n));
    $("#dropnote").textContent = occ
      ? `Slot ${pad} holds “${occ.display_name}” — tap another slot to move it there, drop a file to replace it, or tap ${pad} again to deselect.`
      : nxt.length
        ? `Extra files continue into ${nxt.join(", ")}… Tap the slot again to deselect.`
        : `Slot ${pad} is the last slot. Tap the slot again to deselect.`;
  } else {
    $("#drophead").textContent = "Drop audio here, or tap to choose";
    $("#dropnote").textContent =
      "Drop straight onto a slot to target it, or use a leading number — “07 Blue Bossa.mp3”";
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
  // This page is the only status surface — there is no panel light or display —
  // so the mid-write warning has to be unmissable here or nowhere.
  if (s.ending)            { setText(m, "Shutting down — leave everything plugged in until this page disconnects"); m.className="msg warn"; }
  else if (s.busy && s.busy_kind === "write")
                           { setText(m, s.busy + " — don't unplug"); m.className="msg warn"; }
  else if (s.busy)         { setText(m, s.busy); m.className="msg"; }
  else if (s.error)        { setText(m, s.error); m.className="msg err"; }
  else if (s.pedal==="mounted") { setText(m, "Ready"); m.className="msg"; }
  else                     { setText(m, "Plug in the pedal"); m.className="msg"; }

  $("#done").disabled = s.ending;

  // The library's own rows come from /api/library, but its slot badges and its
  // assign targets come from the snapshot — so a new snapshot re-renders it.
  renderLibrary();
}

/* Rebuild `host` while keeping focus where the user put it.

   A dirty check keeps most rebuilds from happening at all, but the ones that do
   still happen — a track finishing its write, a rename landing — must not steal
   focus. Nodes opt in by setting data-fk to something stable across rebuilds.
*/
function rebuild(host, draw){
  const active = document.activeElement;
  const key = active && host.contains(active) ? active.dataset.fk : null;
  draw();
  if (key){
    const again = host.querySelector(`[data-fk="${CSS.escape(key)}"]`);
    if (again) again.focus();
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

/* Every write to #msg goes through setText, including the one-shot ones that
   could not spam on their own. A reader should not have to work out which
   messages are safe to write directly, and the next message put on a timer
   inherits the guard rather than rediscovering the problem. */

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
  if (updating || checking) return;     // don't clobber a transient label
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

/* Send it, parse whatever came back, say whether it worked — the shape seven of
   the thirteen call sites want. `status` is 0 when the request never reached the
   device: the device's own error is worth showing, a network failure is not. The
   six sites with their own control flow stay written out. */
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

function fail(text){ setText($("#msg"), text); $("#msg").className = "msg err"; }

async function moveTo(src, dst){
  const r = await api(`/api/slots/${src}/move`, jsonBody({to: dst}));
  if (!r.ok) failFrom(r, "Move failed");
}

async function send(files, start){
  if (!files || !files.length) return;
  const fd = new FormData();
  [...files].forEach(f => fd.append("file", f));
  if (start != null) fd.append("start", start);
  setText($("#msg"), start != null
    ? `Uploading to slot ${pad2(start)}…` : "Uploading…");
  let r;
  try {
    r = await fetch("/api/upload", {method:"POST", body:fd});
  } catch {
    selected = null;
    fail("Upload failed — check the connection");
    return;
  }
  const j = await r.json().catch(()=>({}));
  selected = null;
  // A 413 or a 500 carries no `errors` list, so an ok check has to come first
  // or the message stays on "Uploading…" forever.
  if (!r.ok && !(j.errors && j.errors.length)){
    fail(j.error || `Upload failed (${r.status})`);
    return;
  }
  if (j.errors && j.errors.length){
    fail(j.errors.map(e=>`${e.name}: ${e.error}`).join("; "));
  }
  // A slot upload creates a library row too. Without this the new track is
  // missing from the Library card until the next reconnect — in the common
  // case the five-minute stream rotation.
  loadLibrary();
}

/* Arrow-key movement inside the slot grid.

   With a roving tabindex the grid is a single tab stop, so the arrows have to
   provide movement within it. GRID_COLS mirrors the CSS
   `grid-template-columns: repeat(20, 1fr)`; if that changes, change this.

   Movement is linear over slot order, not bounded by the row. Left and Right
   step one slot and will cross a row edge, because slot 21 genuinely does
   follow slot 20 — the rows are how 99 slots are made to fit on a phone, not a
   structure the user is navigating. Stopping at column 20 would leave slot 21
   unreachable from slot 20 except by Down then nineteen Lefts. Up and Down
   step a whole row, so both traversals are available. Only the real ends, slot
   1 and slot 99, stop.
*/
const GRID_COLS = 20;
$("#grid").addEventListener("keydown", e => {
  const step = {ArrowRight: 1, ArrowLeft: -1,
                ArrowDown: GRID_COLS, ArrowUp: -GRID_COLS}[e.key];
  const cells = [...$("#grid").children];
  const here = cells.indexOf(document.activeElement);
  if (here < 0) return;
  let to = null;
  if (step !== undefined) to = here + step;
  else if (e.key === "Home") to = 0;
  else if (e.key === "End") to = cells.length - 1;
  else return;
  if (to < 0 || to >= cells.length) return;   // slot 1 and slot 99 are the ends
  e.preventDefault();
  cells[here].tabIndex = -1;
  cells[to].tabIndex = 0;
  rovingSlot = to + 1;              // survive the next render
  cells[to].focus();
});

const drop = $("#drop");

function setBinMode(on){
  binMode = on;
  drop.classList.toggle("bin", on);
  if (on){
    $("#drophead").innerHTML = "🗑 Drop here to remove from the pedal";
    $("#dropnote").textContent = "You can undo straight afterwards.";
  } else {
    render(state);
  }
}

async function removeSlot(slot, name){
  const resp = await api(`/api/slots/${slot}`, {method:"DELETE"});
  if (!resp.ok){ failFrom(resp, "Could not clear the slot"); return; }
  const j = resp.body;
  const host = $("#undoslot");
  host.innerHTML = "";
  // The delete names its own trash entry. Reading back the newest item
  // instead would offer to restore whatever was deleted last, not this.
  if (j.trash_id == null) return;
  const label = name || "slot " + pad2(slot);
  const b = document.createElement("button");
  b.className = "undo";
  b.textContent = `Undo “${label.length > 16 ? label.slice(0,15)+"…" : label}”`;
  b.onclick = async () => {
    // A 404 means the entry has since been pruned. fetch does not reject on
    // that, so the ok check is what catches it.
    const r = await api(`/api/trash/${j.trash_id}/restore`, {method:"POST"});
    if (!r.ok) failFrom(r, "Undo failed — the trash entry has gone");
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

/* No click handler here: #drop is the input's <label>, so the browser opens
   the picker. Calling .click() as well would open it twice. */
$("#file").onchange = e => { send(e.target.files, selected); e.target.value=""; };
["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add("over"); }));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => {
  const from = e.dataTransfer.getData(SLOT_MIME);
  if (from){
    const slot = parseInt(from, 10);
    const row = (state?.slots || []).find(s => s.slot === slot);
    removeSlot(slot, row && row.display_name);
  } else {
    send(e.dataTransfer.files, selected);
  }
});
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => e.preventDefault());

$("#done").onclick = async () => {
  if (!confirm("End the session? The pedal will be unmounted and the device will shut down.")) return;
  await fetch("/api/session/end", {method:"POST"});
};

$("#print").onclick = printList;

function endUpdating(){ updating = false; clearTimeout(updateTimer); updateTimer = null; }

// The single footer button has two resting states: "Update available" (runs the
// OTA update) and "Check for update" (asks the device to re-check the remote).
// The click dispatches on the current state.
$("#update").onclick = () => {
  if ($("#update").disabled) return;
  if (state && state.update_available) doUpdate();
  else doCheck();
};

async function doCheck(){
  const btn = $("#update");
  checking = true;
  btn.disabled = true; btn.textContent = "Checking…"; btn.className = "link";
  const r = await api("/api/update/check", {method:"POST"});
  if (!r.status){
    checking = false; updateBtnState(state);
    fail("Update check failed — check the connection");
    return;
  }
  const j = r.body;
  checking = false;
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
  // differ from a null baseline and clear `updating` before the update lands.
  if (!state || !state.revision){
    fail("Device state isn't ready yet — try again in a moment");
    return;
  }
  if (!confirm("Update to the latest version and restart? The device will briefly disconnect.")) return;
  updating = true;
  // Remember the revision we're leaving; render() clears `updating` once a
  // snapshot reports a different one (the new build is up).
  updatingFromRev = state.revision;
  btn.disabled = true; btn.textContent = "Updating…"; btn.className = "link";
  // Safety net: if no new revision ever arrives (restart failed to come back),
  // don't leave the button stuck forever.
  clearTimeout(updateTimer);
  updateTimer = setTimeout(() => {
    if (!updating) return;
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
  // Success: the service is restarting. `updating` stays set until a snapshot
  // shows the new revision (see render), keeping the button locked so a second
  // update can't start while the restart is pending.
  setText($("#msg"), "Updating… the page will reconnect"); $("#msg").className = "msg";
}

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
   lists, and this is the first one. Assigning from here costs a transcode at
   most — usually not even that, since the staged WAV may still be cached —
   rather than another upload over WiFi. */

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
  } catch {
    return;             // a snapshot or a later refetch will put it right
  }
  renderLibrary();
}

// Where an "add to the pedal" click would land: the selected slot if there is
// one, else the lowest free slot. Loop-bearing slots are left alone — we don't
// put a backing track under someone's recording by accident.
function assignTarget(){
  if (selected != null) return selected;
  if (!state) return null;
  const taken = new Set([...(state.slots || []).map(x => x.slot),
                         ...(state.loops || [])]);
  for (let i = 1; i <= (state.slot_count || 99); i++){
    if (!taken.has(i)) return i;
  }
  return null;
}

function libraryView(){
  const q = ($("#libq").value || "").trim().toLowerCase();
  const sort = $("#libsort").value;
  const rows = (library || []).filter(
    r => !q || r.name.toLowerCase().includes(q));
  if (sort === "name"){
    rows.sort((a,b) => a.name.localeCompare(b.name, undefined,
                                            {sensitivity:"base"}));
  } else if (sort === "duration"){
    rows.sort((a,b) => b.duration - a.duration);
  }                     // "added" is the order the server already returned
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
  const hideTools = all < 2;         // nothing to search or sort through yet
  // The one place this function writes the search box. The standing rule is
  // that it never does — that is what stops a snapshot arriving mid-keystroke
  // from wiping what is being typed — but the field is about to be hidden, so
  // there is no keystroke in flight to lose. Leaving a stale query applied
  // would filter the last remaining track out of the list with no visible
  // control to clear it.
  if (hideTools) $("#libq").value = "";
  $("#libtools").hidden = hideTools;

  // Which slots hold each track, from the snapshot — so these badges follow an
  // upload or a clear without refetching the library.
  const bySlot = {};
  ((state && state.slots) || []).forEach(s => {
    (bySlot[s.source_hash] = bySlot[s.source_hash] || []).push(s.slot);
  });

  // Everything the rows depend on. libRev stands in for the library's contents
  // so this stays cheap with a large library; the rest is what the snapshot
  // contributes (which slots hold what, and what the assign target would be)
  // plus the two uncontrolled inputs. Progress is deliberately absent.
  const libKey = JSON.stringify([
    libRev, selected, nowPlaying,
    ((state && state.slots) || []).map(s => [s.slot, s.source_hash]),
    (state && state.loops) || [],
    state && state.slot_count,
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

  const target = assignTarget();
  rows.forEach(r => host.appendChild(libraryRow(r, bySlot[r.source_hash], target)));

  const mins = library.reduce((t, r) => t + (r.duration || 0), 0);
  $("#libfoot").innerHTML =
    `<span>${all} track${all === 1 ? "" : "s"} · ${mmss(mins)}</span>`
    + (rows.length === all ? "" : `<span>${rows.length} shown</span>`);
}

function libraryRow(r, slots, target){
  const el = document.createElement("div");
  el.className = "librow";

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

  if (slots && slots.length){
    const b = document.createElement("span");
    b.className = "inslot";
    b.textContent = slots.map(n => pad2(n)).join(" ");
    b.title = slots.length > 1 ? `On the pedal in slots ${b.textContent}`
                               : `On the pedal in slot ${b.textContent}`;
    el.appendChild(b);
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

  const add = document.createElement("button");
  add.className = "libbtn";
  add.type = "button";
  add.disabled = target === null;
  add.textContent = target === null ? "Full" : "→ " + pad2(target);
  add.title = target === null
    ? "Every slot is taken"
    : (selected != null
        ? `Put “${r.name}” in slot ${pad2(target)}, the slot you have selected`
        : `Put “${r.name}” in slot ${pad2(target)}, the lowest free slot`);
  add.dataset.fk = "lib:" + r.source_hash + ":add";
  add.onclick = () => assignToSlot(r, target);
  el.appendChild(add);

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

function startRename(row, nm, r){
  if (editingHash !== null) return;
  editingHash = r.source_hash;
  const input = document.createElement("input");
  input.className = "libedit";
  input.type = "text";
  input.value = r.name;
  input.maxLength = 200;
  input.setAttribute("aria-label", "Rename " + r.name);
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
    editingHash = null;
    // This function replaced a span with an input, so the DOM is dirty on every
    // path out of here — including the two that change no data at all.
    libraryDomDirty();
    if (!save || !name || name === r.name){ renderLibrary(); return; }
    // Optimistic: the row already reads the new name, and a failure re-reads
    // the server's version rather than leaving a lie on screen.
    r.name = name;
    libRev++;          // edited in place, so the key must move
    renderLibrary();
    const resp = await api(`/api/library/${r.source_hash}`,
                           {...jsonBody({name}), method: "PATCH"});
    if (!resp.ok){
      failFrom(resp, "Rename failed");
      loadLibrary();
      return;
    }
    // The optimistic write went to the row object we captured, but
    // renderLibrary is only frozen during the edit — loadLibrary is not, and
    // es.onopen fires on the five-minute rotation. If it replaced `library`
    // while this was in flight, that object is detached and the row would
    // render the old name until some later refresh. Re-read from the server
    // so the rendered name is the committed one either way.
    loadLibrary();
  };

  input.onblur = () => finish(true);
  input.onkeydown = e => {
    if (e.key === "Enter"){ e.preventDefault(); finish(true); }
    else if (e.key === "Escape"){ e.preventDefault(); finish(false); }
  };
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

async function assignToSlot(r, slot){
  if (slot == null) return;
  const pad = pad2(slot);
  const resp = await api(`/api/slots/${slot}/assign`,
                         jsonBody({hash: r.source_hash}));
  if (!resp.ok){ failFrom(resp, `Couldn't put that in slot ${pad}`); return; }
  selected = null;      // consumed; the next click picks its own target
  loadLibrary();        // the slot badges come from the snapshot, but `added`
                        // ordering and any server-side change do not
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
  const fd = new FormData();
  [...files].forEach(f => fd.append("file", f));
  setText($("#msg"), "Adding to the library…");
  let r, j;
  try {
    r = await fetch("/api/library", {method:"POST", body:fd});
    j = await r.json().catch(()=>({}));
  } catch {
    fail("Upload failed — check the connection");
    return;
  }
  if (!r.ok && !(j.errors && j.errors.length)){
    fail(j.error || `Upload failed (${r.status})`);
    return;
  }
  if (j.errors && j.errors.length){
    fail(j.errors.map(e => `${e.name}: ${e.error}`).join("; "));
  }
  loadLibrary();
}

$("#libfile").onchange = e => { sendToLibrary(e.target.files); e.target.value=""; };
// Uncontrolled inputs: read on demand, never written by a render, so a snapshot
// arriving mid-keystroke can't wipe what's being typed.
$("#libq").oninput = renderLibrary;
$("#libsort").onchange = renderLibrary;

// es.onopen also loads it, but only once the stream handshake completes. Ask
// now so the card fills even if the event stream is slow or never comes up.
loadLibrary();
