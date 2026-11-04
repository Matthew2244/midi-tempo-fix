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
                   rpp_beat_at_seconds, rpp_seconds_at_beat, rpp_tempo_points, rpp_time_signatures, time_signatures)

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
    lyrics: list = field(default_factory=list)  # (beat, line) for AbleSet's lyrics track, from the song's start
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
    if is_rpp(path):
        return stem.strip() or "Song"  # a project's name is the song's; "and MIDI" in it isn't a file type
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


LYRIC, KAR_TEXT = 0x05, 0x01
SENTENCE_END = (".", "!", "?", ";", ":")


def _track_name(events) -> str:
    for e in events:
        if e.meta_type == 0x03:
            return e.meta_data().decode("utf-8", "replace").replace("\x00", "").strip()
    return ""


def midi_lyrics(mf) -> tuple[list[tuple[int, str, bool]], str, list[str]]:
    """One track's lyrics: (tick, syllable, a new line starts here), that track's name, and the names
    of other tracks that have lyrics too.

    Parts sung together (a lead and harmonies, or verses in a hymn) each carry their own lyrics on
    their own track, and mixed together they read "PaPaPatience". So the track with the most is
    used, and the rest are named. Karaoke (.kar) files put the words in text events, with / starting
    a new line, \\ a new verse and @ headers. Lyric events may end a line with a return or newline.
    """
    per_track = []
    for events in mf.tracks:
        lyric = [(e.tick, e.meta_data()) for e in events if e.meta_type == LYRIC]
        if not lyric:
            kar = [(e.tick, e.meta_data()) for e in events if e.meta_type == KAR_TEXT]
            if sum(1 for _, d in kar if d[:1] in (b"/", b"\\")) >= 2:
                lyric = [(tk, d) for tk, d in kar if not d.startswith(b"@") and HINT not in d]
        if lyric:
            per_track.append((len(lyric), _track_name(events), sorted(lyric, key=lambda x: x[0])))
    if not per_track:
        return [], "", []
    best = max(per_track, key=lambda x: x[0])
    others = [nm or "an unnamed track" for c, nm, ly in per_track if ly is not best[2]]
    out, new_line = [], False
    for tick, raw in best[2]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        if text[:1] in ("/", "\\"):
            new_line, text = True, text[1:]
        ends_line = text.endswith(("\r", "\n"))
        text = "".join(" " if ord(c) < 32 else c for c in text.rstrip("\r\n"))
        if text.strip() or text == " ":
            out.append((tick, text, new_line))
            new_line = False
        if ends_line:
            new_line = True
    return out, best[1], others


def lyric_lines(sylls: list[tuple[float, str, bool]], sigs, sections: list[float] = ()) -> tuple[list[tuple[float, str]], str]:
    """Join syllables into words and words into lines: (beat, line), and how words were found.

    Words: a file that marks word ends (a space after a word, or a hyphen inside one, as most apps
    and karaoke files do) is joined that way. One that marks nothing (LilyPond's exports) can't
    say where a word ends, so its syllables are kept apart by spaces, and the report says so.
    Lines: where the file starts one; otherwise after a sentence ends, after a comma followed by a
    rest, after a whole bar's rest, at a section, or once a line has run long and the singer breathes.
    """
    # Marked: a fair share of syllables say where words end. A stray space or two doesn't count.
    marks = sum(1 for _, t, _ in sylls if t != t.strip() or t.rstrip().endswith("-"))
    marked = marks >= max(2, len(sylls) // 5)
    words: list[tuple[float, str, bool]] = []  # (beat of its first syllable, word, line starts here)
    cur, start, nl = "", None, False
    for b, t, new_line in sylls:
        if start is None:
            start, nl = b, new_line
        elif new_line and cur:
            words.append((start, cur, nl))
            cur, start, nl = "", b, True
        if marked:
            piece = t.strip()
            hyphen = piece.endswith("-")
            cur += piece.rstrip("-")
            if not hyphen and (t.endswith(" ") or not t.strip()):
                if cur:
                    words.append((start, cur, nl))
                cur, start, nl = "", None, False
        else:
            words.append((b, t.strip(), new_line))
            cur, start, nl = "", None, False
    if cur:
        words.append((start, cur, nl))
    words = [w for w in words if w[1]]

    def bar_len(at):
        num, den = [x for x in sigs if x[0] <= at + 1e-9][-1][1:]
        return num * 4 / den

    lines: list[tuple[float, list[str]]] = []
    explicit = any(nl for _, _, nl in words[1:])
    for i, (b, w, nl) in enumerate(words):
        if not lines:
            lines.append((b, [w]))
            continue
        prev_b = words[i - 1][0]
        prev = lines[-1][1][-1]
        gap = b - prev_b
        brk = nl
        if not explicit:
            brk = (prev.endswith(SENTENCE_END) and gap >= 1
                   or prev.endswith(",") and gap >= 1.5
                   or gap >= bar_len(prev_b) + 1e-6
                   or any(prev_b < sct <= b + 1e-6 for sct in sections)
                   or marked and len(lines[-1][1]) >= 9 and gap >= 1)  # syllables aren't words: no length rule
        if brk:
            lines.append((b, [w]))
        else:
            lines[-1][1].append(w)
    how = "marked" if marked else "unmarked"
    return [(b, " ".join(ws)) for b, ws in lines], how


def file_lyrics_typed(path: str, project: str | None = None) -> tuple[list[TypedMarker], list[str]]:
    """A MIDI file's lyrics as lines at a bar and beat, so they can be read and edited like markers."""
    said: list[str] = []
    mf = _music(path, project, said)
    _, sigs = _file_maps(mf, said)
    raw, track, others = midi_lyrics(mf)
    if not raw:
        return [], ["This file has no lyrics. You can type them in."]
    sections = [tick / mf.division for tick, _ in midi_markers(mf)]
    lines, how = lyric_lines([(t / mf.division, x, nl) for t, x, nl in raw], sigs, sections)
    said = [f"{n(len(lines), 'lyric line')} from {('the track ' + track) if track else 'the file'}."]
    if others:
        said.append(f"Other tracks have lyrics too ({', '.join(others)}); only one track's are used.")
    if how == "unmarked":
        said.append("This file doesn't mark where words end, so syllables are kept apart by spaces. "
                    "Edit the lyrics to join them.")
    out = []
    for b, text in lines:
        bar, beat = bar_number(sigs, b)
        out.append(TypedMarker(bar, round(beat, 6), text))
    return out, said


def lyric_clips(plan: SongPlan, at: float, limit: float | None = None) -> list[tuple[float, float, str, int, int]]:
    """A song's lyric lines as clips in the set: each lasts until the next line, the last for a bar
    (or to where the music ends), and none past limit (the song's own end) so no line runs into the
    next song. Lines starting at or past it are left out and named in plan.said."""
    limit = plan.span_beat if limit is None else limit
    out, late = [], []
    for i, (b, text) in enumerate(plan.lyrics):
        if b >= limit - 1e-6:
            late.append(text)
            continue
        num, den = [x for x in plan.sigs if x[0] <= b + 1e-9][-1][1:]
        end = plan.lyrics[i + 1][0] if i + 1 < len(plan.lyrics) else max(b + num * 4 / den, min(
            plan.span_beat, b + 4 * num * 4 / den))
        out.append((at + b, at + min(end, limit), text, num, den))
    if late:
        note = (f"{n(len(late), 'lyric line')} came after where the song ends, so "
                f"{'it was' if len(late) == 1 else 'they were'} left out: " + ", ".join(late) + ".")
        if note not in plan.said:
            plan.said.append(note)
    return out


def file_markers_typed(path: str, project: str | None = None) -> tuple[list[TypedMarker], list[str]]:
    """A MIDI file's or REAPER project's own markers as bar and beat, so they can be edited like typed ones."""
    said: list[str] = []
    if is_rpp(path):
        _, sigs, markers, _ = _rpp_maps(path, said)
        found = [(b, text) for b, text, _ in markers]
    else:
        mf = _music(path, project, said)
        _, sigs = _file_maps(mf, said)
        found = [(tick / mf.division, text) for tick, text in midi_markers(mf)]
    out = []
    for b, text in found:
        bar, beat = bar_number(sigs, b)
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


def is_rpp(path: str) -> bool:
    """A REAPER project, including REAPER's own backup copies (.rpp-bak)."""
    return path.lower().endswith((".rpp", ".rpp-bak"))


_RPP_TOKEN = re.compile(r'"([^"]*)"|\'([^\']*)\'|`([^`]*)`|(\S+)')
STEP = 0.125  # quarter notes: a REAPER ramp becomes steps a 32nd note apart, as REAPER's own MIDI export does


def rpp_markers(path: str) -> list[tuple[float, str, bool]]:
    """(seconds, name, is a region) for every marker and region start in a REAPER project.

    A marker line is MARKER index seconds name flags ...; the name is quoted with ", ' or `
    when it has spaces. A region is two lines with the same index and flag 1, its start and
    its end; only the start is a section.
    """
    out, regions = [], set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            t = line.strip()
            if not t.startswith("MARKER "):
                continue
            tok = [next(g for g in m.groups() if g is not None) for m in _RPP_TOKEN.finditer(t)]
            if len(tok) < 4:
                continue
            try:
                idx, sec = tok[1], float(tok[2])
                flags = int(tok[4]) if len(tok) > 4 else 0
            except ValueError:
                continue
            region = bool(flags & 1)
            if region:
                if idx in regions:
                    continue  # the region's end
                regions.add(idx)
            out.append((sec, tok[3], region))
    return sorted(out, key=lambda m: m[0])


def rpp_items_end(path: str) -> float:
    """Seconds where the last item in a REAPER project ends."""
    end, pos = 0.0, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            t = line.split()
            if not t:
                continue
            if t[0] == "POSITION" and len(t) > 1:
                pos = float(t[1])
            elif t[0] == "LENGTH" and len(t) > 1 and pos is not None:
                end = max(end, pos + float(t[1]))
                pos = None
    return end


def _rpp_maps(path: str, said: list[str]):
    """A REAPER project's tempo map, time signatures, markers and end, in quarter notes from its start.

    REAPER ramps are linear in time and Live's are linear in beats, so a ramp can't be copied
    as one; it becomes steps a 32nd note apart, each at the average tempo over its step, so
    every step boundary lands exactly when REAPER plays it (REAPER's own MIDI export does the
    same, and matched REAPER to a quarter of a millisecond). =START and =END are REAPER's
    project start and end markers: the song starts and ends there.
    """
    def snap(b: float) -> float:
        """Seconds back to beats leaves noise like 118.9999999999995; on a 3840th of a beat it's that beat."""
        g = round(b * 3840) / 3840
        return g if abs(g - b) < 1e-6 else b

    if os.path.getsize(path) == 0:
        raise SetError(f"{os.path.basename(path)} is empty. If it's in Dropbox, iCloud or OneDrive, it may not be "
                       "downloaded to this computer yet")
    pts = rpp_tempo_points(path)
    if pts[0][0] > 1e-9:
        pts = [(0.0, pts[0][1], 1)] + pts  # the tempo before the first point is that point's
    beat_at = lambda t: rpp_beat_at_seconds(pts, t)  # noqa: E731
    sec_at = rpp_seconds_at_beat(pts)
    marks = rpp_markers(path)
    start_s = next((m[0] for m in marks if m[1].upper() == "=START"), 0.0)
    end_s = next((m[0] for m in marks if m[1].upper() == "=END"), None)
    offset = beat_at(start_s)
    if start_s:
        said.append(f"The song starts at the project's =START marker, {start_s:.2f} seconds in.")

    tempos: list[tuple[float, float]] = []
    for i, (t0, bpm, shape) in enumerate(pts):
        nxt = pts[i + 1] if i + 1 < len(pts) else None
        b0 = snap(beat_at(t0))
        if nxt and shape == 0 and abs(nxt[1] - bpm) > 1e-9:
            b1 = snap(beat_at(nxt[0]))
            b = b0
            while b < b1 - 1e-9:
                e = min(b + STEP, b1)
                tempos.append((b, 60 * (e - b) / (sec_at(e) - sec_at(b))))
                b = e
        else:
            tempos.append((b0, bpm))
    sig0, sig_pts = rpp_time_signatures(path)
    sigs = [(0.0, *sig0)] + [(snap(beat_at(t)), num, den) for t, num, den in sig_pts]

    def from_start(events):
        """Shift to the song's start; what's in force there becomes the first value."""
        before = [e for e in events if e[0] <= offset + 1e-9]
        after = [(e[0] - offset, *e[1:]) for e in events if e[0] > offset + 1e-9]
        return [(0.0, *before[-1][1:])] + after

    tempos, sigs = from_start(tempos), from_start(sigs)
    clean_t: list[tuple[float, float]] = []
    for b, bpm in tempos:
        if not MIN_BPM <= bpm <= MAX_BPM:
            said.append(f"Live's tempo runs from 20 to 999 BPM, so {bpm:g} BPM became "
                        f"{min(max(bpm, MIN_BPM), MAX_BPM):g}.")
            bpm = min(max(bpm, MIN_BPM), MAX_BPM)
        if clean_t and abs(clean_t[-1][0] - b) < 1e-9:
            clean_t[-1] = (b, bpm)
        elif not clean_t or abs(clean_t[-1][1] - bpm) > 1e-9:
            clean_t.append((b, bpm))
    clean_s: list[tuple[float, int, int]] = []
    for b, num, den in sigs:
        if den not in LIVE_DENOMINATORS or not 1 <= num <= 99:
            said.append(f"Live can't show {num}/{den}, so the time signature change there was left out.")
            continue
        if clean_s and abs(clean_s[-1][0] - b) < 1e-9:
            clean_s[-1] = (b, num, den)
        elif not clean_s or clean_s[-1][1:] != (num, den):
            clean_s.append((b, num, den))

    markers, after = [], []
    for sec, name, _ in marks:
        if name.upper() in ("=START", "=END") or sec < start_s - 1e-9:
            continue
        if end_s is not None and sec > end_s + 1e-9:
            after.append(name)  # past the project's end: not part of the song
            continue
        markers.append((snap(beat_at(sec) - offset), name, "file"))
    if after:
        said.append(f"{n(len(after), 'marker')} after the project's =END {'was' if len(after) == 1 else 'were'} "
                    "left out: " + ", ".join(after) + ".")
    if end_s is not None and end_s > start_s:
        markers.append((snap(beat_at(end_s) - offset), "SONG END", "file"))
    music_end = bar_at_or_after(clean_s, max(beat_at(rpp_items_end(path)) - offset, 0.0))
    return clean_t, clean_s, markers, music_end


def plan_song(path: str | None, name: str | None = None, quick: bool = False, song_end: bool = True,
              plain: bool = False, project: str | None = None, typed: list[TypedMarker] | None = None,
              file_markers: bool = True, tempo: float | None = None,
              signature: tuple[int, int] | None = None, set_sigs=None, lyrics=None) -> SongPlan:
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
    if path and is_rpp(path):
        tempos, sigs, markers, music_end = _rpp_maps(path, said)
        if not file_markers:
            markers = []
    elif path:
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
        said.append("This file has no markers, so the song has no sections yet.")
    off = [l for l in locs if l.kind != "song" and abs(l.beat * 4 - round(l.beat * 4)) > 0.01]
    if off:
        said.append(f"{n(len(off), 'marker')} {'is' if len(off) == 1 else 'are'} between beats ("
                    + ", ".join(l.name for l in off) + "). AbleSet asks for locators on a beat, or Live's "
                    "jumps can land late; they were kept where they are.")
    plan = SongPlan(path or "", song, locs, tempos, sigs, end_beat, _seconds(tempos, end_beat), from_marker,
                    len(markers), span_beat=span, said=said)
    plan.typed, plan.options = typed, dict(quick=quick, song_end=song_end, plain=plain)
    if lyrics == "file" and path and is_rpp(path):
        said.append("A REAPER project doesn't hold lyrics in a form that can be read, so there's no lyrics track. "
                    "Type them in instead.")
    if lyrics == "file" and path and not is_rpp(path):
        typed_lines, ly_said = file_lyrics_typed(path, project)
        plan.lyrics = [(resolve_marker(sigs, m), m.text) for m in typed_lines]
        said += ly_said
    elif lyrics and lyrics != "file":
        plan.lyrics = [(resolve_marker(sigs, m), m.text) for m in lyrics if m.text.strip()]
    plan.lyrics.sort(key=lambda x: x[0])
    if not path and plan.lyrics and not from_marker:
        # Typed in with no End: the song runs a bar past its last line, so no line is cut off
        last = plan.lyrics[-1][0]
        plan.span_beat = max(plan.span_beat, bar_at_or_after(sigs, bar_at_or_after(sigs, last + 1e-3) + 1e-3))
    plan.options["lyrics"] = lyrics
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


def _bez(a: float, b: float, u: float) -> float:
    """One axis of Live's automation curve: a cubic Bezier from 0 to 1 with control values a and b."""
    return 3 * (1 - u) ** 2 * u * a + 3 * (1 - u) * u * u * b + u ** 3


def _bez_d(a: float, b: float, u: float) -> float:
    return 3 * (1 - u) ** 2 * a + 6 * (1 - u) * u * (b - a) + 3 * u * u * (1 - b)


class LiveTempo:
    """A Live tempo envelope: lines in beats between points, a jump where two share a time, and a curve
    where a point carries CurveControl1X/1Y/2X/2Y.

    A curve is a cubic Bezier across its segment, time on one axis and tempo on the other, both
    0 to 1 from this point to the next. Proven against Live and AbleSet: see HANDOFF.
    """

    def __init__(self, events: list[tuple], manual: float):
        pts = sorted(((e[0], e[1], e[2] if len(e) > 2 else None) for e in events), key=lambda p: p[0])
        self.points = [(t, v) for t, v, _ in pts] or [(PRE, manual)]
        pts = pts or [(PRE, manual, None)]
        segs = [(-math.inf, pts[0][1], pts[0][0], pts[0][1], None)]
        segs += [(t0, v0, t1, v1, c) for (t0, v0, c), (t1, v1, _) in zip(pts, pts[1:]) if t1 > t0]
        segs.append((pts[-1][0], pts[-1][1], math.inf, pts[-1][1], None))
        self.segs = segs
        self.curved = any(sg[4] for sg in segs)
        self._whole: dict = {}

    @staticmethod
    def _u(seg, x: float) -> float:
        """Where along a curved segment's Bezier a beat falls."""
        t0, _, t1, _, c = seg
        frac = (x - t0) / (t1 - t0)
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if _bez(c[0], c[2], mid) < frac:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    @classmethod
    def _value(cls, seg, x: float) -> float:
        t0, v0, t1, v1, c = seg
        if math.isinf(t0) or math.isinf(t1):
            return v0
        if c:
            return v0 + (v1 - v0) * _bez(c[1], c[3], cls._u(seg, x))
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
            if seg[4]:
                if x == seg[0] and y == seg[2]:  # a whole curved segment: worked out once
                    if seg not in self._whole:
                        self._whole[seg] = self._curve_seconds(seg, x, y)
                    total += self._whole[seg]
                else:
                    total += self._curve_seconds(seg, x, y)
                continue
            vx, vy = self._value(seg, x), self._value(seg, y)
            if abs(vy - vx) < 1e-9:
                total += (y - x) * 60 / vx
            else:
                total += 60 * (y - x) * math.log(vy / vx) / (vy - vx)
        return total

    def _curve_seconds(self, seg, x: float, y: float, steps: int = 400) -> float:
        """Seconds across part of a curved segment: the integral of 60 / tempo over the beats,
        taken along the Bezier's own parameter (Simpson's rule; the curve is smooth)."""
        t0, v0, t1, v1, c = seg
        ua, ub = self._u(seg, x), self._u(seg, y)
        h = (ub - ua) / steps

        def f(u):
            return 60 * (t1 - t0) * _bez_d(c[0], c[2], u) / (v0 + (v1 - v0) * _bez(c[1], c[3], u))

        total = f(ua) + f(ub)
        for k in range(1, steps):
            total += (4 if k % 2 else 2) * f(ua + k * h)
        return total * h / 3

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
        """The tempo envelope, curves included."""
        span = self._envelope(self.tempo_target)
        events = []
        if span:
            for m in re.finditer(r"<FloatEvent ([^>]*)/>", self.text[span[0]:span[1]]):
                at = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
                curve = None
                if "CurveControl1X" in at:
                    curve = tuple(float(at[k]) for k in ("CurveControl1X", "CurveControl1Y",
                                                         "CurveControl2X", "CurveControl2Y"))
                events.append((float(at["Time"]), float(at["Value"]), curve))
        return LiveTempo(events, float(self._manual("Tempo")[1]))

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
              manual_sig: int | None = None, lyrics: "LyricsEdit | None" = None) -> None:
        """Save a copy with the given locators and, if given, tempo and time signature events and lyrics."""
        edits: list[tuple[int, int, str]] = [(*self.loc_span, self._locator_xml(locators))]
        if lyrics is not None:
            edits += self._lyrics_edits(lyrics)
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

    # -- lyrics: a MIDI track AbleSet reads, one clip per line, each clip named with its line

    def tracks(self) -> list[tuple[str, int, int, str]]:
        """(kind, start, end, name) of each track in the set's track list, in order."""
        out = []
        a = self.text.find("<Tracks>")
        z = self.text.find("</Tracks>", a)
        for m in re.compile(r"<(MidiTrack|AudioTrack|GroupTrack|ReturnTrack) Id=").finditer(self.text, a, z):
            if out and m.start() < out[-1][2]:
                continue  # inside the previous track
            end = self.text.find(f"</{m.group(1)}>", m.start()) + len(m.group(1)) + 3
            nm = re.compile(r'<EffectiveName Value="([^"]*)"').search(self.text, m.start(), end)
            out.append((m.group(1), m.start(), end, html.unescape(nm.group(1)) if nm else ""))
        return out

    def lyrics_track(self) -> tuple[int, int, str] | None:
        """The first MIDI track AbleSet reads as lyrics (its name has +LYRICS): (start, end, name)."""
        for kind, a, z, name in self.tracks():
            if kind == "MidiTrack" and "+LYRICS" in name:
                return a, z, name
        return None

    def _clip_events(self, a: int, z: int) -> tuple[int, int]:
        """The span of the arrangement clip list (MainSequencer) inside a MIDI track."""
        k = self.text.find("<MainSequencer", a, z)
        k = self.text.find("<ArrangerAutomation>", k, z)
        e = self.text.find("<Events", k, z)
        if k < 0 or e < 0:
            raise SetError("couldn't find where a MIDI track keeps its arrangement clips")
        if re.compile(r"<Events\s*/>").match(self.text, e):
            return e, self.text.find(">", e) + 1
        return e, _balanced_end(self.text, e, "Events")  # clips' own envelopes have Events lists inside

    def clip_model(self) -> str:
        """A MIDI clip to shape new ones on: one from this set if it has any, so the format matches the
        Live that saved it; else one from the installed Live's own sets; else a built-in one."""
        m = re.search(r'<MidiClip Id="\d+" Time="[^"]*">.*?</MidiClip>', self.text, re.S)
        return m.group(0) if m else installed_clip_model()

    def _lyrics_edits(self, ly: "LyricsEdit") -> list[tuple[int, int, str]]:
        edits = []
        found = self.lyrics_track()
        model = self.clip_model()
        if found:
            a, z, _ = found
            new_track = None
        else:
            midi = [t for t in self.tracks() if t[0] == "MidiTrack"]
            if not midi:
                raise SetError("this set has no MIDI track to make a lyrics track from. Add one MIDI track in "
                               "Live, save, and try again")
            src = self.text[midi[0][1]:midi[0][2]]
            if ly.rename_first and _is_empty_track(src):  # a new set: Live's own empty MIDI track
                a, z = midi[0][1], midi[0][2]
                new_track = None
                edits.append(self._rename_track(a, z, ly.track_name))
            else:
                new_track, next_id = _clone_track(src, self, ly.track_name)
                edits.append(_next_pointee_edit(self, next_id))
        if new_track is None:
            es, ee = self._clip_events(a, z)
            events = self.text[es:ee]
            kept = []
            for m in re.finditer(r'<MidiClip Id="(\d+)" Time="([^"]+)">.*?</MidiClip>', events, re.S):
                t = float(m.group(2))
                if ly.replace and ly.replace[0] - 1e-6 <= t < ly.replace[1] - 1e-6:
                    continue  # the song's old lyrics, replaced
                kept.append(m.group(0))
            ids = [int(x) for x in re.findall(r'<MidiClip Id="(\d+)"', events)]
            first = max(ids, default=-1) + 1
            clips = [_clip_xml(model, first + i, *c) for i, c in enumerate(ly.clips)]
            # In time order, as Live keeps them: clips listed out of order were in the file and didn't play
            every = sorted(kept + clips, key=lambda c: float(re.search(r'Time="([^"]+)"', c).group(1)))
            edits.append((es, ee, "<Events>" + "".join(every) + "</Events>"))
        else:
            track = new_track
            m = re.search(r"<MainSequencer.*?<ArrangerAutomation>\s*(<Events\s*/>|<Events>.*?</Events>)", track, re.S)
            clips = [_clip_xml(model, i, *c) for i, c in enumerate(sorted(ly.clips, key=lambda c: c[0]))]
            track = track[:m.start(1)] + "<Events>" + "".join(clips) + "</Events>" + track[m.end(1):]
            ret = [t for t in self.tracks() if t[0] == "ReturnTrack"]
            at = ret[0][1] if ret else self.text.find("</Tracks>")
            edits.append((at, at, track))
        return edits

    def _rename_track(self, a: int, z: int, name: str) -> tuple[int, int, str]:
        k = self.text.find("<Name>", a, z)
        e = self.text.find("</Name>", k, z)
        block = self.text[k:e]
        esc = html.escape(name, quote=True)
        block = re.sub(r'<EffectiveName Value="[^"]*"', f'<EffectiveName Value="{esc}"', block, count=1)
        block = re.sub(r'<UserName Value="[^"]*"', f'<UserName Value="{esc}"', block, count=1)
        return (k, e, block)

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


@dataclass
class LyricsEdit:
    clips: list[tuple[float, float, str, int, int]]  # (start beat, end beat, line, numerator, denominator)
    track_name: str = "Lyrics +LYRICS"
    replace: tuple[float, float] | None = None  # onto a song: its old lyric clips in this range go
    rename_first: bool = False  # a new set: Live's own empty MIDI track becomes the lyrics track


# A Live 12 arrangement MIDI clip with nothing in it, the last resort when neither the set nor the
# installed Live has a clip to copy the shape of.
BUILTIN_CLIP = (
    '<MidiClip Id="0" Time="0"><LomId Value="0" /><LomIdView Value="0" /><CurrentStart Value="0" />'
    '<CurrentEnd Value="4" /><Loop><LoopStart Value="0" /><LoopEnd Value="4" /><StartRelative Value="0" />'
    '<LoopOn Value="false" /><OutMarker Value="4" /><HiddenLoopStart Value="0" /><HiddenLoopEnd Value="4" />'
    '</Loop><Name Value="" /><Annotation Value="" /><Color Value="16" /><LaunchMode Value="0" />'
    '<LaunchQuantisation Value="0" /><TimeSignature><TimeSignatures><RemoteableTimeSignature Id="0">'
    '<Numerator Value="4" /><Denominator Value="4" /><Time Value="0" /></RemoteableTimeSignature>'
    '</TimeSignatures></TimeSignature><Envelopes><Envelopes /></Envelopes><ScrollerTimePreserver>'
    '<LeftTime Value="0" /><RightTime Value="4" /></ScrollerTimePreserver><TimeSelection>'
    '<AnchorTime Value="0" /><OtherTime Value="0" /></TimeSelection><Legato Value="false" />'
    '<Ram Value="false" /><GrooveSettings><GrooveId Value="-1" /></GrooveSettings><Disabled Value="false" />'
    '<VelocityAmount Value="0" /><FollowAction><FollowTime Value="4" /><IsLinked Value="true" />'
    '<LoopIterations Value="1" /><FollowActionA Value="4" /><FollowActionB Value="0" />'
    '<FollowChanceA Value="100" /><FollowChanceB Value="0" /><JumpIndexA Value="1" /><JumpIndexB Value="1" />'
    '<FollowActionEnabled Value="false" /></FollowAction><Grid><FixedNumerator Value="1" />'
    '<FixedDenominator Value="16" /><GridIntervalPixel Value="20" /><Ntoles Value="2" />'
    '<SnapToGrid Value="true" /><Fixed Value="false" /></Grid><FreezeStart Value="0" />'
    '<FreezeEnd Value="0" /><IsWarped Value="true" /><TakeId Value="1" /><IsInKey Value="true" />'
    '<ScaleInformation><Root Value="0" /><Name Value="0" /></ScaleInformation>'
    '<AutomationEnvelopesListWrapper LomId="0" /><Notes><KeyTracks /><PerNoteEventStore><EventLists />'
    '</PerNoteEventStore><NoteProbabilityGroups /><ProbabilityGroupIdGenerator><NextId Value="1" />'
    '</ProbabilityGroupIdGenerator><NoteIdGenerator><NextId Value="1" /></NoteIdGenerator></Notes>'
    '<BankSelectCoarse Value="-1" /><BankSelectFine Value="-1" /><ProgramChange Value="-1" />'
    '</MidiClip>')
_installed_clip: list = []


def installed_clip_model() -> str:
    """An arrangement MIDI clip from a set inside the installed Live, so its shape matches that Live."""
    if not _installed_clip:
        model = BUILTIN_CLIP
        for _, template in live_templates()[:1]:
            res = template.split("Builtin")[0]
            for path in sorted(glob.glob(os.path.join(res, "**", "*.als"), recursive=True))[:80]:
                try:
                    x = gzip.decompress(open(path, "rb").read()).decode("utf-8", "replace")
                except (OSError, EOFError):
                    continue
                m = re.search(r'<MidiClip Id="\d+" Time="[^"]*">.*?</MidiClip>', x, re.S)
                if m:
                    model = m.group(0)
                    break
        _installed_clip.append(model)
    return _installed_clip[0]


def _clip_xml(model: str, cid: int, start: float, end: float, name: str, num: int, den: int) -> str:
    """An empty arrangement MIDI clip from start to end, named with a lyric line."""
    length = max(end - start, 0.25)
    x = re.sub(r'^<MidiClip Id="\d+" Time="[^"]*">', f'<MidiClip Id="{cid}" Time="{_num(start)}">', model)
    for tag, val in (("CurrentStart", start), ("CurrentEnd", start + length), ("LoopStart", 0),
                     ("LoopEnd", length), ("StartRelative", 0), ("OutMarker", length),
                     ("HiddenLoopStart", 0), ("HiddenLoopEnd", length)):
        x = re.sub(rf'<{tag} Value="[^"]*"', f'<{tag} Value="{_num(val)}"', x, count=1)
    x = re.sub(r'<LoopOn Value="[^"]*"', '<LoopOn Value="false"', x, count=1)
    x = re.sub(r'<Name Value="[^"]*"', lambda m: '<Name Value="%s"' % html.escape(name, quote=True), x, count=1)
    x = re.sub(r'<Annotation Value="[^"]*"', '<Annotation Value=""', x, count=1)
    x = re.sub(r'<Numerator Value="[^"]*"', f'<Numerator Value="{num}"', x, count=1)
    x = re.sub(r'<Denominator Value="[^"]*"', f'<Denominator Value="{den}"', x, count=1)
    x = re.sub(r"<Envelopes>\s*(<Envelopes\s*/>|<Envelopes>.*?</Envelopes>)\s*</Envelopes>",
               "<Envelopes><Envelopes /></Envelopes>", x, count=1, flags=re.S)
    x = re.sub(r"<KeyTracks>.*?</KeyTracks>", "<KeyTracks />", x, count=1, flags=re.S)
    x = re.sub(r"<EventLists>.*?</EventLists>", "<EventLists />", x, count=1, flags=re.S)
    x = re.sub(r'<RightTime Value="[^"]*"', f'<RightTime Value="{_num(length)}"', x, count=1)
    x = re.sub(r'<AnchorTime Value="[^"]*"', '<AnchorTime Value="0"', x, count=1)
    x = re.sub(r'<OtherTime Value="[^"]*"', '<OtherTime Value="0"', x, count=1)
    x = re.sub(r'<Disabled Value="[^"]*"', '<Disabled Value="false"', x, count=1)
    return x


def _clone_track(src: str, ls: "LiveSet", name: str) -> tuple[str, int]:
    """A copy of one of the set's own MIDI tracks, emptied, for a new lyrics track.

    Copying a track of this very set keeps everything Live expects in place (one send per return
    track, the format of the Live that saved it). Emptied: no clips, no devices, no automation.
    Every id Live points at (automation and modulation targets, pointees) is new, numbered from
    the set's NextPointeeId, and the track gets the next track id.
    """
    t = _empty_all(src, "<ArrangerAutomation>", "Events")
    t = _empty_all(t, "<TakeLanes>", "TakeLanes")  # take-lane clips, and their envelopes pointing into this track
    t = re.sub(r'<Freeze Value="true"', '<Freeze Value="false"', t)
    t = re.sub(r'<SelectedDevice Value="\d+"', '<SelectedDevice Value="0"', t)  # no devices left to point at
    t = re.sub(r"<ClipSlot>\s*<Value>.*?</Value>\s*</ClipSlot>", "<ClipSlot><Value /></ClipSlot>", t, flags=re.S)
    k = t.find("<DeviceChain>")
    k = t.find("<DeviceChain>", k + 1)  # the track's own device chain, inside its mixer chain
    d = t.find("<Devices", k)
    if d >= 0:
        if re.compile(r"<Devices\s*/>").match(t, d):
            pass
        else:
            end = _balanced_end(t, d, "Devices")
            t = t[:d] + "<Devices />" + t[end:]
    t = re.sub(r"(<AutomationEnvelopes>\s*)(<Envelopes\s*/>|<Envelopes>.*?</Envelopes>)", r"\1<Envelopes />", t,
               count=1, flags=re.S)
    esc = html.escape(name, quote=True)
    t = re.sub(r'<EffectiveName Value="[^"]*"', f'<EffectiveName Value="{esc}"', t, count=1)
    t = re.sub(r'<UserName Value="[^"]*"', f'<UserName Value="{esc}"', t, count=1)
    t = re.sub(r'<TrackGroupId Value="[^"]*"', '<TrackGroupId Value="-1"', t, count=1)
    track_ids = [int(x) for x in re.findall(r'<(?:MidiTrack|AudioTrack|GroupTrack|ReturnTrack) Id="(\d+)"', ls.text)]
    t = re.sub(r'^<MidiTrack Id="\d+"', f'<MidiTrack Id="{max(track_ids, default=0) + 1}"', t)
    next_id = int(re.search(r'<NextPointeeId Value="(\d+)"', ls.text).group(1))
    counter = [next_id]

    def renumber(m):
        counter[0] += 1
        return f'<{m.group(1)} Id="{counter[0] - 1}"'

    t = re.sub(POINTEE, renumber, t)
    return t, counter[0]


# Every element Live points at by number, and so whose number must be unique in the whole set:
# automation and modulation targets of every kind, MIDI controller targets (ControllerTargets.0 to
# .130 on each MIDI track: Live called a set reusing them "corrupt (non-unique Pointee IDs)"), and
# pointees.
POINTEE = re.compile(r'<([\w.]*Target[\w.]*|Pointee) Id="(\d+)"')


def _is_empty_track(t: str) -> bool:
    """No clips anywhere and no devices: safe to rename into a lyrics track."""
    if "<MidiClip " in t:
        return False
    k = t.find("<DeviceChain>", t.find("<DeviceChain>") + 1)
    return bool(re.compile(r"<Devices\s*/>").match(t, t.find("<Devices", k))) if k >= 0 else False


def _empty_all(t: str, opener: str, tag: str) -> str:
    """Every <tag> list right after opener emptied to <tag />, matched by nesting, not by the first
    closing tag (a clip's envelope has its own Events list inside)."""
    out, pos = [], 0
    for m in re.finditer(re.escape(opener) + r"\s*", t):
        if m.start() < pos:
            continue
        e = m.end()
        if not t.startswith(f"<{tag}", e) or re.compile(rf"<{tag}\s*/>").match(t, e):
            continue
        end = _balanced_end(t, e, tag)
        out.append((e, end))
        pos = end
    for a, z in reversed(out):
        t = t[:a] + f"<{tag} />" + t[z:]
    return t


def _next_pointee_edit(ls: "LiveSet", value: int) -> tuple[int, int, str]:
    m = re.search(r'<NextPointeeId Value="(\d+)"', ls.text)
    return (m.start(1), m.end(1), str(value))


def _balanced_end(text: str, start: int, tag: str) -> int:
    """Where the element opening at start closes, counting nested ones of the same name."""
    depth, i = 0, start
    pat = re.compile(rf"<(/?){tag}(\s[^>]*?)?(/?)>")  # non-greedy, or "<Devices />" reads as an opening
    for m in pat.finditer(text, start):
        if m.group(1):
            depth -= 1
        elif not m.group(3):
            depth += 1
        if depth == 0:
            return m.end()
    raise SetError(f"the set's {tag} list doesn't close")


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
    lyric_clips: list = field(default_factory=list)  # (start, end, line, num, den) in the set
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
    want = [c for p in placed for c in p.lyric_clips]
    if want:
        found = ls.lyrics_track()
        if not found:
            raise SetError("the saved set has no lyrics track")
        es, ee = ls._clip_events(found[0], found[1])
        have_clips = [(float(t), html.unescape(nm)) for t, nm in re.findall(
            r'<MidiClip Id="\d+" Time="([^"]+)">.*?<Name Value="([^"]*)"', ls.text[es:ee], re.S)]
        for a, _, text, _, _ in want:
            if not any(abs(t - a) < 1e-6 and nm == text for t, nm in have_clips):
                raise SetError(f"the saved set is missing the lyric line {text}")
        ids = [m.group(2) for m in re.finditer(POINTEE, ls.text)]
        if len(ids) != len(set(ids)):
            raise SetError("the saved set has two automation targets with the same number")
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
        pl = Placed(p, at, [(at + l.beat, l.name) for l in p.locators] + _tail(p, at), "tempo")
        pl.lyric_clips = lyric_clips(p, at)
        placed.append(pl)
        at += p.span_beat
    if dry_run:
        return Result("", placed, said)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if os.path.exists(out):
        raise SetError(f"{os.path.basename(out)} already exists there")
    clips = [c for pl in placed for c in pl.lyric_clips]
    ly = LyricsEdit(clips, rename_first=True) if clips else None
    ls.write(out, [loc for pl in placed for loc in pl.locators], tempo, sigs,
             manual_tempo=plans[0].tempos[0][1], manual_sig=sig_value(*plans[0].sigs[0][1:]), lyrics=ly)
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
        if not p.markers and not p.lyrics:
            raise SetError(f"{os.path.basename(p.path)} has no markers or lyrics to put on {onto}. "
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
        pl = Placed(p, start, new, how, left_out=left_out, worst=worst,
                    replaced=len([r for r in removed if r[1].strip().startswith(">")]),
                    gone=[(r[0], r[1]) for r in removed])
        if p.lyrics:
            # by bar, lines sit where the file has them; by time, each moves to its moment in the set
            def place(b):
                return start + b if how == "bars" else tm.beat_at(zero + _seconds(p.tempos, b))
            # the song ends where the locators say: its SONG END in the set, else the next song
            lim = limit if limit != math.inf else max(place(p.span_beat), place(p.end_beat))
            clips, late = [], []
            for a, e, text, nm, dn in lyric_clips(p, 0.0, limit=math.inf):
                if place(a) >= lim - 1e-6:
                    late.append(text)
                    continue
                clips.append((place(a), min(place(e), lim), text, nm, dn))
            pl.lyric_clips = clips
            pl.lyric_range = (start, song_end)
            if late:
                said.append(f"{n(len(late), 'lyric line')} came after where {song_title(song.name)} ends in the "
                            f"set, so {'it was' if len(late) == 1 else 'they were'} left out: "
                            + ", ".join(late) + ".")
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
            pl = Placed(p, at, locs, "tempo")
            pl.lyric_clips = lyric_clips(p, at)
            placed.append(pl)
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
    clips = [c for pl in placed for c in pl.lyric_clips]
    ly = None
    if clips:
        rng = getattr(placed[0], "lyric_range", None) if onto is not None else None
        ly = LyricsEdit(clips, replace=rng)
        found = ls.lyrics_track()
        said.append(f"The lyrics go on {'the track ' + found[2] if found else 'a new track, Lyrics +LYRICS'}"
                    + (", replacing the song's lines there." if rng and found else "."))
    ls.write(tmp_check, keep, tempo_events if wrote_tempo else None, sig_events if wrote_tempo else None,
             lyrics=ly)
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
    if p.lyrics:
        lines.append(f"{n(len(p.lyrics), 'lyric line')} for AbleSet's lyrics track, listed after the locators.")
    for loc in p.locators:
        where = where_text(p.sigs, loc.beat, capital=True)
        lines.append(f"{where}: {loc.name}" + (" (where the music ends)" if loc.kind == "end" and
                                                not p.end_from_marker else ""))
    if p.lyrics:
        lines.append("Lyrics:")
        lines += [f"  {where_text(p.sigs, b, capital=True)}: {text}" for b, text in p.lyrics]
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
        if p.lyric_clips:
            line += f", and {n(len(p.lyric_clips), 'lyric line')} on the lyrics track"
        lines.append(line + ".")
        lines += p.plan.said
    return lines + r.said
