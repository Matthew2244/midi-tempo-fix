"""midi-tempo: check and fix MIDI files so every app plays them the same.

    midi-tempo check FILE.mid [FILE.mid ...]   report the tempo map and any problems
    midi-tempo check FILE.mid --project X.RPP  also compare with a REAPER project
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
from .core import MidiError, analyze, compare_project, describe, fix_file, read_midi


def _files(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith((".mid", ".midi", ".smf", ".rmi")) and "(tempo fixed)" not in name:
                    out.append(os.path.join(p, name))
        else:
            out.append(p)
    return out


def cmd_check(a) -> int:
    lines, status = [], 0
    for path in _files(a.files):
        name = os.path.basename(path)
        try:
            mf = read_midi(path)
            r = analyze(mf, name)
            lines += describe(r, notes=not a.tempo_only)
            if a.project:
                lines += compare_project(mf, a.project)
            if r.tempo_needs_fix or (not a.tempo_only and r.notes.fixable):
                status = 1
        except MidiError as e:
            lines.append(f"{name}: could not check it, {e}.")
            status = 2
        lines.append("")
    print("\n".join(lines).rstrip())
    return status


def cmd_fix(a) -> int:
    paths = _files(a.files)
    if a.output and len(paths) != 1:
        print("--output works with one file at a time.")
        return 2
    lines, status = [], 0
    for path in paths:
        try:
            _, said = fix_file(path, a.output, notes=not a.tempo_only)
            lines += said
        except MidiError as e:
            lines.append(f"{os.path.basename(path)}: not fixed, {e}.")
            status = 2
    print("\n".join(lines))
    return status


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="midi-tempo",
        description="Check and fix the tempo map in MIDI files, so every app plays them the same.",
    )
    p.add_argument("--version", action="version", version=f"midi-tempo {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="Report the tempo map and any problems. Changes nothing.")
    c.add_argument("files", nargs="+", help="MIDI files, or folders of them.")
    c.add_argument("--project", help="A REAPER .RPP project to compare the tempo map against.")
    c.add_argument("--tempo-only", action="store_true", help="Check the tempo map only, not the notes.")
    f = sub.add_parser("fix", help="Save a cleaned copy next to each original.")
    f.add_argument("files", nargs="+", help="MIDI files, or folders of them. Never changed.")
    f.add_argument("-o", "--output", help="Where to save the cleaned copy (one file only).")
    f.add_argument("--tempo-only", action="store_true", help="Fix the tempo map only and leave the notes alone.")
    a = p.parse_args(argv)
    return cmd_check(a) if a.cmd == "check" else cmd_fix(a)


if __name__ == "__main__":
    sys.exit(main())
