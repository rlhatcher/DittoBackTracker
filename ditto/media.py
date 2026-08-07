"""ffprobe / ffmpeg. All audio work happens in ffmpeg, never in Python."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from . import config

BYTES_PER_SAMPLE = {"pcm_u8": 1, "pcm_s16le": 2, "pcm_s24le": 3,
                    "pcm_s32le": 4, "pcm_f32le": 4}

PCM_CODECS = set(BYTES_PER_SAMPLE)


class ConvertError(Exception):
    pass


@dataclass(frozen=True)
class AudioInfo:
    codec: str
    sample_rate: int
    channels: int
    duration: float


def bytes_per_second(spec: Dict) -> int:
    width = BYTES_PER_SAMPLE.get(spec["codec"], 2)
    return spec["sample_rate"] * spec["channels"] * width


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


def staged_path(source_hash: str, spec: Dict) -> Path:
    """Content-addressed, keyed on the target format too, so a format change
    invalidates the cache rather than silently reusing a wrong-format WAV."""
    tag = f"{spec['codec']}-{spec['sample_rate']}-{spec['channels']}"
    return config.STAGED / f"{source_hash}-{tag}.wav"


def convert(src: Path, source_hash: str, spec: Dict,
            progress=None) -> Path:
    """Transcode to the pedal's exact format. Returns the staged WAV path.

    Both metadata flags matter: -map_metadata -1 alone still leaves ffmpeg
    writing a LIST/INFO chunk with its own version string. -f wav is needed
    because the .part suffix defeats format inference.
    """
    dest = staged_path(source_hash, spec)
    if dest.exists() and dest.stat().st_size > 44:
        return dest

    tmp = dest.with_suffix(".wav.part")
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-loglevel", "error", "-progress", "pipe:1", "-nostats",
        "-i", str(src),
        "-vn",
        "-ar", str(spec["sample_rate"]),
        "-ac", str(spec["channels"]),
        "-c:a", spec["codec"],
        "-map_metadata", "-1",
        "-fflags", "+bitexact",
        "-f", "wav",
        str(tmp),
    ]

    total = 0.0
    info = probe(src)
    if info:
        total = info.duration

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if progress and total > 0 and line.startswith("out_time_us="):
                try:
                    secs = int(line.split("=", 1)[1]) / 1_000_000
                    progress(min(secs / total, 1.0))
                except ValueError:
                    pass
        proc.wait(timeout=600)
    except Exception:
        proc.kill()
        tmp.unlink(missing_ok=True)
        raise

    if proc.returncode != 0:
        err = (proc.stderr.read() or "").strip().splitlines()
        tmp.unlink(missing_ok=True)
        raise ConvertError(err[-1] if err else f"ffmpeg exited {proc.returncode}")

    tmp.replace(dest)
    return dest
