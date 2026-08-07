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

from flask import (Flask, Response, jsonify, request,
                   send_from_directory, stream_with_context)

from . import config, db, pedal
from .core import Service

STATIC = Path(__file__).parent / "static"
LEADING_NUM = re.compile(r"^\D*?0*(\d{1,2})(?:\D|$)")

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


def create_app(service: Service) -> Flask:
    app = Flask(__name__, static_folder=None)
    # Shared request-size cap for both upload endpoints; Flask returns 413 when
    # a body exceeds it, before it can fill the data partition.
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES

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

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

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
        on success; 409 if the device is busy; 502 if the update itself failed
        (no network, no git checkout, restart not permitted)."""
        ok, message = service.update()
        if ok:
            return jsonify(ok=True, revision=message)
        code = 409 if "busy" in message or "already running" in message else 502
        return jsonify(error=message), code

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
                yield f"data: {json.dumps(service.snapshot())}\n\n"
                while time.monotonic() < deadline:
                    try:
                        snap = q.get(timeout=15)
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
