#!/usr/bin/env python3
"""
NelyConnect - drag files to the edge of the screen to send them to the other computer.

Runs on both the Windows PC and the Mac: one keyboard & mouse for both (nelyinput.py),
drag-to-edge file transfer, and shared copy & paste. Tries the peer's addresses in order
(Ethernet first, then Wi-Fi, then anything else you list) and uses the first that answers.

Settings live in nelyconnect_config.json next to this file (created on first run).
"""
import hmac
import json
import logging
import os
import platform
import queue
import socket
import subprocess
import tempfile
import sys
import threading
import time

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)          # bundled resources (installer build)
ASSETS = os.path.join(RES_DIR, "assets")


def _user_data_dir():
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.path.expanduser("~/.config")
    d = os.path.join(base, "NelyConnect")
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = _user_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "nelyconnect_config.json")
LOG_PATH = os.path.join(DATA_DIR, "nelyconnect.log")

CHUNK = 1024 * 1024          # 1 MB
MAX_CLIP = 5 * 1024 * 1024    # largest clipboard text we sync
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
BAD_CHARS = '<>:"|?*\\'      # not allowed in Windows file names

logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nelyconnect")


# --------------------------------------------------------------------------- config

def default_config():
    home = os.path.expanduser("~")
    # Priority order: fixed Ethernet IP, automatic Ethernet IP, Wi-Fi IP, Wi-Fi name.
    if IS_WIN:
        peer_name, edge = "iMac", "right"
        addrs = [{"address": "10.77.0.2", "label": "Ethernet"},
                 {"address": "169.254.93.113", "label": "Ethernet"},
                 {"address": "10.0.10.125", "label": "Wi-Fi"},
                 {"address": "iMac-de-Nelson.local", "label": "Wi-Fi"}]
    else:
        peer_name, edge = "PC", "left"
        addrs = [{"address": "10.77.0.1", "label": "Ethernet"},
                 {"address": "169.254.18.27", "label": "Ethernet"},
                 {"address": "10.0.10.156", "label": "Wi-Fi"}]
    return {
        "this_computer_name": platform.node().split(".")[0] or "This computer",
        "peer_name": peer_name,
        "peer_addresses": addrs,
        "edge": edge,
        "receive_folder": os.path.join(home, "Desktop", "NelyConnect Inbox"),
        "port": 24860,
        "open_folder_on_receive": True,
        "share_input": True,
        "swap_ctrl_cmd": True,
        "mouse_speed": 1.0,
        "scroll_lines": 3,
        "invert_scroll": False,
        "keyboard_iso": True,
        "share_clipboard": True,
        "share_files": False,
        "share_password": "",
        "shares": [],
        "phone_enabled": False,
        "phone_port": 24880,
        "phone_token": "",
        "strip_width": 2,
        "strip_alpha": 0.35,
        "strip_color": "#3b82f6",
    }


def write_config(cfg):
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config():
    cfg = default_config()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:  # keep defaults, but say why
            log.error("Could not read config: %s", e)
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        cfg["_first_run"] = True
    cfg.pop("launch_deskflow", None)      # Deskflow is no longer needed
    # remove duplicates from the address list (keep the first = higher priority)
    seen, cleaned = set(), []
    for a in cfg.get("peer_addresses", []):
        key = a["address"] if isinstance(a, dict) else a
        if key not in seen:
            seen.add(key)
            cleaned.append(a)
    cfg["peer_addresses"] = cleaned
    cfg["receive_folder"] = os.path.expanduser(cfg["receive_folder"])
    # allow plain strings in peer_addresses too
    norm = []
    for i, a in enumerate(cfg["peer_addresses"]):
        if isinstance(a, str):
            a = {"address": a, "label": "Link %d" % (i + 1)}
        norm.append(a)
    cfg["peer_addresses"] = norm
    return cfg


# --------------------------------------------------------------------------- protocol

def send_json(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def read_json(rfile):
    line = rfile.readline(MAX_CLIP * 2)
    if not line:
        raise ConnectionError("connection closed")
    return json.loads(line.decode("utf-8"))


def ping(address, port, timeout=0.8):
    """Return the peer's name if NelyConnect answers at address, else None."""
    try:
        with socket.create_connection((address, port), timeout=timeout) as s:
            s.settimeout(timeout + 0.5)
            send_json(s, {"type": "ping"})
            reply = read_json(s.makefile("rb"))
            return reply.get("name") if reply.get("ok") else None
    except Exception:
        return None


def send_clip(address, port, text, sender_name):
    with socket.create_connection((address, port), timeout=2) as s:
        s.settimeout(5)
        send_json(s, {"type": "clip", "from": sender_name, "text": text})
        reply = read_json(s.makefile("rb"))
        return reply.get("ok")


def collect(paths):
    """Expand dropped files/folders into (kind, full_path, rel_path, size)."""
    items = []
    for p in paths:
        p = os.path.abspath(p.rstrip("\\/") or p)
        parent = os.path.dirname(p)
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                rel_dir = os.path.relpath(dirpath, parent).replace(os.sep, "/")
                items.append(("dir", dirpath, rel_dir, 0))
                for name in sorted(filenames):
                    if name in SKIP_FILES:
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    items.append(("file", full, rel_dir + "/" + name, size))
        elif os.path.isfile(p):
            items.append(("file", p, os.path.basename(p), os.path.getsize(p)))
    return items


def stream_items(sock, items, progress=None):
    """Write dir/file records (and file bytes) followed by an end marker."""
    total = sum(i[3] for i in items)
    done = 0
    for kind, full, rel, size in items:
        if kind == "dir":
            send_json(sock, {"type": "dir", "rel": rel})
            continue
        send_json(sock, {"type": "file", "rel": rel, "size": size})
        remaining = size
        with open(full, "rb") as fh:
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    raise IOError("File changed while sending: " + rel)
                sock.sendall(chunk)
                remaining -= len(chunk)
                done += len(chunk)
                if progress:
                    progress(rel, done, total)
        if progress and size == 0:
            progress(rel, done, total)
    send_json(sock, {"type": "end"})
    return total


def receive_items(rfile, dest, total=0, progress=None):
    """Read records written by stream_items into dest. Existing names get ' (2)' etc."""
    os.makedirs(dest, exist_ok=True)
    top_map = {}
    done = 0
    last = 0.0
    first_path = None

    def local_path(rel):
        parts = [clean_component(p) for p in rel.split("/") if p not in ("", ".", "..")]
        if not parts:
            raise ValueError("Bad name")
        top = parts[0]
        if top not in top_map:
            top_map[top] = unique_name(dest, top)
        parts[0] = top_map[top]
        return os.path.join(dest, *parts)

    while True:
        msg = read_json(rfile)
        kind = msg.get("type")
        if kind == "end":
            break
        if kind == "error":
            raise IOError(msg.get("error", "Transfer failed"))
        path = local_path(msg["rel"])
        first_path = first_path or path
        if kind == "dir":
            os.makedirs(path, exist_ok=True)
            continue
        if kind != "file":
            raise ValueError("Unexpected message")
        size = int(msg["size"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".nelyconnect-part"
        remaining = size
        try:
            with open(tmp, "wb") as out:
                while remaining > 0:
                    chunk = rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ConnectionError("Connection lost during transfer")
                    out.write(chunk)
                    remaining -= len(chunk)
                    done += len(chunk)
                    now = time.time()
                    if progress and now - last > 0.1:
                        last = now
                        progress(os.path.basename(path), done, total)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    if progress:
        progress("", done, total)
    return first_path


def send_paths(address, port, paths, sender_name, progress=None):
    """Send files/folders to the other computer's inbox. progress(rel, done, total)."""
    items = collect(paths)
    if not items:
        raise ValueError("Nothing to send")
    total = sum(i[3] for i in items)
    with socket.create_connection((address, port), timeout=5) as s:
        s.settimeout(60)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * CHUNK)
        rfile = s.makefile("rb")
        send_json(s, {"type": "batch", "from": sender_name,
                      "count": len(items), "total": total})
        stream_items(s, items, progress)
        reply = read_json(rfile)
        if not reply.get("ok"):
            raise IOError(reply.get("error", "Receiver reported an error"))
    return total


# --------------------------------------------------------------------------- shared folders (client side)

def remote_call(address, port, request, timeout=10):
    """One request -> one JSON reply (shares, list, mkdir)."""
    with socket.create_connection((address, port), timeout=5) as s:
        s.settimeout(timeout)
        send_json(s, request)
        reply = read_json(s.makefile("rb"))
    if not reply.get("ok"):
        raise IOError(reply.get("error", "The other computer refused the request"))
    return reply


def remote_download(address, port, request, dest, progress=None):
    """request = {"type": "get", "share", "paths", "pw"}; files land in dest."""
    with socket.create_connection((address, port), timeout=5) as s:
        s.settimeout(60)
        send_json(s, request)
        rfile = s.makefile("rb")
        hdr = read_json(rfile)
        if not hdr.get("ok"):
            raise IOError(hdr.get("error", "Download refused"))
        return receive_items(rfile, dest, int(hdr.get("total", 0)), progress), int(hdr.get("total", 0))


def remote_upload(address, port, request, local_paths, progress=None):
    """request = {"type": "put", "share", "path", "pw"}; uploads into that remote folder."""
    items = collect(local_paths)
    if not items:
        raise ValueError("Nothing to upload")
    total = sum(i[3] for i in items)
    with socket.create_connection((address, port), timeout=5) as s:
        s.settimeout(60)
        rfile = s.makefile("rb")
        send_json(s, dict(request, total=total, count=len(items)))
        ready = read_json(rfile)
        if not ready.get("ok"):
            raise IOError(ready.get("error", "Upload refused"))
        stream_items(s, items, progress)
        reply = read_json(rfile)
        if not reply.get("ok"):
            raise IOError(reply.get("error", "Upload failed"))
    return total


def clean_component(part):
    """One safe file/folder name: no slashes, no '..', no characters Windows forbids."""
    part = "".join("_" if c in BAD_CHARS or c == "/" or ord(c) < 32 else c for c in part)
    part = part.strip().rstrip(".")
    return part if part and part not in (".", "..") else "_"


def unique_name(folder, name):
    candidate = name
    stem, ext = os.path.splitext(name)
    n = 2
    while os.path.exists(os.path.join(folder, candidate)):
        candidate = "%s (%d)%s" % (stem, n, ext)
        n += 1
    return candidate


class Receiver(threading.Thread):
    """TCP server that answers pings and receives batches."""

    def __init__(self, cfg, events):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.events = events
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if not IS_WIN:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", int(cfg["port"])))   # raises if already running
        self.sock.listen(8)

    def run(self):
        while True:
            conn, addr = self.sock.accept()
            threading.Thread(target=self.handle, args=(conn, addr), daemon=True).start()

    def handle(self, conn, addr):
        tid = "rx-%s-%f" % (addr[0], time.time())
        try:
            conn.settimeout(60)
            rfile = conn.makefile("rb")
            try:
                hdr = read_json(rfile)
            except (ConnectionError, OSError, ValueError):
                return          # connection opened and dropped before saying anything - ignore
            if hdr.get("type") == "ping":
                send_json(conn, {"ok": True, "name": self.cfg["this_computer_name"]})
                return
            if hdr.get("type") == "clip":
                if self.cfg.get("share_clipboard"):
                    self.events.put(("clip", None, str(hdr.get("text", ""))[:MAX_CLIP]))
                send_json(conn, {"ok": True})
                return
            if hdr.get("type") in ("shares", "list", "get", "put", "mkdir"):
                conn.settimeout(300)
                return self.handle_share_request(conn, rfile, hdr, addr[0], tid)
            if hdr.get("type") != "batch":
                return
            self.receive_batch(conn, rfile, hdr, tid, addr[0])
        except Exception as e:
            log.exception("Receive failed")
            self.events.put(("error", tid, "Receive failed: %s" % e))
            try:
                send_json(conn, {"ok": False, "error": str(e)})
            except Exception:
                pass
        finally:
            conn.close()

    def receive_batch(self, conn, rfile, hdr, tid, peer_ip):
        dest = self.cfg["receive_folder"]
        total = int(hdr.get("total", 0))
        sender = hdr.get("from") or self.cfg["peer_name"]
        self.events.put(("start", tid, "Receiving from %s" % sender, total))

        def prog(name, done, tot):
            if name:
                self.events.put(("progress", tid, name, done, tot))

        receive_items(rfile, dest, total, prog)
        send_json(conn, {"ok": True})
        self.events.put(("done", tid, "Saved to %s" % os.path.basename(dest), total))
        log.info("Received %d bytes from %s (%s)", total, sender, peer_ip)
        if self.cfg.get("open_folder_on_receive"):
            open_folder(dest)

    # ---- shared folders (server side)
    _ip_cache = {"t": 0.0, "ips": set()}

    def _peer_ips(self):
        c = self._ip_cache
        if time.time() - c["t"] > 60:
            ips = set()
            for a in self.cfg["peer_addresses"]:
                try:
                    ips.update(i[4][0] for i in socket.getaddrinfo(a["address"], None, socket.AF_INET))
                except OSError:
                    pass
            c["ips"], c["t"] = ips, time.time()
        return c["ips"]

    def check_access(self, req, ip):
        if not self.cfg.get("share_files"):
            raise PermissionError("Shared folders are turned off on the %s" % self.cfg["this_computer_name"])
        if not (ip.startswith("127.") or ip in self._peer_ips()):
            self._ip_cache["t"] = 0           # re-resolve next time
            if ip not in self._peer_ips():
                raise PermissionError("This computer (%s) is not in the %s's address list"
                                      % (ip, self.cfg["this_computer_name"]))
        pw = self.cfg.get("share_password") or ""
        if pw and not hmac.compare_digest(str(req.get("pw", "")), pw):
            raise PermissionError("Wrong sharing password")

    def find_share(self, name):
        for sh in self.cfg.get("shares", []):
            if sh.get("name") == name:
                return sh
        raise FileNotFoundError("Shared item '%s' not found" % name)

    def resolve(self, share, rel):
        root = os.path.realpath(share["path"])
        parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
        if ".." in parts:
            raise PermissionError("Bad path")
        full = os.path.realpath(os.path.join(root, *parts)) if parts else root
        if full != root and os.path.commonpath([root, full]) != root:
            raise PermissionError("Outside the shared folder")
        return full

    def list_entries(self, share, full):
        """Visible items of a shared folder (hides dot-files and links leading outside the share)."""
        entries = []
        names = [os.path.basename(full)] if os.path.isfile(full) else sorted(
            os.listdir(full), key=str.lower)
        base = os.path.dirname(full) if os.path.isfile(full) else full
        for n in names:
            if n.startswith(".") or n in SKIP_FILES or n.endswith(".nelyconnect-part"):
                continue
            fp = os.path.join(base, n)
            real = os.path.realpath(fp)
            root = os.path.realpath(share["path"])
            if real != root and os.path.commonpath([root, real]) != root:
                continue                       # link pointing outside the share
            try:
                st = os.stat(fp)
            except OSError:
                continue
            isdir = os.path.isdir(fp)
            entries.append({"n": n, "d": isdir, "s": 0 if isdir else st.st_size, "m": int(st.st_mtime)})
        return entries, bool(share.get("writable")) and os.path.isdir(full)

    def handle_share_request(self, conn, rfile, req, ip, tid):
        kind = req["type"]
        try:
            self.check_access(req, ip)
            if kind == "shares":
                out = []
                for sh in self.cfg.get("shares", []):
                    p = sh.get("path", "")
                    out.append({"name": sh["name"], "writable": bool(sh.get("writable")),
                                "is_file": os.path.isfile(p), "exists": os.path.exists(p)})
                return send_json(conn, {"ok": True, "shares": out,
                                        "computer": self.cfg["this_computer_name"]})
            share = self.find_share(req.get("share"))
            if kind == "list":
                full = self.resolve(share, req.get("path"))
                entries, writable = self.list_entries(share, full)
                return send_json(conn, {"ok": True, "entries": entries, "writable": writable})
            if kind == "mkdir":
                if not share.get("writable"):
                    raise PermissionError("'%s' is read-only" % share["name"])
                parent = self.resolve(share, req.get("path"))
                name = clean_component(str(req.get("name", "")))
                os.makedirs(os.path.join(parent, unique_name(parent, name)))
                return send_json(conn, {"ok": True})
            if kind == "get":
                if os.path.isfile(os.path.realpath(share["path"])):
                    paths = [os.path.realpath(share["path"])]
                else:
                    paths = [self.resolve(share, p) for p in req.get("paths", [])]
                root = os.path.realpath(share["path"])
                items = [i for i in collect(paths)            # never follow links out of the share
                         if os.path.realpath(i[1]) == root or
                         os.path.commonpath([root, os.path.realpath(i[1])]) == root]
                total = sum(i[3] for i in items)
                send_json(conn, {"ok": True, "total": total, "count": len(items)})
                stream_items(conn, items)
                log.info("Shared %d bytes from '%s' to %s", total, share["name"], ip)
                return
            if kind == "put":
                if not share.get("writable"):
                    raise PermissionError("'%s' is read-only" % share["name"])
                dest = self.resolve(share, req.get("path"))
                if not os.path.isdir(dest):
                    raise NotADirectoryError("Not a folder")
                send_json(conn, {"ok": True})           # ready
                total = int(req.get("total", 0))
                sender = req.get("from") or self.cfg["peer_name"]
                self.events.put(("start", tid, "%s is copying into %s" % (sender, share["name"]), total))
                receive_items(rfile, dest, total,
                              lambda n, d, t: n and self.events.put(("progress", tid, n, d, t)))
                send_json(conn, {"ok": True})
                self.events.put(("done", tid, "Copied into %s" % share["name"], total))
                return
            raise ValueError("Unknown request")
        except (PermissionError, FileNotFoundError, NotADirectoryError, ValueError, OSError) as e:
            log.info("Share request %s from %s refused: %s", kind, ip, e)
            send_json(conn, {"ok": False, "error": str(e)})


# --------------------------------------------------------------------------- link selection

class LinkMonitor(threading.Thread):
    """Checks the peer addresses in priority order and remembers the first that answers."""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.active = None          # dict(address,label) or None
        self.lock = threading.Lock()

    def check_once(self):
        entries = self.cfg["peer_addresses"]
        ok = [False] * len(entries)

        def probe(i, entry):
            name = ping(entry["address"], int(self.cfg["port"]))
            # an address of THIS computer answers too - never treat that as the other computer
            ok[i] = bool(name) and name != self.cfg["this_computer_name"]

        threads = [threading.Thread(target=probe, args=(i, e), daemon=True)
                   for i, e in enumerate(entries)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)
        found = next((e for e, good in zip(entries, ok) if good), None)
        with self.lock:
            if (found or {}).get("address") != (self.active or {}).get("address"):
                log.info("Active link: %s", found)
            self.active = found
        return found

    def run(self):
        while True:
            self.check_once()
            time.sleep(3)

    def get(self):
        with self.lock:
            return self.active


# --------------------------------------------------------------------------- helpers

def open_folder(path):
    try:
        if IS_WIN:
            os.startfile(path)  # noqa
        elif IS_MAC:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        log.exception("Could not open folder")


def open_file(path):
    if IS_MAC:
        subprocess.Popen(["open", "-t", path])     # open in TextEdit
    elif IS_WIN:
        subprocess.Popen(["notepad.exe", path])
    else:
        open_folder(path)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0


# --------------------------------------------------------------------------- UI

def run_ui(cfg):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
    except ImportError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("NelyConnect", "Missing component 'tkinterdnd2'.\n\n"
                             "Run the installer script first (see README).")
        return

    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    events = queue.Queue()
    try:
        receiver = Receiver(cfg, events)
    except OSError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("NelyConnect", "NelyConnect is already running.")
        return
    receiver.start()
    link = LinkMonitor(cfg)
    link.start()

    # phone access (web page for iPhone / Android)
    phone = None
    try:
        import types
        import nelyphone
        phone = nelyphone.PhoneServer(
            cfg, receiver, link, events,
            types.SimpleNamespace(clean_component=clean_component, unique_name=unique_name,
                                  send_paths=send_paths),
            ASSETS, lambda: write_config(cfg))
        phone.start()
    except Exception:
        log.exception("Phone access could not start")
        phone = None

    # keyboard & mouse sharing
    import nelyinput
    input_sys = None
    try:
        if IS_WIN:
            input_sys = nelyinput.WinInputServer(cfg, link, on_change=lambda: events.put(("input", None)))
        elif IS_MAC:
            input_sys = nelyinput.MacInputClient(cfg, on_change=lambda: events.put(("input", None)))
        elif IS_LINUX:
            input_sys = nelyinput.LinuxInputClient(cfg, on_change=lambda: events.put(("input", None)))
        if input_sys:
            input_sys.start()
    except Exception:
        log.exception("Keyboard & mouse sharing could not start")
        input_sys = None

    dnd = {"ok": True}
    try:
        root = TkinterDnD.Tk()
    except Exception as e:
        # e.g. Intel Mac + Python built with Tcl/Tk 9: the drag & drop add-on can't load.
        # Keep running without the edge drop strip instead of crashing.
        log.error("Drag & drop unavailable (%s) - running without the edge drop strip", e)
        dnd["ok"] = False
        try:
            if tk._default_root is not None:
                tk._default_root.destroy()
        except Exception:
            pass
        tk._default_root = None
        root = tk.Tk()
    root.title("NelyConnect")
    root.report_callback_exception = lambda t, v, tb: log.error("UI error", exc_info=(t, v, tb))
    images = {}
    try:
        images["icon"] = tk.PhotoImage(file=os.path.join(ASSETS, "icon_64.png"))
        images["logo"] = tk.PhotoImage(file=os.path.join(ASSETS, "logo_small.png"))
        root.iconphoto(True, images["icon"])
    except Exception:
        log.warning("Logo files not found in %s", ASSETS)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    color = cfg["strip_color"]
    root.configure(bg=color)
    label = tk.Label(root, text="", bg=color, fg="white",
                     font=("Segoe UI" if IS_WIN else "Helvetica", 14, "bold"),
                     justify="center", wraplength=160)

    ui = {"edge": cfg["edge"].lower()}
    thin = max(1, int(cfg["strip_width"]))
    wide = 190

    def bounds():
        if IS_WIN:
            import ctypes
            u = ctypes.windll.user32
            return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                    u.GetSystemMetrics(78), u.GetSystemMetrics(79))
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()

    def place(width):
        vx, vy, vw, vh = bounds()
        edge = ui["edge"]
        if edge == "left":
            g = (width, vh, vx, vy)
        elif edge == "top":
            g = (vw, width, vx, vy)
        elif edge == "bottom":
            g = (vw, width, vx, vy + vh - width)
        else:  # right
            g = (width, vh, vx + vw - width, vy)
        root.geometry("%dx%d+%d+%d" % g)

    def collapse():
        label.place_forget()
        root.attributes("-alpha", float(cfg["strip_alpha"]))
        place(thin)

    def expand():
        active = link.get()
        if active:
            text = "Drop to send to\n%s\n\nvia %s" % (cfg["peer_name"], active["label"])
            bg = color
        else:
            text = "%s is not reachable" % cfg["peer_name"]
            bg = "#b91c1c"
        root.configure(bg=bg)
        label.configure(text=text, bg=bg)
        root.attributes("-alpha", 0.92)
        place(wide)
        label.place(relx=0.5, rely=0.5, anchor="center")

    # ---- progress windows
    windows = {}

    def progress_window(tid, title, total):
        w = tk.Toplevel(root)
        w.title("NelyConnect")
        w.attributes("-topmost", True)
        w.resizable(False, False)
        frm = ttk.Frame(w, padding=14)
        frm.pack(fill="both", expand=True)
        t = ttk.Label(frm, text=title, font=("Segoe UI" if IS_WIN else "Helvetica", 12, "bold"))
        t.pack(anchor="w")
        name = ttk.Label(frm, text="", width=46)
        name.pack(anchor="w", pady=(4, 6))
        bar = ttk.Progressbar(frm, length=340, mode="determinate", maximum=max(total, 1))
        bar.pack(fill="x")
        stat = ttk.Label(frm, text="")
        stat.pack(anchor="w", pady=(6, 0))
        w.update_idletasks()
        vx, vy, vw, vh = bounds()
        ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
        x = vx + vw - ww - 230 if ui["edge"] == "right" else vx + 230
        w.geometry("+%d+%d" % (x, vy + vh // 2 - wh // 2))
        windows[tid] = dict(win=w, title=t, name=name, bar=bar, stat=stat,
                            total=total, start=time.time())
        return windows[tid]

    def finish(tid, text, ok=True, total=None):
        info = windows.get(tid)
        if not info:
            info = progress_window(tid, "NelyConnect", 1)
        info["title"].configure(text=("✓ " if ok else "✗ ") + text)
        if ok:
            info["bar"].configure(value=info["bar"]["maximum"])
            if total:
                el = max(time.time() - info["start"], 0.001)
                info["stat"].configure(text="%s   ·   %s/s" % (human(total), human(total / el)))
        root.after(2500 if ok else 8000, lambda: close(tid))

    def close(tid):
        info = windows.pop(tid, None)
        if info:
            info["win"].destroy()

    def pump():
        try:
            while True:
                ev = events.get_nowait()
                kind, tid = ev[0], ev[1]
                if kind == "input":
                    pass                      # status window refreshes itself
                elif kind == "open_folder":
                    open_folder(ev[2])
                elif kind == "clip":
                    set_clipboard(ev[2])
                elif kind == "call":
                    try:
                        ev[2]()
                    except tk.TclError:
                        pass
                elif kind == "start":
                    progress_window(tid, ev[2], ev[3])
                elif kind == "progress":
                    info = windows.get(tid)
                    if info:
                        _, _, fname, done, total = ev
                        el = max(time.time() - info["start"], 0.001)
                        info["name"].configure(text=fname)
                        info["bar"].configure(maximum=max(total, 1), value=done)
                        info["stat"].configure(text="%s of %s   ·   %s/s" % (
                            human(done), human(total), human(done / el)))
                elif kind == "done":
                    finish(tid, ev[2], True, ev[3] if len(ev) > 3 else None)
                elif kind == "error":
                    finish(tid, ev[2], False)
        except queue.Empty:
            pass
        root.after(80, pump)

    # ---- sending
    def send_worker(paths):
        tid = "tx-%f" % time.time()
        active = link.get() or link.check_once()
        if not active:
            events.put(("error", tid, "%s is not reachable" % cfg["peer_name"]))
            return
        total = sum(i[3] for i in collect(paths))
        events.put(("start", tid, "Sending to %s (via %s)" % (cfg["peer_name"], active["label"]), total))
        last = [0.0]

        def prog(rel, done, tot):
            now = time.time()
            if now - last[0] > 0.1 or done >= tot:
                last[0] = now
                events.put(("progress", tid, rel.split("/")[-1], done, tot))

        # try the active link first, then the others in priority order
        order = [active] + [a for a in cfg["peer_addresses"] if a is not active]
        err = None
        for entry in order:
            try:
                send_paths(entry["address"], int(cfg["port"]), paths,
                           cfg["this_computer_name"], prog)
                events.put(("done", tid, "Sent to %s" % cfg["peer_name"], total))
                log.info("Sent %d bytes via %s", total, entry)
                return
            except Exception as e:
                err = e
                log.exception("Send via %s failed", entry)
        events.put(("error", tid, "Send failed: %s" % err))

    # ---- drag & drop bindings
    def on_enter(event):
        expand()
        return event.action

    def on_leave(event):
        collapse()
        return event.action

    def on_drop(event):
        paths = list(root.tk.splitlist(event.data))
        collapse()
        threading.Thread(target=send_worker, args=(paths,), daemon=True).start()
        return event.action

    if dnd["ok"]:
        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<DropEnter>>", on_enter)
        root.dnd_bind("<<DropLeave>>", on_leave)
        root.dnd_bind("<<Drop>>", on_drop)



    # ---- shared clipboard (text)
    clip = {"last": None}
    clip_var = tk.BooleanVar(value=bool(cfg.get("share_clipboard")))

    def read_clipboard():
        try:
            return root.clipboard_get()
        except tk.TclError:
            return None          # empty or not text (image, files...)

    def set_clipboard(text):
        clip["last"] = text      # so we don't send it straight back
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update_idletasks()
        except tk.TclError:
            log.exception("Could not set clipboard")

    def watch_clipboard():
        if cfg.get("share_clipboard"):
            text = read_clipboard()
            if text is not None and text != clip["last"]:
                clip["last"] = text
                if len(text) <= MAX_CLIP:
                    def push(t=text):
                        a = link.get()
                        if a:
                            try:
                                send_clip(a["address"], int(cfg["port"]), t, cfg["this_computer_name"])
                            except Exception as e:
                                log.info("Clipboard send failed: %s", e)
                    threading.Thread(target=push, daemon=True).start()
        root.after(500, watch_clipboard)

    input_var = tk.BooleanVar(value=bool(cfg.get("share_input", True)))

    def toggle_input():
        cfg["share_input"] = input_var.get()
        save_config()
        if IS_MAC and input_var.get():
            check_accessibility()

    def check_accessibility():
        if IS_MAC and input_sys and cfg.get("share_input") and not input_sys.trusted():
            if messagebox.askyesno(
                    "NelyConnect",
                    "To let the %s's keyboard & mouse control this Mac, turn on NelyConnect in\n"
                    "System Settings → Privacy & Security → Accessibility.\n\n"
                    "Open that page now?" % cfg["peer_name"]):
                subprocess.Popen(["open", "x-apple.systempreferences:"
                                  "com.apple.preference.security?Privacy_Accessibility"])

    def toggle_clipboard():
        cfg["share_clipboard"] = clip_var.get()
        clip["last"] = read_clipboard()   # don't send whatever is already there
        save_config()

    # ---- settings window
    def local_ipv4s():
        ips = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.append(info[4][0])
        except Exception:
            pass
        if IS_LINUX:
            try:
                out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
                ips += [ln.split()[3].split("/")[0] for ln in out.splitlines() if " inet " in ln]
            except Exception:
                pass
        if IS_MAC:
            try:
                out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True).stdout
                ips += [ln.split()[1] for ln in out.splitlines() if ln.strip().startswith("inet ")]
            except Exception:
                pass
        seen = []
        for ip in ips:
            if not ip.startswith("127.") and ip not in seen:
                seen.append(ip)
        return seen

    def save_config():
        data = {k: v for k, v in cfg.items() if not k.startswith("_")}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    open_dialogs = {}

    def open_settings():
        old = open_dialogs.get("settings")
        if old is not None and old.winfo_exists():
            old.lift()
            return
        w = tk.Toplevel(root)
        open_dialogs["settings"] = w
        w.title("NelyConnect Settings")
        w.attributes("-topmost", True)
        w.resizable(False, False)
        bold = ("Segoe UI" if IS_WIN else "Helvetica", 11, "bold")
        outer = ttk.Frame(w, padding=12)
        outer.pack(fill="both", expand=True)
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        tc, tf, ti, ts = (ttk.Frame(nb, padding=14) for _ in range(4))
        nb.add(tc, text="Connection")
        nb.add(tf, text="Files & clipboard")
        nb.add(ti, text="Keyboard & mouse")
        nb.add(ts, text="Shared folders")

        ttk.Label(tc, text="Other computer", font=bold).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(tc, text="Name:").grid(row=1, column=0, sticky="w", pady=4)
        name_var = tk.StringVar(value=cfg["peer_name"])
        ttk.Entry(tc, textvariable=name_var, width=30).grid(row=1, column=1, columnspan=2, sticky="w")

        ttk.Label(tc, text="Its IP addresses (tried from top to bottom — first one that answers is used):"
                  ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 4))
        tree = ttk.Treeview(tc, columns=("address", "label", "status"), show="headings", height=7)
        for col, title, width in (("address", "IP address / name", 210),
                                  ("label", "Connection", 110), ("status", "Status", 120)):
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor="w")
        tree.grid(row=3, column=0, columnspan=2, sticky="nsew")
        for a in cfg["peer_addresses"]:
            tree.insert("", "end", values=(a["address"], a.get("label", ""), ""))

        def entry_dialog(title, address="", label="Ethernet"):
            d = tk.Toplevel(w)
            d.title(title)
            d.attributes("-topmost", True)
            d.transient(w)
            d.resizable(False, False)
            f = ttk.Frame(d, padding=14)
            f.pack()
            ttk.Label(f, text="IP address or computer name:").grid(row=0, column=0, sticky="w")
            av = tk.StringVar(value=address)
            e = ttk.Entry(f, textvariable=av, width=32)
            e.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 8))
            ttk.Label(f, text="Connection type:").grid(row=2, column=0, sticky="w")
            lv = tk.StringVar(value=label)
            ttk.Combobox(f, textvariable=lv, width=29,
                         values=["Ethernet", "Wi-Fi", "Bluetooth", "Tailscale", "Other"]
                         ).grid(row=3, column=0, columnspan=2, sticky="we", pady=(2, 10))
            result = {}

            def ok(*_):
                if av.get().strip():
                    result["v"] = (av.get().strip(), lv.get().strip() or "Other")
                    d.destroy()

            ttk.Button(f, text="OK", command=ok).grid(row=4, column=0, sticky="e", padx=4)
            ttk.Button(f, text="Cancel", command=d.destroy).grid(row=4, column=1, sticky="w")
            d.bind("<Return>", ok)
            e.focus_set()
            d.grab_set()
            w.wait_window(d)
            return result.get("v")

        def add():
            v = entry_dialog("Add address")
            if v:
                tree.insert("", "end", values=(v[0], v[1], ""))

        def edit(*_):
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], "values")
            v = entry_dialog("Edit address", vals[0], vals[1])
            if v:
                tree.item(sel[0], values=(v[0], v[1], ""))

        def remove():
            for i in tree.selection():
                tree.delete(i)

        def move(delta):
            for i in tree.selection():
                tree.move(i, "", max(0, tree.index(i) + delta))

        def test_all():
            port = int(cfg["port"])
            for item in tree.get_children():
                tree.set(item, "status", "testing…")

                def run(item=item, addr=tree.set(item, "address")):
                    t0 = time.time()
                    name = ping(addr, port, timeout=1.5)
                    ms = int((time.time() - t0) * 1000)
                    if name and name == cfg["this_computer_name"]:
                        text = "✗ this computer!"
                    else:
                        text = ("✓ online (%d ms)" % ms) if name else "✗ no answer"
                    events.put(("call", None, lambda: tree.exists(item) and tree.set(item, "status", text)))

                threading.Thread(target=run, daemon=True).start()

        tree.bind("<Double-1>", edit)
        side = ttk.Frame(tc)
        side.grid(row=3, column=2, sticky="n", padx=(8, 0))
        for text, cmd in (("Add…", add), ("Edit…", edit), ("Remove", remove),
                          ("Move up", lambda: move(-1)), ("Move down", lambda: move(1)),
                          ("Test all", test_all)):
            ttk.Button(side, text=text, command=cmd, width=11).pack(pady=2)

        mine = ", ".join(local_ipv4s()) or "unknown"
        ttk.Label(tc, text="This computer's addresses (enter these on the other computer):\n" + mine,
                  foreground="#555", wraplength=520, justify="left"
                  ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        ttk.Label(tf, text="Send edge:").grid(row=0, column=0, sticky="w", pady=4)
        edge_var = tk.StringVar(value=ui["edge"])
        ttk.Combobox(tf, textvariable=edge_var, state="readonly", width=10,
                     values=["right", "left", "top", "bottom"]).grid(row=0, column=1, sticky="w")
        ttk.Label(tf, text="Save received files in:").grid(row=1, column=0, sticky="w", pady=4)
        folder_var = tk.StringVar(value=cfg["receive_folder"])
        ttk.Entry(tf, textvariable=folder_var, width=44).grid(row=1, column=1, sticky="w")

        def browse():
            d = filedialog.askdirectory(parent=w, initialdir=folder_var.get() or os.path.expanduser("~"))
            if d:
                folder_var.set(d)

        ttk.Button(tf, text="Browse…", command=browse, width=11).grid(row=1, column=2, padx=(8, 0))
        open_var = tk.BooleanVar(value=bool(cfg.get("open_folder_on_receive")))
        ttk.Checkbutton(tf, text="Open the folder when files arrive", variable=open_var
                        ).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(tf, text="Share copy & paste (text) between the computers", variable=clip_var
                        ).grid(row=3, column=0, columnspan=3, sticky="w")

        share_text = ("Use this PC's keyboard & mouse on the %s too" if IS_WIN
                      else "Let the %s's keyboard & mouse control this Mac" if IS_MAC
                      else "Let the %s's keyboard & mouse control this computer (X11 desktop)")
        ttk.Checkbutton(ti, text=share_text % cfg["peer_name"], variable=input_var
                        ).grid(row=0, column=0, columnspan=3, sticky="w")
        mac_opts = {}
        if IS_MAC:
            mac_opts["swap"] = tk.BooleanVar(value=bool(cfg.get("swap_ctrl_cmd", True)))
            ttk.Checkbutton(ti, text="PC Ctrl key works as ⌘ Command (Ctrl+C = copy)",
                            variable=mac_opts["swap"]).grid(row=1, column=0, columnspan=3, sticky="w")
            mac_opts["invert"] = tk.BooleanVar(value=bool(cfg.get("invert_scroll")))
            ttk.Checkbutton(ti, text="Reverse scroll direction", variable=mac_opts["invert"]
                            ).grid(row=2, column=0, columnspan=3, sticky="w")
            ttk.Label(ti, text="Mouse speed:").grid(row=3, column=0, sticky="w", pady=4)
            mac_opts["speed"] = tk.DoubleVar(value=float(cfg.get("mouse_speed", 1.0)))
            ttk.Scale(ti, from_=0.5, to=3.0, variable=mac_opts["speed"], length=220
                      ).grid(row=3, column=1, sticky="w")
            ttk.Label(ti, text="Keyboard type:").grid(row=4, column=0, sticky="w", pady=4)
            mac_opts["iso"] = tk.StringVar(value="ISO (Spanish / Latin American)"
                                           if cfg.get("keyboard_iso", True) else "ANSI (US)")
            ttk.Combobox(ti, textvariable=mac_opts["iso"], state="readonly", width=30,
                         values=["ISO (Spanish / Latin American)", "ANSI (US)"]
                         ).grid(row=4, column=1, sticky="w")
        else:
            ttk.Label(ti, text="Move the mouse past the %s edge to use the %s. Scroll Lock: come back "
                                "instantly, or lock the mouse to this screen." % (ui["edge"], cfg["peer_name"]),
                      foreground="#555", wraplength=520, justify="left"
                      ).grid(row=1, column=0, columnspan=3, sticky="w")

        # ---- shared folders tab
        share_var = tk.BooleanVar(value=bool(cfg.get("share_files")))
        ttk.Checkbutton(ts, text="Let the %s browse and copy the items below" % cfg["peer_name"],
                        variable=share_var).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(ts, text="Shared disks, folders and files:").grid(row=1, column=0, columnspan=3,
                                                                     sticky="w", pady=(10, 4))
        stree = ttk.Treeview(ts, columns=("name", "path", "access"), show="headings", height=8)
        for col, title, width in (("name", "Shown as", 130), ("path", "On this computer", 270),
                                  ("access", "Access", 100)):
            stree.heading(col, text=title)
            stree.column(col, width=width, anchor="w")
        stree.grid(row=2, column=0, columnspan=2, sticky="nsew")
        for sh in cfg.get("shares", []):
            stree.insert("", "end", values=(sh["name"], sh["path"],
                                            "Read & write" if sh.get("writable") else "Read only"))

        def share_name(path):
            base = os.path.basename(path.rstrip("\\/")) or path.rstrip("\\/:") or path
            if IS_WIN and len(path) <= 3 and path[1:2] == ":":
                base = "Disk %s" % path[0].upper()
            existing = {stree.set(i, "name") for i in stree.get_children()}
            name, n = base, 2
            while name in existing:
                name, n = "%s (%d)" % (base, n), n + 1
            return name

        def add_share(path):
            if path:
                path = os.path.normpath(path)
                stree.insert("", "end", values=(share_name(path), path, "Read only"))

        def add_share_folder():
            add_share(filedialog.askdirectory(parent=w, title="Choose a folder to share"))

        def add_share_file():
            for f in filedialog.askopenfilenames(parent=w, title="Choose files to share"):
                add_share(f)

        def add_share_disk():
            if IS_WIN:
                import string
                disks = ["%s:\\" % c for c in string.ascii_uppercase if os.path.exists("%s:\\" % c)]
            else:
                disks = ["/"] + [os.path.join("/Volumes", v) for v in sorted(os.listdir("/Volumes"))
                                 if os.path.isdir(os.path.join("/Volumes", v))] if os.path.isdir("/Volumes") else ["/"]
            d = tk.Toplevel(w)
            d.title("Share a disk")
            d.attributes("-topmost", True)
            d.transient(w)
            f = ttk.Frame(d, padding=14)
            f.pack()
            ttk.Label(f, text="Choose a disk to share:").pack(anchor="w")
            lb = tk.Listbox(f, height=min(8, max(3, len(disks))), width=36, selectmode="extended")
            for x in disks:
                lb.insert("end", x)
            lb.pack(pady=6)

            def ok():
                for i in lb.curselection():
                    add_share(disks[i])
                d.destroy()

            ttk.Button(f, text="Share", command=ok).pack(side="left", padx=4)
            ttk.Button(f, text="Cancel", command=d.destroy).pack(side="left")
            d.grab_set()

        def remove_share():
            for i in stree.selection():
                stree.delete(i)

        def toggle_access(*_):
            for i in stree.selection():
                cur = stree.set(i, "access")
                stree.set(i, "access", "Read only" if cur == "Read & write" else "Read & write")

        def rename_share():
            sel = stree.selection()
            if not sel:
                return
            from tkinter import simpledialog
            n = simpledialog.askstring("Rename", "Name the %s will see:" % cfg["peer_name"],
                                       initialvalue=stree.set(sel[0], "name"), parent=w)
            if n and n.strip():
                stree.set(sel[0], "name", clean_component(n.strip()))

        stree.bind("<Double-1>", toggle_access)
        sside = ttk.Frame(ts)
        sside.grid(row=2, column=2, sticky="n", padx=(8, 0))
        for text, cmd in (("Add folder…", add_share_folder), ("Add files…", add_share_file),
                          ("Add disk…", add_share_disk), ("Rename…", rename_share),
                          ("Read / write", toggle_access), ("Remove", remove_share)):
            ttk.Button(sside, text=text, command=cmd, width=12).pack(pady=2)
        ttk.Label(ts, text="Password (optional — type the same one on both computers):"
                  ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 2))
        pw_var = tk.StringVar(value=cfg.get("share_password", ""))
        ttk.Entry(ts, textvariable=pw_var, show="•", width=28).grid(row=4, column=0, sticky="w")
        ttk.Label(ts, text="Only computers in the Connection list can browse. Double-click an item to switch\n"
                           "between Read only and Read & write. Open the other computer's shared items with\n"
                           "right-click on the blue line → Browse %s…" % cfg["peer_name"],
                  foreground="#555", justify="left").grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        def save():
            addrs = [{"address": tree.set(i, "address"), "label": tree.set(i, "label")}
                     for i in tree.get_children()]
            if not addrs:
                messagebox.showwarning("NelyConnect", "Add at least one address.", parent=w)
                return
            mine_now = set(local_ipv4s())
            own = [a["address"] for a in addrs if a["address"] in mine_now]
            if own:
                messagebox.showwarning("NelyConnect", "These addresses belong to THIS computer, not the %s:\n%s"
                                       "\n\nRemove them and add the %s's addresses instead."
                                       % (cfg["peer_name"], ", ".join(own), cfg["peer_name"]), parent=w)
                return
            cfg["peer_addresses"] = addrs
            cfg["peer_name"] = name_var.get().strip() or cfg["peer_name"]
            cfg["edge"] = edge_var.get()
            cfg["receive_folder"] = os.path.expanduser(folder_var.get().strip()) or cfg["receive_folder"]
            cfg["open_folder_on_receive"] = open_var.get()
            cfg["share_input"] = input_var.get()
            if mac_opts:
                cfg["swap_ctrl_cmd"] = mac_opts["swap"].get()
                cfg["invert_scroll"] = mac_opts["invert"].get()
                cfg["mouse_speed"] = round(mac_opts["speed"].get(), 2)
                cfg["keyboard_iso"] = mac_opts["iso"].get().startswith("ISO")
            cfg["share_clipboard"] = clip_var.get()
            cfg["share_files"] = share_var.get()
            cfg["share_password"] = pw_var.get()
            cfg["shares"] = [{"name": stree.set(i, "name"), "path": stree.set(i, "path"),
                              "writable": stree.set(i, "access") == "Read & write"}
                             for i in stree.get_children()]
            clip["last"] = read_clipboard()
            save_config()
            ui["edge"] = cfg["edge"].lower()
            collapse()
            threading.Thread(target=link.check_once, daemon=True).start()
            log.info("Settings saved")
            w.destroy()

        btns = ttk.Frame(outer)
        btns.pack(anchor="e", pady=(12, 0))
        ttk.Button(btns, text="Save", command=save).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=w.destroy).pack(side="left")
        w.after(200, test_all)
        w.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w.geometry("+%d+%d" % (max(0, sw // 2 - w.winfo_reqwidth() // 2),
                               max(0, sh // 2 - w.winfo_reqheight() // 2)))


    # ---- browse the other computer's shared folders
    def open_browser():
        old = open_dialogs.get("browser")
        if old is not None and old.winfo_exists():
            old.lift()
            return
        w = tk.Toplevel(root)
        open_dialogs["browser"] = w
        w.title("NelyConnect — %s" % cfg["peer_name"])
        w.attributes("-topmost", True)
        w.geometry("760x480")
        st = {"share": None, "path": "", "writable": False, "shares": {}, "busy": False}

        bar = ttk.Frame(w, padding=(10, 10, 10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="Shared by the %s:" % cfg["peer_name"]).pack(side="left")
        share_box = ttk.Combobox(bar, state="readonly", width=26)
        share_box.pack(side="left", padx=6)
        up_btn = ttk.Button(bar, text="⬆ Up", width=7)
        up_btn.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="⟳ Refresh", command=lambda: load_list()).pack(side="left", padx=4)
        where = ttk.Label(bar, text="", foreground="#555")
        where.pack(side="left", padx=8)

        mid = ttk.Frame(w, padding=(10, 0))
        mid.pack(fill="both", expand=True)
        tree = ttk.Treeview(mid, columns=("size", "date"), selectmode="extended")
        tree.heading("#0", text="Name")
        tree.heading("size", text="Size")
        tree.heading("date", text="Modified")
        tree.column("#0", width=400)
        tree.column("size", width=100, anchor="e")
        tree.column("date", width=150)
        sb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bottom = ttk.Frame(w, padding=10)
        bottom.pack(fill="x")
        status = ttk.Label(bottom, text="Connecting…")
        status.pack(side="left")
        btn = {}
        for key, text in (("newdir", "New folder"), ("upload", "Upload…"),
                          ("dlto", "Download to…"), ("dl", "Download"), ("open", "Open")):
            btn[key] = ttk.Button(bottom, text=text)
            btn[key].pack(side="right", padx=3)

        def req(obj):
            obj["pw"] = cfg.get("share_password", "")
            obj["from"] = cfg["this_computer_name"]
            return obj

        def address():
            a = link.get() or link.check_once()
            if not a:
                raise IOError("The %s is not reachable" % cfg["peer_name"])
            return a["address"]

        def background(work, done=None, fail=None):
            def run():
                try:
                    result = work()
                    if done:
                        events.put(("call", None, lambda: w.winfo_exists() and done(result)))
                except Exception as e:
                    msg = str(e)
                    events.put(("call", None, lambda: w.winfo_exists() and (fail or show_error)(msg)))
            threading.Thread(target=run, daemon=True).start()

        def show_error(msg):
            status.configure(text="✗ " + msg)

        def rel(name):
            return (st["path"] + "/" + name) if st["path"] else name

        def update_buttons():
            can_write = st["writable"]
            for k in ("upload", "newdir"):
                btn[k].state(["!disabled"] if can_write else ["disabled"])
            up_btn.state(["!disabled"] if st["path"] else ["disabled"])

        def load_shares():
            def got(r):
                st["shares"] = {x["name"]: x for x in r["shares"]}
                names = list(st["shares"])
                share_box["values"] = names
                if not names:
                    status.configure(text="The %s isn't sharing anything yet." % cfg["peer_name"])
                    return
                share_box.set(names[0])
                pick_share()
            background(lambda: remote_call(address(), int(cfg["port"]), req({"type": "shares"})), got)

        def pick_share(*_):
            st["share"], st["path"] = share_box.get(), ""
            load_list()

        def load_list():
            if not st["share"]:
                return load_shares()
            status.configure(text="Loading…")

            def got(r):
                tree.delete(*tree.get_children())
                entries = sorted(r["entries"], key=lambda e: (not e["d"], e["n"].lower()))
                for e in entries:
                    tree.insert("", "end", iid=e["n"], text=("📁  " if e["d"] else "📄  ") + e["n"],
                                values=("" if e["d"] else human(e["s"]),
                                        time.strftime("%Y-%m-%d %H:%M", time.localtime(e["m"]))),
                                tags=("dir",) if e["d"] else ())
                st["writable"] = bool(r.get("writable"))
                where.configure(text=" / ".join([st["share"]] + [p for p in st["path"].split("/") if p]))
                status.configure(text="%d items%s" % (len(entries), "" if st["writable"] else "   ·   read only"))
                update_buttons()
            background(lambda: remote_call(address(), int(cfg["port"]),
                                           req({"type": "list", "share": st["share"], "path": st["path"]})), got)

        def is_dir(iid):
            return "dir" in tree.item(iid, "tags")

        def open_item(*_):
            sel = tree.selection()
            if not sel:
                return
            iid = sel[0]
            if is_dir(iid):
                st["path"] = rel(iid)
                return load_list()
            tmp = tempfile.mkdtemp(prefix="NelyConnect-")
            transfer_download([iid], tmp, open_after=True)

        def go_up():
            st["path"] = "/".join(st["path"].split("/")[:-1])
            load_list()

        def transfer_download(names, dest, open_after=False):
            tid = "dl-%f" % time.time()
            events.put(("start", tid, "Copying from the %s" % cfg["peer_name"], 0))
            last = [0.0]

            def prog(name, done_b, tot):
                now = time.time()
                if name and (now - last[0] > 0.1 or done_b >= tot):
                    last[0] = now
                    events.put(("progress", tid, name, done_b, tot))

            def work():
                first, total = remote_download(address(), int(cfg["port"]), req(
                    {"type": "get", "share": st["share"], "paths": [rel(n) for n in names]}), dest, prog)
                return first, total

            def done(result):
                first, total = result
                info = windows.get(tid)
                if info:
                    info["bar"].configure(maximum=max(total, 1))
                events.put(("done", tid, "Copied to %s" % os.path.basename(dest.rstrip("\\/")), total))
                if open_after and first:
                    open_folder(first)
                elif cfg.get("open_folder_on_receive"):
                    open_folder(dest)

            def fail(msg):
                events.put(("error", tid, "Copy failed: %s" % msg))
            background(work, done, fail)

        def download(ask=False):
            names = list(tree.selection())
            if not names:
                status.configure(text="Select files or folders first")
                return
            dest = cfg["receive_folder"]
            if ask:
                dest = filedialog.askdirectory(parent=w, title="Copy to…", initialdir=dest)
                if not dest:
                    return
            transfer_download(names, dest)

        def upload(paths=None):
            if not st["writable"]:
                status.configure(text="✗ This shared item is read only")
                return
            paths = paths or list(filedialog.askopenfilenames(parent=w, title="Choose files to copy"))
            if not paths:
                return
            tid = "up-%f" % time.time()
            total = sum(i[3] for i in collect(paths))
            events.put(("start", tid, "Copying to the %s" % cfg["peer_name"], total))
            last = [0.0]

            def prog(name, done_b, tot):
                now = time.time()
                if now - last[0] > 0.1 or done_b >= tot:
                    last[0] = now
                    events.put(("progress", tid, name.split("/")[-1], done_b, tot))

            def done(_):
                events.put(("done", tid, "Copied to %s" % (st["path"] or st["share"]), total))
                load_list()

            background(lambda: remote_upload(address(), int(cfg["port"]), req(
                {"type": "put", "share": st["share"], "path": st["path"]}), paths, prog),
                done, lambda m: events.put(("error", tid, "Copy failed: %s" % m)))

        def new_folder():
            from tkinter import simpledialog
            name = simpledialog.askstring("New folder", "Folder name:", parent=w)
            if name and name.strip():
                background(lambda: remote_call(address(), int(cfg["port"]), req(
                    {"type": "mkdir", "share": st["share"], "path": st["path"], "name": name.strip()})),
                    lambda _: load_list())

        def on_drop(event):
            upload(list(root.tk.splitlist(event.data)))
            return event.action

        share_box.bind("<<ComboboxSelected>>", pick_share)
        tree.bind("<Double-1>", open_item)
        tree.bind("<Return>", open_item)
        tree.bind("<BackSpace>", lambda e: st["path"] and go_up())
        up_btn.configure(command=go_up)
        btn["open"].configure(command=open_item)
        btn["dl"].configure(command=download)
        btn["dlto"].configure(command=lambda: download(ask=True))
        btn["upload"].configure(command=upload)
        btn["newdir"].configure(command=new_folder)
        try:
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", on_drop)
        except Exception:
            pass
        update_buttons()
        load_shares()


    # ---- phone window (QR code, on/off, send-to-phone list)
    def open_phone():
        old = open_dialogs.get("phone")
        if old is not None and old.winfo_exists():
            old.lift()
            return
        if phone is None:
            messagebox.showerror("NelyConnect", "Phone access couldn't start on this computer. See the log file.")
            return
        import nelyphone
        w = tk.Toplevel(root)
        open_dialogs["phone"] = w
        w.title("NelyConnect — Phone")
        w.attributes("-topmost", True)
        w.resizable(False, False)
        bold = ("Segoe UI" if IS_WIN else "Helvetica", 12, "bold")
        frm = ttk.Frame(w, padding=16)
        frm.pack(fill="both", expand=True)
        left = ttk.Frame(frm)
        left.grid(row=0, column=0, sticky="n")
        right = ttk.Frame(frm, padding=(18, 0, 0, 0))
        right.grid(row=0, column=1, sticky="n")

        on_var = tk.BooleanVar(value=bool(cfg.get("phone_enabled")))
        ttk.Checkbutton(left, text="Allow my phone to connect", variable=on_var,
                        command=lambda: (cfg.__setitem__("phone_enabled", on_var.get()),
                                         write_config(cfg), draw())).pack(anchor="w")
        qr_label = ttk.Label(left)
        qr_label.pack(pady=10)
        hint = ttk.Label(left, text="", justify="center", wraplength=260)
        hint.pack()
        addr_row = ttk.Frame(left)
        addr_row.pack(fill="x", pady=(8, 0))
        ttk.Label(addr_row, text="Address:").pack(side="left")
        addrs = nelyphone.wifi_addresses() or ["127.0.0.1"]
        addr_box = ttk.Combobox(addr_row, values=addrs, state="readonly", width=16)
        addr_box.set(addrs[0])
        addr_box.pack(side="left", padx=6)
        url_var = tk.StringVar()
        ttk.Entry(left, textvariable=url_var, width=40, state="readonly").pack(fill="x", pady=(8, 4))
        btns = ttk.Frame(left)
        btns.pack(fill="x")

        def copy_link():
            root.clipboard_clear()
            root.clipboard_append(url_var.get())

        def new_code():
            if messagebox.askyesno("NelyConnect", "Make a new code? Phones that scanned the old QR code "
                                   "will need to scan again.", parent=w):
                phone.reset_token()
                draw()

        ttk.Button(btns, text="Copy link", command=copy_link).pack(side="left")
        ttk.Button(btns, text="New code", command=new_code).pack(side="left", padx=6)

        def draw(*_):
            url = phone.url(addr_box.get())
            url_var.set(url)
            if not on_var.get():
                qr_label.configure(image="", text="\n\nTurn on “Allow my phone\nto connect” to show\nthe QR code\n\n",
                                   anchor="center", justify="center")
                hint.configure(text="")
                return
            try:
                import qrcode
                img = qrcode.make(url, box_size=6, border=2)
                path = os.path.join(DATA_DIR, "phone_qr.png")
                img.save(path)
                images["qr"] = tk.PhotoImage(file=path)
                qr_label.configure(image=images["qr"], text="")
            except Exception:
                qr_label.configure(image="", text="(QR code unavailable — type the address below into the phone's browser)")
            hint.configure(text="Scan with the phone's camera (iPhone or Android).\n"
                                "The phone must be on the same Wi-Fi as this computer.")

        addr_box.bind("<<ComboboxSelected>>", draw)

        # send-to-phone list
        ttk.Label(right, text="Send to phone", font=bold).pack(anchor="w")
        ttk.Label(right, text="Files you add here appear on the phone's page,\nready to download.",
                  foreground="#555", justify="left").pack(anchor="w", pady=(2, 8))
        tree = ttk.Treeview(right, columns=("size",), height=9, selectmode="extended")
        tree.heading("#0", text="File")
        tree.heading("size", text="Size")
        tree.column("#0", width=230)
        tree.column("size", width=80, anchor="e")
        tree.pack(fill="both")
        rb = ttk.Frame(right)
        rb.pack(fill="x", pady=(8, 0))

        def refresh_list():
            if not w.winfo_exists():
                return
            have = set(tree.get_children())
            with phone.lock:
                items = list(phone.outbox)
            ids = {o["id"] for o in items}
            for iid in have - ids:
                tree.delete(iid)
            for o in items:
                if o["id"] not in have:
                    tree.insert("", "end", iid=o["id"], text=("📁 " if o["dir"] else "📄 ") + o["name"],
                                values=("folder" if o["dir"] else human(o["size"]),))
            w.after(2000, refresh_list)

        def add_files():
            phone.add_to_outbox(list(filedialog.askopenfilenames(parent=w, title="Send to phone")))
            refresh_list()

        def add_folder():
            d = filedialog.askdirectory(parent=w, title="Send a folder to phone (downloads as .zip)")
            if d:
                phone.add_to_outbox([d])
                refresh_list()

        def remove():
            for iid in tree.selection():
                phone.remove_from_outbox(iid)
            refresh_list()

        ttk.Button(rb, text="Add files…", command=add_files).pack(side="left")
        ttk.Button(rb, text="Add folder…", command=add_folder).pack(side="left", padx=6)
        ttk.Button(rb, text="Remove", command=remove).pack(side="left")
        ttk.Label(right, text="Tip: you can also drag files onto this window.\n"
                              "Files sent from the phone go to your NelyConnect Inbox.",
                  foreground="#555", justify="left").pack(anchor="w", pady=(10, 0))
        if dnd["ok"]:
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", lambda e: (phone.add_to_outbox(list(root.tk.splitlist(e.data))),
                                                  refresh_list(), e.action)[-1])
            except Exception:
                pass
        draw()
        refresh_list()
        w.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w.geometry("+%d+%d" % (max(0, sw // 2 - w.winfo_reqwidth() // 2), max(0, sh // 3 - w.winfo_reqheight() // 2)))

    def toggle_phone():
        cfg["phone_enabled"] = not cfg.get("phone_enabled")
        write_config(cfg)

    # ---- right-click menu
    menu = tk.Menu(root, tearoff=0)
    def open_inbox():
        os.makedirs(cfg["receive_folder"], exist_ok=True)
        open_folder(cfg["receive_folder"])

    menu.add_command(label="Open inbox folder", command=open_inbox)
    menu.add_command(label="Browse %s…" % cfg["peer_name"], command=open_browser)
    menu.add_command(label="Phone…", command=open_phone)
    menu.add_checkbutton(label="Share keyboard & mouse", variable=input_var,
                         command=toggle_input)
    menu.add_checkbutton(label="Share copy & paste (text)", variable=clip_var,
                         command=toggle_clipboard)
    menu.add_command(label="Settings…", command=open_settings)
    menu.add_command(label="Edit settings file (advanced)", command=lambda: open_file(CONFIG_PATH))
    def show_status():
        w = tk.Toplevel(root)
        w.title("NelyConnect")
        w.attributes("-topmost", True)
        w.resizable(False, False)
        frm = ttk.Frame(w, padding=18)
        frm.pack(fill="both", expand=True)
        if "logo" in images:
            ttk.Label(frm, image=images["logo"]).pack(pady=(0, 12))
        status = ttk.Label(frm, text="Checking connection…",
                           font=("Segoe UI" if IS_WIN else "Helvetica", 12, "bold"))
        status.pack()
        detail = ttk.Label(frm, text="", justify="center")
        detail.pack(pady=(4, 4))
        inp = ttk.Label(frm, text="", justify="center")
        inp.pack(pady=(0, 12))
        btns = ttk.Frame(frm)
        btns.pack()
        ttk.Button(btns, text="Open inbox", command=open_inbox).pack(side="left", padx=4)
        ttk.Button(btns, text="Browse %s" % cfg["peer_name"], command=open_browser).pack(side="left", padx=4)
        ttk.Button(btns, text="Settings", command=open_settings).pack(side="left", padx=4)
        ttk.Button(btns, text="Close", command=w.destroy).pack(side="left", padx=4)

        def refresh():
            if not w.winfo_exists():
                return
            a = link.get()
            if a:
                status.configure(text="✓ Connected to %s via %s" % (cfg["peer_name"], a["label"]))
                detail.configure(text="Address %s\nDrag files to the %s edge of the screen to send them.\n"
                                      "Received files go to: %s" % (a["address"], ui["edge"], cfg["receive_folder"]))
            else:
                tried = ", ".join(x["address"] for x in cfg["peer_addresses"])
                status.configure(text="✗ %s is not reachable" % cfg["peer_name"])
                detail.configure(text="Tried: %s\nMake sure NelyConnect is running on the %s\n"
                                      "and the Ethernet cable / Wi-Fi is connected." % (tried, cfg["peer_name"]))
            inp.configure(text=(input_sys.status_text() if input_sys
                                else "Keyboard & mouse sharing is not available") +
                          ("" if dnd["ok"] else "\nDrag & drop to the screen edge is unavailable on this "
                                                "computer — use Browse / Upload instead."))
            w.after(1500, refresh)

        w.after(50, refresh)
        w.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w.geometry("+%d+%d" % (sw // 2 - w.winfo_reqwidth() // 2, sh // 3 - w.winfo_reqheight() // 2))

    menu.add_command(label="Status / connection", command=show_status)
    menu.add_separator()
    menu.add_command(label="Quit NelyConnect", command=lambda: quit_app())
    root.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
    root.bind("<Button-2>", lambda e: menu.tk_popup(e.x_root, e.y_root))  # Mac right-click


    # ---- system tray / menu bar icon
    tray = {"icon": None}

    def toggle_share_files():
        cfg["share_files"] = not cfg.get("share_files")
        save_config()

    def quit_app():
        if tray.get("proc"):
            try:
                tray["proc"].terminate()
            except Exception:
                pass
        if tray["icon"]:
            try:
                tray["icon"].stop()
            except Exception:
                pass
        root.destroy()

    def tray_status():
        a = link.get()
        line = ("Connected to %s via %s" % (cfg["peer_name"], a["label"])) if a else \
               ("%s not reachable" % cfg["peer_name"])
        if input_sys and cfg.get("share_input") and getattr(input_sys, "remote", False):
            line += "  ·  controlling %s" % cfg["peer_name"]
        return line


    # Mac menu-bar icon runs in its own small process (NelyConnect --tray) so that it can
    # never crash or block the main app. It talks to us over 127.0.0.1.
    def start_mac_tray_helper():
        actions = {
            "status": show_status, "browse": open_browser, "inbox": open_inbox,
            "settings": open_settings, "quit": quit_app, "phone": open_phone,
            "toggle_phone": toggle_phone,
            "toggle_input": lambda: (input_var.set(not input_var.get()), toggle_input()),
            "toggle_clip": lambda: (clip_var.set(not clip_var.get()), toggle_clipboard()),
            "toggle_share": toggle_share_files,
        }
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
        except OSError:
            log.exception("Tray control socket")
            return
        port = srv.getsockname()[1]

        def serve():
            while True:
                c, _ = srv.accept()
                try:
                    c.settimeout(5)
                    req = read_json(c.makefile("rb"))
                    cmd = req.get("cmd")
                    if cmd in actions:
                        events.put(("call", None, actions[cmd]))
                    send_json(c, {"ok": True, "status": tray_status(), "peer": cfg["peer_name"],
                                  "share_input": bool(cfg.get("share_input")),
                                  "share_clipboard": bool(cfg.get("share_clipboard")),
                                  "share_files": bool(cfg.get("share_files")),
                                  "phone_enabled": bool(cfg.get("phone_enabled"))})
                except Exception:
                    pass
                finally:
                    c.close()

        threading.Thread(target=serve, daemon=True, name="tray-control").start()
        args = [sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]
        try:
            tray["proc"] = subprocess.Popen(args + ["--tray", str(port)])
            log.info("Menu bar helper started")
        except Exception:
            log.exception("Could not start the menu bar helper")

    def start_tray():
        if IS_MAC:
            return start_mac_tray_helper()
        try:
            if IS_LINUX and "PYSTRAY_BACKEND" not in os.environ:
                try:                                   # AppIndicator if available, else classic tray
                    import gi
                    gi.require_version("Gtk", "3.0")
                    try:
                        gi.require_version("AyatanaAppIndicator3", "0.1")
                    except ValueError:
                        gi.require_version("AppIndicator3", "0.1")
                except Exception:
                    os.environ["PYSTRAY_BACKEND"] = "xorg"
            import pystray
            from PIL import Image
        except Exception as e:
            log.warning("No tray icon: %s", e)
            return

        def on_main(fn):
            return lambda icon=None, item=None: events.put(("call", None, fn))

        def flip(var, fn):
            def run():
                var.set(not var.get())
                fn()
                refresh_tray()
            return run

        M = pystray.MenuItem
        menu_items = (
            M(lambda item: tray_status(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            M("Status…", on_main(show_status), default=True),
            M("Browse %s…" % cfg["peer_name"], on_main(open_browser)),
            M("Open inbox folder", on_main(open_inbox)),
            M("Phone…", on_main(open_phone)),
            pystray.Menu.SEPARATOR,
            M("Share keyboard & mouse", on_main(flip(input_var, toggle_input)),
              checked=lambda item: bool(cfg.get("share_input"))),
            M("Share copy & paste (text)", on_main(flip(clip_var, toggle_clipboard)),
              checked=lambda item: bool(cfg.get("share_clipboard"))),
            M("Share my folders", on_main(lambda: (toggle_share_files(), refresh_tray())),
              checked=lambda item: bool(cfg.get("share_files"))),
            M("Allow phone access", on_main(lambda: (toggle_phone(), refresh_tray())),
              checked=lambda item: bool(cfg.get("phone_enabled"))),
            pystray.Menu.SEPARATOR,
            M("Settings…", on_main(open_settings)),
            M("Quit NelyConnect", on_main(quit_app)),
        )
        size = 22 if IS_MAC else 64
        img = Image.open(os.path.join(ASSETS, "icon_64.png")).convert("RGBA").resize((size, size), Image.LANCZOS)
        icon = pystray.Icon("NelyConnect", img, "NelyConnect", pystray.Menu(*menu_items))
        tray["icon"] = icon
        if IS_MAC:
            icon.run_detached()                  # Tk's main loop drives the macOS menu bar
        else:
            threading.Thread(target=icon.run, daemon=True, name="tray").start()
        log.info("Tray icon started")

    def refresh_tray():
        icon = tray["icon"]
        if icon:
            try:
                icon.title = "NelyConnect — " + tray_status()
                icon.update_menu()
            except Exception:
                pass

    def tray_tick():
        refresh_tray()
        root.after(3000, tray_tick)

    def keep_on_top():
        root.attributes("-topmost", True)
        root.lift()
        if root.winfo_width() <= thin + 1:
            place(thin)   # follow monitor changes
        root.after(3000, keep_on_top)

    collapse()
    keep_on_top()
    pump()
    clip["last"] = read_clipboard()   # don't send what was copied before NelyConnect started
    watch_clipboard()
    if cfg.get("_first_run"):
        root.after(800, show_status)
    root.after(1500, check_accessibility)
    root.after(300, start_tray)
    root.after(3000, tray_tick)
    log.info("NelyConnect started (%s, edge=%s)", cfg["this_computer_name"], ui["edge"])
    root.mainloop()


def run_tray_helper(port):
    """Mac menu-bar icon (separate process). Every click is forwarded to the main app."""
    import pystray
    from PIL import Image
    state = {"status": "NelyConnect", "peer": "other computer", "fails": 0}

    def call(cmd=None):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as c:
                send_json(c, {"cmd": cmd})
                state.update(read_json(c.makefile("rb")))
            state["fails"] = 0
            return True
        except Exception:
            state["fails"] += 1
            return False

    call()

    def act(cmd):
        return lambda icon, item: call(cmd)

    M = pystray.MenuItem
    menu = pystray.Menu(
        M(lambda item: state["status"], None, enabled=False),
        pystray.Menu.SEPARATOR,
        M("Status…", act("status"), default=True),
        M(lambda item: "Browse %s…" % state["peer"], act("browse")),
        M("Open inbox folder", act("inbox")),
        M("Phone…", act("phone")),
        pystray.Menu.SEPARATOR,
        M("Share keyboard & mouse", act("toggle_input"), checked=lambda item: state.get("share_input", False)),
        M("Share copy & paste (text)", act("toggle_clip"), checked=lambda item: state.get("share_clipboard", False)),
        M("Share my folders", act("toggle_share"), checked=lambda item: state.get("share_files", False)),
        M("Allow phone access", act("toggle_phone"), checked=lambda item: state.get("phone_enabled", False)),
        pystray.Menu.SEPARATOR,
        M("Settings…", act("settings")),
        M("Quit NelyConnect", act("quit")),
    )
    img = Image.open(os.path.join(ASSETS, "icon_64.png")).convert("RGBA").resize((22, 22), Image.LANCZOS)
    icon = pystray.Icon("NelyConnect", img, "NelyConnect", menu)

    def poll():
        while True:
            time.sleep(2)
            ok = call()
            if not ok and state["fails"] >= 3:      # main app is gone
                icon.stop()
                return
            try:
                from PyObjCTools import AppHelper
                AppHelper.callAfter(icon.update_menu)
            except Exception:
                pass

    threading.Thread(target=poll, daemon=True).start()
    icon.run()


def main():
    import faulthandler
    try:
        faulthandler.enable(open(os.path.join(DATA_DIR, "crash.log"), "a"))
    except Exception:
        pass
    sys.excepthook = lambda t, v, tb: log.error("Unhandled error", exc_info=(t, v, tb))
    if "--tray" in sys.argv:
        try:
            run_tray_helper(int(sys.argv[sys.argv.index("--tray") + 1]))
        except Exception:
            log.exception("Menu bar helper failed")
        return
    try:
        run_ui(load_config())
    except Exception:
        log.exception("NelyConnect stopped because of an error")
        raise


if __name__ == "__main__":
    main()
