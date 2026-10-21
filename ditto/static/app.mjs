// @ts-check
/* The page: Preact components over the device's JSON API. State arrives on
   the event stream and the components follow it; the library is fetched on
   every (re)connect and after anything that changes it. */

import {
  html, render, useState, useEffect, useRef,
} from "https://cdn.jsdelivr.net/npm/htm@3.1.1/preact/standalone.module.js";

const pad2 = n => String(n).padStart(2, "0");
const mmss = s => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${pad2(s % 60)}`; };
const LABEL = {converting: "converting", staged: "queued", synced: "on pedal", error: "error"};

/* ------------------------------------------------- talking to the device */

async function api(url, opts){
  try {
    const r = await fetch(url, opts);
    return {ok: r.ok, status: r.status, body: await r.json().catch(() => ({}))};
  } catch {
    return {ok: false, status: 0, body: {}};
  }
}
const jsonBody = body => ({method: "POST",
                           headers: {"Content-Type": "application/json"},
                           body: JSON.stringify(body)});
const reason = (r, fallback) =>
  r.status ? (r.body.error || fallback) : `${fallback} — check the connection`;

/* One multipart POST. A 413, a 500 or a dropped connection carries no errors
   list, so each becomes one entry. */
async function postFiles(url, files){
  const fd = new FormData();
  [...files].forEach(f => fd.append("file", f));
  const r = await api(url, {method: "POST", body: fd});
  if (!r.ok && !(r.body.errors && r.body.errors.length)){
    return {added: [], errors: [{error: reason(r, `Upload failed (${r.status})`)}]};
  }
  return {added: r.body.added || [], errors: r.body.errors || []};
}

function reportUpload(res, toPedal, say, fail){
  if (res.errors.length){
    fail(res.errors.map(e => e.name ? `${e.name}: ${e.error}` : e.error).join("; "));
    return;
  }
  const n = res.added.length;
  if (!n) return;
  if (!toPedal) say(`${n} track${n === 1 ? "" : "s"} added to the library`);
  else if (n === 1 && res.added[0].slot) say(`“${res.added[0].display_name}” → slot ${pad2(res.added[0].slot)}`);
  else say(`${n} tracks queued for the pedal`);
}

/* Handlers that take files dropped from the desktop on a section and ignore
   any other drag. dragleave also fires crossing into a child, so only
   leaving the section itself clears the highlight. */
const fileDrop = (setOver, take) => {
  const files = e => e.dataTransfer.types.includes("Files");
  return {
    onDragOver: e => { if (files(e)){ e.preventDefault(); setOver(true); } },
    onDragLeave: e => { if (!e.currentTarget.contains(e.relatedTarget)) setOver(false); },
    onDrop: e => { if (files(e)){ e.preventDefault(); setOver(false); take(e.dataTransfer.files); } },
  };
};

/* What is being dragged from this page, in the same shape as a pick: a slot
   or a library track. dragover can't read the drag's data, only its types,
   and the drop hint has to name the track before the drop. */
let dragging = null;
const dragFrom = item => ({
  draggable: true,
  onDragStart: e => {
    dragging = item;
    e.dataTransfer.setData("text/plain", item.name);   // Firefox drags nothing without data
    e.dataTransfer.effectAllowed = "move";
  },
  onDragEnd: () => { dragging = null; },
});

/* ------------------------------------------------------------------ hooks */

/* The device's state, live over the event stream, and the library, fetched. */
function useDevice(){
  const [state, setState] = useState(null);
  const [library, setLibrary] = useState([]);
  const [link, setLink] = useState("connecting");   // connecting | up | down
  const loadLibrary = async () => {
    const r = await api("/api/library");
    if (r.ok) setLibrary(r.body);
  };
  useEffect(() => {
    const es = new EventSource("/api/events");
    let grace = null;
    es.onopen = () => { clearTimeout(grace); setLink("up"); loadLibrary(); };
    es.onmessage = e => setState(JSON.parse(e.data));
    // The server retires each stream after five minutes and EventSource
    // reconnects on its own; only an outage that outlasts the grace shows.
    es.onerror = () => {
      clearTimeout(grace);
      grace = setTimeout(() => {
        if (es.readyState !== EventSource.OPEN) setLink("down");
      }, 8000);
    };
    return () => { clearTimeout(grace); es.close(); };
  }, []);
  return {state, library, loadLibrary, link};
}

/* A line in the footer that holds for six seconds, then hands the line back
   to the device's own status. */
function useNotice(){
  const [notice, setNotice] = useState(null);
  const timer = useRef(null);
  const show = kind => text => {
    setNotice({text, kind});
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setNotice(null), 6000);
  };
  return {notice, say: show(""), warn: show("warn"), fail: show("err")};
}

/* -------------------------------------------------------------------- app */

function App(){
  const {state, library, loadLibrary, link} = useDevice();
  const {notice, say, warn, fail} = useNotice();
  const [undo, setUndo] = useState(null);         // {slot, hash, name}
  const undoTimer = useRef(null);
  // A track picked up to be put in a slot: {kind: "slot" | "hash", value, name}.
  const [pick, setPick] = useState(null);

  useEffect(() => {
    if (!pick) return;
    const esc = e => { if (e.key === "Escape") setPick(null); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [pick]);

  useEffect(() => {   // a file dropped outside the zone must not navigate away
    const stop = e => e.preventDefault();
    document.addEventListener("dragover", stop);
    document.addEventListener("drop", stop);
    return () => {
      document.removeEventListener("dragover", stop);
      document.removeEventListener("drop", stop);
    };
  }, []);

  const offerUndo = u => {
    setUndo(u);
    clearTimeout(undoTimer.current);
    undoTimer.current = setTimeout(() => setUndo(null), 12000);
  };
  const doUndo = async () => {
    // The track is still in the library, so undo is an assign back.
    const r = await api(`/api/slots/${undo.slot}/assign`, jsonBody({hash: undo.hash}));
    if (!r.ok) fail(reason(r, "Undo failed — the track has gone"));
    setUndo(null);
  };

  const mounted = state && state.pedal === "mounted";
  const status = notice ? notice.text
    : pick ? `Choose a slot for “${pick.name}”`
    : link === "down" ? "Reconnecting…"
    : !state ? "Connecting…"
    : state.busy ? state.busy
    : state.error ? state.error
    : mounted ? "Ready" : "Plug in the pedal";
  const kind = notice ? notice.kind
    : (link === "down" || (state && !state.busy && state.error)) ? "err" : "";

  return html`
    <header class="container">
      <h1>DittoBackTracker</h1>
      <span class=${"pedal " + (mounted ? "ok" : "off")}>${mounted ? "■ Pedal connected" : "■ No pedal"}</span>
      <small>${state && state.version
        ? `v${state.version}${state.revision ? " · " + state.revision : ""}` : ""}</small>
      <${UpdateButton} state=${state} say=${say} fail=${fail}/>
    </header>
    <main class="container grid">
      <${Pedal} state=${state} say=${say} warn=${warn} fail=${fail}
        offerUndo=${offerUndo} clearUndo=${() => setUndo(null)} loadLibrary=${loadLibrary}
        pick=${pick} setPick=${setPick}/>
      <${Library} state=${state} library=${library} loadLibrary=${loadLibrary}
        say=${say} warn=${warn} fail=${fail} clearUndo=${() => setUndo(null)} setPick=${setPick}/>
    </main>
    <footer>
      ${state && state.busy && state.progress != null
        && html`<progress value=${state.progress} max="1" aria-label="Conversion progress"/>`}
      <span class=${"notice " + kind} role="status" aria-live="polite">${status}</span>
      ${pick && html`<button class="secondary outline" onClick=${() => setPick(null)}>Cancel</button>`}
      ${undo && html`<button class="secondary outline" onClick=${doUndo}>Undo “${undo.name}”</button>`}
    </footer>`;
}

/* One button: "Update available" runs the update, otherwise it asks the
   device to check. An update is complete when a snapshot reports a new
   revision, not when the stream reconnects. */
function UpdateButton({state, say, fail}){
  const [busy, setBusy] = useState(null);         // "checking" | "updating" | null
  const fromRev = useRef(null);
  const revision = state && state.revision;
  useEffect(() => {
    if (busy === "updating" && revision && revision !== fromRev.current) setBusy(null);
  }, [revision]);
  useEffect(() => {
    if (busy !== "updating") return;
    const t = setTimeout(() => {
      setBusy(null);
      fail("Update didn't confirm — reload the page to check");
    }, 180000);
    return () => clearTimeout(t);
  }, [busy]);
  if (!state) return null;

  const click = async () => {
    if (state.update_available){
      if (!confirm("Update to the latest version and restart? The device will briefly disconnect.")) return;
      fromRev.current = state.revision;
      setBusy("updating");
      const r = await api("/api/update", {method: "POST"});
      if (!r.ok){ setBusy(null); fail(reason(r, "Update failed")); return; }
      say("Updating… the page will reconnect");
    } else {
      setBusy("checking");
      const r = await api("/api/update/check", {method: "POST"});
      setBusy(null);
      if (!r.ok || !r.body.ok){
        fail(r.body.error ? "Update check failed: " + r.body.error : "Update check failed");
        return;
      }
      if (!r.body.update_available) say("Up to date");
    }
  };
  const label = busy === "updating" ? "Updating…" : busy === "checking" ? "Checking…"
    : state.update_available ? "Update available" : "Check for update";
  return html`<button class=${state.update_available ? "" : "secondary outline"}
    disabled=${busy !== null} onClick=${click}>${label}</button>`;
}

/* ---------------------------------------------------------- on the pedal */

function Pedal({state, say, warn, fail, offerUndo, clearUndo, loadLibrary, pick, setPick}){
  const [over, setOver] = useState(false);
  const [all, setAll] = useState(false);
  const [target, setTarget] = useState(null);     // {n, verb, text}: the slot being dragged over
  const section = useRef(null);
  const back = useRef(null);       // where focus goes when a pick ends: a slot, or an element
  const s = state;
  const slots = s ? s.slots : [];
  const loops = new Set(s ? s.loops : []);
  const tracks = new Set(s ? s.tracks : []);      // a BT.WAV, whether or not a row knows it
  const byslot = Object.fromEntries(slots.map(r => [r.slot, r]));
  const used = new Set([...slots.map(r => r.slot), ...tracks, ...loops]);
  // Empty slots are listed too, so there is somewhere to put a track: every
  // slot up to three past the last one used, at least twelve.
  const max = s ? s.slot_count : 0;
  const shown = all ? max : Math.min(max, Math.max(12, Math.max(0, ...used) + 3));
  const nums = Array.from({length: shown}, (_, i) => i + 1);
  const cap = s && s.capacity;

  // The device decides the slot: a leading number in the name, else the next
  // free one.
  const send = async files => {
    if (!files || !files.length) return;
    reportUpload(await postFiles("/api/upload", files), true, say, fail);
    loadLibrary();
  };
  // A row's track is still in the library, so it gets an undo. A BT.WAV no
  // row knows has no source to come back from, so it gets a confirmation.
  const clear = async (n, row) => {
    if (!row && !confirm(`Delete the backing track in slot ${pad2(n)} from the pedal? `
                         + `It isn't in the library, so you can't undo this.`)) return;
    const r = await api(`/api/slots/${n}`, {method: "DELETE"});
    if (!r.ok){ fail(reason(r, "Could not remove the track")); return; }
    if (row) offerUndo({slot: n, hash: row.source_hash, name: row.display_name});
  };
  const move = async (from, to) => {
    const r = await api(`/api/slots/${from}/move`, jsonBody({to}));
    if (!r.ok) fail(reason(r, "Move failed"));
    return r.ok;
  };
  const removeLoop = async n => {
    // A loop is a live take with no source, so this is the one delete with a
    // confirmation and no undo.
    if (!confirm(`Delete the recorded loop in slot ${pad2(n)}? The backing track stays. You can't undo this.`)) return;
    const r = await api(`/api/loops/${n}`, {method: "DELETE"});
    if (!r.ok) fail(reason(r, "Could not remove the loop"));
  };

  // What putting `item` in slot n would do: a short verb for the button and
  // the whole of it for the label. null when it would change nothing. A move
  // onto a track the database knows swaps the two.
  const hint = (n, item) => {
    const row = byslot[n];
    if (item.kind === "slot" ? item.value === n : row && row.source_hash === item.value) return null;
    if (item.kind === "slot" && row) return {verb: "Swap", text: `Swap with “${row.display_name}”`};
    if (row) return {verb: "Replace", text: `Replace “${row.display_name}”`};
    if (tracks.has(n)) return {verb: "Replace", text: "Replace the unknown backing track"};
    const what = item.kind !== "files" ? `“${item.name}”` : item.count === 1 ? "the file" : `${item.count} files`;
    return {verb: item.kind === "slot" ? "Move here" : "Put here", text: `Put ${what} here`};
  };
  // A track the database doesn't know has no undo, so replacing it asks
  // first, as removing it does.
  const mayReplace = n => byslot[n] || !tracks.has(n)
    || confirm(`Replace the backing track in slot ${pad2(n)}? `
               + `It isn't in the library, so you can't undo this.`);
  // A replaced library track gets an undo. Files go to the library, then
  // the first to slot n and the rest to the empty slots after it; the slot
  // dropped on beats a number in the name. `asked` when mayReplace already ran.
  const placeOn = async (n, item, asked = false) => {
    const row = byslot[n];
    if (!hint(n, item)) return;
    if (!asked && !mayReplace(n)) return;
    if (item.kind === "slot"){
      if (await move(item.value, n)) clearUndo();   // an undo into either slot would undo the move
      return;
    }
    let list = [item], errors = [];
    if (item.kind === "files"){
      const res = await postFiles("/api/library", item.files);
      loadLibrary();
      list = res.added.map(r => ({value: r.source_hash, name: r.name}));
      errors = res.errors;
    }
    const into = [n];
    for (let i = n + 1; i <= max; i++) if (!used.has(i)) into.push(i);
    const placed = [];
    for (const [i, t] of list.slice(0, into.length).entries()){
      const r = await api(`/api/slots/${into[i]}/assign`, jsonBody({hash: t.value}));
      if (r.ok) placed.push(into[i]);
      else errors.push({name: t.name, error: reason(r, "could not put it on the pedal")});
    }
    if (placed[0] === n && row) offerUndo({slot: n, hash: row.source_hash, name: row.display_name});
    else clearUndo();               // its slot may have just been refilled
    const left = Math.max(0, list.length - into.length);
    if (errors.length) reportUpload({added: [], errors}, false, say, fail);
    else if (left > 0) warn(`${left} didn't fit and stayed in the library`);
    else if (placed.length === 1) say(`“${list[0].name}” → slot ${pad2(placed[0])}`);
    else if (placed.length) say(`${placed.length} tracks → slots ${pad2(placed[0])}–${pad2(placed[placed.length - 1])}`);
  };
  // A row takes a drag from the page or files from the desktop, and says
  // what dropping there would do. A drag it would ignore shows nothing.
  const held = e => dragging
    || (e.dataTransfer.types.includes("Files") ? {kind: "files", count: e.dataTransfer.items.length} : null);
  const dropOn = n => ({
    onDragOver: e => {
      const item = held(e), h = item && hint(n, item);
      if (!h){ setTarget(null); return; }
      e.preventDefault();
      e.stopPropagation();          // the row, not the section, takes it
      setOver(false);
      if (!target || target.n !== n) setTarget({n, ...h});
    },
    onDragLeave: e => {
      if (!e.currentTarget.contains(e.relatedTarget)) setTarget(t => t && t.n === n ? null : t);
    },
    onDrop: e => {
      const item = held(e);
      e.preventDefault();
      e.stopPropagation();
      setTarget(null);
      dragging = null;
      if (item) placeOn(n, item.kind === "files" ? {...item, files: e.dataTransfer.files} : item);
    },
  });
  // The first cell: the slot number, as a handle when there is a track to
  // pick up, or while one is picked up, a button that puts it here.
  const lead = n => {
    if (target && target.n === n) return html`<span class="here mark">${pad2(n)} ${target.verb}</span>`;
    const h = pick && hint(n, pick);
    if (h) return html`<button class=${h.verb === "Replace" ? "here replace secondary outline" : "here"} aria-label=${`Slot ${pad2(n)}: ${h.text}`} title=${h.text}
      onClick=${() => {
        if (!mayReplace(n)) return;   // the pick stays, and focus with this button
        back.current = n;
        setPick(null);
        placeOn(n, pick, true);
      }}>${pad2(n)} ${h.verb}</button>`;
    const row = byslot[n];
    if (row && !pick) return html`<button class="handle secondary outline" data-slot=${n}
      aria-label=${`Move “${row.display_name}” to another slot`} title="Move to another slot"
      onClick=${() => setPick({kind: "slot", value: n, name: row.display_name})}>⠿ ${pad2(n)}</button>`;
    return html`<code>${pad2(n)}</code>`;
  };

  useEffect(() => {   // a picked-up slot that empties under us has nothing left to move
    if (pick && pick.kind === "slot" && !byslot[pick.value]) setPick(null);
  }, [pick, state]);
  useEffect(() => {   // keyboard users go straight to the first slot that replaces nothing
    if (!pick) return;
    back.current = pick.kind === "slot" ? pick.value : document.activeElement;
    const b = section.current.querySelector("button.here:not(.replace)")
              || section.current.querySelector("button.here");
    if (b) b.focus();
  }, [pick]);
  useEffect(() => {
    // The buttons that held focus go when the pick ends. Focus goes back to
    // what picked it up, or to where it was put once that slot's handle
    // renders. If focus has moved on meanwhile, it stays where it is.
    if (pick || back.current == null) return;
    const b = typeof back.current === "number"
      ? section.current.querySelector(`button.handle[data-slot="${back.current}"]`) : back.current;
    if (b && b.isConnected){ b.focus(); back.current = null; }
    else if (document.activeElement !== document.body) back.current = null;
  }, [pick, state]);

  return html`
    <section ref=${section} class=${over ? "over" : ""} ...${fileDrop(setOver, send)}>
      <div class="bar">
        <h2>On the pedal</h2>
        <label role="button" class="secondary outline">Choose files
          <input type="file" multiple accept="audio/*" hidden
            onChange=${e => { send(e.currentTarget.files); e.currentTarget.value = ""; }}/>
        </label>
      </div>
      <p class="sub">${cap && cap.total_seconds
        ? `${cap.used_label} of ${cap.total_label} · ${mmss(cap.total_seconds - cap.used_seconds)} free`
          + ` · ${used.size} of ${s.slot_count} slots`
        : "Connect the pedal to see capacity"}</p>
      ${cap && cap.total_seconds
        ? html`<progress value=${cap.used_seconds} max=${cap.total_seconds}/>` : null}
      <p class="sub"><small>Drop audio here to fill the next free slots.
        A leading number picks the slot: “07 Blue Bossa.mp3”</small></p>
      ${nums.length
        ? html`<div class="overflow-auto"><table><tbody>${nums.map(n => used.has(n)
            ? html`<${SlotRow} key=${n} n=${n} row=${byslot[n]} track=${tracks.has(n)} loop=${loops.has(n)}
                lead=${lead(n)} clear=${clear} removeLoop=${removeLoop} target=${target && target.n === n}
                drag=${{...dropOn(n), ...(byslot[n] ? dragFrom({kind: "slot", value: n, name: byslot[n].display_name}) : {})}}/>`
            : html`<tr key=${n} class=${"empty" + (target && target.n === n ? " target" : "")} ...${dropOn(n)}>
                <td>${lead(n)}</td>
                <td class="name" colspan="4">${target && target.n === n ? target.text : "empty"}</td></tr>`)}
          </tbody></table></div>`
        : null}
      ${max > 12 && (all || shown < max)
        ? html`<button class="secondary outline" onClick=${() => setAll(!all)}>
            ${all ? "Show fewer slots" : `Show all ${max} slots`}</button>` : null}
      ${slots.length
        ? html`<button class="secondary outline" onClick=${() => printList(slots)}>Print list</button>` : null}
    </section>`;
}

/* One row per slot, naming both files the pedal can hold there, each with
   its own remove button, so removing one never looks like removing both. */
function SlotRow({n, row, track, loop, lead, clear, removeLoop, target, drag}){
  const pad = pad2(n);
  const name = row ? row.display_name : track ? html`<em>Unknown backing track</em>` : null;
  return [html`
    <tr key="row" class=${target ? "target" : ""} ...${drag}>
      <td>${lead}</td>
      <td class="name">${name
        ? html`${name}${loop && html` <small>+ loop</small>`}`
        : html`<em>Recorded loop</em>`}</td>
      <td><small>${row ? mmss(row.duration) : ""}</small></td>
      <td><small class=${row && (row.state === "converting" || row.state === "error") ? "warn" : ""}>${row ? (LABEL[row.state] || row.state) : ""}</small></td>
      <td class="actions">
        ${(row || track) && html`<button class="secondary outline" title="Remove track"
          aria-label=${"Remove the backing track in slot " + pad}
          onClick=${() => clear(n, row)}>×</button>`}
        ${loop && html`<button class="secondary outline" title="Remove loop"
          aria-label=${"Remove the recorded loop in slot " + pad}
          onClick=${() => removeLoop(n)}>↻×</button>`}
        ${loop && html`<a href=${"/api/loops/" + n} download=${"loop-" + pad + ".wav"} role="button"
          class="secondary outline" aria-label=${"Download the loop in slot " + pad}>⤓</a>`}
      </td>
    </tr>`,
  row && row.state === "error" && row.error
    && html`<tr key="err" class="sub"><td></td><td colspan="4"><small class="err">${row.error}</small></td></tr>`,
  ];
}

// A bare table of slot number and name, in slot order, with the page margin
// zeroed so the browser has nowhere to print its own header and footer.
function printList(slots){
  const rows = slots.slice().sort((a, b) => a.slot - b.slot).map(r =>
    `<tr><td class="n">${pad2(r.slot)}</td><td>${escapeHtml(r.display_name)}</td></tr>`).join("");
  const w = window.open("", "_blank");        // synchronously, so it is not a pop-up
  if (!w) return;
  w.document.write(`<!doctype html><html><head><meta charset="utf-8">` +
    `<title>DittoBackTracker — backing tracks</title><style>@page{margin:0}` +
    `body{font:14px/1.5 system-ui,sans-serif;margin:16mm;color:#000;background:#fff}` +
    `td{padding:2px 0}td.n{padding-right:10px;font-family:monospace;color:#555;text-align:right}` +
    `</style></head><body><table>${rows}</table></body></html>`);
  w.document.close();
  w.focus();
  w.print();
}
const escapeHtml = s => s.replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

/* --------------------------------------------------------------- library */

function Library({state, library, loadLibrary, say, warn, fail, clearUndo, setPick}){
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("name");
  const [checked, setChecked] = useState(() => new Set());
  const [playing, setPlaying] = useState(null);   // hash being auditioned
  const [over, setOver] = useState(false);
  const player = useRef(null);
  if (!player.current){ player.current = new Audio(); player.current.preload = "none"; }
  useEffect(() => { player.current.onended = () => setPlaying(null); }, []);

  const bySlot = {};
  (state ? state.slots : []).forEach(r =>
    (bySlot[r.source_hash] = bySlot[r.source_hash] || []).push(r.slot));
  const needle = q.trim().toLowerCase();
  const rows = library.filter(r => !needle || r.name.toLowerCase().includes(needle));
  if (sort === "name") rows.sort((a, b) => a.name.localeCompare(b.name, undefined, {sensitivity: "base"}));
  else if (sort === "duration") rows.sort((a, b) => b.duration - a.duration);
  const ticked = rows.filter(r => checked.has(r.source_hash));
  const total = library.reduce((t, r) => t + (r.duration || 0), 0);

  const toggle = (h, on) => setChecked(prev => {
    const next = new Set(prev);
    if (on) next.add(h); else next.delete(h);
    return next;
  });
  const addFiles = async files => {
    if (!files.length) return;
    reportUpload(await postFiles("/api/library", files), false, say, fail);
    loadLibrary();
  };
  // One call for the lot: the device knows which slots hold a loop, and one
  // snapshot comes back rather than one per track.
  const addToPedal = async list => {
    const r = await api("/api/slots/assign", jsonBody({hashes: list.map(x => x.source_hash)}));
    if (!r.ok){ fail(reason(r, "Could not put that on the pedal")); return; }
    clearUndo();                    // its slot may have just been refilled
    setChecked(new Set());
    const p = r.body;
    if (!p.assigned.length){ warn("No room on the pedal"); return; }
    let line = p.assigned.length === 1
      ? `“${p.assigned[0].name}” → slot ${pad2(p.start)}`
      : `${p.assigned.length} tracks → slots ${pad2(p.start)}–${pad2(p.end)}`;
    if (p.unplaced.length) line += ` · ${p.unplaced.length} didn't fit`;
    if (!p.loops_known) line += " · plug the pedal in to skip its loops";
    (p.unplaced.length ? warn : say)(line);
  };
  const rename = async (r, name) => {
    const resp = await api(`/api/library/${r.source_hash}`, {...jsonBody({name}), method: "PATCH"});
    if (!resp.ok) fail(reason(resp, "Rename failed"));
    loadLibrary();
  };
  const forget = async (r, force) => {
    if (!force && !confirm(`Delete “${r.name}” from the device? This removes the audio, `
                           + `not just the pedal slot, and you can't undo it.`)) return;
    const resp = await api(`/api/library/${r.source_hash}${force ? "?force" : ""}`, {method: "DELETE"});
    if (resp.status === 409){
      const slots = resp.body.slots || [];
      if (confirm(`“${r.name}” is on the pedal in slot ${slots.map(pad2).join(", ")}. `
                  + `Clear ${slots.length > 1 ? "those slots" : "that slot"} and delete it?`)) return forget(r, true);
      return;
    }
    if (!resp.ok) fail(reason(resp, "Delete failed"));
    if (playing === r.source_hash){ player.current.pause(); setPlaying(null); }
    loadLibrary();
  };
  const audition = r => {
    const p = player.current;
    if (playing === r.source_hash){ p.pause(); setPlaying(null); return; }
    p.src = `/api/library/${r.source_hash}/audio`;   // also stops whatever was playing
    setPlaying(r.source_hash);
    p.play().catch(() => {
      // .ogg, .opus, .wma and older .flac don't play everywhere; the pedal
      // takes them fine.
      fail("This browser can't play that format — it will still convert fine");
      setPlaying(null);
    });
  };

  return html`
    <section class=${over ? "over" : ""} ...${fileDrop(setOver, addFiles)}>
      <div class="bar">
        <h2>Library</h2>
        ${ticked.length ? html`<button onClick=${() => addToPedal(ticked)}>Add ${ticked.length} to pedal</button>` : null}
        <label role="button" class="secondary outline">Add files
          <input type="file" multiple accept="audio/*" hidden
            onChange=${e => { addFiles(e.currentTarget.files); e.currentTarget.value = ""; }}/>
        </label>
      </div>
      ${library.length > 1 && html`<div class="grid">
        <input type="search" placeholder="Search" aria-label="Search the library"
          value=${q} onInput=${e => setQ(e.currentTarget.value)}/>
        <select aria-label="Sort the library" value=${sort} onChange=${e => setSort(e.currentTarget.value)}>
          <option value="name">Name</option>
          <option value="added">Newest first</option>
          <option value="duration">Length</option>
        </select>
      </div>`}
      ${!library.length
        ? html`<p><small>Nothing in the library yet. Anything you upload stays here until you delete it.</small></p>`
        : !rows.length
        ? html`<p><small>Nothing matches that search.</small></p>`
        : html`<table><tbody>${rows.map(r => html`
            <${LibRow} key=${r.source_hash} r=${r} slots=${bySlot[r.source_hash] || []}
              checked=${checked.has(r.source_hash)} toggle=${toggle}
              playing=${playing === r.source_hash} audition=${audition}
              rename=${rename} forget=${forget} add=${() => addToPedal([r])}
              place=${() => setPick({kind: "hash", value: r.source_hash, name: r.name})}/>`)}
          </tbody></table>`}
      <small>${library.length} track${library.length === 1 ? "" : "s"} · ${mmss(total)}
        ${needle ? ` · ${rows.length} match${rows.length === 1 ? "" : "es"}` : ""}</small>
    </section>`;
}

function LibRow({r, slots, checked, toggle, playing, audition, rename, forget, add, place}){
  const [draft, setDraft] = useState(null);       // the name being typed, or null
  const escaped = useRef(false);
  const commit = () => {
    if (escaped.current){ escaped.current = false; return; }
    const name = (draft || "").trim();
    setDraft(null);
    if (name && name !== r.name) rename(r, name);
  };
  return html`<tr ...${draft === null ? dragFrom({kind: "hash", value: r.source_hash, name: r.name}) : {}}>
    <td><input type="checkbox" checked=${checked} aria-label=${"Select " + r.name}
      onChange=${e => toggle(r.source_hash, e.currentTarget.checked)}/></td>
    <td class="name">${draft === null
      ? html`<span class="rename" tabindex="0" title="Click to rename"
          onClick=${() => setDraft(r.name)}
          onKeyDown=${e => { if (e.key === "Enter" || e.key === " "){ e.preventDefault(); setDraft(r.name); } }}
          >${r.name}</span>`
      : html`<input type="text" value=${draft} maxlength="200" aria-label=${"Rename " + r.name}
          ref=${el => { if (el && !el.dataset.focused){ el.dataset.focused = "1"; el.focus(); el.select(); } }}
          onInput=${e => setDraft(e.currentTarget.value)}
          onBlur=${commit}
          onKeyDown=${e => {
            if (e.key === "Enter") e.currentTarget.blur();
            else if (e.key === "Escape"){ escaped.current = true; setDraft(null); }
          }}/>`}</td>
    <td><small>${mmss(r.duration)}</small></td>
    <td>${slots.length
      ? html`<code title=${"On the pedal in slot " + slots.map(pad2).join(", ")}>${pad2(Math.min(...slots))}${slots.length > 1 ? ` +${slots.length - 1}` : ""}</code>`
      : html`<button class="secondary outline" aria-label=${"Add " + r.name + " to the pedal"}
          title="Put on the pedal in the next free slot" onClick=${add}>Add</button>`}</td>
    <td><button class="secondary outline" aria-label=${"Choose a slot for " + r.name}
      title="Choose a slot" onClick=${place}>Place…</button></td>
    <td><button class="secondary outline" aria-label=${(playing ? "Stop " : "Listen to ") + r.name}
      onClick=${() => audition(r)}>${playing ? "■" : "▶"}</button></td>
    <td><button class="secondary outline" aria-label=${"Delete " + r.name}
      onClick=${() => forget(r)}>×</button></td>
  </tr>`;
}

render(html`<${App}/>`, document.getElementById("app"));
