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


class Event:
    """One MIDI event: absolute tick and its raw bytes (status included, running status expanded).

    meta_type is worked out once here; it is asked for hundreds of thousands of
    times on a big piano file, and as a property it was a third of the check time.
    """

    __slots__ = ("tick", "raw", "meta_type")

    def __init__(self, tick: int, raw: bytes):
        self.tick = tick
        self.raw = raw
        self.meta_type = raw[1] if raw[0] == 0xFF and len(raw) > 1 else None

    def __repr__(self) -> str:
        return f"Event({self.tick}, {self.raw!r})"

    @property
    def is_meta(self) -> bool:
        return self.raw[0] == 0xFF

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
        # a real ramp: at least three distinct moments, time passing, and the tempo actually moving
        if len({t for t, _ in run}) >= 3 and run[-1][0] > run[0][0] and abs(run[-1][1] - run[0][1]) > 0.5:
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


def bpm_text(bpm: float) -> str:
    """90.0001 reads as 90: MIDI stores tempo as microseconds per beat, so whole tempos come back
    a hair off. Kept to two decimals otherwise."""
    return f"{round(bpm, 2):g}"


def _beats(ticks: int, division: int) -> str:
    b = ticks / division
    return n(int(b), "beat") if b == int(b) else f"{b:.1f} beats"


def problems(r: Report, notes: bool = True) -> list[str]:
    """Short names of what needs fixing, for the one-line verdict."""
    out = []
    if r.doubled:
        out.append("two tempos on one tick")
    if r.misplaced:
        out.append("tempo on the wrong track")
    if r.hints:
        out.append("REAPER ramp notes")
    c = r.notes
    if notes and c:
        for count, word in ((c.duplicates, "duplicate notes"), (c.overlaps, "overlapping notes"),
                            (c.stuck, "stuck notes"), (c.stray_offs, "stray note releases")):
            if count:
                out.append(word)
    return out


PROJECT_PROBLEM = "REAPER made a short bar whole"  # found only by comparing with the project


def verdict(r: Report, notes: bool = True, extra: tuple = ()) -> str:
    """The answer first: one line a listener can stop after. extra: problems found elsewhere,
    such as by comparing with the REAPER project."""
    found = problems(r, notes) + list(extra)
    text = "clean." if not found else f"needs fixing. {n(len(found), 'problem')}: {', '.join(found)}."
    return f"{r.name}: {text}" if r.name else text[0].upper() + text[1:]


def describe(r: Report, notes: bool = True, extra: tuple = ()) -> list[str]:
    """The report as plain sentences, one per line, for speech and for eyes. The verdict comes first."""
    lines = [verdict(r, notes, extra)]
    if r.smpte:
        lines.append(
            f"Counts time in video frames ({r.smpte:g} per second) instead of beats, so it has "
            f"no tempo map. Last note at {r.length_seconds:.1f} seconds. Its notes can still be checked."
        )
    elif r.first_bpm is None:
        lines.append("No tempo events, so every app plays it at 120 BPM.")
    else:
        lines.append(
            f"{n(r.tempo_count, 'tempo event')}, starts at {bpm_text(r.first_bpm)} BPM, "
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
    if not problems(r, notes) and not extra:
        lines.append("Every app should play this the same way.")
    return lines


def summary(checked: int, needs: int, unreadable: int = 0) -> str:
    """One line for a batch, said before the details."""
    parts = [f"{needs} need{'s' if needs == 1 else ''} fixing", f"{checked - needs - unreadable} clean"]
    if unreadable:
        parts.append(f"{unreadable} could not be read")
    return f"Checked {n(checked, 'file')}: " + ", ".join(parts) + "."


MAX_LISTED = 10


# ---------------------------------------------------------------- repair


def repair(mf: MidiFile, notes: bool = True) -> tuple[MidiFile, dict]:
    """Return a cleaned copy and counts of what changed. The input is not modified."""
    tracks = [list(t) for t in mf.tracks]
    changed = {"doubled": 0, "hints": 0, "moved": 0, "merged": 0, "duplicates": 0, "overlaps": 0, "stuck": 0,
               "stray_offs": 0}

    # 1. tempo map events onto the first track (format 1 only), before anything shifts indexes
    misplaced = set(_misplaced(mf))
    if misplaced:
        moved = []
        for ti in range(1, len(tracks)):
            stay = []
            for ei, e in enumerate(tracks[ti]):
                (moved if (ti, ei) in misplaced else stay).append(e)
            tracks[ti] = stay
        # Several tracks often carry identical copies (LilyPond writes the tempo on every
        # instrument track); one copy on the first track is enough. The LAST copy of each is
        # the one kept: where tracks disagree on a tick, the last one read wins, and dropping
        # a later copy would hand the win to a different tempo (training found two such files).
        first = [e for e in tracks[0] if e.meta_type != END_OF_TRACK]
        eot = [e for e in tracks[0] if e.meta_type == END_OF_TRACK]
        combined = sorted(first + moved, key=lambda e: e.tick)  # stable: track 0, then tracks in order
        moved_ids = {id(e) for e in moved}
        seen, keep = set(), []
        for e in reversed(combined):
            key = (e.tick, e.raw)
            if e.meta_type in TEMPO_MAP_TYPES and key in seen:
                continue  # an identical copy comes later on the same tick
            seen.add(key)
            keep.append(e)
        keep.reverse()
        kept_moved = sum(1 for e in keep if id(e) in moved_ids)
        dropped_first = len(first) - (len(keep) - kept_moved)
        changed["moved"] = kept_moved
        changed["merged"] = len(moved) - kept_moved + dropped_first
        last = max([e.tick for e in keep] + [e.tick for e in eot] + [0])
        tracks[0] = keep + [Event(last, b"\xff\x2f\x00")]

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


MIDI_EXTENSIONS = (".mid", ".midi", ".smf", ".rmi", ".kar")


def midi_files(paths: list[str], recursive: bool = False) -> list[str]:
    """Expand folders into the MIDI files inside them; plain files pass through.

    Cleaned copies ("(tempo fixed)") are skipped so a second run does not
    check its own output. Hidden folders and files are skipped.
    """
    out = []
    for p in paths:
        if not os.path.isdir(p):
            out.append(p)
            continue
        for root, dirs, names in os.walk(p):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")) if recursive else []
            for name in sorted(names):
                if name.startswith(".") or "(tempo fixed)" in name:
                    continue
                if name.lower().endswith(MIDI_EXTENSIONS):
                    out.append(os.path.join(root, name))
    return out


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


def fix_file(path: str, out: str | None = None, notes: bool = True,
             project: str | None = None) -> tuple[str | None, list[str]]:
    """Write a cleaned copy. Returns (saved path or None, sentences).

    With a REAPER project, also undoes the export's padded short bars (see
    project_short_bars) so the copy plays exactly where the project does.
    """
    name = os.path.basename(path)
    mf = read_midi(path)
    before = analyze(mf, name)
    bars = project_short_bars(project) if project else []
    padded = export_has_short_bar_fault(mf, bars)
    if not (before.tempo_needs_fix or (notes and before.notes.fixable) or padded):
        return None, [f"{name}: nothing to fix. No file written."]
    fixed, changed = repair(mf, notes)
    if padded:
        return _fix_short_bars(path, mf, fixed, changed, project, out, notes)
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
    if changed["moved"] or changed["merged"]:
        moved = f"moved {n(changed['moved'], 'tempo map event')} to the first track"
        if changed["merged"]:
            moved += f", merging {n(changed['merged'], 'identical copy', 'identical copies')}"
        said.append(moved)
    if ended:
        said.append("ended " + joined(ended))
    what = said[0] if len(said) == 1 else ", ".join(said[:-1]) + ", and " + said[-1]
    return out, [
        f"{name}: {what}. Every note still starts at the same moment, checked by reading the copy back.",
        f"Saved: {out}",
    ]


def _clock(seconds: float) -> str:
    m, sec = divmod(int(round(seconds)), 60)
    return f"{m}:{sec:02d}"


def _note_beats(mf: MidiFile) -> list[tuple[int, int, int, int, float]]:
    """(track, channel, key, velocity, beat) of every note start, in each track's order."""
    out = []
    for ti, events in enumerate(mf.tracks):
        for e in events:
            if e.raw[0] < 0xF0 and e.is_note_on:
                out.append((ti, e.raw[0] & 0x0F, e.raw[1], e.raw[2], e.tick / mf.division))
    return out


def short_bar_sentences(mf: MidiFile, rpp: str, bars: list[ShortBar]) -> list[str]:
    """Plain words for each padded short bar: where, why, and how late the rest came out."""
    tempos, sigs = _tempo_list(mf), time_signatures(mf)
    f = rpp_seconds_at_beat(rpp_tempo_points(rpp))
    out = []
    for b in bars:
        tick = int(round((b.beat + b.shift) * mf.division))
        late = (seconds_at(tempos, mf.division, tick) - f(b.beat)) * 1000
        short = (f"{round(b.missing, 2):g} beat" + ("" if round(b.missing, 2) == 1 else "s")) if b.missing >= 1 \
            else f"{b.missing:.2f} of a beat"
        late_words = f"{late / 1000:.1f} seconds" if abs(late) >= 1000 else f"{late:.0f} ms"
        out.append(
            f"  {bar_beat(tick, mf.division, sigs)} ({_clock(b.seconds)}): the project's time signature starts "
            f"partway through a bar, leaving that bar {short} short. REAPER's export made the bar whole, so "
            f"everything after it plays {late_words} late."
        )
    return out


def _fix_short_bars(path, original, cleaned, changed, project, out, notes):
    name = os.path.basename(path)
    retimed, bars = match_project_bars(cleaned, project)
    out = out or fixed_name(path)
    if os.path.abspath(out) == os.path.abspath(path):
        raise MidiError("refusing to overwrite the original")
    write_midi(out, retimed)

    # Verify by reading the copy back: nothing left to fix, the tempo map lands on the
    # project's beats, and every note kept its key, velocity and order and moved by exactly
    # the padding before it (no note moves anywhere else).
    back = read_midi(out)
    after = analyze(back, name)
    f = rpp_seconds_at_beat(rpp_tempo_points(project))
    tempos = _tempo_list(back)
    end = max((e.tick for t in back.tracks for e in t if e.is_note_on), default=0)
    worst = max((abs(seconds_at(tempos, back.division, tk) - f(tk / back.division))
                 for tk in range(0, end + 1, max(1, int(back.division // 4)))), default=0.0)
    expect = [(ti, ch, key, vel, beat - _shift_at(bars, cleaned.division, beat * cleaned.division))
              for ti, ch, key, vel, beat in _note_beats(cleaned)]
    got = _note_beats(back)
    half_tick = 0.5 / back.division + 1e-9
    ok = (
        not after.tempo_needs_fix
        and not (notes and after.notes.fixable)
        and worst < 0.001
        and export_has_short_bar_fault(back, bars) is False
        and len(expect) == len(got)
        and all(a[:4] == b[:4] and abs(a[4] - b[4]) <= half_tick for a, b in zip(expect, got))
    )
    if not ok:
        os.remove(out)
        raise MidiError("the cleaned copy did not verify, so it was deleted. The original is untouched")
    lines = [f"{name}: moved everything REAPER's export pushed late back to where the project plays it."]
    lines += short_bar_sentences(original, project, bars)
    other = [n(changed[k], t) for k, t in (
        ("doubled", "doubled tempo event"), ("hints", "REAPER ramp note"), ("duplicates", "duplicate note"),
        ("overlaps", "overlapping note"), ("stuck", "stuck note"), ("stray_offs", "stray note release"),
    ) if changed[k]]
    if other:
        lines.append("Also fixed: " + ", ".join(other) + ".")
    lines.append(f"It now matches {os.path.basename(project)} beat for beat, checked by reading the copy back.")
    lines.append(f"Saved: {out}")
    return out, lines


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


def rpp_time_signatures(path: str) -> tuple[tuple[int, int], list[tuple[float, int, int]]]:
    """The project's starting time signature, and (seconds, num, den) of every tempo point that sets one.

    REAPER stores a time signature on a tempo point as one number: numerator in the
    low 16 bits, denominator above (262147 is 3/4, 524293 is 5/8).
    """
    start, sigs, on = (4, 4), [], False
    with open(path, errors="replace") as f:
        for line in f:
            s = line.strip()
            if s.startswith("TEMPO ") and not on:
                a = s.split()
                if len(a) >= 4:
                    start = (int(float(a[2])), int(float(a[3])))
            if s.startswith("<TEMPOENVEX"):
                on = True
                continue
            if on:
                if s == ">":
                    break
                if s.startswith("PT "):
                    a = s.split()
                    if len(a) > 4:
                        code = int(a[4])
                        num, den = code & 0xFFFF, code >> 16
                        if num and den:
                            sigs.append((float(a[1]), num, den))
    return start, sigs


def rpp_beat_at_seconds(pts: list[tuple[float, float, int]], t: float) -> float:
    """Beats from the start of the project to a moment, ramps linear in time."""
    beat = 0.0
    for i, (t0, bpm, shape) in enumerate(pts):
        last = i + 1 == len(pts)
        t1 = math.inf if last else pts[i + 1][0]
        end = bpm if (last or shape != 0) else pts[i + 1][1]
        if t <= t1 + 1e-9 or last:
            dt = t - t0
            now = bpm + (end - bpm) * dt / (t1 - t0) if end != bpm else bpm
            return beat + dt * (bpm + now) / 120
        beat += (t1 - t0) * (bpm + end) / 120
    return beat


@dataclass
class ShortBar:
    """A bar the project leaves short, because the next time signature starts partway through it."""
    seconds: float      # where the project's next time signature starts
    beat: float         # the same moment, in beats from the start
    missing: float      # beats REAPER's export adds to make the short bar whole
    shift: float        # beats everything after here is pushed late, counting earlier short bars


def project_short_bars(rpp: str) -> list[ShortBar]:
    """Bars a REAPER project leaves short, where a time signature lands partway through a bar.

    REAPER plays a short bar as it is, but its MIDI export makes every bar whole: it
    pads the short bar to full length and pushes everything after it late by the
    difference. Found on Listening Forward (2026-10-12): a 3/4 marker 79.15 beats into
    a 4/4 section, so the export added 0.85 of a beat at 200 BPM and the rest of the
    song came out 254 ms late against the project and its render.
    """
    pts = rpp_tempo_points(rpp)
    start, sigs = rpp_time_signatures(rpp)
    out, shift = [], 0.0
    prev_beat, (num, den) = 0.0, start
    for t, n_, d_ in sigs:
        beat = rpp_beat_at_seconds(pts, t)
        bar = num * 4 / den
        bars = (beat - prev_beat) / bar
        part = bars - math.floor(bars)
        if beat - prev_beat > 1e-6 and 1e-4 < part < 1 - 1e-4:
            missing = (1 - part) * bar
            shift += missing
            out.append(ShortBar(t, beat, missing, shift))
        prev_beat, num, den = beat, n_, d_
    return out


def _shift_at(bars: list[ShortBar], division: float, tick: float) -> float:
    """Beats the export pushed this tick late: the shift of the last short bar it comes after."""
    s = 0.0
    for b in bars:
        if tick >= (b.beat + b.shift) * division - 2:
            s = b.shift
    return s


def export_has_short_bar_fault(mf: MidiFile, bars: list[ShortBar]) -> bool:
    """True when the file's tempo map has REAPER's padding: a tempo change where each padded
    bar's next section starts, and none where the project starts it."""
    if not bars or mf.smpte:
        return False
    ticks = sorted([t for t, _ in _tempo_list(mf)] + [t for t, _, _ in time_signatures(mf)])

    def near(x):
        return any(abs(t - x) <= 2 for t in ticks)

    return all(near((b.beat + b.shift) * mf.division) and not near(b.beat * mf.division) for b in bars
               if b.beat * mf.division <= (ticks[-1] if ticks else 0))


def match_project_bars(mf: MidiFile, rpp: str) -> tuple[MidiFile, list[ShortBar]]:
    """Move everything REAPER's export pushed late back to where the project plays it.

    Returns the corrected copy and the short bars it undid; an empty list means this
    file does not have the fault (nothing changed). The time signature lands partway
    through the bar again, exactly as in the project.
    """
    bars = project_short_bars(rpp)
    if not export_has_short_bar_fault(mf, bars):
        return mf, []
    div = mf.division
    tracks = []
    for events in mf.tracks:
        moved = [Event(int(round(e.tick - _shift_at(bars, div, e.tick) * div)), e.raw) for e in events]
        moved.sort(key=lambda e: e.tick)  # stable; the mapping keeps order everywhere but the empty padding
        tracks.append(moved)
    return MidiFile(mf.header, mf.fmt, div, tracks, mf.smpte), bars


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
    bars = project_short_bars(rpp)
    if export_has_short_bar_fault(mf, bars):  # first: at one steady tempo the beats still match
        return [f"Does NOT match the project {os.path.basename(rpp)}. Problem in REAPER's export:"] + \
            short_bar_sentences(mf, rpp, bars) + [
                "Fixing it with this project moves everything back to where the project plays it."]
    if abs(worst) < 1:
        return [f"Tempo matches the project {os.path.basename(rpp)}: " + _same_moment("every beat", worst)]
    return [
        f"Tempo does NOT match the project {os.path.basename(rpp)}: up to {worst:+.0f} ms off, at {where}.",
        "Usually that means the project's tempo was changed after this file was exported.",
    ]


def needs_project_fix(path: str, rpp: str) -> bool:
    """True when fixing this file with this project would undo REAPER's padded short bars."""
    try:
        return export_has_short_bar_fault(read_midi(path), project_short_bars(rpp))
    except (MidiError, OSError, ValueError):
        return False


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
    """Compare against another MIDI file, a REAPER project, or a recording, chosen by its extension."""
    name = os.path.basename(path)
    if path.lower().endswith(".rpp"):
        return compare_project(mf, path)
    from . import audio  # numpy is only needed for audio, so only loaded for it
    if path.lower().endswith(audio.AUDIO_EXTENSIONS):
        return audio.compare_audio(mf, path)
    return compare_midi(mf, read_midi(path), name)
