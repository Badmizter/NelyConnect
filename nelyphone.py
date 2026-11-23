"""
NelyConnect - phone access (iPhone & Android, no app needed).

A small web server built into NelyConnect. The phone scans a QR code that contains
this computer's Wi-Fi address plus a secret code, and gets a page where it can:
  - send photos / videos / files to this computer (or on to the other computer)
  - download files this computer "sent to phone"
  - browse and download this computer's shared folders

Off by default. Every request needs the secret code (from the QR) - anyone else on the
Wi-Fi only gets "access denied". "New code" invalidates every phone that had the old one.
"""
import hmac
import json
import logging
import mimetypes
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("nelyconnect")

PHONE_PORT = 24880
CHUNK = 1024 * 1024
COOKIE = "nelyconnect_key"


def new_token():
    return secrets.token_urlsafe(18)


def wifi_addresses():
    """This computer's addresses a phone on the Wi-Fi could reach, best first."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # no packet is sent; picks the default route
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    good = []
    for ip in ips:
        if ip.startswith(("127.", "169.254.", "10.77.0.")) or ip in good:
            continue                                # loopback, cable-only, NelyConnect cable
        good.append(ip)
    # private LAN ranges first, VPN / other last
    good.sort(key=lambda ip: 0 if ip.startswith(("192.168.", "10.", "172.")) and not ip.startswith("100.") else 1)
    return good


class _Wrapped:
    """Lets zipfile write straight into the HTTP response (no seeking)."""

    def __init__(self, f):
        self.f = f
        self.n = 0

    def write(self, b):
        self.f.write(b)
        self.n += len(b)
        return len(b)

    def flush(self):
        self.f.flush()


class PhoneServer:
    def __init__(self, cfg, receiver, link, events, helpers, assets_dir, save_config):
        self.cfg = cfg
        self.receiver = receiver          # used for shared-folder rules (find_share / resolve / list)
        self.link = link
        self.events = events
        self.h = helpers                  # clean_component, unique_name, send_paths
        self.assets = assets_dir
        self.save_config = save_config
        self.outbox = []                  # files waiting for the phone
        self.lock = threading.Lock()
        self.httpd = None
        if not cfg.get("phone_token"):
            cfg["phone_token"] = new_token()
            save_config()

    # ---- control
    def start(self):
        port = int(self.cfg.get("phone_port", PHONE_PORT))
        server = self

        class Handler(_Handler):
            app = server

        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        except OSError as e:
            log.error("Phone access could not start on port %s: %s", port, e)
            return False
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="phone-http").start()
        log.info("Phone access listening on port %s", port)
        return True

    def enabled(self):
        return bool(self.cfg.get("phone_enabled"))

    def url(self, ip=None):
        ip = ip or (wifi_addresses() or ["127.0.0.1"])[0]
        return "http://%s:%s/?k=%s" % (ip, int(self.cfg.get("phone_port", PHONE_PORT)), self.cfg["phone_token"])

    def reset_token(self):
        self.cfg["phone_token"] = new_token()
        self.save_config()

    def add_to_outbox(self, paths):
        added = 0
        with self.lock:
            for p in paths:
                if os.path.isfile(p) or os.path.isdir(p):
                    self.outbox.append({"id": secrets.token_hex(6), "name": os.path.basename(p.rstrip("\\/")),
                                        "path": os.path.abspath(p), "dir": os.path.isdir(p),
                                        "size": 0 if os.path.isdir(p) else os.path.getsize(p),
                                        "added": time.time()})
                    added += 1
        return added

    def remove_from_outbox(self, item_id):
        with self.lock:
            self.outbox = [o for o in self.outbox if o["id"] != item_id]

    def check_token(self, token):
        return bool(token) and hmac.compare_digest(str(token), str(self.cfg.get("phone_token", "")))

    # ---- incoming upload from a phone
    def receive_upload(self, rfile, length, name, target, tid):
        name = (name or "file").replace("\\", "/").split("/")[-1]   # keep only the file name
        name = self.h.clean_component(name)
        if target == "peer":
            folder = tempfile.mkdtemp(prefix="NelyConnect-phone-")
        else:
            folder = self.cfg["receive_folder"]
            os.makedirs(folder, exist_ok=True)
        final = os.path.join(folder, self.h.unique_name(folder, name))
        tmp = final + ".nelyconnect-part"
        self.events.put(("start", tid, "Receiving from your phone", length))
        done, last = 0, 0.0
        try:
            with open(tmp, "wb") as out:
                while done < length:
                    chunk = rfile.read(min(CHUNK, length - done))
                    if not chunk:
                        raise ConnectionError("The phone stopped sending")
                    out.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 0.15:
                        last = time.time()
                        self.events.put(("progress", tid, name, done, length))
            os.replace(tmp, final)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self.events.put(("progress", tid, name, done, length))
        if target == "peer":
            self.events.put(("done", tid, "Got %s - sending it on to the %s" % (name, self.cfg["peer_name"]), length))
            threading.Thread(target=self._forward, args=(final, folder), daemon=True).start()
        else:
            self.events.put(("done", tid, "Saved %s from your phone" % name, length))
            if self.cfg.get("open_folder_on_receive"):
                self.events.put(("open_folder", None, folder))
        log.info("Phone upload %s (%d bytes) -> %s", name, length, target)
        return os.path.basename(final)

    def _forward(self, path, folder):
        tid = "fwd-%f" % time.time()
        try:
            a = self.link.get() or self.link.check_once()
            if not a:
                raise IOError("%s is not reachable" % self.cfg["peer_name"])
            size = os.path.getsize(path)
            self.events.put(("start", tid, "Phone → %s" % self.cfg["peer_name"], size))
            self.h.send_paths(a["address"], int(self.cfg["port"]), [path], "Phone (via %s)" %
                              self.cfg["this_computer_name"],
                              lambda r, d, t: self.events.put(("progress", tid, r.split("/")[-1], d, t)))
            self.events.put(("done", tid, "Sent to %s" % self.cfg["peer_name"], size))
            shutil.rmtree(folder, ignore_errors=True)
        except Exception as e:
            # keep the file: move it to the local inbox so nothing is lost
            inbox = self.cfg["receive_folder"]
            os.makedirs(inbox, exist_ok=True)
            try:
                dest = os.path.join(inbox, self.h.unique_name(inbox, os.path.basename(path)))
                shutil.move(path, dest)
                shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass
            self.events.put(("error", tid, "Couldn't forward to %s (%s) - saved here instead" %
                             (self.cfg["peer_name"], e)))

    # ---- info for the page
    def info(self):
        a = self.link.get()
        shares = []
        if self.cfg.get("share_files"):
            shares = [{"name": s["name"], "writable": bool(s.get("writable")),
                       "is_file": os.path.isfile(s.get("path", ""))} for s in self.cfg.get("shares", [])]
        with self.lock:
            outbox = [{"id": o["id"], "name": o["name"], "size": o["size"], "dir": o["dir"]} for o in self.outbox]
        return {"name": self.cfg["this_computer_name"], "peer": self.cfg["peer_name"],
                "peer_online": bool(a), "outbox": outbox, "shares": shares}


class _Handler(BaseHTTPRequestHandler):
    app = None                       # set by PhoneServer.start
    server_version = "NelyConnect"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ---- helpers
    def _q(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

    def _route(self):
        return urllib.parse.urlparse(self.path).path

    def _token(self):
        q = self._q()
        if "k" in q:
            return q["k"]
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return urllib.parse.unquote(v)
        return None

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if ctype == "application/json" else body.encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(data)))
        extra = extra or {}
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _guard(self):
        app = self.app
        if not app.enabled():
            self._send(403, _message_page("Phone access is turned off",
                                          "Turn it on in NelyConnect on %s: tray icon → Phone…" %
                                          app.cfg["this_computer_name"]), "text/html")
            return False
        if not app.check_token(self._token()):
            self._send(403, _message_page("Access denied",
                                          "Scan the QR code again in NelyConnect → Phone… on the computer."),
                       "text/html")
            return False
        return True

    def _stream_file(self, path, name=None):
        size = os.path.getsize(path)
        name = name or os.path.basename(path)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % urllib.parse.quote(name))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, CHUNK)

    def _stream_zip(self, folder, name):
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" %
                         urllib.parse.quote(name + ".zip"))
        self.send_header("Connection", "close")
        self.end_headers()
        root = os.path.realpath(folder)
        with zipfile.ZipFile(_Wrapped(self.wfile), "w", zipfile.ZIP_STORED) as z:
            for dirpath, dirnames, files in os.walk(folder):
                for fn in files:
                    if fn.startswith(".") or fn.endswith(".nelyconnect-part"):
                        continue
                    full = os.path.join(dirpath, fn)
                    real = os.path.realpath(full)
                    if os.path.commonpath([root, real]) != root:
                        continue                      # never follow links out of the folder
                    z.write(full, os.path.join(name, os.path.relpath(full, folder)))
        self.close_connection = True

    # ---- GET
    def do_GET(self):
        app, route, q = self.app, self._route(), self._q()
        try:
            if route in ("/logo.png", "/icon.png"):
                fn = "logo_small.png" if route == "/logo.png" else "icon_180.png"
                with open(os.path.join(app.assets, fn), "rb") as f:
                    return self._send(200, f.read(), "image/png", {"Cache-Control": "max-age=86400"})
            if not self._guard():
                return
            if route == "/" and "k" in q:              # first visit from the QR: remember the code
                return self._send(302, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": "%s=%s; Max-Age=31536000; Path=/; SameSite=Lax; HttpOnly" % (
                        COOKIE, urllib.parse.quote(q["k"]))})
            if route == "/":
                return self._send(200, PAGE, "text/html")
            if route == "/api/info":
                return self._send(200, app.info())
            if route == "/api/outbox":
                with app.lock:
                    item = next((o for o in app.outbox if o["id"] == q.get("id")), None)
                if not item or not os.path.exists(item["path"]):
                    return self._send(404, {"error": "That file is no longer available"})
                return self._stream_zip(item["path"], item["name"]) if item["dir"] else \
                    self._stream_file(item["path"], item["name"])
            if route in ("/api/list", "/api/file"):
                if not app.cfg.get("share_files"):
                    return self._send(403, {"error": "Shared folders are turned off"})
                share = app.receiver.find_share(q.get("share"))
                full = app.receiver.resolve(share, q.get("path", ""))
                if route == "/api/list":
                    entries, writable = app.receiver.list_entries(share, full)
                    return self._send(200, {"entries": entries, "writable": writable})
                if os.path.isdir(full):
                    return self._stream_zip(full, os.path.basename(full.rstrip("\\/")) or share["name"])
                return self._stream_file(full)
            self._send(404, {"error": "Not found"})
        except (PermissionError, FileNotFoundError) as e:
            self._send(403, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.exception("Phone request failed")
            try:
                self._send(500, {"error": str(e)})
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    # ---- POST
    def do_POST(self):
        app, route, q = self.app, self._route(), self._q()
        try:
            if not self._guard():
                return
            if route == "/api/upload":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return self._send(411, {"error": "No file received"})
                target = "peer" if q.get("to") == "peer" else "this"
                saved = app.receive_upload(self.rfile, length, q.get("name", "file"), target,
                                           "ph-%f" % time.time())
                return self._send(200, {"ok": True, "saved": saved})
            if route == "/api/outbox/remove":
                app.remove_from_outbox(q.get("id"))
                return self._send(200, {"ok": True})
            self._send(404, {"error": "Not found"})
        except (BrokenPipeError, ConnectionResetError, ConnectionError) as e:
            log.info("Phone upload interrupted: %s", e)
        except Exception as e:
            log.exception("Phone upload failed")
            try:
                self._send(500, {"error": str(e)})
            except Exception:
                pass


def _message_page(title, text):
    return ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>NelyConnect</title><body style='font-family:-apple-system,system-ui,sans-serif;"
            "padding:32px;text-align:center;color:#0b2a5b'><img src='/logo.png' style='width:180px'>"
            "<h2>%s</h2><p style='color:#555'>%s</p></body>" % (title, text))


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b3a7e">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="NelyConnect">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="icon" href="/icon.png">
<title>NelyConnect</title>
<style>
  :root { --navy:#0b3a7e; --teal:#14b39a; --bg:#f3f6fa; --card:#fff; --ink:#0f1f33; --mute:#6b7a8c; --line:#e3e8ef; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; font-family:-apple-system,system-ui,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--ink); }
  header { background:linear-gradient(135deg,var(--navy),#1565c0 60%,var(--teal)); color:#fff;
           padding:calc(18px + env(safe-area-inset-top)) 20px 22px; }
  header .brand { display:flex; align-items:center; gap:10px; font-weight:700; font-size:20px; }
  header .brand img { width:44px; height:44px; border-radius:11px; }
  header .sub { margin-top:10px; font-size:14px; opacity:.9; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#7CFFB2; margin-right:6px; }
  main { padding:16px; max-width:640px; margin:0 auto; }
  .card { background:var(--card); border-radius:16px; padding:16px; margin-bottom:14px; box-shadow:0 1px 3px rgba(15,31,51,.08); }
  h2 { font-size:16px; margin:0 0 12px; }
  .pick { display:block; width:100%; padding:16px; border:0; border-radius:12px; font-size:17px; font-weight:600;
          color:#fff; background:var(--teal); }
  .pick:active { filter:brightness(.92); }
  .seg { display:flex; background:var(--bg); border-radius:10px; padding:3px; margin-bottom:12px; }
  .seg button { flex:1; border:0; background:none; padding:9px; border-radius:8px; font-size:14px; color:var(--mute); }
  .seg button.on { background:#fff; color:var(--ink); font-weight:600; box-shadow:0 1px 2px rgba(0,0,0,.1); }
  .seg button:disabled { opacity:.4; }
  .row { display:flex; align-items:center; gap:10px; padding:10px 0; border-top:1px solid var(--line); }
  .row:first-child { border-top:0; }
  .ico { font-size:22px; width:28px; text-align:center; }
  .name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:15px; }
  .meta { font-size:12px; color:var(--mute); }
  .btn { border:1px solid var(--line); background:#fff; border-radius:9px; padding:7px 12px; font-size:14px;
         color:var(--navy); text-decoration:none; white-space:nowrap; }
  .bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:6px; }
  .bar i { display:block; height:100%; width:0; background:var(--teal); transition:width .2s; }
  .ok { color:#0a8f5a; } .err { color:#c0392b; }
  .empty { color:var(--mute); font-size:14px; padding:6px 0; }
  .chips { display:flex; gap:8px; overflow-x:auto; padding-bottom:6px; margin-bottom:6px; }
  .chip { border:1px solid var(--line); background:#fff; border-radius:20px; padding:7px 14px; font-size:14px; white-space:nowrap; }
  .chip.on { background:var(--navy); color:#fff; border-color:var(--navy); }
  .crumb { font-size:13px; color:var(--mute); margin:4px 0 6px; display:flex; align-items:center; gap:8px; }
  footer { text-align:center; color:var(--mute); font-size:12px; padding:8px 16px 28px; }
</style>
</head><body>
<header>
  <div class="brand"><img src="/icon.png" alt=""><span>NelyConnect</span></div>
  <div class="sub" id="status">Connecting…</div>
</header>
<main>
  <section class="card">
    <h2>Send from this phone</h2>
    <div class="seg" id="target">
      <button data-to="this" class="on" id="t-this">This computer</button>
      <button data-to="peer" id="t-peer">Other computer</button>
    </div>
    <input type="file" id="files" multiple hidden>
    <button class="pick" id="pickBtn">Choose photos, videos or files</button>
    <div id="uploads"></div>
  </section>

  <section class="card">
    <h2 id="outTitle">Sent to your phone</h2>
    <div id="outbox"><div class="empty">Nothing yet. On the computer: NelyConnect → Phone… → Send files to phone.</div></div>
  </section>

  <section class="card" id="sharesCard" hidden>
    <h2>Shared folders</h2>
    <div class="chips" id="chips"></div>
    <div class="crumb"><button class="btn" id="up">⬆ Up</button><span id="where"></span></div>
    <div id="list"></div>
  </section>
</main>
<footer>Keep this page open until sending finishes.</footer>

<script>
const $ = id => document.getElementById(id);
const st = { to: "this", share: null, path: "", info: null };
const fmt = n => n < 1024 ? n + " B" : n < 1048576 ? (n/1024).toFixed(0) + " KB" :
                 n < 1073741824 ? (n/1048576).toFixed(1) + " MB" : (n/1073741824).toFixed(2) + " GB";
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = (n, dir) => dir ? "📁" : /\.(jpe?g|png|heic|gif|webp)$/i.test(n) ? "🖼️" :
                 /\.(mov|mp4|m4v|avi|mkv)$/i.test(n) ? "🎬" : /\.(mp3|wav|m4a|aac)$/i.test(n) ? "🎵" : "📄";

async function api(p) { const r = await fetch(p, {cache: "no-store"}); if (!r.ok) throw new Error((await r.json()).error || r.status); return r.json(); }

async function refresh() {
  try {
    const i = await api("/api/info"); st.info = i;
    $("status").innerHTML = '<span class="dot"></span>Connected to <b>' + esc(i.name) + '</b>' +
      (i.peer_online ? ' · ' + esc(i.peer) + ' online' : '');
    $("t-this").textContent = esc(i.name);
    $("t-peer").textContent = i.peer;
    $("t-peer").disabled = !i.peer_online;
    if (!i.peer_online && st.to === "peer") setTarget("this");
    $("outTitle").textContent = "From " + i.name;
    $("outbox").innerHTML = i.outbox.length ? i.outbox.map(o =>
      '<div class="row"><span class="ico">' + icon(o.name, o.dir) + '</span><div class="name">' + esc(o.name) +
      '<div class="meta">' + (o.dir ? "folder (zip)" : fmt(o.size)) + '</div></div>' +
      '<a class="btn" href="/api/outbox?id=' + o.id + '" download>Download</a></div>').join("")
      : '<div class="empty">Nothing yet. On the computer: NelyConnect → Phone… → Send files to phone.</div>';
    $("sharesCard").hidden = !i.shares.length;
    if (i.shares.length) {
      if (!st.share || !i.shares.some(s => s.name === st.share)) { st.share = i.shares[0].name; st.path = ""; loadList(); }
      $("chips").innerHTML = i.shares.map(s => '<button class="chip' + (s.name === st.share ? " on" : "") +
        '" data-s="' + esc(s.name) + '">' + esc(s.name) + '</button>').join("");
    }
  } catch (e) { $("status").textContent = "Can't reach the computer: " + e.message; }
}

function setTarget(to) {
  st.to = to;
  document.querySelectorAll("#target button").forEach(b => b.classList.toggle("on", b.dataset.to === to));
}

async function loadList() {
  if (!st.share) return;
  $("where").textContent = [st.share].concat(st.path ? st.path.split("/") : []).join(" / ");
  $("up").disabled = !st.path;
  try {
    const r = await api("/api/list?share=" + encodeURIComponent(st.share) + "&path=" + encodeURIComponent(st.path));
    const e = r.entries.sort((a, b) => (b.d - a.d) || a.n.localeCompare(b.n));
    $("list").innerHTML = e.length ? e.map(x => {
      const p = st.path ? st.path + "/" + x.n : x.n;
      const href = "/api/file?share=" + encodeURIComponent(st.share) + "&path=" + encodeURIComponent(p);
      return '<div class="row"><span class="ico">' + icon(x.n, x.d) + '</span><div class="name"' +
        (x.d ? ' data-dir="' + esc(p) + '" style="color:var(--navy)"' : '') + '>' + esc(x.n) +
        '<div class="meta">' + (x.d ? "folder" : fmt(x.s)) + '</div></div>' +
        '<a class="btn" href="' + href + '" download>' + (x.d ? "Zip" : "Download") + '</a></div>';
    }).join("") : '<div class="empty">Empty folder</div>';
  } catch (err) { $("list").innerHTML = '<div class="empty err">' + esc(err.message) + '</div>'; }
}

function upload(file) {
  return new Promise(resolve => {
    const row = document.createElement("div"); row.className = "row";
    const dest = st.to === "peer" ? st.info.peer : st.info.name;
    row.innerHTML = '<span class="ico">' + icon(file.name) + '</span><div class="name">' + esc(file.name) +
      '<div class="meta">to ' + esc(dest) + ' · waiting…</div><div class="bar"><i></i></div></div>';
    $("uploads").prepend(row);
    const meta = row.querySelector(".meta"), bar = row.querySelector(".bar i");
    const x = new XMLHttpRequest(); const t0 = Date.now();
    x.open("POST", "/api/upload?to=" + st.to + "&name=" + encodeURIComponent(file.name));
    x.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      bar.style.width = (100 * e.loaded / e.total) + "%";
      const s = e.loaded / Math.max((Date.now() - t0) / 1000, .001);
      meta.textContent = "to " + dest + " · " + fmt(e.loaded) + " of " + fmt(e.total) + " · " + fmt(s) + "/s";
    };
    x.onload = () => {
      const ok = x.status === 200;
      bar.style.width = "100%";
      meta.innerHTML = ok ? '<span class="ok">✓ Sent to ' + esc(dest) + '</span>'
                          : '<span class="err">✗ ' + esc((JSON.parse(x.responseText || "{}").error) || x.status) + '</span>';
      resolve();
    };
    x.onerror = () => { meta.innerHTML = '<span class="err">✗ Connection lost</span>'; resolve(); };
    x.send(file);
  });
}

$("pickBtn").onclick = () => $("files").click();
$("files").onchange = async e => { const fs = [...e.target.files]; e.target.value = ""; for (const f of fs) await upload(f); };
$("target").onclick = e => { const b = e.target.closest("button"); if (b && !b.disabled) setTarget(b.dataset.to); };
$("chips").onclick = e => { const b = e.target.closest(".chip"); if (b) { st.share = b.dataset.s; st.path = ""; refresh(); loadList(); } };
$("list").onclick = e => { const d = e.target.closest("[data-dir]"); if (d) { st.path = d.dataset.dir; loadList(); } };
$("up").onclick = () => { st.path = st.path.split("/").slice(0, -1).join("/"); loadList(); };
refresh(); setInterval(refresh, 5000);
</script>
</body></html>
"""
