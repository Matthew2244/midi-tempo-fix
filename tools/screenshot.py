"""Draw the README screenshot: python tools/screenshot.py OUTDIR

Makes a synthetic piano part and a mix with a 250 ms jump, runs the real app window
through Compare, and has the window draw itself into OUTDIR/screenshot.png
(NSView cacheDisplayInRect). No screen capture, so no screen-recording permission
prompt. The window is shown transparent and off-screen without activating, because
macOS paints nothing in a window that was never shown.
"""
import sys, os, time, ctypes, ctypes.util, pathlib
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
import test_project_and_audio as T
D=pathlib.Path(sys.argv[1])
# synthetic demo: a piano part, its full mix with a 250 ms jump at beat 60
notes=T.melody(seed=7, count=160)
mid=T.write(D,"Piano part.mid",T.melody_midi(notes))
T.render(notes, lambda b: b*0.5 + (0.25 if b>=60 else 0), notes[-1][0]*0.5+3, D/"Full mix.wav")
import wx
from midi_tempo import gui
app=wx.App(False)
f=gui.MainFrame(); f.SetSize((900,820))
gui.announce=lambda t: None
class FD:
    def __init__(s,*a,**k): pass
    def __enter__(s): return s
    def __exit__(s,*a): pass
    def ShowModal(s): return wx.ID_OK
    def GetPath(s): return str(D/"Full mix.wav")
gui.wx.FileDialog=FD
def wait(c,t=60):
    e=time.time()+t
    while time.time()<e and not c(): wx.SafeYield(); time.sleep(0.05)
f.check([mid]); wait(lambda: not f.busy and f.files)
f.on_compare(None); wait(lambda: not f.busy)
f.list.SetSelection(0)
f.SetTransparent(0); f.SetPosition((-3000,-3000)); f.ShowWithoutActivating()
f.Layout(); f.panel.Layout()
for _ in range(20): wx.SafeYield(); time.sleep(0.05)
f.list.SetSelection(0)
for _ in range(10): wx.SafeYield(); time.sleep(0.05)
objc=ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
objc.objc_getClass.restype=ctypes.c_void_p; objc.sel_registerName.restype=ctypes.c_void_p; objc.sel_registerName.argtypes=[ctypes.c_char_p]
def send(o,sel,*a,restype=ctypes.c_void_p,argtypes=()):
    fn=objc.objc_msgSend; fn.restype=restype; fn.argtypes=[ctypes.c_void_p,ctypes.c_void_p,*argtypes]
    return fn(o,objc.sel_registerName(sel.encode()),*a)
class R(ctypes.Structure): _fields_=[("x",ctypes.c_double),("y",ctypes.c_double),("w",ctypes.c_double),("h",ctypes.c_double)]
view=f.panel.GetHandle()
win=send(view,"window"); content=send(win,"contentView")
bounds=send(content,"bounds",restype=R)
rep=send(content,"bitmapImageRepForCachingDisplayInRect:",bounds,argtypes=(R,))
send(content,"cacheDisplayInRect:toBitmapImageRep:",bounds,rep,restype=None,argtypes=(R,ctypes.c_void_p))
props=send(objc.objc_getClass(b"NSDictionary"),"dictionary")
data=send(rep,"representationUsingType:properties:",4,props,argtypes=(ctypes.c_ulong,ctypes.c_void_p))
path=send(objc.objc_getClass(b"NSString"),"stringWithUTF8String:",str(D/"screenshot.png").encode(),argtypes=(ctypes.c_char_p,))
ok=send(data,"writeToFile:atomically:",path,True,restype=ctypes.c_bool,argtypes=(ctypes.c_void_p,ctypes.c_bool))
print("wrote",ok, bounds.w, bounds.h); print(f.report.GetValue())
f.Destroy()
