"""media.py — the ffprobe/ffmpeg boundary.

This module had no test file, and two of its properties are load-bearing well
outside it: file_hash has to produce exactly what web.HASH_RE will accept, or
every library route 404s silently, and staged_path's format tag is what stops a
format change reusing a wrong-format WAV.

ffmpeg and ffprobe are not invoked. Where a subprocess would run it is stubbed,
so this stays fast and runs on a machine without either.
"""

import hashlib
import json
import subprocess

import pytest

from ditto import config, media, web


# --- bytes_per_second ------------------------------------------------------

@pytest.mark.parametrize("codec,width", [
    ("pcm_u8", 1), ("pcm_s16le", 2), ("pcm_s24le", 3),
    ("pcm_s32le", 4), ("pcm_f32le", 4),
])
def test_the_rate_follows_the_sample_width(codec, width):
    spec = {"codec": codec, "sample_rate": 44100, "channels": 1}
    assert media.bytes_per_second(spec) == 44100 * width


def test_the_pedal_default_is_132300_bytes_a_second():
    """The figure the capacity gauge and every docs estimate are built on."""
    assert media.bytes_per_second(dict(config.DEFAULT_FORMAT)) == 132300


def test_an_unknown_codec_assumes_two_bytes():
    """A guess, but a documented one — better than dividing by zero when a
    pedal reports a codec this table has never seen."""
    spec = {"codec": "pcm_something_new", "sample_rate": 1000, "channels": 1}
    assert media.bytes_per_second(spec) == 2000


def test_channels_multiply_the_rate():
    mono = {"codec": "pcm_s24le", "sample_rate": 44100, "channels": 1}
    stereo = dict(mono, channels=2)
    assert media.bytes_per_second(stereo) == 2 * media.bytes_per_second(mono)


# --- file_hash -------------------------------------------------------------

def test_the_hash_is_the_first_20_chars_of_sha256(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"some audio bytes")
    assert media.file_hash(p) == hashlib.sha256(b"some audio bytes").hexdigest()[:20]


def test_the_hash_is_shaped_the_way_the_routes_demand(tmp_path):
    """web.HASH_RE gates every library route. If these two ever drift, the
    routes 404 on their own files and nothing says why."""
    p = tmp_path / "a.bin"
    p.write_bytes(b"\xff\xfb" + b"\x00" * 5000)
    assert web.HASH_RE.match(media.file_hash(p))


def test_identical_content_hashes_identically(tmp_path):
    """Content addressing is what makes a re-upload free."""
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    assert media.file_hash(a) == media.file_hash(b)


def test_a_file_larger_than_the_read_buffer_hashes_correctly(tmp_path):
    """It reads in 1 MiB chunks; a single-chunk test would not exercise that."""
    body = b"x" * ((1 << 20) + 12345)
    p = tmp_path / "big.bin"
    p.write_bytes(body)
    assert media.file_hash(p) == hashlib.sha256(body).hexdigest()[:20]


# --- staged_path -----------------------------------------------------------

def test_the_staged_name_carries_the_format(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STAGED", tmp_path)
    spec = {"codec": "pcm_s24le", "sample_rate": 44100, "channels": 1}
    assert media.staged_path("a" * 20, spec).name == \
        f"{'a' * 20}-pcm_s24le-44100-1.wav"


@pytest.mark.parametrize("change", [
    {"codec": "pcm_s16le"}, {"sample_rate": 48000}, {"channels": 2},
])
def test_any_format_change_names_a_different_file(tmp_path, monkeypatch,
                                                  change):
    """The cache key. Without this a pedal reporting a new format would be fed
    a WAV staged for the old one."""
    monkeypatch.setattr(config, "STAGED", tmp_path)
    base = {"codec": "pcm_s24le", "sample_rate": 44100, "channels": 1}
    assert media.staged_path("a" * 20, base) != \
        media.staged_path("a" * 20, dict(base, **change))


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
    """What upload() turns into "not a readable audio file"."""
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
    """A stream that reports no duration must not raise; upload() rejects it
    on the duration check instead."""
    _ffprobe(monkeypatch, payload={"streams": [{"codec_name": "mp3"}]})
    info = media.probe(tmp_path / "x.mp3")
    assert info.duration == 0 and info.sample_rate == 0


# --- convert's cache -------------------------------------------------------

def test_convert_reuses_a_staged_file_without_running_ffmpeg(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(config, "STAGED", tmp_path)
    spec = dict(config.DEFAULT_FORMAT)
    dest = media.staged_path("a" * 20, spec)
    dest.write_bytes(b"\x00" * 100)          # bigger than a bare WAV header

    def explode(*a, **k):
        raise AssertionError("ffmpeg ran despite a usable staged file")

    monkeypatch.setattr(media.subprocess, "Popen", explode)
    assert media.convert(tmp_path / "in.mp3", "a" * 20, spec) == dest


def test_a_truncated_staged_file_is_not_treated_as_a_cache_hit(tmp_path,
                                                               monkeypatch):
    """Header-only means an interrupted write, not a converted track."""
    monkeypatch.setattr(config, "STAGED", tmp_path)
    spec = dict(config.DEFAULT_FORMAT)
    media.staged_path("a" * 20, spec).write_bytes(b"\x00" * 44)
    ran = []

    def fake_popen(cmd, **kw):
        ran.append(cmd)
        raise RuntimeError("stop here — reaching ffmpeg is the assertion")

    monkeypatch.setattr(media.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        media.convert(tmp_path / "in.mp3", "a" * 20, spec, duration=1.0)
    assert ran, "a truncated file was reused as if it were converted"
