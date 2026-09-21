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

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    request,
    send_file,
    send_from_directory,
    stream_with_context,
)

from . import config, db
from .core import Service

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
LEADING_NUM = re.compile(r"^\D*?0*(\d{1,2})(?:\D|$)")

# media.file_hash is 20 lowercase hex chars. A whitelist, because source_for
# globs sources/{h}.* and a `*` in the hash would match another file.
HASH_RE = re.compile(r"\A[0-9a-f]{20}\Z")

# Content types for auditioning an upload. Opus is always in an Ogg container.
AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma",
    ".opus": "audio/ogg",
}
assert set(AUDIO_MIME) == config.AUDIO_SUFFIXES, \
    "AUDIO_MIME and config.AUDIO_SUFFIXES have drifted apart"

MAX_NAME_LEN = 200

# Servable assets, by exact name. Archivo is served from here rather than a
# CDN: the device is often the only thing on its network, and a render-blocking
# request to a host it cannot reach stalls first paint until DNS gives up.
ASSETS = {
    "app.js": "text/javascript",
    "app.css": "text/css",
    "archivo-latin.woff2": "font/woff2",
    "archivo-OFL.txt": "text/plain",
}

# Each open stream holds a server thread. Retiring streams lets EventSource
# reconnect on its own and reclaims threads left by a sleeping phone.
SSE_MAX_SECONDS = 300.0


def slot_from_name(name: str) -> int | None:
    m = LEADING_NUM.match(Path(name).stem)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= config.SLOTS else None


def _json_str(body, field: str):
    """The named string field, or None: a client can send any JSON at all."""
    if not isinstance(body, dict):
        return None
    value = body.get(field)
    return value if isinstance(value, str) else None


class ApiError(Exception):
    """A refusal a request helper raises; the handler turns it into JSON."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _json_name(body) -> str:
    name = (_json_str(body, "name") or "").strip()
    if not name:
        raise ApiError(400, "name must not be empty")
    if len(name) > MAX_NAME_LEN:
        raise ApiError(400, f"name must be {MAX_NAME_LEN} characters or fewer")
    return name


def _require_hash(h: str) -> None:
    if not HASH_RE.match(h):
        raise ApiError(404, "not found")


def _is_audio(name: str) -> bool:
    return Path(name).suffix.lower() in config.AUDIO_SUFFIXES


def _start_slot(raw) -> Optional[int]:
    """The optional start slot on a batch assign. Absent means "wherever there
    is room"; a value the caller got wrong is refused, not treated as absent."""
    if raw is None:
        return None
    if isinstance(raw, bool):          # bool is an int subclass
        raise ApiError(400, "start must be a slot number")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            raise ApiError(400, "start must be a slot number") from None
    raise ApiError(400, "start must be a slot number")


class IngestError(NamedTuple):
    """Why one file could not be taken: a file ffprobe cannot read is the
    client's problem (400), a card that will not take the bytes is ours (500)."""
    message: str
    status: int


def _ingest(f, name: str, store):
    """Save an upload to a temp file and hand it to `store`, which then owns
    the path. Returns (row, IngestError|None)."""
    fd, tmp = tempfile.mkstemp(dir=str(config.DATA),
                               suffix=Path(name).suffix.lower())
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        f.save(str(tmp_path))
        return (store(tmp_path, Path(name).stem), None)
    except ValueError as e:
        tmp_path.unlink(missing_ok=True)
        return (None, IngestError(str(e), 400))
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        log.warning("could not store %s: %s", name, e)
        return (None, IngestError(f"could not be saved: {e}", 500))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def create_app(service: Service) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES

    @app.errorhandler(ApiError)
    def _api_error(e):
        return jsonify(error=e.message), e.status

    @app.before_request
    def block_cross_site():
        """No auth, so a page on another origin must not be able to POST
        through the user's browser. Header-less clients are unaffected."""
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
        """Revalidate every time, so an update never leaves a browser running
        yesterday's app.js against today's API. An unchanged asset is a 304."""
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    def index():
        return _no_cache(send_from_directory(STATIC, "index.html"))

    @app.get("/static/<name>")
    def asset(name: str):
        mime = ASSETS.get(name)
        if mime is None:
            abort(404)
        return _no_cache(send_from_directory(STATIC, name, mimetype=mime))

    @app.get("/api/state")
    def state():
        return jsonify(service.snapshot())

    @app.post("/api/slots/<int:slot>")
    def upload(slot: int):
        if "file" not in request.files:
            return jsonify(error="no file"), 400
        f = request.files["file"]
        name = f.filename or "track"
        if not _is_audio(name):
            return jsonify(error=f"{Path(name).suffix} is not an audio file"), 400
        row, err = _ingest(f, name, lambda p, stem: service.upload(slot, p, stem))
        if err:
            return jsonify(error=err.message), err.status
        return jsonify(row), 201

    @app.post("/api/upload")
    def upload_auto():
        """Multi-file drop. With `start`, consecutive slots from there.
        Without it, a leading number in the name picks the slot and the rest
        take the lowest free ones."""
        files = request.files.getlist("file")
        if not files:
            return jsonify(error="no files"), 400
        start = request.form.get("start", type=int)
        if start is not None and not (1 <= start <= config.SLOTS):
            return jsonify(error=f"start must be 1-{config.SLOTS}"), 400

        # A slot holding a loop counts as taken for automatic placement, so a
        # backing track never lands under a recording by accident.
        reserved = {s["slot"] for s in db.all_slots()}
        if service.mounted:
            reserved |= {n for n in range(1, config.SLOTS + 1)
                         if service.has_loop(n)}
        results, errors = [], []
        # Numbered files claim their slots first, so an unnumbered file earlier
        # in the batch cannot take a number a later one asked for.
        planned = []
        if start is not None:
            for i, f in enumerate(files):
                n = start + i
                name = f.filename or "track"
                if n > config.SLOTS:
                    errors.append({"name": name,
                                   "error": f"no room past slot {config.SLOTS}"})
                else:
                    planned.append((n, f, name))
        else:
            for f in files:
                name = f.filename or "track"
                n = slot_from_name(name) if _is_audio(name) else None
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
            if not _is_audio(name):
                errors.append({"name": name, "error": "not an audio file"})
                continue
            if n is None:
                n = next_free()
            if n is None:
                errors.append({"name": name, "error": "no free slots"})
                continue
            row, err = _ingest(f, name,
                               lambda p, stem, n=n: service.upload(n, p, stem))
            if err:
                errors.append({"name": name, "error": err.message})
            else:
                results.append(row)
        return jsonify(added=results, errors=errors), 201

    @app.delete("/api/slots/<int:slot>")
    def clear(slot: int):
        try:
            service.clear(slot)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    @app.get("/api/loops/<int:slot>")
    def download_loop(slot: int):
        """Stream the slot's LOOP.WAV straight off the mounted pedal. A read,
        so nothing on the pedal changes and no cross-site guard applies."""
        try:
            service.check_slot(slot)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if not service.mounted:
            return jsonify(error="no pedal connected"), 503
        if not service.has_loop(slot):
            return jsonify(error="no loop in that slot"), 404
        try:
            return send_file(service.loop_path(slot), mimetype="audio/wav",
                             as_attachment=True,
                             download_name=f"loop-{slot:02d}.wav")
        except FileNotFoundError:
            return jsonify(error="no loop in that slot"), 404

    @app.delete("/api/loops/<int:slot>")
    def remove_loop(slot: int):
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
        return jsonify(db.library_all())

    @app.post("/api/library")
    def library_add():
        """Ingest files without assigning slots."""
        files = request.files.getlist("file")
        if not files:
            return jsonify(error="no files"), 400
        added, errors = [], []
        for f in files:
            name = f.filename or "track"
            if not _is_audio(name):
                errors.append({"name": name, "error": "not an audio file"})
                continue
            row, err = _ingest(f, name, service.add_to_library)
            if err:
                errors.append({"name": name, "error": err.message})
            else:
                added.append(row)
        return jsonify(added=added, errors=errors), 201

    @app.patch("/api/library/<h>")
    def library_edit(h: str):
        _require_hash(h)
        row = service.rename(h, _json_name(request.get_json(silent=True)))
        if row is None:
            return jsonify(error="not found"), 404
        return jsonify(row)

    @app.delete("/api/library/<h>")
    def library_forget(h: str):
        """Delete a track and its audio. Refuses while a slot holds it, naming
        the slots, unless ?force clears them first."""
        _require_hash(h)
        force = request.args.get("force") is not None
        outcome, slots = service.forget(h, force=force)
        if outcome == "in_use":
            return jsonify(error="in use", slots=slots), 409
        if outcome == "missing":
            return jsonify(error="not found", cleared=slots), 404
        return jsonify(ok=True, cleared=slots)

    @app.get("/api/library/<h>/audio")
    def library_audio(h: str):
        """Stream an original upload so the browser can audition it. Range
        requests come from send_file, so the player can seek."""
        # abort(404), not JSON: this feeds an <audio> element.
        if not HASH_RE.match(h) or not db.hash_in_library(h):
            abort(404)
        path = service.source_for(h)
        if path is None or path.parent != config.SOURCES:
            abort(404)
        resp = send_file(path, conditional=True, as_attachment=False,
                         mimetype=AUDIO_MIME.get(path.suffix.lower(),
                                                 "application/octet-stream"))
        # Content-addressed, so the bytes behind this URL never change.
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.post("/api/slots/assign")
    def assign_many():
        """Put library tracks on the pedal in one call, in the order given.
        One call rather than N so the fill is one locked step with one
        snapshot, using the device's own loop set."""
        body = request.get_json(silent=True)
        hashes = body.get("hashes") if isinstance(body, dict) else None
        if (not isinstance(hashes, list) or not hashes
                or not all(isinstance(h, str) for h in hashes)):
            return jsonify(error="hashes must be a list of library hashes"), 400
        for h in hashes:
            _require_hash(h)
        start = _start_slot(body.get("start"))
        try:
            plan = service.assign_tracks(hashes, start)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if plan is None:
            return jsonify(error="not found"), 404
        return jsonify(plan), 201

    @app.post("/api/slots/<int:slot>/assign")
    def assign(slot: int):
        h = _json_str(request.get_json(silent=True), "hash") or ""
        _require_hash(h)
        try:
            row = service.assign(slot, h)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if row is None:
            return jsonify(error="not found"), 404
        return jsonify(row), 201

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

    @app.post("/api/update")
    def update():
        """200 with the deployed revision; 409 busy; 502 the update failed."""
        ok, message = service.update()
        if ok:
            return jsonify(ok=True, revision=message)
        if "busy" in message or "already running" in message:
            code = 409
        else:
            code = 502
        return jsonify(error=message), code

    @app.post("/api/update/check")
    def update_check():
        return jsonify(service.check_now())

    @app.get("/api/events")
    def events():
        q = service.subscribe()
        released = threading.Event()

        def release():
            # Both the generator's finally and the close hook fire.
            if not released.is_set():
                released.set()
                service.unsubscribe(q)

        @stream_with_context
        def gen():
            deadline = time.monotonic() + SSE_MAX_SECONDS
            try:
                # Frames queued before the first snapshot can be older than it;
                # send strictly increasing sequence numbers only.
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
