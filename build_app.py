"""Build the app for the platform this runs on: python build_app.py

Mac:     dist/MIDI Tempo Fix.app, zipped to dist/MIDI-Tempo-Fix-mac.zip, with Sparkle inside
Windows: dist/MIDI Tempo Fix/ (folder with the .exe), zipped to dist/MIDI-Tempo-Fix-windows.zip,
         and dist/MIDI-Tempo-Fix-Setup.exe when Inno Setup is installed (WinSparkle updates it)
"""
import os
import shutil
import subprocess
import sys

from midi_tempo import __version__

NAME = "MIDI Tempo Fix"
args = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
    "--name", NAME, "--collect-data", "certifi", "--hidden-import", "midi_tempo.audio",
    "--hidden-import", "midi_tempo.ableton_gui", "app.py",
]
if sys.platform == "win32":
    from build_app_support import winsparkle_dll
    args += ["--add-binary", winsparkle_dll() + os.pathsep + "."]  # updates for installed copies
SIGN_ID = os.environ.get("MIDI_TEMPO_FIX_SIGN_ID")  # set by tools/release_mac.py; empty means ad-hoc
if sys.platform == "darwin":
    args += ["--osx-bundle-identifier", "io.github.matthew2244.miditempofix"]
    if SIGN_ID:
        args += ["--codesign-identity", SIGN_ID]
subprocess.run(args, check=True)

from build_app_support import FEED, SPARKLE_PUBLIC_KEY, sparkle_dir  # noqa: E402


def embed_sparkle(app: str, sign_id: str | None) -> None:
    fw_dir = os.path.join(app, "Contents", "Frameworks")
    os.makedirs(fw_dir, exist_ok=True)
    dest = os.path.join(fw_dir, "Sparkle.framework")
    shutil.rmtree(dest, ignore_errors=True)
    subprocess.run(["ditto", os.path.join(sparkle_dir(), "Sparkle.framework"), dest], check=True)
    # The XPC services are for sandboxed apps; this one isn't, so they're left out.
    shutil.rmtree(os.path.join(dest, "Versions", "B", "XPCServices"), ignore_errors=True)
    link = os.path.join(dest, "XPCServices")  # the top-level link to them would point at nothing
    if os.path.islink(link):
        os.remove(link)
    sign = ["codesign", "--force", "--sign", sign_id or "-"]
    if sign_id:
        sign += ["--options", "runtime", "--timestamp"]
    b = os.path.join(dest, "Versions", "B")
    for part in (os.path.join(b, "Autoupdate"), os.path.join(b, "Updater.app"), dest):
        subprocess.run(sign + [part], check=True)


os.chdir("dist")
if sys.platform == "darwin":
    plist = f"{NAME}.app/Contents/Info.plist"
    embed_sparkle(f"{NAME}.app", SIGN_ID)
    for key, kind, value in (("SUFeedURL", "-string", FEED), ("SUPublicEDKey", "-string", SPARKLE_PUBLIC_KEY),
                             # The app looks once a day itself and says so in its title and report;
                             # Sparkle's window comes when asked, from Help, Check for Updates.
                             ("SUEnableAutomaticChecks", "-bool", "NO"),
                             ("SUScheduledCheckInterval", "-integer", "86400")):
        subprocess.run(["plutil", "-replace", key, kind, value, plist], check=True)
    if os.environ.get("MIDI_TEMPO_FIX_TEST_FEED"):  # a local test feed is plain http
        subprocess.run(["plutil", "-replace", "NSAppTransportSecurity", "-json",
                        '{"NSAllowsLocalNetworking": true}', plist], check=True)
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
    if os.path.exists(out):
        os.remove(out)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", f"{NAME}.app", out], check=True)
    exe = f"{NAME}.app/Contents/MacOS/{NAME}"
else:
    out = "MIDI-Tempo-Fix-windows"
    shutil.make_archive(out, "zip", ".", NAME)
    out += ".zip"
    exe = os.path.join(NAME, NAME + ".exe")
    # The installer: per-person, no administrator, Start menu, uninstall in Settings.
    iscc = shutil.which("ISCC") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if os.path.exists(iscc):
        iss = os.path.join("..", "packaging", "windows", "MIDI Tempo Fix.iss")
        subprocess.run([iscc, "/Q", f"/DAppVersion={__version__}", f"/DSourceDir={os.path.abspath(NAME)}", iss],
                       check=True)
        print("Built", os.path.abspath("MIDI-Tempo-Fix-Setup.exe"))
    else:
        print("Inno Setup isn't installed, so no installer this time (the zip still works).")
# Read the self-test through a file, with a time limit: a broken build can leave a helper process
# holding an output pipe open, and reading a pipe would then wait forever.
import tempfile
with tempfile.TemporaryFile("w+") as log:
    try:
        subprocess.run([exe, "--self-test"], stdout=log, stderr=subprocess.STDOUT, timeout=120)
    except subprocess.TimeoutExpired:
        print("Self-test: TIMED OUT")
        sys.exit(1)
    log.seek(0)
    result = log.read().strip().splitlines()[-1:] or ["(no output)"]
print("Self-test:", result[0])
if "OK" not in result[0]:
    sys.exit("The built app failed its self-test.")
print("Built", os.path.abspath(out))
