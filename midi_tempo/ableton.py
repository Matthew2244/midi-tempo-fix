"""Turn a MIDI file's markers into Ableton Live locators, for AbleSet or plain Live.

Write the song anywhere (REAPER, Logic, Pro Tools, a notation app), put markers
where the sections start, export MIDI, and this makes the Live set: one locator
per marker, named the way AbleSet reads them ("Song name", "> Verse",
"SONG END"), with the file's tempo map and time signatures, so Live's bars land
where the song's do.

Two ways in:

* A new set (`new_set`): built from Live's own default set (found in the
  installed Live) or a set you choose to start from. Several MIDI files make
  one set with the songs back to back, or one set each for AbleSet's
  multi-file projects.
* A set you already have (`add_to_set`): as a new song after the last one,
  with its tempo map, or onto a song already there, replacing that song's
  section locators. Its tempo is left alone there, because the stems are
  already lined up with it. When the set's tempo and the file's disagree, the
  markers go either by bar (a set sped up or slowed down on purpose) or by
  time (audio that isn't warped), and that choice is the user's.

Live sets are gzipped XML. Only three things are edited, as text: the locator
list and the main track's tempo and time signature automation (plus their
starting values in a new set). Everything else stays byte for byte as it was,
because a set holds years of someone's work and a rewrite of the whole
document could lose what we don't understand. Every set written is read back
and checked: the XML parses, every locator is where it should be, and with
the tempo map written every locator plays at the moment its marker does,
within a millisecond.

Standard library only.
"""
from __future__ import annotations

import glob
import gzip
import html
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .core import (HINT, MidiError, _tempo_list, analyze, match_project_bars, n, problems, read_midi, repair,
                   time_signatures)

PRE = -63072000  # Live's time for the value an envelope holds before the arrangement starts
MARKER, CUE = 0x06, 0x07
MIN_BPM, MAX_BPM = 20.0, 999.0  # Live's tempo range
LIVE_DENOMINATORS = (1, 2, 4, 8, 16)
SAME_SECONDS = 0.005  # the set's tempo "matches" the file's when every marker lands within this
CHECK_SECONDS = 0.001  # every written locator must play within this of its marker
ABLESET_COMMANDS = ("SONG END", "AUTOSTOP", "STOP")
END_WORDS = ("end", "song end", "end of song", "the end")


class SetError(Exception):
    """A Live set that can't be read or written, in words a person can act on."""


class NeedsLineUp(SetError):
    """The set's tempo isn't the file's, so the user has to say how markers line up."""

    def __init__(self, message: str, worst: float):
        super().__init__(message)
        self.worst = worst


# ---------------------------------------------------------------- the song, from the MIDI file

@dataclass
class Locator:
    beat: float  # quarter notes from the song's start (in a plan) or the set's start (once placed)
    name: str
    seconds: float  # from the song's start, by the MIDI file's own tempo map
    kind: str  # "song", "section" or "end"
    marker: str = ""  # the marker text it came from


@dataclass
class SongPlan:
    path: str
    name: str
    locators: list[Locator]
    tempos: list[tuple[float, float]]  # (beat, bpm), one per beat, first at 0
    sigs: list[tuple[float, int, int]]  # (beat, numerator, denominator), first at 0
    end_beat: float
    end_seconds: float
    end_from_marker: bool
    markers: int
    span_beat: float = 0.0  # where the song's music and tempo map really stop (may run past SONG END)
    typed: list = field(default_factory=list)  # the TypedMarkers it was given
    options: dict = field(default_factory=dict)
    said: list[str] = field(default_factory=list)  # what was done to read it, worth telling

    @property
    def sections(self) -> list[Locator]:
        return [loc for loc in self.locators if loc.kind == "section"]


def midi_markers(mf) -> list[tuple[int, str]]:
    """(tick, text) of every marker and cue point, in order, each once."""
    out, seen = [], set()
    for events in mf.tracks:
        for e in events:
            if e.meta_type in (MARKER, CUE) and HINT not in e.raw:
                raw = e.meta_data()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                # NUL and other control characters can't live in a Live set's XML
                text = "".join(" " if ord(c) < 32 else c for c in text.replace("\x00", "")).strip()
                if text.startswith("#"):  # REAPER can export only markers that start with #
                    text = text[1:].strip()
                if (e.tick, text) not in seen:
                    seen.add((e.tick, text))
                    out.append((e.tick, text))
    out.sort(key=lambda m: m[0])
    return out


def song_name_for(path: str) -> str:
    """The file's name, tidied: "Fresh Air - midi track (tempo fixed).mid" is "Fresh Air".

    Not the sequence name inside the file: on real files that held a notation
    app's first staff ("Trumpet in B flat") or an old project's name as often as
    the song's.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"\s*\(tempo fixed\)\s*", " ", stem)
    stem = re.sub(r"[\s_-]*\b(midi( track| file)?|mid)\b[\s_-]*$", "", stem, flags=re.I)
    return stem.strip(" _-") or "Song"


def section_name(text: str, quick: bool = False) -> str:
    """A marker as an AbleSet section locator: "Verse 2" becomes "> Verse 2".

    Text already in AbleSet's notation goes through as written, so markers can
    carry flags and commands straight from the DAW ("> Chorus +LOOP",
    "SONG END", "* a note to self"). Only AbleSet's own capitals count as a
    command: a section a band calls "Stop" stays a section, never a STOP that
    halts the show.
    """
    t = text.strip()
    if t.startswith((">", "*")):
        return t
    if any(t == c or t.startswith(c + " ") for c in ABLESET_COMMANDS):
        return t
    prefix = ">>" if quick else ">"
    return f"{prefix} {t}" if t else prefix


def has_no_name(locator: str) -> bool:
    """A section locator with nothing but notation: ">", ">>", "> +LOOP", "> {notes} [red]".

    AbleSet 3.1.9 hangs Live while loading a set with a bare ">" locator (found
    2026-11-03: Live 12.4.16 stuck at full CPU before drawing the set; the same
    set opened at once in a Live without AbleSet). So none is ever written.
    """
    t = locator.strip()
    if not t.startswith(">"):
        return False
    return not song_title(t.lstrip(">"))


def _bar_lines(sigs: list[tuple[float, int, int]]):
    """Yield the beat of every bar line, forever. Each time signature change starts a new bar."""
    for i, (b, num, den) in enumerate(sigs):
        length = num * 4 / den
        stop = sigs[i + 1][0] if i + 1 < len(sigs) else math.inf
        k = 0
        while b + k * length < stop - 1e-9:
            yield b + k * length
            k += 1


def bar_at_or_after(sigs: list[tuple[float, int, int]], beat: float) -> float:
    for b in _bar_lines(sigs):
        if b >= beat - 1e-6:
            return b
    raise AssertionError("bar lines never end")


def bar_number(sigs: list[tuple[float, int, int]], beat: float, snap: float = 1e-6) -> tuple[int, float]:
    """(bar, beat within it), both counting from 1, beats in the time signature's own unit.

    snap: how close to a bar line counts as on it. Exact by default; what's said aloud
    uses where_text(), which reads a hair before a bar line as that bar, not "beat 5 of 4".
    """
    prev, bar = 0.0, 0
    for b in _bar_lines(sigs):
        if b > beat + snap:
            break
        prev, bar = b, bar + 1
    den = [s for s in sigs if s[0] <= prev + 1e-9][-1][2]
    return max(bar, 1), max(0.0, beat - prev) * den / 4 + 1


def where_text(sigs, beat: float, capital: bool = False) -> str:
    """'bar 12, beat 3.5', as said aloud: to two decimals, and a hair before a bar line is that bar."""
    bar, b = bar_number(sigs, beat, snap=0.01)
    text = f"bar {bar}" + (f", beat {round(b, 2):g}" if round(b, 2) > 1 else "")
    return text[0].upper() + text[1:] if capital else text


def _where(sigs, beat: float) -> str:
    return where_text(sigs, beat)


def _beats_words(beats: float, sigs, at: float) -> str:
    """How far past a point the music goes, in bars when it's whole bars, else beats."""
    (b0, x0), (b1, x1) = bar_number(sigs, at), bar_number(sigs, at + beats)
    if abs(x0 - 1) < 1e-6 and abs(x1 - 1) < 1e-6 and b1 > b0:
        return n(b1 - b0, "bar")
    den = [s for s in sigs if s[0] <= at + 1e-9][-1][2]
    return n(round(beats * den / 4), "beat")


def _seconds(tempos: list[tuple[float, float]], beat: float) -> float:
    """Seconds at a beat for a stepped tempo map (a MIDI file's)."""
    sec, last, bpm = 0.0, 0.0, tempos[0][1] if tempos else 120.0
    for b, v in tempos:
        if b > beat:
            break
        sec += (b - last) * 60 / bpm
        last, bpm = b, v
    return sec + (beat - last) * 60 / bpm


@dataclass
class TypedMarker:
    """A marker typed in by hand: a bar, a beat in it (1 is the downbeat, 2.5 halfway to 3), a name."""
    bar: int
    beat: float
    text: str

    def where(self) -> str:
        return f"bar {self.bar}" + (f", beat {self.beat:g}" if self.beat != 1 else "")


_MARKER_LINE = re.compile(
    r"""^\s*(?:bar\s*)?(?P<bar>\d+)                      # the bar
        (?:\s*(?:[.:]|,?\s*beat\s*)\s*(?P<beat>\d+(?:[.,]\d+)?)  # 17.3, 17:3, "17 beat 2.5"
           (?:\.(?P<frac>\d{1,2}))?)?                   # REAPER's 17.3.50 (hundredths of a beat)
        \s*(?:[,;:\t]|\s-\s|\s)\s*(?P<text>.*?)\s*$""", re.X | re.I)


def parse_markers(text: str) -> list[TypedMarker]:
    """Markers typed one per line: "17 Verse", "17.3 Pickup", "17:3 Pickup", "bar 33 beat 2.5 Hit",
    or REAPER's "17.3.50 Fill". As many as you like. Blank lines and lines starting with // are skipped.
    """
    out, bad = [], []
    for k, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.strip().startswith("//"):
            continue
        m = _MARKER_LINE.match(line.rstrip() + " ")
        if not m:
            bad.append(f"line {k} (\"{line.strip()}\") doesn't start with a bar number")
            continue
        bar = int(m.group("bar"))
        beat = float((m.group("beat") or "1").replace(",", "."))
        if m.group("frac"):
            beat += int(m.group("frac").ljust(2, "0")) / 100
        if bar < 1:
            bad.append(f"line {k}: bars start at 1")
            continue
        if beat < 1:
            bad.append(f"line {k}: beats start at 1, the downbeat")
            continue
        out.append(TypedMarker(bar, beat, m.group("text") or ""))
    if bad:
        raise SetError("Some markers couldn't be read: " + "; ".join(bad) + ". Write each one as a bar number, "
                       "optionally a dot and a beat, then its name, like 17 Verse or 33.3 Pickup")
    return out


def marker_line(m: TypedMarker) -> str:
    """A marker as one line of text, the way Edit as Text reads it back: 17 Verse, 33.3 Pickup."""
    if m.beat == 1:
        where = str(m.bar)
    elif float(m.beat).is_integer():
        where = f"{m.bar}.{int(m.beat)}"
    else:
        where = f"{m.bar} beat " + ("%.6f" % m.beat).rstrip("0").rstrip(".")
    if re.match(r"(beat\b|[,;:.\t-])", m.text, re.I):
        return f"{where} - {m.text}"  # a name that could read as a beat or a separator keeps its start
    return f"{where} {m.text}".rstrip()


def file_markers_typed(path: str, project: str | None = None) -> tuple[list[TypedMarker], list[str]]:
    """A MIDI file's own markers as bar and beat, so they can be edited like typed ones."""
    said: list[str] = []
    mf = _music(path, project, said)
    _, sigs = _file_maps(mf, said)
    out = []
    for tick, text in midi_markers(mf):
        bar, beat = bar_number(sigs, tick / mf.division)
        out.append(TypedMarker(bar, round(beat, 6), text))
    return out, said


def bar_start(sigs: list[tuple[float, int, int]], bar: int) -> float:
    for k, b in enumerate(_bar_lines(sigs), 1):
        if k == bar:
            return b
    raise AssertionError("bar lines never end")


def resolve_marker(sigs: list[tuple[float, int, int]], m: TypedMarker) -> float:
    """A typed marker's position in quarter notes, by the song's own time signatures."""
    start = bar_start(sigs, m.bar)
    num, den = [s for s in sigs if s[0] <= start + 1e-9][-1][1:]
    if m.beat >= num + 1:
        raise SetError(f"bar {m.bar} is in {num}/{den}, so it has no beat {m.beat:g} ({m.text or 'unnamed marker'})")
    at = start + (m.beat - 1) * 4 / den
    nxt = bar_start(sigs, m.bar + 1)
    if at >= nxt - 1e-9:  # a bar cut short by a time signature change partway through it
        short = round((nxt - start) * den / 4, 3)
        raise SetError(f"bar {m.bar} is only {short:g} beats long, so it has no beat {m.beat:g} "
                       f"({m.text or 'unnamed marker'})")
    return at


def _music(path, project, said):
    """A MIDI file read for its timing: the tempo fixes applied in memory, REAPER's short bars undone."""
    mf = read_midi(path)
    if project:
        mf, bars = match_project_bars(mf, project)
        if bars:
            said.append(f"Undid REAPER's {n(len(bars), 'padded short bar')} using {os.path.basename(project)}, "
                        "so every marker is where the project has it.")
    r = analyze(mf, os.path.basename(path))
    if r.tempo_needs_fix:
        mf, _ = repair(mf, notes=False)
        said.append("Read with the tempo map fixed first (" + ", ".join(problems(r, notes=False))
                    + "). Your file is unchanged.")
    if mf.smpte:
        said.append("This file counts time in video frames and has no tempo map, so the set runs at 60 BPM "
                    "and one beat is one second.")
    return mf


def _file_maps(mf, said):
    div = mf.division
    tempos: list[tuple[float, float]] = []
    for tick, bpm in _tempo_list(mf):
        if not MIN_BPM <= bpm <= MAX_BPM:
            clamped = min(max(bpm, MIN_BPM), MAX_BPM)
            said.append(f"Live's tempo runs from 20 to 999 BPM, so {bpm:g} BPM became {clamped:g}.")
            bpm = clamped
        b = tick / div
        if tempos and abs(tempos[-1][0] - b) < 1e-9:
            tempos[-1] = (b, bpm)  # the last tempo on a tick wins
        elif not tempos or abs(tempos[-1][1] - bpm) > 1e-9:
            tempos.append((b, bpm))
    if not tempos or tempos[0][0] > 0:
        tempos.insert(0, (0.0, 120.0))
    sigs: list[tuple[float, int, int]] = []
    for tick, num, den in time_signatures(mf):
        b = tick / div
        if den not in LIVE_DENOMINATORS or not 1 <= num <= 99:
            said.append(f"Live can't show {num}/{den}, so the time signature change there was left out.")
            continue
        if sigs and abs(sigs[-1][0] - b) < 1e-9:
            sigs[-1] = (b, num, den)
        elif not sigs or sigs[-1][1:] != (num, den):
            sigs.append((b, num, den))
    if not sigs or sigs[0][0] > 0:
        sigs.insert(0, (0.0, 4, 4))  # MIDI's default
    return tempos, sigs


def plan_song(path: str | None, name: str | None = None, quick: bool = False, song_end: bool = True,
              plain: bool = False, project: str | None = None, typed: list[TypedMarker] | None = None,
              file_markers: bool = True, tempo: float | None = None,
              signature: tuple[int, int] | None = None, set_sigs=None) -> SongPlan:
    """Work out a song: its locators, tempo map and time signatures.

    From a MIDI file, its markers and any typed ones; or, with no file, from
    typed markers alone at a given tempo and time signature. A typed marker at
    the same place as one of the file's takes its place.

    The tempo fixes this app makes are applied first, in memory (the file is
    never written), because a doubled tempo or a REAPER ramp note would put
    every later locator in the wrong place. With the REAPER project it was
    exported from, REAPER's padded short bars are undone too.
    """
    said: list[str] = []
    typed = list(typed or [])
    if path:
        mf = _music(path, project, said)
        tempos, sigs = _file_maps(mf, said)
        markers = [(e_tick / mf.division, text, "file") for e_tick, text in midi_markers(mf)] if file_markers else []
        # The music's end: the last note, controller or other channel event. Not End of Track, which some
        # apps put well after the music, and not tempo or marker events.
        last_tick = max((e.tick for events in mf.tracks for e in events if e.raw[0] < 0xF0), default=None)
        if last_tick is None:
            last_tick = max((e.tick for events in mf.tracks for e in events), default=0)
        music_end = bar_at_or_after(sigs, last_tick / mf.division)
    else:
        if not name:
            raise SetError("a song made from typed markers needs a name")
        bpm = 120.0 if tempo is None else tempo
        if not MIN_BPM <= bpm <= MAX_BPM:
            raise SetError("Live's tempo runs from 20 to 999 BPM")
        num, den = signature or (4, 4)
        if den not in LIVE_DENOMINATORS or not 1 <= num <= 99:
            raise SetError(f"Live can't use {num}/{den}; the bottom number can be 1, 2, 4, 8 or 16")
        tempos, sigs, markers, music_end = [(0.0, bpm)], set_sigs or [(0.0, num, den)], [], 0.0
    for m in typed:
        b = resolve_marker(sigs, m)
        clash = [x for x in markers if x[2] == "file" and abs(x[0] - b) < 1e-6]
        for x in clash:
            markers.remove(x)
            said.append(f"Your marker at {m.where()} ({m.text or 'unnamed'}) takes the place of the file's "
                        f"({x[1] or 'unnamed'}).")
        markers.append((b, m.text, "typed"))
    markers.sort(key=lambda x: x[0])
    if not path and not markers:
        raise SetError("there are no markers to make a song from")

    song = name or song_name_for(path)
    locs = [] if plain else [Locator(0.0, song, 0.0, "song")]
    end_locator = None
    final = markers[-1][0] if markers else None
    for i, (b, text, _) in enumerate(markers):
        last = final is not None and abs(b - final) < 1e-9  # Outro and End can share the last beat
        if not plain and last and text.lower() in END_WORDS:
            end_locator = Locator(b, "SONG END", _seconds(tempos, b), "end", text)
            continue
        locator = text if plain else section_name(text, quick)
        if plain and not locator:
            locator = "Marker"
        if not plain and has_no_name(locator):
            number = sum(1 for l in locs if l.kind == "section") + 1
            head = ">>" if locator.startswith(">>") else ">"
            locator = (f"{head} Section {number} " + locator.lstrip(">").strip()).strip()
            said.append(f"A marker at {_where(sigs, b)} has no name, so it's called Section {number}.")
        kind = "end" if locator.startswith("SONG END") else "section"
        loc = Locator(b, locator, _seconds(tempos, b), kind, text)
        if kind == "end":
            end_locator = loc
        else:
            locs.append(loc)

    last_marker = max((l.beat for l in locs), default=0.0)
    if end_locator:
        end_beat, from_marker = end_locator.beat, True
        if song_end:
            locs.append(end_locator)
    elif path:
        # Where the music ends: the last thing in the file, out to the end of its bar
        end_beat, from_marker = max(music_end, last_marker), False
        if song_end and not plain:
            locs.append(Locator(end_beat, "SONG END", _seconds(tempos, end_beat), "end"))
    else:
        end_beat, from_marker = bar_at_or_after(sigs, last_marker + 1e-3), False
        if not plain:
            said.append("No end was typed, so there's no SONG END: the song runs on to whatever comes next. "
                        "Type a marker called End where it stops.")
    span = max(end_beat, music_end)
    if from_marker and music_end > end_beat + 1e-6:
        said.append(f"The music goes on {_beats_words(music_end - end_beat, sigs, end_beat)} past the marker "
                    "where SONG END is. The tempo map runs to the end of the music.")
    if path and not markers:
        said.append("This file has no markers, so the song has no sections yet. You can type them in.")
    off = [l for l in locs if l.kind != "song" and abs(l.beat * 4 - round(l.beat * 4)) > 0.01]
    if off:
        said.append(f"{n(len(off), 'marker')} {'is' if len(off) == 1 else 'are'} between beats ("
                    + ", ".join(l.name for l in off) + "). AbleSet asks for locators on a beat, or Live's "
                    "jumps can land late; they were kept where they are.")
    plan = SongPlan(path or "", song, locs, tempos, sigs, end_beat, _seconds(tempos, end_beat), from_marker,
                    len(markers), span_beat=span, said=said)
    plan.typed, plan.options = typed, dict(quick=quick, song_end=song_end, plain=plain)
    return plan


def replan_on(plan: SongPlan, tempo: float, sigs: list[tuple[float, int, int]]) -> SongPlan:
    """A typed-only song worked out again on a set's own time signatures, bar 1 at the song's start."""
    return plan_song(None, plan.name, typed=plan.typed, tempo=tempo, set_sigs=sigs, **plan.options)


# ---------------------------------------------------------------- the Live set

def _num(x: float) -> str:
    """A number the way Live writes one: whole numbers bare, others in full."""
    return str(int(x)) if float(x).is_integer() and abs(x) < 1e15 else repr(float(x))


def sig_value(num: int, den: int) -> int:
    """Live's time signature as one number: 4/4 is 201, 3/4 is 200, 6/8 is 302."""
    return (num - 1) + 99 * int(math.log2(den))


def sig_from_value(v: int) -> tuple[int, int]:
    return v % 99 + 1, 2 ** (v // 99)


class LiveTempo:
    """A Live tempo envelope: straight lines in beats between points, a jump where two share a time."""

    def __init__(self, events: list[tuple[float, float]], manual: float):
        self.points = sorted(events, key=lambda p: p[0]) or [(PRE, manual)]
        pts = self.points
        segs = [(-math.inf, pts[0][1], pts[0][0], pts[0][1])]
        segs += [(t0, v0, t1, v1) for (t0, v0), (t1, v1) in zip(pts, pts[1:]) if t1 > t0]
        segs.append((pts[-1][0], pts[-1][1], math.inf, pts[-1][1]))
        self.segs = segs

    @staticmethod
    def _value(seg, x: float) -> float:
        t0, v0, t1, v1 = seg
        if math.isinf(t0) or math.isinf(t1):
            return v0
        return v0 + (v1 - v0) * (x - t0) / (t1 - t0)

    def bpm_at(self, beat: float) -> float:
        """The tempo just after a beat (after any jump there)."""
        for seg in self.segs:
            if seg[0] <= beat < seg[2]:
                return self._value(seg, beat)
        return self.segs[-1][1]

    def seconds(self, beat: float) -> float:
        """Seconds from the arrangement's start (beat 0) to a beat."""
        return self._span(0.0, beat) if beat >= 0 else -self._span(beat, 0.0)

    def _span(self, a: float, b: float) -> float:
        total = 0.0
        for seg in self.segs:
            x, y = max(a, seg[0]), min(b, seg[2])
            if y <= x:
                continue
            vx, vy = self._value(seg, x), self._value(seg, y)
            if abs(vy - vx) < 1e-9:
                total += (y - x) * 60 / vx
            else:
                total += 60 * (y - x) * math.log(vy / vx) / (vy - vx)
        return total

    def beat_at(self, seconds: float) -> float:
        lo, hi = 0.0, 64.0
        while self.seconds(hi) < seconds:
            hi *= 2
        for _ in range(80):
            mid = (lo + hi) / 2
            if self.seconds(mid) < seconds:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2


@dataclass
class SetLocator:
    id: int
    beat: float
    name: str
    xml: str


class LiveSet:
    """A Live set read as text, with the spans this module may change located."""

    def __init__(self, path: str, text: str, gz: bool):
        self.path, self.text, self.gz = path, text, gz
        m = re.search(r'<Ableton [^>]*Creator="([^"]*)"', text)
        if not m or "<LiveSet>" not in text:
            raise SetError(f"{os.path.basename(path)} isn't a Live set")
        self.creator = m.group(1)
        self._find()

    @classmethod
    def load(cls, path: str) -> "LiveSet":
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise SetError(f"couldn't open {os.path.basename(path)}: {e.strerror or e}")
        gz = raw[:2] == b"\x1f\x8b"
        try:
            data = gzip.decompress(raw) if gz else raw
            text = data.decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError):
            raise SetError(f"{os.path.basename(path)} isn't a Live set Live could open")
        return cls(path, text, gz)

    # -- locating the parts we touch

    def _find(self) -> None:
        t = self.text
        m = re.search(r"<(MainTrack|MasterTrack)[ >]", t)
        if not m:
            raise SetError("this set has no main track, which every Live set has")
        tag = m.group(1)
        end = t.find(f"</{tag}>", m.start())
        self.main = (m.start(), end)
        main = t[m.start():end]

        def target(param: str) -> str:
            k = main.find(f"<{param}>")
            mm = re.search(r'<AutomationTarget Id="(\d+)"', main[k:]) if k >= 0 else None
            if not mm:
                raise SetError(f"couldn't find the set's {param.lower()}")
            return mm.group(1)

        self.tempo_target, self.sig_target = target("Tempo"), target("TimeSignature")
        locs = [mm for mm in re.finditer(r"<Locators>\s*(<Locators\s*/>|<Locators>.*?</Locators>)", t, re.S)]
        if len(locs) != 1:
            raise SetError("couldn't find the set's locators")
        self.loc_span = locs[0].span(1)

    def _manual(self, param: str) -> tuple[tuple[int, int], str]:
        a, b = self.main
        k = self.text.find(f"<{param}>", a, b)
        mm = re.compile(r'<Manual Value="([^"]*)"').search(self.text, k, b)
        return mm.span(1), mm.group(1)

    def _envelope(self, target: str) -> tuple[int, int] | None:
        """The span of the Events list inside the main track's envelope for a target, if there is one."""
        a, b = self.main
        mm = re.compile(r'<PointeeId Value="%s"\s*/>' % target).search(self.text, a, b)
        if not mm:
            return None
        s = self.text.find("<Events", mm.end(), b)
        if self.text.startswith("<Events />", s) or self.text.startswith("<Events/>", s):
            return (s, s + self.text[s:].find(">") + 1)
        e = self.text.find("</Events>", s, b)
        return (s, e + len("</Events>"))

    # -- reading

    def locators(self) -> list[SetLocator]:
        s, e = self.loc_span
        out = []
        for mm in re.finditer(r'<Locator Id="(\d+)">(.*?)</Locator>', self.text[s:e], re.S):
            time = re.search(r'<Time Value="([^"]*)"', mm.group(2))
            name = re.search(r'<Name Value="([^"]*)"', mm.group(2))
            out.append(SetLocator(int(mm.group(1)), float(time.group(1)) if time else 0.0,
                                  html.unescape(name.group(1)) if name else "", mm.group(0)))
        return sorted(out, key=lambda l: l.beat)

    def _events(self, target: str) -> list[tuple[float, float]]:
        span = self._envelope(target)
        if not span:
            return []
        return [(float(t), float(v)) for t, v in
                re.findall(r'<(?:Float|Enum)Event Id="\d+" Time="([^"]+)" Value="([^"]+)"',
                           self.text[span[0]:span[1]])]

    def tempo(self) -> LiveTempo:
        return LiveTempo(self._events(self.tempo_target), float(self._manual("Tempo")[1]))

    def sigs(self) -> list[tuple[float, int, int]]:
        ev = self._events(self.sig_target) or [(PRE, float(self._manual("TimeSignature")[1]))]
        out: list[tuple[float, int, int]] = []
        for t, v in sorted(ev, key=lambda p: p[0]):
            num, den = sig_from_value(int(v))
            b = max(t, 0.0)
            if out and abs(out[-1][0] - b) < 1e-9:
                out[-1] = (b, num, den)
            elif not out or out[-1][1:] != (num, den):
                out.append((b, num, den))
        return out

    def content_end(self) -> float:
        """The last beat anything in the arrangement reaches: clips, locators, tempo changes."""
        end = 0.0
        for block in re.findall(r"<ArrangerAutomation>(.*?)</ArrangerAutomation>", self.text, re.S):
            for v in re.findall(r'<CurrentEnd Value="([^"]+)"', block):
                end = max(end, float(v))
        for loc in self.locators():
            end = max(end, loc.beat)
        for target in (self.tempo_target, self.sig_target):
            for t, _ in self._events(target):
                end = max(end, t)
        return end

    def has_sections_track(self) -> bool:
        names = re.findall(r'<(?:MidiTrack|AudioTrack)[^>]*>.*?<EffectiveName Value="([^"]*)"', self.text, re.S)
        return any(html.unescape(nm).strip().lower() == "sections" or "+SECTIONS" in nm for nm in names)

    # -- writing

    def write(self, path: str, locators: list[tuple[float, str]], tempo: list[tuple[float, float]] | None,
              sigs: list[tuple[float, int]] | None, manual_tempo: float | None = None,
              manual_sig: int | None = None) -> None:
        """Save a copy with the given locators and, if given, tempo and time signature events."""
        edits: list[tuple[int, int, str]] = [(*self.loc_span, self._locator_xml(locators))]
        for target, kind, events in ((self.tempo_target, "FloatEvent", tempo), (self.sig_target, "EnumEvent", sigs)):
            if events is None:
                continue
            if isinstance(events, tuple) and events[0] == "append":
                edits.append(self._envelope_append(target, kind, events[1]))
            else:
                edits.append(self._envelope_edit(target, kind, events))
        if manual_tempo is not None:
            edits.append((*self._manual("Tempo")[0], _num(manual_tempo)))
        if manual_sig is not None:
            edits.append((*self._manual("TimeSignature")[0], str(manual_sig)))
        text = self.text
        for s, e, new in sorted(edits, key=lambda x: x[0], reverse=True):
            text = text[:s] + new + text[e:]
        data = text.encode("utf-8")
        out = gzip.compress(data, compresslevel=6, mtime=0) if self.gz else data
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(out)
        os.replace(tmp, path)

    def _locator_xml(self, locators: list[tuple]) -> str:
        """The locator list. (beat, name, xml) keeps a locator already in the set exactly as it was,
        info text and all; (beat, name) is a new one, shaped like the set's own."""
        if not locators:
            return "<Locators />"
        existing = self.locators()
        model = existing[0].xml if existing else (
            '<Locator Id="0"><LomId Value="0" /><Time Value="0" /><Name Value="" />'
            '<Annotation Value="" /><IsSongStart Value="false" /></Locator>')
        next_id = max((l.id for l in existing), default=-1) + 1
        parts = []
        for loc in sorted(locators, key=lambda l: l[0]):
            if len(loc) > 2 and loc[2]:
                parts.append(loc[2])
                continue
            beat, name = loc[0], loc[1]
            x = re.sub(r'^<Locator Id="\d+">', f'<Locator Id="{next_id}">', model)
            next_id += 1
            x = re.sub(r'<Time Value="[^"]*"', f'<Time Value="{_num(beat)}"', x)
            x = re.sub(r'<Name Value="[^"]*"', lambda m: '<Name Value="%s"' % html.escape(name, quote=True), x)
            x = re.sub(r'<Annotation Value="[^"]*"', '<Annotation Value=""', x)
            x = re.sub(r'<IsSongStart Value="[^"]*"', '<IsSongStart Value="false"', x)
            parts.append(x)
        return "<Locators>" + "".join(parts) + "</Locators>"

    def _envelope_append(self, target: str, kind: str, events: list[tuple[float, float]]) -> tuple[int, int, str]:
        """New events added after the envelope's own, which stay exactly as written."""
        s, e = self._envelope(target)
        body = self.text[s:e]
        ids = [int(x) for x in re.findall(r'<(?:Float|Enum)Event Id="(\d+)"', body)]
        k = max(ids, default=-1) + 1
        new = "".join(f'<{kind} Id="{k + i}" Time="{_num(t)}" Value="{_num(v)}" />' for i, (t, v) in enumerate(events))
        close = body.rfind("</Events>")
        return (s + close, s + close, new)

    def _envelope_edit(self, target: str, kind: str, events: list[tuple[float, float]]) -> tuple[int, int, str]:
        body = "<Events>" + "".join(
            f'<{kind} Id="{i}" Time="{_num(t)}" Value="{_num(v)}" />' for i, (t, v) in enumerate(events)
        ) + "</Events>"
        span = self._envelope(target)
        if span:
            return (*span, body)
        # No envelope yet: add one to the main track's list
        a, b = self.main
        ids = [int(x) for x in re.findall(r'<AutomationEnvelope Id="(\d+)"', self.text[a:b])]
        env = (f'<AutomationEnvelope Id="{max(ids, default=-1) + 1}"><EnvelopeTarget><PointeeId Value="{target}" />'
               f"</EnvelopeTarget><Automation>{body}<AutomationTransformViewState>"
               '<IsTransformPending Value="false" /><TimeAndValueTransforms /></AutomationTransformViewState>'
               "</Automation></AutomationEnvelope>")
        k = self.text.find("<Envelopes", a, b)
        if k < 0:
            raise SetError("couldn't find where the set keeps its main track automation")
        close = self.text.find(">", k)
        if self.text[close - 1] == "/":  # <Envelopes />
            return (k, close + 1, f"<Envelopes>{env}</Envelopes>")
        return (close + 1, close + 1, env)


# ---------------------------------------------------------------- songs in a set (AbleSet's reading)

def is_song_locator(name: str, sections_track: bool = False) -> bool:
    t = name.strip()
    if not t or t.startswith((">", "*", "[")):
        return False
    if any(t == c or t.startswith(c + " ") for c in ABLESET_COMMANDS):
        return False
    if sections_track and t.isdigit():
        return False  # AbleSet's "Place Locators on Section Clips" names them 1, 2, 3
    return True


def song_title(locator: str) -> str:
    """A song locator without AbleSet's notation: ". Song [blue] {notes} #tag +FLAG" is "Song"."""
    t = locator.strip().lstrip(".").strip()
    t = re.sub(r"\{[^}]*\}|\[[^\]]*\]|#\S+|\+[A-Z]+(:\d+)?", "", t)
    return re.sub(r"\s+", " ", t).strip()


def section_clips(ls: LiveSet) -> list[tuple[float, str]]:
    """(beat, name) of every clip on AbleSet's Sections track, the other way songs get sections."""
    out = []
    for m in re.finditer(r"<MidiTrack[ >]", ls.text):
        end = ls.text.find("</MidiTrack>", m.start())
        track = ls.text[m.start():end]
        nm = re.search(r'<EffectiveName Value="([^"]*)"', track)
        name = html.unescape(nm.group(1)) if nm else ""
        if name.strip().lower() != "sections" and "+SECTIONS" not in name:
            continue
        for block in re.findall(r"<ArrangerAutomation>(.*?)</ArrangerAutomation>", track, re.S):
            for c in re.finditer(r'<MidiClip Id="\d+" Time="([^"]+)"', block):
                cn = re.search(r'<Name Value="([^"]*)"', block[c.end():])
                out.append((float(c.group(1)), html.unescape(cn.group(1)) if cn else ""))
    return sorted(out)


def describe_set(ls: LiveSet) -> list[str]:
    """Every song in a set and its sections, by bar, the way AbleSet will show them."""
    songs = set_songs(ls)
    sigs = ls.sigs()
    name = os.path.basename(ls.path)
    if not songs:
        return [f"{name} has no songs yet: no locators AbleSet reads as songs."]
    clips = section_clips(ls)
    lines = [f"{name}: {n(len(songs), 'song')}."]
    for s, end in songs:
        lines.append(f"{where_text(sigs, s.beat, capital=True)}: {song_title(s.name)}"
                     + ("" if song_title(s.name) == s.name else f" (locator: {s.name})"))
        here = [(l.beat, l.name) for l in ls.locators() if s.beat <= l.beat < end and l is not s
                and not is_song_locator(l.name)]
        here += [(b, f"{nm} (section clip)") for b, nm in clips if s.beat <= b < end]
        for b, nm in sorted(here):
            lines.append(f"  {where_text(sigs, b, capital=True)}: {nm}")
    return lines


def set_songs(ls: LiveSet) -> list[tuple[SetLocator, float]]:
    """Each song's locator and where it ends (the next song, or infinity)."""
    st = ls.has_sections_track()
    songs = [l for l in ls.locators() if is_song_locator(l.name, st)]
    return [(s, songs[i + 1].beat if i + 1 < len(songs) else math.inf) for i, s in enumerate(songs)]


# ---------------------------------------------------------------- putting it together

@dataclass
class Placed:
    plan: SongPlan
    start: float  # the song's first beat in the set
    locators: list[tuple[float, str]]  # (beat in the set, name) this song added
    line_up: str  # "tempo" (wrote its tempo map), "bars" or "time"
    left_out: list[Locator] = field(default_factory=list)
    replaced: int = 0  # section locators removed from a song already there
    gone: list = field(default_factory=list)  # (beat, name) of every locator taken out
    worst: float = 0.0  # how far the set's tempo and the file's disagree, seconds


@dataclass
class Result:
    path: str
    placed: list[Placed]
    said: list[str]


def _append(plan: SongPlan, at: float, tempo: list[tuple[float, float]], sigs: list[tuple[float, int]],
            prev_bpm: float) -> None:
    """Add a song's tempo and time signature events to envelope lists, starting at a beat."""
    for i, (b, bpm) in enumerate(plan.tempos):
        if i and b >= plan.span_beat - 1e-9:
            break  # anything after the song's music would run into the next song
        t = at + b
        before = prev_bpm if i == 0 else plan.tempos[i - 1][1]
        if t <= 0 and i == 0:
            tempo[:] = [(PRE, bpm)]
        else:
            tempo += [(t, before), (t, bpm)]
    for i, (b, num, den) in enumerate(plan.sigs):
        if i and b >= plan.span_beat - 1e-9:
            break
        t = at + b
        if t <= 0:
            sigs[:] = [(PRE, sig_value(num, den))]
        else:
            sigs.append((t, sig_value(num, den)))


def _tail(plan: SongPlan, at: float) -> list[tuple[float, str]]:
    """Where a song's music really ends, when that's after its SONG END: a note locator AbleSet
    ignores, so a song added later starts after the ringing tail, not inside it."""
    if plan.options.get("plain") or not plan.end_from_marker or plan.span_beat <= plan.end_beat + 1e-6:
        return []
    return [(at + plan.span_beat, f"* {plan.name}: the music ends here")]


def _verify(path: str, placed: list[Placed], wrote_tempo: bool) -> None:
    """Read the saved set back: the XML parses, every locator is there, and plays on time."""
    ls = LiveSet.load(path)
    try:
        ET.fromstring(ls.text)
    except ET.ParseError as e:
        raise SetError(f"the saved set didn't read back as a Live set ({e})")
    have = [(l.beat, l.name) for l in ls.locators()]
    if len({l.id for l in ls.locators()}) != len(have):
        raise SetError("the saved set has two locators with the same number")
    tm = ls.tempo()
    for p in placed:
        for beat, name in p.locators:
            if not any(abs(b - beat) < 1e-6 and nm == name for b, nm in have):
                raise SetError(f"the saved set is missing the locator {name}")
        if wrote_tempo and p.line_up == "tempo":
            zero = tm.seconds(p.start)
            for loc in p.plan.locators:
                if loc in p.left_out:
                    continue
                got = tm.seconds(p.start + loc.beat) - zero
                if abs(got - loc.seconds) > CHECK_SECONDS:
                    raise SetError(f"in the saved set, {loc.name} plays {abs(got - loc.seconds) * 1000:.0f} ms "
                                   "away from its marker")


def live_templates() -> list[tuple[str, str]]:
    """(name, path) of the default set inside each installed Live, newest first, betas last."""
    if sys.platform == "darwin":
        pats = ["/Applications/Ableton Live*.app/Contents/App-Resources/Builtin/Templates/DefaultLiveSet.als",
                os.path.expanduser("~/Applications/Ableton Live*.app/Contents/App-Resources/Builtin/Templates/"
                                   "DefaultLiveSet.als")]
    elif sys.platform == "win32":
        root = os.environ.get("ProgramData", r"C:\ProgramData")
        pats = [os.path.join(root, "Ableton", "Live*", "Resources", "Builtin", "Templates", "DefaultLiveSet.als")]
    else:
        pats = []
    found = []
    for pat in pats:
        for p in glob.glob(pat):
            m = (re.search(r"(Ableton Live [^/\\]*?)\.app[/\\]", p)  # Mac: /Applications/Ableton Live 12 Suite.app
                 or re.search(r"[/\\](Live [^/\\]+)[/\\]Resources", p))  # Windows: ...\Ableton\Live 12 Suite\Resources
            found.append((m.group(1) if m else "Ableton Live", p))

    def key(item):
        name = item[0]
        ver = [int(x) for x in re.findall(r"\d+", name)] or [0]
        return ("beta" in name.lower(), [-v for v in ver])

    return sorted(found, key=key)


def _unique(path: str) -> str:
    stem, ext = os.path.splitext(path)
    k, out = 2, path
    while os.path.exists(out):
        out = f"{stem} {k}{ext}"
        k += 1
    return out


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "-", name).strip(" .") or "Song"


def default_new_set_path(plan: SongPlan) -> str:
    """Live's own layout: "<Song> Project/<Song>.als", next to the MIDI file."""
    name = safe_name(plan.name)
    folder = os.path.join(os.path.dirname(os.path.abspath(plan.path)), f"{name} Project")
    return _unique(os.path.join(folder, f"{name}.als")) if os.path.isdir(folder) else os.path.join(folder, f"{name}.als")


def new_set(plans: list[SongPlan], out: str, template: str | None = None, dry_run: bool = False) -> Result:
    """A new Live set holding the songs back to back, each starting on a bar line."""
    if template is None:
        found = live_templates()
        if not found:
            raise SetError("couldn't find Ableton Live on this computer. Choose a Live set to start from instead")
        template = found[0][1]
    ls = LiveSet.load(template)
    said = []
    if ls.locators():
        said.append(f"The set it started from had {n(len(ls.locators()), 'locator')}; the new set has only these.")
    tempo: list[tuple[float, float]] = [(PRE, plans[0].tempos[0][1])]
    sigs: list[tuple[float, int]] = [(PRE, sig_value(*plans[0].sigs[0][1:]))]
    placed, at = [], 0.0
    cur_sigs = [(0.0, *plans[0].sigs[0][1:])]
    for p in plans:
        at = bar_at_or_after(cur_sigs, at)
        _append(p, at, tempo, sigs, tempo[-1][1])
        cur_sigs = [(max(t, 0.0), *sig_from_value(v)) for t, v in sigs]
        placed.append(Placed(p, at, [(at + l.beat, l.name) for l in p.locators] + _tail(p, at), "tempo"))
        at += p.span_beat
    if dry_run:
        return Result("", placed, said)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if os.path.exists(out):
        raise SetError(f"{os.path.basename(out)} already exists there")
    ls.write(out, [loc for pl in placed for loc in pl.locators], tempo, sigs,
             manual_tempo=plans[0].tempos[0][1], manual_sig=sig_value(*plans[0].sigs[0][1:]))
    try:
        _verify(out, placed, True)
    except SetError:
        os.remove(out)
        raise
    return Result(out, placed, said)


def add_to_set(set_path: str, plans: list[SongPlan], out: str | None = None, onto: str | None = None,
               line_up: str | None = None, in_place: bool = False, dry_run: bool = False,
               ls: "LiveSet | None" = None) -> Result:
    """Add songs to a set you already have.

    `onto` names a song already in the set (its locator, or just its title):
    that song's section locators are replaced and its tempo is left alone.
    Without it, each song goes after the last thing in the arrangement, on a
    bar line, with its own tempo map. `line_up` ("bars" or "time") is needed
    only when the set's tempo and the file's disagree; NeedsLineUp says so.
    """
    ls = ls or LiveSet.load(set_path)
    existing = ls.locators()
    st = ls.has_sections_track()
    said: list[str] = []
    keep = [(l.beat, l.name, l.xml) for l in existing]
    placed: list[Placed] = []
    tempo_events = ls._events(ls.tempo_target)
    sig_events = ls._events(ls.sig_target)
    wrote_tempo = False

    if onto is not None:
        if len(plans) != 1:
            raise SetError("one MIDI file at a time can go onto a song already in the set")
        p = plans[0]
        if not p.markers:
            raise SetError(f"{os.path.basename(p.path)} has no markers, so there are no sections to put on {onto}. "
                           "Type some in")
        match = [(s, end) for s, end in set_songs(ls) if s.name == onto] or \
                [(s, end) for s, end in set_songs(ls) if song_title(s.name).lower() == song_title(onto).lower()]
        if not match:
            raise SetError(f"there's no song called {onto} in this set")
        song, song_end = match[0]
        start = song.beat
        tm = ls.tempo()
        zero = tm.seconds(start)
        if not p.path:
            # Typed markers only: bar 1 is the song's start, counted in the set's own time signatures
            here = [x for x in ls.sigs() if x[0] <= start + 1e-9][-1]
            rel = [(0.0, *here[1:])] + [(b - start, num, den) for b, num, den in ls.sigs() if b > start + 1e-9]
            p = plans[0] = replan_on(p, tm.bpm_at(start), rel)
            p.said = [x for x in p.said if not x.startswith("No end was typed")]  # the set says where it ends
            for l in p.locators:
                l.seconds = tm.seconds(start + l.beat) - zero
        span = ls._envelope(ls.tempo_target)
        if span and "CurveControl" in ls.text[span[0]:span[1]]:
            said.append("The set's tempo automation has curves. Comparing it with the file's tempo, they're "
                        "counted as straight lines, so check how the markers line up by ear.")
        by_bars = [start + l.beat for l in p.locators]
        worst = max((abs((tm.seconds(b) - zero) - l.seconds) for b, l in zip(by_bars, p.locators)
                     if l.kind != "song"), default=0.0)
        if worst <= SAME_SECONDS:
            how, beats = "bars", by_bars
        else:
            if line_up not in ("bars", "time"):
                raise NeedsLineUp(
                    f"The set's tempo for {song_title(song.name)} isn't the MIDI file's: by its last marker they're "
                    f"{worst:.1f} seconds apart. Choose how the markers line up: by bar, if the set is sped up or "
                    "slowed down on purpose and the stems follow its tempo; or by time, if the stems aren't warped "
                    "and play at their own speed.", worst)
            how = line_up
            beats = by_bars if how == "bars" else [tm.beat_at(zero + l.seconds) for l in p.locators]
        new, left_out = [], []
        explicit_end = any(l.kind == "end" for l in p.locators) and p.end_from_marker
        set_end = [k[0] for k in keep if start < k[0] < song_end and k[1].startswith("SONG END")]
        limit = song_end if explicit_end or not set_end else set_end[0]
        for b, l in zip(beats, p.locators):
            if l.kind == "song" or (l.kind == "end" and not explicit_end and set_end):
                continue  # a guessed end is never better than the one the set already has
            if l.kind == "end" and explicit_end and abs(b - song_end) < 1e-6:
                continue  # the next song starts right there and ends this one anyway
            if b >= limit - 1e-6 and not (l.kind == "end" and explicit_end and b < song_end - 1e-6):
                if l.kind != "end" or explicit_end:  # a guessed end at the next song's start is no loss
                    left_out.append(l)
                continue
            if l.kind == "end" and not explicit_end and song_end != math.inf:
                continue  # the next song ends this one; a guessed end would only get in the way
            if l.kind == "end" and not explicit_end and any(
                    start < k[0] < song_end and k[1].startswith("SONG END") for k in keep):
                continue  # the set already says where this song ends
            new.append((b, l.name))

        def replaceable(beat: float, name: str) -> bool:
            if not start < beat < song_end and not (beat == start and name.startswith(">")):
                return False
            t = name.strip()
            if t.startswith(">") or (st and t.isdigit()):
                return True
            return explicit_end and t.startswith("SONG END")

        removed = [k for k in keep if replaceable(k[0], k[1])]
        keep = [k for k in keep if not replaceable(k[0], k[1])] + new
        pl = Placed(p, start, new, how, left_out, len([r for r in removed if r[1].strip().startswith(">")]), worst,
                    [(r[0], r[1]) for r in removed])
        placed.append(pl)
        if left_out:
            one = len(left_out) == 1
            said.append(f"{n(len(left_out), 'marker')} came after where {song_title(song.name)} ends in the set, so "
                        f"{'it was' if one else 'they were'} left out: "
                        + ", ".join(l.marker or l.name for l in left_out) + ".")
        if st:
            said.append("This set also has a Sections track. Where a section clip and a locator share a place, "
                        "AbleSet uses the locator.")
    else:
        tempo_now = sorted(tempo_events, key=lambda e: e[0]) or [(PRE, float(ls._manual("Tempo")[1]))]
        sig_now = sorted(sig_events, key=lambda e: e[0]) or [(PRE, float(ls._manual("TimeSignature")[1]))]
        tempo_list = [(t, v) for t, v in tempo_now]
        sig_list = [(t, int(v)) for t, v in sig_now]
        at = ls.content_end()
        for p in plans:
            cur_sigs = [(max(t, 0.0), *sig_from_value(v)) for t, v in sig_list]
            at = bar_at_or_after(cur_sigs, at)
            prev = LiveTempo(tempo_list, tempo_list[0][1]).bpm_at(at)
            _append(p, at, tempo_list, sig_list, prev)
            locs = [(at + l.beat, l.name) for l in p.locators] + _tail(p, at)
            keep += locs
            placed.append(Placed(p, at, locs, "tempo"))
            at += p.span_beat
        # The set's own tempo points stay exactly as written (curves and all); only new ones are added,
        # unless the set was empty and its starting tempo was replaced.
        t_new = tempo_list[len(tempo_now):] if tempo_list[:len(tempo_now)] == tempo_now else None
        s_new = sig_list[len(sig_now):] if sig_list[:len(sig_now)] == [(t, int(v)) for t, v in sig_now] else None
        tempo_events = ("append", t_new) if t_new is not None and tempo_events else tempo_list
        sig_events = ("append", s_new) if s_new is not None and sig_events else sig_list
        wrote_tempo = True

    if dry_run:
        return Result("", placed, said)
    if in_place:
        backup = _unique(os.path.splitext(set_path)[0] + " (before markers).als")
        with open(set_path, "rb") as f, open(backup, "wb") as g:
            g.write(f.read())
        target = set_path
        said.append(f"The set as it was is kept as {os.path.basename(backup)}.")
    else:
        target = out or _unique(os.path.splitext(set_path)[0] + " (markers added).als")
        if os.path.abspath(target) == os.path.abspath(set_path):
            raise SetError("that would save over the set itself; choose another name, or update it with a backup")
        if os.path.exists(target):
            raise SetError(f"{os.path.basename(target)} already exists there")
    tmp_check = target + ".check"
    ls.write(tmp_check, keep, tempo_events if wrote_tempo else None, sig_events if wrote_tempo else None)
    try:
        _verify(tmp_check, placed, wrote_tempo)
    except SetError:
        os.remove(tmp_check)
        raise
    os.replace(tmp_check, target)
    said.append("If this set is open in Live, close it without saving, then open the one saved here.")
    return Result(target, placed, said)


# ---------------------------------------------------------------- saying what happened

def clock(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def describe_plan(p: SongPlan) -> list[str]:
    """What a MIDI file will become, before anything is saved."""
    secs = len(p.sections)
    head = f"{p.name}: {n(secs, 'section')}, {clock(p.end_seconds)} long"
    tempo = (f"{p.tempos[0][1]:g} BPM" if len(p.tempos) == 1
             else f"{n(len(p.tempos) - 1, 'tempo change')}, starting at {p.tempos[0][1]:g} BPM")
    sig = "/".join(map(str, p.sigs[0][1:])) + (f", {n(len(p.sigs) - 1, 'time signature change')}"
                                               if len(p.sigs) > 1 else "")
    lines = [head + f". {tempo}, {sig}."]
    lines += p.said
    for loc in p.locators:
        where = where_text(p.sigs, loc.beat, capital=True)
        lines.append(f"{where}: {loc.name}" + (" (where the music ends)" if loc.kind == "end" and
                                                not p.end_from_marker else ""))
    return lines


def describe_result(r: Result) -> list[str]:
    name = os.path.basename(r.path)
    folder = os.path.basename(os.path.dirname(r.path))
    songs = len(r.placed)
    head = (f"Saved {name} in {folder}: " + (f"{r.placed[0].plan.name}, " if songs == 1 else f"{n(songs, 'song')}, ")
            + n(sum(len(p.locators) for p in r.placed), "locator") + ".")
    lines = [head]
    for p in r.placed:
        how = {"tempo": "with its tempo map", "bars": "by bar, on the set's tempo",
               "time": "by time, on the set's tempo"}[p.line_up]
        line = f"{p.plan.name}: {n(len(p.locators), 'locator')}, {how}"
        if p.replaced:
            line += f", replacing {n(p.replaced, 'section locator')} that were there"
        lines.append(line + ".")
        lines += p.plan.said
    return lines + r.said
