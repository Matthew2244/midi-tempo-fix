# MIDI Tempo Fix

You export a MIDI file with tempo changes, open it in another app, and the notes drift away from the click. This tool finds out why and fixes it, so the file plays with the same timing in REAPER, Logic, Pro Tools and anything else.

It runs on Mac and Windows, as an app or from the command line. Everything works with VoiceOver, NVDA, JAWS and Narrator, and just as well with a mouse.

## What goes wrong

A MIDI file can't hold a smooth tempo ramp. DAWs write each ramp as lots of small fixed-tempo steps instead. Done right, every app plays those steps the same way. These are the things that break that:

- **Two tempo changes on the same tick.** The MIDI rules say the second one wins. An app that keeps the first one plays the wrong tempo for the rest of the song. REAPER exports can do this at the end of a ramp, and Logic was measured drifting almost a second over one song because of it.
- **REAPER's ramp notes.** REAPER hides text notes in its exports ("REAPER gradual tempo change start ...") so its own import can rebuild each ramp. That rebuild puts the end of a ramp in the wrong place when the ramp ends on a time signature change. One song came back half a second off. Other apps ignore these notes. Without them, REAPER reads the exact steps like everyone else.
- **Tempo changes on the wrong track.** In a multi-track MIDI file the tempo map belongs on the first track, and some apps only look there.

MIDI Tempo Fix removes those problems and leaves everything else alone: notes, controllers, track names, markers and timing. It never changes your original file. It saves a cleaned copy next to it, with "(tempo fixed)" added to the name, then reads that copy back and checks that every event plays at the same moment it did before.

The decimal tempos you see inside a ramp (like 99.78, then 99.35) are normal. They're how the steps keep the timing exact.

## Download

Get the latest version from the [Releases page](../../releases):

- **Mac:** MIDI-Tempo-Fix-mac.zip. Unzip it and move MIDI Tempo Fix to Applications. The app isn't signed by Apple yet, so the first time you open it, macOS may say it can't check it. Control-click the app, choose Open, then Open again. Or run this once in Terminal:

  ```
  xattr -dr com.apple.quarantine "/Applications/MIDI Tempo Fix.app"
  ```

  The download is built for Apple silicon Macs. On an Intel Mac, install the command line version below and run `midi-tempo-fix` to open the app.

- **Windows:** MIDI-Tempo-Fix-windows.zip. Unzip it anywhere and run MIDI Tempo Fix.exe inside the folder. If SmartScreen warns you, choose More info, then Run anyway.

## Using the app

1. Press **Choose MIDI files** (Command-O on Mac, Control-O on Windows) and pick one or more files. You can also drop files on the window, or on the app's Dock icon on a Mac.
2. The report says what's in each file's tempo map and whether anything needs fixing. Focus moves to the report so your screen reader reads it right away.
3. If something needs fixing, press **Fix and save cleaned copies** (Command-S or Control-S). The report says where each copy went, and **Show cleaned files** (Command-Shift-S or Control-Shift-S) opens that folder with them selected.

**Compare with a REAPER project** (Command-R or Control-R) checks a MIDI file's tempo against a REAPER project's tempo map, beat by beat. It's useful for confirming an export matches the session it came from.

## Command line

With Python 3.9 or newer, install it straight from this repository:

```
pip install "git+https://github.com/Matthew2244/midi-tempo-fix"
```

Then:

```
midi-tempo check song.mid
midi-tempo check song.mid --project "My Song.RPP"
midi-tempo fix song.mid
midi-tempo fix "Exports folder"
```

`check` changes nothing. It exits with 0 when files are clean, 1 when something needs fixing, and 2 when a file can't be read. `fix` takes files or folders and saves a cleaned copy of each file that needs one. The command line needs nothing beyond Python itself. To open the app from a pip install, add the app extra (`pip install "midi-tempo-fix[app] @ git+https://github.com/Matthew2244/midi-tempo-fix"`) and run `midi-tempo-fix`.

Output is plain sentences, printed once per command, so a screen reader doesn't get interrupted mid-sentence.

## Building it yourself

```
python -m pip install wxPython pyinstaller pytest
python -m pytest
python build_app.py
```

`build_app.py` builds the app for whichever system it runs on and puts a zip in `dist`.

## License

MIT. Free to use, share and change.
