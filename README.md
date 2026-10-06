# Flipper Zero — DedSec / Watch Dogs pack

Personal Flipper Zero project in a Watch Dogs / DedSec style, for **Unleashed firmware**
(`unlshd-093c`, API 88.9). Two independent parts:

## 1. DedSec Uplink — PC / Codex / Claude monitor + pocket cmd.exe

A Flipper app plus a Windows tray companion that talk over BLE (no pairing).

- **SYS** — live CPU / RAM / network / disk load (bars with autoscale, or text), CPU history graph.
- **CDX / CLD** — state of your running **Codex** (`codex --profile …`) and **Claude Code** sessions:
  working / needs you / your turn / idle, progress, and what each is doing right now. The Flipper
  **vibrates** when a session needs your attention.
- **CMD** — a real remote shell: type a command on the Flipper, it runs on the PC (persistent
  `cmd.exe`, directory preserved), output and exit code come back. Vibrates when the console replies.
- In-app **settings**: vibration, LED, indicators (bars/text), theme (normal/inverted), font size,
  tab order. Optional **Windows autostart** and precise **Claude Code hooks**.

Code: [`apps/dedsec_uplink`](apps/dedsec_uplink) (the FAP) and [`uplink/`](uplink) (the companion).
Full docs: **[uplink/README.md](uplink/README.md)**.

## 2. Desktop animations — Watch Dogs style

Nine original 1-bit idle animations for the dolphin desktop (WATCH_DOGS wordmark, Blume/ctOS boot,
DEDSEC decrypt, hooded skull, Legion W, brush-ring W, spray WD2, LED operator mask, ctOS breach
terminal). Built from code with PIL, packed to `.bm` + `meta.txt`.

Code & details: **[assets/README.md](assets/README.md)**. Helper scripts: **[tools/README.md](tools/README.md)**.

> The animations are original pixel art *inspired by* the games — not copied Ubisoft assets — made for
> personal use on my own device.

## Build

```bash
# Flipper app
cd apps/dedsec_uplink && ufbt            # -> dist/dedsec_uplink.fap
# Animations
python tools/build_anims.py              # -> assets/dolphin/*
# PC companion
python -m pip install "bleak==0.22.3" pystray psutil pillow
pythonw uplink/dedsec_uplink.pyw
```

Target: Flipper Zero, Unleashed firmware, API 88.9.
