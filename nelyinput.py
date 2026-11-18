"""
NelyConnect - keyboard & mouse sharing.

WinInputServer  (on the PC that has the keyboard and mouse)
    Low-level Windows hooks watch the mouse. When it is pushed past the configured
    edge, local input is blocked and every move / click / key is sent to the Mac.
    Scroll Lock: while on the Mac -> come back instantly; on the PC -> lock/unlock
    the mouse to the PC screen.

MacInputClient  (on the iMac)
    Receives those events and replays them with Quartz (CoreGraphics). When the
    cursor is pushed back past the edge facing the PC, control returns to the PC.

If the connection drops while the Mac is being controlled, control returns to the
PC immediately so you are never left without a working mouse.
"""
import json
import logging
import queue
import socket
import sys
import threading
import time

log = logging.getLogger("nelyconnect")

INPUT_PORT = 24870
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def _hard_close(s):
    """Close a socket even if a reader thread still holds a file object on it."""
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def _line(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


# =============================================================================
# Windows: capture
# =============================================================================

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x200, 0x201, 0x202
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP = 0x204, 0x205, 0x207, 0x208
WM_MOUSEWHEEL, WM_XBUTTONDOWN, WM_XBUTTONUP, WM_MOUSEHWHEEL = 0x20A, 0x20B, 0x20C, 0x20E
BUTTON_MSG = {WM_LBUTTONDOWN: (1, 1), WM_LBUTTONUP: (1, 0),
              WM_RBUTTONDOWN: (2, 1), WM_RBUTTONUP: (2, 0),
              WM_MBUTTONDOWN: (3, 1), WM_MBUTTONUP: (3, 0)}
DOWN_MSGS = (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN)
UP_MSGS = (WM_LBUTTONUP, WM_RBUTTONUP, WM_MBUTTONUP, WM_XBUTTONUP)
VK_SCROLL = 0x91
LLMHF_INJECTED = 0x01
LLKHF_EXTENDED, LLKHF_INJECTED, LLKHF_UP = 0x01, 0x10, 0x80
ALTGR_FAKE_CTRL_SCAN = 0x21D


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class WinInputServer:
    def __init__(self, cfg, link, on_change=None, win_api=None):
        self.cfg = cfg
        self.link = link
        self.on_change = on_change or (lambda: None)
        self.api = win_api                 # injected for tests; real one built in hook thread
        self.remote = False
        self.locked = False
        self.sock = None
        self.sock_addr = None
        self.sock_lock = threading.Lock()
        self.out = queue.Queue(maxsize=20000)
        self.buttons = set()
        self.local_keys = set()
        self.cx = self.cy = 0

    # ---- public
    def enabled(self):
        return bool(self.cfg.get("share_input", True))

    def is_connected(self):
        return self.sock is not None

    def start(self):
        threading.Thread(target=self._hook_thread, daemon=True, name="hooks").start()
        threading.Thread(target=self._conn_thread, daemon=True, name="input-conn").start()
        threading.Thread(target=self._send_thread, daemon=True, name="input-send").start()

    def status_text(self):
        if not self.enabled():
            return "Keyboard & mouse sharing is off"
        if not self.sock:
            return "Keyboard & mouse: waiting for the %s" % self.cfg["peer_name"]
        if self.remote:
            return "Keyboard & mouse: controlling the %s" % self.cfg["peer_name"]
        return "Keyboard & mouse: ready%s" % (" (locked to this screen)" if self.locked else "")

    # ---- geometry
    def _bounds(self):
        gm = self.api.GetSystemMetrics
        return gm(76), gm(77), gm(78), gm(79)     # virtual screen x, y, w, h

    def _edge_hit(self, x, y):
        vx, vy, vw, vh = self._bounds()
        edge = self.cfg.get("edge", "right")
        if edge == "right" and x >= vx + vw - 1:
            return (y - vy) / max(vh, 1)
        if edge == "left" and x <= vx:
            return (y - vy) / max(vh, 1)
        if edge == "top" and y <= vy:
            return (x - vx) / max(vw, 1)
        if edge == "bottom" and y >= vy + vh - 1:
            return (x - vx) / max(vw, 1)
        return None

    def _edge_pos(self, f):
        vx, vy, vw, vh = self._bounds()
        f = min(max(f, 0.0), 1.0)
        edge = self.cfg.get("edge", "right")
        if edge == "right":
            return vx + vw - 4, int(vy + f * (vh - 1))
        if edge == "left":
            return vx + 3, int(vy + f * (vh - 1))
        if edge == "top":
            return int(vx + f * (vw - 1)), vy + 3
        return int(vx + f * (vw - 1)), vy + vh - 4

    # ---- switching
    def _enter_remote(self, f):
        vx, vy, vw, vh = self._bounds()
        self.cx, self.cy = vx + vw // 2, vy + vh // 2
        self.remote = True
        self.api.SetCursorPos(self.cx, self.cy)
        self._queue({"t": "enter", "f": round(f, 4)})
        log.info("-> controlling %s", self.cfg["peer_name"])
        self.on_change()

    def return_local(self, f=0.5, tell_peer=False):
        if not self.remote:
            return
        self.remote = False
        if tell_peer:
            self._queue({"t": "leave"})
        x, y = self._edge_pos(f)
        self.api.SetCursorPos(x, y)
        log.info("<- back on this computer")
        self.on_change()

    # ---- hook handlers (return True = block the event locally)
    def on_mouse(self, msg, x, y, mouse_data, flags):
        if flags & LLMHF_INJECTED:
            return False
        if self.remote and (not self.enabled() or self.sock is None):
            self.return_local(0.5)
            return False
        if not self.remote:
            if msg in DOWN_MSGS:
                self.buttons.add(msg)
            elif msg in UP_MSGS:
                self.buttons.discard(msg - 1)
            elif (msg == WM_MOUSEMOVE and self.enabled() and self.sock is not None
                  and not self.locked and not self.buttons):
                f = self._edge_hit(x, y)
                if f is not None:
                    self._enter_remote(f)
                    return True
            return False

        if msg == WM_MOUSEMOVE:
            dx = max(-300, min(300, x - self.cx))
            dy = max(-300, min(300, y - self.cy))
            if dx or dy:
                self._queue({"t": "m", "x": dx, "y": dy})
        elif msg in BUTTON_MSG:
            b, d = BUTTON_MSG[msg]
            self._queue({"t": "b", "b": b, "d": d})
        elif msg in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            which = (mouse_data >> 16) & 0xFFFF            # 1 = back, 2 = forward
            self._queue({"t": "b", "b": 3 + which, "d": 1 if msg == WM_XBUTTONDOWN else 0})
        elif msg == WM_MOUSEWHEEL:
            self._queue({"t": "w", "v": _s16(mouse_data >> 16)})
        elif msg == WM_MOUSEHWHEEL:
            self._queue({"t": "w", "h": _s16(mouse_data >> 16)})
        return True

    def on_key(self, vk, scan, flags):
        if flags & LLKHF_INJECTED:
            return False
        up = bool(flags & LLKHF_UP)
        ext = 1 if flags & LLKHF_EXTENDED else 0
        if vk == VK_SCROLL:
            if not up:
                if self.remote:
                    self.return_local(0.5, tell_peer=True)
                    return True
                self.locked = not self.locked
                log.info("Mouse %s this screen", "locked to" if self.locked else "unlocked from")
                self.on_change()
            return self.remote
        if self.remote and (not self.enabled() or self.sock is None):
            self.return_local(0.5)
        if not self.remote:
            if up:
                self.local_keys.discard((scan, ext))
            else:
                self.local_keys.add((scan, ext))
            return False
        if scan == ALTGR_FAKE_CTRL_SCAN:          # Windows adds a fake Ctrl to AltGr
            return True
        if up and (scan, ext) in self.local_keys:  # pressed before switching: let Windows release it
            self.local_keys.discard((scan, ext))
            return False
        self._queue({"t": "k", "s": scan, "e": ext, "d": 0 if up else 1})
        return True

    # ---- networking
    def _queue(self, obj):
        try:
            self.out.put_nowait(obj)
        except queue.Full:
            pass

    def _drop(self, s=None):
        with self.sock_lock:
            if s is not None and s is not self.sock:
                return
            old, self.sock, self.sock_addr = self.sock, None, None
        if old:
            _hard_close(old)
            log.info("Input link closed")
        self.return_local(0.5)
        self.on_change()

    def _priority(self, entry):
        addrs = [a["address"] for a in self.cfg["peer_addresses"]]
        return addrs.index(entry["address"]) if entry and entry["address"] in addrs else 99

    def _conn_thread(self):
        while True:
            try:
                if not self.enabled():
                    if self.sock:
                        self._drop()
                elif self.sock is None:
                    a = self.link.get()
                    if a:
                        s = socket.create_connection((a["address"], INPUT_PORT), timeout=2)
                        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        s.settimeout(4)           # no answer for 4 s -> give the mouse back
                        s.sendall(_line({"t": "hello", "name": self.cfg["this_computer_name"]}))
                        with self.sock_lock:
                            self.sock, self.sock_addr = s, a
                        threading.Thread(target=self._reader, args=(s,), daemon=True).start()
                        log.info("Input link up via %s (%s)", a["label"], a["address"])
                        self.on_change()
                elif not self.remote:
                    a = self.link.get()          # move back to Ethernet when it returns
                    if a and self._priority(a) < self._priority(self.sock_addr):
                        self._drop()
                        continue
                if self.sock:
                    self._queue({"t": "ka"})
            except OSError:
                pass
            except Exception:
                log.exception("input connection")
            time.sleep(1)

    def _reader(self, s):
        try:
            f = s.makefile("rb")
            for raw in f:
                msg = json.loads(raw.decode("utf-8"))
                if msg.get("t") == "leave":
                    self.return_local(float(msg.get("f", 0.5)))
        except Exception:
            pass
        self._drop(s)

    def _send_thread(self):
        while True:
            batch = [self.out.get()]
            while True:
                try:
                    batch.append(self.out.get_nowait())
                except queue.Empty:
                    break
            merged = []
            for m in batch:                    # combine back-to-back moves
                if m["t"] == "m" and merged and merged[-1]["t"] == "m":
                    merged[-1] = {"t": "m", "x": merged[-1]["x"] + m["x"], "y": merged[-1]["y"] + m["y"]}
                else:
                    merged.append(m)
            s = self.sock
            if s is None:
                continue
            try:
                s.sendall(b"".join(_line(m) for m in merged))
            except OSError:
                self._drop(s)

    # ---- real Windows hooks
    def _hook_thread(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class MSLL(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

        class KBLL(ctypes.Structure):
            _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        if self.api is None:
            self.api = user32

        def mouse_proc(n, w, l):
            if n == 0:
                try:
                    ms = ctypes.cast(l, ctypes.POINTER(MSLL)).contents
                    if self.on_mouse(w, ms.x, ms.y, ms.mouseData, ms.flags):
                        return 1
                except Exception:
                    log.exception("mouse hook")
            return user32.CallNextHookEx(None, n, w, l)

        def key_proc(n, w, l):
            if n == 0:
                try:
                    kb = ctypes.cast(l, ctypes.POINTER(KBLL)).contents
                    if self.on_key(kb.vk, kb.scan, kb.flags):
                        return 1
                except Exception:
                    log.exception("keyboard hook")
            return user32.CallNextHookEx(None, n, w, l)

        self._procs = (HOOKPROC(mouse_proc), HOOKPROC(key_proc))   # keep alive
        hmod = kernel32.GetModuleHandleW(None)
        hm = user32.SetWindowsHookExW(14, self._procs[0], hmod, 0)   # WH_MOUSE_LL
        hk = user32.SetWindowsHookExW(13, self._procs[1], hmod, 0)   # WH_KEYBOARD_LL
        if not hm or not hk:
            log.error("Could not install input hooks (error %s)", ctypes.get_last_error())
            return
        log.info("Input hooks installed")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# =============================================================================
# Mac: replay
# =============================================================================

# Windows scan code (set 1) + extended flag  ->  macOS virtual key code (by key position)
SCAN_TO_MAC = {
    0x01: 0x35, 0x02: 0x12, 0x03: 0x13, 0x04: 0x14, 0x05: 0x15, 0x06: 0x17, 0x07: 0x16,
    0x08: 0x1A, 0x09: 0x1C, 0x0A: 0x19, 0x0B: 0x1D, 0x0C: 0x1B, 0x0D: 0x18, 0x0E: 0x33,
    0x0F: 0x30, 0x10: 0x0C, 0x11: 0x0D, 0x12: 0x0E, 0x13: 0x0F, 0x14: 0x11, 0x15: 0x10,
    0x16: 0x20, 0x17: 0x22, 0x18: 0x1F, 0x19: 0x23, 0x1A: 0x21, 0x1B: 0x1E, 0x1C: 0x24,
    0x1D: 0x3B, 0x1E: 0x00, 0x1F: 0x01, 0x20: 0x02, 0x21: 0x03, 0x22: 0x05, 0x23: 0x04,
    0x24: 0x26, 0x25: 0x28, 0x26: 0x25, 0x27: 0x29, 0x28: 0x27, 0x29: 0x32, 0x2A: 0x38,
    0x2B: 0x2A, 0x2C: 0x06, 0x2D: 0x07, 0x2E: 0x08, 0x2F: 0x09, 0x30: 0x0B, 0x31: 0x2D,
    0x32: 0x2E, 0x33: 0x2B, 0x34: 0x2F, 0x35: 0x2C, 0x36: 0x3C, 0x37: 0x43, 0x38: 0x3A,
    0x39: 0x31, 0x3A: 0x39, 0x3B: 0x7A, 0x3C: 0x78, 0x3D: 0x63, 0x3E: 0x76, 0x3F: 0x60,
    0x40: 0x61, 0x41: 0x62, 0x42: 0x64, 0x43: 0x65, 0x44: 0x6D, 0x45: 0x47, 0x47: 0x59,
    0x48: 0x5B, 0x49: 0x5C, 0x4A: 0x4E, 0x4B: 0x56, 0x4C: 0x57, 0x4D: 0x58, 0x4E: 0x45,
    0x4F: 0x53, 0x50: 0x54, 0x51: 0x55, 0x52: 0x52, 0x53: 0x41, 0x56: 0x32, 0x57: 0x67,
    0x58: 0x6F,
}
SCAN_EXT_TO_MAC = {
    0x1C: 0x4C,  # keypad Enter
    0x1D: 0x3E,  # right Ctrl
    0x35: 0x4B,  # keypad /
    0x37: 0x69,  # Print Screen -> F13
    0x38: 0x3D,  # right Alt / AltGr -> right Option
    0x47: 0x73, 0x48: 0x7E, 0x49: 0x74, 0x4B: 0x7B, 0x4D: 0x7C,   # Home Up PgUp Left Right
    0x4F: 0x77, 0x50: 0x7D, 0x51: 0x79, 0x52: 0x72, 0x53: 0x75,   # End Down PgDn Insert Delete
    0x5B: 0x37, 0x5C: 0x36,  # Windows keys -> Command
    0x5D: 0x6E,  # Menu key
}
MAC_SHIFT, MAC_CTRL, MAC_ALT, MAC_CMD = 0x20000, 0x40000, 0x80000, 0x100000
MOD_FLAG = {0x38: MAC_SHIFT, 0x3C: MAC_SHIFT, 0x3B: MAC_CTRL, 0x3E: MAC_CTRL,
            0x3A: MAC_ALT, 0x3D: MAC_ALT, 0x37: MAC_CMD, 0x36: MAC_CMD}
CTRL_CMD_SWAP = {0x3B: 0x37, 0x37: 0x3B, 0x3E: 0x36, 0x36: 0x3E}

# CGEventType
EV_LDOWN, EV_LUP, EV_RDOWN, EV_RUP, EV_MOVED = 1, 2, 3, 4, 5
EV_LDRAG, EV_RDRAG, EV_FLAGS, EV_ODOWN, EV_OUP, EV_ODRAG = 6, 7, 12, 25, 26, 27
# CGEventField
F_CLICKSTATE, F_BUTTON, F_DX, F_DY, F_AUTOREPEAT = 1, 3, 4, 5, 8


def mac_keycode(scan, ext, iso=True, swap=True):
    code = SCAN_EXT_TO_MAC.get(scan) if ext else None
    if code is None:
        code = SCAN_TO_MAC.get(scan)
    if code is None:
        return None
    if scan == 0x29 and not ext and iso:
        code = 0x0A           # key left of "1" on ISO keyboards
    if swap:
        code = CTRL_CMD_SWAP.get(code, code)
    return code


class _Quartz:
    """Thin ctypes wrapper around the CoreGraphics calls we need."""

    def __init__(self):
        import ctypes
        from ctypes import c_void_p, c_uint32, c_uint16, c_int32, c_int64, c_uint64, c_bool, c_double

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", c_double), ("y", c_double)]

        class CGSize(ctypes.Structure):
            _fields_ = [("width", c_double), ("height", c_double)]

        class CGRect(ctypes.Structure):
            _fields_ = [("origin", CGPoint), ("size", CGSize)]

        self.CGPoint = CGPoint
        self.ctypes = ctypes
        cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg.CGEventSourceCreate.restype = c_void_p
        cg.CGEventSourceCreate.argtypes = [c_int32]
        cg.CGEventCreateMouseEvent.restype = c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, CGPoint, c_uint32]
        cg.CGEventCreateKeyboardEvent.restype = c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint16, c_bool]
        cg.CGEventCreateScrollWheelEvent2.restype = c_void_p
        cg.CGEventCreateScrollWheelEvent2.argtypes = [c_void_p, c_uint32, c_uint32, c_int32, c_int32, c_int32]
        cg.CGEventCreate.restype = c_void_p
        cg.CGEventCreate.argtypes = [c_void_p]
        cg.CGEventGetLocation.restype = CGPoint
        cg.CGEventGetLocation.argtypes = [c_void_p]
        cg.CGEventSetFlags.argtypes = [c_void_p, c_uint64]
        cg.CGEventSetType.argtypes = [c_void_p, c_uint32]
        cg.CGEventSetIntegerValueField.argtypes = [c_void_p, c_uint32, c_int64]
        cg.CGEventPost.argtypes = [c_uint32, c_void_p]
        cg.CGWarpMouseCursorPosition.argtypes = [CGPoint]
        cg.CGAssociateMouseAndMouseCursorPosition.argtypes = [c_bool]
        cg.CGGetActiveDisplayList.argtypes = [c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(c_uint32)]
        cg.CGDisplayBounds.restype = CGRect
        cg.CGDisplayBounds.argtypes = [c_uint32]
        cg.AXIsProcessTrusted.restype = c_bool
        cf.CFRelease.argtypes = [c_void_p]
        self.cg, self.cf = cg, cf
        self.src = cg.CGEventSourceCreate(1)       # HID system state

    def trusted(self):
        return bool(self.cg.AXIsProcessTrusted())

    def bounds(self):
        ct = self.ctypes
        ids = (ct.c_uint32 * 16)()
        n = ct.c_uint32(0)
        self.cg.CGGetActiveDisplayList(16, ids, ct.byref(n))
        rects = [self.cg.CGDisplayBounds(ids[i]) for i in range(n.value)] or [self.cg.CGDisplayBounds(0)]
        x0 = min(r.origin.x for r in rects)
        y0 = min(r.origin.y for r in rects)
        x1 = max(r.origin.x + r.size.width for r in rects)
        y1 = max(r.origin.y + r.size.height for r in rects)
        return x0, y0, x1, y1

    def cursor(self):
        ev = self.cg.CGEventCreate(None)
        p = self.cg.CGEventGetLocation(ev)
        self.cf.CFRelease(ev)
        return p.x, p.y

    def warp(self, x, y):
        self.cg.CGWarpMouseCursorPosition(self.CGPoint(x, y))
        self.cg.CGAssociateMouseAndMouseCursorPosition(True)

    def mouse(self, etype, x, y, button=0, fields=None, flags=None):
        ev = self.cg.CGEventCreateMouseEvent(self.src, etype, self.CGPoint(x, y), button)
        for k, v in (fields or {}).items():
            self.cg.CGEventSetIntegerValueField(ev, k, int(v))
        if flags is not None:
            self.cg.CGEventSetFlags(ev, flags)
        self.cg.CGEventPost(0, ev)
        self.cf.CFRelease(ev)

    def key(self, code, down, flags, is_mod=False, repeat=False):
        ev = self.cg.CGEventCreateKeyboardEvent(self.src, code, bool(down))
        if is_mod:
            self.cg.CGEventSetType(ev, EV_FLAGS)
        self.cg.CGEventSetFlags(ev, flags)
        if repeat:
            self.cg.CGEventSetIntegerValueField(ev, F_AUTOREPEAT, 1)
        self.cg.CGEventPost(0, ev)
        self.cf.CFRelease(ev)

    def scroll(self, lines_y, lines_x):
        ev = self.cg.CGEventCreateScrollWheelEvent2(self.src, 1, 2, int(lines_y), int(lines_x), 0)
        self.cg.CGEventPost(0, ev)
        self.cf.CFRelease(ev)


class MacInputClient:
    def __init__(self, cfg, on_change=None, quartz=None):
        self.cfg = cfg
        self.on_change = on_change or (lambda: None)
        self.q = quartz or _Quartz()
        self.active = False
        self.conn = None
        self.peer = None
        self.send_lock = threading.Lock()
        self.keys = set()
        self.buttons = set()
        self.mods = 0
        self.acc = [0.0, 0.0, 0.0, 0.0]     # mouse x/y and wheel y/x remainders
        self.clicks = {}                     # button -> (time, x, y, count)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", int(cfg.get("input_port", INPUT_PORT))))
        self.sock.listen(2)

    def enabled(self):
        return bool(self.cfg.get("share_input", True))

    def trusted(self):
        try:
            return self.q.trusted()
        except Exception:
            return True

    def is_connected(self):
        return self.conn is not None

    def status_text(self):
        if not self.enabled():
            return "Keyboard & mouse sharing is off"
        if not self.conn:
            return "Keyboard & mouse: waiting for the %s" % self.cfg["peer_name"]
        if self.active:
            return "Keyboard & mouse: the %s is controlling this Mac" % self.cfg["peer_name"]
        return "Keyboard & mouse: ready"

    def start(self):
        threading.Thread(target=self._accept, daemon=True, name="input-accept").start()

    # ---- network
    def _accept(self):
        while True:
            conn, addr = self.sock.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            old, self.conn, self.peer = self.conn, conn, addr[0]
            if old:
                _hard_close(old)
            log.info("Input link from %s", addr[0])
            threading.Thread(target=self._reader, args=(conn,), daemon=True).start()
            self.on_change()

    def _send(self, obj):
        c = self.conn
        if c:
            try:
                with self.send_lock:
                    c.sendall(_line(obj))
            except OSError:
                pass

    def _reader(self, conn):
        try:
            conn.settimeout(10)          # the PC sends a keep-alive every second
            f = conn.makefile("rb")
            for raw in f:
                self.handle(json.loads(raw.decode("utf-8")))
        except Exception:
            pass
        if conn is self.conn:
            self.conn = None
            self.release_all()
            self.active = False
            log.info("Input link closed")
            self.on_change()

    # ---- events
    def handle(self, m):
        t = m.get("t")
        if t == "ka":
            self._send({"t": "ka"})          # lets the PC know this Mac is still there
            return
        if t == "hello":
            return
        if t == "enter":
            if not self.enabled():
                self._send({"t": "leave", "f": m.get("f", 0.5)})   # bounce straight back
                return
            self._enter(float(m.get("f", 0.5)))
        elif t == "leave":
            self.release_all()
            self.active = False
            self.on_change()
        elif not self.active:
            return
        elif t == "m":
            self._move(m.get("x", 0), m.get("y", 0))
        elif t == "b":
            self._button(int(m["b"]), bool(m["d"]))
        elif t == "w":
            self._wheel(m.get("v", 0), m.get("h", 0))
        elif t == "k":
            self._key(int(m["s"]), int(m.get("e", 0)), bool(m["d"]))

    def _edge(self):
        return self.cfg.get("edge", "left")

    def _enter(self, f):
        x0, y0, x1, y1 = self.q.bounds()
        f = min(max(f, 0.0), 1.0)
        e = self._edge()
        if e == "left":
            x, y = x0 + 2, y0 + f * (y1 - y0 - 1)
        elif e == "right":
            x, y = x1 - 3, y0 + f * (y1 - y0 - 1)
        elif e == "top":
            x, y = x0 + f * (x1 - x0 - 1), y0 + 2
        else:
            x, y = x0 + f * (x1 - x0 - 1), y1 - 3
        self.acc = [0.0, 0.0, 0.0, 0.0]
        self.q.warp(x, y)
        self.active = True
        log.info("Controlled by %s", self.cfg["peer_name"])
        self.on_change()

    def _leave(self, f):
        self.release_all()
        self.active = False
        self._send({"t": "leave", "f": round(f, 4)})
        log.info("Control returned to %s", self.cfg["peer_name"])
        self.on_change()

    def _move(self, dx, dy):
        speed = float(self.cfg.get("mouse_speed", 1.0))
        self.acc[0] += dx * speed
        self.acc[1] += dy * speed
        ix, iy = int(self.acc[0]), int(self.acc[1])
        self.acc[0] -= ix
        self.acc[1] -= iy
        if not ix and not iy:
            return
        x0, y0, x1, y1 = self.q.bounds()
        cx, cy = self.q.cursor()
        nx, ny = cx + ix, cy + iy
        if not self.buttons:
            e = self._edge()
            if e == "left" and nx < x0:
                return self._leave((cy - y0) / max(y1 - y0, 1))
            if e == "right" and nx > x1 - 1:
                return self._leave((cy - y0) / max(y1 - y0, 1))
            if e == "top" and ny < y0:
                return self._leave((cx - x0) / max(x1 - x0, 1))
            if e == "bottom" and ny > y1 - 1:
                return self._leave((cx - x0) / max(x1 - x0, 1))
        nx = min(max(nx, x0), x1 - 1)
        ny = min(max(ny, y0), y1 - 1)
        if 1 in self.buttons:
            etype, btn = EV_LDRAG, 0
        elif 2 in self.buttons:
            etype, btn = EV_RDRAG, 1
        elif self.buttons:
            etype, btn = EV_ODRAG, min(self.buttons) - 1
        else:
            etype, btn = EV_MOVED, 0
        self.q.mouse(etype, nx, ny, btn, {F_DX: ix, F_DY: iy}, self.mods)

    def _button(self, b, down):
        x, y = self.q.cursor()
        if down:
            t, px, py, n = self.clicks.get(b, (0, -99, -99, 0))
            n = n + 1 if (time.time() - t < 0.5 and abs(x - px) < 5 and abs(y - py) < 5) else 1
            self.clicks[b] = (time.time(), x, y, n)
            self.buttons.add(b)
        else:
            n = self.clicks.get(b, (0, 0, 0, 1))[3]
            self.buttons.discard(b)
        if b == 1:
            etype, btn = (EV_LDOWN if down else EV_LUP), 0
        elif b == 2:
            etype, btn = (EV_RDOWN if down else EV_RUP), 1
        else:
            etype, btn = (EV_ODOWN if down else EV_OUP), b - 1     # 2 = middle, 3/4 = back/forward
        self.q.mouse(etype, x, y, btn, {F_CLICKSTATE: n, F_BUTTON: btn}, self.mods)

    def _wheel(self, v, h):
        lines = float(self.cfg.get("scroll_lines", 3))
        sign = -1 if self.cfg.get("invert_scroll") else 1
        self.acc[2] += v / 120.0 * lines * sign
        self.acc[3] += -h / 120.0 * lines * sign
        ly, lx = int(self.acc[2]), int(self.acc[3])
        self.acc[2] -= ly
        self.acc[3] -= lx
        if ly or lx:
            self.q.scroll(ly, lx)

    def _key(self, scan, ext, down):
        code = mac_keycode(scan, ext, iso=self.cfg.get("keyboard_iso", True),
                           swap=self.cfg.get("swap_ctrl_cmd", True))
        if code is None:
            return
        repeat = down and code in self.keys
        if down:
            self.keys.add(code)
        else:
            self.keys.discard(code)
        is_mod = code in MOD_FLAG
        if is_mod:
            m = 0
            for k in self.keys:
                m |= MOD_FLAG.get(k, 0)
            self.mods = m
        self.q.key(code, down, self.mods, is_mod, repeat)

    def release_all(self):
        try:
            for code in list(self.keys):
                self.keys.discard(code)
                if code in MOD_FLAG:
                    m = 0
                    for k in self.keys:
                        m |= MOD_FLAG.get(k, 0)
                    self.mods = m
                self.q.key(code, False, self.mods, code in MOD_FLAG)
            if self.buttons:
                x, y = self.q.cursor()
                for b in list(self.buttons):
                    self.buttons.discard(b)
                    etype, btn = {1: (EV_LUP, 0), 2: (EV_RUP, 1)}.get(b, (EV_OUP, b - 1))
                    self.q.mouse(etype, x, y, btn, {F_BUTTON: btn}, 0)
            self.mods = 0
        except Exception:
            log.exception("release_all")


# =============================================================================
# Linux (X11): replay
# =============================================================================

IS_LINUX = sys.platform.startswith("linux")

# Windows set-1 scan codes match Linux evdev key codes for the main block (0x01-0x58);
# only the "extended" keys need a table.  X11 key code = evdev code + 8.
SCAN_EXT_TO_EVDEV = {
    0x1C: 96, 0x1D: 97, 0x35: 98, 0x37: 99, 0x38: 100, 0x47: 102, 0x48: 103, 0x49: 104,
    0x4B: 105, 0x4D: 106, 0x4F: 107, 0x50: 108, 0x51: 109, 0x52: 110, 0x53: 111,
    0x5B: 125, 0x5C: 126, 0x5D: 127,
}


def x11_keycode(scan, ext):
    if ext:
        code = SCAN_EXT_TO_EVDEV.get(scan)
    else:
        code = scan if 0x01 <= scan <= 0x58 else None
    return None if code is None else code + 8


class _XTest:
    """Same interface as _Quartz, implemented with Xlib + XTest (X11 sessions)."""

    X_BUTTON = {0: 1, 1: 3, 2: 2, 3: 8, 4: 9}     # Mac-style button index -> X button

    def __init__(self):
        import ctypes
        import ctypes.util
        if os_environ_wayland():
            raise RuntimeError("Wayland session: keyboard & mouse control needs an X11 (Xorg) session")
        c = ctypes
        x11 = c.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        xt = c.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
        x11.XOpenDisplay.restype = c.c_void_p
        x11.XOpenDisplay.argtypes = [c.c_char_p]
        x11.XDefaultScreen.argtypes = [c.c_void_p]
        x11.XDisplayWidth.argtypes = [c.c_void_p, c.c_int]
        x11.XDisplayHeight.argtypes = [c.c_void_p, c.c_int]
        x11.XDefaultRootWindow.restype = c.c_ulong
        x11.XDefaultRootWindow.argtypes = [c.c_void_p]
        x11.XQueryPointer.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong), c.POINTER(c.c_ulong),
                                      c.POINTER(c.c_int), c.POINTER(c.c_int), c.POINTER(c.c_int),
                                      c.POINTER(c.c_int), c.POINTER(c.c_uint)]
        x11.XFlush.argtypes = [c.c_void_p]
        xt.XTestFakeMotionEvent.argtypes = [c.c_void_p, c.c_int, c.c_int, c.c_int, c.c_ulong]
        xt.XTestFakeButtonEvent.argtypes = [c.c_void_p, c.c_uint, c.c_int, c.c_ulong]
        xt.XTestFakeKeyEvent.argtypes = [c.c_void_p, c.c_uint, c.c_int, c.c_ulong]
        self.c, self.x11, self.xt = c, x11, xt
        self.dpy = x11.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("Cannot open the X display")
        self.scr = x11.XDefaultScreen(self.dpy)
        self.root = x11.XDefaultRootWindow(self.dpy)

    def trusted(self):
        return True

    def bounds(self):
        return (0, 0, self.x11.XDisplayWidth(self.dpy, self.scr), self.x11.XDisplayHeight(self.dpy, self.scr))

    def cursor(self):
        c = self.c
        r, ch = c.c_ulong(), c.c_ulong()
        rx, ry, wx, wy, m = c.c_int(), c.c_int(), c.c_int(), c.c_int(), c.c_uint()
        self.x11.XQueryPointer(self.dpy, self.root, c.byref(r), c.byref(ch), c.byref(rx), c.byref(ry),
                               c.byref(wx), c.byref(wy), c.byref(m))
        return float(rx.value), float(ry.value)

    def warp(self, x, y):
        self.xt.XTestFakeMotionEvent(self.dpy, -1, int(x), int(y), 0)
        self.x11.XFlush(self.dpy)

    def mouse(self, etype, x, y, button=0, fields=None, flags=None):
        if etype in (EV_MOVED, EV_LDRAG, EV_RDRAG, EV_ODRAG):
            self.xt.XTestFakeMotionEvent(self.dpy, -1, int(x), int(y), 0)
        else:
            down = etype in (EV_LDOWN, EV_RDOWN, EV_ODOWN)
            self.xt.XTestFakeButtonEvent(self.dpy, self.X_BUTTON.get(button, 1), 1 if down else 0, 0)
        self.x11.XFlush(self.dpy)

    def xkey(self, keycode, down):
        self.xt.XTestFakeKeyEvent(self.dpy, keycode, 1 if down else 0, 0)
        self.x11.XFlush(self.dpy)

    def scroll(self, lines_y, lines_x):
        for count, pos, neg in ((lines_y, 4, 5), (lines_x, 7, 6)):
            b = pos if count > 0 else neg
            for _ in range(min(abs(int(count)), 30)):
                self.xt.XTestFakeButtonEvent(self.dpy, b, 1, 0)
                self.xt.XTestFakeButtonEvent(self.dpy, b, 0, 0)
        self.x11.XFlush(self.dpy)


def os_environ_wayland():
    import os
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" and not os.environ.get("DISPLAY")


class LinuxInputClient(MacInputClient):
    """Same behaviour as the Mac client; keys go straight to X11 (Ctrl stays Ctrl)."""

    def __init__(self, cfg, on_change=None, backend=None):
        super().__init__(cfg, on_change, quartz=backend or _XTest())

    def _wheel(self, v, h):
        sign = -1 if self.cfg.get("invert_scroll") else 1
        self.acc[2] += v / 120.0 * sign          # one notch = one X wheel click
        self.acc[3] += h / 120.0 * sign
        ly, lx = int(self.acc[2]), int(self.acc[3])
        self.acc[2] -= ly
        self.acc[3] -= lx
        if ly or lx:
            self.q.scroll(ly, lx)

    def _key(self, scan, ext, down):
        code = x11_keycode(scan, ext)
        if code is None:
            return
        if down:
            self.keys.add(code)
        else:
            self.keys.discard(code)
        self.q.xkey(code, down)

    def release_all(self):
        try:
            for code in list(self.keys):
                self.keys.discard(code)
                self.q.xkey(code, False)
            if self.buttons:
                x, y = self.q.cursor()
                for b in list(self.buttons):
                    self.buttons.discard(b)
                    self.q.mouse(EV_LUP, x, y, {1: 0, 2: 1}.get(b, b - 1))
        except Exception:
            log.exception("release_all")
