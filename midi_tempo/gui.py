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
import webbrowser

from .core import MidiError, analyze, compare_with, describe, fix_file, midi_files, read_midi, summary, verdict

APP_NAME = "MIDI Tempo Fix"
WILDCARD = "MIDI files (*.mid;*.midi;*.smf)|*.mid;*.midi;*.smf;*.MID;*.MIDI|All files|*.*"
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
        super().__init__(None, title=APP_NAME, size=(760, 560))
        self.files: list[str] = []
        self.fixable: list[str] = []
        self.saved: list[str] = []

        panel = wx.Panel(self)
        intro = wx.StaticText(panel, label=INTRO)
        intro.Wrap(700)

        self.choose = wx.Button(panel, label="&Choose MIDI files...")
        self.choose.SetToolTip("Pick one or more MIDI files to check. You can also drop files or folders on this window.")
        self.choose_dir = wx.Button(panel, label="Choose a f&older...")
        self.choose_dir.SetToolTip("Checks every MIDI file in a folder, including folders inside it.")
        self.notes = wx.CheckBox(panel, label="Also check and fix &notes: duplicates, overlaps, stuck notes")
        self.notes.SetValue(True)
        self.notes.SetToolTip("When off, only the tempo map is checked and fixed, and notes are left exactly as they are.")

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
            "Compares timing with another MIDI file of the same music, from any app, or with a REAPER project. "
            "Shows whether an app changed the tempo or moved any notes."
        )
        self.fix.Disable()
        self.compare.Disable()
        self.show_saved.Disable()

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.fix, 0, wx.RIGHT, 8)
        buttons.Add(self.show_saved, 0, wx.RIGHT, 8)
        buttons.Add(self.compare, 0)

        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(intro, 0, wx.ALL, 12)
        pick = wx.BoxSizer(wx.HORIZONTAL)
        pick.Add(self.choose, 0, wx.RIGHT, 8)
        pick.Add(self.choose_dir, 0)
        s.Add(pick, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        s.Add(self.notes, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        s.Add(report_label, 0, wx.LEFT | wx.RIGHT, 12)
        s.Add(self.report, 1, wx.EXPAND | wx.ALL, 12)
        s.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        panel.SetSizer(s)
        self.CreateStatusBar()
        self.busy = False

        self._menus()
        self.choose_dir.Bind(wx.EVT_BUTTON, self.on_choose_dir)
        self.choose.Bind(wx.EVT_BUTTON, self.on_choose)
        self.fix.Bind(wx.EVT_BUTTON, self.on_fix)
        self.compare.Bind(wx.EVT_BUTTON, self.on_compare)
        self.show_saved.Bind(wx.EVT_BUTTON, lambda e: reveal(self.saved))
        self.notes.Bind(wx.EVT_CHECKBOX, lambda e: self.files and self.check(self.files, focus_report=False))
        self.files, self.fixable, self.saved = [], [], []
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
        self.report.SetValue("\n".join(lines).rstrip())
        self.report.SetInsertionPoint(0)

        def land():
            if self.FindFocus() is self.report:
                self.choose.SetFocus()
                wx.CallLater(150, self.report.SetFocus)
            else:
                self.report.SetFocus()

        # Wait a moment: right after a file dialog closes, macOS hands focus back on its
        # own, and VoiceOver then announced only "Report scroll area" without the text.
        wx.CallLater(250, land)

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
        if len(files) > BRIEF_OVER:
            self.show([f"Checking {len(files)} MIDI files. Large folders take a while; progress is in the title bar."])

        def work():
            results, last = [], time.time()
            for i, p in enumerate(files, 1):
                name = label(p)
                try:
                    r = analyze(read_midi(p), name)
                    results.append((p, r, None))
                except (MidiError, OSError) as e:
                    results.append((p, None, f"{name}: could not check it, {e}."))
                if time.time() - last > 1:
                    last = time.time()
                    wx.CallAfter(self.SetTitle, f"{APP_NAME}: checking {i} of {len(files)}")
                    if i % 500 < 50:
                        wx.CallAfter(announce, f"Checked {i} of {len(files)}")
            wx.CallAfter(self._checked, files, results, missing, notes, focus_report)

        threading.Thread(target=work, daemon=True).start()

    def _checked(self, files, results, missing, notes, focus_report):
        self._set_busy(False)
        self.SetTitle(APP_NAME + (" (update available)" if self.update_info else ""))
        self.files = files
        self.fixable = [p for p, r, err in results if r and (r.tempo_needs_fix or (notes and r.notes.fixable))]
        unreadable = sum(1 for _, r, _ in results if r is None)
        brief = len(files) > BRIEF_OVER
        lines = [f"{os.path.basename(p)}: not found." for p in missing]
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
            lines += ["", "Clean files are not listed. Choose a single file to hear everything about it."]
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
        """While checking or fixing, every control is off; afterwards the caller decides what comes back."""
        self.busy = busy
        if busy:
            for c in (self.choose, self.choose_dir, self.notes, self.fix, self.compare, self.show_saved):
                c.Disable()
        else:
            for c in (self.choose, self.choose_dir, self.notes):
                c.Enable()

    def on_choose_dir(self, _):
        with wx.DirDialog(self, "Choose a folder of MIDI files to check. Folders inside it are checked too.") as dlg:
            if dlg.ShowModal() == wx.ID_OK:
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
            self, "Choose MIDI files to check", wildcard=WILDCARD,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.check(dlg.GetPaths())

    def on_fix(self, _):
        if self.busy:
            return
        if not self.fixable:
            self.show(["Nothing to fix. Choose MIDI files first."])
            return
        todo, notes = list(self.fixable), self.notes.GetValue()
        self._set_busy(True)
        if len(todo) > BRIEF_OVER:
            self.show([f"Fixing {len(todo)} files. Progress is in the title bar."])

        def work():
            done, last = [], time.time()
            for i, p in enumerate(todo, 1):
                try:
                    out, said = fix_file(p, notes=notes)
                    done.append((p, out, said, None))
                except PermissionError as e:
                    done.append((p, None, None, e))
                except (MidiError, OSError) as e:
                    done.append((p, None, [f"{os.path.basename(p)}: not fixed, {e}."], None))
                if time.time() - last > 1:
                    last = time.time()
                    wx.CallAfter(self.SetTitle, f"{APP_NAME}: fixing {i} of {len(todo)}")
            wx.CallAfter(self._fixed, done, notes)

        threading.Thread(target=work, daemon=True).start()

    def _fixed(self, done, notes):
        self._set_busy(False)
        self.SetTitle(APP_NAME + (" (update available)" if self.update_info else ""))
        brief = len(done) > BRIEF_OVER
        lines, self.saved, failed = [], [], 0
        for p, out, said, perm in done:
            if perm is not None:  # can't write next to the original: ask where, here on the main thread
                out, said = self._fix_elsewhere(p)
            if out:
                self.saved.append(out)
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
        self.fixable = []
        self.fix.Disable()
        self.show_saved.Enable(bool(self.saved))
        lines.append("Done. Your original files are unchanged. Show cleaned files opens their folder.")
        self.show(lines)

    def _fix_elsewhere(self, p: str):
        name = os.path.basename(p)
        with wx.DirDialog(self, f"Can't save next to {name}. Choose a folder for the cleaned copy.") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return None, [f"{name}: not fixed, no folder chosen."]
            stem, ext = os.path.splitext(name)
            out = os.path.join(dlg.GetPath(), f"{stem} (tempo fixed){ext or '.mid'}")
        try:
            return fix_file(p, out, notes=self.notes.GetValue())
        except (MidiError, OSError) as e:
            return None, [f"{name}: not fixed, {e}."]

    def on_compare(self, _):
        if not self.files:
            self.show([
                "Choose MIDI files first, then compare them with another file.",
                "For example, export from one app, import into another, export again, and compare the "
                "two exports to see whether anything moved.",
            ])
            return
        with wx.FileDialog(
            self, "Choose the file to compare with: a MIDI file from any app, or a REAPER project",
            wildcard="MIDI files and REAPER projects|*.mid;*.midi;*.smf;*.rmi;*.MID;*.MIDI;*.rpp;*.RPP|All files|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            other = dlg.GetPath()
        lines = []
        for p in self.files:
            name = os.path.basename(p)
            try:
                lines += [f"{name} compared with {os.path.basename(other)}:"] + compare_with(read_midi(p), other) + [""]
            except (MidiError, OSError) as e:
                lines += [f"{name}: could not compare, {e}.", ""]
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
        print(f"{APP_NAME} {__version__} OK, wxPython {wx.version()}")
        return
    App(False).MainLoop()


if __name__ == "__main__":
    main()
