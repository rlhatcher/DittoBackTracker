"""ffprobe and ffmpeg. All audio work happens in ffmpeg, never in Python."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config

_CONVERT_TIMEOUT = 600.0


class ConvertError(Exception):
    pass


@dataclass(frozen=True)
class AudioInfo:
    codec: str
    sample_rate: int
    channels: int
    duration: float


def probe(path: Path) -> Optional[AudioInfo]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "a:0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return None
        s = streams[0]
        return AudioInfo(
            codec=s.get("codec_name", "?"),
            sample_rate=int(s.get("sample_rate", 0) or 0),
            channels=int(s.get("channels", 0) or 0),
            duration=float(s.get("duration", 0) or 0),
        )
    except (subprocess.SubprocessError, ValueError, KeyError):
        return None


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:20]


def staged_path(source_hash: str) -> Path:
    return config.STAGED / f"{source_hash}.wav"


def convert(src: Path, source_hash: str, progress=None,
            duration: float = 0.0) -> Path:
    """Transcode to the pedal's format. Returns the staged WAV path.

    -map_metadata -1 and -fflags +bitexact together strip the tags; either one
    alone leaves ffmpeg writing a LIST/INFO chunk with its version string. -f
    wav because the .part suffix defeats format inference. `duration` scales
    the progress fraction; passing it saves an ffprobe.
    """
    dest = staged_path(source_hash)
    if dest.exists() and dest.stat().st_size > 44:
        return dest

    tmp = dest.with_suffix(".wav.part")
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-loglevel", "error", "-progress", "pipe:1", "-nostats",
        "-i", str(src),
        "-vn",
        "-ar", str(config.SAMPLE_RATE),
        "-ac", str(config.CHANNELS),
        "-c:a", config.CODEC,
        "-map_metadata", "-1",
        "-fflags", "+bitexact",
        "-f", "wav",
        str(tmp),
    ]

    total = duration
    if total <= 0:
        info = probe(src)
        if info:
            total = info.duration

    # stderr to a file: a chatty ffmpeg filling a pipe would deadlock the
    # progress loop, which only reads stdout.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                errors="replace") as errf:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE,
                              stderr=errf, text=True) as proc:
            # The read blocks until ffmpeg writes, so a silent stall never
            # reaches proc.wait's timeout; the watchdog ends it instead.
            watchdog = threading.Timer(_CONVERT_TIMEOUT, proc.kill)
            watchdog.start()
            try:
                for line in proc.stdout:
                    if progress and total > 0 and line.startswith("out_time_us="):
                        try:
                            secs = int(line.split("=", 1)[1]) / 1_000_000
                            progress(min(secs / total, 1.0))
                        except ValueError:
                            pass
                proc.wait(timeout=_CONVERT_TIMEOUT)
            except Exception:
                proc.kill()
                proc.wait()
                tmp.unlink(missing_ok=True)
                raise
            finally:
                watchdog.cancel()

        if proc.returncode != 0:
            errf.seek(0)
            err = errf.read().strip().splitlines()
            tmp.unlink(missing_ok=True)
            raise ConvertError(
                err[-1] if err else f"ffmpeg exited {proc.returncode}")

    # Sync before the rename, so a power cut cannot leave a complete-looking
    # name over truncated bytes.
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    tmp.replace(dest)
    return dest
