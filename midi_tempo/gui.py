"""MIDI Tempo Fix: the same checks and fixes as the command, in a window.

Built on wxPython because it draws the platform's own controls, so VoiceOver,
NVDA, JAWS and Narrator read every button, field and menu natively.

Layout, in tab order: what this does, Choose MIDI files, the report, Fix and
save cleaned copies, Compare with a REAPER project. After any action, focus
goes to the report so a screen reader reads the result straight away.
"""
from __future__ import annotations

import os
import sys

import wx

from . import __version__
from .core import MidiError, analyze, compare_project, describe, fix_file, read_midi

APP_NAME = "MIDI Tempo Fix"
WILDCARD = "MIDI files (*.mid;*.midi;*.smf)|*.mid;*.midi;*.smf;*.MID;*.MIDI|All files|*.*"
INTRO = (
    "Checks the tempo map in MIDI files so every app plays them the same. "
    "Choose files to check. Nothing changes until you press Fix, "
    "and your original files are never changed."
)


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

        panel = wx.Panel(self)
        intro = wx.StaticText(panel, label=INTRO)
        intro.Wrap(700)

        self.choose = wx.Button(panel, label="&Choose MIDI files...")
        self.choose.SetToolTip("Pick one or more MIDI files to check. You can also drop files on this window.")

        report_label = wx.StaticText(panel, label="&Report")
        self.report = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2, name="Report"
        )
        self.report.SetValue("No files checked yet.")

        self.fix = wx.Button(panel, label="&Fix and save cleaned copies")
        self.fix.SetToolTip("Saves a copy of each file that needs it, next to the original, named '(tempo fixed)'.")
        self.compare = wx.Button(panel, label="Compare with a REAPER &project...")
        self.compare.SetToolTip("Checks the chosen files' tempo against a REAPER project's tempo map.")
        self.fix.Disable()
        self.compare.Disable()

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.fix, 0, wx.RIGHT, 8)
        buttons.Add(self.compare, 0)

        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(intro, 0, wx.ALL, 12)
        s.Add(self.choose, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        s.Add(report_label, 0, wx.LEFT | wx.RIGHT, 12)
        s.Add(self.report, 1, wx.EXPAND | wx.ALL, 12)
        s.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        panel.SetSizer(s)

        self._menus()
        self.choose.Bind(wx.EVT_BUTTON, self.on_choose)
        self.fix.Bind(wx.EVT_BUTTON, self.on_fix)
        self.compare.Bind(wx.EVT_BUTTON, self.on_compare)
        self.SetDropTarget(FileDrop(self))
        self.report.SetDropTarget(FileDrop(self))
        self.choose.SetFocus()

    def _menus(self):
        bar = wx.MenuBar()
        f = wx.Menu()
        m_open = f.Append(wx.ID_OPEN, "&Choose MIDI files...\tCtrl+O")
        m_fix = f.Append(wx.ID_SAVE, "&Fix and save cleaned copies\tCtrl+S")
        m_cmp = f.Append(wx.ID_ANY, "Compare with a REAPER &project...\tCtrl+R")
        f.AppendSeparator()
        m_quit = f.Append(wx.ID_EXIT, "&Quit\tCtrl+Q")
        h = wx.Menu()
        m_about = h.Append(wx.ID_ABOUT, f"&About {APP_NAME}")
        bar.Append(f, "&File")
        bar.Append(h, "&Help")
        self.SetMenuBar(bar)
        self.Bind(wx.EVT_MENU, self.on_choose, m_open)
        self.Bind(wx.EVT_MENU, self.on_fix, m_fix)
        self.Bind(wx.EVT_MENU, self.on_compare, m_cmp)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), m_quit)
        self.Bind(wx.EVT_MENU, self.on_about, m_about)

    # -------------------------------------------------------- actions

    def show(self, lines: list[str]):
        self.report.SetValue("\n".join(lines).rstrip())
        self.report.SetInsertionPoint(0)
        self.report.SetFocus()

    def check(self, paths: list[str]):
        self.files = [p for p in paths if os.path.isfile(p)]
        self.fixable = []
        lines = [f"{os.path.basename(p)}: not found." for p in paths if not os.path.isfile(p)]
        for p in self.files:
            name = os.path.basename(p)
            try:
                r = analyze(read_midi(p), name)
                lines += describe(r)
                if r.needs_fix:
                    self.fixable.append(p)
            except MidiError as e:
                lines.append(f"{name}: could not check it, {e}.")
            lines.append("")
        if self.fixable:
            lines.append(
                f"{len(self.fixable)} of {len(self.files)} file{'s' if len(self.files) != 1 else ''} "
                f"need fixing. Press Fix and save cleaned copies."
            )
        elif self.files:
            lines.append("Nothing needs fixing.")
        else:
            lines.append("No files to check.")
        self.fix.Enable(bool(self.fixable))
        self.compare.Enable(bool(self.files))
        self.show(lines)

    def on_choose(self, _):
        with wx.FileDialog(
            self, "Choose MIDI files to check", wildcard=WILDCARD,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.check(dlg.GetPaths())

    def on_fix(self, _):
        if not self.fixable:
            self.show(["Nothing to fix. Choose MIDI files first."])
            return
        lines = []
        for p in self.fixable:
            try:
                _, said = fix_file(p)
            except PermissionError:
                said = self._fix_elsewhere(p)
            except MidiError as e:
                said = [f"{os.path.basename(p)}: not fixed, {e}."]
            lines += said + [""]
        self.fixable = []
        self.fix.Disable()
        lines.append("Done. Your original files are unchanged.")
        self.show(lines)

    def _fix_elsewhere(self, p: str) -> list[str]:
        name = os.path.basename(p)
        with wx.DirDialog(self, f"Can't save next to {name}. Choose a folder for the cleaned copy.") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return [f"{name}: not fixed, no folder chosen."]
            stem, ext = os.path.splitext(name)
            out = os.path.join(dlg.GetPath(), f"{stem} (tempo fixed){ext or '.mid'}")
        try:
            return fix_file(p, out)[1]
        except (MidiError, OSError) as e:
            return [f"{name}: not fixed, {e}."]

    def on_compare(self, _):
        if not self.files:
            self.show(["Choose MIDI files first, then compare them with a REAPER project."])
            return
        with wx.FileDialog(
            self, "Choose the REAPER project to compare with",
            wildcard="REAPER projects (*.rpp)|*.rpp;*.RPP|All files|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            rpp = dlg.GetPath()
        lines = []
        for p in self.files:
            name = os.path.basename(p)
            try:
                lines += [f"{name}:"] + compare_project(read_midi(p), rpp) + [""]
            except (MidiError, OSError) as e:
                lines += [f"{name}: could not compare, {e}.", ""]
        self.show(lines)

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
    if "--self-test" in sys.argv:  # used by the build checks: load everything, show nothing
        app = wx.App(False)
        MainFrame().Destroy()
        print(f"{APP_NAME} {__version__} OK, wxPython {wx.version()}")
        return
    App(False).MainLoop()


if __name__ == "__main__":
    main()
