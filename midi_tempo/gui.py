"""MIDI Tempo Fix: the same checks and fixes as the command, in a window.

Built on wxPython because it draws the platform's own controls, so VoiceOver,
NVDA, JAWS and Narrator read every button, field and menu natively.

Layout, in tab order: what this does, Choose MIDI files, the report, Fix and
save cleaned copies, Show cleaned files, Compare with another file. After any action, focus
goes to the report so a screen reader reads the result straight away.
"""
from __future__ import annotations

import os
import subprocess
import sys

import wx

import threading
import time

from . import __version__, update
from .batch import check_many, fix_many
import webbrowser

from .core import (PROJECT_PROBLEM, MidiError, analyze, compare_with, describe, fix_file, midi_files,
                   needs_project_fix, problems, read_midi, summary, verdict)

APP_NAME = "MIDI Tempo Fix"
WILDCARD = "MIDI files (*.mid;*.midi;*.smf)|*.mid;*.midi;*.smf;*.MID;*.MIDI|All files|*.*"
AUDIO_TYPES = (".wav", ".wave", ".aif", ".aiff", ".aifc", ".mp3", ".m4a", ".aac", ".flac", ".caf", ".ogg", ".opus")
COMPARE_WILDCARD = (
    "Recordings, MIDI files and REAPER projects|"
    + ";".join(f"*{e};*{e.upper()}" for e in AUDIO_TYPES + (".mid", ".midi", ".smf", ".rmi", ".rpp"))
    + "|All files|*.*"
)
INTRO = (
    "Checks MIDI files from any app for the tempo and note mistakes that make apps play them "
    "differently. Nothing changes until you press Fix, and your originals are never changed."
)
DAY = 24 * 3600
BRIEF_OVER = 20  # more files than this: list only what needs fixing, so the report stays listenable
HELP_URL = "https://github.com/Matthew2244/midi-tempo-fix#readme"


def mac_accessibility_label(ctrl: wx.Window, label: str) -> None:
    """Give a control a spoken name on macOS, where wx has no API for it.

    Without this VoiceOver announces the report as just "text". Windows screen
    readers already take the name from the static label placed before it.
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def send(obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
            f = objc.objc_msgSend
            f.restype = restype
            f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
            return f(obj, objc.sel_registerName(sel.encode()), *args)

        text = send(objc.objc_getClass(b"NSString"), "stringWithUTF8String:",
                    label.encode(), argtypes=(ctypes.c_char_p,))
        view = ctrl.GetHandle()
        targets = [view]
        if send(view, "respondsToSelector:", objc.sel_registerName(b"documentView"),
                restype=ctypes.c_bool, argtypes=(ctypes.c_void_p,)):
            doc = send(view, "documentView")
            if doc:
                targets.append(doc)
        for t in targets:
            send(t, "setAccessibilityLabel:", text, restype=None, argtypes=(ctypes.c_void_p,))
    except Exception:
        pass  # a missing label must never stop the app


def announce(text: str) -> None:
    """Have the screen reader say something without moving focus (macOS VoiceOver).

    Used for short confirmations like "Report copied". Elsewhere the same text
    also goes to the status bar, where Windows screen readers can read it.
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        appkit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def send(obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
            f = objc.objc_msgSend
            f.restype = restype
            f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
            return f(obj, objc.sel_registerName(sel.encode()), *args)

        def nsstr(t):
            return send(objc.objc_getClass(b"NSString"), "stringWithUTF8String:", t.encode(),
                        argtypes=(ctypes.c_char_p,))

        info = send(objc.objc_getClass(b"NSMutableDictionary"), "dictionary")
        send(info, "setObject:forKey:", nsstr(text), nsstr("AXAnnouncementKey"),
             restype=None, argtypes=(ctypes.c_void_p, ctypes.c_void_p))
        high = send(objc.objc_getClass(b"NSNumber"), "numberWithInt:", 90, argtypes=(ctypes.c_int,))
        send(info, "setObject:forKey:", high, nsstr("AXPriorityKey"),
             restype=None, argtypes=(ctypes.c_void_p, ctypes.c_void_p))
        app = send(objc.objc_getClass(b"NSApplication"), "sharedApplication")
        post = appkit.NSAccessibilityPostNotificationWithUserInfo
        post.restype = None
        post.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        post(app, nsstr("AXAnnouncementRequested"), info)
    except Exception:
        pass


def reveal(paths: list[str]) -> None:
    """Show files in Finder or File Explorer, selected.

    Copies spread over many folders would open a window per folder, so past a
    few folders only the first copy is shown.
    """
    if not paths:
        return
    if len({os.path.dirname(p) for p in paths}) > 3:
        paths = paths[:1]
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", *paths])
    elif sys.platform == "win32":
        subprocess.run(["explorer", "/select,", os.path.normpath(paths[0])])
    else:
        subprocess.run(["xdg-open", os.path.dirname(paths[0])])


class FileDrop(wx.FileDropTarget):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame

    def OnDropFiles(self, x, y, filenames):
        self.frame.check(filenames)
        return True


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=APP_NAME, size=(820, 760))
        self.files: list[str] = []
        self.fixable: list[str] = []
        self.saved: list[str] = []

        panel = self.panel = wx.Panel(self)
        intro = self.intro = wx.StaticText(panel, label=INTRO)
        intro.Wrap(700)

        self.choose = wx.Button(panel, label="&Choose MIDI files...")
        self.choose.SetToolTip("Pick one or more MIDI files to check. You can also drop files or folders on this window.")
        self.choose_dir = wx.Button(panel, label="Choose a f&older...")
        self.choose_dir.SetToolTip("Checks every MIDI file in a folder, including folders inside it.")
        self.notes = wx.CheckBox(panel, label="Also check and fix &notes: duplicates, overlaps, stuck notes")
        self.cfg = wx.Config(APP_NAME)
        self.notes.SetValue(self.cfg.ReadBool("check_notes", True))  # remembered between runs
        self.notes.SetToolTip("When off, only the tempo map is checked and fixed, and notes are left exactly as they are.")

        files_label = wx.StaticText(panel, label="F&iles")
        self.list = wx.ListBox(panel, style=wx.LB_SINGLE, name="Files")
        self.list.SetToolTip("Each file and its verdict, files that need fixing first. "
                             "Select one to read everything about it in the report.")
        mac_accessibility_label(self.list, "Files")
        self.entries: list[tuple] = []  # (path, report or None, error or None), in list order

        report_label = wx.StaticText(panel, label="&Report")
        self.report = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2, name="Report"
        )
        self.report.SetValue("No files checked yet.")
        mac_accessibility_label(self.report, "Report")

        self.fix = wx.Button(panel, label="&Fix and save cleaned copies")
        self.fix.SetToolTip("Saves a copy of each file that needs it, next to the original, named '(tempo fixed)'.")
        self.show_saved = wx.Button(panel, label="S&how cleaned files")
        self.show_saved.SetToolTip("Opens the folder with the cleaned copies selected.")
        self.compare = wx.Button(panel, label="Com&pare with another file...")
        self.compare.SetToolTip(
            "Compares timing with another MIDI file of the same music from any app, a REAPER project, or a "
            "recording (WAV, AIFF, MP3 and more). Shows whether anything moved, where, and why."
        )
        self.fix.Disable()
        self.compare.Disable()
        self.show_saved.Disable()

        self.stop_btn = wx.Button(panel, label="S&top")
        self.stop_btn.SetToolTip("Stops checking or fixing. Escape does the same.")
        self.stop_btn.Disable()
        self.gauge = wx.Gauge(panel, range=100, name="Progress")
        self.gauge.SetToolTip("How far through the files it is.")
        self.gauge.Hide()
        self.stopping = False

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.fix, 0, wx.RIGHT, 8)
        buttons.Add(self.show_saved, 0, wx.RIGHT, 8)
        buttons.Add(self.compare, 0, wx.RIGHT, 8)
        buttons.Add(self.stop_btn, 0, wx.RIGHT, 8)
        buttons.Add(self.gauge, 1, wx.ALIGN_CENTER_VERTICAL)

        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(intro, 0, wx.ALL, 12)
        pick = wx.BoxSizer(wx.HORIZONTAL)
        pick.Add(self.choose, 0, wx.RIGHT, 8)
        pick.Add(self.choose_dir, 0)
        s.Add(pick, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        s.Add(self.notes, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        s.Add(files_label, 0, wx.LEFT | wx.RIGHT, 12)
        s.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 12)
        s.Add(report_label, 0, wx.LEFT | wx.RIGHT, 12)
        s.Add(self.report, 2, wx.EXPAND | wx.ALL, 12)
        s.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        panel.SetSizer(s)
        self.CreateStatusBar()
        self.busy = False

        self._menus()
        self.choose_dir.Bind(wx.EVT_BUTTON, self.on_choose_dir)
        self.list.Bind(wx.EVT_LISTBOX, self.on_select_file)
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self._texts = [intro, files_label, report_label]
        self.text_pt = self.cfg.ReadInt("text_size", 0) or self.report.GetFont().GetPointSize()
        self.default_pt = self.report.GetFont().GetPointSize()
        self._apply_text_size()
        self.panel.Bind(wx.EVT_SIZE, self._rewrap)
        self.choose.Bind(wx.EVT_BUTTON, self.on_choose)
        self.fix.Bind(wx.EVT_BUTTON, self.on_fix)
        self.compare.Bind(wx.EVT_BUTTON, self.on_compare)
        self.show_saved.Bind(wx.EVT_BUTTON, lambda e: reveal(self.saved))
        self.notes.Bind(wx.EVT_CHECKBOX, self.on_notes_toggled)
        self.files, self.fixable, self.saved = [], [], []
        self.project_for: dict[str, str] = {}  # file -> REAPER project whose padded short bars Fix undoes
        self.update_info = None
        wx.CallLater(1500, self.quiet_update_check)
        self.SetDropTarget(FileDrop(self))
        self.report.SetDropTarget(FileDrop(self))
        self.choose.SetFocus()

    def _menus(self):
        bar = wx.MenuBar()
        f = wx.Menu()
        m_open = f.Append(wx.ID_OPEN, "&Choose MIDI files...\tCtrl+O")
        m_dir = f.Append(wx.ID_ANY, "Choose a f&older...\tCtrl+Shift+O")
        m_fix = f.Append(wx.ID_SAVE, "&Fix and save cleaned copies\tCtrl+S")
        m_show = f.Append(wx.ID_ANY, "S&how cleaned files\tCtrl+Shift+S")
        m_cmp = f.Append(wx.ID_ANY, "Com&pare with another file...\tCtrl+R")
        f.AppendSeparator()
        m_save_rep = f.Append(wx.ID_ANY, "Save &report as text...")
        f.AppendSeparator()
        m_quit = f.Append(wx.ID_EXIT, "&Quit\tCtrl+Q")
        ed = wx.Menu()
        m_copy = ed.Append(wx.ID_ANY, "Copy &report\tCtrl+Shift+C")
        h = wx.Menu()
        m_help = h.Append(wx.ID_HELP, f"{APP_NAME} &Help")
        m_upd = h.Append(wx.ID_ANY, "Check for &Updates...")
        m_about = h.Append(wx.ID_ABOUT, f"&About {APP_NAME}")
        self.Bind(wx.EVT_MENU, self.on_check_updates, m_upd)
        bar.Append(f, "&File")
        bar.Append(ed, "&Edit")
        v = wx.Menu()
        m_big = v.Append(wx.ID_ANY, "&Bigger Text\tCtrl+=")
        m_small = v.Append(wx.ID_ANY, "&Smaller Text\tCtrl+-")
        m_actual = v.Append(wx.ID_ANY, "&Actual Size\tCtrl+0")
        bar.Append(v, "&View")
        self.Bind(wx.EVT_MENU, lambda e: self._text_size(+2), m_big)
        self.Bind(wx.EVT_MENU, lambda e: self._text_size(-2), m_small)
        self.Bind(wx.EVT_MENU, lambda e: self._text_size(0), m_actual)
        bar.Append(h, "&Help")
        self.Bind(wx.EVT_MENU, self.on_choose_dir, m_dir)
        self.Bind(wx.EVT_MENU, self.on_copy_report, m_copy)
        self.Bind(wx.EVT_MENU, self.on_save_report, m_save_rep)
        self.Bind(wx.EVT_MENU, lambda e: webbrowser.open(HELP_URL), m_help)
        self.SetMenuBar(bar)
        self.Bind(wx.EVT_MENU, self.on_choose, m_open)
        self.Bind(wx.EVT_MENU, self.on_fix, m_fix)
        self.Bind(wx.EVT_MENU, self.on_compare, m_cmp)
        self.Bind(wx.EVT_MENU, lambda e: reveal(self.saved), m_show)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), m_quit)
        self.Bind(wx.EVT_MENU, self.on_about, m_about)

    # -------------------------------------------------------- actions

    def show(self, lines: list[str]):
        """Put the result in the report and move focus there so it is read aloud.

        A screen reader only reads a field when focus arrives, so if the report
        already has focus, step away for a moment and come back.
        """
        self.set_report(lines)

        def land():
            if self.FindFocus() is self.report:
                self.choose.SetFocus()
                wx.CallLater(150, self.report.SetFocus)
            else:
                self.report.SetFocus()

        # Wait a moment: right after a file dialog closes, macOS hands focus back on its
        # own, and VoiceOver then announced only "Report scroll area" without the text.
        wx.CallLater(250, land)

    def set_report(self, lines: list[str]):
        """Fill the report without moving focus. Verdict and summary lines are bold, so the
        answer stands out by weight as well as by words; no meaning is carried by colour."""
        text = "\n".join(lines).rstrip()
        self.report.SetValue(text)
        # Reset to plain first: on macOS, replaced text inherits the style of what was there,
        # so a bold first line from the previous report turned the whole new report bold.
        plain = wx.Font(self.report.GetFont())
        plain.SetPointSize(self.text_pt)
        plain.SetWeight(wx.FONTWEIGHT_NORMAL)
        self.report.SetStyle(0, len(text), wx.TextAttr(wx.NullColour, wx.NullColour, plain))
        bold = wx.Font(plain)
        bold.SetWeight(wx.FONTWEIGHT_BOLD)
        pos, prev = 0, ""
        for line in text.split("\n"):
            # Only the first line of each section is bold: the summary, or a file's verdict.
            # A run of verdicts (a big batch) stays plain after the first, or nothing stands out.
            heading = line and not line.startswith("  ") and (pos == 0 or prev == "")
            if heading:
                self.report.SetStyle(pos, pos + len(line), wx.TextAttr(wx.NullColour, wx.NullColour, bold))
            pos += len(line) + 1
            prev = line
        self.report.SetInsertionPoint(0)

    # -------------------------------------------------------- text size (low vision)

    def _apply_text_size(self):
        # The list, report, intro, labels and checkbox all grow; buttons keep the system size,
        # because a bigger font inside a fixed-height Mac button gets clipped.
        for c in [self.list, self.report, self.notes] + self._texts:
            f = c.GetFont()
            f.SetPointSize(self.text_pt)
            c.SetFont(f)
        for c in [self.notes] + self._texts:  # let labels and the checkbox grow with their text
            c.InvalidateBestSize()
            c.SetMinSize(c.GetBestSize())
        self._rewrap()
        if self.report.GetValue():
            self.set_report(self.report.GetValue().split("\n"))
        self.Layout()

    def _rewrap(self, event=None):
        """Wrap the intro to the window's width, from the original text each time, so a bigger
        font or a narrower window never pushes it past the right edge."""
        if event:
            event.Skip()
        width = max(300, self.panel.GetClientSize().width - 30)
        # Wrap by measuring each word in the label's current font. wx's own Wrap() on macOS kept
        # measuring the old font after a size change, so large text ran off the right edge.
        dc = wx.ClientDC(self.intro)
        dc.SetFont(self.intro.GetFont())
        lines, line = [], ""
        for word in INTRO.split():
            trial = f"{line} {word}".strip()
            if line and dc.GetTextExtent(trial)[0] > width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
        text = "\n".join(lines)
        self.intro.SetLabel(text)
        w, h = dc.GetMultiLineTextExtent(text)[:2]
        self.intro.SetMinSize((w, h + 4))
        self.panel.Layout()

    def _text_size(self, step: int):
        self.text_pt = self.default_pt if step == 0 else max(10, min(48, self.text_pt + step))
        self.cfg.WriteInt("text_size", self.text_pt)
        self.cfg.Flush()
        self._apply_text_size()
        self.tell(f"Text size {self.text_pt} points.")

    # -------------------------------------------------------- the files list

    def _fill_list(self, keep_selection: bool = False):
        sel = self.list.GetSelection()
        self.list.Set([self._entry_text(e) for e in self.entries])
        if keep_selection and 0 <= sel < len(self.entries):
            self.list.SetSelection(sel)

    def _entry_text(self, entry) -> str:
        path, r, err = entry[:3]
        fixed = entry[3] if len(entry) > 3 else None
        if r is None:
            return err
        if fixed:
            return f"Fixed: {r.name}, saved as \"{os.path.basename(fixed)}\"."
        found = problems(r, self.notes.GetValue()) + list(self._extra(path))
        # Status first: long names get cut off at the end of a list row, and a listener
        # should hear the answer before the name.
        return f"Needs fixing: {r.name}, {', '.join(found)}." if found else f"Clean: {r.name}"

    def _extra(self, path) -> tuple:
        """Problems found by comparing with a REAPER project, which the file alone can't show."""
        return (PROJECT_PROBLEM,) if path in self.project_for else ()

    def on_select_file(self, _):
        i = self.list.GetSelection()
        if not (0 <= i < len(self.entries)):
            return
        path, r, err = self.entries[i][:3]
        if r is None:
            self.set_report([err])
            return
        lines = describe(r, self.notes.GetValue(), self._extra(path))
        fixed = self.entries[i][3] if len(self.entries[i]) > 3 else None
        if fixed:
            lines.append(f"A cleaned copy is saved next to it as \"{os.path.basename(fixed)}\".")
        self.set_report(lines)

    def _finished_signal(self, started: float):
        """After a long batch: a sound and a Dock bounce, in case attention went elsewhere."""
        if time.time() - started < 5:
            return
        self.RequestUserAttention()
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
            elif sys.platform == "win32":
                import winsound
                winsound.MessageBeep()
        except Exception:
            pass

    def tell(self, text: str):
        """A short confirmation: spoken by VoiceOver, shown in the status bar."""
        self.SetStatusText(text)
        announce(text)

    def check(self, paths: list[str], focus_report: bool = True):
        if self.busy:
            return
        missing = [p for p in paths if not os.path.exists(p)]
        files = midi_files([p for p in paths if os.path.exists(p)], recursive=True)
        if not files:
            self.files, self.fixable = [], []
            self.fix.Disable()
            self.compare.Disable()
            self.show([f"{os.path.basename(p)}: not found." for p in missing] + ["No MIDI files to check there."])
            return
        notes = self.notes.GetValue()
        folders = [p for p in paths if os.path.isdir(p)]

        def label(p):  # inside a chosen folder, show the subfolder too: names repeat across folders
            for root in folders:
                if p.startswith(root.rstrip(os.sep) + os.sep):
                    return os.path.relpath(p, root)
            return os.path.basename(p)

        self._set_busy(True)
        self.entries = []  # last run's rows would describe files that are no longer what's shown
        self.list.Clear()
        if len(files) > BRIEF_OVER:
            self.show([f"Checking {len(files)} MIDI files. Progress shows in the bar at the bottom and the "
                       f"title bar. Escape stops."])

        started = time.time()

        def work():
            progress = self._progress_reporter("checking", "Checked")
            try:
                results, stopped = check_many([(p, label(p)) for p in files], progress=progress,
                                              stop=lambda: self.stopping)
            except Exception as e:  # never leave the window stuck busy
                wx.CallAfter(self._failed, f"Checking stopped because of an unexpected error: {e}")
                return
            done = files[:len(results)]
            wx.CallAfter(self._checked, done, results, missing, notes, focus_report, started, stopped, len(files))

        threading.Thread(target=work, daemon=True).start()

    def _checked(self, files, results, missing, notes, focus_report, started=None, stopped=False, asked=0):
        self._set_busy(False)
        self.SetTitle(APP_NAME + (" (update available)" if self.update_info else ""))
        self.files = files
        self.project_for = {}  # a new check starts fresh; comparing again with the project brings it back
        self.fixable = [p for p, r, err in results if r and (r.tempo_needs_fix or (notes and r.notes.fixable))]
        unreadable = sum(1 for _, r, _ in results if r is None)
        brief = len(files) > BRIEF_OVER
        lines = [f"{os.path.basename(p)}: not found." for p in missing]
        if stopped:
            lines += [f"Stopped after {len(files)} of {asked} files. These are the results so far.", ""]
        if len(files) > 1:
            lines += [summary(len(files), len(self.fixable), unreadable), ""]
        for p, r, err in results:
            if r is None:
                lines += [err] + ([] if brief else [""])
            elif brief:
                if p in self.fixable:
                    lines.append(verdict(r, notes))
            else:
                lines += describe(r, notes) + [""]
        if brief:
            lines += ["", "Every file, clean ones too, is in the Files list. Select one to read everything about it."]
        # Files list: needing a fix first, then unreadable, then clean; each keeps its folder order
        fix_set = set(self.fixable)

        def order(entry):
            path, r, _ = entry
            return 0 if path in fix_set else (1 if r is None else 2)

        self.entries = sorted(results, key=order)  # sorted() is stable: folder order kept within each group
        self._fill_list()
        if started:
            self._finished_signal(started)
        if self.fixable:
            lines.append("Press Fix and save cleaned copies to fix " +
                         ("it." if len(self.fixable) == 1 else f"all {len(self.fixable)}."))
        self.fix.Enable(bool(self.fixable))
        self.saved = []
        self.show_saved.Disable()
        self.compare.Enable(bool(files))
        if focus_report:
            self.show(lines)
        else:
            self.report.SetValue("\n".join(lines).rstrip())

    def _set_busy(self, busy: bool):
        """While checking or fixing, every control but Stop is off; afterwards the caller decides
        what comes back. The progress bar shows only while there is progress to show."""
        self.busy = busy
        self.stopping = False
        if busy:
            self.SetStatusText("")
            for c in (self.choose, self.choose_dir, self.notes, self.fix, self.compare, self.show_saved):
                c.Disable()
            self.stop_btn.Enable()
            self.gauge.SetValue(0)
            self.gauge.Show()
        else:
            for c in (self.choose, self.choose_dir, self.notes):
                c.Enable()
            self.stop_btn.Disable()
            self.gauge.Hide()
        self.panel.Layout()

    def _progress_reporter(self, verb: str, past: str):
        """Called from the worker as files finish: updates the bar and title a few times a second,
        and has VoiceOver say the count every few seconds on a long batch."""
        state = {"last": 0.0, "spoken": time.time()}

        def progress(i, total):
            now = time.time()
            if now - state["last"] < 0.25 and i < total:
                return
            state["last"] = now
            wx.CallAfter(self._show_progress, verb, i, total)
            if now - state["spoken"] > 5 and i < total:
                state["spoken"] = now
                wx.CallAfter(announce, f"{past} {i} of {total}")

        return progress

    def _show_progress(self, verb, i, total):
        if not self.busy:
            return
        self.gauge.SetValue(int(100 * i / max(1, total)))
        self.SetTitle(f"{APP_NAME}: {verb} {i} of {total}")

    def _failed(self, message: str):
        self._set_busy(False)
        self.SetTitle(APP_NAME)
        self.show([message, "Nothing was changed by the part that failed. Your original files are untouched."])

    def on_stop(self, _=None):
        if self.busy and not self.stopping:
            self.stopping = True
            self.stop_btn.Disable()
            self.tell("Stopping.")

    def _on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE and self.busy:
            self.on_stop()
            return
        event.Skip()

    def on_notes_toggled(self, _):
        self.cfg.WriteBool("check_notes", self.notes.GetValue())
        self.cfg.Flush()
        if self.files:
            self.check(self.files, focus_report=False)

    def _last_dir(self) -> str:
        d = self.cfg.Read("last_dir", "")
        return d if d and os.path.isdir(d) else ""

    def _remember_dir(self, path: str):
        self.cfg.Write("last_dir", path if os.path.isdir(path) else os.path.dirname(path))
        self.cfg.Flush()

    def on_choose_dir(self, _):
        with wx.DirDialog(self, "Choose a folder of MIDI files to check. Folders inside it are checked too.",
                          defaultPath=self._last_dir()) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self._remember_dir(dlg.GetPath())
                self.check([dlg.GetPath()])

    def on_copy_report(self, _):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self.report.GetValue()))
            wx.TheClipboard.Close()
            self.tell("Report copied.")
        else:
            self.tell("Could not reach the clipboard.")

    def on_save_report(self, _):
        with wx.FileDialog(self, "Save the report as a text file", defaultFile="MIDI Tempo Fix report.txt",
                           wildcard="Text files (*.txt)|*.txt", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.report.GetValue() + "\n")
            self.tell(f"Report saved as {os.path.basename(path)}.")
        except OSError as e:
            self.tell(f"Could not save the report: {e.strerror}.")

    def on_choose(self, _):
        with wx.FileDialog(
            self, "Choose MIDI files to check", wildcard=WILDCARD, defaultDir=self._last_dir(),
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self._remember_dir(dlg.GetPaths()[0])
                self.check(dlg.GetPaths())

    def on_fix(self, _):
        if self.busy:
            return
        if not self.fixable:
            self.show(["Nothing to fix. Choose MIDI files first."])
            return
        todo, notes = list(self.fixable), self.notes.GetValue()
        started = time.time()
        self._set_busy(True)
        if len(todo) > BRIEF_OVER:
            self.show([f"Fixing {len(todo)} files. Progress shows in the bar at the bottom and the title bar. "
                       f"Escape stops."])

        def work():
            progress = self._progress_reporter("fixing", "Fixed")
            try:
                done, stopped = fix_many([(p, notes, self.project_for.get(p)) for p in todo], progress=progress,
                                         stop=lambda: self.stopping)
            except Exception as e:  # never leave the window stuck busy
                wx.CallAfter(self._failed, f"Fixing stopped because of an unexpected error: {e}")
                return
            wx.CallAfter(self._fixed, done, notes, started, stopped, len(todo))

        threading.Thread(target=work, daemon=True).start()

    def _fixed(self, done, notes, started=None, stopped=False, asked=0):
        self._set_busy(False)
        self.SetTitle(APP_NAME + (" (update available)" if self.update_info else ""))
        brief = len(done) > BRIEF_OVER
        lines, self.saved, failed, copy_of = [], [], 0, {}
        for p, out, said, perm in done:
            if perm is not None:  # can't write next to the original: ask where, here on the main thread
                out, said = self._fix_elsewhere(p)
            if out:
                self.saved.append(out)
                copy_of[p] = out
                if brief:
                    continue
                where = "next to the original" if os.path.dirname(out) == os.path.dirname(p) else \
                    f"in {os.path.basename(os.path.dirname(out)) or os.path.dirname(out)}"
                said = [line for line in said if not line.startswith("Saved: ")]
                said.append(f"Saved {where} as \"{os.path.basename(out)}\".")
                lines += said + [""]
            else:
                failed += 1
                lines += said + ([] if brief else [""])
        if brief:
            head = f"Fixed {len(self.saved)} of {len(done)} files. Each cleaned copy is saved next to its original."
            lines = [head] + (["", f"{failed} could not be fixed:"] + lines if failed else [])
            lines.append("")
        handled = {d[0] for d in done}
        self.fixable = [p for p in self.fixable if p not in handled]  # after a Stop, the rest stay fixable
        self.fix.Enable(bool(self.fixable))
        self.show_saved.Enable(bool(self.saved))
        if stopped:
            lines.insert(0, f"Stopped after {len(done)} of {asked} files. The rest were not fixed; "
                            f"press Fix and save cleaned copies again to do them.")
        lines.append("Done. Your original files are unchanged. Show cleaned files opens their folder.")
        self.entries = [(e[0], e[1], e[2], copy_of[e[0]]) if e[0] in copy_of else e for e in self.entries]
        self._fill_list(keep_selection=True)
        if started:
            self._finished_signal(started)
        self.show(lines)

    def _fix_elsewhere(self, p: str):
        name = os.path.basename(p)
        with wx.DirDialog(self, f"Can't save next to {name}. Choose a folder for the cleaned copy.") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return None, [f"{name}: not fixed, no folder chosen."]
            stem, ext = os.path.splitext(name)
            out = os.path.join(dlg.GetPath(), f"{stem} (tempo fixed){ext or '.mid'}")
        try:
            return fix_file(p, out, notes=self.notes.GetValue(), project=self.project_for.get(p))
        except (MidiError, OSError) as e:
            return None, [f"{name}: not fixed, {e}."]

    def on_compare(self, _):
        if self.busy:
            return
        if not self.files:
            self.show([
                "Choose MIDI files first, then compare them with another file.",
                "For example: a recording of the same music, to check the MIDI lines up with it; or export from "
                "one app, import into another, export again, and compare the two exports.",
            ])
            return
        with wx.FileDialog(
            self, "Choose what to compare with: a recording, a MIDI file from any app, or a REAPER project",
            wildcard=COMPARE_WILDCARD, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            other = dlg.GetPath()
        files = list(self.files)
        is_audio = other.lower().endswith(AUDIO_TYPES)
        started = time.time()
        self._set_busy(True)
        if is_audio:  # a few seconds a file: say so, so the wait is not silence
            text = (f"Listening to {os.path.basename(other)} and lining up "
                    f"{'the MIDI file' if len(files) == 1 else f'{len(files)} MIDI files'}. Escape stops.")
            self.SetStatusText(text)
            # Said through the report, not an announcement: when the file dialog closes, VoiceOver lands
            # on the report and reads it, talking over (or dropping) any announcement. Heard both ways
            # on 1.7.1 and 1.7.2; the old result was read instead.
            self.set_report([text])

        def work():
            done = []
            for i, p in enumerate(files):
                if self.stopping:
                    break
                name = os.path.basename(p)
                try:
                    lines = [f"{name} compared with {os.path.basename(other)}:"] + compare_with(read_midi(p), other)
                    padded = other.lower().endswith(".rpp") and needs_project_fix(p, other)
                except (MidiError, OSError, ValueError) as e:
                    lines, padded = [f"{name}: could not compare, {e}."], False
                except Exception as e:  # never leave the window stuck busy
                    lines, padded = [f"{name}: could not compare, unexpected error: {e}."], False
                done.append((p, lines, padded))
                if len(files) > 1:
                    wx.CallAfter(self._show_progress, "comparing", i + 1, len(files))
            wx.CallAfter(self._compared, other, done, len(files), started)

        threading.Thread(target=work, daemon=True).start()

    def _compared(self, other, done, asked, started):
        self._set_busy(False)
        self.SetStatusText("")  # the "Listening to ..." line would read as still working
        self.SetTitle(APP_NAME + (" (update available)" if self.update_info else ""))
        lines, newly = [], 0
        if len(done) < asked:
            lines += [f"Stopped after {len(done)} of {asked} files.", ""]
        for p, said, padded in done:
            lines += said + [""]
            if padded:
                self.project_for[p] = other
                if p not in self.fixable:
                    self.fixable.append(p)
                newly += 1
        if newly:
            lines.append("Press Fix and save cleaned copies to move " + ("it" if newly == 1 else "them") +
                         " back to where the project plays.")
        self.fix.Enable(bool(self.fixable))
        self.compare.Enable(bool(self.files))
        self.show_saved.Enable(bool(self.saved))
        if newly:  # the Files list said "Clean" for these; now it knows better
            fix_set = set(self.fixable)
            self.entries.sort(key=lambda e: 0 if e[0] in fix_set else (1 if e[1] is None else 2))
            self._fill_list(keep_selection=True)
        self._finished_signal(started)
        self.show(lines)

    # -------------------------------------------------------- updates

    def quiet_update_check(self):
        """Once a day, ask GitHub if there is a newer version; say so only if there is."""
        cfg = wx.Config(APP_NAME)
        if time.time() - cfg.ReadFloat("last_update_check", 0) < DAY:
            return
        cfg.WriteFloat("last_update_check", time.time())

        def work():
            info = update.latest()
            if update.newer(info):
                wx.CallAfter(self._announce_update, info)

        threading.Thread(target=work, daemon=True).start()

    def _announce_update(self, info):
        self.update_info = info
        line = f"Version {info['version']} is available. Choose Help, Check for Updates to install it."
        self.SetTitle(f"{APP_NAME} (update available)")
        if self.report.GetValue() == "No files checked yet.":
            self.report.SetValue(f"No files checked yet.\n\n{line}")
        else:
            self.report.AppendText(f"\n\n{line}")

    def on_check_updates(self, _):
        self.show([f"Checking for a newer version. You have {update.current_version()}."])
        wx.Yield()
        info = update.latest()
        if info is None:
            self.show(["Could not reach GitHub to check for updates. Check the internet connection and try again."])
            return
        if not update.newer(info):
            self.show([f"You have the latest version, {update.current_version()}."])
            return
        if not update.installed_app():
            self.show([f"Version {info['version']} is available. This copy runs from Python, so update it with:",
                       update.how_to_update_source()])
            return
        dlg = wx.MessageDialog(
            self,
            f"Version {info['version']} is available. You have {update.current_version()}.\n\n"
            "Install it now? The app downloads it, checks it, then closes and opens the new version.",
            "Update available", wx.OK | wx.CANCEL | wx.OK_DEFAULT | wx.ICON_INFORMATION,
        )
        # The second button must be titled exactly "Cancel": macOS gives Escape
        # only to a button with that title, and a dialog you cannot leave from
        # the keyboard is a trap. ("Not now" looked friendlier and broke Escape.)
        dlg.SetOKCancelLabels("&Install", "Cancel")
        go = dlg.ShowModal() == wx.ID_OK
        dlg.Destroy()
        if not go:
            self.show([f"Not installed. Version {info['version']} stays available in Help, Check for Updates."])
            return
        self.show([f"Downloading version {info['version']}. This takes a few seconds."])
        wx.Yield()
        try:
            said = update.install(info)
        except update.UpdateError as e:
            self.show([f"The update did not install: {e}."])
            return
        self.show([said, "Closing now."])
        wx.CallLater(1500, self.Close)

    def on_about(self, _):
        wx.MessageBox(
            f"{APP_NAME} {__version__}\n\nChecks and fixes MIDI tempo maps so every app plays them the same.\n"
            "Free and open source, MIT license.",
            f"About {APP_NAME}", wx.OK | wx.ICON_INFORMATION, self,
        )


class App(wx.App):
    def OnInit(self):
        self.SetAppName(APP_NAME)
        self.frame = MainFrame()
        self.frame.Show()
        files = [a for a in sys.argv[1:] if not a.startswith("-")]
        if files:
            self.frame.check(files)
        return True

    def MacOpenFiles(self, filenames):  # files dropped on the Dock icon or opened with the app
        self.frame.check(list(filenames))


def main():
    if any(a.startswith("--multiprocessing-fork") for a in sys.argv) or "-c" in sys.argv[1:2]:
        # Started as a worker process without freeze_support having caught it: never open a
        # window. Exiting makes the batch fall back to one core instead of piling up copies of
        # the app (which is what happened when freeze_support was deliberately removed).
        sys.exit(1)
    if "--update-now" in sys.argv:  # used to test the update path end to end without the window
        info = update.latest()
        if not update.newer(info):
            print("No newer version.")
            return
        try:
            print(update.install(info, relaunch="--no-relaunch" not in sys.argv))
        except update.UpdateError as e:
            print(f"Not installed: {e}.")
            sys.exit(1)
        return
    if "--self-test" in sys.argv:  # used by the build checks: load everything, show nothing
        app = wx.App(False)
        MainFrame().Destroy()
        # Prove worker processes start inside the packaged app (needs freeze_support in app.py).
        import tempfile
        from .batch import check_many, last_run
        with tempfile.TemporaryDirectory() as d:
            jobs = []
            for i in range(12):
                path = os.path.join(d, f"t{i}.mid")
                with open(path, "wb") as f:
                    f.write(b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk\x00\x00\x00\x04\x00\xff\x2f\x00")
                jobs.append((path, f"t{i}.mid"))
            box = {}
            t = threading.Thread(target=lambda: box.update(res=check_many(jobs, workers=2)[0]), daemon=True)
            t.start()
            t.join(30)  # a broken setup must fail the test, not hang it
            res = box.get("res", [])
        ok = len(res) == 12 and all(r[1] is not None for r in res) and last_run["parallel"]
        try:  # the audio check needs numpy inside the app; a build that lost it must fail here
            from . import audio
            audio._np()
            audio_ok = True
        except Exception:
            audio_ok = False
        status = "OK" if ok and audio_ok else ("PARALLEL FAILED" if not ok else "AUDIO CHECK MISSING")
        ok = ok and audio_ok
        print(f"{APP_NAME} {__version__} {status}, wxPython {wx.version()}")
        if not ok:
            sys.exit(1)
        return
    App(False).MainLoop()


if __name__ == "__main__":
    main()
