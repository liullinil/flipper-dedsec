# DedSec Uplink (Flipper app)

PC load, Codex and Claude sessions, a remote shell and the RF Hunter logger, over BLE to the PC
companion. Full description, settings and protocol: [../../uplink/README.md](../../uplink/README.md).

```bash
ufbt          # build -> dist/dedsec_uplink.fap
ufbt launch   # install to /ext/apps/Bluetooth and start
```

| File | Contents |
|---|---|
| `uplink.c` | UI (both orientations), BLE line protocol, alerts, settings screen, RF tab |
| `uplink_ble.c/.h` | the app's own GATT service (RX write / TX notify), advertising |
| `uplink_ota.c/.h` | self-update: receives a new `.fap` from the companion, CRC check, relaunch |
| `uplink_settings.c/.h` | settings file (`apps_data/dedsec_uplink/.uplink.settings`) |
| `uplink_fonts.h` | Latin + Cyrillic u8g2 fonts for the four text sizes |
| `rf_*.c/.h` | RF Hunter engine — see [RF_ENGINE.md](RF_ENGINE.md) |

Screens can be checked on a PC without a Flipper: [tools/uplink_render](../../tools/uplink_render).
