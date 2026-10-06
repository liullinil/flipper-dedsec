"""Build the Watch Dogs style idle animations for the Flipper Zero dolphin.

    python tools/build_anims.py

Output (D:\\src\\Flipper\\assets):
    dolphin/<Name>/frame_N.bm + meta.txt   - exactly what goes to /ext/dolphin on the SD card
    dolphin/manifest.txt                   - manifest listing only these animations
    frames/<Name>/frame_N.png              - unique frames, for review / editing
    previews/<Name>.gif, sheet_<Name>.png  - LCD-coloured previews, showreel.gif plays them all

Timing: 2 fps (stock pace). Holds come from repeating frames in "Frames order".
Rotation: Unleashed has no "cycle animations" setting; the firmware picks a new random idle
animation when the current one's Duration (seconds) runs out. Duration is set to a whole number
of loops close to TARGET_SHOW_S (wdanim/core.py), so each one changes after about 30 s.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wdanim import core  # noqa: E402
from wdanim import (scene_wordmark, scene_blume, scene_dedsec, scene_hood,  # noqa: E402
                    scene_legion, scene_emblem, scene_spray, scene_operator, scene_breach)

SCENES = [scene_wordmark, scene_blume, scene_dedsec, scene_hood, scene_legion,
          scene_emblem, scene_spray, scene_operator, scene_breach]
# The firmware always adds its built-in "L1_Tv_128x47" (dolphin watching TV, weight 3,
# Duration 3600 s) to the random pool, so if it wins it stays for an hour. Weight is a uint8
# (max 255) but repeated manifest entries add up: COPIES x 255 per animation pushes the TV's
# odds from 3/48 (6 %) per switch down to ~0.01 %.
WEIGHT = 255
COPIES = 16

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
DOLPHIN = os.path.join(ASSETS, "dolphin")
FRAMES = os.path.join(ASSETS, "frames")
PREVIEWS = os.path.join(ASSETS, "previews")

MANIFEST_HEAD = "Filetype: Flipper Animation Manifest\nVersion: 1\n"
ENTRY = ("\nName: {name}\nMin butthurt: 0\nMax butthurt: 14\n"
         "Min level: 1\nMax level: 3\nWeight: {weight}\n")


def main():
    for p in (DOLPHIN, FRAMES, PREVIEWS):
        os.makedirs(p, exist_ok=True)
    anims, rows = [], []
    for scene in SCENES:
        a = scene.build()
        info = core.pack(a, DOLPHIN, FRAMES)
        core.save_gif(a, os.path.join(PREVIEWS, f"{a.name}.gif"))
        core.contact_sheet(a, os.path.join(PREVIEWS, f"sheet_{a.name}.png"))
        anims.append(a)
        rows.append(info)
    core.showreel(anims, os.path.join(PREVIEWS, "showreel.gif"))
    with open(os.path.join(DOLPHIN, "manifest.txt"), "w", newline="\n") as fh:
        fh.write(MANIFEST_HEAD + "".join(ENTRY.format(name=a.name, weight=WEIGHT)
                                         for _ in range(COPIES) for a in anims))

    print(f"{'animation':28} {'frames':>6} {'loop s':>7} {'shown s':>8} {'RAM KB':>7}")
    for r in rows:
        print(f"{r['name']:28} {r['unique']:>6} {r['loop_s']:>7.1f} {r['duration']:>8} "
              f"{r['bytes'] / 1024:>7.1f}")
    print(f"\n{len(rows)} animations -> {DOLPHIN}")


if __name__ == "__main__":
    main()
