"""media.py, the ffprobe/ffmpeg boundary. Neither binary runs here: every
crossing is stubbed, so this runs on a machine without them."""

import hashlib
import json
import subprocess

import pytest

from ditto import config, media, web

# --- file_hash -------------------------------------------------------------

def test_the_hash_is_the_first_20_chars_of_sha256(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"some audio bytes")
    assert media.file_hash(p) == hashlib.sha256(b"some audio bytes").hexdigest()[:20]


def test_the_hash_is_shaped_the_way_the_routes_demand(tmp_path):
    """web.HASH_RE gates every library route. If these drift, the routes 404
    on their own files."""
    p = tmp_path / "a.bin"
    p.write_bytes(b"\xff\xfb" + b"\x00" * 5000)
    assert web.HASH_RE.match(media.file_hash(p))


def test_a_file_larger_than_the_read_buffer_hashes_correctly(tmp_path):
    body = b"x" * ((1 << 20) + 12345)
    p = tmp_path / "big.bin"
    p.write_bytes(body)
    assert media.file_hash(p) == hashlib.sha256(body).hexdigest()[:20]


# --- probe -----------------------------------------------------------------

def _ffprobe(monkeypatch, returncode=0, payload=None):
    out = json.dumps(payload if payload is not None else {})

    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, returncode, out, "")

    monkeypatch.setattr(media.subprocess, "run", fake)


def test_probe_reads_the_first_audio_stream(monkeypatch, tmp_path):
    _ffprobe(monkeypatch, payload={"streams": [
        {"codec_name": "mp3", "sample_rate": "44100", "channels": 2,
         "duration": "212.5"}]})
    info = media.probe(tmp_path / "x.mp3")
    assert (info.codec, info.sample_rate, info.channels, info.duration) == \
        ("mp3", 44100, 2, 212.5)


def test_probe_returns_none_when_there_is_no_audio_stream(monkeypatch,
                                                          tmp_path):
    _ffprobe(monkeypatch, payload={"streams": []})
    assert media.probe(tmp_path / "x.txt") is None


def test_probe_returns_none_when_ffprobe_fails(monkeypatch, tmp_path):
    _ffprobe(monkeypatch, returncode=1)
    assert media.probe(tmp_path / "x.mp3") is None


def test_probe_survives_unparseable_output(monkeypatch, tmp_path):
    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "not json at all", "")

    monkeypatch.setattr(media.subprocess, "run", fake)
    assert media.probe(tmp_path / "x.mp3") is None


def test_probe_tolerates_missing_fields(monkeypatch, tmp_path):
    _ffprobe(monkeypatch, payload={"streams": [{"codec_name": "mp3"}]})
    info = media.probe(tmp_path / "x.mp3")
    assert info.duration == 0 and info.sample_rate == 0


# --- convert's cache -------------------------------------------------------

def test_convert_reuses_a_staged_file_without_running_ffmpeg(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(config, "STAGED", tmp_path)
    dest = media.staged_path("a" * 20)
    dest.write_bytes(b"\x00" * 100)          # bigger than a bare WAV header

    def explode(*a, **k):
        raise AssertionError("ffmpeg ran despite a usable staged file")

    monkeypatch.setattr(media.subprocess, "Popen", explode)
    assert media.convert(tmp_path / "in.mp3", "a" * 20) == dest


def test_a_truncated_staged_file_is_not_treated_as_a_cache_hit(tmp_path,
                                                               monkeypatch):
    """Header-only means an interrupted write, not a converted track."""
    monkeypatch.setattr(config, "STAGED", tmp_path)
    media.staged_path("a" * 20).write_bytes(b"\x00" * 44)
    ran = []

    def fake_popen(cmd, **kw):
        ran.append(cmd)
        raise RuntimeError("stop here — reaching ffmpeg is the assertion")

    monkeypatch.setattr(media.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        media.convert(tmp_path / "in.mp3", "a" * 20, duration=1.0)
    assert ran, "a truncated file was reused as if it were converted"


# --- the command lines -----------------------------------------------------
#
# Neither binary runs in the suite or in CI, so the arguments are pinned as
# text: not proof they work, proof they have not changed by accident.

def test_the_ffprobe_command_asks_for_exactly_one_audio_stream(monkeypatch,
                                                               tmp_path):
    """Without -select_streams a:0 a video container puts its picture stream
    first, and every field reads off the wrong stream."""
    seen = []

    def fake(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(media.subprocess, "run", fake)
    media.probe(tmp_path / "x.mp3")
    assert seen[0] == [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "a:0", str(tmp_path / "x.mp3"),
    ]


def test_the_ffmpeg_command_writes_the_pedal_format_and_nothing_else(
        monkeypatch, tmp_path):
    """-map_metadata -1 and -fflags +bitexact together strip the tags; -f wav
    because the .part suffix defeats inference; -progress pipe:1 is what the
    progress bar reads; -vn keeps cover art out of the WAV."""
    monkeypatch.setattr(config, "STAGED", tmp_path)
    seen = []

    def fake_popen(cmd, **kw):
        seen.append(cmd)
        raise RuntimeError("stop here — the command line is the assertion")

    monkeypatch.setattr(media.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        media.convert(tmp_path / "in.mp3", "b" * 20, duration=1.0)

    dest = media.staged_path("b" * 20)
    assert seen[0] == [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-loglevel", "error", "-progress", "pipe:1", "-nostats",
        "-i", str(tmp_path / "in.mp3"),
        "-vn",
        "-ar", "44100",
        "-ac", "1",
        "-c:a", "pcm_s24le",
        "-map_metadata", "-1",
        "-fflags", "+bitexact",
        "-f", "wav",
        str(dest.with_suffix(".wav.part")),
    ]


def test_the_pedal_rate_is_132300_bytes_a_second():
    """The figure the capacity gauge is built on: 44.1 kHz, mono, 24-bit."""
    assert config.BYTES_PER_SECOND == 132300
