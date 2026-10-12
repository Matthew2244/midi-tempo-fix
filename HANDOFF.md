# Handoff: MIDI Tempo Fix

Read this before changing anything. It's the current state, not a history.

## What it is

A tool that checks and fixes Standard MIDI Files so every DAW plays them the same: the tempo map (`core.py`) and the notes (`notes.py`), both standard library only, behind two front ends: the `midi-tempo` command (`cli.py`) and a wxPython app (`gui.py`). `update.py` lets the packaged app find and install new releases. Mac and Windows; the command also runs on Linux.

Matthew's own copy of the command is `~/bin/midi-tempo`, a symlink to `midi-tempo` at the repo root, so the live tool and the repo can't drift. Edit the repo.

## The three faults it fixes, and how each was proven

All found on 2026-10-11 from real REAPER 7.82 exports, measured rather than assumed:

1. **Doubled tempo event at a ramp's end.** REAPER wrote `84.7442` then `85.0` on the same tick at the end of a slowdown. The second is correct by the MIDI rules; an app keeping the first drifts (0.9 s by the end of that song). Logic played it wrong; the cleaned copy played right.
2. **REAPER ramp hints.** REAPER's export writes text meta events (type 1) reading `REAPER gradual tempo change start <bpm>` and `REAPER gradual tempo change interpolation end`. REAPER's import uses them to rebuild a smooth ramp, and when the ramp ends on a time signature change it places the end marker as if the tempo had stayed at the start value (127 to 90 over 6 beats came back as 6 beats of 127: 2.835 s instead of 3.318). Drift reached 543 ms. With the hints stripped, REAPER imported the same file as 166 square tempo markers, within 0.1 ms of the source project. Ramps ending on a plain tempo marker import fine even with hints, but stripping is harmless there too.
3. **Tempo map events on a later track** of a format 1 file. Tempo changes are always moved to track 0. Time signatures only move when track 0 lacks one at that tick: notation apps copy them to every track and that is harmless. Key signatures are per-track by design and never move. (Version 1.0 counted all three and called 3,000 of 17,000 files broken; the scan below caught it.)

Doubled tempo events only count when the values differ by more than 0.001 BPM. The same tempo written twice can't make apps disagree and is left alone. (1.0 flagged those too; 36 of the first 40 hits in the scan were this harmless kind.)

## Reports are verdict first

Every file's report starts with one line a listener can stop after: "name: clean." or "name: needs fixing. 2 problems: two tempos on one tick, REAPER ramp notes." (`verdict()`), then the details. More than one file adds `summary()` on top ("Checked 40 files: 3 need fixing, 37 clean."). In the app, more than `BRIEF_OVER` (20) files lists only the verdicts of files needing fixing; the command's `--brief` does the same per file. Inside a chosen folder, names include the subfolder, since names repeat across folders.

## Batches in the app

Checking and fixing run on a worker thread; every control is disabled meanwhile (`_set_busy`). Progress goes to the window title once a second, and roughly every 500 files `announce()` asks VoiceOver to say "Checked 1000 of 5343" (macOS AXAnnouncementRequested through the Objective-C runtime, like the report label; no extra dependency). Short confirmations (Report copied, Report saved) go to the status bar and `announce()`, so focus never moves. Files that can't be written next to their original are collected and handled after the worker, on the main thread, where the folder dialog can be shown. `reveal()` opens only the first copy when copies span more than three folders, so a big batch doesn't open dozens of Finder windows.

`Event` uses `__slots__` and computes `meta_type` once; as a property it was a third of the time on a big piano file. The 5,343-file library went from 48 to 36 seconds.

## SMPTE-timed files

A file whose header counts time in video frames (division's top bit set) has no tempo map; tempo events in it do not apply. It is modelled as a fixed 60 BPM with frames × ticks-per-frame ticks per beat, so one beat is one second and every time calculation stays the same code. Tempo checks are skipped, notes are checked and fixed, and the original header is written back unchanged. 29 means 29.97 (drop frame).

## Note problems (`notes.py`)

Duplicates, same-key overlaps, stuck notes and stray releases are fixed; zero-length notes are reported only. Overlaps on the drum channel (channel 10, index 9) are not counted or trimmed: drum hits are one-shots and drum performances overlap constantly. Before this rule, 881 of the 5,343 library files "needed fixing", mostly Groove drum performances; after it, 314.

Tempo map events moved to the first track are de-duplicated, keeping the LAST identical copy on each tick. Keeping the first was a real bug, found by training: in two LilyPond files the tracks disagree on one tick (120, then 60, then 120 again on other tracks), the last one read wins (120), and dropping the later copies handed the win to 60. Verification caught both, so no bad file was written; `test_merging_copies_keeps_the_tempo_that_wins` and the fuzz test's disagreeing copies now cover it (both fail on the old code). LilyPond (all of Mutopia) writes the tempo on every instrument track, and moving them all used to stack identical copies; the report says "moved 2 ..., merging 4 identical copies". Rebuilding pairs note-offs first in, first out, merges same-start duplicates (first velocity, longest end), ends an overlapped note at the re-strike, and ends stuck notes at the track's last tick. At one tick it writes note-offs, then other events in their original order, then note-ons; a zero-length note keeps on-then-off. Only tracks with a fixable problem are rebuilt.

Verification for note fixes: every note start (track, moment, channel, key, first velocity) is identical before and after, every non-note event is at the same moment, and the copy has nothing left to fix.

## How it was trained and broken (2026-10-11)

- Scanned every MIDI file on Matthew's Studio: 17,371 paths, 17,074 readable. The other 297 are genuinely not MIDI (129 empty files, 165 plugin presets named .midi, 3 SMPTE-timed files). No parser bugs.
- Ran `fix` on all of them: 1,566 fixed, 15,508 clean, **0 crashes, 0 failed verifications**.
- 1.2 training: the same 17,371 paths through fix, then `compare_with(original, fixed)` and `compare_with(original, copy at double resolution)`. 17,077 readable (the 3 SMPTE files now included), 1,566 fixed, every comparison matched, 0 crashes.
- 1.3 training: same run; the first pass found the two merge failures above, the second came back 17,077 readable, 978 fixed, every comparison matched, 0 failures. (Fewer "fixed" than 1.2 because drum overlaps no longer count.)
- `test_fuzz_random_broken_files`: 300 random files with doubled tempos, hints, sigs, overlaps, stuck and stray notes; also run once at 4,000 with another seed. Each must verify and fixing the fixed copy must find nothing.

## Updates (`update.py`)

The app asks `api.github.com/repos/Matthew2244/midi-tempo-fix/releases/latest` at most once a day (time stored with `wx.Config`) and on Help, Check for Updates. Installing downloads the platform zip, runs the new app's `--self-test`, then starts a detached script (`swap.sh` / `swap.bat`) that waits for this process to exit, swaps the new app in (Mac: rename old aside, move new in, restore on failure; Windows: robocopy /MIR) and relaunches. `certifi` is bundled so HTTPS works on machines without Homebrew's certificates. `MIDI_TEMPO_FIX_PRETEND_VERSION` and the hidden `--update-now --no-relaunch` flags exist to test the whole path against a real release without the window.

REAPER's own MIDI export is exact: steps of 1/32 note, each at the average tempo over its step, ramps linear in time. Checked on three songs to under 0.25 ms, and one against a rendered click stem (791 clicks within 0.22 ms).

## Rules the code keeps

- **The original file is never written.** Fixes go to `<name> (tempo fixed).<ext>`; the code refuses to overwrite the input.
- **Every fix verifies itself** by reading the saved file back: no faults left, and the playback time of every non-tempo event matches the original within a microsecond. On failure the copy is deleted.
- **Nothing beyond the three faults is changed.** No re-quantizing, no rounding tempos, no PPQ change.
- **Output is sentences, written once.** No "(s)" plurals (`n()` in core), no progress spam. A screen reader user hears one clean report.
- **In the app, focus goes to the report after every action.** All actions are in the File menu with shortcuts as well as on buttons.

## Settings and scripting

The app remembers, through `wx.Config`, whether notes are checked and the last folder files were chosen from, so dialogs open there. `midi-tempo check --json` prints machine-readable results (per-file problems, doubled tempos, note counts, optional comparison) for scripts and other tools.

## Comparing (universal)

`compare_with(mf, path)` picks by extension: any MIDI file goes to `compare_midi`, a `.rpp` to `compare_project`. The app's button and the command's `--compare` (alias `--project`) both use it. The point is app-neutral: export from one app, import into another, export again, compare the two.

`compare_midi` checks the tempo map beat by beat (quarter-beat steps, each file at its own resolution, so a 480-PPQ Logic export compares cleanly with a 960-PPQ REAPER one), then every note: starts in seconds grouped by channel and key, matched in order, ignoring tracks because apps merge and split them. Differences under `SAME_MS` (2 ms) are tick rounding and count as matching. When the note counts differ it lists which keys and stops: matching by order means nothing once notes differ, and the "moved by" number would be garbage (it was, -322 seconds, before that early return).

### REAPER project comparison

`compare_project` reads a `.RPP`'s `<TEMPOENVEX>` points (`PT seconds bpm shape ...`; shape 0 = ramp to the next point, 1 = square) and computes beat times with ramps linear in time: beats = seconds × (start + end) / 120. That model was confirmed against REAPER's own export to the microsecond.

## Building and releasing

- `python -m pytest`: synthetic fixtures only. **No real music goes in this repo.**
- `python build_app.py`: PyInstaller build for the current OS, zip in `dist/`, then runs the frozen app with `--self-test` (loads the window without showing it).
- CI (`.github/workflows/build.yml`): tests on Linux, Mac and Windows with Python 3.9 and 3.13, then builds both apps. Pushing a `v*` tag attaches the zips to a GitHub release. Watch the run to green after every push.
- **Signing the Mac app (Developer ID + notarization) happens on the Studio, never in CI**, so the signing key and the App Store Connect key never leave that Mac. After CI has published a release (with an unsigned Mac zip), run `python tools/release_mac.py vX.Y.Z` on the Studio: it builds with `MIDI_TEMPO_FIX_SIGN_ID` set (PyInstaller signs every binary, `build_app.py` seals the bundle with the hardened runtime and a timestamp), verifies, submits to Apple's notary service with the API key from the Keychain entry `asc-api-key` (key 44L73HXU2W, issuer in the script), staples, checks Gatekeeper accepts it, self-tests, and replaces the release's Mac zip (`gh release upload --clobber`).
- **The certificate exists:** "Developer ID Application: MATTHEW CHRISTOPHER WHITAKER (FXDZR9CYHP)", G2, certificate ID M5J9N659UT, expires 2031-09-17, in the Studio's login Keychain with its private key (key name "devid-rsa"). Matthew granted codesign "Always Allow" on that key, so signing runs without a prompt. Only the Account Holder can create these (the API returns 403), so a replacement in 2031 means Matthew signs in to developer.apple.com (passkey works) and creates one from a new request file; the API can then download it (`GET /v1/certificates/{id}`, field `certificateContent`).
- First signed release: v1.4.0 (2026-10-11). Apple accepted it in about a minute; `spctl` reports "Notarized Developer ID", including on a downloaded copy with the quarantine flag set. The in-app updater moved both Macs to it with the signature intact.
- **The hardened runtime needs no entitlements.** Tested with a temporary signature on 2026-10-11: with only library validation relaxed (temporary signatures have no team), the app starts, draws its window, uses the Objective-C runtime for VoiceOver, and runs a full self-update. A real Developer ID gives every piece the same team, so library validation passes as it is.
- Windows builds are unsigned; SmartScreen warns once. Signing them would need a code-signing certificate from a Windows certificate authority.

## Screen reader behaviour, and why the code looks the way it does

Tested with VoiceOver on macOS 26 on 2026-10-11, driving the real app with keystrokes and reading `content of last phrase` from VoiceOver:

- **The report needs an explicit label on macOS.** wx has no API for it, so VoiceOver said only "text". `mac_accessibility_label()` sets `accessibilityLabel` on the scroll view and its text view through the Objective-C runtime (ctypes, no extra dependency). It now says "Report, text". Windows readers take the name from the static label created just before the field, so keep that label directly before the report in creation order.
- **A field that already has focus is not re-read when its text changes.** After Fix, VoiceOver kept reading the old report. `show()` steps focus to the Choose button and back after 150 ms when the report is already focused; VoiceOver then reads the new result in full.
- **Tab order is creation order.** Buttons are created in on-screen order: Fix, Show cleaned files, Compare with another file.
- **The update dialog's second button must be titled exactly "Cancel".** macOS alerts give Escape only to a button with that title. As "Not now" (and earlier as Yes/No), Escape did nothing and the dialog could only be left by Install or by hunting for the other button. Found in the VoiceOver pass; checked fixed the same way.
- **A checkbox reads its state** ("checked checkbox" / "unchecked"). Toggling it re-checks the files into the report without moving focus, so the user stays on the checkbox.
- **Move focus to the report a moment after any dialog closes.** Right after a file dialog, macOS restores focus itself; setting it then made VoiceOver say only "Report scroll area" and skip the text. `show()` now defers the focus move by 250 ms; VoiceOver then reads the whole result after Choose and after Compare.
- The saved line is short ("Saved next to the original as ...") because a full path is noise spoken aloud and clutter on screen. The command line keeps the full path for scripts.

## Not verified yet

- NVDA, JAWS and Narrator on a real Windows machine. CI builds and self-tests the Windows app, but no person has used it there.
- The Windows build has not run on a real Windows machine; CI builds and self-tests it.
- Pro Tools has not been tested with a cleaned file.
