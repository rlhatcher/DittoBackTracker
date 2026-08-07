"""Flask routes and SSE. Single user on a trusted LAN — no auth."""

from __future__ import annotations

import json
import queue
import re
import tempfile
from pathlib import Path

from flask import (Flask, Response, jsonify, request,
                   send_from_directory, stream_with_context)

from . import config, db, pedal
from .core import Service

STATIC = Path(__file__).parent / "static"
LEADING_NUM = re.compile(r"^\D*?0*(\d{1,2})(?:\D|$)")


def slot_from_name(name: str) -> int | None:
    m = LEADING_NUM.match(Path(name).stem)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= config.SLOTS else None


def create_app(service: Service) -> Flask:
    app = Flask(__name__, static_folder=None)

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
        import os
        os.close(fd)
        tmp_path = Path(tmp)
        f.save(str(tmp_path))
        try:
            row = service.upload(slot, tmp_path, Path(name).stem)
        except ValueError as e:
            return jsonify(error=str(e)), 400
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
        taken = {s["slot"] for s in db.all_slots()}
        reserved = set(taken)
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
                n = slot_from_name(name)
                if n is not None and n not in taken:
                    taken.add(n)
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

        import os
        for n, f, name in planned:
            if n is None:
                n = next_free()
            if n is None:
                errors.append({"name": name, "error": "no free slots"})
                continue
            if Path(name).suffix.lower() not in config.AUDIO_SUFFIXES:
                errors.append({"name": name, "error": "not an audio file"})
                continue
            fd, tmp = tempfile.mkstemp(dir=str(config.DATA),
                                       suffix=Path(name).suffix.lower())
            os.close(fd)
            tmp_path = Path(tmp)
            f.save(str(tmp_path))
            try:
                results.append(service.upload(n, tmp_path, Path(name).stem))
            except ValueError as e:
                errors.append({"name": name, "error": str(e)})

        return jsonify(added=results, errors=errors), 201

    @app.delete("/api/slots/<int:slot>")
    def clear(slot: int):
        service.clear(slot)
        return jsonify(ok=True)

    @app.post("/api/slots/<int:slot>/move")
    def move(slot: int):
        body = request.get_json(silent=True) or {}
        try:
            service.move(slot, int(body.get("to", 0)))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    @app.post("/api/slots/<int:slot>/retry")
    def retry(slot: int):
        service.retry(slot)
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

    @app.get("/api/events")
    def events():
        q = service.subscribe()

        @stream_with_context
        def gen():
            try:
                yield f"data: {json.dumps(service.snapshot())}\n\n"
                while True:
                    try:
                        snap = q.get(timeout=15)
                        yield f"data: {json.dumps(snap)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                service.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    return app
