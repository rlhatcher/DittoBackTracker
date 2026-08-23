"""Flask routes and SSE. Single user on a trusted LAN — no auth."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import NamedTuple, Optional

from flask import (Flask, Response, abort, jsonify, request, send_file,
                   send_from_directory, stream_with_context)

from . import config, db, pedal
from .core import Service, ShuttingDown

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
LEADING_NUM = re.compile(r"^\D*?0*(\d{1,2})(?:\D|$)")

# media.file_hash is sha256().hexdigest()[:20] — exactly 20 lowercase hex chars.
# This has to be a whitelist match rather than a resolve()-and-contain check,
# because _source_for *globs* sources/{h}.*: a `*`, `?` or `[` in the hash would
# make the glob match some other library file entirely, which no amount of path
# containment checking would catch.
HASH_RE = re.compile(r"\A[0-9a-f]{20}\Z")

# Content types for auditioning an original upload. Keep in step with
# config.AUDIO_SUFFIXES — the assertion below fails the import, not a request,
# if the two ever drift.
AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma",
    # Opus here is always in an Ogg container. "audio/opus" names the codec,
    # not a container, and browsers sniff audio/ogg more reliably.
    ".opus": "audio/ogg",
}
assert set(AUDIO_MIME) == config.AUDIO_SUFFIXES, \
    "AUDIO_MIME and config.AUDIO_SUFFIXES have drifted apart"

MAX_NAME_LEN = 200

# Servable front-end assets, by exact name. No charset here — Werkzeug appends
# one for text/* and would otherwise emit it twice.
#
# Archivo is served from here rather than fetched from Google Fonts: the device
# is usually the only thing on its network, and a render-blocking font request
# to a host it cannot reach stalls first paint until DNS gives up. One variable
# woff2 covers the whole 400-800 weight axis, so there is no second file. The
# licence sits beside it and is served too, because OFL 1.1 asks that it travel
# with the font.
ASSETS = {
    "app.js": "text/javascript",
    "app.css": "text/css",
    "archivo-latin.woff2": "font/woff2",
    "archivo-OFL.txt": "text/plain",
}

# Each open stream holds a server thread for its whole life, and there are only
# a handful. Retiring a stream periodically lets EventSource reconnect (it does
# so on its own) and reclaims threads left behind by a sleeping phone, so a
# handful of stale tabs can't lock everyone out of the control API.
SSE_MAX_SECONDS = 300.0


def slot_from_name(name: str) -> int | None:
    m = LEADING_NUM.match(Path(name).stem)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= config.SLOTS else None


def _json_str(body, field: str):
    """The named field from a JSON object, or None if the request didn't supply
    a usable one.

    A client can send any JSON at all — a bare array, or the right key with the
    wrong type. Without this, `{"name": 42}` reaches .strip() and `{"hash": 42}`
    reaches the hash regex, and both surface as a 500 rather than the 400 or 404
    the caller deserves.
    """
    if not isinstance(body, dict):
        return None
    value = body.get(field)
    return value if isinstance(value, str) else None


def _json_name(body) -> "tuple[Optional[str], Optional[tuple]]":
    """A trimmed, length-checked name, or the error response to return.

    Same rules as a track rename, because a folder in the tree and a track in a
    row sit next to each other and there is no reason one may be longer.
    """
    name = (_json_str(body, "name") or "").strip()
    if not name:
        return None, (jsonify(error="name must not be empty"), 400)
    if len(name) > MAX_NAME_LEN:
        return None, (jsonify(
            error=f"name must be {MAX_NAME_LEN} characters or fewer"), 400)
    return name, None


def _json_folder_ref(body, field: str) -> "tuple[bool, Optional[int]]":
    """(supplied, value) for a field naming a folder, or null for the top level.

    Absent and null mean different things — leave it where it is, or move it to
    the top level — so a caller cannot express one with the other, and the
    caller of this cannot collapse them either.
    """
    if not isinstance(body, dict) or field not in body:
        return False, None
    v = body[field]
    if v is None:
        return True, None
    # bool is an int subclass, and True would silently mean folder 1.
    return (True, v) if isinstance(v, int) and not isinstance(v, bool) else (False, None)


def _form_folder():
    """The optional folder_id form field on the three ingest routes.

    Returns (folder_id, error_response). An unknown folder fails the whole
    request rather than each file in it: every file would fail identically, and
    the client's tree is stale, which is one problem and not N. 404 rather than
    400 to match POST /api/slots/<n>/assign, where naming a track that does not
    exist is already a 404.
    """
    raw = request.form.get("folder_id")
    if raw is None or raw == "":
        return None, None
    try:
        n = int(raw)
    except ValueError:
        return None, (jsonify(error="folder_id must be a folder id"), 400)
    if db.folder_get(n) is None:
        return None, (jsonify(error="no such folder"), 404)
    return n, None


def _start_slot(raw) -> "tuple[bool, Optional[int]]":
    """(ok, value) for the optional start slot on a folder assign.

    Absent means "wherever there is room", which is not the same as a value the
    caller got wrong, so a junk start is refused rather than quietly treated as
    absent. Range is checked in the service, which owns config.SLOTS.

    An empty query string is the one thing read as absent, and only because the
    caller of this collapses it first: a cleared first-slot field renders
    `?start=`, and that means "wherever there is room". An empty string in a
    JSON body is junk and is refused.
    """
    if raw is None:
        return True, None
    if isinstance(raw, bool):          # bool is an int subclass; True is not 1
        return False, None
    if isinstance(raw, int):
        return True, raw
    if isinstance(raw, str):
        try:
            return True, int(raw)
        except ValueError:
            return False, None
    return False, None


class IngestError(NamedTuple):
    """Why one file could not be taken, and what that means over HTTP.

    The distinction matters: a file ffprobe cannot read is the client's problem
    (400), a card that will not accept the bytes is ours (500). A batch reports
    either per file and still returns 201; a single-file route needs the code.
    """
    message: str
    status: int


def _ingest(f, name: str, store):
    """Save an upload to a temp file and hand it to `store`.

    The single place the three ingest routes spill a file to disk, so the
    ownership rule lives once: on success `store` owns the temp path — it moves
    it into sources/ or unlinks it there — and we must not touch it again.

    Returns (row, IngestError|None). ShuttingDown is deliberately *not* caught:
    the device is halting, no further file can land, and a batch has to stop and
    report what already did rather than mark every remaining file failed.
    """
    fd, tmp = tempfile.mkstemp(dir=str(config.DATA),
                               suffix=Path(name).suffix.lower())
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        f.save(str(tmp_path))
        return (store(tmp_path, Path(name).stem), None)
    except ValueError as e:
        # Not audio, or ffprobe could not read it. The client's problem.
        tmp_path.unlink(missing_ok=True)
        return (None, IngestError(str(e), 400))
    except OSError as e:
        # The card is full, or the bytes could not be confirmed — _store_source
        # raises rather than acknowledge an upload it cannot vouch for. This is
        # the expected failure on this device, and in a batch it must not throw
        # away the files that already landed.
        tmp_path.unlink(missing_ok=True)
        log.warning("could not store %s: %s", name, e)
        return (None, IngestError(f"could not be saved: {e}", 500))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _file_row(row, folder_id):
    """File a freshly ingested track, if the request named a folder.

    Done after the ingest rather than inside it so the three routes and the
    service keep the shapes they already have: a slot upload returns a slot row
    and a library upload returns a library row, and both carry the hash.

    Returns the row as it now stands, re-read rather than assumed. The folder
    was checked before the file was taken, but a forced delete elsewhere can
    commit while the upload is being written, and then the filing quietly does
    nothing. Reporting the requested folder in that case would be the response
    describing a state the database is not in.
    """
    if folder_id is None or not row:
        return row
    h = row.get("source_hash")
    if not h:
        return row
    db.library_set_folder(h, folder_id)
    stored = db.library_get(h)
    return dict(row, folder_id=stored["folder_id"]) if stored else row


def create_app(service: Service) -> Flask:
    app = Flask(__name__, static_folder=None)
    # Shared request-size cap for both upload endpoints; Flask returns 413 when
    # a body exceeds it, before it can fill the data partition.
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES

    @app.errorhandler(ShuttingDown)
    def _shutting_down(e):
        """503, not 400: the request was fine, the device just isn't taking
        work any more. A client that retries next session is doing the right
        thing."""
        return jsonify(error=str(e)), 503

    @app.before_request
    def block_cross_site():
        """There is no auth, so any page the phone happens to visit could
        otherwise POST to the pedal. Same-origin and header-less clients
        (curl, the test suite) are unaffected."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "same-site", "none"):
            return jsonify(error="cross-site request rejected"), 403
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="cross-site request rejected"), 403
        return None

    def _no_cache(resp):
        """"Revalidate every time", not "don't cache".

        send_from_directory sets a strong ETag, so an unchanged asset still
        costs one conditional request and a 304 with no body — nothing on a LAN.
        What it buys is that an over-the-air update can never leave a browser
        running yesterday's app.js against today's API. max-age/immutable would
        need content-hashed filenames, which would need a build step.
        """
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    def index():
        return _no_cache(send_from_directory(STATIC, "index.html"))

    @app.get("/static/<name>")
    def asset(name: str):
        """An allowlist rather than a directory route: static_folder=None keeps
        Flask from adding its own /static rule, and naming the files means a
        stray file in the directory is never reachable."""
        mime = ASSETS.get(name)
        if mime is None:
            abort(404)
        return _no_cache(send_from_directory(STATIC, name, mimetype=mime))

    @app.get("/api/state")
    def state():
        return jsonify(service.snapshot())

    @app.get("/api/trash")
    def trash():
        return jsonify(db.trash_items())

    @app.post("/api/slots/<int:slot>")
    def upload(slot: int):
        if "file" not in request.files:
            return jsonify(error="no file"), 400
        f = request.files["file"]
        name = f.filename or "track"
        if Path(name).suffix.lower() not in config.AUDIO_SUFFIXES:
            return jsonify(error=f"{Path(name).suffix} is not an audio file"), 400

        folder, ferr = _form_folder()
        if ferr:
            return ferr

        row, err = _ingest(f, name, lambda p, stem: service.upload(slot, p, stem))
        if err:
            return jsonify(error=err.message), err.status
        row = _file_row(row, folder)
        return jsonify(row), 201

    @app.post("/api/upload")
    def upload_auto():
        """Multi-file drop.

        With `start`, files land in consecutive slots from there — the user
        pointed at a slot, so that instruction wins over filename numbering.
        Without it, files with a leading number self-assign and the rest fill
        the lowest free slots.
        """
        files = request.files.getlist("file")
        if not files:
            return jsonify(error="no files"), 400

        start = request.form.get("start", type=int)
        if start is not None and not (1 <= start <= config.SLOTS):
            return jsonify(error=f"start must be 1-{config.SLOTS}"), 400

        # Both targeting fields are checked before any file is taken, so a
        # request aimed at somewhere that does not exist lands nothing at all
        # rather than half a batch.
        folder, ferr = _form_folder()
        if ferr:
            return ferr

        # A slot holding a recorded LOOP.WAV counts as taken for the purposes
        # of auto-assignment. An explicitly targeted slot may still be used —
        # the two files coexist — but we don't put a backing track under
        # someone's performance by accident.
        reserved = {s["slot"] for s in db.all_slots()}
        if pedal.mounted():
            # service.has_loop reads the cache built once per mount. Calling
            # pedal.has_loop here instead would stat 99 directories over a
            # ~1 MB/s USB link on every upload request — the exact cost the
            # cache exists to avoid.
            reserved |= {n for n in range(1, config.SLOTS + 1)
                         if service.has_loop(n)}
        results, errors = [], []
        planned = []
        if start is not None:
            for i, f in enumerate(files):
                n = start + i
                name = f.filename or "track"
                if n > config.SLOTS:
                    # Explicit target overflowed. Say so rather than quietly
                    # relocating the file somewhere the user didn't ask for.
                    errors.append({"name": name,
                                   "error": f"no room past slot {config.SLOTS}"})
                else:
                    planned.append((n, f, name))
        else:
            for f in files:
                name = f.filename or "track"
                # Reject the suffix here, before a numbered non-audio file
                # (e.g. "07 notes.txt") can reserve slot 7 and block it for
                # the rest of the batch. It still gets its per-file error below.
                if Path(name).suffix.lower() not in config.AUDIO_SUFFIXES:
                    planned.append((None, f, name))
                    continue
                n = slot_from_name(name)
                if n is not None and n not in reserved:
                    reserved.add(n)
                    planned.append((n, f, name))
                else:
                    planned.append((None, f, name))

        def next_free():
            for i in range(1, config.SLOTS + 1):
                if i not in reserved:
                    reserved.add(i)
                    return i
            return None

        for n, f, name in planned:
            # Rejected before slot resolution: a file we won't accept must not
            # consume a free slot on its way out.
            if Path(name).suffix.lower() not in config.AUDIO_SUFFIXES:
                errors.append({"name": name, "error": "not an audio file"})
                continue
            if n is None:
                n = next_free()
            if n is None:
                errors.append({"name": name, "error": "no free slots"})
                continue
            try:
                row, err = _ingest(f, name,
                                   lambda p, stem, n=n: service.upload(n, p, stem))
            except ShuttingDown as e:
                # Nothing further can land. Report what did rather than losing
                # the whole batch — those files are already committed and queued.
                errors.append({"name": name, "error": str(e)})
                return jsonify(added=results, errors=errors), 503
            if err:
                errors.append({"name": name, "error": err.message})
            else:
                results.append(_file_row(row, folder))

        return jsonify(added=results, errors=errors), 201

    @app.delete("/api/slots/<int:slot>")
    def clear(slot: int):
        try:
            trash_id = service.clear(slot)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, trash_id=trash_id)

    @app.get("/api/loops/<int:slot>")
    def download_loop(slot: int):
        """Stage the slot's LOOP.WAV off the pedal, stream it as a download,
        then purge the staged copy. A GET is fine: reading a loop changes
        nothing on the pedal (download never deletes), and the Pi copy is
        transient. No cross-site guard because it is a safe method.
        """
        try:
            service._check_slot(slot)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if not pedal.mounted():
            return jsonify(error="no pedal connected"), 503
        if not service.has_loop(slot):
            return jsonify(error="no loop in that slot"), 404

        stage = service.stage_loop(slot)
        if not stage.done.wait(config.LOOP_STAGE_TIMEOUT):
            # Give up. cancel_and_reap settles the handoff under a lock: if the
            # worker already published a file we purge it here; otherwise the
            # worker sees the cancel and purges its own. Either way, no orphan.
            orphan = stage.cancel_and_reap()
            if orphan:
                Path(orphan).unlink(missing_ok=True)
            return jsonify(error="timed out staging loop"), 503

        if stage.error:
            err = stage.error
            code = 404 if err == "no loop" else (503 if err == "no pedal" else 500)
            return jsonify(error=err), code

        path = stage.path
        released = threading.Event()

        def purge():
            # Idempotent: the generator's finally and the response close hook
            # both fire. The staged copy is disposable once streamed — the pedal
            # still holds the original. (send_file's own response would only run
            # call_on_close on an explicit Response.close(), which no WSGI server
            # calls, so we stream the file ourselves and purge in a finally —
            # the same belt-and-suspenders shape as the SSE endpoint below.)
            if not released.is_set():
                released.set()
                Path(path).unlink(missing_ok=True)

        @stream_with_context
        def gen():
            try:
                # The staged file may be unlinked while this fd is open; on
                # POSIX the bytes remain readable until the fd closes, so the
                # download always completes.
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(1 << 19)
                        if not chunk:
                            break
                        yield chunk
            finally:
                purge()

        try:
            size = os.path.getsize(path)
        except OSError:
            purge()
            return jsonify(error="loop staging vanished"), 500

        resp = Response(gen(), mimetype="audio/wav",
                        headers={
                            "Content-Length": str(size),
                            "Content-Disposition":
                                f'attachment; filename="loop-{slot:02d}.wav"',
                        })
        resp.call_on_close(purge)
        return resp

    @app.delete("/api/loops/<int:slot>")
    def remove_loop(slot: int):
        """Delete a slot's loop from the pedal. State-changing, so the
        block_cross_site guard covers it automatically."""
        try:
            ok = service.delete_loop(slot)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if not ok:
            return jsonify(error="no loop in that slot"), 404
        return jsonify(ok=True)

    # ------------------------------------------------------------- library

    @app.get("/api/library")
    def library():
        """Every track on the device, newest first. Searching and sorting happen
        in the browser: a few hundred rows is a small response, and the snapshot
        that rides the SSE stream must not grow to carry this."""
        return jsonify(db.library_all())

    @app.post("/api/library")
    def library_add():
        """Ingest files without assigning slots. The pedal holds about twelve
        tracks; the library holds as many as the card does."""
        files = request.files.getlist("file")
        if not files:
            return jsonify(error="no files"), 400
        folder, ferr = _form_folder()
        if ferr:
            return ferr
        added, errors = [], []
        for f in files:
            name = f.filename or "track"
            suffix = Path(name).suffix.lower()
            if suffix not in config.AUDIO_SUFFIXES:
                errors.append({"name": name, "error": "not an audio file"})
                continue
            try:
                row, err = _ingest(f, name, service.add_to_library)
            except ShuttingDown as e:
                errors.append({"name": name, "error": str(e)})
                return jsonify(added=added, errors=errors), 503
            if err:
                errors.append({"name": name, "error": err.message})
            else:
                added.append(_file_row(row, folder))
        return jsonify(added=added, errors=errors), 201

    @app.patch("/api/library/<h>")
    def library_edit(h: str):
        """Rename a track, file it in a folder, or both in one call.

        Extended rather than given a second route: folder_id is a column of the
        row this already edits and returns, and the hash validation and the
        cross-site guard are already here. Doing both at once is also what a
        "new folder from these tracks" gesture wants.
        """
        if not HASH_RE.match(h):
            return jsonify(error="not found"), 404
        body = request.get_json(silent=True)
        has_name = isinstance(body, dict) and "name" in body
        supplied, folder = _json_folder_ref(body, "folder_id")
        if isinstance(body, dict) and "folder_id" in body and not supplied:
            return jsonify(error="folder_id must be a folder id or null"), 400
        if not has_name and not supplied:
            return jsonify(error="nothing to change"), 400
        if db.library_get(h) is None:
            return jsonify(error="not found"), 404
        # Checked before anything is written, so a bad folder cannot leave the
        # rename applied and the filing not.
        if folder is not None and db.folder_get(folder) is None:
            return jsonify(error="no such folder"), 404

        if has_name:
            name, err = _json_name(body)
            if err:
                return err
            if service.rename(h, name) is None:
                return jsonify(error="not found"), 404
        if supplied:
            db.library_set_folder(h, folder)
        return jsonify(db.library_get(h))

    @app.delete("/api/library/<h>")
    def library_forget(h: str):
        """Delete a track and, with it, the only copy of its audio.

        Refuses while a slot still holds it, naming the slots, unless the caller
        confirms with ?force — in which case those slots are cleared first.
        """
        if not HASH_RE.match(h):
            return jsonify(error="not found"), 404
        force = request.args.get("force") is not None
        outcome, slots = service.forget(h, force=force)
        if outcome == "in_use":
            return jsonify(error="in use", slots=slots), 409
        if outcome == "missing":
            # The row had already gone, but any slots pointing at it were still
            # cleared — report them, or the caller cannot tell that the pedal
            # assignments changed underneath it.
            return jsonify(error="not found", cleared=slots), 404
        return jsonify(ok=True, cleared=slots)

    # -- folders ---------------------------------------------------------
    #
    # These talk to db directly rather than through the service. There is no
    # device I/O to admit and nothing to queue, and folders do not ride the
    # state snapshot, so calling library_changed() would rebuild and broadcast a
    # full snapshot that says nothing new. They also keep working while the
    # device is shutting down, for the same reason a rename does: one row
    # changes and the pedal is never touched.

    @app.get("/api/folders")
    def folders():
        """Every folder, flat. The client builds the tree and folds the counts.

        No counts or durations here on purpose. The browser already holds every
        track with its duration and its folder, so summing a subtree is one pass
        with no round trip, and it stays right when the search box filters the
        rows. Aggregating here would be a recursive CTE per render on a Pi Zero,
        and a second source of truth for "16 tracks, 7 folders" that can
        disagree with what is actually on screen.
        """
        return jsonify(db.folders_all())

    @app.post("/api/folders")
    def folder_create():
        body = request.get_json(silent=True)
        name, err = _json_name(body)
        if err:
            return err
        supplied, parent = _json_folder_ref(body, "parent_id")
        if isinstance(body, dict) and "parent_id" in body and not supplied:
            return jsonify(error="parent_id must be a folder id or null"), 400
        if parent is not None and db.folder_get(parent) is None:
            return jsonify(error="no such folder"), 404
        row = db.folder_add(name, parent)
        if row is None:
            # folder_add re-checks the parent under its own lock, so a None here
            # with a parent that existed a moment ago is the depth limit.
            return jsonify(
                error=f"folders may nest {config.MAX_FOLDER_DEPTH} deep"), 400
        return jsonify(row), 201

    @app.patch("/api/folders/<int:folder_id>")
    def folder_update(folder_id: int):
        body = request.get_json(silent=True)
        has_name = isinstance(body, dict) and "name" in body
        supplied, parent = _json_folder_ref(body, "parent_id")
        if isinstance(body, dict) and "parent_id" in body and not supplied:
            return jsonify(error="parent_id must be a folder id or null"), 400
        if not has_name and not supplied:
            return jsonify(error="nothing to change"), 400
        name = None
        if has_name:
            name, err = _json_name(body)
            if err:
                return err

        # One call, so a rejected move cannot leave the rename applied. Doing
        # them in sequence answered 404 for {"name": "x", "parent_id": 999}
        # with the folder already renamed.
        outcome = db.folder_edit(folder_id, name=name, parent_id=parent,
                                 move=supplied)
        if outcome == "unknown":
            return jsonify(error="no such folder"), 404
        if outcome == "cycle":
            return jsonify(
                error="a folder cannot be moved into its own subtree"), 400
        if outcome == "too deep":
            return jsonify(
                error=f"folders may nest {config.MAX_FOLDER_DEPTH} deep"), 400
        return jsonify(db.folder_get(folder_id))

    @app.delete("/api/folders/<int:folder_id>")
    def folder_remove(folder_id: int):
        """Remove a folder. Never removes a track.

        A library row is the only thing keeping its audio alive, so this is a
        grouping being dissolved, not a container being emptied. `?force`
        promotes the contents to the folder's own parent; without it a folder
        holding anything is refused so the client can say what would move.
        """
        force = "force" in request.args
        r = db.folder_delete(folder_id, force=force)
        if r is None:
            return jsonify(error="no such folder"), 404
        if not r["deleted"]:
            return jsonify(error="not empty", folders=r["folders"],
                           tracks=r["tracks"]), 409
        return jsonify(ok=True, to=r["to"],
                       promoted={"folders": r["folders"], "tracks": r["tracks"]})

    @app.get("/api/folders/<int:folder_id>/assign")
    def folder_assign_preview(folder_id: int):
        """What POSTing this would do, without doing it.

        The button reads its own label off this — "Assign 09-17", or "No room" —
        so the range on screen and the range written come from one function.
        Safe method, so no cross-site guard, and nothing is queued.
        """
        ok, start = _start_slot(request.args.get("start") or None)
        if not ok:
            return jsonify(error="start must be a slot number"), 400
        try:
            plan = service.plan_folder(folder_id, start)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if plan is None:
            return jsonify(error="no such folder"), 404
        return jsonify(dict(plan, dry_run=True))

    @app.post("/api/folders/<int:folder_id>/assign")
    def folder_assign(folder_id: int):
        """Put a folder's tracks on the pedal, in tree order, from `start`.

        One call rather than N calls to /api/slots/<n>/assign: the loop set it
        skips is the device's own and is only correct under the lock that queues
        the work, the whole fill is one admission so a shutdown cannot take half
        of it, and it broadcasts one snapshot instead of one per track.

        201 with the plan that was executed, as the preview would have returned
        it. Read the body — like the batch upload, a 201 does not mean every
        track landed; `unplaced` names the ones that did not fit.
        """
        body = request.get_json(silent=True)
        raw = body.get("start") if isinstance(body, dict) else None
        ok, start = _start_slot(raw)
        if not ok:
            return jsonify(error="start must be a slot number"), 400
        try:
            plan = service.assign_folder(folder_id, start)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if plan is None:
            return jsonify(error="no such folder"), 404
        return jsonify(dict(plan, dry_run=False)), 201

    @app.get("/api/library/<h>/audio")
    def library_audio(h: str):
        """Stream an original upload so the browser can audition it.

        Range-capable, so an <audio> element can seek; send_file with
        conditional=True is what implements Range/If-Range/206/416, and unlike
        the loop endpoint there is nothing transient to purge afterwards, so
        there is no reason to hand-roll the streaming.

        Nothing on this device plays audio. This is bytes to the browser.
        """
        if not HASH_RE.match(h) or not db.hash_in_library(h):
            abort(404)
        path = service._source_for(h)
        # The library row is the gate: bytes can outlive a delete by up to one
        # collector pass, and shouldn't stay reachable in the meantime.
        if path is None or path.parent != config.SOURCES:
            abort(404)
        resp = send_file(path, conditional=True, as_attachment=False,
                         mimetype=AUDIO_MIME.get(path.suffix.lower(),
                                                 "application/octet-stream"))
        # sources/ is content-addressed, so the bytes behind this URL can never
        # change. Caching them makes seeking free after the first pass.
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.post("/api/slots/<int:slot>/assign")
    def assign(slot: int):
        """Put a library track into a slot without uploading it again."""
        h = _json_str(request.get_json(silent=True), "hash") or ""
        if not HASH_RE.match(h):
            return jsonify(error="not found"), 404
        try:
            row = service.assign(slot, h)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if row is None:
            return jsonify(error="not found"), 404
        return jsonify(row), 201

    # ---------------------------------------------------------------- slots

    @app.post("/api/slots/<int:slot>/move")
    def move(slot: int):
        body = request.get_json(silent=True) or {}
        try:
            service.move(slot, int(body.get("to", 0)))
        except (TypeError, ValueError) as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    @app.post("/api/slots/<int:slot>/retry")
    def retry(slot: int):
        try:
            service.retry(slot)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    @app.post("/api/trash/<int:trash_id>/restore")
    def restore(trash_id: int):
        slot = service.restore(trash_id)
        if slot is None:
            return jsonify(error="not found"), 404
        return jsonify(slot=slot)

    @app.post("/api/session/end")
    def end():
        service.end_session()
        return jsonify(ok=True)

    @app.post("/api/update")
    def update():
        """Pull the latest code and restart. State-changing, so the
        block_cross_site guard covers it. Returns 200 with the deployed revision
        on success; 409 if the device is busy; 503 if it's shutting down; 502 if
        the update itself failed (no network, no git checkout, bad code that was
        rolled back, restart not permitted)."""
        ok, message = service.update()
        if ok:
            return jsonify(ok=True, revision=message)
        if "busy" in message or "already running" in message:
            code = 409
        elif "shutting down" in message:
            code = 503
        else:
            code = 502
        return jsonify(error=message), code

    @app.post("/api/update/check")
    def update_check():
        """Check the remote for a newer version, on demand. State-changing (it
        fetches and may flip update_available), so the block_cross_site guard
        covers it. Always 200 with the result; the SSE stream also carries any
        change. `ok` is false with a reason when the check couldn't run (no
        deployment, offline)."""
        return jsonify(service.check_now())

    @app.get("/api/events")
    def events():
        q = service.subscribe()
        released = threading.Event()

        def release():
            # Idempotent: the generator's finally and the response close hook
            # both fire, and a client that disconnects before the generator
            # starts only gets the latter.
            if not released.is_set():
                released.set()
                service.unsubscribe(q)

        @stream_with_context
        def gen():
            deadline = time.monotonic() + SSE_MAX_SECONDS
            try:
                # The queue is subscribed before this snapshot is taken, so it
                # can already hold frames built earlier — and _emit builds a
                # snapshot before it takes the subscriber lock, so even later
                # arrivals can carry an older sequence. Send strictly
                # increasing frames and drop the rest, or a stale one would
                # land after the initial frame and roll the UI backwards.
                first = service.snapshot()
                last_seq = first.get("seq", 0)
                yield f"data: {json.dumps(first)}\n\n"
                while time.monotonic() < deadline:
                    try:
                        snap = q.get(timeout=15)
                        if snap.get("seq", 0) <= last_seq:
                            continue
                        last_seq = snap["seq"]
                        yield f"data: {json.dumps(snap)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                release()

        resp = Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})
        resp.call_on_close(release)
        return resp

    return app
