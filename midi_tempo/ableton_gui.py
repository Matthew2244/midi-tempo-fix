"""The Make an Ableton Set window: markers to Live locators, for AbleSet or Live on its own.

One column, each label above its control, in tab order: where the song comes
from, its name, the markers (a list with Add, Change, Remove and Type
Several), where it goes, a few options, then a preview of exactly what will
be saved, read straight from the same code that saves it. Nothing is written
until Make Set; the preview is how you check first.

Every marker is shown and edited as a bar and a beat, whether it came from the
file or was typed in, and there is no limit on how many. The file's own
markers are turned into bar and beat with its time signatures, so changing or
removing one is the same as for a typed one.
"""
from __future__ import annotations

import os
import sys
import threading

import wx

from . import ableton as A
from .gui import MIDI_PATTERNS, drop_mac_mnemonics, keys
from .core import MidiError


CMD = "Control" if sys.platform == "win32" else "Command"  # how a shortcut is said on each system


def _beat_text(beat: float) -> str:
    return f"{round(beat, 3):g}"


def marker_row(m: A.TypedMarker, source: str) -> str:
    """'Bar 17, beat 3: Pickup (from the file)'. Where first: a listener hears the place, then the name."""
    where = f"Bar {m.bar}" + (f", beat {_beat_text(m.beat)}" if m.beat != 1 else "")
    return f"{where}: {m.text or 'no name'}" + (" (from the file)" if source == "file" else "")


class TextDialog(wx.Dialog):
    """Every marker as plain text, one per line: read it, edit it, paste a list in, or copy it out."""

    LYRICS_INTRO = ("One lyric line per line: the bar where it's sung, then a dot and the beat if it isn't the "
                    "downbeat, then the words. Like 17 Here comes the morning, or 25.3 and the light gets in. "
                    "Each becomes a clip on the lyrics track and shows in AbleSet when it's sung. What's here "
                    "replaces the lyrics. Copy it out to keep it or share it.")

    def __init__(self, parent, text: str, lyrics: bool = False):
        title = "Edit Lyrics as Text" if lyrics else "Edit Markers as Text"
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        p = wx.Panel(self)
        intro = wx.StaticText(p, label=self.LYRICS_INTRO if lyrics else (
            "One marker per line: the bar, then a dot and the beat if it isn't the downbeat, then the name. "
            "Like 17 Verse, or 33.3 Pickup for bar 33, beat 3. What's here replaces the list. Copy it out to "
            "keep it or share it."))
        intro.Wrap(440)
        what = "Lyrics" if lyrics else "Markers"
        label = wx.StaticText(p, label=f"&{what}")
        self.text = wx.TextCtrl(p, value=text, style=wx.TE_MULTILINE, size=(460, 260), name=what)
        self.text.SetHint("17 Here comes the morning\n25.3 and the light gets in" if lyrics else
                          "17 Verse\n33.3 Pickup\n49 Chorus +LOOP\n97 End")
        from .gui import mac_accessibility_label
        mac_accessibility_label(self.text, what)
        self.lyrics = lyrics
        cancel = wx.Button(p, wx.ID_CANCEL, "Cancel")  # made first: Tab goes in the order they're shown
        ok = wx.Button(p, wx.ID_OK, f"Use These {what}")
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(intro, 0, wx.ALL, 12)
        s.Add(label, 0, wx.LEFT | wx.RIGHT, 12)
        s.Add(self.text, 1, wx.EXPAND | wx.ALL, 12)
        b = wx.BoxSizer(wx.HORIZONTAL)
        b.Add(cancel, 0, wx.RIGHT, 8)
        b.Add(ok)
        s.Add(b, 0, wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        p.SetSizer(s)
        s.Fit(self)
        self.fonts = [intro, label, self.text]
        drop_mac_mnemonics(self)
        self.text.SetFocus()
        self.text.SetInsertionPointEnd()
        ok.Bind(wx.EVT_BUTTON, self._ok)

    def _ok(self, _):
        try:
            self.result = A.parse_markers(self.text.GetValue())
        except A.SetError as e:
            wx.MessageBox(str(e) + ".", "Markers", wx.OK, self)
            self.text.SetFocus()
            return
        self.EndModal(wx.ID_OK)


class AbletonDialog(wx.Dialog):
    NEW, EXISTING = 0, 1

    def __init__(self, main, files: list[str]):
        super().__init__(main, title="Make an Ableton Set",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.main = main
        self.cfg = main.cfg
        self.files = list(files)
        self.path = self.files[0] if self.files else None
        self.items: list[tuple[A.TypedMarker, str]] = []  # (marker, "file" or "typed")
        self.set_path: str | None = None
        self.live_set: A.LiveSet | None = None
        self.result = None
        # The REAPER project the main window compared a file with: its padded short bars are undone
        # here too, or every locator after one would land late
        self.projects = dict(getattr(main, "project_for", {}) or {})
        self._preview_timer = None
        self._loading = False

        from .gui import mac_accessibility_label
        p = self.panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
        p.SetScrollRate(0, 20)
        s = wx.BoxSizer(wx.VERTICAL)

        self._labels: dict = {}

        def add(label, ctrl, gap=12):
            if label:
                lab = wx.StaticText(p, label=label)
                s.Add(lab, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
                self._labels[ctrl] = lab
                # VoiceOver doesn't take a field's name from the label above it, so say it outright
                wx.CallAfter(mac_accessibility_label, ctrl, label.replace("&", ""))
            s.Add(ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6 if label else gap)
            return ctrl

        # One sentence: VoiceOver reads every label in this window again each time a file dialog closes
        intro = wx.StaticText(p, label="Turns markers into Live locators AbleSet reads, with the song's tempo. "
                                       "Nothing is saved until Make Set, and your files are never changed.")
        intro.Wrap(520)
        s.Add(intro, 0, wx.ALL, 12)

        self.source = add("Song &from", wx.TextCtrl(p, style=wx.TE_READONLY, name="Song from"))
        src_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.choose_midi = wx.Button(p, label="Choose MIDI File or REAPER Project...")
        self.choose_midi.SetToolTip("The MIDI file or REAPER project whose markers, tempo and time signatures make "
                                    "the song. A REAPER project needs no MIDI export, and its regions count too.")
        self.no_midi = wx.Button(p, label="No File")
        self.no_midi.SetToolTip("Make the song from markers you type in, at a tempo and time signature you give.")
        src_buttons.Add(self.choose_midi, 0, wx.RIGHT, 8)
        src_buttons.Add(self.no_midi)
        s.Add(src_buttons, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        self.songs_choice = None
        if len(self.files) > 1:
            self.songs_choice = add("Songs", wx.Choice(p, choices=[
                "Just this one", f"All {len(self.files)} files, back to back in one set",
                f"All {len(self.files)} files, one set each"], name="Songs"))
            self.songs_choice.SetSelection(0)
            self.songs_choice.SetToolTip("Typed markers go with one song at a time, so they're for Just this one.")

        self.tempo = add("&Tempo, with no file", wx.TextCtrl(p, value="120", name="Tempo"))
        self.tempo.SetToolTip("Beats per minute, 20 to 999.")
        self.signature = add("Time si&gnature, with no file", wx.TextCtrl(p, value="4/4", name="Time signature"))
        self.signature.SetToolTip("Like 4/4 or 6/8.")

        self.name = add("&Song name", wx.TextCtrl(p, name="Song name"))
        self.name.SetToolTip("The song locator's name: what AbleSet shows in the setlist. It goes at the very start of the "
                              "song on its own, so it isn't one of your markers. An Intro on bar 1 shares that spot.")

        self.list = add("Mar&kers", wx.ListBox(p, style=wx.LB_SINGLE, size=(-1, 170), name="Markers"))
        mac_accessibility_label(self.list, "Markers")
        self.list.SetToolTip("Every marker, by bar and beat, the file's own included: change, remove or add any of "
                             f"them. Your file isn't changed, only the set. {CMD}-Z here undoes the last change.")
        mk = wx.BoxSizer(wx.HORIZONTAL)
        self.change_btn = wx.Button(p, label="&Change")
        self.change_btn.SetToolTip("Puts the selected marker in the fields below to change it. Return in the list "
                                   "does the same.")
        self.remove_btn = wx.Button(p, label="&Remove")
        self.remove_btn.SetToolTip("Remove the selected marker. Delete in the list does the same.")
        self.several_btn = wx.Button(p, label="Edit as Te&xt...")
        self.several_btn.SetToolTip("Every marker as plain text, one per line, like 17 Verse: edit them all at "
                                    "once, paste a list in, or copy them out.")
        self.undo_btn = wx.Button(p, label="&Undo")
        self.undo_btn.SetToolTip("Takes back the last change to the markers: an add, a change, a remove or an edit as "
                                 f"text. Press it again to go further back. {CMD}-Z in the list does the same.")
        for b in (self.change_btn, self.remove_btn, self.undo_btn, self.several_btn):
            mk.Add(b, 0, wx.RIGHT, 8)
        s.Add(mk, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.save_midi_btn = wx.Button(p, label="Save Markers in a M&IDI Copy...")
        self.save_midi_btn.SetToolTip("Saves a copy of the MIDI file with these markers in it, so other apps get "
                                      "your changes too. The copy goes next to your file; the file itself is never "
                                      "changed. Cue points are saved as markers, and if the file's timing needed "
                                      "fixing, the copy's is fixed too.")
        s.Add(self.save_midi_btn, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.undo: list[tuple] = []  # (the list as it was, what was done, the marker to land on), newest last

        # A new marker is typed right here, no window of its own: closing a small window made
        # VoiceOver read this whole window again and lose the new row. Return adds it and comes
        # back to Bar, so a long list goes in quickly.
        self.m_bar = add("New marker: &bar", wx.TextCtrl(p, value="1", style=wx.TE_PROCESS_ENTER, name="Bar"))
        self.m_bar.SetToolTip(f"The bar, counting the first as 1. {CMD}-N comes here.")
        self.m_beat = add("B&eat", wx.TextCtrl(p, value="1", style=wx.TE_PROCESS_ENTER, name="Beat"))
        self.m_beat.SetToolTip("1 is the downbeat. Halfway to beat 3 is 2.5.")
        self.m_name = add("Marker &name", wx.TextCtrl(p, style=wx.TE_PROCESS_ENTER, name="Marker name"))
        self.m_name.SetToolTip("Like Verse 2. Put each one where that part starts, an Intro too, even on bar 1. End marks "
                                 "where the song stops. Return adds it.")
        self.add_btn = wx.Button(p, label="&Add Marker")
        self.add_btn.SetToolTip("Adds the marker typed above. Return in any of the three fields does the same.")
        s.Add(self.add_btn, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        # Lyrics: AbleSet shows them as they're sung, from a +LYRICS track with one clip per line
        self.lyric_items: list[A.TypedMarker] = []
        self.lyrics_cb = wx.CheckBox(p, label="Add a l&yrics track for AbleSet")
        self.lyrics_cb.SetToolTip("One clip per line on a track named Lyrics +LYRICS, or on the set's own +LYRICS "
                                  "track. The lines come from the MIDI file's lyrics, or type them in.")
        s.Add(self.lyrics_cb, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.lyrics_btn = wx.Button(p, label="Edit Lyrics as Text...")
        self.lyrics_btn.SetToolTip("Every lyric line, one per line with its bar and beat, like 17 First line: "
                                   "read them, fix them, paste them in or copy them out.")
        s.Add(self.lyrics_btn, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.editing = None  # the marker being changed (the object, not its row), while Change has it

        self.dest = add("&Put it in", wx.Choice(p, choices=["A new Live set", "A set I already have"],
                                                 name="Put it in"))
        self.dest.SetSelection(0)
        self.choose_set = wx.Button(p, label="Choose Set...")
        self.choose_set.SetToolTip("The Live set to add the song to. A copy is saved; the set itself isn't changed "
                                   "unless you choose to update it.")
        s.Add(self.choose_set, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.where = add("&Where in the set", wx.Choice(p, choices=["After the last song"], name="Where in the set"))
        self.where.SetSelection(0)
        self.where.SetToolTip("After the last song brings the song's tempo map with it. Onto a song already there "
                              "replaces that song's section locators and keeps the set's tempo.")
        self.line_up = add("&Line up by, if the set's tempo isn't the file's", wx.Choice(p, choices=[
            "Bar: follow the set's tempo (sped up or slowed down on purpose)",
            "Time: each marker at its moment (stems that aren't warped)"], name="Line up by"))
        self.line_up.SetSelection(0)
        self.save_as = add("Sa&ve", wx.Choice(p, choices=[
            "As a new copy next to the set", "Update the set itself, keeping a backup"], name="Save"))
        self.save_as.SetSelection(0)

        self.style = add("L&ocator names", wx.Choice(p, choices=[
            "For AbleSet: the song's name, > Verse, SONG END", "For Live on its own: just the marker names"],
            name="Locator names"))
        self.style.SetSelection(1 if self.cfg.ReadBool("ableton_plain", False) else 0)
        self.quick = wx.CheckBox(p, label="Make every section a &quick-jump section (>>)")
        self.quick.SetValue(self.cfg.ReadBool("ableton_quick", False))
        s.Add(self.quick, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.song_end = wx.CheckBox(p, label="Add SONG EN&D where the song ends")
        self.song_end.SetValue(self.cfg.ReadBool("ableton_song_end", True))
        self.song_end.SetToolTip("At an End marker if there is one, otherwise at the end of the bar where the music "
                                 "stops.")
        s.Add(self.song_end, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self.preview = add("What the set will &hold", wx.TextCtrl(
            p, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 200), name="What the set will hold"))
        mac_accessibility_label(self.preview, "What the set will hold")
        p.SetSizer(s)

        cancel = wx.Button(self, wx.ID_CANCEL, "Cancel")  # made first: Tab goes in the order they're shown
        self.make = wx.Button(self, wx.ID_OK, "&Make Set")
        self.make.SetDefault()
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(p, 1, wx.EXPAND)
        b = wx.BoxSizer(wx.HORIZONTAL)
        b.Add(cancel, 0, wx.RIGHT, 8)
        b.Add(self.make)
        outer.Add(b, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        self.status = None
        if sys.platform == "win32":  # see _say: the main window's status bar is out of reach behind this one
            self.status = wx.StatusBar(self)
            outer.Add(self.status, 0, wx.EXPAND)
        self.SetSizer(outer)
        for c in p.GetChildren():  # the scroll bar is for the mouse; Tab should go control to control
            if isinstance(c, wx.ScrollBar):
                c.DisableFocusFromKeyboard()
        drop_mac_mnemonics(self)
        self.SetSize((620, 780))
        self._fonts([c for c in p.GetChildren() if isinstance(c, (wx.StaticText, wx.TextCtrl, wx.ListBox,
                                                                    wx.CheckBox))])
        p.Layout()
        p.FitInside()
        self.SetMinSize((480, 420))
        self.CentreOnParent()

        self.choose_midi.Bind(wx.EVT_BUTTON, self.on_choose_midi)
        self.no_midi.Bind(wx.EVT_BUTTON, self.on_no_midi)
        self.add_btn.Bind(wx.EVT_BUTTON, self.on_add)
        for c in (self.m_bar, self.m_beat, self.m_name):
            c.Bind(wx.EVT_TEXT_ENTER, self.on_add)
        self.Bind(wx.EVT_BUTTON, self.on_cancel, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self.on_cancel)
        self.change_btn.Bind(wx.EVT_BUTTON, self.on_change)
        self.remove_btn.Bind(wx.EVT_BUTTON, self.on_remove)
        self.undo_btn.Bind(wx.EVT_BUTTON, self.on_undo)
        self.save_midi_btn.Bind(wx.EVT_BUTTON, self.on_save_midi)
        self.several_btn.Bind(wx.EVT_BUTTON, self.on_several)
        self.lyrics_btn.Bind(wx.EVT_BUTTON, self.on_lyrics)
        self.lyrics_cb.Bind(wx.EVT_CHECKBOX, self.on_option)
        self.choose_set.Bind(wx.EVT_BUTTON, self.on_choose_set)
        self.make.Bind(wx.EVT_BUTTON, self.on_make)
        self.list.Bind(wx.EVT_KEY_DOWN, self.on_list_key)
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, self.on_change)
        self.list.Bind(wx.EVT_LISTBOX, lambda e: self._enable())
        for c in (self.dest, self.where, self.line_up, self.save_as, self.style):
            c.Bind(wx.EVT_CHOICE, self.on_option)
        if self.songs_choice:
            self.songs_choice.Bind(wx.EVT_CHOICE, self.on_option)
        for c in (self.quick, self.song_end):
            c.Bind(wx.EVT_CHECKBOX, self.on_option)
        for c in (self.name, self.tempo, self.signature):
            c.Bind(wx.EVT_TEXT, lambda e: self._later())
        new_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, lambda e: self.m_bar.SetFocus(), id=new_id)
        self.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_CTRL, ord("N"), new_id)]))

        last_set = self.cfg.Read("ableton_last_set", "")
        if last_set and os.path.exists(last_set) and self.cfg.ReadBool("ableton_into_existing", False):
            self.dest.SetSelection(self.EXISTING)
            self._load_set(last_set, quiet=True)
        self._load_midi(self.path)
        self.name.SetFocus()

    # ---------------------------------------------------------------- reading what's chosen

    def _load_midi(self, path: str | None):
        self.path = path
        self.file_original = []
        self._loading = True
        self.items = [(m, t) for m, t in self.items if t == "typed"]
        if getattr(self, "editing", None) is not None and not any(m is self.editing for m, _ in self.items):
            self._stop_editing()
        if path:
            try:
                file_items, _ = A.file_markers_typed(path, self._project())
            except (MidiError, OSError, ValueError) as e:
                wx.MessageBox(f"{os.path.basename(path)} couldn't be read: {e}.", "Make an Ableton Set", wx.OK, self)
                file_items = []
                self.path = None
            self.file_original = sorted((m.bar, round(m.beat, 6), m.text) for m in file_items)
            self.items = [(m, "file") for m in file_items] + self.items
            self.items.sort(key=lambda x: (x[0].bar, x[0].beat))
            self.source.SetValue(os.path.basename(path) if self.path else "No file: markers typed in only")
            # The file's lyrics, ready to use and to edit; the box is ticked when there are some
            self.lyric_items, self.lyrics_said = [], []
            if self.path and not A.is_rpp(self.path):
                try:
                    self.lyric_items, self.lyrics_said = A.file_lyrics_typed(self.path, self._project())
                except (MidiError, OSError, ValueError):
                    pass
            self.lyrics_cb.SetValue(bool(self.lyric_items))
            if self.path:
                self.name.ChangeValue(A.song_name_for(path))
        else:
            self.source.SetValue("No file: markers typed in only")
            self.lyric_items, self.lyrics_said = [], []  # nothing of the last file's stays behind
            self.lyrics_cb.SetValue(False)
        self.undo = []
        # What Cancel compares with: the file's own markers only, so markers typed before choosing
        # another file still count as work to lose
        self.original = getattr(self, "file_original", []) if self.path else []
        self._loading = False
        self._fill()

    def _project(self) -> str | None:
        return self.projects.get(self.path) if self.path and not A.is_rpp(self.path) else None

    def _snapshot(self):
        return sorted((m.bar, round(m.beat, 6), m.text) for m, _ in self.items)

    def _push(self, what: str, focus=None):
        """Remember the list before a change, so Undo can put it back and land on the marker it restored."""
        if focus is None and 0 <= self.list.GetSelection() < len(self.items):
            focus = self.items[self.list.GetSelection()][0]
        self.undo.append(([(m, t) for m, t in self.items], what, focus))

    def _load_set(self, path: str, quiet: bool = False):
        try:
            ls = A.LiveSet.load(path)
        except A.SetError as e:
            if not quiet:
                wx.MessageBox(str(e)[0].upper() + str(e)[1:] + ".", "Choose Set", wx.OK, self)
            return
        self.set_path, self.live_set = path, ls
        songs = A.set_songs(ls)
        self.where.Set(["After the last song"] + [f"Onto {A.song_title(x.name)}" for x, _ in songs])
        self.song_locators = [x.name for x, _ in songs]
        pick = 0
        want = self.name.GetValue().strip().lower()
        for k, x in enumerate(self.song_locators, 1):
            if want and A.song_title(x).lower() == want:
                pick = k  # a song with this name is already in the set: likely the one to put markers on
        self.where.SetSelection(pick)
        self.cfg.Write("ableton_last_set", path)
        self.cfg.Flush()

    def _plans(self) -> list[A.SongPlan]:
        opts = dict(quick=self.quick.GetValue(), song_end=self.song_end.GetValue(),
                    plain=self.style.GetSelection() == 1)
        typed = [m for m, _ in self.items]
        which = self.songs_choice.GetSelection() if self.songs_choice else 0
        want_lyrics = self.lyrics_cb.GetValue()
        if which and self.files:
            return [A.plan_song(f, lyrics="file" if want_lyrics else None, project=self.projects.get(f), **opts)
                    for f in self.files]
        lyrics = list(self.lyric_items) if want_lyrics else None
        name = self.name.GetValue().strip() or None
        if self.path:
            return [A.plan_song(self.path, name, typed=typed, file_markers=False, lyrics=lyrics,
                                project=self._project(), **opts)]
        try:
            tempo = float(self.tempo.GetValue().strip() or "120")
        except ValueError:
            raise A.SetError("write the tempo as a number of beats per minute, like 96")
        try:
            num, den = (int(x) for x in (self.signature.GetValue().strip() or "4/4").split("/"))
        except ValueError:
            raise A.SetError("write the time signature like 4/4 or 6/8")
        onto = self._onto()
        if not name and onto:
            name = A.song_title(onto)
        if not name:
            raise A.SetError("give the song a name")
        return [A.plan_song(None, name, typed=typed, tempo=tempo, signature=(num, den), lyrics=lyrics, **opts)]

    def _onto(self) -> str | None:
        if self.dest.GetSelection() != self.EXISTING or not self.live_set:
            return None
        k = self.where.GetSelection()
        return self.song_locators[k - 1] if k > 0 else None

    # ---------------------------------------------------------------- showing it

    def _fill(self, select: int | None = None):
        self.list.Set([marker_row(m, t) for m, t in self.items] or [])
        if self.items:
            self.list.SetSelection(min(select if select is not None else 0, len(self.items) - 1))
        self._enable()
        self._later()

    def _enable(self):
        has = self.list.GetSelection() != wx.NOT_FOUND
        self.change_btn.Enable(has)
        self.remove_btn.Enable(has)
        self.undo_btn.Enable(bool(getattr(self, "undo", None)))
        midi_file = bool(self.path) and not A.is_rpp(self.path)
        no_file = self.path is None
        existing = self.dest.GetSelection() == self.EXISTING
        several = bool(self.songs_choice and self.songs_choice.GetSelection())
        # What doesn't apply is hidden, not dimmed: a shorter window, and nothing to tab past
        changed = False
        for c, on in ((self.tempo, no_file), (self.signature, no_file), (self.choose_set, existing),
                      (self.where, existing and self.set_path is not None), (self.save_as, existing),
                      (self.line_up, existing and self._onto() is not None and not no_file)):
            if c.IsShown() != on:
                c.Show(on)
                if c in self._labels:
                    self._labels[c].Show(on)
                changed = True
        for c in (self.add_btn, self.several_btn, self.list, self.m_bar, self.m_beat, self.m_name, self.lyrics_btn):
            c.Enable(not several)
        if self.save_midi_btn.IsShown() != (midi_file and not several):
            self.save_midi_btn.Show(midi_file and not several)
            changed = True
        if several:
            self.undo_btn.Enable(False)
        label = f"Choose Set... ({os.path.basename(self.set_path)})" if self.set_path else "Choose Set..."
        if self.choose_set.GetLabel() != label:
            self.choose_set.SetLabel(label)
            changed = True
        if changed:
            self.panel.Layout()
            self.panel.FitInside()

    def _later(self):
        if self._loading:
            return
        if self._preview_timer:
            self._preview_timer.Stop()
        self._preview_timer = wx.CallLater(250, self._update_preview)

    def _run(self, dry_run: bool, line_up: str | None = None):
        plans = self._plans()
        existing = self.dest.GetSelection() == self.EXISTING
        if existing:
            if not self.set_path:
                raise A.SetError("choose the set to add it to")
            onto = self._onto()
            if onto and not self.path and len(plans) == 1:
                pass  # typed markers alone: counted from the song's start, in the set's own bars
            return A.add_to_set(self.set_path, plans, onto=onto, line_up=line_up, dry_run=dry_run,
                                in_place=self.save_as.GetSelection() == 1, ls=self.live_set)
        which = self.songs_choice.GetSelection() if self.songs_choice else 0
        if dry_run:
            return A.new_set(plans, "", dry_run=True)
        if which == 2:
            return [A.new_set([p], A.default_new_set_path(p), self.template) for p in plans]
        return A.new_set(plans, self.out_path, self.template)

    def _update_preview(self):
        self._preview_timer = None
        lines = []
        try:
            plans = self._plans()
            for p in plans:
                desc = A.describe_plan(p)
                extra = []
                if len(plans) == 1 and self.lyrics_cb.GetValue():
                    # where the lyrics came from, right under the song's first line
                    extra = (list(getattr(self, "lyrics_said", []) or []) if self.lyric_items else
                             ["No lyrics yet: Edit Lyrics as Text to type them in, or the lyrics track is left out."])
                lines += desc[:1] + extra + desc[1:] + [""]
            if self.dest.GetSelection() == self.EXISTING and self.set_path:
                lines += self._where_words(plans)
        except A.SetError as e:
            msg = str(e)
            lines = [msg[0].upper() + msg[1:] + "."]
        except (MidiError, OSError) as e:
            lines = [f"Couldn't read it: {e}."]
        self.preview.SetValue("\n".join(lines).rstrip())
        self.preview.SetInsertionPoint(0)

    def _where_words(self, plans) -> list[str]:
        try:
            r = self._run(True, None)
        except A.NeedsLineUp as e:
            how = "by bar" if self.line_up.GetSelection() == 0 else "by time"
            return [f"The set's tempo for this song isn't the file's: by its last marker they're {e.worst:.1f} "
                    f"seconds apart. The markers will line up {how}, as chosen under Line up by."]
        lines = []
        for pl in r.placed:
            bar, _ = A.bar_number(self.live_set.sigs(), pl.start)
            if pl.line_up == "tempo":
                lines.append(f"{pl.plan.name} goes in at bar {bar}, after the last thing in the set, with its "
                             "tempo map.")
            else:
                gone = (f"the {A.n(pl.replaced, 'section locator')} there now {'is' if pl.replaced == 1 else 'are'} "
                        "replaced" if pl.replaced else "it has no section locators yet")
                lines.append(f"Onto {pl.plan.name} at bar {bar}: {gone}, and the set's tempo is kept" +
                             (", which matches the file's." if pl.worst <= A.SAME_SECONDS and self.path else "."))
                if pl.gone:
                    lines.append("There now, and replaced:")
                    sigs = self.live_set.sigs()
                    for gb, gname in pl.gone:
                        lines.append(f"  {A.where_text(sigs, gb, capital=True)}: {gname}")
        lines += [x for x in r.said if not x.startswith("If this set is open")]
        return lines

    # ---------------------------------------------------------------- actions

    def on_option(self, _):
        if self.dest.GetSelection() == self.EXISTING and not self.set_path:
            wx.CallAfter(self.on_choose_set, None)
        self._enable()
        self._later()

    def on_choose_midi(self, _):
        with wx.FileDialog(self, "Choose a MIDI file or a REAPER project", defaultDir=self.cfg.Read("last_dir", ""),
                           wildcard="MIDI files and REAPER projects|" + MIDI_PATTERNS +
                                    ";*.rpp;*.RPP;*.rpp-bak;*.RPP-bak|All files|*.*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as d:
            if d.ShowModal() != wx.ID_OK:
                return
            path = d.GetPath()
        self.files = [path]
        if self.songs_choice:
            self.songs_choice.SetSelection(0)
            self.songs_choice.Disable()
        self._load_midi(path)
        self.main.SetStatusText(f"{self.list.GetCount()} markers from {os.path.basename(path)}.")
        self._land(self.list, after_file_dialog=True)
        wx.CallLater(3000, lambda: self._say(f"{self.list.GetCount()} markers from {os.path.basename(path)}."))

    def on_no_midi(self, _):
        self._load_midi(None)
        self.files = []
        if self.songs_choice:
            self.songs_choice.SetSelection(0)
            self.songs_choice.Disable()
        self._tell("No file. Type the markers, tempo and time signature.")
        self.tempo.SetFocus()

    def _fonts(self, ctrls):
        """View, Bigger Text reaches this window too: labels, fields, the list and the preview grow.
        Buttons and popups keep the system size, as in the main window (a bigger font clips them)."""
        pt = getattr(self.main, "text_pt", 0)
        if not pt or pt == getattr(self.main, "default_pt", pt):
            return
        for c in ctrls:
            f = c.GetFont()
            f.SetPointSize(pt)
            c.SetFont(f)
            c.InvalidateBestSize()
        intro = [c for c in ctrls if isinstance(c, wx.StaticText) and len(c.GetLabel()) > 120]
        for c in intro:
            c.Wrap(max(300, self.GetClientSize().width - 60))

    def _say(self, text: str):
        """News for the listener: VoiceOver on a Mac; this window's own status bar on Windows.

        The main window's status bar is behind this window while it's open, so on Windows
        the line goes in a status bar here, where NVDA (Insert-End) and JAWS (Insert-Page
        Down) read it. Refusals use _refuse, which every screen reader reads at once.
        """
        if sys.platform == "win32":
            if self.status is not None:
                self.status.SetStatusText(text)
            return
        from .gui import announce
        announce(text)

    def _tell(self, text: str):
        self.main.SetStatusText(text)
        self._say(text)

    def _refuse(self, text: str):
        """Why something wasn't done. A Windows screen reader reads a message box at once; announce() can't."""
        self.main.SetStatusText(text)
        if sys.platform == "win32":
            wx.MessageBox(text, "Make an Ableton Set", wx.OK, self)
        else:
            self._say(text)

    def on_list_key(self, e):
        k = e.GetKeyCode()
        if k == ord("Z") and e.CmdDown() and not e.ShiftDown():
            self.on_undo(None)
        elif k in (wx.WXK_DELETE, wx.WXK_BACK):
            self.on_remove(None)
        elif k in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.on_change(None)
        else:
            e.Skip()

    def _land(self, ctrl, after_file_dialog: bool = False):
        """Focus a control after a small window closes, so VoiceOver reads it, not the whole window.

        macOS puts focus back on its own as a window closes and VoiceOver then reads the
        dialog from the top; stepping away and back a moment later makes it read the row.
        """
        def go():
            if self.FindFocus() is ctrl:
                other = self.change_btn if ctrl is not self.change_btn else self.name
                other.SetFocus()
                wx.CallLater(150, ctrl.SetFocus)
            else:
                ctrl.SetFocus()
        # After the system's file dialog VoiceOver re-reads this window about 1.3 s later; landing after
        # that starts means the control is what's heard last. After our own small windows, 0.9 s is enough.
        wx.CallLater(2200 if after_file_dialog else 900, go)

    def _selected(self) -> int:
        return self.list.GetSelection()

    def _stop_editing(self):
        self.editing = None
        self.add_btn.SetLabel(keys("&Add Marker"))

    def _typed_marker(self) -> A.TypedMarker | None:
        try:
            bar = int(self.m_bar.GetValue().strip())
            if bar < 1:
                raise ValueError
        except ValueError:
            self._refuse("Write the bar as a whole number. The song's first bar is 1.")
            self.m_bar.SetFocus()
            return None
        try:
            beat = float(self.m_beat.GetValue().strip().replace(",", ".") or "1")
            if beat < 1:
                raise ValueError
        except ValueError:
            self._refuse("Write the beat as a number: 1 is the downbeat, 2.5 is halfway to beat 3.")
            self.m_beat.SetFocus()
            return None
        return A.TypedMarker(bar, beat, self.m_name.GetValue().strip())

    def _song_sigs(self):
        """The time signatures typed markers are counted in: the file's, or the one typed in."""
        if self.path:
            if getattr(self, "_sigs_for", None) != self.path:
                said: list[str] = []
                try:
                    if A.is_rpp(self.path):
                        self._sigs = A._rpp_maps(self.path, said)[1]
                    else:
                        self._sigs = A._file_maps(A._music(self.path, self._project(), said), said)[1]
                except (MidiError, OSError, ValueError):
                    return None
                self._sigs_for = self.path
            return self._sigs
        try:
            num, den = (int(x) for x in (self.signature.GetValue().strip() or "4/4").split("/"))
            return [(0.0, num, den)]
        except ValueError:
            return None

    def on_add(self, _):
        m = self._typed_marker()
        if m is None:
            return
        if not m.text:
            self._refuse("Give the marker a name first.")
            self.m_name.SetFocus()
            return
        taken = [x for x, _ in self.items if x is not self.editing and x.bar == m.bar and abs(x.beat - m.beat) < 1e-6]
        if taken:
            said = f"There's already a marker there: {marker_row(taken[0], 'typed')}. Change that one instead."
            self._refuse(said)
            self.m_bar.SetFocus()
            return
        sigs = None if self._onto() and not self.path else self._song_sigs()  # onto a song: its bars, checked on Make
        if sigs:
            try:
                A.resolve_marker(sigs, m)
            except A.SetError as e:
                msg = str(e)
                self._refuse(msg[0].upper() + msg[1:] + ".")
                self.m_beat.SetFocus()
                self.m_beat.SetSelection(-1, -1)
                return
        rows = [i for i, (x, _) in enumerate(self.items) if x is self.editing]
        changing = bool(rows)
        self._push(f"changed {marker_row(*self.items[rows[0]])}" if changing else f"added {marker_row(m, 'typed')}",
                   self.items[rows[0]][0] if changing else None)
        if changing:
            self.items[rows[0]] = (m, "typed")
        else:
            self.items.append((m, "typed"))
        self.items.sort(key=lambda x: (x[0].bar, x[0].beat))
        k = next(i for i, x in enumerate(self.items) if x[0] is m)
        self.editing = None
        self.add_btn.SetLabel(keys("&Add Marker"))
        self._fill(k)
        said = f"{'Changed to' if changing else 'Added'} {marker_row(m, 'typed')}. {len(self.items)} markers."
        self.main.SetStatusText(said)
        self.m_name.SetValue("")
        self.m_beat.SetValue("1")
        self.m_bar.SetFocus()
        self.m_bar.SetSelection(-1, -1)
        wx.CallLater(700, lambda: self._say(said))  # after VoiceOver has read the Bar field

    def on_change(self, _):
        sel = self._selected()
        if not 0 <= sel < len(self.items):
            return
        m = self.items[sel][0]
        self.editing = m
        self.m_bar.SetValue(str(m.bar))
        self.m_beat.SetValue(_beat_text(m.beat))
        self.m_name.SetValue(m.text)
        self.add_btn.SetLabel(keys("S&ave Change"))
        self.m_bar.SetFocus()
        self.m_bar.SetSelection(-1, -1)
        said = f"Changing {marker_row(*self.items[sel])}. Edit it, then press Return."
        wx.CallLater(700, lambda: self._say(said))

    def on_cancel(self, e):
        # Any change counts, a file marker removed or changed as much as one typed in
        if self._snapshot() != self.original and self.result is None:
            # Two buttons, the safe one titled exactly Cancel: macOS gives Escape only to that title
            with wx.MessageDialog(self, "Close without making the set? Your changes to the markers will be "
                                        "lost. Cancel keeps working.", "Make an Ableton Set",
                                  wx.OK | wx.CANCEL | wx.CANCEL_DEFAULT) as q:
                q.SetOKCancelLabels("Close Anyway", "Cancel")
                if q.ShowModal() != wx.ID_OK:
                    self._land(self.list if self.items else self.m_bar)
                    return
        self.EndModal(wx.ID_CANCEL)

    def on_remove(self, _):
        sel = self._selected()
        if not 0 <= sel < len(self.items):
            return
        self._push(f"removed {marker_row(*self.items[sel])}", self.items[sel][0])
        m, t = self.items.pop(sel)
        if m is self.editing:
            self._stop_editing()
        self._fill(min(sel, len(self.items) - 1) if self.items else None)
        said = f"Removed {marker_row(m, t)}. {len(self.items)} markers left."
        self.main.SetStatusText(said)
        self._say(said)
        (self.list if self.items else self.m_bar).SetFocus()

    def on_undo(self, _):
        if not self.undo:
            self._refuse("Nothing to undo.")
            return
        items, what, focus = self.undo.pop()
        self.items = items
        self._stop_editing()
        self._fill(next((i for i, (m, _) in enumerate(items) if m is focus), 0))
        said = f"Undid: {what}. {len(self.items)} markers."
        self.main.SetStatusText(said)
        self.list.SetFocus() if self.items else self.m_bar.SetFocus()
        wx.CallLater(700, lambda: self._say(said))

    def on_save_midi(self, _):
        if not self.path or A.is_rpp(self.path):
            return
        start = A.markers_copy_name(self.path)
        with wx.FileDialog(self, "Save the markers in a copy of the MIDI file", os.path.dirname(start),
                           os.path.basename(start), "MIDI files|*.mid;*.MID",
                           wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as d:
            if d.ShowModal() != wx.ID_OK:
                self._land(self.save_midi_btn, after_file_dialog=True)
                return
            out = d.GetPath()
        try:
            said = A.save_markers_midi(self.path, [m for m, _ in self.items], out, self._project())
        except (A.SetError, MidiError, OSError, ValueError) as e:
            msg = str(e)
            wx.MessageBox(msg[0].upper() + msg[1:] + ".", "Save Markers", wx.OK, self)
            self._land(self.save_midi_btn)
            return
        fixed = (" Its timing is fixed too, the way Fix would." if any("tempo map fixed" in x or "Undid REAPER" in x
                                                                      for x in said) else "")
        msg = (f"Saved {os.path.basename(out)} with {len(self.items)} "
               f"{'marker' if len(self.items) == 1 else 'markers'}.{fixed} Your file wasn't changed.")
        self.main.SetStatusText(msg)
        self._land(self.save_midi_btn, after_file_dialog=True)
        wx.CallLater(2600, lambda: self._say(msg))

    def on_several(self, _):
        before = len(self.items)
        with TextDialog(self, "\n".join(A.marker_line(m) for m, _ in self.items)) as d:
            self._fonts(d.fonts)
            d.Fit()
            if d.ShowModal() != wx.ID_OK:
                return
            new = d.result
        sigs = self._song_sigs() if not (self._onto() and not self.path) else None
        if sigs:
            for m in new:
                try:
                    A.resolve_marker(sigs, m)
                except A.SetError as e:
                    msg = str(e)
                    wx.MessageBox(msg[0].upper() + msg[1:] + ". Nothing was changed.", "Markers", wx.OK, self)
                    return
        self._push("edited the markers as text")
        # A line left as it was keeps "(from the file)"
        was = {(m.bar, round(m.beat, 6), m.text): t for m, t in self.items}
        self.items = sorted(((m, was.get((m.bar, round(m.beat, 6), m.text), "typed")) for m in new),
                            key=lambda x: (x[0].bar, x[0].beat))
        self.editing = None
        self.add_btn.SetLabel(keys("&Add Marker"))
        self._fill(0)
        diff = len(self.items) - before
        said = f"{len(self.items)} markers now" + (", the same number as before." if diff == 0 else
                                                   f", {abs(diff)} {'more' if diff > 0 else 'fewer'} than before.")
        self.main.SetStatusText(said)
        self._land(self.list if self.items else self.m_bar)
        wx.CallLater(1400, lambda: self._say(said))

    def on_lyrics(self, _):
        before = len(self.lyric_items)
        with TextDialog(self, "\n".join(A.marker_line(m) for m in self.lyric_items), lyrics=True) as d:
            self._fonts(d.fonts)
            d.Fit()
            if d.ShowModal() != wx.ID_OK:
                return
            new = [m for m in d.result if m.text.strip()]
        sigs = self._song_sigs() if not (self._onto() and not self.path) else None
        if sigs:
            for m in new:
                try:
                    A.resolve_marker(sigs, m)
                except A.SetError as e:
                    msg = str(e).replace("unnamed marker", "a line with no words")
                    wx.MessageBox(msg[0].upper() + msg[1:] + ". Nothing was changed.", "Lyrics", wx.OK, self)
                    return
        self.lyric_items = sorted(new, key=lambda m: (m.bar, m.beat))
        self.lyrics_said = [f"{len(self.lyric_items)} lyric lines, as edited."]
        if self.lyric_items:
            self.lyrics_cb.SetValue(True)
        diff = len(self.lyric_items) - before
        said = f"{len(self.lyric_items)} lyric lines now" + (
            ", the same number as before." if diff == 0 else
            f", {abs(diff)} {'more' if diff > 0 else 'fewer'} than before.")
        self.main.SetStatusText(said)
        self._later()
        self._land(self.lyrics_btn)
        wx.CallLater(1400, lambda: self._say(said))

    def on_choose_set(self, _):
        with wx.FileDialog(self, "Choose the Live set to add it to",
                           defaultDir=os.path.dirname(self.set_path) if self.set_path else "",
                           wildcard="Live sets (*.als)|*.als|All files|*.*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as d:
            if d.ShowModal() != wx.ID_OK:
                if not self.set_path:
                    self.dest.SetSelection(self.NEW)
                    self._enable()
                return
            path = d.GetPath()
        wx.BeginBusyCursor()
        try:
            self._load_set(path)
        finally:
            wx.EndBusyCursor()
        self._enable()
        self._later()
        if self.set_path:
            songs = len(self.song_locators)
            self.main.SetStatusText(f"{os.path.basename(path)}: {songs} song{'s' if songs != 1 else ''}.")
            self._land(self.where, after_file_dialog=True)

    def on_make(self, _):
        self.cfg.WriteBool("ableton_plain", self.style.GetSelection() == 1)
        self.cfg.WriteBool("ableton_quick", self.quick.GetValue())
        self.cfg.WriteBool("ableton_song_end", self.song_end.GetValue())
        self.cfg.WriteBool("ableton_into_existing", self.dest.GetSelection() == self.EXISTING)
        self.cfg.Flush()
        line_up = ("bars", "time")[self.line_up.GetSelection()]
        try:
            plans = self._plans()
        except (A.SetError, MidiError, OSError) as e:
            msg = str(e)
            wx.MessageBox(msg[0].upper() + msg[1:] + ".", "Make an Ableton Set", wx.OK, self)
            return
        existing = self.dest.GetSelection() == self.EXISTING
        which = self.songs_choice.GetSelection() if self.songs_choice else 0
        if existing and which == 2:
            wx.MessageBox("One set each makes new sets, so it doesn't go with a set you already have. Choose "
                          "back to back, or put them in a new set.", "Make an Ableton Set", wx.OK, self)
            return
        self.template = None
        if not existing:
            kept = self.cfg.Read("ableton_template", "")
            if kept and os.path.exists(kept):
                self.template = kept
            elif not A.live_templates():
                # No Live found where it usually lives (another drive, a portable copy): any set will do
                wx.MessageBox("Live's own empty set wasn't found on this computer, so choose a Live set to start "
                              "from. An empty one is best. It's remembered for next time, and never changed.",
                              "Make an Ableton Set", wx.OK, self)
                with wx.FileDialog(self, "Choose a Live set to start new sets from",
                                   wildcard="Live sets (*.als)|*.als|All files|*.*",
                                   style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as d:
                    if d.ShowModal() != wx.ID_OK:
                        return
                    self.template = d.GetPath()
                self.cfg.Write("ableton_template", self.template)
                self.cfg.Flush()
        if not existing and which != 2:
            first = plans[0]
            default = (A.default_new_set_path(first) if first.path else
                       os.path.join(os.path.expanduser("~/Music"), f"{A.safe_name(first.name)} Project",
                                    f"{A.safe_name(first.name)}.als"))
            if which == 1:
                default = os.path.join(os.path.dirname(os.path.abspath(self.files[0])), "Set Project", "Set.als")
            # The dialog opens where the project folder will go (it may not exist yet; a missing folder
            # sent the dialog somewhere else entirely), and the project folder is made around the set.
            proj = os.path.dirname(default)
            start = proj if os.path.isdir(proj) else os.path.dirname(proj)
            with wx.FileDialog(self, "Save the new Live set (it goes in its own project folder)", defaultDir=start,
                               defaultFile=os.path.basename(default), wildcard="Live sets (*.als)|*.als",
                               style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as d:
                if d.ShowModal() != wx.ID_OK:
                    return
                self.out_path = d.GetPath()
                if not self.out_path.lower().endswith(".als"):
                    self.out_path += ".als"
            # Live keeps a set in its own project folder, and adds files beside it when it saves. Saved
            # anywhere else (next to the MIDI file, among stems), it goes in "<name> Project" there.
            folder, file = os.path.split(self.out_path)
            if not folder.endswith(" Project") and not os.path.isdir(os.path.join(folder, "Ableton Project Info")):
                self.out_path = os.path.join(folder, f"{os.path.splitext(file)[0]} Project", file)
            if os.path.exists(self.out_path):
                wx.MessageBox(f"{os.path.basename(self.out_path)} already exists, and an existing set is never "
                              "saved over. Choose another name.", "Make an Ableton Set", wx.OK, self)
                return
        wx.BeginBusyCursor()
        self.make.Disable()
        self._tell("Making the set...")

        def work():
            try:
                r = self._run(False, line_up)
                wx.CallAfter(self._done, r if isinstance(r, list) else [r], None)
            except Exception as e:  # never leave the window stuck
                wx.CallAfter(self._done, None, e)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, results, err):
        wx.EndBusyCursor()
        self.make.Enable()
        if err is not None:
            msg = str(err) or err.__class__.__name__
            wx.MessageBox("The set wasn't saved: " + msg[0].lower() + msg[1:] + ("" if msg.endswith(".") else "."),
                          "Make an Ableton Set", wx.OK, self)
            return
        self.result = results
        self.EndModal(wx.ID_OK)
