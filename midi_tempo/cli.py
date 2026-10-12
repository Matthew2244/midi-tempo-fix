"""midi-tempo: check and fix MIDI files so every app plays them the same.

    midi-tempo check FILE.mid [FILE.mid ...]   report the tempo map and any problems
    midi-tempo check A.mid --compare B.mid     also compare with another MIDI file (any app)
    midi-tempo check A.mid --compare X.RPP     ... or with a REAPER project
    midi-tempo check A.mid --compare mix.wav   ... or with a recording: does it line up, and if not, how
    midi-tempo check FOLDER --brief -r         one line per file, folders within folders too
    midi-tempo fix FILE.mid [FILE.mid ...]     save a cleaned copy next to each original
    midi-tempo fix A.mid --project X.RPP       ... also undo REAPER's padded short bars

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

from .batch import check_many, fix_many
from .core import (PROJECT_PROBLEM, MidiError, compare_with, describe, fix_file, midi_files, needs_project_fix,
                   problems, read_midi, summary, verdict)


def as_json(path: str, name: str, r, notes: bool, compare: list[str] | None, extra: tuple = ()) -> dict:
    c = r.notes
    found = problems(r, notes) + list(extra)
    out = {
        "file": path, "name": name, "clean": not found, "problems": found,
        "verdict": verdict(r, notes, extra), "tempo_events": r.tempo_count, "first_bpm": r.first_bpm,
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

    def label(path):
        root = next((f for f in folders if path.startswith(f)), None)
        return os.path.relpath(path, root) if root else os.path.basename(path)

    results, _ = check_many([(p, label(p)) for p in paths], workers=a.jobs)
    for path, r, err in results:
        name = label(path)
        if r is None:
            lines.append(err)
            records.append({"file": path, "name": name, "error": err})
            unreadable += 1
            status = 2
        else:
            try:
                cmp = compare_with(read_midi(path), a.compare) if a.compare else None
            except (MidiError, OSError, ValueError) as e:
                cmp = [f"Could not compare with {os.path.basename(a.compare)}: {e}."]
                status = 2
            padded = bool(a.compare) and a.compare.lower().endswith(".rpp") and needs_project_fix(path, a.compare)
            extra = (PROJECT_PROBLEM,) if padded else ()
            records.append(as_json(path, name, r, notes, cmp, extra))
            lines += [verdict(r, notes, extra)] if a.brief else describe(r, notes, extra)
            if cmp:
                lines += cmp
            if padded and not a.brief:
                lines.append(f'To fix: midi-tempo fix "{path}" --project "{a.compare}"')
            if r.tempo_needs_fix or (notes and r.notes.fixable) or padded:
                needs += 1
                status = max(status, 1)
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
    if a.output:
        try:
            lines += fix_file(paths[0], a.output, notes=not a.tempo_only, project=a.project)[1]
        except (MidiError, OSError) as e:
            lines.append(f"{os.path.basename(paths[0])}: not fixed, {e}.")
            status = 2
    else:
        results, _ = fix_many([(p, not a.tempo_only, a.project) for p in paths], workers=a.jobs)
        for path, out, said, perm in results:
            if perm is not None:
                said = [f"{os.path.basename(path)}: not fixed, no permission to save next to it ({perm})."]
            if said:
                lines += said
            if out is None and said and "nothing to fix" not in said[0]:
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
        help="What to compare timing with: a recording of the same music (WAV, AIFF, MP3 and more; needs numpy), "
             "another MIDI file from any app, or a REAPER project.",
    )
    c.add_argument("--tempo-only", action="store_true", help="Check the tempo map only, not the notes.")
    c.add_argument("--brief", action="store_true", help="One line per file: clean, or what needs fixing.")
    c.add_argument("--json", action="store_true", help="Machine-readable results, for scripts and other tools.")
    c.add_argument("-j", "--jobs", type=int, default=None,
                   help="How many processor cores to use. Default: all but one. 1 checks one file at a time.")
    c.add_argument("-r", "--recursive", action="store_true", help="Also look inside folders within folders.")
    f = sub.add_parser("fix", help="Save a cleaned copy next to each original.")
    f.add_argument("files", nargs="+", help="MIDI files, or folders of them. Never changed.")
    f.add_argument("-o", "--output", help="Where to save the cleaned copy (one file only).")
    f.add_argument(
        "--project", metavar="X.RPP",
        help="The REAPER project the file was exported from. Undoes the export's padded short bars, where a "
             "time signature starts partway through a bar, so the copy plays exactly where the project does.",
    )
    f.add_argument("--tempo-only", action="store_true", help="Fix the tempo map only and leave the notes alone.")
    f.add_argument("-r", "--recursive", action="store_true", help="Also look inside folders within folders.")
    f.add_argument("-j", "--jobs", type=int, default=None, help="How many processor cores to use.")
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
