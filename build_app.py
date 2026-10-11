"""Build the app for the platform this runs on: python build_app.py

Mac:     dist/MIDI Tempo Fix.app, zipped to dist/MIDI-Tempo-Fix-mac.zip
Windows: dist/MIDI Tempo Fix/ (folder with the .exe), zipped to dist/MIDI-Tempo-Fix-windows.zip
"""
import os
import shutil
import subprocess
import sys

from midi_tempo import __version__

NAME = "MIDI Tempo Fix"
args = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
    "--name", NAME, "--collect-data", "certifi", "app.py",
]
if sys.platform == "darwin":
    args += ["--osx-bundle-identifier", "io.github.matthew2244.miditempofix"]
subprocess.run(args, check=True)

os.chdir("dist")
if sys.platform == "darwin":
    plist = f"{NAME}.app/Contents/Info.plist"
    subprocess.run(["plutil", "-replace", "CFBundleShortVersionString", "-string", __version__, plist], check=True)
    subprocess.run(["plutil", "-replace", "CFBundleVersion", "-string", __version__, plist], check=True)
    # MIDI files can be dropped on the app or opened with it
    subprocess.run(["plutil", "-insert", "CFBundleDocumentTypes", "-json",
                    '[{"CFBundleTypeName":"MIDI file","CFBundleTypeRole":"Viewer",'
                    '"LSItemContentTypes":["public.midi-audio"],"LSHandlerRank":"Alternate"}]', plist], check=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", f"{NAME}.app"], check=True)
    out = "MIDI-Tempo-Fix-mac.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", f"{NAME}.app", out], check=True)
    exe = f"{NAME}.app/Contents/MacOS/{NAME}"
else:
    out = "MIDI-Tempo-Fix-windows"
    shutil.make_archive(out, "zip", ".", NAME)
    out += ".zip"
    exe = os.path.join(NAME, NAME + ".exe")
print("Self-test:", subprocess.run([exe, "--self-test"], capture_output=True, text=True).stdout.strip())
print("Built", os.path.abspath(out))
