# MIDI Tempo Fix

You save a MIDI file in one app, open it in another, and the notes drift away from the click after a tempo change. Or a note that held in one app gets cut short in the next. Or the MIDI you send a string arranger doesn't line up with the mix you sent with it. MIDI Tempo Fix finds out why and fixes it. It works on the file itself, so it helps wherever the file goes next: Logic, Pro Tools, REAPER, Cubase, Ableton Live, Studio One, MuseScore, Sibelius, a hardware sequencer, anything that reads MIDI files.

![MIDI Tempo Fix on a Mac after comparing a piano part with a full mix. The Files list shows "Clean: Piano part.mid". The report starts with "Piano part.mid compared with Full mix.wav" in bold, then: "Does NOT line up with Full mix.wav. 0:05 to 0:26: lines up. At 0:30 (bar 16, beat 1), the timing jumps 248 ms: from there the audio is 251 ms behind the MIDI. 0:30 to 1:34: the audio is 251 ms behind the MIDI throughout." It ends by saying the tempo matches but time was added or removed, and suggests comparing with the REAPER project. Buttons along the bottom: Fix and save cleaned copies, Show cleaned files, Compare with another file, Make Ableton Set, Stop.](docs/screenshot.png)

It also turns a song's markers, straight from a MIDI file or a REAPER project, into an Ableton Live set for [AbleSet](https://ableset.com), lyrics track included.

It runs on Mac and Windows, as an app or from the command line. Everything works with VoiceOver, NVDA, JAWS and Narrator, and just as well with a mouse.

## What goes wrong

A MIDI file can't hold a smooth tempo ramp, so apps write each ramp as lots of small fixed-tempo steps. The decimal tempos you see inside a ramp (99.78, then 99.35) are those steps, and they keep the timing exact. When every app reads the steps the same way, the file plays the same everywhere. These are the things that break that.

In the tempo map:

- **Two different tempos on the same tick.** The MIDI rules say the second one wins, but some apps keep the first and play the wrong tempo from there on. Drum-loop libraries often start with a default 120 BPM followed by the real tempo on the same tick, which can put a loop seconds off. REAPER's export can do it at the end of a ramp; Logic drifted almost a second over one song. The same tempo written twice is harmless and left alone.
- **Tempo changes on the wrong track.** In a multi-track file the tempo map belongs on the first track, and some apps only look there; anything else plays at the default 120 BPM. Score apps such as LilyPond write the tempo on every instrument track instead. The fix moves it to the first track and merges the identical copies. Time signatures copied onto every track are fine and left alone.
- **A bar REAPER's export made whole.** When a time signature starts partway through a bar, REAPER plays that bar short. Its MIDI export makes the bar full length instead, so everything after it comes out late: 254 ms in one song, enough to hear against strings. The file alone can't show this, so compare it with the REAPER project it came from. The report names the bar, and Fix moves everything back to where the project plays it.
- **REAPER ramp notes.** REAPER hides text notes in its exports ("REAPER gradual tempo change start ...") so that its own import can rebuild each ramp. That rebuild puts the end of a ramp in the wrong place when the ramp ends on a time signature change; one song came back half a second off. Other apps ignore the notes, and without them REAPER reads the exact steps like everyone else.

In the notes:

- **Duplicate notes:** the same key struck twice at the same moment. Some apps play both, some play one. The fix keeps one, as long as the longer of the two.
- **Overlapping notes:** a key struck again before it was released. Apps disagree about which release ends which note, so lengths change. The fix ends the earlier note where the key is struck again. Drums (channel 10) are left alone here: drum hits are one-shots, so their length rarely matters, and drum performances overlap constantly.
- **Stuck notes** that never end. Some apps cut them off, some hold them forever. The fix ends them at the end of their track.
- **Stray note releases** with no note playing. Removed.
- **Notes with no length** are reported but left alone, because no length is right for every case.

Fixes never move a note's start, key or velocity, and never touch controllers, track names or markers. Your original file is never changed: the cleaned copy is saved next to it with "(tempo fixed)" added to the name, then read back to prove every note still starts at the same moment. To leave notes alone and fix only the tempo map, turn off **Also check and fix notes** in the app, or add `--tempo-only` on the command line.

It reads every kind of Standard MIDI File, including Windows .rmi files (the cleaned copy is saved as a plain .mid) and files that count time in video frames instead of beats.

## Does it line up with the recording?

Choose the MIDI file, press **Compare with another file**, and pick the recording it should match: a WAV, AIFF, MP3, M4A, FLAC or CAF. The MIDI can be one part of the recording, like a piano part against the full mix, and the recording can start earlier or end later. You get one of these answers:

- **Lines up**, and how closely. Note attacks can be measured to about 10 to 20 ms, so that's what "lines up" means.
- **An offset**: the recording is later or earlier by the same amount all the way through, so sliding one fixes it.
- **A jump** at one place, with the time and bar. When the MIDI rests across it, the report gives the range of the rest. A jump with no tempo change usually means time was added or removed. If the MIDI came from REAPER, compare it with the project next: that is usually the bar its export made whole.
- **A different tempo**, section by section: "the audio is slower, about 155 BPM where the MIDI says 160", and how far apart they've drifted by the end of each section. That means the two came from different versions of the tempo map.

It follows the harmony first, so it can track a recording that wanders a long way from the MIDI, then checks the note attacks to the millisecond. A five-minute song takes about two seconds.

## Did an app change my file?

**Compare with another file** answers that. Save the same music from two places, for example your export and the file another app saved after importing it, and compare them. It reports whether the tempo matches beat by beat, whether the same notes are there, and how far any note moved. Different track layouts don't matter. Differences under 2 ms are rounding no one can hear (some apps save at a lower resolution) and are reported as matching.

You can also compare against a REAPER project (.rpp), which reads the project's tempo map directly. That confirms an export matches the session it came from, and catches the bar REAPER's export made whole. After that comparison, **Fix and save cleaned copies** moves everything back to where the project plays it.

## Ableton Live sets for AbleSet

Write the song anywhere, put markers where the sections start, and export MIDI. **Make Ableton Set** (Command-L or Control-L) turns it into a Live set with a locator at every marker, named the way [AbleSet](https://ableset.com) reads them: the song's name at bar 1, "> Verse" for each section, and SONG END where it stops. The file's tempo map and time signatures come too, so Live's bars land where the song's do.

- **Markers from the file, typed in, or both.** Every marker is listed by bar and beat ("Bar 33, beat 3: Pickup"). Add as many more as you like: type the bar, the beat and the name, then press Return, and you're back on Bar for the next one. **Edit as Text** shows every marker as one line each, like `17 Verse` or `33.3 Pickup`, to edit them all at once, paste in a list, or copy them out to keep. No MIDI file at all works too: give the tempo and time signature and type the markers.
- **A new set, or one you already have.** A new set starts from the empty set inside your installed Live (if Live is somewhere unusual, you choose a set to start from once, and it's remembered). Several MIDI files make one set with the songs back to back, or one set each for AbleSet's multi-file projects. In a set you already have, a song goes after the last one with its tempo map, or onto a song already there, replacing that song's section locators and leaving its tempo alone. When the set's tempo and the file's disagree, you choose how the markers line up: by bar (the set is sped up or slowed down on purpose) or by time (stems that aren't warped).
- **Your files are never changed.** A set you already have is saved as a copy named "(markers added)" unless you choose to update it, and then the old one is kept as "(before markers)". Every set is read back and checked before it's kept: each locator where it should be, and with the tempo map written, each one playing at its marker's moment to the millisecond.
- **What the set will hold** shows every locator, by bar and beat, before anything is saved. Afterwards, **File, Open the Last Ableton Set in Live** opens it.
- **Bigger Text** in the View menu reaches this window too.

**Straight from a REAPER project.** Choose the .RPP itself and skip the MIDI export: its markers, regions, tempo map and time signatures come straight from the project, and REAPER's =START and =END markers set where the song starts and ends. REAPER's tempo ramps are linear in time and Live's are linear in beats, so a ramp becomes small steps a 32nd note apart, the way REAPER's own MIDI export writes it. Checked on a real song: the set made from the project and the one made from its MIDI export agree to a tenth of a millisecond.

**Lyrics for AbleSet.** Tick "Add a lyrics track for AbleSet" and the song's lyrics become a lyrics track: one clip per line, on a track named "Lyrics +LYRICS" (or the set's own +LYRICS track), so AbleSet shows each line as it's sung. They come from the MIDI file's lyrics (most notation apps and karaoke files carry them) or you type them in. **Edit Lyrics as Text** shows every line with its bar and beat, like `78 Patience...`, to read, fix, paste or copy. When a file has lyrics on several tracks (a lead and harmonies), the one with the most is used and the others are named. Where a file doesn't say where words end, syllables are kept apart by spaces for you to join.

Markers can carry AbleSet's own notation straight from your DAW: "> Chorus +LOOP", "SONG END", "* note to self" all go through as written. Only AbleSet's capitals count as commands, so a section your band calls "Stop" stays a section and never stops the show. A marker called End, as the last marker, becomes SONG END. From REAPER, choose the project itself (above); if you'd rather send a MIDI file, export with File, Export project MIDI and set "Export project markers as MIDI" to markers.

For Live on its own, **Locator names** can be just the marker names, with no song name and no SONG END.

Putting markers onto a song whose tempo automation has curves works out the curves exactly. That was checked against Live's own clock: straight lines would have been off by up to six seconds.

## Download

Get the latest version from the [Releases page](../../releases):

- **Mac:** MIDI-Tempo-Fix-mac.zip. Unzip it and move MIDI Tempo Fix to Applications. It's signed with a Developer ID and notarized by Apple, so it opens like any other app.

  The download is built for Apple silicon Macs. On an Intel Mac, install the command line version below and run `midi-tempo-fix` to open the app.

- **Windows:** MIDI-Tempo-Fix-Setup.exe installs it for you alone, with no administrator password, a Start menu entry, and an entry in Settings, Apps to uninstall it. Installed copies update themselves. Or take MIDI-Tempo-Fix-windows.zip, unzip it anywhere and run MIDI Tempo Fix.exe inside the folder. If SmartScreen warns you, choose More info, then Run anyway.

## Using the app

1. Press **Choose MIDI files** (Command-O on Mac, Control-O on Windows) and pick one or more files, or **Choose a folder** (Command-Shift-O or Control-Shift-O) to check every MIDI file in it, including folders inside it. You can also drop files or folders on the window, or on the app's Dock icon on a Mac. Drop a REAPER project (.RPP) instead and Make Ableton Set opens with it.
2. The **Files** list shows every file with its status first ("Needs fixing: name, what's wrong" or "Clean: name"), files that need fixing at the top. Arrow through it to hear each verdict, and select a file to read everything about it in the report.
3. The report starts with the answer: each file is either "clean" or "needs fixing", with the problems named. The details follow. With more than one file, a summary line comes first ("Checked 40 files: 3 need fixing, 37 clean"). With more than 20, only the files that need fixing are listed, so the report stays short enough to listen to. Focus moves to the report, so your screen reader reads it right away. Big folders are checked in the background on every processor core (a 5,000-file library takes about four seconds on a recent Mac), with a progress bar, progress in the title bar, and the count spoken now and then. **Stop** (or Escape) ends a batch early and keeps the results so far.
4. If something needs fixing, press **Fix and save cleaned copies** (Command-S or Control-S). The report says where each copy went, and **Show cleaned files** (Command-Shift-S or Control-Shift-S) opens that folder with them selected.
5. **Compare with another file** (Command-R or Control-R) compares the chosen files with a recording, any other MIDI file, or a REAPER project. A recording takes a few seconds per file; the window says so while it listens, and Escape stops.
6. **Make Ableton Set** (Command-L or Control-L) makes a Live set from the selected file's markers, a REAPER project, or markers you type in, with a lyrics track if you want one. See above. **File, Open the Last Ableton Set in Live** opens the set it saved, and **File, Show the Last Ableton Set** (Command-Shift-L or Control-Shift-L) shows it in its folder.
7. **Copy report** (Command-Shift-C or Control-Shift-C) puts the whole report on the clipboard, ready to paste into a message. **File, Save report as text** saves it as a file. **Help, MIDI Tempo Fix Help** opens this page.

**Bigger text:** View, Bigger Text (Command-equals or Control-equals) makes the list, report, labels and checkbox larger; Smaller Text and Actual Size undo it. The size is remembered. Verdicts and summaries are bold, so the answer stands out by weight as well as by words; nothing relies on colour.

When a long batch finishes, the app plays a sound and bounces its Dock icon, in case you've switched to something else.

The app remembers whether notes are checked, your text size, which folder you last chose files from, and your Make Ableton Set choices (locator names, quick-jump sections, SONG END, and the set you last added to).

**Updates:** once a day the app asks GitHub whether there's a newer version, and the title bar and the report say so if there is. Nothing pops up. **Help, Check for Updates** opens the update window, with what's new and Install. On a Mac and in an installed Windows copy that's Sparkle (WinSparkle on Windows), and every update is signed, so a copy only installs what was really released here. A Windows copy run from the zip downloads the new version, checks that it starts, and replaces itself. Nothing else is sent anywhere.

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
midi-tempo check export.mid --compare "mix.wav"
midi-tempo fix export.mid --project "My Song.RPP"
midi-tempo fix song.mid
midi-tempo fix "Exports folder"
midi-tempo fix --tempo-only song.mid
midi-tempo ableton song.mid
midi-tempo ableton song.mid --marker "17 Verse" --marker "33.3 Pickup"
midi-tempo ableton song.mid --into "Show.als"
midi-tempo ableton "My Song.RPP"
midi-tempo ableton song.mid --lyrics
midi-tempo ableton song.mid --lyrics-text lyrics.txt
midi-tempo ableton --songs "Show.als"
midi-tempo ableton song.mid --into "Show.als" --onto "Song Name" --line-up bars
midi-tempo ableton --name "New Song" --tempo 96 --time 6/8 --markers markers.txt
midi-tempo ableton *.mid --each
```

`ableton` makes a Live set from markers: `--marker` adds typed ones, `--markers` reads a list from a file, `--into` adds to a set you have (a copy is saved), `--onto` puts the markers on a song already there, `--songs` lists a set's songs and each one's sections by bar, `--each` makes one set per file, `--lyrics` adds the file's lyrics as an AbleSet lyrics track and `--lyrics-text` reads typed lines instead, `--plain` names locators for Live without AbleSet, and `--preview` says what the set would hold without saving anything.

Both commands use every processor core but one; `--jobs 1` keeps it to one. `--json` prints the results in a form scripts and other tools can read. `check` starts each file with its verdict, and puts a summary line first when there's more than one. `--brief` gives one line per file. `-r` also looks inside folders within folders. `check` changes nothing. It exits with 0 when files are clean, 1 when something needs fixing, and 2 when a file can't be read. `fix` takes files or folders and saves a cleaned copy of each file that needs one. The command line needs nothing beyond Python itself, except checking against a recording, which needs numpy (`pip install numpy`). The app includes it. Recordings other than WAV are converted with afconvert on a Mac, or ffmpeg if it's installed. To open the app from a pip install, add the app extra (`pip install "midi-tempo-fix[app] @ git+https://github.com/Matthew2244/midi-tempo-fix"`) and run `midi-tempo-fix`.

Output is plain sentences, printed once per command, so a screen reader doesn't get interrupted mid-sentence.

## How it's tested

Before each release it runs against more than 17,000 real MIDI files from many apps and libraries, and thousands of deliberately broken ones. The recording check is tested on synthetic songs with known offsets, jumps and tempo changes, and on real songs whose answers were measured by hand. Every cleaned copy has to pass its own read-back check, compare as identical to the original apart from what was fixed, and have nothing left to fix.

Make Ableton Set was checked in Live 12 with AbleSet running, by reading back what AbleSet sees: every song length to the millisecond, every section on its beat, and every lyric line where it's sung. Sets made from REAPER projects were compared with REAPER's own MIDI exports, and more than a thousand real projects were read without an error. Every set it saves is read back and checked before it's kept.

## Building it yourself

```
python -m pip install wxPython pyinstaller pytest certifi numpy
python -m pytest
python build_app.py
```

`build_app.py` builds the app for whichever system it runs on and puts a zip in `dist`. It fetches Sparkle (Mac) or WinSparkle (Windows) itself, checking each download against a known checksum. On Windows with Inno Setup 6 installed, it also builds the installer.

## License

MIT. Free to use, share and change.
