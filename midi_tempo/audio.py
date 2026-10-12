"""Check a MIDI file against a recording of the same music: does it line up, and if not, how.

Built after Listening Forward (2026-10-12): a MIDI export and a WAV that were meant
to match drifted seconds apart, and nothing short of listening could say where or
why. This answers in sentences: lines up; a fixed offset; a tempo that differs from
some point on; or a jump at one place (REAPER's padded short bar is one).

How, in two passes:

1. Coarse: harmony over time. The audio's pitch classes (a chromagram) against the
   MIDI's notes, aligned by dynamic time warping, so a recording that wanders from
   the MIDI's tempo is still followed. Harmony survives a full mix around a single
   MIDI part, and does not repeat every beat the way a rhythm does.
2. Fine: note attacks. Around that path, the audio's onsets (spectral flux, 4 ms
   steps) against the MIDI's note starts, in 8-second windows, to the millisecond.

The MIDI can be one part of the recording (a piano part against the full mix).
The recording may start earlier or end later than the MIDI.

Needs numpy (the app bundles it). Reads WAV itself (16, 24 or 32-bit, or float);
other formats go through macOS's afconvert, or ffmpeg where installed.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile

from .core import MidiError, MidiFile, _tempo_list, bar_beat, n, seconds_at, time_signatures

AUDIO_EXTENSIONS = (".wav", ".wave", ".aif", ".aiff", ".aifc", ".mp3", ".m4a", ".aac", ".flac", ".caf", ".ogg",
                    ".opus")

RATE = 22050          # analysis sample rate
FINE_HOP = 88 / RATE  # seconds per onset frame, about 4 ms: a whole number of samples, or every time drifts
LINES_UP_MS = 30      # within this, call it lined up: attack shapes and render latency differ by this much
WINDOW = 8.0          # seconds between fine measurements, times two
FINE_WINDOW = 12.0    # seconds of notes each fine measurement listens to: long enough that a
                      # repeating rhythm can't fit a 16th off as well as on (8 s could, in Fresh Air)


def _np():
    try:
        import numpy
    except ImportError as e:
        raise MidiError("checking against audio needs numpy, which is not installed here "
                        "(pip install numpy). The MIDI Tempo Fix app includes it") from e
    return numpy


# ---------------------------------------------------------------- reading audio


def _read_wav(path: str):
    """Mono float samples and sample rate from a WAV file: 8/16/24/32-bit PCM or 32/64-bit float."""
    np = _np()
    with open(path, "rb") as f:
        b = f.read()
    if b[:4] not in (b"RIFF", b"RF64") or b[8:12] != b"WAVE":
        raise MidiError("it is not a WAV file")
    i, fmt, data = 12, None, None
    while i + 8 <= len(b):
        tag, size = b[i:i + 4], struct.unpack("<I", b[i + 4:i + 8])[0]
        if tag == b"fmt ":
            fmt = b[i + 8:i + 8 + size]
        elif tag == b"data":
            if size == 0xFFFFFFFF or i + 8 + size > len(b):  # RF64, or a size that was never filled in
                size = len(b) - i - 8
            data = b[i + 8:i + 8 + size]
            break
        i += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise MidiError("the WAV file has no audio in it")
    tag, ch, rate = struct.unpack("<HHI", fmt[:8])
    bits = struct.unpack("<H", fmt[14:16])[0]
    if tag == 0xFFFE and len(fmt) >= 26:  # WAVE_FORMAT_EXTENSIBLE: the real format is in the GUID
        tag = struct.unpack("<H", fmt[24:26])[0]
    width = bits // 8
    frames = len(data) // (width * ch)
    data = data[:frames * width * ch]
    if tag == 3 and bits in (32, 64):
        x = np.frombuffer(data, dtype="<f4" if bits == 32 else "<f8").astype(np.float32)
    elif tag == 1 and bits == 8:
        x = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif tag == 1 and bits == 16:
        x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
    elif tag == 1 and bits == 24:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        x = (np.where(v >= 1 << 23, v - (1 << 24), v)).astype(np.float32) / (1 << 23)
    elif tag == 1 and bits == 32:
        x = np.frombuffer(data, dtype="<i4").astype(np.float32) / (1 << 31)
    else:
        raise MidiError(f"this WAV format can't be read ({bits}-bit, format {tag}). Save it as a 24-bit WAV")
    return x.reshape(-1, ch).mean(axis=1), rate


def _convert(path: str) -> str:
    """Another format to a temporary WAV, with whatever converter this computer has."""
    out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    tools = []
    if shutil.which("afconvert"):  # every Mac
        tools.append(["afconvert", "-f", "WAVE", "-d", "LEI16", path, out])
    if shutil.which("ffmpeg"):
        tools.append(["ffmpeg", "-v", "error", "-y", "-i", path, "-ac", "1", "-c:a", "pcm_s16le", out])
    for cmd in tools:
        try:
            if subprocess.run(cmd, capture_output=True, timeout=600).returncode == 0 and os.path.getsize(out) > 44:
                return out
        except (OSError, subprocess.TimeoutExpired):
            pass
    os.remove(out)
    raise MidiError("this computer can't read that audio format. Save it as a WAV and try again")


def read_audio(path: str):
    """Mono samples at RATE."""
    np = _np()
    if path.lower().endswith((".wav", ".wave")):
        x, rate = _read_wav(path)
    else:
        tmp = _convert(path)
        try:
            x, rate = _read_wav(tmp)
        finally:
            os.remove(tmp)
    if len(x) < rate:
        raise MidiError("the audio is shorter than a second")
    if rate != RATE:  # linear interpolation is plenty for pitch classes and attacks
        t = np.arange(int(len(x) * RATE / rate)) * (rate / RATE)
        x = np.interp(t, np.arange(len(x)), x).astype(np.float32)
    return x


# ---------------------------------------------------------------- features


def _frames(x, size: int, hop: int):
    np = _np()
    count = max(0, (len(x) - size) // hop + 1)
    win = np.hanning(size).astype(np.float32)
    for s in range(0, count, 4000):
        idx = np.arange(size)[None, :] + hop * np.arange(s, min(count, s + 4000))[:, None]
        yield np.abs(np.fft.rfft(x[idx] * win, axis=1))


def audio_chroma(x, hop: float):
    """Pitch-class energy per frame, frame centres at k * hop."""
    np = _np()
    size, h = 4096, int(round(hop * RATE))
    pad = np.concatenate([np.zeros(size // 2, np.float32), x, np.zeros(size // 2, np.float32)])
    freqs = np.fft.rfftfreq(size, 1 / RATE)
    keep = (freqs > 50) & (freqs < 2000)
    pc = (np.round(12 * np.log2(freqs[keep] / 440) + 69) % 12).astype(int)  # MIDI key numbering: C is 0
    rows = [np.stack([np.bincount(pc, np.log1p(m[keep]), 12) for m in block]) for block in _frames(pad, size, h)]
    return np.concatenate(rows) if rows else np.zeros((0, 12))


def audio_onsets(x):
    """Spectral flux every FINE_HOP seconds: how much new sound starts in each moment."""
    np = _np()
    size, h = 1024, int(round(FINE_HOP * RATE))  # exact: FINE_HOP is defined in samples
    pad = np.concatenate([np.zeros(size // 2, np.float32), x, np.zeros(size // 2, np.float32)])
    out, prev = [], None
    for m in _frames(pad, size, h):
        lg = np.log1p(100 * m)
        first = lg[:1] if prev is None else prev
        out.append(np.maximum(np.diff(np.vstack([first, lg]), axis=0), 0).sum(axis=1))
        prev = lg[-1:]
    f = np.concatenate(out)
    f = f - np.convolve(f, np.ones(25) / 25, "same")  # keep the attacks, drop slow swells
    # A 46 ms window sees an attack before its centre reaches it: flux peaks 16 ms early,
    # measured on synthetic plucks with attacks from 2.5 to 10 ms (12 ms for a slow 33 ms
    # swell). Shift by that, or every offset reads 16 ms short.
    lead = int(round(0.016 / FINE_HOP))
    f = np.concatenate([np.zeros(lead), f[:len(f) - lead]])
    return np.maximum(f, 0)


def midi_notes(mf: MidiFile):
    """(start seconds, end seconds, key, velocity) for every note, drums left out (no pitch)."""
    tempos = _tempo_list(mf)
    out = []
    for events in mf.tracks:
        on: dict[tuple[int, int], list] = {}
        for e in events:
            if e.raw[0] >= 0xF0:
                continue
            ch, kind = e.raw[0] & 0x0F, e.raw[0] & 0xF0
            if kind == 0x90 and e.raw[2] > 0:
                on.setdefault((ch, e.raw[1]), []).append((e.tick, e.raw[2]))
            elif kind in (0x80, 0x90) and on.get((ch, e.raw[1])):
                t0, vel = on[(ch, e.raw[1])].pop(0)
                out.append((seconds_at(tempos, mf.division, t0), seconds_at(tempos, mf.division, e.tick),
                            e.raw[1], vel, ch))
    return sorted(out)


def midi_chroma(notes, frames: int, hop: float):
    np = _np()
    c = np.zeros((frames, 12))
    for s, e, key, vel, ch in notes:
        if ch == 9:
            continue
        i0 = int(s / hop)
        i1 = min(frames, max(i0 + 1, int(min(e, s + 2.0) / hop) + 1))
        if i0 < frames:
            c[i0:i1, key % 12] += vel / 127 * np.exp(-np.arange(i1 - i0) * hop / 1.0)
    return c


def _normalize(c):
    np = _np()
    c = c - c.mean(axis=1, keepdims=True)
    return c / (np.linalg.norm(c, axis=1, keepdims=True) + 1e-6)


# ---------------------------------------------------------------- alignment


STEP_PENALTY = 0.5    # added to every off-diagonal step: a performance's tempo moves slowly, noise does not


def coarse_path(midi_c, audio_c):
    """For each MIDI frame, the audio frame it matches: subsequence DTW over harmony.

    Steps (1,1), (1,2) and (2,1) let the recording run from half to twice the MIDI's
    speed; the recording may start and end anywhere (subsequence), the MIDI is matched
    from its first frame to its last. Off-diagonal steps cost STEP_PENALTY extra:
    without it the path jittered by whole beats wherever the harmony repeats (measured
    on Listening Forward: +-400 ms swings; with it, steady). Rows are worked one at a
    time over the whole audio, so memory is the backtrack table (one byte a cell).
    """
    np = _np()
    cost = (1 - _normalize(midi_c) @ _normalize(audio_c).T).astype(np.float32)  # MIDI rows x audio columns
    rows, cols = cost.shape
    inf = np.float32(1e18)
    pen = np.float32(STEP_PENALTY)
    back = np.zeros((rows, cols), dtype=np.int8)
    d2 = np.full(cols, inf, dtype=np.float32)
    d1 = cost[0].copy()  # free start anywhere in the audio
    every = np.arange(cols)
    for i in range(1, rows):
        diag = np.concatenate([[inf], d1[:-1]])
        skip_a = np.concatenate([[inf, inf], d1[:-2]]) + pen           # (1,2): audio moves two frames
        skip_m = np.concatenate([[inf], d2[:-1]]) + cost[i - 1] + pen  # (2,1): MIDI moves two frames
        choice = np.stack([diag, skip_a, skip_m])
        k = np.argmin(choice, axis=0)
        back[i] = k
        d2, d1 = d1, choice[k, every] + cost[i]
    j = int(np.argmin(d1))
    path = np.zeros(rows, dtype=np.int64)
    i = rows - 1
    while i > 0:
        path[i] = j
        k = back[i, j]
        if k == 0:
            i, j = i - 1, j - 1
        elif k == 1:
            i, j = i - 1, j - 2
        else:
            path[i - 1] = j - 1
            i, j = i - 2, j - 1
        j = max(j, 0)
    path[0] = j
    # judged only where the MIDI plays: a part that rests for minutes is not a worse match
    voiced = np.abs(midi_c).sum(axis=1) > 1e-9
    on = cost[np.arange(rows), path][voiced] if voiced.any() else cost[np.arange(rows), path]
    quality = float(np.mean(on))
    return path, quality


def _running_median(y, width: int):
    np = _np()
    half = width // 2
    pad = np.concatenate([np.full(half, y[0]), y, np.full(half, y[-1])])
    return np.array([np.median(pad[k:k + width]) for k in range(len(y))])


FINE_WIDE = 0.6       # seconds searched either side of the coarse path for candidate attacks
# Seconds of lag change that cost as much as a weak peak. Rhythm repeats every 8th and 16th,
# so a short passage can fit a 16th off almost as well as on; at 0.03 the path stepped out
# and back by exactly a 16th or an 8th (118 ms at 127 BPM, 291 ms at 103) in three songs.
# A real jump persists, so it still wins.
JUMP_COST = 0.01


def fine_offsets(starts, flux, coarse_lag, wide: float = None, trend=None):
    """[(midi seconds, audio-minus-MIDI seconds, trusted)] every half window, to the millisecond.

    Each window scores every lag near the coarse path by how much attack energy lands
    under the MIDI's note starts. Rhythm repeats, so a window often has several good
    peaks a beat or half a beat apart; picking each window's best on its own swung by
    exactly those amounts. So the windows choose together (Viterbi): a candidate is
    cheap when its peak is strong and when it moves from the last window about as much
    as the coarse path does.
    """
    np = _np()
    wide = wide or FINE_WIDE
    windows = []
    end = starts[-1] if len(starts) else 0
    t = starts[0] if len(starts) else 0
    while t <= end:
        sel = starts[(starts >= t) & (starts < t + FINE_WINDOW)]
        if len(sel) >= 6:
            mid = float(np.median(sel))
            centre = float(coarse_lag(np.array([mid]))[0])
            # the lag itself moves inside a window when the tempos differ (3% is 240 ms over 8 s),
            # so follow the coarse path's slope rather than smearing the attacks
            slope = float(np.clip((coarse_lag(np.array([t + FINE_WINDOW]))[0] - coarse_lag(np.array([t]))[0])
                                  / FINE_WINDOW, -0.5, 0.5))
            level = float(trend(np.array([mid]))[0]) if trend else centre
            # search around both the local path and the long trend: where sparse chords sit half a
            # beat apart the local path can be the wrong one (Fresh Air's last 15 seconds)
            lags = np.arange(min(centre, level) - wide, max(centre, level) + wide + FINE_HOP / 2, FINE_HOP)
            rel = np.round((sel + slope * (sel - mid)) / FINE_HOP).astype(int)
            shift = np.round(lags / FINE_HOP).astype(int)
            score = np.array([flux[np.clip(rel + k, 0, len(flux) - 1)].sum() for k in shift])
            med = float(np.median(score)) + 1e-9
            peaks = [k for k in range(1, len(score) - 1) if score[k] >= score[k - 1] and score[k] > score[k + 1]]
            peaks = sorted(peaks, key=lambda k: -score[k])[:8]
            cands = [(float(lags[k]), score[k] / med) for k in peaks]
            cands.append((level, 1.0))  # nothing clear here: trust the trend
            windows.append((mid, level, cands))
        t += WINDOW / 2
    if not windows:
        return []
    # Viterbi over windows
    cost = [[-np.log(max(r, 1e-3)) for _, r in windows[0][2]]]
    back = [[0] * len(windows[0][2])]
    for w in range(1, len(windows)):
        _, c_prev, prev = windows[w - 1]
        _, c_now, now = windows[w]
        expected = c_now - c_prev  # from the long-smoothed trend: its slope is sound even where its level wobbles
        scale = JUMP_COST + 0.1 * abs(expected)
        row, brow = [], []
        for lag, r in now:
            # moving with the trend is cheap, and so is staying put: a steady lag is the commonest
            # truth, and the trend itself goes wrong where the path does (sparse chords at the end)
            trans = [cost[-1][q] + min(abs((lag - pl) - expected) / scale, abs(lag - pl) / JUMP_COST)
                     for q, (pl, _) in enumerate(prev)]
            q = int(np.argmin(trans))
            row.append(trans[q] - np.log(max(r, 1e-3)))
            brow.append(q)
        cost.append(row)
        back.append(brow)
    k = int(np.argmin(cost[-1]))
    chosen = [0] * len(windows)
    for w in range(len(windows) - 1, -1, -1):
        chosen[w] = k
        k = back[w][k]
    out = []
    for (mid, _, cands), k in zip(windows, chosen):
        lag, r = cands[k]
        out.append((mid, lag, bool(r >= 1.6 and k < len(cands) - 1)))
    return out


# ---------------------------------------------------------------- the report


def _clock(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def _ms(x: float) -> str:
    return f"{abs(x) * 1000:.0f} ms"


def _segments(points, tol: float = 0.025):
    """Split (t, lag) into straight runs, each fitting within tol seconds: split where it fits worst."""
    np = _np()

    def worst(seg):
        if len(seg) < 3:
            return 0.0, None
        t = np.array([p[0] for p in seg]); y = np.array([p[1] for p in seg])
        a, b = np.polyfit(t, y, 1)
        r = np.abs(a * t + b - y)
        k = int(np.argmax(r[1:-1])) + 1
        return float(r.max()), k

    todo, done = [list(points)], []
    while todo:
        seg = todo.pop()
        err, k = worst(seg)
        if err <= tol or k is None:
            done.append(seg)
            continue
        # a jump splits cleanest between two points; a bend at the point itself
        jumps = [abs(seg[i + 1][1] - seg[i][1]) for i in range(len(seg) - 1)]
        cut = int(np.argmax(jumps)) + 1 if max(jumps) > tol else k
        cut = min(max(cut, 1), len(seg) - 1)
        todo += [seg[:cut], seg[cut:]]
    return sorted(done, key=lambda seg: seg[0][0])


def compare_audio(mf: MidiFile, path: str) -> list[str]:
    """Sentences: does this MIDI file line up with this recording, and if not, where and how."""
    np = _np()
    name = os.path.basename(path)
    notes = midi_notes(mf)
    if len(notes) < 8:
        return [f"Too few notes to line up with {name}."]
    x = read_audio(path)
    audio_len = len(x) / RATE
    midi_len = notes[-1][1]
    hop = max(0.05, max(audio_len, midi_len) / 6000)  # keeps the backtrack table under ~40 MB
    hop = round(hop * RATE) / RATE  # a whole number of samples per frame: 1102 rounded from 1102.5 drifts 0.05%
    first = max(0.0, notes[0][0] - 1.0)  # skip the MIDI's silent lead-in: the recording's may differ
    ac = audio_chroma(x, hop)
    mc = midi_chroma([(s - first, e - first, k, v, c) for s, e, k, v, c in notes],
                     int((midi_len - first) / hop) + 2, hop)
    if len(ac) < 4 or len(mc) < 4:
        return [f"{name} is too short to compare."]
    path_, quality = coarse_path(mc, ac)
    midi_t = np.arange(len(mc)) * hop + first
    lag_t = _running_median(path_ * hop - midi_t, max(3, int(4 / hop)))  # audio minus MIDI seconds, per frame
    voiced = (mc ** 2).sum(axis=1) > 1e-6

    trend_t = _running_median(lag_t, max(3, int(20 / hop)))

    def coarse_lag(ts):
        return np.interp(ts, midi_t[voiced], lag_t[voiced])

    def trend(ts):
        return np.interp(ts, midi_t[voiced], trend_t[voiced])

    if quality > 0.75:
        return [f"{name} does not seem to be the same music as this MIDI file. The harmony never lines up, "
                f"so the timing can't be measured."]
    starts = np.array(sorted({round(s, 4) for s, e, k, v, c in notes}))
    flux = audio_onsets(x)
    pts = fine_offsets(starts, flux, coarse_lag, wide=max(FINE_WIDE, 2 * hop), trend=trend)  # long files: coarser
    trusted = [p for p in pts if p[2]]
    use = trusted if len(trusted) >= max(3, len(pts) // 3) else pts
    if not use:
        return [f"Could not find enough clear note attacks in {name} to measure the timing."]
    tempos, sigs = _tempo_list(mf), time_signatures(mf)

    def where(t):
        tick = _tick_at(tempos, mf.division, t)
        return f"{_clock(t)} ({bar_beat(tick, mf.division, sigs)})"

    lines = describe_offsets(use, name, where, lambda t: _bpm_at(tempos, mf.division, t),
                             lambda t0, t1, a, b: _find_jump(starts, flux, t0, t1, a, b))
    if len(use) < len(pts):
        lines.append(f"Measured in {n(len(use), 'place')} with clear attacks, of {len(pts)} checked.")
    return lines


def _find_jump(starts, flux, t0: float, t1: float, lag_a: float, lag_b: float) -> tuple[float, float]:
    """Where the timing changes from lag_a to lag_b, between window centres t0 and t1:
    (last note still at the old timing, first note at the new one).

    Each note votes by whether more attack sits under it at the old lag or the new one;
    the split that agrees with the most votes is the jump. When the MIDI rests across
    it (Listening Forward's piano does, right through REAPER's padded bar), the jump can
    only be placed somewhere in the rest, and the two times say so.
    """
    np = _np()
    sel = starts[(starts >= t0) & (starts <= t1)]
    if len(sel) < 2:
        return t0, t1
    w = int(0.012 / FINE_HOP)

    def energy(lag):
        idx = np.round((sel + lag) / FINE_HOP).astype(int)
        return np.array([flux[max(0, k - w):k + w + 1].max() if 0 <= k < len(flux) else 0.0 for k in idx])

    vote = energy(lag_a) - energy(lag_b)  # positive: this note still plays at the old lag
    score = np.array([vote[:k].sum() - vote[k:].sum() for k in range(1, len(sel))])
    # quiet notes near the change vote almost nothing, so several splits score the same;
    # of those, the one across the longest silence is where the change can actually be
    near = np.flatnonzero(score >= score.max() - 0.02 * (np.abs(vote).sum() + 1e-9)) + 1
    k = int(max(near, key=lambda i: sel[i] - sel[i - 1]))
    return float(sel[k - 1]), float(sel[k])


def _amount(seconds: float) -> str:
    x = abs(seconds)
    return f"{x:.1f} seconds" if x >= 1 else f"{x * 1000:.0f} ms"


def _side(lag: float) -> str:
    return "behind" if lag > 0 else "ahead of"


def _bpm_words(bpm: float) -> str:
    return f"{bpm:.0f}" if abs(bpm - round(bpm)) < 0.05 else f"{bpm:.1f}"


def describe_offsets(points, name: str, where, bpm_at, find_jump=None) -> list[str]:
    """Turn measured (MIDI seconds, audio-minus-MIDI seconds) points into sentences.

    A positive lag means the audio plays later than the MIDI (the audio is behind).
    Single stray measurements are smoothed out first (a running median of three),
    then the curve is split into straight runs: flat ones line up or sit at a steady
    offset, sloped ones are a different tempo, and a step between runs is a jump.
    """
    np = _np()
    up = LINES_UP_MS / 1000
    t = np.array([p[0] for p in points])
    raw = np.array([p[1] for p in points])
    y = raw if len(raw) < 3 else _running_median(raw, 3)
    spread = float(y.max() - y.min())
    worst = float(np.percentile(np.abs(y), 90))
    if spread <= 2 * up and abs(float(np.median(y))) <= up:
        return [f"Lines up with {name}: every part of the MIDI lands within {_amount(worst)} of the audio, "
                f"as close as note attacks can be measured."]
    if spread <= 2 * up:
        med = float(np.median(y))
        return [f"Offset from {name}: the audio is {_amount(med)} {'later' if med > 0 else 'earlier'} than the "
                f"MIDI all the way through. Nothing else differs, so sliding one by {_amount(med)} lines them up."]
    tol = max(up, 0.015 * spread)  # once the drift runs to seconds, tens of ms are noise
    segs = _segments(list(zip(t, y)), tol=tol)

    def fit(seg):
        ts = np.array([p[0] for p in seg]); ys = np.array([p[1] for p in seg])
        if len(seg) < 2 or ts[-1] - ts[0] < WINDOW / 2:
            return 0.0, float(np.median(ys))
        a, b = np.polyfit(ts, ys, 1)
        return float(a), float(b)

    # A window that straddles a jump measures something in between and becomes a run of its
    # own, one point long. It says nothing about either side: drop it, and let the jump
    # search place the change from the notes.
    segs = [seg for i, seg in enumerate(segs) if len(seg) > 1 or i in (0, len(segs) - 1)] or segs

    def level(seg):
        return float(np.median([p[1] for p in seg]))

    # A short excursion that comes back to where it left is a passage matched a beat or
    # half a beat off, not the music moving and moving back: drop it.
    i = 1
    while i < len(segs) - 1:
        if segs[i][-1][0] - segs[i][0][0] < 20 and abs(level(segs[i - 1]) - level(segs[i + 1])) <= tol:
            del segs[i]
        else:
            i += 1
    # merge neighbours that are one run split by noise: same slope, no step between them
    merged = True
    while merged and len(segs) > 1:
        merged = False
        for i in range(len(segs) - 1):
            (a1, b1), (a2, b2) = fit(segs[i]), fit(segs[i + 1])
            at = segs[i + 1][0][0]
            gap = (a2 * at + b2) - (a1 * at + b1)
            if abs(gap) <= tol and abs(a1 - a2) < max(0.004, 0.2 * max(abs(a1), abs(a2))):
                segs[i:i + 2] = [segs[i] + segs[i + 1]]
                merged = True
                break
    lines = [f"Does NOT line up with {name}:"]
    tempo_differs, jumped = False, False
    prev = None
    for seg in segs:
        a, b = fit(seg)
        t0, t1 = seg[0][0], seg[-1][0]
        y0, y1 = a * t0 + b, a * t1 + b
        start = t0
        jumped_here = False
        if prev is not None:
            pa, pb, pt1 = prev
            d0, d1 = (a * pt1 + b) - (pa * pt1 + pb), y0 - (pa * t0 + pb)
            meet = (pb - b) / (a - pa) if abs(a - pa) > 1e-6 else None
            if meet is not None and pt1 - WINDOW <= meet <= t0 + WINDOW:
                start = min(max(meet, pt1), t0)  # the two runs meet: a change of tempo, not a jump
            elif min(abs(d0), abs(d1)) > tol:
                jumped = jumped_here = True
                last, first = find_jump(pt1, t0, pa * pt1 + pb, y0) if find_jump else (pt1, t0)
                start = first
                # each side's level at the change itself; a steady side by the median of all its points,
                # since a window that half-straddles the change pulls a fit or its nearest points
                after = float(np.median([p[1] for p in seg])) if abs(a) < 0.004 else a * first + b
                before = float(np.median([p[1] for p in prev_seg])) if abs(pa) < 0.004 else pa * last + pb
                gap = after - before
                if first - last > 2:
                    at = f"Between {_clock(last)} and {where(first)}, where the MIDI has no notes"
                else:
                    at = f"At {where(first)}"
                lines.append(f"  {at}, the timing jumps {_amount(gap)}: from there the audio is "
                             f"{_amount(after)} {_side(after)} the MIDI.")
        here = _clock(start) if jumped_here else where(start)
        if abs(a) >= 0.004 and abs(a) * (t1 - t0) > up:  # a slope is a tempo however wide the tolerance
            tempo_differs = True
            bpm = bpm_at((t0 + t1) / 2)
            rate = 1 + a  # audio seconds per MIDI second
            lines.append(
                f"  {here} to {_clock(t1)}: the audio is {'slower' if rate > 1 else 'faster'}, about "
                f"{bpm / rate:.0f} BPM where the MIDI says {_bpm_words(bpm)} ({abs(rate - 1) * 100:.1f}%). "
                f"By {_clock(t1)} it is {_amount(y1)} {_side(y1)} the MIDI.")
        elif abs(b + a * (t0 + t1) / 2) <= up:
            lines.append(f"  {here} to {_clock(t1)}: lines up.")
        else:
            mid = float(np.median([p[1] for p in seg])) if abs(a) < 0.004 else a * (t0 + t1) / 2 + b
            lines.append(f"  {here} to {_clock(t1)}: the audio is {_amount(mid)} {_side(mid)} the MIDI "
                         f"throughout.")
        prev, prev_seg = (a, b, t1), seg
    if not tempo_differs and not jumped and all(line.endswith(": lines up.") for line in lines[1:]):
        # stray measurements made the spread look wide, but every run lines up
        return [f"Lines up with {name}: every part of the MIDI lands within {_amount(worst)} of the audio, "
                f"as close as note attacks can be measured."]
    if tempo_differs:
        lines.append("The tempo differs, so the two came from different tempo maps. Usually the project's tempo "
                     "changed between rendering the audio and exporting the MIDI; render or export again from the "
                     "same version of the project.")
    elif jumped:
        lines.append("The tempo matches but the timing jumps, so time was added or removed at those places. If the "
                     "MIDI came from REAPER, compare it with the project: REAPER's export makes a bar whole when a "
                     "time signature cuts it short, and fixing with the project moves everything back.")
    return lines


def _tick_at(tempos, division, t: float) -> int:
    lo, hi = 0, max(1, tempos[-1][0] if tempos else 1) * 4 + int(division * 2 * max(1.0, t))
    while seconds_at(tempos, division, hi) < t:
        hi *= 2
    while lo < hi:
        mid = (lo + hi) // 2
        if seconds_at(tempos, division, mid) < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bpm_at(tempos, division, t: float) -> float:
    tick = _tick_at(tempos, division, t)
    bpm = 120.0
    for tk, b in tempos:
        if tk > tick:
            break
        bpm = b
    return bpm
