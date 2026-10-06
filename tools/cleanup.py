"""Remove everything app-related that the installed firmware does not own (per /ext/Manifest)."""
import os, sys
from flipper_lib import Flipper

port = sys.argv[1]
dry = "--dry" in sys.argv

PRUNE_ROOTS = ["/ext/apps", "/ext/apps_assets", "/ext/apps_manifests",
               "/ext/ibtnfuzzer", "/ext/rfidfuzzer", "/ext/subplaylist", "/ext/swd_scripts",
               "/ext/nfc_sniffer_logs", "/ext/music_player", "/ext/subghz_remote"]
KEEP_ROOTS = {"/ext/apps", "/ext/apps_assets", "/ext/apps_data", "/ext/update"}
APPS_DATA_KEEP = {"cli", "js_app", "nfc", "subghz", "bad_usb", "bad_kb", "totp", "unit_tests", "loader"}
LOOSE_FILES = ["/ext/spground.cfg"]

removed, kept_fw, failed = [], [], []

def log(action, path):
    print(f"{'[dry] ' if dry else ''}{action:6} {path}")

with Flipper(port) as f:
    def rm(path):
        log("rm", path)
        removed.append(path)
        if dry:
            return
        try:
            f.remove(path)
        except RuntimeError as e:
            failed.append(str(e))

    def rmdir(path):
        log("rmdir", path)
        if dry:
            return
        try:
            f.remove(path)
        except RuntimeError as e:
            failed.append(str(e))

    manifest = f.cmd('storage read "/ext/Manifest"', timeout=60)
    keep_files, keep_dirs = set(), set()
    for line in manifest.splitlines():
        line = line.strip()
        if line.startswith("F:"):
            keep_files.add("/ext/" + line.split(":", 3)[3])
        elif line.startswith("D:"):
            keep_dirs.add("/ext/" + line[2:])
    print(f"firmware manifest owns {len(keep_files)} files in {len(keep_dirs)} dirs")
    if not keep_files:
        sys.exit("manifest not readable, refusing to clean")

    def prune(root):
        """True if root is (or would be) empty after pruning."""
        try:
            entries = f.list(root)
        except RuntimeError:
            return False
        empty = True
        for t, name, _ in entries:
            p = f"{root}/{name}"
            if t == "D":
                if prune(p) and p not in keep_dirs and p not in KEEP_ROOTS:
                    rmdir(p)
                else:
                    empty = False
            elif p in keep_files:
                kept_fw.append(p)
                empty = False
            else:
                rm(p)
        return empty

    for root in PRUNE_ROOTS:
        if f.stat(root) != "D":
            continue
        if prune(root) and root not in KEEP_ROOTS and root not in keep_dirs:
            rmdir(root)

    surviving = {os.path.splitext(os.path.basename(p))[0] for p in kept_fw if p.endswith(".fap")}
    for t, name, _ in f.list("/ext/apps_data"):
        p = f"/ext/apps_data/{name}"
        if t == "D" and name not in APPS_DATA_KEEP and name not in surviving:
            log("rmtree", p)
            if not dry:
                try:
                    f.rmtree(p, errors=failed)
                except RuntimeError as e:
                    failed.append(str(e))

    for p in LOOSE_FILES:
        if f.stat(p) == "F" and p not in keep_files:
            rm(p)

    for t, name, _ in f.list("/ext/update"):
        p = f"/ext/update/{name}"
        log("rmtree" if t == "D" else "rm", p)
        if not dry:
            try:
                f.rmtree(p, errors=failed) if t == "D" else f.remove(p)
            except RuntimeError as e:
                failed.append(str(e))

    if not dry:
        f.mkdir("/ext/apps")

    print(f"\nremoved {len(removed)} files, {len(failed)} failures")
    for e in failed:
        print("  FAILED:", e)
    print(f"kept {len([p for p in kept_fw if p.startswith('/ext/apps/')])} firmware-owned apps:")
    for p in sorted(kept_fw):
        if p.startswith("/ext/apps/"):
            print("  ", p)
