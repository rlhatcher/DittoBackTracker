"""Flask routes and SSE. Single user on a trusted LAN — no auth."""

from __future__ import annotations

import json
import os
import queue
import re
import tempfile
import threading
import time
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, request, send_file,
                   send_from_directory, stream_with_context)

from . import config, db, pedal
from .core import Service, ShuttingDown

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
ASSETS = {"app.js": "text/javascript", "app.css": "text/css"}

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

        fd, tmp = tempfile.mkstemp(dir=str(config.DATA),
                                   suffix=Path(name).suffix.lower())
        os.close(fd)
        tmp_path = Path(tmp)
        try:
            f.save(str(tmp_path))
            # On success service.upload owns tmp_path — it is moved into
            # sources/ or unlinked there, so we must not touch it again.
            row = service.upload(slot, tmp_path, Path(name).stem)
        except ValueError as e:
            tmp_path.unlink(missing_ok=True)
            return jsonify(error=str(e)), 400
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
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

        # A slot holding a recorded LOOP.WAV counts as taken for the purposes
        # of auto-assignment. An explicitly targeted slot may still be used —
        # the two files coexist — but we don't put a backing track under
        # someone's performance by accident.
        reserved = {s["slot"] for s in db.all_slots()}
        if pedal.mounted():
            reserved |= {n for n in range(1, config.SLOTS + 1) if pedal.has_loop(n)}
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
            fd, tmp = tempfile.mkstemp(dir=str(config.DATA),
                                       suffix=Path(name).suffix.lower())
            os.close(fd)
            tmp_path = Path(tmp)
            try:
                f.save(str(tmp_path))
                results.append(service.upload(n, tmp_path, Path(name).stem))
            except ValueError as e:
                tmp_path.unlink(missing_ok=True)
                errors.append({"name": name, "error": str(e)})
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

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
        added, errors = [], []
        for f in files:
            name = f.filename or "track"
            suffix = Path(name).suffix.lower()
            if suffix not in config.AUDIO_SUFFIXES:
                errors.append({"name": name, "error": "not an audio file"})
                continue
            fd, tmp = tempfile.mkstemp(dir=str(config.DATA), suffix=suffix)
            os.close(fd)
            tmp_path = Path(tmp)
            try:
                f.save(str(tmp_path))
                # On success the service owns tmp_path — it is moved into
                # sources/ or unlinked there, so we must not touch it again.
                added.append(service.add_to_library(tmp_path, Path(name).stem))
            except ValueError as e:
                tmp_path.unlink(missing_ok=True)
                errors.append({"name": name, "error": str(e)})
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
        return jsonify(added=added, errors=errors), 201

    @app.patch("/api/library/<h>")
    def library_rename(h: str):
        if not HASH_RE.match(h):
            return jsonify(error="not found"), 404
        name = (_json_str(request.get_json(silent=True), "name") or "").strip()
        if not name:
            return jsonify(error="name must not be empty"), 400
        if len(name) > MAX_NAME_LEN:
            return jsonify(error=f"name must be {MAX_NAME_LEN} characters or "
                                 f"fewer"), 400
        row = service.rename(h, name)
        if row is None:
            return jsonify(error="not found"), 404
        return jsonify(row)

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
