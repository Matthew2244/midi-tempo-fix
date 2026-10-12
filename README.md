# MIDI Tempo Fix

You save a MIDI file in one app, open it in another, and the notes drift away from the click after a tempo change. Or a note that held in one app gets cut short in the next. MIDI Tempo Fix finds out why and fixes it. It works on the file itself, so it helps wherever the file goes next: Logic, Pro Tools, REAPER, Cubase, Ableton Live, Studio One, MuseScore, Sibelius, a hardware sequencer, anything that reads MIDI files.

![MIDI Tempo Fix on a Mac after checking a folder of 20 MIDI files. The Files list shows three rows starting "Needs fixing", each saying "tempo on the wrong track", followed by rows starting "Clean". Below it, the report opens with "Checked 20 files: 3 need fixing, 17 clean" in bold, then each file's verdict in bold with its details underneath. Buttons along the bottom: Fix and save cleaned copies, Show cleaned files, Compare with another file.](docs/screenshot.png)

It runs on Mac and Windows, as an app or from the command line. Everything works with VoiceOver, NVDA, JAWS and Narrator, and just as well with a mouse.

## What goes wrong

A MIDI file can't hold a smooth tempo ramp, so apps write each ramp as lots of small fixed-tempo steps. The decimal tempos you see inside a ramp (99.78, then 99.35) are those steps, and they keep the timing exact. When every app reads the steps the same way, the file plays the same everywhere. These are the things that break that.

In the tempo map:

- **Two different tempos on the same tick.** The MIDI rules say the second one wins, but some apps keep the first and play the wrong tempo from there on. Drum-loop libraries often start with a default 120 BPM followed by the real tempo on the same tick, which can put a loop seconds off. REAPER's export can do it at the end of a ramp; Logic drifted almost a second over one song. The same tempo written twice is harmless and left alone.
- **Tempo changes on the wrong track.** In a multi-track file the tempo map belongs on the first track, and some apps only look there; anything else plays at the default 120 BPM. Score apps such as LilyPond write the tempo on every instrument track instead. The fix moves it to the first track and merges the identical copies. Time signatures copied onto every track are fine and left alone.
- **REAPER ramp notes.** REAPER hides text notes in its exports ("REAPER gradual tempo change start ...") so that its own import can rebuild each ramp. That rebuild puts the end of a ramp in the wrong place when the ramp ends on a time signature change; one song came back half a second off. Other apps ignore the notes, and without them REAPER reads the exact steps like everyone else.

In the notes:

- **Duplicate notes:** the same key struck twice at the same moment. Some apps play both, some play one. The fix keeps one, as long as the longer of the two.
- **Overlapping notes:** a key struck again before it was released. Apps disagree about which release ends which note, so lengths change. The fix ends the earlier note where the key is struck again. Drums (channel 10) are left alone here: drum hits are one-shots, so their length rarely matters, and drum performances overlap constantly.
- **Stuck notes** that never end. Some apps cut them off, some hold them forever. The fix ends them at the end of their track.
- **Stray note releases** with no note playing. Removed.
- **Notes with no length** are reported but left alone, because no length is right for every case.

Fixes never move a note's start, key or velocity, and never touch controllers, track names or markers. Your original file is never changed: the cleaned copy is saved next to it with "(tempo fixed)" added to the name, then read back to prove every note still starts at the same moment. To leave notes alone and fix only the tempo map, turn off **Also check and fix notes** in the app, or add `--tempo-only` on the command line.

It reads every kind of Standard MIDI File, including Windows .rmi files (the cleaned copy is saved as a plain .mid) and files that count time in video frames instead of beats.

## Did an app change my file?

**Compare with another file** answers that. Save the same music from two places, for example your export and the file another app saved after importing it, and compare them. It reports whether the tempo matches beat by beat, whether the same notes are there, and how far any note moved. Different track layouts don't matter. Differences under 2 ms are rounding no one can hear (some apps save at a lower resolution) and are reported as matching.

You can also compare against a REAPER project (.rpp), which reads the project's tempo map directly. That's useful for confirming an export matches the session it came from.

## Download

Get the latest version from the [Releases page](../../releases):

- **Mac:** MIDI-Tempo-Fix-mac.zip. Unzip it and move MIDI Tempo Fix to Applications. It's signed with a Developer ID and notarized by Apple, so it opens like any other app.

  The download is built for Apple silicon Macs. On an Intel Mac, install the command line version below and run `midi-tempo-fix` to open the app.

- **Windows:** MIDI-Tempo-Fix-windows.zip. Unzip it anywhere and run MIDI Tempo Fix.exe inside the folder. If SmartScreen warns you, choose More info, then Run anyway.

## Using the app

1. Press **Choose MIDI files** (Command-O on Mac, Control-O on Windows) and pick one or more files, or **Choose a folder** (Command-Shift-O or Control-Shift-O) to check every MIDI file in it, including folders inside it. You can also drop files or folders on the window, or on the app's Dock icon on a Mac.
2. The **Files** list shows every file with its status first ("Needs fixing: name, what's wrong" or "Clean: name"), files that need fixing at the top. Arrow through it to hear each verdict, and select a file to read everything about it in the report.
3. The report starts with the answer: each file is either "clean" or "needs fixing", with the problems named. The details follow. With more than one file, a summary line comes first ("Checked 40 files: 3 need fixing, 37 clean"). With more than 20, only the files that need fixing are listed, so the report stays short enough to listen to. Focus moves to the report, so your screen reader reads it right away. Big folders are checked in the background on every processor core (a 5,000-file library takes about four seconds on a recent Mac), with a progress bar, progress in the title bar, and the count spoken now and then. **Stop** (or Escape) ends a batch early and keeps the results so far.
4. If something needs fixing, press **Fix and save cleaned copies** (Command-S or Control-S). The report says where each copy went, and **Show cleaned files** (Command-Shift-S or Control-Shift-S) opens that folder with them selected.
5. **Compare with another file** (Command-R or Control-R) compares the chosen files with any other MIDI file or a REAPER project.
6. **Copy report** (Command-Shift-C or Control-Shift-C) puts the whole report on the clipboard, ready to paste into a message. **File, Save report as text** saves it as a file. **Help, MIDI Tempo Fix Help** opens this page.

**Bigger text:** View, Bigger Text (Command-equals or Control-equals) makes the list, report, labels and checkbox larger; Smaller Text and Actual Size undo it. The size is remembered. Verdicts and summaries are bold, so the answer stands out by weight as well as by words; nothing relies on colour.

When a long batch finishes, the app plays a sound and bounces its Dock icon, in case you've switched to something else.

The app remembers whether notes are checked, your text size, and which folder you last chose files from.

**Updates:** once a day the app asks GitHub whether there's a newer version, and the report says so if there is. **Help, Check for Updates** downloads it, checks that it starts, and replaces the app you have. Nothing else is sent anywhere.

## Command line

With Python 3.9 or newer, install it straight from this repository:

```
pip install "git+https://github.com/Matthew2244/midi-tempo-fix"
```

Then:

```
midi-tempo check song.mid
midi-tempo check "Exports folder" --brief -r
midi-tempo check export.mid --compare "after another app.mid"
midi-tempo check export.mid --compare "My Song.RPP"
midi-tempo fix song.mid
midi-tempo fix "Exports folder"
midi-tempo fix --tempo-only song.mid
```

Both commands use every processor core but one; `--jobs 1` keeps it to one. `--json` prints the results in a form scripts and other tools can read. `check` starts each file with its verdict, and puts a summary line first when there's more than one. `--brief` gives one line per file. `-r` also looks inside folders within folders. `check` changes nothing. It exits with 0 when files are clean, 1 when something needs fixing, and 2 when a file can't be read. `fix` takes files or folders and saves a cleaned copy of each file that needs one. The command line needs nothing beyond Python itself. To open the app from a pip install, add the app extra (`pip install "midi-tempo-fix[app] @ git+https://github.com/Matthew2244/midi-tempo-fix"`) and run `midi-tempo-fix`.

Output is plain sentences, printed once per command, so a screen reader doesn't get interrupted mid-sentence.

## How it's tested

Before each release it runs against more than 17,000 real MIDI files from many apps and libraries, and thousands of deliberately broken ones. Every cleaned copy has to pass its own read-back check, compare as identical to the original apart from what was fixed, and have nothing left to fix.

## Building it yourself

```
python -m pip install wxPython pyinstaller pytest certifi
python -m pytest
python build_app.py
```

`build_app.py` builds the app for whichever system it runs on and puts a zip in `dist`.

## License

MIT. Free to use, share and change.
