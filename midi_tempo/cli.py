"""midi-tempo: check and fix MIDI files so every app plays them the same.

    midi-tempo check FILE.mid [FILE.mid ...]   report the tempo map and any problems
    midi-tempo check A.mid --compare B.mid     also compare with another MIDI file (any app)
    midi-tempo check A.mid --compare X.RPP     ... or with a REAPER project
    midi-tempo check FOLDER --brief -r         one line per file, folders within folders too
    midi-tempo fix FILE.mid [FILE.mid ...]     save a cleaned copy next to each original

Checks the tempo map (doubled tempos, REAPER ramp notes, tempo on the wrong
track) and the notes (duplicates, overlaps, stuck notes, stray releases).
--tempo-only limits either command to the tempo map.

Originals are never changed. Every command builds its answer and prints it once,
so a screen reader is not interrupted mid-sentence.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
import json

from .core import (MidiError, analyze, compare_with, describe, fix_file, midi_files, problems, read_midi,
                   summary, verdict)


def as_json(path: str, name: str, r, notes: bool, compare: list[str] | None) -> dict:
    c = r.notes
    out = {
        "file": path, "name": name, "clean": not problems(r, notes), "problems": problems(r, notes),
        "verdict": verdict(r, notes), "tempo_events": r.tempo_count, "first_bpm": r.first_bpm,
        "length_seconds": round(r.length_seconds, 3), "ramps": len(r.ramps),
        "doubled_tempos": [{"tick": d.tick, "lost_bpm": round(d.lost_bpm, 4), "winning_bpm": round(d.winning_bpm, 4)}
                           for d in r.doubled],
        "reaper_ramp_notes": r.hints, "misplaced_tempo_events": r.misplaced, "smpte_fps": r.smpte,
        "notes": {"duplicates": c.duplicates, "overlaps": c.overlaps, "stuck": c.stuck,
                  "stray_releases": c.stray_offs, "zero_length": c.zero_length},
    }
    if compare is not None:
        out["compare"] = compare
    return out


def cmd_check(a) -> int:
    paths = midi_files(a.files, a.recursive)
    if not paths:
        print("No MIDI files found there.")
        return 2
    lines, status, needs, unreadable, records = [], 0, 0, 0, []
    notes = not a.tempo_only
    folders = [f.rstrip(os.sep) + os.sep for f in a.files if os.path.isdir(f)]
    for path in paths:
        root = next((f for f in folders if path.startswith(f)), None)
        name = os.path.relpath(path, root) if root else os.path.basename(path)
        try:
            mf = read_midi(path)
            r = analyze(mf, name)
            cmp = compare_with(mf, a.compare) if a.compare else None
            records.append(as_json(path, name, r, notes, cmp))
            lines += [verdict(r, notes)] if a.brief else describe(r, notes)
            if cmp:
                lines += cmp
            if r.tempo_needs_fix or (notes and r.notes.fixable):
                needs += 1
                status = max(status, 1)
        except (MidiError, OSError) as e:
            lines.append(f"{name}: could not check it, {e}.")
            records.append({"file": path, "name": name, "error": str(e)})
            unreadable += 1
            status = 2
        if not a.brief:
            lines.append("")
    if a.json:
        print(json.dumps({"checked": len(paths), "need_fixing": needs, "unreadable": unreadable,
                          "files": records}, indent=2, ensure_ascii=False))
        return status
    if len(paths) > 1:
        lines = [summary(len(paths), needs, unreadable), ""] + lines
    print("\n".join(lines).rstrip())
    return status


def cmd_fix(a) -> int:
    paths = midi_files(a.files, a.recursive)
    if not paths:
        print("No MIDI files found there.")
        return 2
    if a.output and len(paths) != 1:
        print("--output works with one file at a time.")
        return 2
    lines, status = [], 0
    for path in paths:
        try:
            _, said = fix_file(path, a.output, notes=not a.tempo_only)
            lines += said
        except (MidiError, OSError) as e:
            lines.append(f"{os.path.basename(path)}: not fixed, {e}.")
            status = 2
    print("\n".join(lines))
    return status


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="midi-tempo",
        description="Check and fix MIDI files so every app plays them the same.",
    )
    p.add_argument("--version", action="version", version=f"midi-tempo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="Report the tempo map and any problems. Changes nothing.")
    c.add_argument("files", nargs="+", help="MIDI files, or folders of them.")
    c.add_argument(
        "--compare", "--project", metavar="FILE",
        help="Another MIDI file of the same music (from any app), or a REAPER project, to compare timing with.",
    )
    c.add_argument("--tempo-only", action="store_true", help="Check the tempo map only, not the notes.")
    c.add_argument("--brief", action="store_true", help="One line per file: clean, or what needs fixing.")
    c.add_argument("--json", action="store_true", help="Machine-readable results, for scripts and other tools.")
    c.add_argument("-r", "--recursive", action="store_true", help="Also look inside folders within folders.")
    f = sub.add_parser("fix", help="Save a cleaned copy next to each original.")
    f.add_argument("files", nargs="+", help="MIDI files, or folders of them. Never changed.")
    f.add_argument("-o", "--output", help="Where to save the cleaned copy (one file only).")
    f.add_argument("--tempo-only", action="store_true", help="Fix the tempo map only and leave the notes alone.")
    f.add_argument("-r", "--recursive", action="store_true", help="Also look inside folders within folders.")
    a = p.parse_args(argv)
    return cmd_check(a) if a.cmd == "check" else cmd_fix(a)


def run() -> int:
    """Entry point: like main(), but a closed pipe (| head) ends quietly instead of with a traceback."""
    try:
        return main()
    except BrokenPipeError:
        sys.stderr.close()
        return 0


if __name__ == "__main__":
    sys.exit(run())
