import os, sys, queue, shutil, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nelyconnect as E
import tempfile
B=os.path.join(tempfile.gettempdir(),"nelyconnect-sharetest"); shutil.rmtree(B, ignore_errors=True)
for d in ("mac/Movies/Clips","mac/Docs","mac/secret","pc/dl","pc/up/Folder"): os.makedirs(f"{B}/{d}")
open(f"{B}/mac/Movies/a.mov","wb").write(os.urandom(3_000_000))
open(f"{B}/mac/Movies/Clips/b.mp4","wb").write(b"clip")
open(f"{B}/mac/Movies/.hidden","w").write("x")
open(f"{B}/mac/Docs/contract.pdf","wb").write(b"pdf")
open(f"{B}/mac/secret/passwords.txt","w").write("nope")
open(f"{B}/pc/up/Folder/new.txt","w").write("uploaded")
os.symlink(f"{B}/mac/secret", f"{B}/mac/Movies/escape")
cfg=E.default_config(); cfg.update(port=24990, receive_folder=f"{B}/mac/inbox", open_folder_on_receive=False,
    share_files=True, share_password="1234", this_computer_name="iMac",
    peer_addresses=[{"address":"127.0.0.1","label":"Loop"}],
    shares=[{"name":"Movies","path":f"{B}/mac/Movies","writable":True},
            {"name":"Docs","path":f"{B}/mac/Docs","writable":False},
            {"name":"Contract","path":f"{B}/mac/Docs/contract.pdf","writable":False}])
r=E.Receiver(cfg, queue.Queue()); r.start()
A=("127.0.0.1",24990)
def call(req):
    try: return E.remote_call(*A, dict(req, pw=req.get("pw","1234")))
    except Exception as e: return "ERROR: %s" % e
print("shares:", [ (s["name"],s["writable"],s["is_file"]) for s in call({"type":"shares"})["shares"]])
print("list Movies:", [(e["n"],e["d"],e["s"]) for e in call({"type":"list","share":"Movies","path":""})["entries"]])
print("list single file:", [e["n"] for e in call({"type":"list","share":"Contract","path":""})["entries"]])
print("wrong password:", call({"type":"shares","pw":"0000"}))
print("traversal ..:", call({"type":"list","share":"Movies","path":"../secret"}))
print("symlink escape:", call({"type":"list","share":"Movies","path":"escape"}))
print("unknown share:", call({"type":"list","share":"Root","path":""}))
print("mkdir read-only:", call({"type":"mkdir","share":"Docs","path":"","name":"x"}))
print("mkdir ok:", call({"type":"mkdir","share":"Movies","path":"Clips","name":"New folder"}))
first,total=E.remote_download(*A, {"type":"get","share":"Movies","paths":["a.mov","Clips"],"pw":"1234"}, f"{B}/pc/dl")
print("download:", sorted(os.listdir(f"{B}/pc/dl")), total, "->", os.path.basename(first))
assert open(f"{B}/pc/dl/a.mov","rb").read()==open(f"{B}/mac/Movies/a.mov","rb").read()
E.remote_download(*A, {"type":"get","share":"Contract","paths":[],"pw":"1234"}, f"{B}/pc/dl")
print("download single-file share:", sorted(os.listdir(f"{B}/pc/dl")))
try: E.remote_download(*A, {"type":"get","share":"Movies","paths":["../secret/passwords.txt"],"pw":"1234"}, f"{B}/pc/dl")
except Exception as e: print("download traversal:", e)
E.remote_upload(*A, {"type":"put","share":"Movies","path":"Clips","pw":"1234","from":"PC"}, [f"{B}/pc/up/Folder"])
print("upload ->", sorted(os.listdir(f"{B}/mac/Movies/Clips")), os.listdir(f"{B}/mac/Movies/Clips/Folder"))
try: E.remote_upload(*A, {"type":"put","share":"Docs","path":"","pw":"1234"}, [f"{B}/pc/up/Folder"])
except Exception as e: print("upload read-only:", e)
cfg["share_files"]=False
print("sharing off:", call({"type":"shares"}))
cfg["share_files"]=True
# IP not in list
try: r.check_access({"pw":"1234"}, "10.9.9.9")
except PermissionError as e: print("stranger IP:", e)
print("ALL OK")
