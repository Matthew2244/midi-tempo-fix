"""Note problems that make apps play the same MIDI file differently.

* Duplicate notes: the same key, channel and start tick twice on one track.
  Some apps play both (louder, phasey, or a flam), some merge them.
* Overlapping notes: a key struck again before its previous note ended. Apps
  disagree about which note-off ends which note, so lengths change between
  apps. Fixed by ending the earlier note where the new one starts.
* Stuck notes: a note that never ends. Some apps cut it at the end of the
  file, some hold it forever. Fixed by ending it at the end of its track.
* Stray note-offs: a note-off with no note playing. Harmless to most apps,
  confusing to some; removed.
* Zero-length notes: on and off on the same tick. Some apps skip them, some
  play a click. Reported only, because no length is right for every case.

Pairing rule when rebuilding: a note-off ends the earliest sounding note of
that key and channel (first in, first out), the most common behaviour.
Note starts, keys, channels and velocities are never changed.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import core


@dataclass
class NoteCounts:
    duplicates: int = 0
    overlaps: int = 0
    stuck: int = 0
    stray_offs: int = 0
    zero_length: int = 0

    @property
    def fixable(self) -> int:
        return self.duplicates + self.overlaps + self.stuck + self.stray_offs


def _is_on(e) -> bool:
    return (e.raw[0] & 0xF0) == 0x90 and e.raw[2] > 0


def _is_off(e) -> bool:
    k = e.raw[0] & 0xF0
    return e.raw[0] < 0xF0 and (k == 0x80 or (k == 0x90 and e.raw[2] == 0))


def analyze_notes(tracks) -> NoteCounts:
    c = NoteCounts()
    for events in tracks:
        sounding: dict[tuple[int, int], list[int]] = {}
        starts: set[tuple[int, int, int]] = set()
        for e in events:
            if e.raw[0] >= 0xF0:
                continue
            if _is_on(e):
                key = (e.raw[0] & 0x0F, e.raw[1])
                if (key[0], key[1], e.tick) in starts:
                    c.duplicates += 1
                elif sounding.get(key):
                    c.overlaps += 1
                starts.add((key[0], key[1], e.tick))
                sounding.setdefault(key, []).append(e.tick)
            elif _is_off(e):
                key = (e.raw[0] & 0x0F, e.raw[1])
                if sounding.get(key):
                    if sounding[key].pop(0) == e.tick:
                        c.zero_length += 1
                else:
                    c.stray_offs += 1
        c.stuck += sum(len(v) for v in sounding.values())
    return c


def repair_notes(tracks):
    """Rebuild only the tracks that have a fixable note problem."""
    total = NoteCounts()
    out = []
    for events in tracks:
        c = analyze_notes([events])
        total.zero_length += c.zero_length
        if not c.fixable:
            out.append(events)
            continue
        total.duplicates += c.duplicates
        total.overlaps += c.overlaps
        total.stuck += c.stuck
        total.stray_offs += c.stray_offs
        out.append(_rebuild(events))
    return out, total


def _rebuild(events):
    Event = core.Event
    others = []  # (tick, original order, event) for everything that is not a note
    notes = []  # [start, end, on_raw, off_raw]
    sounding = {}
    end_tick = max((e.tick for e in events), default=0)
    for order, e in enumerate(events):
        if e.raw[0] < 0xF0 and _is_on(e):
            notes.append([e.tick, None, e.raw, None])
            sounding.setdefault((e.raw[0] & 0x0F, e.raw[1]), []).append(notes[-1])
        elif e.raw[0] < 0xF0 and _is_off(e):
            waiting = sounding.get((e.raw[0] & 0x0F, e.raw[1]))
            if waiting:  # first in, first out; a note-off with nothing sounding is dropped
                n = waiting.pop(0)
                n[1], n[3] = e.tick, e.raw
        elif e.meta_type != 0x2F:
            others.append((e.tick, order, e))
    by_key = {}
    for n in notes:
        if n[1] is None:  # stuck: end it with the track
            n[1] = end_tick
        by_key.setdefault((n[2][0] & 0x0F, n[2][1]), []).append(n)
    kept = []
    for group in by_key.values():
        group.sort(key=lambda n: n[0])
        merged = []
        for n in group:
            if merged and merged[-1][0] == n[0]:  # duplicate: keep the first, as long as the longest
                if n[1] > merged[-1][1]:
                    merged[-1][1], merged[-1][3] = n[1], n[3]
                continue
            merged.append(n)
        for a, b in zip(merged, merged[1:]):  # overlap: end where the next strike begins
            if a[1] > b[0]:
                a[1] = b[0]
        kept += merged
    # At one tick: note-offs, then other events in their original order, then note-ons.
    rows = []
    for start, end, on, off in kept:
        off_event = Event(end, off or bytes([0x80 | (on[0] & 0x0F), on[1], 0]))
        rows.append((end, 3 if end == start else 0, 0, off_event))  # a zero-length note keeps on-then-off
        rows.append((start, 2, 0, Event(start, on)))
    rows += [(tick, 1, order, e) for tick, order, e in others]
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    rebuilt = [r[3] for r in rows]
    return rebuilt + [Event(max([e.tick for e in rebuilt] + [end_tick]), b"\xff\x2f\x00")]
