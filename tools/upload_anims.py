"""Upload the built animations (assets/dolphin) to the Flipper and replace the dolphin manifest.

    python tools/upload_anims.py [COM5] [--reboot] [--manifest-only]

Each animation folder on the SD card is wiped and re-uploaded, every file is checked by md5,
then /ext/dolphin/manifest.txt is replaced by assets/dolphin/manifest.txt (only our animations).
Folders from the earlier build (old names) are removed. Stock animation folders are untouched,
so the original manifest in backup/dolphin_manifest_before_custom.txt can still be restored.
"""
import os
import sys
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flipper_lib import Flipper  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "assets", "dolphin")
REMOTE = "/ext/dolphin"
OBSOLETE = ["Operator_128x64", "Breach_Cyprus_128x64"]   # first build, renamed to WD_*


def md5_local(path):
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def names_from_manifest(path):
    with open(path) as fh:
        names = [ln.split(":", 1)[1].strip() for ln in fh if ln.startswith("Name:")]
    return list(dict.fromkeys(names))   # entries repeat on purpose (weight), upload once


def validate(name):
    """Cheap sanity check of meta.txt against the frame files before anything is uploaded."""
    d = os.path.join(SRC, name)
    meta = dict(ln.split(":", 1) for ln in open(os.path.join(d, "meta.txt")) if ":" in ln)
    order = [int(x) for x in meta["Frames order"].split()]
    frames = sorted(f for f in os.listdir(d) if f.endswith(".bm"))
    assert int(meta["Passive frames"]) == len(order) <= 255, name
    assert max(order) + 1 == len(frames) == len(set(order)), name
    for f in frames:
        size = os.path.getsize(os.path.join(d, f))
        assert size <= 128 // 8 * 64 + 1, (name, f, size)
    return frames


def upload_dir(f, name):
    local = os.path.join(SRC, name)
    remote = f"{REMOTE}/{name}"
    if f.stat(remote) == "D":
        f.rmtree(remote)
    f.mkdir(remote)
    files = sorted(os.listdir(local))
    for fn in files:
        lp, rp = os.path.join(local, fn), f"{remote}/{fn}"
        f.send_file(lp, rp)
        if f.md5(rp) != md5_local(lp):
            sys.exit(f"md5 mismatch: {rp}")
    return len(files)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = args[0] if args else "COM5"
    manifest = os.path.join(SRC, "manifest.txt")
    names = names_from_manifest(manifest)
    for n in names:
        validate(n)
    print(f"{len(names)} animations validated")

    with Flipper(port) as f:
        for n in names:
            if "--manifest-only" in sys.argv:
                break
            print(f"  {n}: {upload_dir(f, n)} files, md5 OK")
        for n in OBSOLETE:
            if n not in names and f.stat(f"{REMOTE}/{n}") == "D":
                f.rmtree(f"{REMOTE}/{n}")
                print(f"  removed old {n}")
        f.send_file(manifest, f"{REMOTE}/manifest.txt")
        if f.md5(f"{REMOTE}/manifest.txt") != md5_local(manifest):
            sys.exit("manifest md5 mismatch")
        print("manifest replaced:", ", ".join(names))
        if "--reboot" in sys.argv:
            f.ser.write(b"power reboot\r")
            print("rebooting the Flipper")


if __name__ == "__main__":
    main()
