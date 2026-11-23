import os, sys, io, queue, shutil, tempfile, time, json, zipfile, urllib.request, urllib.error, http.cookiejar
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nelyconnect as E, nelyphone as P
B=os.path.join(tempfile.gettempdir(),"nelyconnect-phonetest"); shutil.rmtree(B, ignore_errors=True)
for d in ("pc/Projects/Edit","pc/secret","inbox","out"): os.makedirs(os.path.join(B,d))
open(f"{B}/pc/Projects/cut.mov","wb").write(os.urandom(2_000_000))
open(f"{B}/pc/Projects/Edit/notes.txt","w").write("hola")
open(f"{B}/pc/secret/pw.txt","w").write("nope")
open(f"{B}/out/for_phone.pdf","wb").write(b"%PDF demo")
cfg=E.default_config(); cfg.update(port=24979, receive_folder=f"{B}/inbox", open_folder_on_receive=False,
    this_computer_name="PC", peer_name="iMac", share_files=True, phone_enabled=True, phone_port=24978,
    peer_addresses=[{"address":"127.0.0.1","label":"Ethernet"}],
    shares=[{"name":"Projects","path":f"{B}/pc/Projects","writable":False}])
ev=queue.Queue(); r=E.Receiver(cfg, ev); r.start()
class Link:
    def get(s): return {"address":"127.0.0.1","label":"Ethernet"}
    def check_once(s): return s.get()
helpers=type("H",(),{"clean_component":staticmethod(E.clean_component),"unique_name":staticmethod(E.unique_name),"send_paths":staticmethod(E.send_paths)})
ph=P.PhoneServer(cfg, r, Link(), ev, helpers, os.path.join(os.path.dirname(E.__file__),"assets"), lambda: None)
assert ph.start()
base="http://127.0.0.1:24978"
tok=cfg["phone_token"]
def get(path, opener=urllib.request.build_opener()):
    try:
        with opener.open(base+path) as resp: return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e: return e.code, e.read(), e.headers
print("no code      ->", get("/")[0])
print("wrong code   ->", get("/?k=guess")[0])
jar=http.cookiejar.CookieJar(); phone=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
code, body, _ = get("/?k="+tok, phone); print("QR scan      ->", code, "page ok:", b"Send from this phone" in body, "| cookie saved:", len(jar)==1)
info=json.loads(get("/api/info", phone)[1]); print("info         ->", info["name"], info["peer"], info["peer_online"], [s["name"] for s in info["shares"]])
def up(name, data, to="this"):
    req=urllib.request.Request(base+"/api/upload?to=%s&name=%s"%(to, urllib.parse.quote(name)), data=data, method="POST")
    with phone.open(req) as resp: return json.loads(resp.read())
import urllib.parse
photo=os.urandom(3_500_000)
print("upload photo ->", up("IMG_0042.HEIC", photo))
print("same name    ->", up("IMG_0042.HEIC", b"second"))
print("evil name    ->", up("../../evil.txt", b"x"))
print("inbox        ->", sorted(os.listdir(f"{B}/inbox")))
assert not os.path.exists(f"{B}/evil.txt") and not os.path.exists(os.path.join(tempfile.gettempdir(),"evil.txt")), "escaped the inbox!"
assert os.path.exists(f"{B}/inbox/evil.txt")
assert open(f"{B}/inbox/IMG_0042.HEIC","rb").read()==photo
print("to iMac      ->", up("clip.mp4", b"video-bytes", to="peer")); time.sleep(1.0)
print("inbox after forward ->", sorted(os.listdir(f"{B}/inbox")))
ph.add_to_outbox([f"{B}/out/for_phone.pdf", f"{B}/pc/Projects/Edit"])
info=json.loads(get("/api/info", phone)[1]); print("outbox       ->", [(o["name"],o["dir"]) for o in info["outbox"]])
c,b,h=get("/api/outbox?id="+info["outbox"][0]["id"], phone); print("download pdf ->", c, b, h["Content-Disposition"])
c,b,h=get("/api/outbox?id="+info["outbox"][1]["id"], phone); print("download dir ->", c, zipfile.ZipFile(io.BytesIO(b)).namelist())
print("list share   ->", [(e["n"],e["d"]) for e in json.loads(get("/api/list?share=Projects&path=", phone)[1])["entries"]])
c,b,_=get("/api/file?share=Projects&path=cut.mov", phone); print("file         ->", c, len(b))
print("traversal    ->", get("/api/file?share=Projects&path=../secret/pw.txt", phone)[:2])
cfg["phone_token"]="rotated"; print("old code after reset ->", get("/api/info", phone)[0]); cfg["phone_token"]=tok
cfg["phone_enabled"]=False; print("turned off   ->", get("/api/info", phone)[0])
kinds=set(e[0] for e in list(ev.queue)); print("desktop progress events:", sorted(kinds))
print("ALL OK")
