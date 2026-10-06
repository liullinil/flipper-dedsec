import sys
from flipper_lib import Flipper

def nice(s):
    return s.encode('latin-1', 'replace').decode('utf-8', 'replace')


port, out_path = sys.argv[1], sys.argv[2]
roots = ["/ext/apps", "/ext/apps_data", "/ext/apps_assets", "/ext/apps_manifests", "/ext/update"]
with Flipper(port) as f, open(out_path, "w", encoding="utf-8") as out:
    for root in roots:
        total = nfiles = ndirs = 0
        for dirpath, dirs, files in f.walk(root):
            ndirs += 1
            out.write(f"{dirpath}/\n")
            for _, name, size in files:
                out.write(f"    {name}  {size}\n")
                total += size
                nfiles += 1
        out.write("\n")
        print(f"{root}: {nfiles} files, {ndirs} dirs, {total/1e6:.1f} MB")
