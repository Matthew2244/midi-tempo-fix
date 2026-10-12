"""Read, check and repair the tempo map of a Standard MIDI File.

A tempo ramp cannot live in a MIDI file, so DAWs write each one as many small
fixed-tempo steps. Done right, every app plays those steps identically. This
module finds the things that make apps disagree and removes them:

* Doubled tempo events: two tempo changes on the same tick. The MIDI rules say
  the second wins, but an app that keeps the first holds the wrong tempo for
  the rest of the song. Seen in REAPER exports at the end of a ramp; Logic
  drifted almost a second over one song.
* REAPER ramp notes: text events ("REAPER gradual tempo change start ...")
  that REAPER's export writes so its own import can rebuild each ramp. That
  rebuild misplaces any ramp ending on a time signature change. Without the
  notes, REAPER reads the exact steps like every other app.
* Tempo map events on the wrong track: in a multi-track (format 1) file the
  tempo map belongs in the first track. Some apps read it only from there.

Nothing else in the file is touched: notes, controllers, names, markers and
timing stay as they were, and every fix is checked by reading the result back
and comparing the time of every event before and after.

Standard library only.
"""
from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass, field

HINT = b"REAPER gradual tempo change"
TEMPO, TIME_SIG, KEY_SIG, END_OF_TRACK = 0x51, 0x58, 0x59, 0x2F
TEMPO_MAP_TYPES = (TEMPO, TIME_SIG, KEY_SIG)
DOUBLE_TOLERANCE = 0.001  # BPM; the same tempo written twice is harmless and left alone


class MidiError(Exception):
    """A file that cannot be read as a Standard MIDI File."""


# ---------------------------------------------------------------- reading


def _vlq_read(b: bytes, i: int) -> tuple[int, int]:
    v = 0
    for _ in range(4):
        c = b[i]
        i += 1
        v = (v << 7) | (c & 0x7F)
        if c < 0x80:
            return v, i
    raise MidiError("a length field in the file is corrupt")


def _vlq_write(v: int) -> bytes:
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    return bytes(reversed(out))


@dataclass
class Event:
    tick: int
    raw: bytes  # status byte included, running status expanded

    @property
    def is_meta(self) -> bool:
        return self.raw[0] == 0xFF

    @property
    def meta_type(self) -> int | None:
        return self.raw[1] if self.is_meta else None

    def meta_data(self) -> bytes:
        ln, k = _vlq_read(self.raw, 2)
        return self.raw[k : k + ln]

    @property
    def is_note_on(self) -> bool:
        return (self.raw[0] & 0xF0) == 0x90 and len(self.raw) > 2 and self.raw[2] > 0

    def bpm(self) -> float:
        return 60e6 / int.from_bytes(self.meta_data(), "big")


@dataclass
class MidiFile:
    header: bytes
    fmt: int
    division: float  # ticks per quarter note; for SMPTE files, ticks per second (see smpte)
    tracks: list[list[Event]]
    smpte: float | None = None  # frames per second when the file counts time in video frames


from . import notes as notes_mod  # noqa: E402  (imported here: notes uses Event above)


def read_midi(path: str) -> MidiFile:
    try:
        with open(path, "rb") as f:
            b = f.read()
    except OSError as e:
        raise MidiError(f"could not open it ({e.strerror})") from e
    if not b:
        raise MidiError("the file is empty")
    if b[:4] == b"RIFF" and b[8:12] == b"RMID":  # Windows .rmi: a MIDI file in a RIFF wrapper
        i = 12
        while i + 8 <= len(b):
            size = struct.unpack("<I", b[i + 4 : i + 8])[0]
            if b[i : i + 4] == b"data":
                b = b[i + 8 : i + 8 + size]
                break
            i += 8 + size + (size & 1)
    if b[:4] != b"MThd" or len(b) < 14:
        raise MidiError("it is not a MIDI file")
    hlen = struct.unpack(">I", b[4:8])[0]
    fmt, ntr, div = struct.unpack(">HHH", b[8:14])
    smpte = None
    if div & 0x8000:
        # SMPTE timing: time is frames x ticks per frame, and tempo events do not apply.
        # Modelled as a fixed 60 BPM with that many ticks per "beat", so one beat is one second.
        fps = -struct.unpack("b", bytes([div >> 8]))[0]
        smpte = 29.97 if fps == 29 else float(fps)
        div = smpte * (div & 0xFF)
        if div <= 0:
            raise MidiError("its timing header is corrupt")
    header = b[: 8 + hlen]
    i = 8 + hlen
    tracks: list[list[Event]] = []
    try:
        while i + 8 <= len(b) and len(tracks) < ntr:
            tag = b[i : i + 4]
            length = struct.unpack(">I", b[i + 4 : i + 8])[0]
            body = b[i + 8 : i + 8 + length]
            i += 8 + length
            if tag != b"MTrk":
                continue
            events: list[Event] = []
            j, tick, run = 0, 0, None
            while j < len(body):
                d, j = _vlq_read(body, j)
                tick += d
                s = body[j]
                if s == 0xFF:
                    ln, k = _vlq_read(body, j + 2)
                    events.append(Event(tick, body[j : k + ln]))
                    j = k + ln
                elif s in (0xF0, 0xF7):
                    ln, k = _vlq_read(body, j + 1)
                    events.append(Event(tick, body[j : k + ln]))
                    j = k + ln
                else:
                    if s & 0x80:
                        run = s
                        j += 1
                    if run is None:
                        raise MidiError("a track starts with data but no status byte")
                    n = 1 if (run & 0xF0) in (0xC0, 0xD0) else 2
                    events.append(Event(tick, bytes([run]) + body[j : j + n]))
                    j += n
            tracks.append(events)
    except IndexError as e:
        raise MidiError("the file is cut short or corrupt") from e
    return MidiFile(header, fmt, div, tracks, smpte)


def write_midi(path: str, mf: MidiFile) -> None:
    out = bytearray(mf.header)
    for events in mf.tracks:
        body = bytearray()
        last = 0
        for e in events:
            body += _vlq_write(e.tick - last) + e.raw
            last = e.tick
        out += b"MTrk" + struct.pack(">I", len(body)) + body
    with open(path, "wb") as f:
        f.write(out)


# ---------------------------------------------------------------- analysis


def n(count: int, word: str, plural: str | None = None) -> str:
    """'1 tempo event', '3 tempo events': no '(s)' for a screen reader to spell out."""
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _tempo_list(mf: MidiFile) -> list[tuple[int, float]]:
    """(tick, bpm) in playback order; for same-tick events the later one wins."""
    if mf.smpte:
        return [(0, 60.0)]
    ev = []
    for ti, events in enumerate(mf.tracks):
        for ei, e in enumerate(events):
            if e.meta_type == TEMPO:
                ev.append((e.tick, ti, ei, e.bpm()))
    ev.sort()
    return [(t, b) for t, _, _, b in ev]


def seconds_at(tempos: list[tuple[int, float]], division: int, tick: int) -> float:
    sec, last, bpm = 0.0, 0, 120.0
    for tk, b in tempos:
        if tk > tick:
            break
        sec += (tk - last) / division * 60 / bpm
        last, bpm = tk, b
    return sec + (tick - last) / division * 60 / bpm


def _event_seconds(mf: MidiFile) -> list[float]:
    """The playback time of every non-tempo-map event, in a stable order."""
    tempos = _tempo_list(mf)
    out = []
    for events in mf.tracks:
        for e in events:
            if e.meta_type in TEMPO_MAP_TYPES or e.meta_type == END_OF_TRACK:
                continue
            if e.meta_type in (1, 6) and HINT in e.raw:
                continue
            out.append(seconds_at(tempos, mf.division, e.tick))
    return out


def time_signatures(mf: MidiFile) -> list[tuple[int, int, int]]:
    out = []
    for events in mf.tracks:
        for e in events:
            if e.meta_type == TIME_SIG:
                d = e.meta_data()
                if len(d) >= 2:
                    out.append((e.tick, d[0], 2 ** d[1]))
    return sorted(out)


def bar_beat(tick: int, division: int, sigs: list[tuple[int, int, int]]) -> str:
    """Human position, 'bar 12, beat 3', honoring time signature changes."""
    sigs = [s for s in sigs if s[0] <= tick] or [(0, 4, 4)]
    if sigs[0][0] != 0:
        sigs.insert(0, (0, 4, 4))
    bar = 1
    for i, (tk, num, den) in enumerate(sigs):
        beat_len = int(division * 4 // den)
        bar_len = num * beat_len
        end = sigs[i + 1][0] if i + 1 < len(sigs) else tick
        span = end - tk
        if i + 1 == len(sigs):
            bars, rem = divmod(span, bar_len)
            return f"bar {bar + bars}, beat {rem // beat_len + 1}"
        bar += -(-span // bar_len)
    return f"bar {bar}"


@dataclass
class Ramp:
    start_tick: int
    end_tick: int
    from_bpm: float
    to_bpm: float


@dataclass
class Doubled:
    tick: int
    lost_bpm: float
    winning_bpm: float


@dataclass
class Report:
    name: str
    division: int
    tempo_count: int
    first_bpm: float | None
    length_seconds: float
    ramps: list[Ramp] = field(default_factory=list)
    doubled: list[Doubled] = field(default_factory=list)
    doubled_cost_ms: float = 0.0
    hints: int = 0
    misplaced: int = 0
    sigs: list[tuple[int, int, int]] = field(default_factory=list)
    notes: "notes_mod.NoteCounts" = None
    smpte: float | None = None

    @property
    def tempo_needs_fix(self) -> bool:
        return bool(self.doubled or self.hints or self.misplaced)

    @property
    def needs_fix(self) -> bool:
        return self.tempo_needs_fix or bool(self.notes and self.notes.fixable)


def analyze(mf: MidiFile, name: str = "") -> Report:
    tempos = _tempo_list(mf)
    end = max((e.tick for t in mf.tracks for e in t if e.is_note_on), default=0)
    r = Report(
        name=name,
        division=mf.division,
        tempo_count=len(tempos),
        first_bpm=tempos[0][1] if tempos else None,
        length_seconds=seconds_at(tempos, mf.division, end),
        sigs=time_signatures(mf),
        smpte=mf.smpte,
    )
    if mf.smpte:
        r.tempo_count, r.first_bpm = 0, None
    # doubled tempo events
    i = 0
    first_wins: list[tuple[int, float]] = []
    while i < len(tempos):
        j = i
        while j + 1 < len(tempos) and tempos[j + 1][0] == tempos[i][0]:
            j += 1
        first_wins.append(tempos[i])
        if j > i and any(abs(tempos[k][1] - tempos[j][1]) > DOUBLE_TOLERANCE for k in range(i, j)):
            r.doubled.append(Doubled(tempos[i][0], tempos[i][1], tempos[j][1]))
        i = j + 1
    if r.doubled:
        r.doubled_cost_ms = (seconds_at(first_wins, mf.division, end) - r.length_seconds) * 1000
    # ramps: runs of 3+ tempo changes at most an eighth note apart
    run: list[tuple[int, float]] = []
    for t in tempos + [(None, None)]:
        if run and t[0] is not None and t[0] - run[-1][0] <= mf.division // 2:
            run.append(t)
            continue
        if len(run) >= 3:
            r.ramps.append(Ramp(run[0][0], run[-1][0], run[0][1], run[-1][1]))
        run = [t] if t[0] is not None else []
    # REAPER ramp notes and misplaced tempo map events
    for ti, events in enumerate(mf.tracks):
        for e in events:
            if e.meta_type in (1, 6) and HINT in e.raw:
                r.hints += 1
    r.misplaced = len(_misplaced(mf))
    r.notes = notes_mod.analyze_notes(mf.tracks)
    return r


def _misplaced(mf: MidiFile) -> list[tuple[int, int]]:
    """(track, index) of tempo map events that belong on the first track and are missing there.

    Tempo changes always belong on the first track of a format 1 file. Time
    signatures on other tracks are common (notation apps copy them to every
    track) and harmless when the first track has the same change; only ones the
    first track lacks are counted. Key signatures are per-track by design.
    """
    if mf.fmt != 1 or len(mf.tracks) < 2 or mf.smpte:
        return []
    sig_ticks = {e.tick for e in mf.tracks[0] if e.meta_type == TIME_SIG}
    out = []
    for ti in range(1, len(mf.tracks)):
        for ei, e in enumerate(mf.tracks[ti]):
            if e.meta_type == TEMPO or (e.meta_type == TIME_SIG and e.tick not in sig_ticks):
                out.append((ti, ei))
    return out


def _beats(ticks: int, division: int) -> str:
    b = ticks / division
    return n(int(b), "beat") if b == int(b) else f"{b:.1f} beats"


def describe(r: Report, notes: bool = True) -> list[str]:
    """The report as plain sentences, one per line, for speech and for eyes."""
    lines = []
    if r.smpte:
        lines.append(
            f"{r.name}: counts time in video frames ({r.smpte:g} per second) instead of beats, so it has "
            f"no tempo map. Last note at {r.length_seconds:.1f} seconds. Its notes can still be checked."
        )
    elif r.first_bpm is None:
        lines.append(f"{r.name}: no tempo events, so every app plays it at 120 BPM.")
    else:
        lines.append(
            f"{r.name}: {n(r.tempo_count, 'tempo event')}, starts at {r.first_bpm:g} BPM, "
            f"last note at {r.length_seconds:.1f} seconds."
        )
    if r.ramps:
        lines.append(f"{n(len(r.ramps), 'tempo ramp')}, written as small steps the way MIDI requires:")
        for rp in r.ramps:
            lines.append(
                f"  {bar_beat(rp.start_tick, r.division, r.sigs)}: about {rp.from_bpm:.0f} to "
                f"{rp.to_bpm:.0f} BPM over {_beats(rp.end_tick - rp.start_tick, r.division)}."
            )
        lines.append("The decimal tempos inside a ramp are expected. They keep the timing exact.")
    if r.doubled:
        lines.append(f"Problem: {n(len(r.doubled), 'place')} with two different tempos on the same tick.")
        for d in r.doubled[:MAX_LISTED]:
            lines.append(
                f"  {bar_beat(d.tick, r.division, r.sigs)}: {d.lost_bpm:.4f} then {d.winning_bpm:.4f}. "
                f"The second is right."
            )
        if len(r.doubled) > MAX_LISTED:
            lines.append(f"  And {len(r.doubled) - MAX_LISTED} more.")
        if abs(r.doubled_cost_ms) >= 0.5:
            lines.append(
                f"  An app that keeps the first one, as Logic can, lands the last note "
                f"{r.doubled_cost_ms:+.0f} ms off."
            )
    if r.hints:
        lines.append(
            f"Problem in REAPER: {n(r.hints, 'ramp note')} left by REAPER's export. When REAPER imports the "
            f"file it rebuilds ramps from them, and puts any ramp that ends on a time signature change in "
            f"the wrong place. Other apps ignore them."
        )
    if r.misplaced:
        lines.append(
            f"Problem: {n(r.misplaced, 'tempo or time signature change')} only on a later track. "
            f"They belong on the first track, and some apps only look there."
        )
    c = r.notes
    if notes and c:
        if c.duplicates:
            lines.append(
                f"Problem: {n(c.duplicates, 'duplicate note')}, the same key struck twice at the same moment. "
                f"Some apps play both, some play one."
            )
        if c.overlaps:
            lines.append(
                f"Problem: {n(c.overlaps, 'overlapping note')}, a key struck again before it was released. "
                f"Apps disagree about which release ends which note, so note lengths change between apps."
            )
        if c.stuck:
            lines.append(
                f"Problem: {n(c.stuck, 'note')} that never end. Some apps cut them at the end, "
                f"some hold them forever."
            )
        if c.stray_offs:
            lines.append(f"Problem: {n(c.stray_offs, 'note release')} with no note playing.")
        if c.zero_length:
            lines.append(
                f"Left alone: {n(c.zero_length, 'note')} with no length. Some apps skip these and some "
                f"play a click, and no length is right for every case."
            )
    fixable = r.tempo_needs_fix or (notes and c and c.fixable)
    lines.append("Needs fixing." if fixable else "Clean. Every app should play this the same way.")
    return lines


MAX_LISTED = 10


# ---------------------------------------------------------------- repair


def repair(mf: MidiFile, notes: bool = True) -> tuple[MidiFile, dict]:
    """Return a cleaned copy and counts of what changed. The input is not modified."""
    tracks = [list(t) for t in mf.tracks]
    changed = {"doubled": 0, "hints": 0, "moved": 0, "duplicates": 0, "overlaps": 0, "stuck": 0, "stray_offs": 0}

    # 1. tempo map events onto the first track (format 1 only), before anything shifts indexes
    misplaced = set(_misplaced(mf))
    if misplaced:
        moved = []
        for ti in range(1, len(tracks)):
            stay = []
            for ei, e in enumerate(tracks[ti]):
                (moved if (ti, ei) in misplaced else stay).append(e)
            tracks[ti] = stay
        changed["moved"] = len(moved)
        first = [e for e in tracks[0] if e.meta_type != END_OF_TRACK]
        eot = [e for e in tracks[0] if e.meta_type == END_OF_TRACK]
        merged = sorted(first + moved, key=lambda e: e.tick)  # stable: track 0's own events first
        last = max([e.tick for e in merged] + [e.tick for e in eot] + [0])
        tracks[0] = merged + [Event(last, b"\xff\x2f\x00")]

    # 2. REAPER ramp notes
    for ti, events in enumerate(tracks):
        keep = [e for e in events if not (e.meta_type in (1, 6) and HINT in e.raw)]
        changed["hints"] += len(events) - len(keep)
        tracks[ti] = keep

    # 3. different tempos on one tick: keep the last; the same tempo twice is left alone
    for ti, events in enumerate(tracks):
        out: list[Event] = []
        for e in events:
            if e.meta_type == TEMPO:
                k = len(out) - 1
                while k >= 0 and out[k].tick == e.tick:
                    if out[k].meta_type == TEMPO and abs(out[k].bpm() - e.bpm()) > DOUBLE_TOLERANCE:
                        del out[k]
                        changed["doubled"] += 1
                    k -= 1
            out.append(e)
        tracks[ti] = out

    # 4. note problems
    if notes:
        tracks, c = notes_mod.repair_notes(tracks)
        changed.update(duplicates=c.duplicates, overlaps=c.overlaps, stuck=c.stuck, stray_offs=c.stray_offs)

    return MidiFile(mf.header, mf.fmt, mf.division, tracks), changed


def fixed_name(path: str) -> str:
    stem, ext = os.path.splitext(path)
    if ext.lower() in (".rmi", ""):
        ext = ".mid"  # a fixed .rmi is saved as a plain MIDI file, which every app opens
    return f"{stem} (tempo fixed){ext}"


def _note_starts(mf: MidiFile) -> list[tuple[int, float, int, int, int]]:
    """(track, seconds, channel, key, velocity) of every note start, rounded to a microsecond.

    A duplicate (same track, moment, channel and key) counts once, with the
    velocity of the first, which is the one the fix keeps.
    """
    tempos = _tempo_list(mf)
    out = {}
    for ti, events in enumerate(mf.tracks):
        for e in events:
            if e.raw[0] < 0xF0 and e.is_note_on:
                k = (ti, round(seconds_at(tempos, mf.division, e.tick), 6), e.raw[0] & 0x0F, e.raw[1])
                out.setdefault(k, e.raw[2])
    return sorted(k + (v,) for k, v in out.items())


def _other_seconds(mf: MidiFile) -> list[float]:
    """Times of everything that is not a note or part of the tempo map."""
    tempos = _tempo_list(mf)
    out = []
    for events in mf.tracks:
        for e in events:
            if e.raw[0] < 0xF0 and (e.raw[0] & 0xF0) in (0x80, 0x90):
                continue
            if e.meta_type in TEMPO_MAP_TYPES or e.meta_type == END_OF_TRACK:
                continue
            if e.meta_type in (1, 6) and HINT in e.raw:
                continue
            out.append(round(seconds_at(tempos, mf.division, e.tick), 6))
    return sorted(out)


def fix_file(path: str, out: str | None = None, notes: bool = True) -> tuple[str | None, list[str]]:
    """Write a cleaned copy. Returns (saved path or None, sentences)."""
    name = os.path.basename(path)
    mf = read_midi(path)
    before = analyze(mf, name)
    if not (before.tempo_needs_fix or (notes and before.notes.fixable)):
        return None, [f"{name}: nothing to fix. No file written."]
    fixed, changed = repair(mf, notes)
    out = out or fixed_name(path)
    if os.path.abspath(out) == os.path.abspath(path):
        raise MidiError("refusing to overwrite the original")
    write_midi(out, fixed)

    # Verify by reading the saved file back: nothing left to fix, every note still
    # starts at the same moment with the same key and velocity, everything else
    # (controllers, names, markers) at the same moment as before.
    back = read_midi(out)
    after = analyze(back, name)
    ok = (
        not after.tempo_needs_fix
        and not (notes and after.notes.fixable)
        and _note_starts(back) == _note_starts(mf)
        and _other_seconds(back) == _other_seconds(mf)
    )
    if not ok:
        os.remove(out)
        raise MidiError("the cleaned copy did not verify, so it was deleted. The original is untouched")

    def joined(items):
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]

    removed = [n(changed[k], t) for k, t in (
        ("doubled", "doubled tempo event"), ("hints", "REAPER ramp note"),
        ("duplicates", "duplicate note"), ("stray_offs", "stray note release"),
    ) if changed[k]]
    ended = [t for k, t in (
        ("overlaps", f"{n(changed['overlaps'], 'overlapping note')} where the key is struck again"),
        ("stuck", f"{n(changed['stuck'], 'stuck note')} at the end of the track"),
    ) if changed[k]]
    said = []
    if removed:
        said.append("removed " + joined(removed))
    if changed["moved"]:
        said.append(f"moved {n(changed['moved'], 'tempo map event')} to the first track")
    if ended:
        said.append("ended " + joined(ended))
    what = said[0] if len(said) == 1 else ", ".join(said[:-1]) + ", and " + said[-1]
    return out, [
        f"{name}: {what}. Every note still starts at the same moment, checked by reading the copy back.",
        f"Saved: {out}",
    ]


# ---------------------------------------------------------------- REAPER projects


def rpp_tempo_points(path: str) -> list[tuple[float, float, int]]:
    """(seconds, bpm, shape) from a REAPER project's tempo envelope."""
    pts, on, base = [], False, None
    with open(path, errors="replace") as f:
        for line in f:
            s = line.strip()
            if base is None and s.startswith("TEMPO "):
                base = float(s.split()[1])
            if s.startswith("<TEMPOENVEX"):
                on = True
                continue
            if on:
                if s == ">":
                    break
                if s.startswith("PT "):
                    a = s.split()
                    pts.append((float(a[1]), float(a[2]), int(a[3]) if len(a) > 3 else 1))
    if not pts and base is not None:
        pts = [(0.0, base, 1)]
    if not pts:
        raise MidiError("no tempo found in that REAPER project")
    return pts


def rpp_seconds_at_beat(pts: list[tuple[float, float, int]]):
    """REAPER ramps are linear in time: beats = seconds * (start + end) / 120."""
    segs, beat = [], 0.0
    for i, (t, bpm, shape) in enumerate(pts):
        if i + 1 < len(pts):
            t1, b1 = pts[i + 1][0], pts[i + 1][1]
            dt = t1 - t
            end = b1 if shape == 0 else bpm
            nb = dt * (bpm + end) / 120
            segs.append((beat, t, bpm, end, dt, nb))
            beat += nb
        else:
            segs.append((beat, t, bpm, bpm, 0.0, math.inf))

    def f(b: float) -> float:
        for b0, t0, s, e, dt, nb in segs:
            if b <= b0 + nb + 1e-9:
                x = b - b0
                if s != e:
                    k = (e - s) / dt
                    return t0 + (-s + math.sqrt(s * s + 2 * k * 60 * x)) / k
                return t0 + x * 60 / s
        return math.nan

    return f


def compare_project(mf: MidiFile, rpp: str) -> list[str]:
    """Compare a MIDI file's tempo map with a REAPER project's, beat by beat."""
    f = rpp_seconds_at_beat(rpp_tempo_points(rpp))
    tempos = _tempo_list(mf)
    end = max((e.tick for t in mf.tracks for e in t if e.is_note_on), default=0)
    worst, at = 0.0, 0
    for tick in range(0, end + 1, max(1, int(mf.division // 4))):
        d = (seconds_at(tempos, mf.division, tick) - f(tick / mf.division)) * 1000
        if abs(d) > abs(worst):
            worst, at = d, tick
    where = bar_beat(at, mf.division, time_signatures(mf))
    if abs(worst) < 1:
        return [f"Tempo matches the project {os.path.basename(rpp)}: " + _same_moment("every beat", worst)]
    return [
        f"Tempo does NOT match the project {os.path.basename(rpp)}: up to {worst:+.0f} ms off, at {where}.",
        "Usually that means the project's tempo was changed after this file was exported.",
    ]


def _same_moment(what: str, worst_ms: float, capital: bool = False) -> str:
    if abs(worst_ms) < 0.05:
        verb = "lands" if what == "every beat" else "starts"
        return f"{what.replace(' starts', '')} {verb} at exactly the same moment." if not capital else \
            f"{what.replace(' starts', '')} starts at exactly the same moment."
    return f"{what} within {abs(worst_ms):.1f} ms of the same moment, which is rounding no one can hear."


def _timed_notes(mf: MidiFile) -> dict[tuple[int, int], list[float]]:
    """Note start times in seconds, grouped by (channel, key), whatever track they are on."""
    tempos = _tempo_list(mf)
    out: dict[tuple[int, int], list[float]] = {}
    for events in mf.tracks:
        for e in events:
            if e.raw[0] < 0xF0 and e.is_note_on:
                out.setdefault((e.raw[0] & 0x0F, e.raw[1]), []).append(seconds_at(tempos, mf.division, e.tick))
    for v in out.values():
        v.sort()
    return out


KEY_NAMES = ["C", "C sharp", "D", "E flat", "E", "F", "F sharp", "G", "A flat", "A", "B flat", "B"]


def _key_name(key: int) -> str:
    return f"{KEY_NAMES[key % 12]} {key // 12 - 1}"


def compare_midi(a: MidiFile, b: MidiFile, b_name: str) -> list[str]:
    """Compare two MIDI files of the same music, from any app: tempo map, then every note's timing.

    Typical use: export from one app, import into another, export again, and
    compare the two exports to see whether the second app changed anything.
    Tracks may be merged, split or renamed between apps; notes are matched by
    channel and key, in order, so that does not matter.
    """
    lines = []
    ta, tb = _tempo_list(a), _tempo_list(b)
    end_a = max((e.tick for t in a.tracks for e in t if e.is_note_on), default=0)
    beats = end_a / a.division
    if not a.smpte and not b.smpte:
        worst, at = 0.0, 0.0
        steps = max(1, int(beats * 4))
        for i in range(steps + 1):
            beat = beats * i / steps
            d = (seconds_at(tb, b.division, beat * b.division) - seconds_at(ta, a.division, beat * a.division)) * 1000
            if abs(d) > abs(worst):
                worst, at = d, beat
        where = bar_beat(int(at * a.division), a.division, time_signatures(a))
        if abs(worst) < SAME_MS:
            lines.append(f"Tempo matches {b_name}: " + _same_moment("every beat", worst))
        else:
            lines.append(f"Tempo does NOT match {b_name}: up to {worst:+.0f} ms off, at {where}.")

    na, nb = _timed_notes(a), _timed_notes(b)
    count_a, count_b = sum(map(len, na.values())), sum(map(len, nb.values()))
    worst, worst_key, worst_t = 0.0, None, 0.0
    differ = []
    for key in sorted(set(na) | set(nb)):
        xa, xb = na.get(key, []), nb.get(key, [])
        if len(xa) != len(xb):
            differ.append((key, len(xa), len(xb)))
        for s1, s2 in zip(xa, xb):
            d = (s2 - s1) * 1000
            if worst_key is None or abs(d) > abs(worst):
                worst, worst_key, worst_t = d, key, s1
    if count_a == count_b and not differ:
        lines.append(f"Same {n(count_a, 'note')} in both.")
    else:
        lines.append(f"Different notes: {n(count_a, 'note')} here, {count_b} in {b_name}.")
        for key, ca, cb in differ[:MAX_LISTED]:
            lines.append(f"  {_key_name(key[1])} on channel {key[0] + 1}: {ca} here, {cb} there.")
        if len(differ) > MAX_LISTED:
            lines.append(f"  And {len(differ) - MAX_LISTED} more keys.")
            lines.append("So many notes differ that these look like different versions of the music.")
        return lines  # matching notes by order means nothing once the notes differ
    if worst_key is not None:
        if abs(worst) < SAME_MS:
            lines.append(_same_moment("Every note starts", worst, capital=True))
        else:
            lines.append(
                f"Notes move by up to {worst:+.0f} ms. The biggest is {_key_name(worst_key[1])} on channel "
                f"{worst_key[0] + 1}, {worst_t:.1f} seconds in."
            )
    return lines


REFERENCE_TYPES = (".mid", ".midi", ".smf", ".rmi", ".rpp")
SAME_MS = 2.0  # below this, timing differences are tick rounding (Logic saves at half REAPER's resolution)


def compare_with(mf: MidiFile, path: str) -> list[str]:
    """Compare against another MIDI file, or a REAPER project, chosen by its extension."""
    name = os.path.basename(path)
    if path.lower().endswith(".rpp"):
        return compare_project(mf, path)
    return compare_midi(mf, read_midi(path), name)
