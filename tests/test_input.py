import os
import sys, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nelyinput as N

class FakeWin:
    def __init__(s): s.pos=None
    def GetSystemMetrics(s,i): return {76:0,77:0,78:1920,79:1080}[i]
    def SetCursorPos(s,x,y): s.pos=(x,y)

class FakeQuartz:
    def __init__(s): s.x,s.y=1000.0,700.0; s.log=[]
    def trusted(s): return True
    def bounds(s): return (0,0,2560,1440)
    def cursor(s): return (s.x,s.y)
    def warp(s,x,y): s.x,s.y=x,y; s.log.append(("warp",round(x),round(y)))
    def mouse(s,t,x,y,b=0,fields=None,flags=None):
        if t in (5,6,7,27): s.x,s.y=x,y
        s.log.append(("mouse",t,round(x),round(y),b,dict(fields or {}),flags))
    def key(s,code,down,flags,is_mod=False,repeat=False): s.log.append(("key",hex(code),down,hex(flags),is_mod,repeat))
    def scroll(s,ly,lx): s.log.append(("scroll",ly,lx))

class Link:
    def get(s): return {"address":"127.0.0.1","label":"Ethernet"}

pc_cfg={"share_input":True,"edge":"right","peer_name":"iMac","this_computer_name":"PC",
        "peer_addresses":[{"address":"127.0.0.1","label":"Ethernet"}]}
mac_cfg={"share_input":True,"edge":"left","peer_name":"PC","swap_ctrl_cmd":True,"keyboard_iso":True,"mouse_speed":1.0}
q=FakeQuartz(); mac=N.MacInputClient(mac_cfg, quartz=q); mac.start()
w=FakeWin(); pc=N.WinInputServer(pc_cfg, Link(), win_api=w)
threading.Thread(target=pc._conn_thread,daemon=True).start()
threading.Thread(target=pc._send_thread,daemon=True).start()
for _ in range(30):
    if pc.is_connected() and mac.is_connected(): break
    time.sleep(0.1)
print("connected:", pc.is_connected(), mac.is_connected(), "|", pc.status_text())

# local key held before switching (Shift), must be released locally later
assert pc.on_key(0xA0, 0x2A, 0) is False
assert pc.on_key(0xA0, 0x2A, N.LLKHF_UP) is False
# file-drag: button held at edge -> must NOT switch
pc.on_mouse(N.WM_LBUTTONDOWN,1919,300,0,0)
assert pc.on_mouse(N.WM_MOUSEMOVE,1919,300,0,0) is False and not pc.remote
pc.on_mouse(N.WM_LBUTTONUP,1919,300,0,0)
# push past right edge at 25% height
assert pc.on_mouse(N.WM_MOUSEMOVE,1919,270,0,0) is True and pc.remote
print("parked PC cursor at", w.pos)
time.sleep(0.2); print("mac after enter:", q.log[-1], "active:", mac.active)
# move right/down
pc.on_mouse(N.WM_MOUSEMOVE,960+15,540+5,0,0); pc.on_mouse(N.WM_MOUSEMOVE,960+15,540+5,0,0)
# double click
for _ in range(2):
    pc.on_mouse(N.WM_LBUTTONDOWN,960,540,0,0); pc.on_mouse(N.WM_LBUTTONUP,960,540,0,0)
# Ctrl+C  (Ctrl should become Cmd)
pc.on_key(0xA2,0x1D,0); pc.on_key(0x43,0x2E,0); pc.on_key(0x43,0x2E,N.LLKHF_UP); pc.on_key(0xA2,0x1D,N.LLKHF_UP)
# AltGr+Q (fake ctrl must be dropped): fake LCtrl scan 0x21D, then RAlt ext
pc.on_key(0xA2,0x21D,0); pc.on_key(0xA5,0x38,N.LLKHF_EXTENDED); pc.on_key(0x51,0x10,0)
pc.on_key(0x51,0x10,N.LLKHF_UP); pc.on_key(0xA5,0x38,N.LLKHF_EXTENDED|N.LLKHF_UP); pc.on_key(0xA2,0x21D,N.LLKHF_UP)
# arrow key (extended) and wheel
pc.on_key(0x25,0x4B,N.LLKHF_EXTENDED); pc.on_key(0x25,0x4B,N.LLKHF_EXTENDED|N.LLKHF_UP)
pc.on_mouse(N.WM_MOUSEWHEEL,960,540,(120<<16),0)
time.sleep(0.3)
for e in q.log[1:]: print("  ", e)
q.log.clear()
# now move far left on the Mac to come back
for _ in range(20): pc.on_mouse(N.WM_MOUSEMOVE,960-300,540,0,0)
time.sleep(0.4)
print("after moving left: pc.remote =", pc.remote, "PC cursor =", w.pos, "mac active =", mac.active)
# Scroll Lock locks to PC
pc.on_key(0x91,0x46,0); pc.on_key(0x91,0x46,N.LLKHF_UP)
print("locked:", pc.locked, "switch blocked:", pc.on_mouse(N.WM_MOUSEMOVE,1919,500,0,0) is False)
pc.on_key(0x91,0x46,0); pc.on_key(0x91,0x46,N.LLKHF_UP)
# enter again then kill the Mac connection -> PC must get control back
pc.on_mouse(N.WM_MOUSEMOVE,1919,500,0,0); print("entered again:", pc.remote)
N._hard_close(mac.conn)
time.sleep(0.5)
print("after Mac closed link: pc.remote =", pc.remote, "| key blocked?", pc.on_key(0x41,0x1E,0))
# reconnect, then simulate a Mac that freezes silently (no FIN, no keep-alive)
for _ in range(40):
    if pc.is_connected(): break
    time.sleep(0.1)
mac.handle = lambda m: None          # Mac stops answering
mac._send = lambda o: None
time.sleep(0.5); pc.on_mouse(N.WM_MOUSEMOVE,1919,500,0,0); print("entered frozen Mac:", pc.remote)
t0=time.time()
while pc.remote and time.time()-t0<8: time.sleep(0.2)
print("after Mac froze: pc.remote =", pc.remote, "in %.1fs" % (time.time()-t0))
