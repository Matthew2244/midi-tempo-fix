"""REAPER's padded short bars, and checking MIDI against audio. Synthetic files only."""
import math
import random
import re
import struct

import pytest

from midi_tempo.core import (compare_project, compare_with, fix_file, needs_project_fix, project_short_bars,
                             read_midi)

from test_core import PPQ, smf, tempo, timesig, track, write

# A project at 120 BPM in 4/4, with a 3/4 marker at 9 seconds: 18 beats in, halfway
# through bar 5. REAPER plays bar 5 two beats long; its export makes it four.
RPP = """<REAPER_PROJECT 0.1 "7.0"
  TEMPO 120 4 4 0
  <TEMPOENVEX
    ACT 0 -1
    PT 0.000000000000 120.0000000000 1 262148 0 1 0 "" 0 0 0 ABBB
    PT 9.000000000000 120.0000000000 1 262147 0 1 0 "" 0 0 0 ABB
  >
>
"""
NOTE_BEATS = [0, 4, 8, 12, 16, 18, 21, 24, 27]  # 18 is the first beat of the 3/4


def reaper_style_export(pad_beats):
    """What REAPER writes: everything from the 3/4 on pushed late by the padding."""
    ev = [(0, timesig(4, 2)), (0, tempo(120)), (int((18 + pad_beats) * PPQ), timesig(3, 2)),
          (int((18 + pad_beats) * PPQ), tempo(120))]
    notes = []
    for b in NOTE_BEATS:
        t = int((b + (pad_beats if b >= 18 else 0)) * PPQ)
        notes += [(t, bytes([0x90, 60, 100])), (t + PPQ // 2, bytes([0x80, 60, 0]))]
    return smf([track(ev), track(notes)])


def test_short_bar_is_found_in_the_project(tmp_path):
    rpp = write(tmp_path, "song.RPP", RPP.encode())
    bars = project_short_bars(rpp)
    assert len(bars) == 1
    assert bars[0].beat == pytest.approx(18)
    assert bars[0].missing == pytest.approx(2)


def test_padded_export_is_explained_and_fixed_with_the_project(tmp_path):
    rpp = write(tmp_path, "song.RPP", RPP.encode())
    mid = write(tmp_path, "song.mid", reaper_style_export(2))
    lines = compare_project(read_midi(mid), rpp)
    assert "REAPER's export" in lines[0] and "1000 ms late" in lines[1]
    assert needs_project_fix(mid, rpp)
    out, said = fix_file(mid, project=rpp)
    assert out and "checked by reading the copy back" in " ".join(said)
    fixed = read_midi(out)
    starts = sorted(e.tick for t in fixed.tracks for e in t if e.is_note_on)
    assert starts == [b * PPQ for b in NOTE_BEATS]  # back where the project plays them
    assert compare_project(fixed, rpp)[0].startswith("Tempo matches")
    assert not needs_project_fix(out, rpp)


def test_an_export_without_the_padding_is_left_alone(tmp_path):
    rpp = write(tmp_path, "song.RPP", RPP.encode())
    mid = write(tmp_path, "song.mid", reaper_style_export(0))
    assert not needs_project_fix(mid, rpp)
    assert fix_file(mid, project=rpp)[0] is None


# ---------------------------------------------------------------- audio

np = pytest.importorskip("numpy")
RATE = 22050


def melody(seed=1, count=120, bpm=120.0):
    """Random notes on a grid: (beat, key). Random, so the harmony never repeats exactly."""
    rnd = random.Random(seed)
    out, beat = [], 0.0
    for _ in range(count):
        out.append((beat, rnd.choice([48, 50, 52, 53, 55, 57, 59, 60, 62, 64, 65, 67, 69, 71, 72])))
        beat += rnd.choice([0.5, 1, 1, 1.5, 2])
    return out


def melody_midi(notes, bpm=120.0):
    ev = []
    for beat, key in notes:
        t = int(beat * PPQ)
        ev += [(t, bytes([0x90, key, 100])), (t + PPQ // 2, bytes([0x80, key, 0]))]
    return smf([track([(0, tempo(bpm)), (0, timesig(4, 2))]), track(ev)])


def render(notes, seconds_of, length, path):
    """Plucked tones (a few harmonics, fast decay) at the moments seconds_of(beat) says."""
    x = np.zeros(int(length * RATE), dtype=np.float32)
    t = np.arange(int(1.2 * RATE)) / RATE
    env = np.exp(-t * 4) * np.minimum(1, t * 400)
    for beat, key in notes:
        f = 440 * 2 ** ((key - 69) / 12)
        tone = env * sum(np.sin(2 * math.pi * f * h * t) / h for h in (1, 2, 3))
        i = int(seconds_of(beat) * RATE)
        if 0 <= i < len(x):
            n = min(len(tone), len(x) - i)
            x[i:i + n] += tone[:n] * 0.2
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    head = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, RATE,
                                                                                  RATE * 2, 2, 16)
    with open(path, "wb") as f:
        f.write(head + b"data" + struct.pack("<I", len(pcm)) + pcm)
    return str(path)


def audio_case(tmp_path, seconds_of):
    notes = melody()
    mid = write(tmp_path, "m.mid", melody_midi(notes))
    length = seconds_of(notes[-1][0]) + 3
    wav = render(notes, seconds_of, length, tmp_path / "a.wav")
    return " ".join(compare_with(read_midi(mid), wav))


def test_audio_lines_up_exactly(tmp_path):
    said = audio_case(tmp_path, lambda b: b * 0.5)
    assert said.startswith("Lines up"), said


def test_audio_offset_is_measured(tmp_path):
    said = audio_case(tmp_path, lambda b: b * 0.5 + 0.2)
    assert said.startswith("Offset") and "later" in said, said
    assert abs(int(re.search(r"(\d+) ms later", said).group(1)) - 200) <= 10, said  # attacks measure to ~10 ms


def test_audio_at_a_different_tempo_says_so(tmp_path):
    said = audio_case(tmp_path, lambda b: b * 60 / 115)  # recorded at 115 where the MIDI says 120
    assert "slower, about 115 BPM where the MIDI says 120" in said, said
    assert "different tempo maps" in said


def test_audio_with_a_jump_says_where(tmp_path):
    said = audio_case(tmp_path, lambda b: b * 0.5 + (0.25 if b >= 60 else 0))
    assert "time was added or removed" in said, said
    assert abs(int(re.search(r"jumps (\d+) ms", said).group(1)) - 250) <= 15, said
    assert "At 0:30" in said, said  # beat 60 at 120 BPM: where the jump really is


def test_unrelated_audio_is_not_measured(tmp_path):
    notes = melody(seed=1)
    other = melody(seed=99)
    mid = write(tmp_path, "m.mid", melody_midi(notes))
    wav = render(other, lambda b: b * 0.5, other[-1][0] * 0.5 + 3, tmp_path / "a.wav")
    said = " ".join(compare_with(read_midi(mid), wav))
    assert "not seem to be the same music" in said or "Does NOT line up" in said


def test_check_with_the_project_says_needs_fixing(tmp_path, capsys):
    from midi_tempo.cli import main
    rpp = write(tmp_path, "song.RPP", RPP.encode())
    mid = write(tmp_path, "song.mid", reaper_style_export(2))
    assert main(["check", mid, "--compare", rpp]) == 1
    out = capsys.readouterr().out
    assert out.startswith("song.mid: needs fixing.") and "short bar whole" in out
    assert "To fix: midi-tempo fix" in out
