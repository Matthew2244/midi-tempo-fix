# Handoff: MIDI Tempo Fix

Read this before changing anything. It's the current state, not a history.

## What it is

A tool that checks and fixes the tempo map in Standard MIDI Files so every DAW plays them with the same timing. One engine (`midi_tempo/core.py`, standard library only) behind two front ends: the `midi-tempo` command (`cli.py`) and a wxPython app (`gui.py`). Mac and Windows; the command also runs on Linux.

Matthew's own copy of the command is `~/bin/midi-tempo`, a symlink to `midi-tempo` at the repo root, so the live tool and the repo can't drift. Edit the repo.

## The three faults it fixes, and how each was proven

All found on 2026-10-11 from real REAPER 7.82 exports, measured rather than assumed:

1. **Doubled tempo event at a ramp's end.** REAPER wrote `84.7442` then `85.0` on the same tick at the end of a slowdown. The second is correct by the MIDI rules; an app keeping the first drifts (0.9 s by the end of that song). Logic played it wrong; the cleaned copy played right.
2. **REAPER ramp hints.** REAPER's export writes text meta events (type 1) reading `REAPER gradual tempo change start <bpm>` and `REAPER gradual tempo change interpolation end`. REAPER's import uses them to rebuild a smooth ramp, and when the ramp ends on a time signature change it places the end marker as if the tempo had stayed at the start value (127 to 90 over 6 beats came back as 6 beats of 127: 2.835 s instead of 3.318). Drift reached 543 ms. With the hints stripped, REAPER imported the same file as 166 square tempo markers, within 0.1 ms of the source project. Ramps ending on a plain tempo marker import fine even with hints, but stripping is harmless there too.
3. **Tempo map events on a later track** of a format 1 file. Moved to track 0. This one is from the MIDI spec and is covered by tests, not by a real-world failure we observed.

REAPER's own MIDI export is exact: steps of 1/32 note, each at the average tempo over its step, ramps linear in time. Checked on three songs to under 0.25 ms, and one against a rendered click stem (791 clicks within 0.22 ms).

## Rules the code keeps

- **The original file is never written.** Fixes go to `<name> (tempo fixed).<ext>`; the code refuses to overwrite the input.
- **Every fix verifies itself** by reading the saved file back: no faults left, and the playback time of every non-tempo event matches the original within a microsecond. On failure the copy is deleted.
- **Nothing beyond the three faults is changed.** No re-quantizing, no rounding tempos, no PPQ change.
- **Output is sentences, written once.** No "(s)" plurals (`n()` in core), no progress spam. A screen reader user hears one clean report.
- **In the app, focus goes to the report after every action.** All actions are in the File menu with shortcuts as well as on buttons.

## REAPER project comparison

`compare_project` reads a `.RPP`'s `<TEMPOENVEX>` points (`PT seconds bpm shape ...`; shape 0 = ramp to the next point, 1 = square) and computes beat times with ramps linear in time: beats = seconds × (start + end) / 120. That model was confirmed against REAPER's own export to the microsecond.

## Building and releasing

- `python -m pytest`: synthetic fixtures only. **No real music goes in this repo.**
- `python build_app.py`: PyInstaller build for the current OS, zip in `dist/`, then runs the frozen app with `--self-test` (loads the window without showing it).
- CI (`.github/workflows/build.yml`): tests on Linux, Mac and Windows with Python 3.9 and 3.13, then builds both apps. Pushing a `v*` tag attaches the zips to a GitHub release. Watch the run to green after every push.
- The Mac build is ad-hoc signed only, for Apple silicon. The README gives the Gatekeeper and SmartScreen steps. Proper signing and notarization would need an Apple Developer ID certificate in CI secrets.

## Screen reader behaviour, and why the code looks the way it does

Tested with VoiceOver on macOS 26 on 2026-10-11, driving the real app with keystrokes and reading `content of last phrase` from VoiceOver:

- **The report needs an explicit label on macOS.** wx has no API for it, so VoiceOver said only "text". `mac_accessibility_label()` sets `accessibilityLabel` on the scroll view and its text view through the Objective-C runtime (ctypes, no extra dependency). It now says "Report, text". Windows readers take the name from the static label created just before the field, so keep that label directly before the report in creation order.
- **A field that already has focus is not re-read when its text changes.** After Fix, VoiceOver kept reading the old report. `show()` steps focus to the Choose button and back after 150 ms when the report is already focused; VoiceOver then reads the new result in full.
- **Tab order is creation order.** Buttons are created in on-screen order: Fix, Show cleaned files, Compare.
- The saved line is short ("Saved next to the original as ...") because a full path is noise spoken aloud and clutter on screen. The command line keeps the full path for scripts.

## Not verified yet

- NVDA, JAWS and Narrator on a real Windows machine. CI builds and self-tests the Windows app, but no person has used it there.
- The Windows build has not run on a real Windows machine; CI builds and self-tests it.
- Pro Tools has not been tested with a cleaned file.
