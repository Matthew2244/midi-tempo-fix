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
import threading

import wx

from . import ableton as A
from .core import MidiError


def _beat_text(beat: float) -> str:
    return f"{round(beat, 3):g}"


def marker_row(m: A.TypedMarker, source: str) -> str:
    """'Bar 17, beat 3: Pickup (from the file)'. Where first: a listener hears the place, then the name."""
    where = f"Bar {m.bar}" + (f", beat {_beat_text(m.beat)}" if m.beat != 1 else "")
    return f"{where}: {m.text or 'no name'}" + (" (from the file)" if source == "file" else "")


class TextDialog(wx.Dialog):
    """Every marker as plain text, one per line: read it, edit it, paste a list in, or copy it out."""

    def __init__(self, parent, text: str):
        super().__init__(parent, title="Edit Markers as Text", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        p = wx.Panel(self)
        intro = wx.StaticText(p, label="One marker per line: the bar, then a dot and the beat if it isn't the "
                                       "downbeat, then the name. Like 17 Verse, or 33.3 Pickup for bar 33, beat 3. "
                                       "What's here replaces the list. Copy it out to keep it or share it.")
        intro.Wrap(440)
        label = wx.StaticText(p, label="&Markers")
        self.text = wx.TextCtrl(p, value=text, style=wx.TE_MULTILINE, size=(460, 260), name="Markers")
        self.text.SetHint("17 Verse\n33.3 Pickup\n49 Chorus +LOOP\n97 End")
        from .gui import mac_accessibility_label
        mac_accessibility_label(self.text, "Markers")
        ok = wx.Button(p, wx.ID_OK, "Use These Markers")
        cancel = wx.Button(p, wx.ID_CANCEL, "Cancel")
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

        intro = wx.StaticText(p, label="Makes an Ableton Live set with a locator at every marker, named the way "
                                       "AbleSet reads them, and the song's tempo and time signatures so Live's bars "
                                       "match. Markers come from the MIDI file, typed in, or both. Nothing is saved "
                                       "until Make Set, and your files are never changed.")
        intro.Wrap(520)
        s.Add(intro, 0, wx.ALL, 12)

        self.source = add("&Song from", wx.TextCtrl(p, style=wx.TE_READONLY, name="Song from"))
        src_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.choose_midi = wx.Button(p, label="Choose MIDI File...")
        self.choose_midi.SetToolTip("The MIDI file whose markers, tempo and time signatures make the song.")
        self.no_midi = wx.Button(p, label="No MIDI File")
        self.no_midi.SetToolTip("Make the song from markers you type in, at a tempo and time signature you give.")
        src_buttons.Add(self.choose_midi, 0, wx.RIGHT, 8)
        src_buttons.Add(self.no_midi)
        s.Add(src_buttons, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        self.songs_choice = None
        if len(self.files) > 1:
            self.songs_choice = add("S&ongs", wx.Choice(p, choices=[
                "Just this one", f"All {len(self.files)} files, back to back in one set",
                f"All {len(self.files)} files, one set each"], name="Songs"))
            self.songs_choice.SetSelection(0)
            self.songs_choice.SetToolTip("Typed markers go with one song at a time, so they're for Just this one.")

        self.tempo = add("&Tempo, with no MIDI file", wx.TextCtrl(p, value="120", name="Tempo"))
        self.tempo.SetToolTip("Beats per minute, 20 to 999.")
        self.signature = add("Time si&gnature, with no MIDI file", wx.TextCtrl(p, value="4/4", name="Time signature"))
        self.signature.SetToolTip("Like 4/4 or 6/8.")

        self.name = add("Song &name", wx.TextCtrl(p, name="Song name"))
        self.name.SetToolTip("The song locator's name: what AbleSet shows in the setlist.")

        self.list = add("&Markers", wx.ListBox(p, style=wx.LB_SINGLE, size=(-1, 170), name="Markers"))
        mac_accessibility_label(self.list, "Markers")
        self.list.SetToolTip("Every marker, by bar and beat. Add as many as you like.")
        mk = wx.BoxSizer(wx.HORIZONTAL)
        self.change_btn = wx.Button(p, label="&Change")
        self.change_btn.SetToolTip("Puts the selected marker in the fields below to change it. Return in the list "
                                   "does the same.")
        self.remove_btn = wx.Button(p, label="&Remove")
        self.remove_btn.SetToolTip("Remove the selected marker. Delete in the list does the same.")
        self.several_btn = wx.Button(p, label="Edit as &Text...")
        self.several_btn.SetToolTip("Every marker as plain text, one per line, like 17 Verse: edit them all at "
                                    "once, paste a list in, or copy them out.")
        for b in (self.change_btn, self.remove_btn, self.several_btn):
            mk.Add(b, 0, wx.RIGHT, 8)
        s.Add(mk, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        # A new marker is typed right here, no window of its own: closing a small window made
        # VoiceOver read this whole window again and lose the new row. Return adds it and comes
        # back to Bar, so a long list goes in quickly.
        self.m_bar = add("New marker: &bar", wx.TextCtrl(p, value="1", style=wx.TE_PROCESS_ENTER, name="Bar"))
        self.m_bar.SetToolTip("The bar, counting the first as 1. Command-N comes here.")
        self.m_beat = add("B&eat", wx.TextCtrl(p, value="1", style=wx.TE_PROCESS_ENTER, name="Beat"))
        self.m_beat.SetToolTip("1 is the downbeat. Halfway to beat 3 is 2.5.")
        self.m_name = add("Marker na&me", wx.TextCtrl(p, style=wx.TE_PROCESS_ENTER, name="Marker name"))
        self.m_name.SetToolTip("Like Verse 2. End marks where the song stops. Return adds it.")
        self.add_btn = wx.Button(p, label="&Add Marker")
        self.add_btn.SetToolTip("Adds the marker typed above. Return in any of the three fields does the same.")
        s.Add(self.add_btn, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
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

        self.style = add("Locator &names", wx.Choice(p, choices=[
            "For AbleSet: the song's name, > Verse, SONG END", "For Live on its own: just the marker names"],
            name="Locator names"))
        self.style.SetSelection(1 if self.cfg.ReadBool("ableton_plain", False) else 0)
        self.quick = wx.CheckBox(p, label="Make every section a &quick-jump section (>>)")
        self.quick.SetValue(self.cfg.ReadBool("ableton_quick", False))
        s.Add(self.quick, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.song_end = wx.CheckBox(p, label="Add SONG &END where the song ends")
        self.song_end.SetValue(self.cfg.ReadBool("ableton_song_end", True))
        self.song_end.SetToolTip("At an End marker if there is one, otherwise at the end of the bar where the music "
                                 "stops.")
        s.Add(self.song_end, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self.preview = add("What the set will &hold", wx.TextCtrl(
            p, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 200), name="What the set will hold"))
        mac_accessibility_label(self.preview, "What the set will hold")
        p.SetSizer(s)

        self.make = wx.Button(self, wx.ID_OK, "&Make Set")
        self.make.SetDefault()
        cancel = wx.Button(self, wx.ID_CANCEL, "Cancel")
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(p, 1, wx.EXPAND)
        b = wx.BoxSizer(wx.HORIZONTAL)
        b.Add(cancel, 0, wx.RIGHT, 8)
        b.Add(self.make)
        outer.Add(b, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
        self.SetSizer(outer)
        for c in p.GetChildren():  # the scroll bar is for the mouse; Tab should go control to control
            if isinstance(c, wx.ScrollBar):
                c.DisableFocusFromKeyboard()
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
        self.several_btn.Bind(wx.EVT_BUTTON, self.on_several)
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
        self._loading = True
        self.items = [(m, t) for m, t in self.items if t == "typed"]
        if getattr(self, "editing", None) is not None and not any(m is self.editing for m, _ in self.items):
            self._stop_editing()
        if path:
            try:
                file_items, _ = A.file_markers_typed(path)
            except (MidiError, OSError) as e:
                wx.MessageBox(f"{os.path.basename(path)} couldn't be read: {e}.", "Make an Ableton Set", wx.OK, self)
                file_items = []
                self.path = None
            self.items = [(m, "file") for m in file_items] + self.items
            self.items.sort(key=lambda x: (x[0].bar, x[0].beat))
            self.source.SetValue(os.path.basename(path) if self.path else "No MIDI file: markers typed in only")
            if self.path:
                self.name.ChangeValue(A.song_name_for(path))
        else:
            self.source.SetValue("No MIDI file: markers typed in only")
        self._loading = False
        self._fill()

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
        if which and self.files:
            return [A.plan_song(f, **opts) for f in self.files]
        name = self.name.GetValue().strip() or None
        if self.path:
            return [A.plan_song(self.path, name, typed=typed, file_markers=False, **opts)]
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
        return [A.plan_song(None, name, typed=typed, tempo=tempo, signature=(num, den), **opts)]

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
        for c in (self.add_btn, self.several_btn, self.list, self.m_bar, self.m_beat, self.m_name):
            c.Enable(not several)
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
                lines += A.describe_plan(p) + [""]
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
        lines += [x for x in r.said if not x.startswith("If this set is open")]
        return lines

    # ---------------------------------------------------------------- actions

    def on_option(self, _):
        if self.dest.GetSelection() == self.EXISTING and not self.set_path:
            wx.CallAfter(self.on_choose_set, None)
        self._enable()
        self._later()

    def on_choose_midi(self, _):
        with wx.FileDialog(self, "Choose a MIDI file", defaultDir=self.cfg.Read("last_dir", ""),
                           wildcard="MIDI files (*.mid;*.midi;*.smf)|*.mid;*.midi;*.smf;*.MID;*.MIDI|All files|*.*",
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
        self._land(self.list)
        wx.CallLater(1400, lambda: self._say(f"{self.list.GetCount()} markers from {os.path.basename(path)}."))

    def on_no_midi(self, _):
        self._load_midi(None)
        self.files = []
        if self.songs_choice:
            self.songs_choice.SetSelection(0)
            self.songs_choice.Disable()
        self.main.tell("No MIDI file. Type the markers, tempo and time signature.")
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
        from .gui import announce
        announce(text)

    def on_list_key(self, e):
        k = e.GetKeyCode()
        if k in (wx.WXK_DELETE, wx.WXK_BACK):
            self.on_remove(None)
        elif k in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.on_change(None)
        else:
            e.Skip()

    def _land(self, ctrl):
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
        wx.CallLater(900, go)

    def _selected(self) -> int:
        return self.list.GetSelection()

    def _stop_editing(self):
        self.editing = None
        self.add_btn.SetLabel("&Add Marker")

    def _typed_marker(self) -> A.TypedMarker | None:
        try:
            bar = int(self.m_bar.GetValue().strip())
            if bar < 1:
                raise ValueError
        except ValueError:
            self._say("Write the bar as a whole number. The song's first bar is 1.")
            self.main.SetStatusText("Write the bar as a whole number.")
            self.m_bar.SetFocus()
            return None
        try:
            beat = float(self.m_beat.GetValue().strip().replace(",", ".") or "1")
            if beat < 1:
                raise ValueError
        except ValueError:
            self._say("Write the beat as a number: 1 is the downbeat, 2.5 is halfway to beat 3.")
            self.main.SetStatusText("Write the beat as a number.")
            self.m_beat.SetFocus()
            return None
        return A.TypedMarker(bar, beat, self.m_name.GetValue().strip())

    def _song_sigs(self):
        """The time signatures typed markers are counted in: the file's, or the one typed in."""
        if self.path:
            if getattr(self, "_sigs_for", None) != self.path:
                said: list[str] = []
                self._sigs = A._file_maps(A._music(self.path, None, said), said)[1]
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
        sigs = None if self._onto() and not self.path else self._song_sigs()  # onto a song: its bars, checked on Make
        if sigs:
            try:
                A.resolve_marker(sigs, m)
            except A.SetError as e:
                msg = str(e)
                self._say(msg[0].upper() + msg[1:] + ".")
                self.main.SetStatusText(msg[0].upper() + msg[1:] + ".")
                self.m_beat.SetFocus()
                self.m_beat.SetSelection(-1, -1)
                return
        rows = [i for i, (x, _) in enumerate(self.items) if x is self.editing]
        changing = bool(rows)
        if changing:
            self.items[rows[0]] = (m, "typed")
        else:
            self.items.append((m, "typed"))
        self.items.sort(key=lambda x: (x[0].bar, x[0].beat))
        k = next(i for i, x in enumerate(self.items) if x[0] is m)
        self.editing = None
        self.add_btn.SetLabel("&Add Marker")
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
        self.add_btn.SetLabel("S&ave Change")
        self.m_bar.SetFocus()
        self.m_bar.SetSelection(-1, -1)
        said = f"Changing {marker_row(*self.items[sel])}. Edit it, then press Return."
        wx.CallLater(700, lambda: self._say(said))

    def on_cancel(self, e):
        typed = sum(1 for _, t in self.items if t == "typed")
        if typed and self.result is None:
            word = "marker" if typed == 1 else "markers"
            with wx.MessageDialog(self, f"Close without making the set? The {typed} {word} you typed will be lost.",
                                  "Make an Ableton Set", wx.YES_NO | wx.CANCEL | wx.NO_DEFAULT) as q:
                q.SetYesNoLabels("Close", "Keep Working")
                if q.ShowModal() != wx.ID_YES:
                    self._land(self.list if self.items else self.m_bar)
                    return
        self.EndModal(wx.ID_CANCEL)

    def on_remove(self, _):
        sel = self._selected()
        if not 0 <= sel < len(self.items):
            return
        m, t = self.items.pop(sel)
        if m is self.editing:
            self._stop_editing()
        self._fill(min(sel, len(self.items) - 1) if self.items else None)
        self.main.tell(f"Removed {marker_row(m, t)}. {len(self.items)} markers left.")
        (self.list if self.items else self.m_bar).SetFocus()

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
        self.items = sorted(((m, "typed") for m in new), key=lambda x: (x[0].bar, x[0].beat))
        self.editing = None
        self.add_btn.SetLabel("&Add Marker")
        self._fill(0)
        said = f"{len(self.items)} markers now, {len(self.items) - before:+d}."
        self.main.SetStatusText(said)
        self._land(self.list if self.items else self.m_bar)
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
            self._land(self.where)

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
            with wx.FileDialog(self, "Save the new Live set", defaultDir=os.path.dirname(default),
                               defaultFile=os.path.basename(default), wildcard="Live sets (*.als)|*.als",
                               style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as d:
                if d.ShowModal() != wx.ID_OK:
                    return
                self.out_path = d.GetPath()
                if not self.out_path.lower().endswith(".als"):
                    self.out_path += ".als"
            if os.path.exists(self.out_path):
                wx.MessageBox(f"{os.path.basename(self.out_path)} already exists, and an existing set is never "
                              "saved over. Choose another name.", "Make an Ableton Set", wx.OK, self)
                return
        wx.BeginBusyCursor()
        self.make.Disable()
        self.main.tell("Making the set...")

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
