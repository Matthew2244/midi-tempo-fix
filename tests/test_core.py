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
    assert lines[0] == "d.mid: needs fixing. 1 problem: two tempos on one tick."
    assert lines[1].startswith("3 tempo events")


# ---------------------------------------------------------------- notes and the refinements of 1.1


def on(t, key=60, vel=100, ch=0):
    return (t, bytes([0x90 | ch, key, vel]))


def off(t, key=60, ch=0):
    return (t, bytes([0x80 | ch, key, 0]))


def note_file(tmp_path, name, note_events):
    return write(tmp_path, name, smf([track([(0, tempo(100))]), track(note_events)]))


def test_duplicate_notes_are_merged_keeping_the_longest(tmp_path):
    p = note_file(tmp_path, "dup.mid", [on(0), on(0, vel=80), off(PPQ), off(2 * PPQ), on(4 * PPQ), off(5 * PPQ)])
    r = analyze(read_midi(p))
    assert r.notes.duplicates == 1 and r.needs_fix
    out, said = fix_file(p)
    fixed = read_midi(out)
    ons = [e for e in fixed.tracks[1] if e.is_note_on]
    assert [(e.tick, e.raw[2]) for e in ons] == [(0, 100), (4 * PPQ, 100)]  # first velocity kept
    offs = [e.tick for e in fixed.tracks[1] if (e.raw[0] & 0xF0) == 0x80]
    assert offs[0] == 2 * PPQ  # as long as the longer duplicate
    assert "duplicate" in said[0]


def test_overlapping_notes_end_where_the_key_is_struck_again(tmp_path):
    p = note_file(tmp_path, "ovl.mid", [on(0), on(PPQ // 2, vel=90), off(PPQ), off(2 * PPQ)])
    assert analyze(read_midi(p)).notes.overlaps == 1
    out, _ = fix_file(p)
    ev = [(e.tick, e.raw[0] & 0xF0) for e in read_midi(out).tracks[1] if e.raw[0] < 0xF0]
    # first note ends at PPQ/2 (off before the re-strike), second note keeps the later release
    assert ev == [(0, 0x90), (PPQ // 2, 0x80), (PPQ // 2, 0x90), (PPQ, 0x80)] or ev[1:3] == [(PPQ // 2, 0x80), (PPQ // 2, 0x90)]
    assert not analyze(read_midi(out)).needs_fix


def test_stuck_note_ends_with_the_track_and_stray_release_goes(tmp_path):
    p = note_file(tmp_path, "stuck.mid", [off(0, key=50), on(0), on(PPQ, key=64), off(2 * PPQ, key=64), (8 * PPQ, text("end"))])
    r = analyze(read_midi(p))
    assert r.notes.stuck == 1 and r.notes.stray_offs == 1
    out, said = fix_file(p)
    fixed = read_midi(out)
    assert not analyze(fixed).needs_fix
    offs = [e.tick for e in fixed.tracks[1] if (e.raw[0] & 0xF0) == 0x80 and e.raw[1] == 60]
    assert offs == [8 * PPQ]
    assert "stuck" in said[0] and "stray" in said[0]


def test_zero_length_notes_are_reported_but_kept(tmp_path):
    p = note_file(tmp_path, "zero.mid", [on(0), off(0), on(PPQ, key=62), off(PPQ, key=62)])
    r = analyze(read_midi(p), "zero.mid")
    assert r.notes.zero_length == 2 and not r.needs_fix
    lines = describe(r)
    assert lines[0] == "zero.mid: clean."
    assert any(line.startswith("Left alone: 2 notes with no length") for line in lines)


def test_zero_length_note_survives_a_rebuild_in_order(tmp_path):
    # a duplicate forces the track to be rebuilt; the zero-length note must stay on-then-off
    p = note_file(tmp_path, "zr.mid", [on(0), on(0), off(PPQ), off(PPQ), on(2 * PPQ, key=70), off(2 * PPQ, key=70)])
    out, _ = fix_file(p)
    r = analyze(read_midi(out))
    assert r.notes.zero_length == 1 and r.notes.stuck == 0 and r.notes.stray_offs == 0


def test_same_tempo_twice_is_not_a_problem(tmp_path):
    p = write(tmp_path, "same.mid", smf([track([(0, tempo(100)), (0, tempo(100))]), track(notes())]))
    assert not analyze(read_midi(p)).needs_fix


def test_time_signatures_copied_to_every_track_are_fine(tmp_path):
    sig = [(0, timesig(3, 2))]
    p = write(tmp_path, "sigs.mid", smf([track([(0, tempo(100))] + sig), track(notes() + sig), track(sig)]))
    assert analyze(read_midi(p)).misplaced == 0


def test_time_signature_only_on_a_later_track_moves(tmp_path):
    p = write(tmp_path, "sig2.mid", smf([track([(0, tempo(100))]), track(notes() + [(4 * PPQ, timesig(6, 3))])]))
    assert analyze(read_midi(p)).misplaced == 1
    out, _ = fix_file(p)
    assert analyze(read_midi(out)).misplaced == 0


def test_tempo_only_leaves_notes_alone(tmp_path):
    p = write(tmp_path, "t.mid", smf([track([(0, tempo(100)), (PPQ, tempo(90)), (PPQ, tempo(95))]), track([on(0), on(0), off(PPQ), off(PPQ)])]))
    out, said = fix_file(p, notes=False)
    r = analyze(read_midi(out))
    assert not r.doubled and r.notes.duplicates == 1


def test_rmi_files_are_read_and_saved_as_mid(tmp_path):
    inner = smf([track([(0, tempo(100)), (PPQ, tempo(90)), (PPQ, tempo(91))]), track(notes())])
    data = inner + (b"\0" if len(inner) % 2 else b"")
    riff = b"RIFF" + struct.pack("<I", 4 + 8 + len(data)) + b"RMID" + b"data" + struct.pack("<I", len(inner)) + data
    p = write(tmp_path, "song.rmi", riff)
    assert analyze(read_midi(p)).doubled
    out, _ = fix_file(p)
    assert out.endswith("song (tempo fixed).mid")
    assert open(out, "rb").read()[:4] == b"MThd"


def test_empty_file_says_so(tmp_path):
    p = write(tmp_path, "e.mid", b"")
    with pytest.raises(MidiError, match="empty"):
        read_midi(p)


def test_fuzz_random_broken_files(tmp_path):
    """Random tempo maps and notes full of problems: never crash, always verify, nothing left after."""
    import random

    rng = random.Random(20261011)
    for i in range(300):
        tm = [(0, tempo(rng.uniform(40, 200)))]
        for _ in range(rng.randint(0, 12)):
            t = rng.randrange(0, 64) * rng.choice([PPQ, PPQ // 8, 1])
            tm.append((t, tempo(rng.uniform(30, 250))))
            if rng.random() < 0.3:
                tm.append((t, tempo(rng.uniform(30, 250))))  # doubled
            if rng.random() < 0.2:
                tm.append((t, text("REAPER gradual tempo change start 90.000000")))
            if rng.random() < 0.2:
                tm.append((t, timesig(rng.choice([3, 4, 5, 7]), rng.choice([2, 3]))))
        ev = []
        for _ in range(rng.randint(0, 80)):
            t, key, ch = rng.randrange(0, 64 * PPQ, 30), rng.randint(36, 84), rng.choice([0, 0, 9])
            ln = rng.choice([0, 1, PPQ // 4, PPQ, 3 * PPQ])
            ev.append(on(t, key, rng.randint(1, 127), ch))
            if rng.random() < 0.9:
                ev.append(off(t + ln, key, ch))
            if rng.random() < 0.05:
                ev.append(off(rng.randrange(0, 64 * PPQ), rng.randint(36, 84), ch))  # stray
        late = [(rng.randrange(0, 32 * PPQ), tempo(rng.uniform(60, 180)))] if rng.random() < 0.2 else []
        tracks = [track(tm), track(ev + late)]
        if rng.random() < 0.3:  # score-app style: tempo copies on more tracks, some disagreeing
            copies = [(t, raw) for t, raw in tm if raw[1] in (0x51, 0x58) and rng.random() < 0.7]
            copies += [(t, tempo(rng.uniform(40, 200))) for t, raw in tm if raw[1] == 0x51 and rng.random() < 0.2]
            tracks.append(track(copies or [(0, tempo(100))]))
        p = write(tmp_path, f"f{i}.mid", smf(tracks))
        out, _ = fix_file(p)
        if out:
            again = analyze(read_midi(out))
            assert not again.needs_fix, f"case {i} still needs fixing"
            assert fix_file(out, str(tmp_path / f"g{i}.mid"))[0] is None


# ---------------------------------------------------------------- 1.2: comparing, SMPTE

from midi_tempo.core import compare_with  # noqa: E402


def smf_ppq(tracks_events, ppq, fmt=1):
    """Like smf(), at any resolution: events are given in beats."""
    out = []
    for evs in tracks_events:
        out.append(track([(round(b * ppq), raw) for b, raw in evs]))
    return b"MThd" + struct.pack(">IHHH", 6, fmt, len(out), ppq) + b"".join(out)


def song(shift_beat=None):
    tm = [(0, tempo(90)), (8, tempo(120)), (16, tempo(72))]
    ns = []
    for i in range(32):
        b = i + (0.5 if shift_beat == i else 0)
        ns += [(b, bytes([0x90, 60 + i % 12, 100])), (b + 0.5, bytes([0x80, 60 + i % 12, 0]))]
    return [tm, ns]


def test_compare_two_midi_files_at_different_resolutions(tmp_path):
    a = write(tmp_path, "a.mid", smf_ppq(song(), 960))
    b = write(tmp_path, "b.mid", smf_ppq(song(), 480))  # what Logic would save
    lines = compare_with(read_midi(a), b)
    assert lines[0].startswith("Tempo matches b.mid")
    assert "Same 32 notes in both." in lines
    assert lines[-1] == "Every note starts at exactly the same moment."


def test_compare_finds_a_moved_note_and_a_changed_tempo(tmp_path):
    a = write(tmp_path, "a.mid", smf_ppq(song(), 960))
    moved = write(tmp_path, "m.mid", smf_ppq(song(shift_beat=20), 960))
    lines = compare_with(read_midi(a), moved)
    assert any("Notes move by up to" in x for x in lines)
    other = song()
    other[0] = [(0, tempo(90)), (8, tempo(121)), (16, tempo(72))]
    t = write(tmp_path, "t.mid", smf_ppq(other, 960))
    assert compare_with(read_midi(a), t)[0].startswith("Tempo does NOT match")


def test_compare_different_versions_says_so(tmp_path):
    a = write(tmp_path, "a.mid", smf_ppq(song(), 960))
    fewer = song()
    fewer[1] = fewer[1][:20]
    b = write(tmp_path, "b.mid", smf_ppq(fewer, 960))
    lines = compare_with(read_midi(a), b)
    assert any(x.startswith("Different notes: 32 notes here, 10 in b.mid") for x in lines)
    assert not any("move by" in x for x in lines)


def test_smpte_file_is_read_and_its_notes_checked(tmp_path):
    body = vlq(0) + bytes([0x90, 60, 100]) + vlq(0) + bytes([0x90, 60, 100]) + vlq(40) + bytes([0x80, 60, 0])
    body += vlq(0) + bytes([0x80, 60, 0]) + vlq(0) + b"\xff\x2f\x00"
    div = struct.pack(">b", -25) + bytes([40])  # 25 frames a second, 40 ticks a frame
    p = write(tmp_path, "smpte.mid", b"MThd" + struct.pack(">IHH", 6, 0, 1) + div + b"MTrk" + struct.pack(">I", len(body)) + body)
    r = analyze(read_midi(p), "smpte.mid")
    assert r.smpte == 25 and r.notes.duplicates == 1
    assert describe(r)[0] == "smpte.mid: needs fixing. 1 problem: duplicate notes."
    assert "video frames" in describe(r)[1]
    out, _ = fix_file(p)
    assert open(out, "rb").read()[12:14] == div  # the timing header is kept
    assert not analyze(read_midi(out)).needs_fix


def test_summary_line():
    from midi_tempo.core import summary
    assert summary(12, 3, 1) == "Checked 12 files: 3 need fixing, 8 clean, 1 could not be read."
    assert summary(1, 1) == "Checked 1 file: 1 needs fixing, 0 clean."


def test_drum_overlaps_are_not_a_problem_but_drum_duplicates_are(tmp_path):
    drums = [on(0, 42, ch=9), on(PPQ // 4, 42, ch=9), off(PPQ, 42, ch=9), off(PPQ, 42, ch=9)]
    p = note_file(tmp_path, "drums.mid", drums)
    r = analyze(read_midi(p))
    assert r.notes.overlaps == 0 and not r.needs_fix
    p2 = note_file(tmp_path, "drums2.mid", [on(0, 36, ch=9), on(0, 36, 90, ch=9), off(10, 36, ch=9), off(10, 36, ch=9)])
    assert analyze(read_midi(p2)).notes.duplicates == 1


def test_identical_tempo_copies_on_every_track_become_one(tmp_path):
    copies = [(0, tempo(72)), (8 * PPQ, tempo(96)), (0, timesig(3, 2))]
    p = write(tmp_path, "lily.mid", smf([track([(0, text("Title", 3))]), track(notes() + copies), track(copies)]))
    assert analyze(read_midi(p)).misplaced == 6
    out, _ = fix_file(p)
    fixed = read_midi(out)
    assert [e.bpm() for e in fixed.tracks[0] if e.meta_type == 0x51] == pytest.approx([72, 96], abs=1e-3)
    assert sum(1 for e in fixed.tracks[0] if e.meta_type == 0x58) == 1
    assert all(e.meta_type not in (0x51, 0x58) for t in fixed.tracks[1:] for e in t)


def test_merging_copies_keeps_the_tempo_that_wins(tmp_path):
    # Found by training on a LilyPond hymn: tracks disagree on one tick (120, 60, then 120 again).
    # The last one read wins (120); merging copies must not hand the win to 60.
    t = 8 * PPQ
    p = write(tmp_path, "conflict.mid", smf([
        track([(0, text("Hymn", 3))]),
        track(notes() + [(0, tempo(120)), (t, tempo(120)), (t, tempo(60))]),
        track([(0, tempo(120)), (t, tempo(120))]),
    ]))
    before = analyze(read_midi(p))
    out, said = fix_file(p)  # verifies note timing itself; raises if the winner changed
    after = read_midi(out)
    assert [round(e.bpm()) for e in after.tracks[0] if e.meta_type == 0x51 and e.tick == t][-1] == 120
    assert before.length_seconds == pytest.approx(analyze(after).length_seconds, abs=1e-9)


def test_json_output(tmp_path, capsys):
    import json as _json
    from midi_tempo.cli import main
    p = write(tmp_path, "j.mid", smf([track([(0, tempo(100)), (PPQ, tempo(90)), (PPQ, tempo(95))]), track(notes())]))
    assert main(["check", p, "--json"]) == 1
    data = _json.loads(capsys.readouterr().out)
    f = data["files"][0]
    assert data["need_fixing"] == 1 and f["problems"] == ["two tempos on one tick"] and not f["clean"]
    assert f["doubled_tempos"][0]["winning_bpm"] == pytest.approx(95, abs=0.01)
