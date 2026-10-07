# DedSec Uplink screen renderer

Renders every screen of the Flipper app on a PC — both orientations, all four font sizes —
without a Flipper. It compiles the app's real drawing code (`apps/dedsec_uplink/uplink.c`)
against [u8g2](https://github.com/olikraus/u8g2), the graphics library the firmware uses, with
the same text and alignment rules as the firmware's `canvas.c`. Everything else in the SDK is a
no-op stub (`fake/`). Sample data is fed through the app's own protocol parser.

![All horizontal screens](../../docs/uplink_screens.png)

```bat
tools\uplink_render\build.bat
```

The script downloads the u8g2 sources (pinned tag) on the first run, builds `render.exe` with
MSVC (Visual Studio 2022 or its Build Tools), writes one `.pbm` per screen into `out\`, then
`sheet.py` (Pillow) makes contact sheets `out\all_h.png` / `out\all_v.png` and refreshes the
images in `docs/`.

| File | What it is |
|---|---|
| `harness.c` | includes `uplink.c`, fills an `App` with sample sessions, renders each screen |
| `fake/` | stand-ins for the Flipper SDK headers; the canvas is implemented on u8g2 |
| `host_fonts.c` | the firmware's built-in fonts (`FontPrimary`, `FontSecondary`, …) from u8g2 |
| `sheet.py` | PBM frames → LCD-coloured PNGs |

The harness reaches into the app's internals (`App`, `parse_line`, `main_draw`), so a change to
those may need a matching change here.
