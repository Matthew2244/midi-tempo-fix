"""Tests built from synthetic files, so no real music ships with the repo."""
import os
import struct

import pytest

from midi_tempo.core import (
    MidiError,
    _event_seconds,
    analyze,
    describe,
    fix_file,
    read_midi,
    rpp_seconds_at_beat,
)

PPQ = 960


def vlq(v):
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    return bytes(reversed(out))


def tempo(bpm):
    return b"\xff\x51\x03" + round(60e6 / bpm).to_bytes(3, "big")


def text(s, kind=1):
    b = s.encode()
    return bytes([0xFF, kind]) + vlq(len(b)) + b


def timesig(num, den_pow):
    return bytes([0xFF, 0x58, 4, num, den_pow, 24, 8])


def track(events):
    """events: list of (abs_tick, raw bytes)."""
    body, last = bytearray(), 0
    for t, raw in sorted(events, key=lambda e: e[0]) + [(max([e[0] for e in events] + [0]), b"\xff\x2f\x00")]:
        body += vlq(t - last) + raw
        last = t
    return b"MTrk" + struct.pack(">I", len(body)) + body


def smf(tracks, fmt=1):
    return b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), PPQ) + b"".join(tracks)


def notes(n=64, step=PPQ):
    ev = []
    for i in range(n):
        ev += [(i * step, bytes([0x90, 60, 100])), (i * step + step // 2, bytes([0x80, 60, 0]))]
    return ev


def ramp(start_tick, start_bpm, end_bpm, beats, steps_per_beat=8):
    """REAPER-style stepped ramp, linear in time, each step at its average tempo."""
    n = beats * steps_per_beat
    total = beats * 60 * 2 / (start_bpm + end_bpm)  # seconds for a linear-in-time ramp
    k = (end_bpm - start_bpm) / total
    ev, t_prev = [], 0.0
    import math

    def t_at(b):  # seconds into the ramp when b beats have passed
        return (-start_bpm + math.sqrt(start_bpm**2 + 2 * k * 60 * b)) / k

    for i in range(n):
        b0, b1 = i / steps_per_beat, (i + 1) / steps_per_beat
        dt = t_at(b1) - t_at(b0)
        ev.append((start_tick + i * PPQ // steps_per_beat, tempo((b1 - b0) * 60 / dt)))
    return ev


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_clean_file_is_left_alone(tmp_path):
    p = write(tmp_path, "clean.mid", smf([track([(0, tempo(100)), (16 * PPQ, tempo(120))]), track(notes())]))
    r = analyze(read_midi(p), "clean.mid")
    assert not r.needs_fix
    out, said = fix_file(p)
    assert out is None and "nothing to fix" in said[0]
    assert not os.path.exists(p.replace(".mid", " (tempo fixed).mid"))


def test_doubled_tempo_event_is_removed_and_timing_kept(tmp_path):
    tm = [(0, tempo(100)), (32 * PPQ, tempo(84.7442)), (32 * PPQ, tempo(85))]
    p = write(tmp_path, "doubled.mid", smf([track(tm), track(notes())]))
    r = analyze(read_midi(p), "doubled.mid")
    assert len(r.doubled) == 1 and r.doubled[0].winning_bpm == pytest.approx(85, abs=1e-3)
    assert r.doubled_cost_ms > 0
    before = _event_seconds(read_midi(p))
    out, said = fix_file(p)
    fixed = read_midi(out)
    assert not analyze(fixed).needs_fix
    assert _event_seconds(fixed) == pytest.approx(before, abs=1e-9)
    assert "doubled" in said[0]


def test_reaper_ramp_notes_are_removed(tmp_path):
    tm = [(0, tempo(127))] + ramp(8 * PPQ, 127, 90, 6) + [(14 * PPQ, tempo(90)), (14 * PPQ, timesig(7, 3))]
    tm += [(8 * PPQ, text("REAPER gradual tempo change start 127.000000"))]
    tm += [(14 * PPQ - 120, text("REAPER gradual tempo change interpolation end"))]
    p = write(tmp_path, "ramp.mid", smf([track(tm), track(notes())]))
    r = analyze(read_midi(p))
    assert r.hints == 2 and len(r.ramps) == 1
    out, _ = fix_file(p)
    fixed = read_midi(out)
    assert analyze(fixed).hints == 0
    assert len(analyze(fixed).ramps) == 1  # the steps themselves stay
    assert _event_seconds(fixed) == pytest.approx(_event_seconds(read_midi(p)), abs=1e-9)


def test_tempo_on_a_later_track_moves_to_the_first(tmp_path):
    p = write(
        tmp_path,
        "late.mid",
        smf([track([(0, text("Song", 3))]), track(notes() + [(0, tempo(90)), (8 * PPQ, tempo(140))])]),
    )
    r = analyze(read_midi(p))
    assert r.misplaced == 2
    out, said = fix_file(p)
    fixed = read_midi(out)
    assert analyze(fixed).misplaced == 0
    assert [e.bpm() for e in fixed.tracks[0] if e.meta_type == 0x51] == pytest.approx([90, 140], abs=1e-3)
    assert fixed.tracks[0][-1].raw == b"\xff\x2f\x00"
    assert _event_seconds(fixed) == pytest.approx(_event_seconds(read_midi(p)), abs=1e-9)
    assert "first track" in said[0]


def test_original_is_never_changed(tmp_path):
    data = smf([track([(0, tempo(100)), (4 * PPQ, tempo(90)), (4 * PPQ, tempo(91))]), track(notes())])
    p = write(tmp_path, "orig.mid", data)
    fix_file(p)
    assert open(p, "rb").read() == data
    with pytest.raises(MidiError):
        fix_file(p, p)


def test_running_status_and_format_0(tmp_path):
    body = vlq(0) + tempo(100) + vlq(0) + bytes([0x90, 60, 100]) + vlq(PPQ) + bytes([62, 100])
    body += vlq(0) + tempo(90) + vlq(0) + tempo(95) + vlq(PPQ) + bytes([0x80, 60, 0]) + vlq(0) + b"\xff\x2f\x00"
    p = write(tmp_path, "f0.mid", b"MThd" + struct.pack(">IHHH", 6, 0, 1, PPQ) + b"MTrk" + struct.pack(">I", len(body)) + body)
    out, _ = fix_file(p)
    fixed = read_midi(out)
    assert sum(1 for e in fixed.tracks[0] if e.is_note_on) == 2
    assert not analyze(fixed).needs_fix


def test_not_a_midi_file(tmp_path):
    p = write(tmp_path, "x.mid", b"hello")
    with pytest.raises(MidiError):
        read_midi(p)


def test_reaper_ramps_are_linear_in_time():
    # 127 -> 90 over 3.317974 s is exactly 6 beats when the ramp is linear in time
    f = rpp_seconds_at_beat([(0.0, 127.0, 0), (3.317974, 90.0, 1)])
    assert f(6) == pytest.approx(3.317974, abs=1e-5)


def test_describe_reads_as_sentences(tmp_path):
    tm = [(0, tempo(100)), (4 * PPQ, tempo(90)), (4 * PPQ, tempo(91))]
    p = write(tmp_path, "d.mid", smf([track(tm), track(notes())]))
    lines = describe(analyze(read_midi(p), "d.mid"))
    assert lines[0].startswith("d.mid:") and lines[-1] == "Needs fixing."
