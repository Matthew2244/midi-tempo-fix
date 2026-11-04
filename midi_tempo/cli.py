"""midi-tempo: check and fix MIDI files so every app plays them the same.

    midi-tempo check FILE.mid [FILE.mid ...]   report the tempo map and any problems
    midi-tempo check A.mid --compare B.mid     also compare with another MIDI file (any app)
    midi-tempo check A.mid --compare X.RPP     ... or with a REAPER project
    midi-tempo check A.mid --compare mix.wav   ... or with a recording: does it line up, and if not, how
    midi-tempo check FOLDER --brief -r         one line per file, folders within folders too
    midi-tempo fix FILE.mid [FILE.mid ...]     save a cleaned copy next to each original
    midi-tempo fix A.mid --project X.RPP       ... also undo REAPER's padded short bars
    midi-tempo ableton song.mid                a new Live set: one locator per marker, for AbleSet
    midi-tempo ableton song.mid --into Show.als              ... added after the last song in a set
    midi-tempo ableton song.mid --into Show.als --onto Song  ... onto a song already there
    midi-tempo ableton song.mid --marker "17 Verse" --marker "33.3 Hit"   ... plus markers typed in
    midi-tempo ableton --name Song --tempo 96 --markers list.txt         ... or typed in alone
    midi-tempo ableton song.mid --lyrics       ... with the file's lyrics as an AbleSet lyrics track
    midi-tempo ableton "My Song.RPP"           ... straight from a REAPER project, no MIDI export
    midi-tempo ableton --songs Show.als        list the songs in a set

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
        "markers": r.markers,
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


def cmd_ableton(a) -> int:
    from . import ableton as A

    try:
        if a.songs:
            print("\n".join(A.describe_set(A.LiveSet.load(a.songs))))
            return 0
        if a.onto and not a.into:
            print("--onto names a song in a set you already have; give the set with --into.")
            return 2
        if a.update_set and a.output:
            print("--update-set changes the set itself, so it doesn't take -o.")
            return 2
        if a.each and a.into:
            print("--each makes a new set per file, so it doesn't go with --into.")
            return 2
        typed = []
        if a.markers:
            text = sys.stdin.read() if a.markers == "-" else open(a.markers, encoding="utf-8-sig").read()
            typed += A.parse_markers(text)
        for m in a.marker or []:
            typed += A.parse_markers(m)
        files = midi_files(a.files) if a.files else []
        if a.files and not files:
            print("No MIDI files found there.")
            return 2
        if typed and len(files) > 1:
            print("Typed markers go with one song at a time; give one MIDI file, or none.")
            return 2
        if not files and not typed:
            print("Give a MIDI file, or type markers with --marker or --markers.")
            return 2
        sig = None
        if a.time:
            try:
                num, den = (int(x) for x in a.time.split("/"))
                sig = (num, den)
            except ValueError:
                print("Write the time signature like 4/4 or 6/8.")
                return 2
        opts = dict(quick=a.quick, song_end=not a.no_song_end, plain=a.plain)
        lyrics = None
        if a.lyrics_text:
            text = sys.stdin.read() if a.lyrics_text == "-" else open(a.lyrics_text, encoding="utf-8-sig").read()
            lyrics = A.parse_markers(text)
        elif a.lyrics:
            if not files:
                print("--lyrics reads a MIDI file's lyrics; with no file, type them with --lyrics-text.")
                return 2
            lyrics = "file"
        if isinstance(lyrics, list) and len(files) > 1:
            print("Typed lyrics go with one song at a time; give one MIDI file, or none.")
            return 2
        if files:
            if a.name and len(files) > 1:
                print("--name names one song; with several files each takes its file's name.")
                return 2
            plans = [A.plan_song(f, a.name, project=a.project, typed=typed, file_markers=not a.no_file_markers,
                                 lyrics=lyrics, **opts) for f in files]
        else:
            if not a.name and not a.onto:
                print("Name the song with --name.")
                return 2
            plans = [A.plan_song(None, a.name or a.onto, typed=typed, tempo=a.tempo, signature=sig,
                                 lyrics=lyrics if isinstance(lyrics, list) else None, **opts)]
        if a.save_midi:
            if len(files) != 1 or A.is_rpp(files[0]):
                print("--save-midi writes markers into a copy of one MIDI file; give exactly one.")
                return 2
            items = [] if a.no_file_markers else A.file_markers_typed(files[0], a.project)[0]
            for m in typed:  # a typed marker at the same place takes the file's one's place
                items = [x for x in items if not (x.bar == m.bar and abs(x.beat - m.beat) < 1e-6)] + [m]
            items.sort(key=lambda m: (m.bar, m.beat))
            said = A.save_markers_midi(files[0], items, a.save_midi, a.project)
            print("\n".join(said + [f"Saved {A.n(len(items), 'marker')} in {a.save_midi}. "
                                     f"{os.path.basename(files[0])} wasn't changed."]))
            return 0
        if a.preview:
            out = []
            for p in plans:
                out += A.describe_plan(p) + [""]
            print("\n".join(out).rstrip())
            return 0
        results = []
        if a.into:
            results.append(A.add_to_set(a.into, plans, out=a.output, onto=a.onto, line_up=a.line_up,
                                        in_place=a.update_set))
        elif a.each:
            for p in plans:
                results.append(A.new_set([p], A.default_new_set_path(p), a.template))
        else:
            out = a.output or (A.default_new_set_path(plans[0]) if len(plans) == 1 else os.path.join(
                os.path.dirname(os.path.abspath(files[0])), "Set Project", "Set.als"))
            results.append(A.new_set(plans, out, a.template))
    except A.NeedsLineUp as e:
        print(f"{e} Run it again with --line-up bars or --line-up time.")
        return 1
    except (A.SetError, MidiError, OSError) as e:
        msg = str(e)
        print("Not saved: " + msg[0].lower() + msg[1:] + ("" if msg.endswith(".") else "."))
        return 2
    lines = []
    for r in results:
        lines += A.describe_result(r) + [f"Saved at {r.path}", ""]
    print("\n".join(lines).rstrip())
    return 0


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
    ab = sub.add_parser(
        "ableton", help="Make an Ableton Live set with a locator for each marker, named for AbleSet.",
        description="Turns markers into Ableton Live locators named the way AbleSet reads them: the song's name "
                    "at bar 1, \"> Verse\" for each section, and SONG END. The MIDI file's tempo map and time "
                    "signatures come too, so Live's bars match the song's. Markers can also be typed in, as many "
                    "as you like. Your files are never changed: a new set is saved, or a copy of yours.")
    ab.add_argument("files", nargs="*",
                    help="MIDI files or REAPER projects (.rpp). Several make one set with the songs back to back.")
    ab.add_argument("--marker", action="append", metavar='"BAR[.BEAT] NAME"',
                    help='A marker typed in, like "17 Verse" or "33.3 Pickup" (bar 33, beat 3). Repeat it for more.')
    ab.add_argument("--markers", metavar="FILE",
                    help="A text file of markers, one per line, like 17 Verse. A dash reads them from the keyboard "
                         "or a pipe.")
    ab.add_argument("--no-file-markers", action="store_true",
                    help="Leave out the MIDI file's own markers and use only the typed ones.")
    ab.add_argument("--name", help="The song's name. Default: the file's name.")
    ab.add_argument("--lyrics", action="store_true",
                    help="Add the MIDI file's lyrics as a lyrics track AbleSet shows: one clip per line, on a track "
                         "named Lyrics +LYRICS (or the set's own +LYRICS track).")
    ab.add_argument("--lyrics-text", metavar="FILE",
                    help="Lyrics typed in instead, one line each, like 17 First line of the song or 33.3 Next "
                         "line. A dash reads them from the keyboard or a pipe.")
    ab.add_argument("--tempo", type=float, help="With no MIDI file: the song's tempo in BPM. Default 120.")
    ab.add_argument("--time", metavar="4/4", help="With no MIDI file: the time signature. Default 4/4.")
    ab.add_argument("--into", metavar="SET.als",
                    help="Add to a set you already have instead of making a new one. A copy is saved next to it, "
                         "named (markers added).")
    ab.add_argument("--onto", metavar="SONG",
                    help="With --into: put the markers on this song, already in the set, replacing its section "
                         "locators and keeping its tempo. Without it, the song goes after the last one.")
    ab.add_argument("--line-up", choices=("bars", "time"),
                    help="With --onto, when the set's tempo isn't the file's: bars follows the set's tempo (a set "
                         "sped up or slowed down on purpose); time keeps each marker at its moment (stems that "
                         "aren't warped).")
    ab.add_argument("--update-set", action="store_true",
                    help="With --into: change the set itself, keeping the old one as (before markers).")
    ab.add_argument("--songs", metavar="SET.als",
                    help="List the songs in a set and each one's sections, by bar, to pick one for --onto.")
    ab.add_argument("--each", action="store_true", help="One set per MIDI file, for AbleSet's multi-file projects.")
    ab.add_argument("--template", metavar="SET.als",
                    help="A set to start new sets from. Default: the empty set inside your installed Live.")
    ab.add_argument("--quick", action="store_true", help='Make every section a quick-jump section (">>").')
    ab.add_argument("--plain", action="store_true",
                    help="Name locators exactly as the markers, for Live without AbleSet: no song name, no SONG END.")
    ab.add_argument("--no-song-end", action="store_true", help="Don't add a SONG END locator.")
    ab.add_argument("--project", metavar="X.RPP",
                    help="The REAPER project the MIDI file came from, to undo REAPER's padded short bars first.")
    ab.add_argument("--preview", action="store_true", help="Say what the set would hold, and save nothing.")
    ab.add_argument("--save-midi", metavar="COPY.mid",
                    help="Instead of a set, save a copy of the MIDI file with its markers and the typed ones "
                         "(--marker, --markers), so other apps get them. The file itself is never changed.")
    ab.add_argument("-o", "--output", metavar="SET.als", help="Where to save the set.")
    a = p.parse_args(argv)
    if a.cmd == "ableton":
        return cmd_ableton(a)
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
