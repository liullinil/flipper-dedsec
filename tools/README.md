# Flipper Zero USB helpers

Scripts that talk to the Flipper's built-in command line over its USB serial port (here `COM5`).
Python 3.9+ with `pyserial`; the animation tools also need `pillow` and `heatshrink2`.

| Script | Purpose |
|---|---|
| `flipper_lib.py` | Small CLI client: `list`, `walk`, `stat`, `remove`, `rmtree`, `mkdir`, `md5`, `receive_file`, `send_file` |
| `inventory.py COM5 out.txt` | Full listing of `apps`, `apps_data`, `apps_assets`, `apps_manifests`, `update` |
| `backup.py COM5 dest_dir` | Copy personal records and small `apps_data` folders to the PC |
| `cleanup.py COM5 [--dry]` | Remove everything that doesn't belong to the firmware according to `/ext/Manifest` |
| `install_update.py COM5 pkg_dir [--install]` | Upload a firmware update package to `/ext/update` with md5 checks and start it |
| `wait_port.py [sec]` | Wait for a reboot and print the firmware version |
| `build_anims.py` / `upload_anims.py` | Build and upload the Watch Dogs desktop animations (see [assets/README.md](../assets/README.md)) |
| `screen.py [sec] [dir]` | Record the Flipper screen over USB (RPC screen stream): a PNG per new frame and a GIF |
| `rpc_drive.py dir keys…` | Press buttons and grab frames in one RPC session (`ok`, `back`, `up`…; `ok_long` for a long press) |
| `press.py back [short\|long]` | Press a button through the CLI (press + short + release) |
| `watch_log.py [sec]` | Stream the firmware log, animation-manager lines |

Notes about the Flipper CLI:

- quote paths with spaces; paths with non-ASCII characters can't be addressed;
- `storage remove` deletes only files and empty folders, recursion is done on the PC;
- `storage write_chunk` appends, so a file must be removed before uploading;
- a lone `input send back short` is ignored by the GUI — it needs press → short → release (`press.py`);
- the RPC screen stream only sends frames on redraw, so a static view (e.g. the keyboard) yields no frames —
  use `rpc_drive.py`, which sends input and grabs frames in the same session.

Building Flipper apps: `ufbt` with the Unleashed SDK:

```bash
ufbt update --index-url=https://up.unleashedflip.com/directory.json --channel=release
```
