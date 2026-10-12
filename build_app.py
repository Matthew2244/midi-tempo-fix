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
SIGN_ID = os.environ.get("MIDI_TEMPO_FIX_SIGN_ID")  # set by tools/release_mac.py; empty means ad-hoc
if sys.platform == "darwin":
    args += ["--osx-bundle-identifier", "io.github.matthew2244.miditempofix"]
    if SIGN_ID:
        args += ["--codesign-identity", SIGN_ID]
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
    if SIGN_ID:
        # PyInstaller already signed every binary inside; seal the bundle with the
        # hardened runtime and a secure timestamp, both required for notarization.
        subprocess.run(["codesign", "--force", "--options", "runtime", "--timestamp",
                        "--sign", SIGN_ID, f"{NAME}.app"], check=True)
    else:
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", f"{NAME}.app"], check=True)
    out = "MIDI-Tempo-Fix-mac.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", f"{NAME}.app", out], check=True)
    exe = f"{NAME}.app/Contents/MacOS/{NAME}"
else:
    out = "MIDI-Tempo-Fix-windows"
    shutil.make_archive(out, "zip", ".", NAME)
    out += ".zip"
    exe = os.path.join(NAME, NAME + ".exe")
# Read the self-test through a file, with a time limit: a broken build can leave a helper process
# holding an output pipe open, and reading a pipe would then wait forever.
import tempfile
with tempfile.TemporaryFile("w+") as out:
    try:
        subprocess.run([exe, "--self-test"], stdout=out, stderr=subprocess.STDOUT, timeout=120)
    except subprocess.TimeoutExpired:
        print("Self-test: TIMED OUT")
        sys.exit(1)
    out.seek(0)
    result = out.read().strip().splitlines()[-1:] or ["(no output)"]
print("Self-test:", result[0])
if "OK" not in result[0]:
    sys.exit("The built app failed its self-test.")
print("Built", os.path.abspath(out))
