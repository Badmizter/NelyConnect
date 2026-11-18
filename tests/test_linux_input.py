import os
import sys, time, threading, tkinter as tk
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nelyinput as N
class FakeWin:
    def GetSystemMetrics(s,i): return {76:0,77:0,78:1920,79:1080}[i]
    def SetCursorPos(s,x,y): pass
class Link:
    def get(s): return {"address":"127.0.0.1","label":"Ethernet"}
root=tk.Tk(); root.geometry("400x200+300+300")
e=tk.Entry(root); e.place(x=10,y=10,width=380)
clicked=[]
b=tk.Button(root,text="Click me",command=lambda: clicked.append(1)); b.place(x=150,y=100,width=100,height=40)
mac_cfg={"share_input":True,"edge":"left","peer_name":"PC","mouse_speed":1.0}
lin=N.LinuxInputClient(mac_cfg); lin.start()
pc=N.WinInputServer({"share_input":True,"edge":"right","peer_name":"Linux","this_computer_name":"PC",
     "peer_addresses":[{"address":"127.0.0.1","label":"Ethernet"}]}, Link(), win_api=FakeWin())
threading.Thread(target=pc._conn_thread,daemon=True).start(); threading.Thread(target=pc._send_thread,daemon=True).start()
def script():
    for _ in range(30):
        if pc.is_connected(): break
        time.sleep(0.1)
    pc.on_mouse(N.WM_MOUSEMOVE,1919,540,0,0)        # enter at mid height
    time.sleep(0.3); print("after enter cursor:", lin.q.cursor())
    # move to the entry (x≈330,y≈320) and click it to focus
    tx,ty=330,322; cx,cy=lin.q.cursor()
    pc.on_mouse(N.WM_MOUSEMOVE,960+int(tx-cx)//2,540+int(ty-cy)//2,0,0); pc.on_mouse(N.WM_MOUSEMOVE,960+int(tx-cx)-int(tx-cx)//2,540+int(ty-cy)-int(ty-cy)//2,0,0)
    time.sleep(0.3); print("at entry:", lin.q.cursor())
    pc.on_mouse(N.WM_LBUTTONDOWN,960,540,0,0); pc.on_mouse(N.WM_LBUTTONUP,960,540,0,0); time.sleep(0.3)
    # type: Shift+h, i, space, 1, 2  (scan codes)
    for scan,up in ((0x2A,0),(0x23,0),(0x23,1),(0x2A,1),(0x17,0),(0x17,1),(0x39,0),(0x39,1),(0x02,0),(0x02,1),(0x03,0),(0x03,1)):
        pc.on_key(0, scan, N.LLKHF_UP if up else 0)
    # Left arrow (extended) then 'x'
    pc.on_key(0,0x4B,N.LLKHF_EXTENDED); pc.on_key(0,0x4B,N.LLKHF_EXTENDED|N.LLKHF_UP); pc.on_key(0,0x2D,0); pc.on_key(0,0x2D,N.LLKHF_UP)
    time.sleep(0.4)
    # move to button center (500,420) and click
    cx,cy=lin.q.cursor(); pc.on_mouse(N.WM_MOUSEMOVE,960+int(500-cx),540+int(420-cy),0,0); time.sleep(0.2)
    pc.on_mouse(N.WM_LBUTTONDOWN,960,540,0,0); pc.on_mouse(N.WM_LBUTTONUP,960,540,0,0); time.sleep(0.4)
    # leave via left edge
    for _ in range(10): pc.on_mouse(N.WM_MOUSEMOVE,960-200,540,0,0)
    time.sleep(0.4)
    root.after(0, lambda: (print("typed:", repr(e.get())), print("button clicked:", bool(clicked)),
                           print("PC got control back:", not pc.remote), root.destroy()))
threading.Thread(target=script,daemon=True).start()
root.mainloop()
