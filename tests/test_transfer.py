import os, sys, queue, time, hashlib, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nelyconnect as E
import tempfile
base=os.path.join(tempfile.gettempdir(),"nelyconnect-test"); shutil.rmtree(base, ignore_errors=True)
src=os.path.join(base,"src"); inbox=os.path.join(base,"inbox")
os.makedirs(os.path.join(src,"Project/sub/empty"))
open(os.path.join(src,"big.mov"),"wb").write(os.urandom(25*1024*1024+7))
open(os.path.join(src,"Project/a:b.txt"),"wb").write(b"hello")
open(os.path.join(src,"Project/sub/zero.bin"),"wb").write(b"")
open(os.path.join(src,"Project/.DS_Store"),"wb").write(b"x")
cfg=E.default_config(); cfg.update(port=24999, receive_folder=inbox, open_folder_on_receive=False,
  peer_addresses=[{"address":"10.255.255.1","label":"Ethernet"},{"address":"127.0.0.1","label":"Wi-Fi"}])
ev=queue.Queue(); r=E.Receiver(cfg,ev); r.start()
lm=E.LinkMonitor(cfg); print("active:", lm.check_once())
t=time.time()
for _ in range(2):
    E.send_paths("127.0.0.1",24999,[os.path.join(src,"big.mov"),os.path.join(src,"Project")],"PC")
print("secs",round(time.time()-t,2))
time.sleep(0.3)
for d,_,fs in os.walk(inbox):
    for f in fs: print(os.path.relpath(os.path.join(d,f),inbox))
    if not fs: print("dir:",os.path.relpath(d,inbox))
h=lambda p: hashlib.md5(open(p,"rb").read()).hexdigest()
assert h(os.path.join(src,"big.mov"))==h(os.path.join(inbox,"big.mov"))==h(os.path.join(inbox,"big (2).mov"))
kinds=[e[0] for e in list(ev.queue)]; print(set(kinds))
print("OK")
