# MIDI Tempo Fix

You export a MIDI file with tempo changes, open it in another app, and the notes drift away from the click. Or a note that held in one app gets cut short in another. This tool finds out why and fixes it, so the file plays the same in REAPER, Logic, Pro Tools and anything else.

It runs on Mac and Windows, as an app or from the command line. Everything works with VoiceOver, NVDA, JAWS and Narrator, and just as well with a mouse.

## What goes wrong

A MIDI file can't hold a smooth tempo ramp. DAWs write each ramp as lots of small fixed-tempo steps instead. Done right, every app plays those steps the same way. These are the things that break that:

- **Two tempo changes on the same tick.** The MIDI rules say the second one wins. An app that keeps the first one plays the wrong tempo for the rest of the song. REAPER exports can do this at the end of a ramp, and Logic was measured drifting almost a second over one song because of it.
- **REAPER's ramp notes.** REAPER hides text notes in its exports ("REAPER gradual tempo change start ...") so its own import can rebuild each ramp. That rebuild puts the end of a ramp in the wrong place when the ramp ends on a time signature change. One song came back half a second off. Other apps ignore these notes. Without them, REAPER reads the exact steps like everyone else.
- **Tempo changes on the wrong track.** In a multi-track MIDI file the tempo map belongs on the first track, and some apps only look there. Time signatures copied onto every track are fine and left alone.

The notes can disagree between apps too:

- **Duplicate notes:** the same key struck twice at the same moment. Some apps play both, some play one. The fix keeps one, as long as the longer of the two.
- **Overlapping notes:** a key struck again before it was released. Apps disagree about which release ends which note, so note lengths change. The fix ends the earlier note where the key is struck again.
- **Stuck notes** that never end. Some apps cut them off, some hold them forever. The fix ends them at the end of their track.
- **Stray note releases** with no note playing. Removed.
- **Notes with no length** are reported but left alone, because no length is right for every case.

Note fixes never move a note's start, key or velocity. If you only want the tempo map touched, turn off **Also check and fix notes** in the app, or use `--tempo-only` on the command line.

MIDI Tempo Fix removes those problems and leaves everything else alone: controllers, track names, markers and timing. It never changes your original file. It saves a cleaned copy next to it, with "(tempo fixed)" added to the name, then reads that copy back and checks that every event plays at the same moment it did before.

The decimal tempos you see inside a ramp (like 99.78, then 99.35) are normal. They're how the steps keep the timing exact.

Before each release it's run against more than 17,000 real MIDI files and thousands of deliberately broken ones, and every cleaned copy has to pass that same read-back check.

It also reads Windows .rmi files, and saves the cleaned copy as a plain .mid that every app opens.

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

**Updates:** once a day the app asks GitHub whether there's a newer version, and the report says so if there is. **Help, Check for Updates** downloads it, checks that it starts, and replaces the app you have. Nothing else is sent anywhere.

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
midi-tempo fix --tempo-only song.mid
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
