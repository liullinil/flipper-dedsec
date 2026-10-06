import os, sys
from flipper_lib import Flipper, ascii_ok

port, dest = sys.argv[1], sys.argv[2]
ITEMS = ["/ext/nfc", "/ext/subghz", "/ext/infrared", "/ext/lfrfid", "/ext/badusb", "/ext/apps_manifests",
         "/ext/apps/Bluetooth/claude_remote_ble.fap", "/ext/apps/Games/myblab.fap"]
APPS_DATA_LIMIT = 3_000_000  # per sub-folder; bigger ones are listed and skipped

def nice(s):
    return s.encode("latin-1", "replace").decode("utf-8", "replace")

def local_for(p):
    return os.path.join(dest, *[nice(c) for c in p[len("/ext/"):].split("/")])

def plan_tree(f, root):
    plan = []
    for dirpath, dirs, files in f.walk(root):
        for _, name, size in files:
            if not ascii_ok(name):
                print("skip (non-ascii name):", dirpath + "/" + repr(name))
                continue
            plan.append((f"{dirpath}/{name}", size))
    return plan

n = total = 0
def fetch(f, plan):
    global n, total
    for rp, size in plan:
        lp = local_for(rp)
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        total += f.receive_file(rp, lp)
        n += 1

with Flipper(port) as f:
    for item in ITEMS:
        kind = f.stat(item)
        if kind == "F":
            fetch(f, [(item, 0)])
        elif kind == "D":
            fetch(f, plan_tree(f, item))
        else:
            print("missing:", item)
        print(f"done {item}  ({n} files, {total/1e6:.2f} MB so far)")
    skipped = []
    for t, name, size in f.list("/ext/apps_data"):
        p = f"/ext/apps_data/{name}"
        if t == "F":
            fetch(f, [(p, size)])
            continue
        if not ascii_ok(name):
            skipped.append((p, -1)); continue
        plan = plan_tree(f, p)
        sz = sum(s for _, s in plan)
        if sz > APPS_DATA_LIMIT:
            skipped.append((p, sz)); continue
        fetch(f, plan)
    print(f"done /ext/apps_data  ({n} files, {total/1e6:.2f} MB so far)")
    print("skipped apps_data folders:")
    for p, sz in skipped:
        print(f"   {sz/1e6:7.1f} MB  {p}")
print(f"backed up {n} files, {total/1e6:.2f} MB -> {dest}")
