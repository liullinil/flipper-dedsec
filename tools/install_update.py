"""Upload a Flipper update package to /ext/update/<pkg> with md5 verification; --install starts it."""
import hashlib, os, sys, time
from flipper_lib import Flipper

port, pkg_dir = sys.argv[1], sys.argv[2]
pkg_name = os.path.basename(os.path.normpath(pkg_dir))
remote_dir = f"/ext/update/{pkg_name}"

with Flipper(port) as f:
    f.mkdir("/ext/update")
    f.mkdir(remote_dir)
    for name in sorted(os.listdir(pkg_dir)):
        lp = os.path.join(pkg_dir, name)
        rp = f"{remote_dir}/{name}"
        size = os.path.getsize(lp)
        t0 = time.time()
        f.send_file(lp, rp)
        with open(lp, "rb") as fh:
            local_md5 = hashlib.md5(fh.read()).hexdigest()
        remote_md5 = f.md5(rp)
        ok = local_md5 == remote_md5
        print(f"{name:16} {size/1e3:8.1f} KB  {time.time()-t0:5.1f}s  md5 {'OK' if ok else 'MISMATCH'}")
        if not ok:
            sys.exit(f"md5 mismatch for {name}")
    if "--install" in sys.argv:
        f.ser.write(f"update install {remote_dir}/update.fuf\r".encode())
        f._read_until(b"\r\n", 10)  # echo
        l1 = f._read_until(b"\r\n", 30).decode(errors="replace").strip()
        l2 = f._read_until(b"\r\n", 30).decode(errors="replace").strip()
        print(l1)
        print(l2)
        if not l2.startswith("OK"):
            sys.exit("update was not started")
        print("device is rebooting into the updater")
